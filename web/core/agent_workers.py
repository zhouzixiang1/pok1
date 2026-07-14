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
import hashlib
import logging
import stat
from pathlib import Path

log = logging.getLogger("pok.workers")

from bot_namespace import bot_relpath
from evolution_infra import (
    run_claude_query, substitute_template, verify_code,
    locked_file, get_bot_dir, get_logs_dir,
    _target_rel, _get_worker_semaphore,
    WORKER_FAILURES_FILE, MAX_WORKER_RETRIES, WORKER_TIMEOUT,
)
from worker_boundary import (
    ArtifactSnapshotError,
    allowed_files_for_task,
    audit_worker_boundary,
    diff_snapshot,
    is_binary_artifact_path,
    read_regular_file_bytes,
    restore_python_files,
    snapshot_python_files,
)
from llm_availability import LLMAvailabilityBlocked, gather_llm_fail_fast

# Maximum number of LLM turns for the debug sub-agent (budget cap).
_DEBUG_AGENT_MAX_TURNS = 5
QUALITY_REWORK_WORKER_TIMEOUT = int(os.environ.get("POK_WORKER_QUALITY_REWORK_TIMEOUT", "600"))


class WorkerInfrastructureError(RuntimeError):
    """The worker role produced no code verdict because its LLM transport failed."""

    def __init__(self, worker_id, role, issues):
        self.worker_id = worker_id
        self.role = role
        self.issues = [str(item)[:500] for item in issues]
        super().__init__(
            f"worker {worker_id} ({role}) infrastructure unavailable: "
            + "; ".join(self.issues[:3])
        )


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


def _is_file_scoped_precommit_repair_task(task):
    if not isinstance(task, dict):
        return False
    task_kind = str(task.get("task_kind", "")).lower()
    if "precommit_repair" not in task_kind:
        return False
    contract = task.get("repair_contract") if isinstance(task.get("repair_contract"), dict) else {}
    blocker = str(contract.get("blocker") or task.get("repair_blocker") or "").lower()
    if blocker != "precommit_regression":
        return False
    targets = [
        target for target in (task.get("must_change_files") or task.get("target_files") or [])
        if target
    ]
    return len(targets) == 1


def _runtime_contract_block(task):
    contract = task.get("runtime_contract")
    if not isinstance(contract, dict) or not contract:
        return ""
    lines = ["# Runtime Contract"]
    reference_pack_id = str(contract.get("reference_pack_id") or "").strip()
    if reference_pack_id:
        try:
            from strategy_reference_pack import worker_reference_card

            card = worker_reference_card(reference_pack_id)
        except Exception as exc:
            card = (
                "# Binding Local Strategy Reference Card\n"
                f"- unavailable for {reference_pack_id}: {type(exc).__name__}: {str(exc)[:160]}"
            )
        if card:
            lines.append(card)
    decision = contract.get("decision") if isinstance(contract.get("decision"), dict) else None
    if decision:
        lines.append(
            "- Decision timing: "
            f"clock={decision.get('clock')}, "
            f"hard_deadline={decision.get('hard_deadline_ms')} ms, "
            f"baseline_target={decision.get('baseline_target_ms')} ms, "
            f"refinement_budget={decision.get('refinement_budget_ms')} ms."
        )
        lines.append(f"- Baseline path: {decision.get('baseline_path')}.")
        lines.append(f"- Fallback action: {decision.get('fallback_action')}.")
        lines.append(f"- Refinement bound: {decision.get('refinement_bound')}.")
        if decision.get("max_samples") is not None:
            lines.append(f"- Maximum refinement samples: {decision.get('max_samples')}.")
    artifacts = contract.get("precompute_artifacts") or []
    if artifacts:
        for artifact in artifacts[:4]:
            if isinstance(artifact, dict):
                lines.append(
                    "- Precompute artifact: "
                    f"{artifact.get('name')} in {artifact.get('owner_file')}, "
                    f"phase={artifact.get('build_phase')}, max_build_ms={artifact.get('max_build_ms')}, "
                    f"max_entries={artifact.get('max_entries')}, "
                    f"max_bytes={artifact.get('max_bytes')}, key={artifact.get('key_shape')}, "
                    f"consumer={artifact.get('consumer')}, fallback={artifact.get('fallback')}."
                )
    memory = contract.get("match_memory") if isinstance(contract.get("match_memory"), dict) else None
    if memory:
        lines.append(
            "- Match memory: "
            f"{memory.get('tracker_class')} in {memory.get('owner_file')}; "
            f"reset={memory.get('reset_boundary')}; events={memory.get('update_events')}; "
            f"snapshot={memory.get('snapshot_field')}; recent_hands<={memory.get('max_recent_hands')}."
        )
        lines.append(
            f"- Adaptation: prior={memory.get('prior_rule')}; confidence={memory.get('confidence_rule')}; "
            f"cap={memory.get('adaptation_cap')}; consumer={memory.get('consumer')}."
        )
    refs = contract.get("official_feedback_refs") or []
    if refs:
        lines.append("- Official feedback refs: " + ", ".join(str(item) for item in refs[:8]) + ".")
    forbidden = contract.get("forbidden_runtime_work") or []
    if forbidden:
        lines.append("- Forbidden runtime work: " + ", ".join(str(item) for item in forbidden[:8]) + ".")
    lines.append(
        "Mirror these points in the code change and checks; do not treat this block as optional context."
    )
    return "\n".join(lines)


