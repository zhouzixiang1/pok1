"""Worker agent execution logic.

Handles running individual worker LLM calls with retries and timeout isolation.
Workers execute in parallel when target files don't overlap, falling back to
sequential execution when they do.
"""

import json
import shutil
import asyncio

from evolution_infra import (
    run_claude_query, substitute_template, verify_code, run_smoke_test,
    locked_file, get_bot_dir, get_logs_dir,
    _target_rel, get_model_for_role,
    WORKER_FAILURES_FILE, MAX_WORKER_RETRIES, WORKER_TIMEOUT, _COPY_IGNORE,
)


def _record_worker_failure(gen, worker_id, role, error, failure_type="unknown"):
    """Append a worker failure record to the JSONL file."""
    entry = {"gen": gen, "worker_id": worker_id, "role": role, "error": error, "failure_type": failure_type}
    with locked_file(WORKER_FAILURES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    try:
        from system_log import log_system_event
        log_system_event("pipeline.worker_failed", "error",
                         f"Worker {worker_id} ({role}) failed for v{gen}",
                         {"gen": gen, "worker_id": worker_id, "role": role, "error": error[:200]})
    except Exception:
        pass


def _load_recent_failures(n=5):
    """Load the n most recent worker failure records."""
    if not WORKER_FAILURES_FILE.exists():
        return []
    entries = []
    with locked_file(WORKER_FAILURES_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return entries[-n:]


def _files_overlap(tasks):
    """Check if any two tasks share target files (normalized)."""
    all_targets = set()
    for task in tasks:
        task_targets = set()
        for f in task.get("target_files", []):
            rel = f.replace("\\", "/")
            # Normalize: strip leading bots/claude_vN/ prefix if present
            parts = rel.split("/")
            if len(parts) > 2 and parts[0] == "bots" and parts[1].startswith("claude_v"):
                rel = "/".join(parts[2:])
            task_targets.add(rel)
        if task_targets & all_targets:
            return True
        all_targets |= task_targets
    return False


async def _run_single_worker(task, idx, worker_template, next_dir, next_v,
                              context_files, ui, reviewer_feedback,
                              source_v=None):
    """Run a single worker task with retries. Returns (bool, int) = (success, task_idx)."""
    w_id = task.get("worker_id", idx + 1)
    role = task.get("role", f"Expert Coder {w_id}")
    base_worker_prompt = task.get("worker_prompt", task.get("instruction", ""))

    if reviewer_feedback:
        base_worker_prompt = f"CRITICAL REVISION NEEDED:\n{reviewer_feedback}\n\nORIGINAL:\n{base_worker_prompt}"

    # Inject recent worker failure memory
    recent_failures = _load_recent_failures(5)
    if recent_failures:
        failure_lines = ["# Recent Worker Failures (avoid repeating these mistakes):"]
        for f in recent_failures:
            failure_lines.append(f"- Gen {f['gen']} Worker {f['worker_id']} ({f['role']}): {f['error'][:300]}")
        base_worker_prompt += "\n\n" + "\n".join(failure_lines)

    worker_log_file = get_logs_dir(next_v) / f"worker_{w_id}_io.txt"

    compile_errors = []
    smoke_errors = []
    _last_reason = "unknown"
    _last_failure_type = "unknown"
    ui.log_history(f"Worker {w_id} ({role}) started", "info")
    for attempt in range(MAX_WORKER_RETRIES):
        ui.clear_io()
        ui.set_status(f"[{role}] coding for v{next_v}...", is_working=True)

        attempt_note = ""
        if attempt > 0:
            attempt_note = (
                f"\n\n# Retry Context\nThis is attempt {attempt+1} of {MAX_WORKER_RETRIES}. "
                f"Previous attempt failed: {_last_reason}. "
                f"{'Consider a FUNDAMENTALLY DIFFERENT approach.' if attempt >= 2 else 'Try a different strategy.'}"
            )

        worker_prompt = substitute_template(worker_template, {
            "role": role,
            "worker_prompt": base_worker_prompt + attempt_note,
            "version": str(next_v),
            "parent_version": str(source_v),
        })

        # ── Timeout isolation: abort and retry if worker hangs for >WORKER_TIMEOUT sec ──
        try:
            llm_task = asyncio.create_task(run_claude_query(
                worker_prompt, context_files, ui,
                f"WORKER {w_id} ({role})", worker_log_file,
                model=get_model_for_role("worker"),
                tools=["Bash", "Read", "Edit"],
            ))
            await asyncio.wait_for(llm_task, timeout=WORKER_TIMEOUT)
        except (asyncio.TimeoutError, Exception) as exc:
            if isinstance(exc, asyncio.TimeoutError):
                _last_reason = f"timed out after {WORKER_TIMEOUT}s (attempt {attempt+1}/{MAX_WORKER_RETRIES})"
                _last_failure_type = "timeout"
                ui.log_history(
                    f"Worker {w_id} ({role}) timed out after {WORKER_TIMEOUT}s. Retrying with simpler task...",
                    "warn",
                )
            else:
                _last_reason = f"unexpected error: {type(exc).__name__}: {str(exc)[:200]}"
                ui.log_history(f"Worker {w_id} ({role}) error: {exc}", "error")
            # Note: do NOT reset next_dir here during parallel execution — other workers
            # may still be editing files. The serial fallback path handles full reset.
            base_worker_prompt += (
                "\n\nPREVIOUS ATTEMPT TIMED OUT. Start fresh with a minimal, focused implementation. "
                "Implement only the single most impactful change — do NOT try to do everything at once."
            )
            continue

        # Verify target files were actually modified (catch zero-change workers)
        target_rels = [_target_rel(f, next_v) for f in task.get("target_files", [])]
        target_rels = [r for r in target_rels if r]
        if target_rels and source_v is not None:
            src_dir = get_bot_dir(source_v)
            unchanged = []
            for rel in target_rels:
                src_file = src_dir / rel
                dst_file = next_dir / rel
                src_text = src_file.read_text() if src_file.exists() else ""
                dst_text = dst_file.read_text() if dst_file.exists() else ""
                if src_text == dst_text:
                    unchanged.append(rel)
            if unchanged:
                _last_reason = f"zero changes in target files: {', '.join(unchanged)}"
                _last_failure_type = "zero_changes"
                base_worker_prompt += (
                    f"\n\nCRITICAL: Your target files were NOT modified: {', '.join(unchanged)}. "
                    f"You MUST use the Edit tool to change these files. Do NOT just analyze — make actual edits."
                )
                ui.log_history(f"Worker {w_id} ({role}) zero changes in: {', '.join(unchanged)}", "warn")
                continue

        compile_errors = verify_code(next_dir)
        if compile_errors:
            _last_reason = f"compile error: {compile_errors[0][:200]}"
            _last_failure_type = "compile_error"
            base_worker_prompt += f"\n\nCRITICAL FIX: Fix syntax error:\n{compile_errors[0]}"
            continue

        smoke_errors = run_smoke_test(next_dir)
        if smoke_errors:
            _last_reason = f"smoke test error: {smoke_errors[0][:200]}"
            _last_failure_type = "smoke_error"
            base_worker_prompt += f"\n\nCRITICAL FIX: Fix runtime error:\n{smoke_errors[0]}"
            continue

        ui.log_history(f"Worker {w_id} ({role}) done", "info")
        return True, idx

    # Worker failed all retries — record failure
    _record_worker_failure(next_v, w_id, role, _last_reason, failure_type=_last_failure_type)
    return False, idx


def _snapshot_target_files(tasks, next_dir, next_v):
    """Capture file content for all task target files before any worker runs."""
    snapshots = {}
    for i, task in enumerate(tasks):
        for target in task.get("target_files", []):
            rel = _target_rel(target, next_v)
            if rel:
                fpath = next_dir / rel
                snapshots[(i, rel)] = fpath.read_text() if fpath.exists() else ""
    return snapshots


async def _execute_workers(tasks, worker_template, next_dir, next_v,
                            context_files, ui, reviewer_feedback,
                            source_v=None):
    """Execute worker tasks — in parallel when target files don't overlap.

    Returns (success, worker_snapshots) where worker_snapshots maps
    (task_idx, file_rel) -> file_content_before_worker_ran, used for
    accurate per-worker boundary validation.
    """
    # Always capture snapshots before any worker runs
    worker_snapshots = _snapshot_target_files(tasks, next_dir, next_v)

    if len(tasks) <= 1:
        # Single task — just run it
        ok, _ = await _run_single_worker(
            tasks[0], 0, worker_template, next_dir, next_v,
            context_files, ui, reviewer_feedback,
            source_v=source_v,
        )
        return ok, worker_snapshots

    # Check for file overlap — if found, fall back to sequential
    if _files_overlap(tasks):
        ui.log_history(f"Target files overlap — running {len(tasks)} workers sequentially...", "info")
        for i, task in enumerate(tasks):
            ok, _ = await _run_single_worker(
                task, i, worker_template, next_dir, next_v,
                context_files, ui, reviewer_feedback,
                source_v=source_v,
            )
            if not ok:
                return False, worker_snapshots
        return True, worker_snapshots

    # Parallel execution — all target files are disjoint
    ui.log_history(f"Running {len(tasks)} workers in parallel (no file overlap)...", "info")
    results = await asyncio.gather(
        *[
            _run_single_worker(
                task, i, worker_template, next_dir, next_v,
                context_files, ui, reviewer_feedback,
                source_v=source_v,
            )
            for i, task in enumerate(tasks)
        ],
        return_exceptions=True,
    )

    # Process results — check for failures
    all_ok = True
    for result in results:
        if isinstance(result, Exception):
            ui.log_history(f"Worker parallel execution error: {result}", "error")
            all_ok = False
        elif isinstance(result, tuple):
            ok, idx = result
            if not ok:
                all_ok = False
        else:
            all_ok = False

    return all_ok, worker_snapshots
