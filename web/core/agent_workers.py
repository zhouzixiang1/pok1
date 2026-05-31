"""Worker agent execution logic.

Handles running individual worker LLM calls with retries, timeout isolation,
and parallel/serial worker scheduling.
"""

import json
import shutil
import asyncio

from evolution_infra import (
    run_claude_query, substitute_template, verify_code, run_smoke_test,
    locked_file, get_bot_dir, get_logs_dir, _get_worker_semaphore,
    find_current_v,
    WORKER_FAILURES_FILE, MAX_WORKER_RETRIES, WORKER_TIMEOUT, MAX_PARALLEL_WORKERS, _COPY_IGNORE,
)


def _record_worker_failure(gen, worker_id, role, error):
    """Append a worker failure record to the JSONL file."""
    entry = {"gen": gen, "worker_id": worker_id, "role": role, "error": error}
    with locked_file(WORKER_FAILURES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _load_recent_failures(n=3):
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


async def _run_single_worker(task, idx, worker_template, next_dir, next_v,
                              context_files, ui, reviewer_feedback,
                              source_v=None):
    """Run a single worker task with retries. Returns True on success."""
    w_id = task.get("worker_id", idx + 1)
    role = task.get("role", f"Expert Coder {w_id}")
    base_worker_prompt = task.get("worker_prompt", task.get("instruction", ""))

    if reviewer_feedback:
        base_worker_prompt = f"CRITICAL REVISION NEEDED:\n{reviewer_feedback}\n\nORIGINAL:\n{base_worker_prompt}"

    # Inject recent worker failure memory
    recent_failures = _load_recent_failures(3)
    if recent_failures:
        failure_lines = ["# Recent Worker Failures (avoid repeating these mistakes):"]
        for f in recent_failures:
            failure_lines.append(f"- Gen {f['gen']} Worker {f['worker_id']} ({f['role']}): {f['error'][:150]}")
        base_worker_prompt += "\n\n" + "\n".join(failure_lines)

    worker_log_file = get_logs_dir(next_v) / f"worker_{w_id}_io.txt"

    compile_errors = []
    smoke_errors = []
    _last_reason = "unknown"
    for attempt in range(MAX_WORKER_RETRIES):
        ui.clear_io()
        ui.set_status(f"[{role}] coding for v{next_v}...", is_working=True)

        worker_prompt = substitute_template(worker_template, {
            "role": role,
            "worker_prompt": base_worker_prompt,
            "version": str(next_v),
        })

        # ── Timeout isolation: abort and retry if worker hangs for >WORKER_TIMEOUT sec ──
        try:
            llm_task = asyncio.create_task(run_claude_query(
                worker_prompt, context_files, ui,
                f"WORKER {w_id} ({role})", worker_log_file,
                tools=["Bash", "Read", "Edit"],
            ))
            await asyncio.wait_for(llm_task, timeout=WORKER_TIMEOUT)
        except asyncio.TimeoutError:
            _last_reason = f"timed out after {WORKER_TIMEOUT}s (attempt {attempt+1}/{MAX_WORKER_RETRIES})"
            ui.log_history(
                f"Worker {w_id} ({role}) timed out after {WORKER_TIMEOUT}s. Retrying with simpler task...",
                "warn",
            )
            # Reset code directory from source to avoid half-edited state
            if source_v is not None:
                src_dir = get_bot_dir(source_v)
                if src_dir.exists() and next_dir.exists():
                    shutil.rmtree(next_dir)
                    shutil.copytree(src_dir, next_dir, ignore=_COPY_IGNORE)
                    (next_dir / ".completed").unlink(missing_ok=True)
            base_worker_prompt += (
                "\n\nPREVIOUS ATTEMPT TIMED OUT. Start fresh with a minimal, focused implementation. "
                "Implement only the single most impactful change — do NOT try to do everything at once."
            )
            continue
        except Exception as e:
            _last_reason = f"unexpected error: {type(e).__name__}: {str(e)[:200]}"
            ui.log_history(f"Worker {w_id} ({role}) error: {e}", "error")
            continue

        compile_errors = verify_code(next_dir)
        if compile_errors:
            _last_reason = f"compile error: {compile_errors[0][:200]}"
            base_worker_prompt += f"\n\nCRITICAL FIX: Fix syntax error:\n{compile_errors[0]}"
            continue

        smoke_errors = run_smoke_test(next_dir)
        if smoke_errors:
            _last_reason = f"smoke test error: {smoke_errors[0][:200]}"
            base_worker_prompt += f"\n\nCRITICAL FIX: Fix runtime error:\n{smoke_errors[0]}"
            continue

        return True

    # Worker failed all retries — record failure
    _record_worker_failure(next_v, w_id, role, _last_reason)
    return False


async def _execute_workers(tasks, worker_template, next_dir, next_v,
                            context_files, ui, reviewer_feedback,
                            source_v=None):
    """Execute worker tasks. Runs sequentially when Architect+Tuner roles coexist,
    otherwise tries parallel first with serial fallback."""
    if len(tasks) <= 1:
        # Single task — run directly
        return await _run_single_worker(
            tasks[0], 0, worker_template, next_dir, next_v,
            context_files, ui, reviewer_feedback,
            source_v=source_v,
        )

    # Check for Architect + Tuner dependency — Tuner needs Architect's output first
    has_architect = any("Architect" in t.get("role", "") for t in tasks)
    has_tuner = any("Tuner" in t.get("role", "") for t in tasks)
    if has_architect and has_tuner:
        ui.log_history("Architect + Tuner detected — running sequentially to respect dependencies.", "info")
        for i, task in enumerate(tasks):
            ok = await _run_single_worker(
                task, i, worker_template, next_dir, next_v,
                context_files, ui, reviewer_feedback,
                source_v=source_v,
            )
            if not ok:
                return False
        return True

    # Try parallel execution (capped at MAX_PARALLEL_WORKERS via semaphore)
    ui.log_history(f"Launching {len(tasks)} workers in parallel (max {MAX_PARALLEL_WORKERS} concurrent)...", "info")

    async def _guarded_worker(task, i):
        async with _get_worker_semaphore():
            return await _run_single_worker(
                task, i, worker_template, next_dir, next_v,
                context_files, ui, reviewer_feedback,
                source_v=source_v,
            )

    coros = [_guarded_worker(task, i) for i, task in enumerate(tasks)]
    results = await asyncio.gather(*coros, return_exceptions=True)

    all_ok = all(r is True for r in results)
    if all_ok:
        return True

    # Parallel had issues — fall back to serial with fresh copy
    ui.log_history("Parallel execution had issues, retrying serially with fresh code copy...", "warn")
    _source = source_v if source_v is not None else find_current_v()
    src_dir = get_bot_dir(_source)
    if next_dir.exists():
        shutil.rmtree(next_dir)
    shutil.copytree(src_dir, next_dir, ignore=_COPY_IGNORE)
    (next_dir / ".completed").unlink(missing_ok=True)

    # Append note so serial workers know prior failures may not apply to fresh copy
    serial_reviewer_feedback = (reviewer_feedback or "") + (
        "\n\nNOTE: Previous parallel attempt failed and code was reset from source. "
        "Prior error messages may reference issues that no longer exist — focus on the current code state."
    )

    for i, task in enumerate(tasks):
        ok = await _run_single_worker(
            task, i, worker_template, next_dir, next_v,
            context_files, ui, serial_reviewer_feedback,
            source_v=source_v,
        )
        if not ok:
            return False
    return True