def _compose_worker_task_prompt(task, reviewer_feedback):
    base_prompt = task.get("worker_prompt", task.get("instruction", ""))
    contract_block = _runtime_contract_block(task)
    if contract_block:
        base_prompt = base_prompt + "\n\n" + contract_block
    if not reviewer_feedback:
        return base_prompt
    if _is_file_scoped_quality_repair_task(task) or _is_file_scoped_precommit_repair_task(task):
        return (
            base_prompt
            + "\n\n# Scope Isolation\n"
            + "This worker is one file-scoped repair from a larger gate "
              "failure. Other blockers may exist, but they are assigned to other "
              "workers. Do not inspect, edit, or attempt to fix files outside this "
              "task's target_files/must_change_files."
        )
    return f"CRITICAL REVISION NEEDED:\n{reviewer_feedback}\n\nORIGINAL:\n{base_prompt}"


def _allowed_write_scope_for_task(task, next_dir, next_v):
    return {
        "files": [
            next_dir / rel
            for rel in allowed_files_for_task(task, next_v)
        ]
    }


def _record_worker_failure(gen, worker_id, role, error, failure_type="unknown"):
    """Append a worker failure record to the JSONL file.

    RC5: category="worker" distinguishes real worker-exec failures from the
    reviewer/critic gate rejections that _record_quality_failure writes into the
    same file — historically 49 critic + 9 reviewer + only 1 real worker, all
    indistinguishable without this field.
    """
    entry = {"gen": gen, "worker_id": worker_id, "role": role, "error": error,
             "failure_type": failure_type, "category": "worker"}
    # Bind new rows at write time.  Readers deliberately do not infer these
    # fields from a generation number because doing so would make pre-policy
    # failure rows look current after an epoch reset.
    try:
        from bot_namespace import EVALUATION_EPOCH
        from checkpoint_schema import FRESH_BOOTSTRAP_MODE, checkpoint_epoch_errors
        from evolution_infra import read_pipeline_checkpoint
        from system_strict_bootstrap import load_policy_epoch_reset_receipt

        checkpoint = read_pipeline_checkpoint() or {}
        receipt, receipt_errors = load_policy_epoch_reset_receipt()
        workflow_run_id = checkpoint.get("workflow_run_id")
        binding = checkpoint.get("epoch_binding") or {}
        live_reset_matches = (
            binding.get("mode") != FRESH_BOOTSTRAP_MODE
            or binding.get("policy_epoch_reset_receipt_digest")
            == receipt.get("receipt_digest")
        ) if isinstance(receipt, dict) else False
        if (
            not receipt_errors
            and isinstance(receipt, dict)
            and live_reset_matches
            and not checkpoint_epoch_errors(checkpoint)
            and checkpoint.get("next_v") == gen
            and isinstance(workflow_run_id, str)
            and workflow_run_id.strip()
        ):
            entry.update({
                "evaluation_epoch": EVALUATION_EPOCH,
                "workflow_run_id": workflow_run_id,
            })
    except Exception:
        pass
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


