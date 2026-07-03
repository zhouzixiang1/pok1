"""Worker agent execution logic.

Handles running individual worker LLM calls with retries and timeout isolation.
When worker target_files are disjoint, workers execute in parallel via asyncio.gather
for higher throughput. Falls back to sequential execution when files overlap or when
there is only one worker task.
"""

import json
import os
import re
import shutil
import asyncio
import logging
from pathlib import Path

log = logging.getLogger("pok.workers")

from evolution_infra import (
    run_claude_query, substitute_template, verify_code,
    locked_file, get_bot_dir, get_logs_dir,
    _target_rel, _get_worker_semaphore,
    WORKER_FAILURES_FILE, MAX_WORKER_RETRIES, WORKER_TIMEOUT,
    EXPERIENCE_FILE, find_current_v,
)
from worker_boundary import (
    audit_worker_boundary,
    diff_snapshot,
    restore_python_files,
    snapshot_python_files,
)

# Maximum number of LLM turns for the debug sub-agent (budget cap).
_DEBUG_AGENT_MAX_TURNS = 5
QUALITY_REWORK_WORKER_TIMEOUT = int(os.environ.get("POK_WORKER_QUALITY_REWORK_TIMEOUT", "600"))


def _worker_timeout_for_task(task, reviewer_feedback):
    text = " ".join([
        str(task.get("task_kind", "")),
        str(task.get("worker_id", "")),
        str(task.get("role", "")),
        str(task.get("worker_prompt", task.get("instruction", "")))[:1000],
        str(reviewer_feedback or "")[:1000],
    ]).lower()
    quality_rework_markers = (
        "quality_repair",
        "quality gate",
        "quality gates failed",
        "file_size(",
        "position_semantics(",
        "size_recovery",
        "loc limit",
    )
    if any(marker in text for marker in quality_rework_markers):
        return min(WORKER_TIMEOUT, QUALITY_REWORK_WORKER_TIMEOUT)
    return WORKER_TIMEOUT


def _is_file_scoped_quality_repair_task(task):
    if not isinstance(task, dict):
        return False
    task_kind = str(task.get("task_kind", "")).lower()
    if "quality_repair" not in task_kind:
        return False
    contract = task.get("repair_contract") if isinstance(task.get("repair_contract"), dict) else {}
    blocker = str(contract.get("blocker") or task.get("repair_blocker") or "").lower()
    if blocker not in {"file_size", "position_semantics", "quality_gate"}:
        return False
    targets = task.get("must_change_files") or task.get("target_files") or []
    contract_file = contract.get("file")
    return bool(targets or contract_file)


def _compose_worker_task_prompt(task, reviewer_feedback):
    base_prompt = task.get("worker_prompt", task.get("instruction", ""))
    if not reviewer_feedback:
        return base_prompt
    if _is_file_scoped_quality_repair_task(task):
        return (
            base_prompt
            + "\n\n# Scope Isolation\n"
            + "This worker is one file-scoped quality repair from a larger gate "
              "failure. Other blockers may exist, but they are assigned to other "
              "workers. Do not inspect, edit, or attempt to fix files outside this "
              "task's target_files/must_change_files."
        )
    return f"CRITICAL REVISION NEEDED:\n{reviewer_feedback}\n\nORIGINAL:\n{base_prompt}"


def _allowed_write_scope_for_task(task, next_dir, next_v):
    files = []
    for key in ("target_files", "files_allowed"):
        for target in task.get(key, []) or []:
            rel = _target_rel(target, next_v)
            if rel:
                files.append(next_dir / rel)
    # Deduplicate while preserving stable order for logs.
    seen = set()
    deduped = []
    for path in files:
        key = str(path)
        if key not in seen:
            seen.add(key)
            deduped.append(path)
    return {"files": deduped}


