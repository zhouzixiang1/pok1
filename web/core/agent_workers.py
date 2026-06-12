"""Worker agent execution logic.

Handles running individual worker LLM calls with retries and timeout isolation.
When worker target_files are disjoint, workers execute in parallel via asyncio.gather
for higher throughput. Falls back to sequential execution when files overlap or when
there is only one worker task.
"""

import json
import shutil
import asyncio
import logging

log = logging.getLogger("pok.workers")

from evolution_infra import (
    run_claude_query, substitute_template, verify_code,
    locked_file, get_bot_dir, get_logs_dir,
    _target_rel, _get_worker_semaphore,
    WORKER_FAILURES_FILE, MAX_WORKER_RETRIES, WORKER_TIMEOUT,
    EXPERIENCE_FILE,
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
    except Exception as e:
        log.warning("Failed to log worker failure event: %s", e)


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
                except json.JSONDecodeError as e:
                    log.debug("Malformed worker failure entry: %s", line[:80])
    return entries[-n:]


def _extract_exhausted_block():
    """Read experience_pool.md and extract [POSSIBLY EXHAUSTED] entries as a constraint block.

    Returns a formatted forbidden_directions XML block string, or empty string
    if no EXHAUSTED entries are found. This block is prepended to worker prompts
    so workers cannot claim they never saw the constraints.
    """
    if not EXPERIENCE_FILE.exists():
        return ""

    try:
        text = EXPERIENCE_FILE.read_text(encoding="utf-8")
    except Exception:
        return ""

    exhausted_lines = []
    for line in text.splitlines():
        if "EXHAUSTED]" in line:
            # Strip the leading markdown header markers and the marker itself
            cleaned = line.replace("[POSSIBLY EXHAUSTED]", "").replace("[EXHAUSTED]", "").strip(" -•")
            if cleaned:
                exhausted_lines.append(cleaned)

    if not exhausted_lines:
        return ""

    items = "\n".join(f"  - {entry}" for entry in exhausted_lines)
    return (
        "<forbidden_directions>\n"
        "These evolution directions are EXHAUSTED. Do NOT implement changes in these areas:\n"
        f"{items}\n"
        "Violating these constraints will result in automatic rejection.\n"
        "</forbidden_directions>\n\n"
    )


def _target_rel_set(task, next_v):
    """Extract the set of relative file paths from a task's target_files.

    Returns a set of strings (relative paths within the bot directory) that
    the worker is expected to modify. Used for disjointness checks to decide
    whether parallel execution is safe.
    """
    result = set()
    for target in task.get("target_files", []):
        rel = _target_rel(target, next_v)
        if rel:
            result.add(rel)
    return result


async def _run_single_worker(task, idx, worker_template, next_dir, next_v,
                              context_files, ui, reviewer_feedback,
                              source_v=None, parallel_mode=False):
    """Run a single worker task with retries. Returns True on success."""
    w_id = task.get("worker_id", idx + 1)
    role = task.get("role", f"Expert Coder {w_id}")
    base_worker_prompt = task.get("worker_prompt", task.get("instruction", ""))

    if reviewer_feedback:
        base_worker_prompt = f"CRITICAL REVISION NEEDED:\n{reviewer_feedback}\n\nORIGINAL:\n{base_worker_prompt}"

    # Inject EXHAUSTED constraint block from experience pool.
    # Prepended (not appended) so it appears before the worker's task instructions
    # and cannot be missed or dismissed as a footnote.
    exhausted_block = _extract_exhausted_block()
    if exhausted_block:
        base_worker_prompt = exhausted_block + base_worker_prompt

    # Inject recent worker failure memory
    recent_failures = _load_recent_failures(5)
    if recent_failures:
        failure_lines = ["# Recent Worker Failures (avoid repeating these mistakes):"]
        for f in recent_failures:
            failure_lines.append(f"- Gen {f['gen']} Worker {f['worker_id']} ({f.get('role', 'unknown')}): {f['error'][:300]}")
        base_worker_prompt += "\n\n" + "\n".join(failure_lines)

    worker_log_file = get_logs_dir(next_v) / f"worker_{w_id}_io.txt"

    compile_errors = []
    _last_reason = "unknown"
    _last_failure_type = "unknown"
    ui.log_history(f"Worker {w_id} ({role}) started", "info")
    for attempt in range(MAX_WORKER_RETRIES):
        if not parallel_mode:
            ui.clear_io()
            ui.set_status(f"[{role}] coding for v{next_v}...", is_working=True)
        else:
            ui.log_history(f"[{role}] coding for v{next_v}...", "info")

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
            # Roll back target files from source to avoid partial-edit contamination.
            # Workers run sequentially, so this is safe.
            if source_v is not None:
                src_dir = get_bot_dir(source_v)
                for target in task.get("target_files", []):
                    rel = _target_rel(target, next_v)
                    if rel:
                        src_file = src_dir / rel
                        dst_file = next_dir / rel
                        if src_file.exists():
                            dst_file.write_text(src_file.read_text())
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

        if parallel_mode:
            _target_names = [_target_rel(f, next_v) for f in task.get("target_files", [])]
            _target_names = [r for r in _target_names if r]
            compile_errors = verify_code(next_dir, target_files=_target_names)
        else:
            compile_errors = verify_code(next_dir)
        if compile_errors:
            _last_reason = f"compile error: {compile_errors[0][:200]}"
            _last_failure_type = "compile_error"
            base_worker_prompt += f"\n\nCRITICAL FIX: Fix syntax error:\n{compile_errors[0]}"
            continue

        # Smoke test is NOT run here — it is deferred to the quality gate
        # (run_quality_gates in tool_gates.py) to save ~60-120s per retry attempt.
        ui.log_history(f"Worker {w_id} ({role}) done", "info")
        return True

    # Worker failed all retries — record failure
    _record_worker_failure(next_v, w_id, role, _last_reason, failure_type=_last_failure_type)
    return False


async def _execute_workers(tasks, worker_template, next_dir, next_v,
                            context_files, ui, reviewer_feedback,
                            source_v=None):
    """Execute worker tasks, capturing per-worker file snapshots.

    When all workers have disjoint target_files, executes in parallel via
    asyncio.gather for higher throughput. Falls back to sequential execution
    when target files overlap or any task has no target_files.

    Returns (success, worker_snapshots, audit_focus_areas) where worker_snapshots maps
    (task_idx, file_rel) -> file_content_before_worker_ran, used for
    accurate per-worker boundary validation. audit_focus_areas contains
    focus areas from P0-2 Worker CoT checks to inject into Reviewer.
    """
    # Snapshots: (task_idx, file_rel) -> file content before that worker ran.
    # This enables the boundary validator to check only the Tuner's own changes
    # rather than seeing all preceding workers' changes mixed in.
    worker_snapshots = {}
    audit_focus_areas = []  # P0-2: Collected from Worker CoT checks

    if len(tasks) <= 1:
        # Single task — snapshot before running
        for target in tasks[0].get("target_files", []):
            rel = _target_rel(target, next_v)
            if rel:
                fpath = next_dir / rel
                worker_snapshots[(0, rel)] = fpath.read_text() if fpath.exists() else ""
        ok = await _run_single_worker(
            tasks[0], 0, worker_template, next_dir, next_v,
            context_files, ui, reviewer_feedback,
            source_v=source_v,
        )
        # P0-2: Worker CoT consistency check
        if ok:
            try:
                from audit_agents import _run_worker_cot_check
                cot = await _run_worker_cot_check(
                    tasks[0], 0, next_v, source_v, next_dir, worker_snapshots, ui
                )
                if not cot.get("cot_consistent", True):
                    audit_focus_areas.extend(cot.get("focus_areas", []))
            except Exception as e:
                log.warning("CoT audit failed for worker 0: %s", e)
        return ok, worker_snapshots, audit_focus_areas

    # ── Disjointness check: can we safely run workers in parallel? ──
    # Compute per-task target file sets and check for intersections.
    task_file_sets = [_target_rel_set(task, next_v) for task in tasks]
    all_disjoint = True
    seen = set()
    for i, fset in enumerate(task_file_sets):
        if not fset:
            # A task with no target files cannot be parallelized safely
            # (its edits are unpredictable).
            all_disjoint = False
            break
        if fset & seen:
            all_disjoint = False
            break
        seen |= fset

    if all_disjoint:
        # ── Parallel path: all target_files are disjoint ──
        # Pre-snapshot all target files at once — safe because no two workers
        # touch the same file.
        ui.log_history(
            f"Running {len(tasks)} workers in PARALLEL (disjoint target files)...", "info"
        )
        for i, task in enumerate(tasks):
            for target in task.get("target_files", []):
                rel = _target_rel(target, next_v)
                if rel:
                    fpath = next_dir / rel
                    worker_snapshots[(i, rel)] = (
                        fpath.read_text() if fpath.exists() else ""
                    )

        # Wrap each worker call with semaphore gating for concurrency control.
        async def _gated_worker(task, i):
            sem = _get_worker_semaphore()
            async with sem:
                return await _run_single_worker(
                    task, i, worker_template, next_dir, next_v,
                    context_files, ui, reviewer_feedback,
                    source_v=source_v, parallel_mode=True,
                )

        results = await asyncio.gather(
            *[_gated_worker(task, i) for i, task in enumerate(tasks)],
            return_exceptions=True,
        )

        # Check results — roll back failed workers' target files from source.
        # Since files are disjoint, rolling back one worker cannot corrupt another.
        any_failed = False
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                any_failed = True
                ui.log_history(
                    f"Worker {tasks[i].get('worker_id', i+1)} raised exception: {result}",
                    "error",
                )
                # Roll back this worker's target files from source
                if source_v is not None:
                    src_dir = get_bot_dir(source_v)
                    for target in tasks[i].get("target_files", []):
                        rel = _target_rel(target, next_v)
                        if rel:
                            src_file = src_dir / rel
                            dst_file = next_dir / rel
                            if src_file.exists():
                                dst_file.write_text(src_file.read_text())
            elif not result:
                any_failed = True
                # _run_single_worker exhausted retries without rolling back
                if source_v is not None:
                    src_dir = get_bot_dir(source_v)
                    for target in tasks[i].get("target_files", []):
                        rel = _target_rel(target, next_v)
                        if rel:
                            src_file = src_dir / rel
                            dst_file = next_dir / rel
                            if src_file.exists():
                                dst_file.write_text(src_file.read_text())

        if any_failed:
            return False, worker_snapshots, audit_focus_areas

        # P0-2: Run Worker CoT checks sequentially (they are fast, read-only).
        for i, task in enumerate(tasks):
            try:
                from audit_agents import _run_worker_cot_check
                cot = await _run_worker_cot_check(
                    task, i, next_v, source_v, next_dir, worker_snapshots, ui
                )
                if not cot.get("cot_consistent", True):
                    audit_focus_areas.extend(cot.get("focus_areas", []))
            except Exception as e:
                log.warning("CoT audit failed for worker %d: %s", i, e)

        return True, worker_snapshots, audit_focus_areas

    # ── Sequential fallback: target files overlap or empty sets ──
    # Snapshot each worker's target files BEFORE it runs. This way the
    # boundary check can compare each worker's input vs output, not source
    # vs output (which would include all preceding workers' changes).
    ui.log_history(f"Running {len(tasks)} workers SEQUENTIALLY (overlapping files)...", "info")
    for i, task in enumerate(tasks):
        # Capture file state before this worker runs
        for target in task.get("target_files", []):
            rel = _target_rel(target, next_v)
            if rel:
                fpath = next_dir / rel
                worker_snapshots[(i, rel)] = fpath.read_text() if fpath.exists() else ""
        ok = await _run_single_worker(
            task, i, worker_template, next_dir, next_v,
            context_files, ui, reviewer_feedback,
            source_v=source_v,
        )
        if not ok:
            return False, worker_snapshots, audit_focus_areas
        # P0-2: Worker CoT consistency check after each successful worker
        try:
            from audit_agents import _run_worker_cot_check
            cot = await _run_worker_cot_check(
                task, i, next_v, source_v, next_dir, worker_snapshots, ui
            )
            if not cot.get("cot_consistent", True):
                audit_focus_areas.extend(cot.get("focus_areas", []))
        except Exception as e:
            log.warning("CoT audit failed for worker %d (sequential): %s", i, e)
    return True, worker_snapshots, audit_focus_areas