def _target_rel_set(task, next_v):
    """Extract the task's complete normalized writable file set.

    Returns a set of strings (relative paths within the bot directory) that
    the worker may modify. Both ``target_files`` and ``files_allowed`` must take
    part in parallel disjointness.  In national_tcp_policy_v1 every valid task
    resolves to policy.py, so multiple implementation workers are serialized.
    """
    return set(allowed_files_for_task(task, next_v))


class _ExistingEmptyContent(str):
    """Empty UTF-8 file marker that remains compatible with string consumers."""

    def __new__(cls):
        return super().__new__(cls, "")

    def __bool__(self):
        return True


class _ExistingEmptyBytes(bytes):
    """Empty binary file marker with the same existence semantics."""

    def __new__(cls):
        return super().__new__(cls, b"")

    def __bool__(self):
        return True


def _target_file_content(path):
    """Return ``(is_regular_file, content)`` without decoding binary assets.

    UTF-8 source remains ``str`` for compatibility with the existing CoT and
    tuner checks. Invalid UTF-8 is retained as raw ``bytes`` so a packed table
    can be compared and restored without lossy decoding.
    """
    path = Path(path)
    try:
        metadata = path.lstat()
    except OSError:
        return False, ""
    if not stat.S_ISREG(metadata.st_mode):
        return False, ""
    try:
        data = read_regular_file_bytes(path.parent, path, metadata)
    except OSError:
        return False, ""
    if is_binary_artifact_path(path):
        return True, data
    try:
        return True, data.decode("utf-8")
    except UnicodeDecodeError:
        return True, data


def _target_snapshot_content(path):
    exists, content = _target_file_content(path)
    if not exists:
        return ""
    if isinstance(content, bytes) and not content:
        return _ExistingEmptyBytes()
    if content == "":
        return _ExistingEmptyContent()
    return content