def _record_worker_failure(gen, worker_id, role, error, failure_type="unknown"):
    """Append a worker failure record to the JSONL file.

    RC5: category="worker" distinguishes real worker-exec failures from the
    reviewer/critic gate rejections that _record_quality_failure writes into the
    same file — historically 49 critic + 9 reviewer + only 1 real worker, all
    indistinguishable without this field.
    """
    entry = {"gen": gen, "worker_id": worker_id, "role": role, "error": error,
             "failure_type": failure_type, "category": "worker"}
    with locked_file(WORKER_FAILURES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    try:
        from system_log import log_system_event
        log_system_event("pipeline.worker_failed", "error",
                         f"Worker {worker_id} ({role}) failed for v{gen}",
                         {"gen": gen, "worker_id": worker_id, "role": role,
                          "error": error[:200], "category": "worker"})
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


def _infer_current_generation():
    """Best-effort current generation lookup for EXHAUSTED tiering."""
    try:
        current = find_current_v()
        return current if isinstance(current, int) and current > 0 else None
    except Exception as e:
        log.debug("Could not infer current generation for EXHAUSTED tiering: %s", e)
        return None


def _version_refs(entry):
    """Return unique generation numbers referenced by vN/claude_vN/bot-vN tokens."""
    versions = set()
    for match in re.finditer(r"\b(?:v|claude_v|bot-v)(\d+)\b", entry, re.IGNORECASE):
        try:
            versions.add(int(match.group(1)))
        except ValueError:
            pass
    return versions


def _extract_exhausted_block():
    """Read experience_pool.md and extract [POSSIBLY EXHAUSTED] entries as constraint blocks.

    Returns tiered constraint blocks:
    - <forbidden_directions>: RECENT (## RECENT_LESSONS section) EXHAUSTED entries
      that are safe to treat as hard "Do NOT implement" bans.
    - <advisory_directions>: older EXHAUSTED entries, plus single-generation RECENT
      entries from the current/previous generation. These are surfaced as historical
      cautions, NOT hard bans — they expire naturally instead of permanently
      blacklisting directions.

    The two blocks (when present) are joined with "\n\n"; returns "" if neither.
    RECENT_LESSONS can contain a just-created single-generation mechanism marked
    [POSSIBLY EXHAUSTED] before the consolidator has 3+ consecutive-generation
    evidence. Such single-generation recent entries are advisory only.
    """
    if not EXPERIENCE_FILE.exists():
        return ""

    try:
        text = EXPERIENCE_FILE.read_text(encoding="utf-8")
    except Exception:
        return ""

    current_gen = _infer_current_generation()

    # Tolerant marker: matches [POSSIBLY EXHAUSTED] AND [EXHAUSTED — hard gate]
    # (any bracketed tag containing EXHAUSTED). The old .replace() cleanup only
    # stripped "[POSSIBLY EXHAUSTED]" / "[EXHAUSTED]", leaving the "— hard gate]"
    # suffix from LLM-escalated markers as residue in the constraint block.
    marker_re = re.compile(r"\[[A-Z ]*EXHAUSTED[^\]]*\]")
    hard_lines = []        # RECENT_LESSONS section (hard ban)
    advisory_lines = []    # other sections and uncertain recent entries
    in_recent = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## RECENT_LESSONS"):
            in_recent = True
        elif stripped.startswith("## "):
            in_recent = False
        if marker_re.search(line):
            # Strip the leading markdown header markers and the marker itself
            cleaned = marker_re.sub("", line).strip(" -•")
            if cleaned:
                version_refs = _version_refs(cleaned)
                downgrade_recent = False
                if in_recent:
                    if current_gen is None:
                        # If recency cannot be judged reliably, do not create a hard ban.
                        downgrade_recent = True
                    elif len(version_refs) == 1:
                        only_gen = next(iter(version_refs))
                        downgrade_recent = only_gen >= current_gen - 1
                (advisory_lines if downgrade_recent or not in_recent else hard_lines).append(cleaned)

    blocks = []
    if hard_lines:
        hard_items = "\n".join(f"  - {entry}" for entry in hard_lines)
        blocks.append(
            "<forbidden_directions>\n"
            "These RECENT directions have enough evidence to be treated as EXHAUSTED. Do NOT implement:\n"
            f"{hard_items}\n"
            "Violating these constraints will result in automatic rejection.\n"
            "</forbidden_directions>"
        )
    if advisory_lines:
        advisory_items = "\n".join(f"  - {entry}" for entry in advisory_lines)
        blocks.append(
            "<advisory_directions>\n"
            "These directions are historical cautions or single-generation recent warnings, "
            "NOT hard bans. Revisit ONLY if combined with a NEW independent mechanism AND "
            ">=30g paired net-chips H2H evidence:\n"
            f"{advisory_items}\n"
            "</advisory_directions>"
        )
    return "\n\n".join(blocks) + "\n\n" if blocks else ""


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


def _reset_target_files_to_source(task, source_v, next_dir, next_v,
                                   baseline_snapshots=None, task_idx=None):
    """Reset only this task's target files back to a clean baseline state.

    Resolution order per target file:
    1. If `baseline_snapshots` (a {(task_idx, rel) -> str} dict) and `task_idx`
       are provided AND a snapshot exists for that key, write the snapshot
       (empty string means the file did not exist pre-worker → unlink).
       This is REQUIRED in sequential overlap mode where multiple workers may
       share a target file: rolling back to the worker's own pre-run snapshot
       preserves earlier siblings' edits, whereas rolling back to source would
       silently delete them.
    2. Otherwise fall back to source: src exists → write src; src missing &
       dst exists → unlink. If source_v / source dir is unavailable, skip
       deletion to avoid removing legitimate cross-ancestor NEW files.

    Only this task's `target_files` are touched, so disjoint parallel workers
    are unaffected even when this is called concurrently from gather().
    """
    if source_v is not None:
        src_dir = get_bot_dir(source_v)
        src_dir_exists = src_dir.exists()
    else:
        src_dir = None
        src_dir_exists = False

    have_baseline = baseline_snapshots is not None and task_idx is not None
    # LOG GAP FIX (2026-06-29): track which files were reset + the mode used, so
    # silent rollbacks are auditable (previously this function had zero logging).
    _reset_log = []
    _skip_no_source = []

    for target in task.get("target_files", []):
        rel = _target_rel(target, next_v)
        if not rel:
            continue
        dst_file = next_dir / rel

        if have_baseline and (task_idx, rel) in baseline_snapshots:
            snap = baseline_snapshots[(task_idx, rel)]
            if snap:
                dst_file.write_text(snap)
                _reset_log.append(rel + " (baseline)")
            elif dst_file.exists():
                dst_file.unlink(missing_ok=True)
                _reset_log.append(rel + " (baseline-unlink)")
            continue

        if not src_dir_exists:
            _skip_no_source.append(rel)
            continue
        src_file = src_dir / rel
        if src_file.exists():
            dst_file.write_text(src_file.read_text())
            _reset_log.append(rel + " (source)")
        elif dst_file.exists():
            dst_file.unlink(missing_ok=True)
            _reset_log.append(rel + " (source-unlink)")

    # Emit one structured event per reset call summarizing what was rolled back.
    if _reset_log or _skip_no_source:
        try:
            from system_log import log_system_event
            log_system_event(
                "pipeline.worker_files_reset", "info",
                f"v{next_v}: reset {len(_reset_log)} file(s) for task_idx={task_idx} "
                f"(mode={'baseline' if have_baseline else 'source'}, "
                f"skipped_no_source={len(_skip_no_source)})",
                {"next_v": next_v, "source_v": source_v, "task_idx": task_idx,
                 "mode": "baseline" if have_baseline else "source",
                 "reset_files": _reset_log[:20],
                 "skipped_no_source": _skip_no_source[:10]},
            )
        except Exception:
            pass


def _unlink_undeclared_new_files(next_dir, pre_run_py_files):
    """Remove .py files a worker created that were NOT present before it ran.

    Complements _reset_target_files_to_source (which only touches declared
    target_files): an Edit-tool worker can write to a path outside its declared
    target_files, and such a partial/stale undeclared NEW file would otherwise
    survive rollback and pollute the next retry or the verification step.

    Safety: only files ABSENT from `pre_run_py_files` are removed, so any file
    that existed before the worker ran (legitimate sibling edits, cross-ancestor
    files) is always preserved.
    """
    if not next_dir.is_dir() or not pre_run_py_files:
        # pre_run_py_files == empty set means "snapshot was never captured";
        # never unlink in that case (would risk removing legitimate files).
        return
    for p in next_dir.glob("*.py"):
        if p.name not in pre_run_py_files:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


def _classify_target_change(src_exists, dst_exists, src_text, dst_text):
    """Classify how a target file changed. Returns one of:
    new_file (worker created with real content, success) | invalid_target (path
    resolves nowhere, or worker wrote an empty file, failure) | deleted (existed
    now gone, failure) | unchanged (identical, failure) | modified (success).

    Branch order matters: every not-src_exists case is resolved BEFORE the
    src_text==dst_text comparison, so an empty new-file (False, True, "", "") is
    classified invalid_target, not folded into unchanged via the ""=="" equality."""
    if not src_exists:
        # Source doesn't exist — worker output lands on a new path.
        if dst_exists and dst_text:
            return "new_file"        # worker wrote real content to a brand-new file
        return "invalid_target"      # bogus path (neither side) OR empty file written
    if not dst_exists:
        return "deleted"
    if src_text == dst_text:
        return "unchanged"
    return "modified"


def _must_change_rels_for_task(task, next_v):
    """Return the normalized files this worker must actually edit.

    ``target_files`` defines the write boundary. ``must_change_files`` narrows the
    completion contract when some targets are context-only. Existing tasks do not
    set ``must_change_files``, so they retain the stricter historical behavior:
    every declared target file must change.
    """
    raw_files = task.get("must_change_files") or task.get("target_files") or []
    rels = []
    seen = set()
    for target in raw_files:
        rel = _target_rel(target, next_v)
        if rel and rel not in seen:
            seen.add(rel)
            rels.append(rel)
    return rels


def _classify_target_change_for_worker(task, task_idx, rel, next_dir, next_v,
                                       source_v=None, baseline_snapshots=None):
    """Classify a target's change relative to the worker's own pre-run baseline.

    The old check compared candidate files to the source parent. That is wrong for
    in-place crossover/precommit repairs: the candidate is already different from
    the source before the repair worker starts, so a worker that edits nothing can
    look successful. Prefer the per-worker snapshot and fall back to source only
    for older callers/tests without snapshots.
    """
    dst_file = next_dir / rel
    dst_exists = dst_file.exists()
    dst_text = dst_file.read_text() if dst_exists else ""

    if baseline_snapshots is not None and (task_idx, rel) in baseline_snapshots:
        before_text = baseline_snapshots[(task_idx, rel)]
        return _classify_target_change(True, dst_exists, before_text, dst_text)

    if source_v is None:
        return _classify_target_change(True, dst_exists, dst_text, dst_text)

    src_dir = get_bot_dir(source_v)
    src_file = src_dir / rel
    src_exists = src_file.exists()
    src_text = src_file.read_text() if src_exists else ""
    return _classify_target_change(src_exists, dst_exists, src_text, dst_text)


def _target_change_failures_for_worker(task, task_idx, next_dir, next_v,
                                       source_v=None, baseline_snapshots=None):
    """Return target files that fail the worker's must-change contract."""
    target_rels = _must_change_rels_for_task(task, next_v)
    if not target_rels or source_v is None:
        return [], []

    invalid_targets = []
    unchanged = []
    for rel in target_rels:
        change = _classify_target_change_for_worker(
            task, task_idx, rel, next_dir, next_v,
            source_v=source_v, baseline_snapshots=baseline_snapshots,
        )
        if change in ("invalid_target", "deleted"):
            invalid_targets.append(rel)
        elif change == "unchanged":
            unchanged.append(rel)
        # new_file and modified are successes.
    return invalid_targets, unchanged


def _cot_inconsistency_blocks_task(task):
    """Only hard-block CoT inconsistencies for repair tasks.

    Normal innovation workers can surface reviewer focus areas without forcing an
    immediate retry. Gate/precommit repairs are different: their whole purpose is
    to resolve exact blockers, so a claim-vs-diff mismatch is actionable failure.
    """
    text = " ".join([
        str(task.get("task_kind", "")),
        str(task.get("worker_id", "")),
        str(task.get("role", "")),
        str(task.get("worker_prompt", task.get("instruction", "")))[:1000],
    ]).lower()
    return any(marker in text for marker in (
        "quality_repair",
        "precommit_repair",
        "file_size(",
        "position_semantics(",
        "protected_contract",
    ))


async def _run_debug_agent(error_output, changed_diff, target_file, next_v, ui):
    """Run the DeepEvolve debug sub-agent to diagnose and fix a compile/crash error.

    This is a lightweight LLM call with only the Read tool (budget=5 turns max).
    It examines the error output and changed diff to produce a minimal diagnosis.

    Returns a dict with 'diagnosis', 'fix', 'confidence' keys on success,
    or an empty dict on failure. Never raises — caller relies on dict emptiness.
    """
    from llm_query import parse_json_output

    try:
        prompt_template_file = Path(__file__).resolve().parent / "prompts" / "debug_worker_prompt.md"
        if not prompt_template_file.exists():
            log.warning("Debug agent prompt not found: %s", prompt_template_file)
            return {}

        prompt_template = prompt_template_file.read_text(encoding="utf-8")

        debug_prompt = (
            f"{prompt_template}\n\n"
            f"## Error Output\n```\n{error_output[:3000]}\n```\n\n"
            f"## Changed Diff\n```\n{changed_diff[:3000]}\n```\n\n"
            f"## Target File\n{target_file}\n\n"
            f"Read the target file `{target_file}` for full context, then produce your diagnosis.\n"
            f"Return ONLY a ```json``` block with your diagnosis, fix, and confidence level."
        )

        logs_dir = get_logs_dir(next_v)
        logs_dir.mkdir(parents=True, exist_ok=True)
        debug_log = logs_dir / "debug_agent_io.txt"

        output, _cost, _usage = await run_claude_query(
            debug_prompt, [], ui,
            f"DEBUG AGENT (v{next_v})", debug_log,
            tools=["Read"],
        )

        if not output or not output.strip():
            return {}

        result = parse_json_output(output)
        if not isinstance(result, dict):
            return {}

        # Validate required keys
        if "diagnosis" not in result or "confidence" not in result:
            return {}

        # Ensure confidence is a valid value
        if result.get("confidence") not in ("high", "medium", "low"):
            result["confidence"] = "low"

        return result

    except Exception as e:
        log.warning("Debug agent failed: %s", e)
        return {}


def _preserve_timed_out_worker_if_blocker_cleared(
    task, idx, next_dir, next_v, source_v, worker_snapshots, local_snapshots,
    boundary_snapshot, boundary_allowed_files, parallel_mode, task_skipper,
    ui, worker_id, role, timeout_sec,
):
    """Accept a timed-out quality repair when current code proves its blocker cleared.

    Slow models can finish the actual file edit and then spend minutes explaining
    themselves. The outer worker watchdog may cancel that stream before the final
    assistant message arrives. For file-scoped quality repairs, the quality-gate
    skipper is the authoritative cheap validator for whether this task's blocker
    still exists. If the edit is scoped, compile-clean, and the blocker is gone,
    preserving it is safer than rolling back useful code and retrying blindly.
    """
    if task_skipper is None:
        return ""

    try:
        cleared_reason = task_skipper(task)
    except Exception as e:
        log.warning("Task skipper failed after worker timeout for %s: %s", worker_id, e)
        return ""
    if not cleared_reason:
        return ""

    snapshots = worker_snapshots or local_snapshots
    invalid_targets, unchanged = _target_change_failures_for_worker(
        task, idx, next_dir, next_v,
        source_v=source_v, baseline_snapshots=snapshots,
    )
    if invalid_targets or unchanged:
        log.info(
            "Not preserving timed-out worker %s: invalid_targets=%s unchanged=%s",
            worker_id, invalid_targets, unchanged,
        )
        return ""

    boundary_task = dict(task)
    if boundary_allowed_files:
        boundary_task["files_allowed"] = list({
            *(boundary_task.get("files_allowed", []) or []),
            *boundary_allowed_files,
        })
    boundary = audit_worker_boundary(next_dir, boundary_task, boundary_snapshot, next_v=next_v)
    if not boundary.passed:
        log.info(
            "Not preserving timed-out worker %s due boundary violations: %s",
            worker_id, boundary.violations[:3],
        )
        return ""

    if parallel_mode:
        target_names = [_target_rel(f, next_v) for f in task.get("target_files", [])]
        target_names = [rel for rel in target_names if rel]
        compile_errors = verify_code(next_dir, target_files=target_names)
    else:
        compile_errors = verify_code(next_dir)
    if compile_errors:
        log.info(
            "Not preserving timed-out worker %s due compile errors: %s",
            worker_id, compile_errors[:2],
        )
        return ""

    ui.log_history(
        f"Worker {worker_id} ({role}) timed out after {timeout_sec}s, "
        f"but its scoped edit cleared the blocker; preserving it.",
        "warn",
    )
    try:
        from system_log import log_system_event
        log_system_event(
            "pipeline.worker_timeout_preserved",
            "warn",
            f"Preserved timed-out worker {worker_id} for v{next_v}: {cleared_reason}",
            {
                "next_v": next_v,
                "source_v": source_v,
                "worker_id": worker_id,
                "role": role,
                "reason": cleared_reason,
                "timeout_sec": timeout_sec,
                "target_files": task.get("target_files", []),
                "must_change_files": task.get("must_change_files", []),
            },
        )
    except Exception:
        pass
    return cleared_reason


async def _run_single_worker(task, idx, worker_template, next_dir, next_v,
                              context_files, ui, reviewer_feedback,
                              source_v=None, parallel_mode=False, worker_snapshots=None,
                              boundary_allowed_files=None, task_skipper=None):
    """Run a single worker task with retries. Returns True on success."""
    w_id = task.get("worker_id", idx + 1)
    role = task.get("role", f"Expert Coder {w_id}")
    base_worker_prompt = _compose_worker_task_prompt(task, reviewer_feedback)

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
    worker_timeout = _worker_timeout_for_task(task, reviewer_feedback)
    allowed_write_scope = _allowed_write_scope_for_task(task, next_dir, next_v)

    compile_errors = []
    _last_reason = "unknown"
    _last_failure_type = "unknown"
    _last_error_output = ""   # fix-7: captured error output for debug agent
    _last_changed_diff = ""   # fix-7: captured changed diff for debug agent
    ui.log_history(f"Worker {w_id} ({role}) started", "info")
    # Capture this worker's own pre-run baseline if caller did not supply
    # snapshots. Retries roll back to this baseline (NOT source) so that, in
    # sequential-overlap mode where several workers share a target file, a
    # failed retry does not silently delete earlier siblings' edits.
    _local_snapshots = None
    if worker_snapshots is None:
        _local_snapshots = {}
        for target in task.get("target_files", []):
            rel = _target_rel(target, next_v)
            if rel:
                fpath = next_dir / rel
                _local_snapshots[(idx, rel)] = (
                    fpath.read_text() if fpath.exists() else ""
                )

    # Snapshot the set of .py files present in next_dir BEFORE any attempt runs.
    # On rollback this lets us unlink files the worker CREATED that were NOT in
    # its declared target_files (an Edit-tool worker can write to an undeclared
    # path). Declared NEW files are handled by the baseline-snapshot path in
    # _reset_target_files_to_source; this closes the undeclared-file gap. Only
    # files absent from the pre-run set are ever removed, so legitimate sibling
    # edits and cross-ancestor files (present pre-run) are always preserved.
    _pre_run_py_files = {p.name for p in next_dir.glob("*.py")} if next_dir.is_dir() else set()
    _boundary_snapshot = snapshot_python_files(next_dir)

    for attempt in range(MAX_WORKER_RETRIES):
        if not parallel_mode:
            ui.clear_io()
            ui.set_status(f"[{role}] coding for v{next_v}...", is_working=True)
        else:
            ui.log_history(f"[{role}] coding for v{next_v}...", "info")

        # On retry, reset to the worker's own pre-run baseline. Attempt 0 runs
        # against the live (possibly sibling-modified) state; only retries need
        # a clean slate, and baseline-based reset preserves siblings' edits.
        if attempt > 0:
            _reset_target_files_to_source(
                task, source_v, next_dir, next_v,
                baseline_snapshots=worker_snapshots or _local_snapshots, task_idx=idx,
            )
            # Also clear undeclared NEW files a prior failed attempt may have left.
            _unlink_undeclared_new_files(next_dir, _pre_run_py_files)

        attempt_note = ""
        if attempt > 0:
            attempt_note = (
                f"\n\n# Retry Context\nThis is attempt {attempt+1} of {MAX_WORKER_RETRIES}. "
                f"Previous attempt failed: {_last_reason}. "
                f"{'Consider a FUNDAMENTALLY DIFFERENT approach.' if attempt >= 2 else 'Try a different strategy.'}"
            )

            # fix-7: DeepEvolve debug sub-agent for compile/crash failures
            # Only invoke when the failure is actionable (compile/smoke/timeout) and
            # we have at least one more retry remaining after this one.
            if _last_failure_type in ("compile_error", "smoke_test_fail", "timeout") and attempt < MAX_WORKER_RETRIES - 1:
                try:
                    _target_file = task.get("target_files", ["unknown"])[0]
                    debug_result = await _run_debug_agent(
                        error_output=_last_error_output,
                        changed_diff=_last_changed_diff,
                        target_file=_target_file,
                        next_v=next_v,
                        ui=ui,
                    )
                    if debug_result.get("confidence") in ("high", "medium"):
                        attempt_note += (
                            f"\n\n[DEBUG AGENT DIAGNOSIS]: {debug_result['diagnosis']}"
                        )
                        if debug_result.get("fix"):
                            attempt_note += f"\n[PROPOSED FIX]: {debug_result['fix']}"
                        ui.log_history(
                            f"Worker {w_id} debug agent diagnosed: {debug_result['diagnosis'][:120]}",
                            "info",
                        )
                except Exception:
                    pass  # Debug agent failure must not block worker retry

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
                allowed_write_dir=allowed_write_scope,
            ))
            await asyncio.wait_for(llm_task, timeout=worker_timeout)
        except (asyncio.TimeoutError, Exception) as exc:
            if isinstance(exc, asyncio.TimeoutError):
                _last_reason = f"timed out after {worker_timeout}s (attempt {attempt+1}/{MAX_WORKER_RETRIES})"
                _last_failure_type = "timeout"
                # fix-7: capture timeout context for debug agent
                _last_error_output = f"Worker timed out after {worker_timeout}s"
                _last_changed_diff = "(timeout — partial edits rolled back)"
                ui.log_history(
                    f"Worker {w_id} ({role}) timed out after {worker_timeout}s. Retrying with simpler task...",
                    "warn",
                )
            else:
                _last_reason = f"unexpected error: {type(exc).__name__}: {str(exc)[:200]}"
                _last_failure_type = "timeout"  # treat as timeout for debug agent trigger
                _last_error_output = f"{type(exc).__name__}: {str(exc)[:500]}"
                _last_changed_diff = "(exception — partial edits rolled back)"
                ui.log_history(f"Worker {w_id} ({role}) error: {exc}", "error")
            preserve_reason = _preserve_timed_out_worker_if_blocker_cleared(
                task, idx, next_dir, next_v, source_v,
                worker_snapshots, _local_snapshots, _boundary_snapshot,
                boundary_allowed_files, parallel_mode, task_skipper,
                ui, w_id, role, worker_timeout,
            )
            if preserve_reason:
                return True
            # Roll back target files to the worker's pre-run baseline to avoid
            # partial-edit contamination. Baseline (not source) is used so
            # sequential-overlap siblings' edits are preserved.
            _reset_target_files_to_source(
                task, source_v, next_dir, next_v,
                baseline_snapshots=worker_snapshots or _local_snapshots, task_idx=idx,
            )
            # Clear undeclared NEW files the timed-out worker may have created.
            _unlink_undeclared_new_files(next_dir, _pre_run_py_files)
            restore_python_files(next_dir, _boundary_snapshot, diff_snapshot(next_dir, _boundary_snapshot))
            base_worker_prompt += (
                "\n\nPREVIOUS ATTEMPT TIMED OUT. Start fresh with a minimal, focused implementation. "
                "Implement only the single most impactful change — do NOT try to do everything at once."
            )
            continue

        # Verify required target files were actually modified (catch zero-change
        # workers and bogus paths that resolve to nothing on disk). This is
        # intentionally measured against the worker's own pre-run snapshot, not
        # the source parent, because in-place crossover/precommit repairs start
        # from a candidate that may already differ from source.
        target_rels = _must_change_rels_for_task(task, next_v)
        if target_rels and source_v is not None:
            snapshots = worker_snapshots or _local_snapshots
            invalid_targets, unchanged = _target_change_failures_for_worker(
                task, idx, next_dir, next_v,
                source_v=source_v, baseline_snapshots=snapshots,
            )
            if invalid_targets:
                _last_reason = f"invalid/deleted target files: {', '.join(invalid_targets)}"
                _last_failure_type = "invalid_target"
                base_worker_prompt += (
                    f"\n\nCRITICAL: These target paths do NOT exist on disk: {', '.join(invalid_targets)}. "
                    f"You likely wrote to a bogus path (e.g. an unstripped \"(NEW)\" suffix). "
                    f"Write each file to its PLAIN relative path, e.g. 'postflop.py' — no annotations. "
                    f"Use the Edit tool to create/edit the file at the correct path."
                )
                ui.log_history(
                    f"Worker {w_id} ({role}) invalid/deleted targets: {', '.join(invalid_targets)}",
                    "warn",
                )
                continue
            if unchanged:
                _last_reason = f"zero changes in target files: {', '.join(unchanged)}"
                _last_failure_type = "zero_changes"
                base_worker_prompt += (
                    f"\n\nCRITICAL: Your target files were NOT modified: {', '.join(unchanged)}. "
                    f"You MUST use the Edit tool to change these files. Do NOT just analyze — make actual edits."
                )
                ui.log_history(f"Worker {w_id} ({role}) zero changes in: {', '.join(unchanged)}", "warn")
                continue

        boundary_task = dict(task)
        if boundary_allowed_files:
            boundary_task["files_allowed"] = list({
                *(boundary_task.get("files_allowed", []) or []),
                *boundary_allowed_files,
            })
        boundary = audit_worker_boundary(next_dir, boundary_task, _boundary_snapshot, next_v=next_v)
        if not boundary.passed:
            _last_reason = "; ".join(boundary.violations[:3])
            _last_failure_type = "boundary_violation"
            restore_python_files(next_dir, _boundary_snapshot, boundary.changed_files)
            base_worker_prompt += (
                "\n\nCRITICAL BOUNDARY VIOLATION: You changed files outside your declared "
                "target_files/files_allowed. Only edit these files: "
                f"{', '.join(boundary.allowed_files) or '(none declared)'}. "
                f"Violations: {'; '.join(boundary.violations[:5])}"
            )
            try:
                from system_log import log_system_event
                log_system_event(
                    "pipeline.worker_boundary_violation",
                    "error",
                    f"Worker {w_id} ({role}) changed undeclared files for v{next_v}",
                    {
                        "version": next_v,
                        "worker_id": w_id,
                        "role": role,
                        "changed_files": boundary.changed_files[:20],
                        "allowed_files": boundary.allowed_files[:20],
                        "violations": boundary.violations[:10],
                    },
                )
            except Exception:
                pass
            ui.log_history(
                f"Worker {w_id} ({role}) boundary violation: {boundary.violations[0]}",
                "warn",
            )
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
            # fix-7: capture compile error output for debug agent
            _last_error_output = "\n".join(compile_errors)
            # Generate a diff-like summary of what changed for the debug agent
            _changed_files = []
            for target in task.get("target_files", []):
                rel = _target_rel(target, next_v)
                if rel:
                    dst_file = next_dir / rel
                    if dst_file.exists():
                        _changed_files.append(f"--- {rel} (modified) ---\n{dst_file.read_text()[:2000]}")
            _last_changed_diff = "\n".join(_changed_files) if _changed_files else "(no diff available)"
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
                            source_v=None, force_sequential=False,
                            task_skipper=None):
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
        if task_skipper is not None:
            try:
                skip_reason = task_skipper(tasks[0])
            except Exception as e:
                skip_reason = ""
                log.warning("Task skipper failed for worker 0: %s", e)
            if skip_reason:
                ui.log_history(
                    f"Skipping worker {tasks[0].get('worker_id', 1)}: {skip_reason}",
                    "info",
                )
                try:
                    from system_log import log_system_event
                    log_system_event(
                        "pipeline.worker_skipped_blocker_cleared",
                        "info",
                        f"Skipped worker {tasks[0].get('worker_id', 1)} for v{next_v}: {skip_reason}",
                        {"next_v": next_v, "source_v": source_v,
                         "worker_id": tasks[0].get("worker_id", 1),
                         "reason": skip_reason},
                    )
                except Exception:
                    pass
                return True, worker_snapshots, audit_focus_areas
        ok = await _run_single_worker(
            tasks[0], 0, worker_template, next_dir, next_v,
            context_files, ui, reviewer_feedback,
            source_v=source_v, worker_snapshots=worker_snapshots,
            task_skipper=task_skipper,
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
                    if _cot_inconsistency_blocks_task(tasks[0]):
                        _reset_target_files_to_source(
                            tasks[0], source_v, next_dir, next_v,
                            baseline_snapshots=worker_snapshots, task_idx=0,
                        )
                        ok = False
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

    if all_disjoint and not force_sequential:
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

        parallel_allowed_files = sorted({rel for fset in task_file_sets for rel in fset})

        # Wrap each worker call with semaphore gating for concurrency control.
        async def _gated_worker(task, i):
            sem = _get_worker_semaphore()
            async with sem:
                return await _run_single_worker(
                    task, i, worker_template, next_dir, next_v,
                    context_files, ui, reviewer_feedback,
                    source_v=source_v, parallel_mode=True,
                    worker_snapshots=worker_snapshots,
                    boundary_allowed_files=parallel_allowed_files,
                )

        results = await asyncio.gather(
            *[_gated_worker(task, i) for i, task in enumerate(tasks)],
            return_exceptions=True,
        )

        # H2 (2026-06-29): CancelledError must propagate, not be swallowed.
        # return_exceptions=True turns CancelledError into a result element; the
        # generic `isinstance(result, Exception)` branch below would then treat a
        # CYCLE_TIMEOUT/cancel as an ordinary worker failure (reset files, return
        # False) instead of unwinding the gather so the orchestrator's timeout
        # handler can take over. Detect CancelledError explicitly and re-raise.
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result

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
                _reset_target_files_to_source(
                    tasks[i], source_v, next_dir, next_v,
                    baseline_snapshots=worker_snapshots, task_idx=i,
                )
            elif not result:
                any_failed = True
                # _run_single_worker exhausted retries; reset only this worker's targets.
                _reset_target_files_to_source(
                    tasks[i], source_v, next_dir, next_v,
                    baseline_snapshots=worker_snapshots, task_idx=i,
                )

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
                    if _cot_inconsistency_blocks_task(task):
                        _reset_target_files_to_source(
                            task, source_v, next_dir, next_v,
                            baseline_snapshots=worker_snapshots, task_idx=i,
                        )
                        any_failed = True
            except Exception as e:
                log.warning("CoT audit failed for worker %d: %s", i, e)

        if any_failed:
            return False, worker_snapshots, audit_focus_areas
        return True, worker_snapshots, audit_focus_areas

    # ── Sequential fallback: target files overlap or empty sets ──
    # Snapshot each worker's target files BEFORE it runs. This way the
    # boundary check can compare each worker's input vs output, not source
    # vs output (which would include all preceding workers' changes).
    ui.log_history(f"Running {len(tasks)} workers SEQUENTIALLY (overlapping files)...", "info")
    for i, task in enumerate(tasks):
        # Capture file state before this worker runs or is skipped. When a cheap
        # quality-repair skipper observes that a blocker is already cleared, the
        # outer boundary validator still needs proof that this batch used
        # per-worker snapshots rather than a whole-candidate source diff.
        for target in task.get("target_files", []):
            rel = _target_rel(target, next_v)
            if rel:
                fpath = next_dir / rel
                worker_snapshots[(i, rel)] = fpath.read_text() if fpath.exists() else ""
        if task_skipper is not None:
            try:
                skip_reason = task_skipper(task)
            except Exception as e:
                skip_reason = ""
                log.warning("Task skipper failed for worker %d: %s", i, e)
            if skip_reason:
                ui.log_history(
                    f"Skipping worker {task.get('worker_id', i + 1)}: {skip_reason}",
                    "info",
                )
                try:
                    from system_log import log_system_event
                    log_system_event(
                        "pipeline.worker_skipped_blocker_cleared",
                        "info",
                        f"Skipped worker {task.get('worker_id', i + 1)} for v{next_v}: {skip_reason}",
                        {"next_v": next_v, "source_v": source_v, "worker_id": task.get("worker_id", i + 1),
                         "reason": skip_reason},
                    )
                except Exception:
                    pass
                continue
        ok = await _run_single_worker(
            task, i, worker_template, next_dir, next_v,
            context_files, ui, reviewer_feedback,
            source_v=source_v, worker_snapshots=worker_snapshots,
            task_skipper=task_skipper,
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
                if _cot_inconsistency_blocks_task(task):
                    _reset_target_files_to_source(
                        task, source_v, next_dir, next_v,
                        baseline_snapshots=worker_snapshots, task_idx=i,
                    )
                    return False, worker_snapshots, audit_focus_areas
        except Exception as e:
            log.warning("CoT audit failed for worker %d (sequential): %s", i, e)
    return True, worker_snapshots, audit_focus_areas