def _remove_target_entry(path):
    """Remove one target without following a worker-created symlink."""
    path = Path(path)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _write_target_snapshot(path, content, *, root=None):
    path = Path(path)
    if root is not None:
        root = Path(root)
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"target snapshot path escapes root: {path}") from exc
        cursor = root
        for part in relative.parts[:-1]:
            cursor = cursor / part
            try:
                metadata = cursor.lstat()
            except FileNotFoundError:
                cursor.mkdir()
                continue
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                _remove_target_entry(cursor)
                cursor.mkdir()
    _remove_target_entry(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(str(content), encoding="utf-8")


def _target_preview(path, limit=2000):
    exists, content = _target_file_content(path)
    if not exists:
        return ""
    if isinstance(content, bytes):
        return (
            f"<binary {len(content)} bytes sha256="
            f"{hashlib.sha256(content).hexdigest()}>"
        )
    return content[:limit]


def _restore_worker_changes(
    next_dir,
    boundary_snapshot,
    *,
    ignored_files=None,
):
    """Restore this worker's artifact delta while preserving parallel siblings.

    A malformed entry (symlink, FIFO, device, or a racing file) is itself an
    artifact delta.  Restore it first, then rescan so ordinary changed/new files
    from the same failed attempt are also rolled back.
    """
    ignored = set(ignored_files or ())
    for _ in range(2):
        try:
            changed = diff_snapshot(next_dir, boundary_snapshot)
        except ArtifactSnapshotError as exc:
            changed = exc.violation_files
        changed_set = set(changed)
        ignored_ancestors = set()
        for rel in ignored & changed_set:
            parent = Path(rel).parent
            while parent.as_posix() not in ("", "."):
                ignored_ancestors.add(parent.as_posix())
                parent = parent.parent
        changed = [
            rel for rel in changed
            if rel not in ignored and rel not in ignored_ancestors
        ]
        if not changed:
            return
        restore_python_files(next_dir, boundary_snapshot, changed)


def _reset_target_files_to_source(task, source_v, next_dir, next_v,
                                   baseline_snapshots=None, task_idx=None):
    """Reset only this task's target files back to a clean baseline state.

    Resolution order per target file:
    1. If `baseline_snapshots` (a {(task_idx, rel) -> str|bytes} dict) and `task_idx`
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
                _write_target_snapshot(dst_file, snap, root=next_dir)
                _reset_log.append(rel + " (baseline)")
            elif dst_file.exists() or dst_file.is_symlink():
                _remove_target_entry(dst_file)
                _reset_log.append(rel + " (baseline-unlink)")
            continue

        if not src_dir_exists:
            _skip_no_source.append(rel)
            continue
        src_file = src_dir / rel
        src_exists, src_content = _target_file_content(src_file)
        if src_exists:
            _write_target_snapshot(dst_file, src_content, root=next_dir)
            _reset_log.append(rel + " (source)")
        elif dst_file.exists() or dst_file.is_symlink():
            _remove_target_entry(dst_file)
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
    dst_exists, dst_content = _target_file_content(dst_file)

    if baseline_snapshots is not None and (task_idx, rel) in baseline_snapshots:
        before_content = baseline_snapshots[(task_idx, rel)]
        return _classify_target_change(
            True, dst_exists, before_content, dst_content
        )

    if source_v is None:
        return _classify_target_change(
            True, dst_exists, dst_content, dst_content
        )

    src_dir = get_bot_dir(source_v)
    src_file = src_dir / rel
    src_exists, src_content = _target_file_content(src_file)
    return _classify_target_change(
        src_exists, dst_exists, src_content, dst_content
    )


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


_COT_RUNTIME_SIDE_EFFECT_RE = re.compile(
    r"(stderr|stdout|sys\.stderr|_sys\.stderr|telemetry|debug|logging|"
    r"print\(|runtime\s+side[- ]effect|side[- ]effect|unconditional\s+log)",
    re.IGNORECASE,
)

_COT_TASK_MISMATCH_RE = re.compile(
    r"(assigned\s+task|task\s+was|task\s+steps?|diff|changed_functions|"
    r"worker'?s\s+changed|actual\s+surface\s+area).{0,240}"
    r"(performs?\s+none|none\s+of\s+these|does\s+not\s+implement|"
    r"not\s+implemented|revers(?:e|es|ed|ing)|inverted|opposite|"
    r"omits?|omitting|undisclosed|larger\s+and\s+more\s+invasive)",
    re.IGNORECASE | re.DOTALL,
)


def _cot_inconsistency_text(cot) -> str:
    if not isinstance(cot, dict):
        return ""
    parts = []
    for key in ("discrepancies", "focus_areas"):
        value = cot.get(key)
        if isinstance(value, (list, tuple)):
            parts.extend(str(item) for item in value)
        elif value:
            parts.append(str(value))
    return "\n".join(parts)


def _cot_inconsistency_has_runtime_side_effect(cot):
    """Return True when CoT found an undisclosed runtime/logging side effect."""
    text = _cot_inconsistency_text(cot)
    return bool(text and _COT_RUNTIME_SIDE_EFFECT_RE.search(text))


def _cot_inconsistency_is_task_mismatch(cot):
    """Return True when CoT proves the worker did not perform its assignment."""
    text = _cot_inconsistency_text(cot)
    return bool(text and _COT_TASK_MISMATCH_RE.search(text))


def _cot_inconsistency_blocks_task(task, cot=None):
    """Hard-block repair mismatches and undisclosed runtime side effects.

    Normal innovation workers can surface reviewer focus areas without forcing an
    immediate retry. Repair tasks are different: their whole purpose is to
    resolve exact blockers, so a claim-vs-diff mismatch is actionable failure.
    Runtime side effects are also different: hidden stderr/stdout/debug/telemetry
    changes can pollute match logs or affect timing, so they must be disclosed in
    the worker output or reverted regardless of task kind. Severe task-mismatch
    evidence such as reversing the assignment or omitting the actual edited
    surface is also a hard failure even for non-repair feature work.
    """
    if _cot_inconsistency_has_runtime_side_effect(cot):
        return True
    if _cot_inconsistency_is_task_mismatch(cot):
        return True
    text = " ".join([
        str(task.get("task_kind", "")),
        str(task.get("worker_id", "")),
        str(task.get("role", "")),
        str(task.get("worker_prompt", task.get("instruction", "")))[:1000],
    ]).lower()
    return any(marker in text for marker in (
        "quality_repair",
        "precommit_repair",
        "critic_repair",
        "review_repair",
        "reviewer_repair",
        "official_repair",
        "repair_planned",
        "critic rejection",
        "review rejection",
        "file_size(",
        "position_semantics(",
        "protected_contract",
    ))


def _cot_inconsistency_override_reason(task, task_skipper, worker_id, next_v, source_v, ui):
    """Let authoritative cheap rechecks override noisy COT text mismatches."""
    if task_skipper is None:
        return ""
    try:
        reason = task_skipper(task)
    except Exception as e:
        log.warning("Task skipper failed during CoT override for %s: %s", worker_id, e)
        return ""
    if not reason:
        return ""
    message = (
        f"Worker {worker_id} CoT check was inconsistent, but the scoped quality "
        f"blocker is now cleared; preserving edit. Recheck: {reason}"
    )
    try:
        ui.log_history(message, "warn")
    except Exception:
        pass
    try:
        from system_log import log_system_event
        log_system_event(
            "pipeline.worker_cot_inconsistency_overridden",
            "warn",
            message,
            {
                "next_v": next_v,
                "source_v": source_v,
                "worker_id": worker_id,
                "reason": reason,
            },
        )
    except Exception:
        pass
    return reason


async def _run_debug_agent(
    error_output,
    changed_diff,
    target_file,
    next_v,
    ui,
    *,
    candidate_dir=None,
):
    """Run the DeepEvolve debug sub-agent to diagnose and fix a compile/crash error.

    This is a lightweight LLM call with only the Read tool (budget=5 turns max).
    It examines the error output and changed diff to produce a minimal diagnosis.

    Returns a dict with 'diagnosis', 'fix', 'confidence' keys on success,
    or an empty dict on ordinary diagnostic failure.  A typed provider-wide
    ``LLMAvailabilityBlocked`` is deliberately propagated so the enclosing
    durable Worker activity can pause without consuming an attempt.
    """
    from llm_query import parse_json_output

    try:
        prompt_template_file = Path(__file__).resolve().parent / "prompts" / "debug_worker_prompt.md"
        if not prompt_template_file.exists():
            log.warning("Debug agent prompt not found: %s", prompt_template_file)
            return {}

        prompt_template = prompt_template_file.read_text(encoding="utf-8")

        target_rel = _target_rel(target_file, next_v) or str(target_file or "").strip()
        target_display = f"{bot_relpath(next_v)}/{target_rel}" if target_rel else str(target_file or "")
        candidate_root = (
            Path(candidate_dir) if candidate_dir is not None else get_bot_dir(next_v)
        )
        target_abs = candidate_root / target_rel if target_rel else candidate_root

        debug_prompt = (
            f"{prompt_template}\n\n"
            f"## Error Output\n```\n{error_output[:3000]}\n```\n\n"
            f"## Changed Diff\n```\n{changed_diff[:3000]}\n```\n\n"
            f"## Target File\n"
            f"- Generation: v{next_v}\n"
            f"- Repository-relative path: `{target_display}`\n"
            f"- Absolute path: `{target_abs}`\n\n"
            f"Read exactly `{target_abs}` for full context. Do not inspect or infer from "
            f"any other bot version unless the error output explicitly names that file. "
            f"Then produce your diagnosis.\n"
            f"Return ONLY a ```json``` block with your diagnosis, fix, and confidence level."
        )

        logs_dir = get_logs_dir(next_v)
        logs_dir.mkdir(parents=True, exist_ok=True)
        debug_log = logs_dir / "debug_agent_io.txt"

        output, _cost, _usage = await run_claude_query(
            debug_prompt, [], ui,
            f"DEBUG AGENT (v{next_v})", debug_log,
            tools=["Read"],
            allowed_read_dirs=[candidate_root],
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

    except LLMAvailabilityBlocked:
        raise
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
    if boundary_allowed_files and not parallel_mode:
        boundary_task["files_allowed"] = list({
            *(boundary_task.get("files_allowed", []) or []),
            *boundary_allowed_files,
        })
    boundary = audit_worker_boundary(
        next_dir,
        boundary_task,
        boundary_snapshot,
        next_v=next_v,
        ignored_changed_files=boundary_allowed_files if parallel_mode else None,
    )
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
                              boundary_allowed_files=None, task_skipper=None,
                              boundary_snapshot=None):
    """Run a single worker task with retries. Returns True on success."""
    w_id = task.get("worker_id", idx + 1)
    role = task.get("role", f"Expert Coder {w_id}")
    base_worker_prompt = _compose_worker_task_prompt(task, reviewer_feedback)
    base_worker_prompt = (
        "# Lease-Isolated Candidate\n"
        f"The only writable candidate tree for this attempt is `{next_dir}`. "
        f"Any older instruction that names `bots/national_v{next_v}` means this "
        "lease-isolated tree, never the canonical bot directory. Read, edit, "
        "compile, and probe only this tree; publication is owned by the harness.\n\n"
        + base_worker_prompt
    )

    worker_log_file = get_logs_dir(next_v) / f"worker_{w_id}_io.txt"
    worker_timeout = _worker_timeout_for_task(task, reviewer_feedback)
    allowed_write_scope = _allowed_write_scope_for_task(task, next_dir, next_v)

    compile_errors = []
    _last_reason = "unknown"
    _last_failure_type = "unknown"
    _last_error_output = ""   # fix-7: captured error output for debug agent
    _last_changed_diff = ""   # fix-7: captured changed diff for debug agent
    _infrastructure_issues = []
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
                _local_snapshots[(idx, rel)] = _target_snapshot_content(fpath)

    # Full artifact baseline: source, nested binary tables, and directory shape.
    # Parallel batches pass one pre-gather snapshot to every worker so scanning
    # cannot race a sibling that has already begun editing.
    _boundary_snapshot = (
        boundary_snapshot
        if boundary_snapshot is not None
        else snapshot_python_files(next_dir)
    )
    own_write_files = _target_rel_set(task, next_v)
    sibling_files = (
        set(boundary_allowed_files or ()) - own_write_files
        if parallel_mode else set()
    )

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
            _restore_worker_changes(
                next_dir,
                _boundary_snapshot,
                ignored_files=sibling_files,
            )

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
                        candidate_dir=next_dir,
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
                except LLMAvailabilityBlocked:
                    raise
                except Exception:
                    pass  # Debug agent failure must not block worker retry

        worker_prompt = substitute_template(worker_template, {
            "role": role,
            "worker_prompt": base_worker_prompt + attempt_note,
            "version": str(next_v),
            "parent_version": str(source_v),
            "candidate_path": str(next_dir),
        })

        # ── Timeout isolation: abort and retry if worker hangs for >WORKER_TIMEOUT sec ──
        try:
            llm_task = asyncio.create_task(run_claude_query(
                worker_prompt, context_files, ui,
                f"WORKER {w_id} ({role})", worker_log_file,
                tools=["Bash", "Read", "Edit"],
                allowed_write_dir=allowed_write_scope,
                allowed_read_dirs=[next_dir],
            ))
            await asyncio.wait_for(llm_task, timeout=worker_timeout)
        except LLMAvailabilityBlocked:
            # A provider-wide pause is not a Worker implementation attempt.
            # Remove any partial edits and let the durable activity boundary
            # release the lease without entering the retry/failure path.
            try:
                _restore_worker_changes(
                    next_dir,
                    _boundary_snapshot,
                    ignored_files=sibling_files,
                )
            finally:
                raise
        except (asyncio.TimeoutError, Exception) as exc:
            if isinstance(exc, asyncio.TimeoutError):
                _last_reason = f"timed out after {worker_timeout}s (attempt {attempt+1}/{MAX_WORKER_RETRIES})"
                _last_failure_type = "llm_infrastructure"
                # fix-7: capture timeout context for debug agent
                _last_error_output = f"Worker timed out after {worker_timeout}s"
                _last_changed_diff = "(timeout — partial edits rolled back)"
                ui.log_history(
                    f"Worker {w_id} ({role}) timed out after {worker_timeout}s. "
                    "Retrying the same hard contract with a more direct implementation...",
                    "warn",
                )
            else:
                _last_reason = f"unexpected error: {type(exc).__name__}: {str(exc)[:200]}"
                _last_failure_type = "llm_infrastructure"
                _last_error_output = f"{type(exc).__name__}: {str(exc)[:500]}"
                _last_changed_diff = "(exception — partial edits rolled back)"
                ui.log_history(f"Worker {w_id} ({role}) error: {exc}", "error")
            _infrastructure_issues.append(_last_reason)
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
            _restore_worker_changes(
                next_dir,
                _boundary_snapshot,
                ignored_files=sibling_files,
            )
            base_worker_prompt += (
                "\n\nPREVIOUS ATTEMPT TIMED OUT. Start fresh with a more direct, bounded "
                "implementation of the SAME assigned task. Reduce incidental complexity, "
                "but implement every mandatory Runtime Contract boundary and every assigned "
                "target; do not narrow the contract to one convenient change."
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
                    f"Write the candidate-owned file to its plain relative path `policy.py` — no annotations. "
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
        if boundary_allowed_files and not parallel_mode:
            boundary_task["files_allowed"] = list({
                *(boundary_task.get("files_allowed", []) or []),
                *boundary_allowed_files,
            })
        boundary = audit_worker_boundary(
            next_dir,
            boundary_task,
            _boundary_snapshot,
            next_v=next_v,
            ignored_changed_files=boundary_allowed_files if parallel_mode else None,
        )
        if not boundary.passed:
            _last_reason = "; ".join(boundary.violations[:3])
            _last_failure_type = "boundary_violation"
            restore_python_files(next_dir, _boundary_snapshot, boundary.violation_files)
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
                        "ignored_changed_files": boundary.ignored_changed_files[:20],
                        "violation_files": boundary.violation_files[:20],
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
                    if dst_file.exists() or dst_file.is_symlink():
                        _changed_files.append(
                            f"--- {rel} (modified) ---\n{_target_preview(dst_file)}"
                        )
            _last_changed_diff = "\n".join(_changed_files) if _changed_files else "(no diff available)"
            base_worker_prompt += f"\n\nCRITICAL FIX: Fix syntax error:\n{compile_errors[0]}"
            continue

        # Smoke test is NOT run here — it is deferred to the quality gate
        # (run_quality_gates in tool_gates.py) to save ~60-120s per retry attempt.
        ui.log_history(f"Worker {w_id} ({role}) done", "info")
        return True

    if _last_failure_type == "llm_infrastructure":
        _restore_worker_changes(
            next_dir,
            _boundary_snapshot,
            ignored_files=sibling_files,
        )
        raise WorkerInfrastructureError(
            w_id,
            role,
            _infrastructure_issues or [_last_reason],
        )
    # Worker failed all retries — record a real implementation failure.
    _restore_worker_changes(
        next_dir,
        _boundary_snapshot,
        ignored_files=sibling_files,
    )
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
    (task_idx, file_rel) -> UTF-8 text or raw bytes before the worker ran, used for
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
                worker_snapshots[(0, rel)] = _target_snapshot_content(fpath)
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
                    if _cot_inconsistency_blocks_task(tasks[0], cot):
                        override = _cot_inconsistency_override_reason(
                            tasks[0], task_skipper, tasks[0].get("worker_id", 1),
                            next_v, source_v, ui,
                        )
                        if not override:
                            audit_focus_areas.extend(cot.get("focus_areas", []))
                            _reset_target_files_to_source(
                                tasks[0], source_v, next_dir, next_v,
                                baseline_snapshots=worker_snapshots, task_idx=0,
                            )
                            ok = False
                    else:
                        audit_focus_areas.extend(cot.get("focus_areas", []))
            except LLMAvailabilityBlocked:
                raise
            except Exception as e:
                log.warning("CoT audit failed for worker 0: %s", e)
        return ok, worker_snapshots, audit_focus_areas

    # ── Disjointness check: can we safely run workers in parallel? ──
    # Compute complete per-task writable sets and check for intersections.
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
        # ── Parallel path: all writable file sets are disjoint ──
        # Pre-snapshot all required targets at once.
        ui.log_history(
            f"Running {len(tasks)} workers in PARALLEL (disjoint target files)...", "info"
        )
        for i, task in enumerate(tasks):
            for target in task.get("target_files", []):
                rel = _target_rel(target, next_v)
                if rel:
                    fpath = next_dir / rel
                    worker_snapshots[(i, rel)] = _target_snapshot_content(fpath)

        parallel_allowed_files = sorted({rel for fset in task_file_sets for rel in fset})
        parallel_boundary_snapshot = snapshot_python_files(next_dir)

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
                    boundary_snapshot=parallel_boundary_snapshot,
                )

        results = await gather_llm_fail_fast(
            *[_gated_worker(task, i) for i, task in enumerate(tasks)],
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
        infrastructure_failures = [
            result for result in results
            if isinstance(result, WorkerInfrastructureError)
        ]
        if infrastructure_failures:
            issues = [
                issue
                for failure in infrastructure_failures
                for issue in failure.issues
            ]
            raise WorkerInfrastructureError(
                ",".join(str(failure.worker_id) for failure in infrastructure_failures),
                "parallel worker batch",
                issues,
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
                    if _cot_inconsistency_blocks_task(task, cot):
                        override = _cot_inconsistency_override_reason(
                            task, task_skipper, task.get("worker_id", i + 1),
                            next_v, source_v, ui,
                        )
                        if not override:
                            audit_focus_areas.extend(cot.get("focus_areas", []))
                            _reset_target_files_to_source(
                                task, source_v, next_dir, next_v,
                                baseline_snapshots=worker_snapshots, task_idx=i,
                            )
                            any_failed = True
                    else:
                        audit_focus_areas.extend(cot.get("focus_areas", []))
            except LLMAvailabilityBlocked:
                raise
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
                worker_snapshots[(i, rel)] = _target_snapshot_content(fpath)
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
                if _cot_inconsistency_blocks_task(task, cot):
                    override = _cot_inconsistency_override_reason(
                        task, task_skipper, task.get("worker_id", i + 1),
                        next_v, source_v, ui,
                    )
                    if not override:
                        audit_focus_areas.extend(cot.get("focus_areas", []))
                        _reset_target_files_to_source(
                            task, source_v, next_dir, next_v,
                            baseline_snapshots=worker_snapshots, task_idx=i,
                        )
                        return False, worker_snapshots, audit_focus_areas
                else:
                    audit_focus_areas.extend(cot.get("focus_areas", []))
        except LLMAvailabilityBlocked:
            raise
        except Exception as e:
            log.warning("CoT audit failed for worker %d (sequential): %s", i, e)
    return True, worker_snapshots, audit_focus_areas
