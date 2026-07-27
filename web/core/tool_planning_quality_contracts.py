"""Worker quality-contract engine (Group E of tool_planning_worker).

Extracted from tool_planning_worker.py for maintainability. Contains the
quality/repair task classification, checkpoint inspection, the three
failure-source families (precommit/official/review), the contract builders
(position/national_native/official_smoke/architecture), reviewer-feedback
splitting, mechanical Python-source trimming, repair-task construction, and
authoritative rework synthesis.

This is a self-contained leaf: it imports only shared dependencies and does
NOT call back into tool_planning_worker's F group (durable worker execution)
or its proxy/bootstrap machinery. Static analysis confirmed zero references
to names defined outside Group E.

All symbols are re-exported by tool_planning_worker.py (which itself is
re-exported by tool_planning.py), so every existing
``from tool_planning import <name>`` and ``tool_planning.<name>`` site keeps
resolving. Tests monkeypatch ``tool_planning`` (never ``tool_planning_worker``),
so a plain re-export suffices.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import py_compile
import re
import tokenize
from copy import deepcopy
from pathlib import Path

from bot_namespace import bot_name
from evolution_core import check_code_size
from output_schema import (
    NATIONAL_POLICY_FOCUS_ID,
    POLICY_CONTEXT_SCHEMA_VERSION,
    POLICY_CONTEXT_TOP_LEVEL_FIELDS,
    POLICY_ENTRYPOINTS,
    POLICY_INTENT_KINDS,
    PRECOMPUTE_KEY_SHAPE_PATTERN,
    PRECOMPUTE_MAX_BUILD_MS,
    PRECOMPUTE_MAX_BYTES,
    PRECOMPUTE_MAX_ENTRIES,
    RuntimeContract,
)
from tool_helpers import _target_rel
import tool_planning_quality_rework as _qc


# ---------------------------------------------------------------------------
# Parent-module symbol forwarding (mirrors tool_planning_worker.py:80-307).
#
# Group E was extracted from tool_planning_worker, which itself resolves a small
# set of parent-module (tool_planning) symbols live so that test monkeypatches
# ``monkeypatch.setattr(tool_planning, name, fake)`` keep working. The symbols
# below are used by Group E functions; they must resolve through tool_planning
# at call time, not be snapshotted at import (with the exception of the two
# data constants, which are immutable and snapshot-safe).
#
# Monkeypatched (callable, re-read on every call via _TPCallableProxy):
#   get_bot_dir, log_system_event, _py_files_changed_between
# Snapshot (immutable data copied once at bootstrap):
#   _ACTIVE_CANDIDATE_WRITABLE_FILES, _is_fresh_empty_pool_bootstrap
# ---------------------------------------------------------------------------
import sys as _sys


class _TPCallableProxy:
    """Callable proxy that re-reads ``tool_planning.<name>`` on every call.

    Mirrors the same class in tool_planning_worker.py. Static analysis confirms
    every reference to these names in Group E is a plain call, so __call__ plus
    attribute forwarding is sufficient.
    """

    __slots__ = ("_name",)

    def __init__(self, name):
        object.__setattr__(self, "_name", name)

    def _resolve(self):
        tp = _sys.modules.get("tool_planning")
        if tp is None:
            raise RuntimeError(
                "tool_planning is not initialized; _TPCallableProxy cannot resolve "
                + object.__getattribute__(self, "_name")
            )
        return getattr(tp, object.__getattribute__(self, "_name"))

    def __call__(self, *args, **kwargs):
        return object.__getattribute__(self, "_resolve")()(*args, **kwargs)

    def __getattr__(self, attr):
        return getattr(object.__getattribute__(self, "_resolve")(), attr)

    def __repr__(self):
        try:
            return repr(object.__getattribute__(self, "_resolve")())
        except Exception:
            return f"<_TPCallableProxy name={object.__getattribute__(self, '_name')!r}>"


# Names that test suites historically monkeypatch on tool_planning and that
# Group E calls. Resolved live through tool_planning so
# monkeypatch.setattr(tool_planning, <name>, ...) keeps working.
_MONKEYPATCHED_TP_SYMBOLS_QC = (
    "get_bot_dir",
    "log_system_event",
    "_py_files_changed_between",
)
for _name in _MONKEYPATCHED_TP_SYMBOLS_QC:
    globals()[_name] = _TPCallableProxy(_name)

# Worker-execution identity helpers that physically live in
# tool_planning_worker (their rightful business home) but were historically
# importable from this companion. Bound as lazy proxies that resolve through
# tool_planning (which re-exports them from tool_planning_worker), so the
# ``from tool_planning_quality_contracts import (...)`` / ``tool_planning_
# quality_contracts.<name>`` surface keeps resolving without forcing a
# circular top-level import of tool_planning_worker into this leaf module.
_QC_LAZY_WORKER_IDENTITY_SYMBOLS = (
    "_worker_execution_task_digest",
    "_worker_backend_contract",
    "_expected_worker_backend_contract",
)
for _name in _QC_LAZY_WORKER_IDENTITY_SYMBOLS:
    globals()[_name] = _TPCallableProxy(_name)

# Immutable parent-module data constants Group E reads. Snapshotted once at
# bootstrap (after tool_planning has imported this companion). The companion's
# __getattr__ fills them lazily on the first LOAD_GLOBAL miss as a safety net.
_QC_SNAPSHOT_SYMBOLS = (
    "_ACTIVE_CANDIDATE_WRITABLE_FILES",
    "_is_fresh_empty_pool_bootstrap",
)


def _bootstrap_qc_snapshot_symbols():
    """Bind the parent-module data constants into this module's globals."""
    tp = _sys.modules.get("tool_planning")
    if tp is None:
        return
    _g = globals()
    for _n in _QC_SNAPSHOT_SYMBOLS:
        if hasattr(tp, _n):
            _g[_n] = getattr(tp, _n)


def __getattr__(name):
    """Lazy fallback for LOAD_GLOBAL miss on snapshot symbols.

    Only reached for attribute access or the first LOAD_GLOBAL before bootstrap;
    caches into globals() so subsequent LOAD_GLOBALs find it.
    """
    if name in _QC_SNAPSHOT_SYMBOLS:
        tp = _sys.modules.get("tool_planning")
        if tp is not None and hasattr(tp, name):
            value = getattr(tp, name)
            globals()[name] = value
            return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _checkpoint_plan_with_tasks(ckpt, tasks, replace_existing_tasks=False):
    """Return a checkpoint master_plan that can resume the given worker tasks."""
    existing_plan = ckpt.get("master_plan") if ckpt else None
    if isinstance(existing_plan, dict):
        if existing_plan.get("tasks") and not replace_existing_tasks:
            return existing_plan
        plan = {**existing_plan, "tasks": tasks}
    else:
        plan = {"tasks": tasks}
    try:
        from runtime_architecture_policy import attach_runtime_contract_ledger

        return attach_runtime_contract_ledger(plan)
    except Exception:
        # Keep the original ledger intact. Quality validation will fail closed
        # with its precise integrity error rather than silently replacing it.
        return plan


def _task_declared_scope_files(task, next_v):
    files = set()
    if not isinstance(task, dict):
        return files
    for key in ("target_files", "files_allowed"):
        for target in task.get(key, []) or []:
            rel = _target_rel(target, next_v)
            if rel:
                files.add(rel)
    return files


def _task_write_scope_errors(tasks, next_v):
    """Keep completion requirements from silently becoming write authority."""
    errors = []
    for index, task in enumerate(tasks or []):
        if not isinstance(task, dict):
            errors.append(f"task[{index}]_not_object")
            continue
        writable = _task_declared_scope_files(task, next_v)
        must_change = set()
        for target in task.get("must_change_files", []) or []:
            rel = _target_rel(target, next_v)
            if not rel:
                errors.append(f"task[{index}]_must_change_path_invalid:{target}")
            else:
                must_change.add(rel)
        unauthorized = sorted(must_change - writable)
        if unauthorized:
            errors.append(
                f"task[{index}]_must_change_outside_writable_scope:{unauthorized}"
            )
    return errors


def _plan_repair_scope_files(plan, next_v):
    files = set()
    if not isinstance(plan, dict):
        return files
    raw_scope = plan.get("repair_scope_files", []) or []
    if not isinstance(raw_scope, list):
        raw_scope = []
    for item in raw_scope:
        rel = _target_rel(item, next_v)
        if rel:
            files.add(rel)
    raw_tasks = plan.get("tasks", []) or []
    if not isinstance(raw_tasks, list):
        raw_tasks = []
    for task in raw_tasks:
        files.update(_task_declared_scope_files(task, next_v))
    return files


def _plan_with_accumulated_repair_scope(ckpt, plan, tasks, next_v):
    """Preserve final declared-scope coverage across in-place repair rounds.

    Rework execution may refresh ``tasks`` to only the newest blocker, but the
    repair edits are cumulative. Store only files already authorized by a
    Master/repair task or the immutable repair ledger; observed diffs are
    evidence, never authority. In particular, a crossover's Parent-A→child
    preparation diff must not auto-authorize a later Worker edit.
    """
    if not isinstance(plan, dict):
        return plan
    existing_plan = ckpt.get("master_plan") if isinstance(ckpt, dict) else {}
    scope = set()
    scope.update(_plan_repair_scope_files(existing_plan, next_v))
    scope.update(_plan_repair_scope_files(plan, next_v))
    for task in tasks or []:
        scope.update(_task_declared_scope_files(task, next_v))
    if not scope:
        return plan
    return {**plan, "repair_scope_files": sorted(scope)}


def _task_matches_quality_blocker(task, blocker):
    if str(task.get("repair_blocker") or "") == blocker:
        return True
    if blocker == "size" and str(task.get("repair_blocker") or "") == "file_size":
        return True
    text = " ".join([
        str(task.get("worker_id", "")),
        str(task.get("role", "")),
        str(task.get("repair_blocker", "")),
        " ".join(str(x) for x in task.get("target_files", []) or []),
        str(task.get("worker_prompt", task.get("instruction", ""))),
    ]).lower()
    if blocker == "size":
        return (
            "file_size" in text
            or "line_count" in text
            or "line count" in text
            or "loc limit" in text
            or "oversized" in text
            or "wc -l" in text
            or re.search(r"\bsize\b", text) is not None
            or re.search(r"\d+L/\d+L", text) is not None
        )
    if blocker == "position_semantics":
        return any(marker in text for marker in ("position_semantics", "dealer", "small blind", "big blind", "sb", "bb"))
    return False


def _task_quality_recheck_blockers(task):
    """Return cheap static quality blockers this task is trying to repair.

    Generic ``quality_gate`` tasks are only skippable when their evidence maps to
    a checker we can rerun cheaply. Compile, smoke, decision, and national
    acceptance repairs still run because this callback is intentionally not a
    replacement for the full quality gate.
    """
    if not isinstance(task, dict):
        return set()
    contract = task.get("repair_contract") if isinstance(task.get("repair_contract"), dict) else {}
    blocker = _normalize_repair_blocker(contract.get("blocker") or task.get("repair_blocker"))
    text = " ".join([
        str(task.get("worker_id", "")),
        str(task.get("role", "")),
        str(task.get("repair_blocker", "")),
        str(contract.get("blocker", "")),
        str(contract.get("evidence", "")),
        " ".join(str(x) for x in task.get("target_files", []) or []),
        str(task.get("worker_prompt", task.get("instruction", ""))),
    ]).lower()

    blockers = set()
    if blocker == "file_size" or _task_matches_quality_blocker(task, "size"):
        blockers.add("file_size")
    if blocker == "position_semantics" or _task_matches_quality_blocker(task, "position_semantics"):
        blockers.add("position_semantics")
    if blocker == "national_native_contract" or _is_national_native_contract_failure_text(text):
        blockers.add("national_native_contract")
    if blocker == "runtime_architecture" or "architecture_focus" in text or "architecture_regression" in text:
        blockers.add("runtime_architecture")
    if (
        "protected_contract" in text
        or "tcp action text" in text
        or "output must be json response int" in text
    ):
        blockers.add("protected_contract")
    if "reachability" in text:
        blockers.add("reachability")
    return blockers


def _normalize_repair_blocker(value):
    text = str(value or "").strip().lower()
    if text in {"size", "file_size", "line_count", "loc"}:
        return "file_size"
    if text in {"position", "position_semantics"}:
        return "position_semantics"
    if text in {"national_native", "national_native_contract", "native_tcp_contract"}:
        return "national_native_contract"
    if text in {"official_smoke", "official_platform", "official_platform_compliance"}:
        return "official_smoke"
    if text in {
        "runtime_architecture",
        "architecture_focus",
        "architecture_regression",
        "national_capability_contract",
    }:
        return "runtime_architecture"
    if text in {"quality", "quality_gate", "protected_contract", "compile", "smoke_test"}:
        return "quality_gate"
    return text


def _task_target_filenames(tasks):
    files = set()
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        for target in task.get("target_files", []) or []:
            name = Path(str(target)).name
            if name:
                files.add(name)
    return files


def _quality_contract_signature(contract):
    if not isinstance(contract, dict):
        return ("", "")
    blocker = _normalize_repair_blocker(contract.get("blocker"))
    filename = Path(str(contract.get("file", ""))).name
    return (blocker, filename) if blocker and filename else ("", "")


def _quality_contract_signatures(ckpt, reviewer_feedback=""):
    return {
        signature
        for signature in (
            _quality_contract_signature(contract)
            for contract in _quality_repair_contracts(ckpt, reviewer_feedback)
        )
        if all(signature)
    }


def _task_quality_contract_signatures(tasks):
    signatures = set()
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        files = _task_must_change_filenames(task)
        contract = task.get("repair_contract") if isinstance(task.get("repair_contract"), dict) else {}
        blocker = _normalize_repair_blocker(
            contract.get("blocker")
            or task.get("repair_blocker")
        )
        contract_file = Path(str(contract.get("file", ""))).name
        if contract_file:
            files.add(contract_file)
        if blocker:
            for filename in files:
                signatures.add((blocker, filename))
            continue
        if _task_matches_quality_blocker(task, "size"):
            for filename in files:
                signatures.add(("file_size", filename))
        if _task_matches_quality_blocker(task, "position_semantics"):
            for filename in files:
                signatures.add(("position_semantics", filename))
        text = " ".join([
            str(task.get("worker_id", "")),
            str(task.get("role", "")),
            str(task.get("task_kind", "")),
            str(task.get("worker_prompt", task.get("instruction", ""))),
        ]).lower()
        if "quality_gate" in text or "protected_contract" in text:
            for filename in files:
                signatures.add(("quality_gate", filename))
    return signatures


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _quality_task_contract_refresh_reason(task, current_contract):
    """Return why a saved quality repair task should be regenerated."""
    if not isinstance(task, dict) or not isinstance(current_contract, dict):
        return ""
    signature = _quality_contract_signature(current_contract)
    blocker, filename = signature
    if blocker == "runtime_architecture":
        expected_focus = str(current_contract.get("focus_id") or "")
        if str(task.get("architecture_focus_id") or "") != expected_focus:
            return f"{blocker}:{filename}:architecture_focus_changed"
        expected_layer = str(current_contract.get("skill_layer") or "")
        if str(task.get("skill_layer") or "") != expected_layer:
            return f"{blocker}:{filename}:skill_layer_changed"
        if task.get("runtime_contract") != current_contract.get("runtime_contract"):
            return f"{blocker}:{filename}:runtime_contract_changed"
        expected_checks = [str(item) for item in current_contract.get("required_checks") or []]
        actual_checks = [str(item) for item in task.get("checks_required") or []]
        if actual_checks != expected_checks:
            return f"{blocker}:{filename}:required_checks_changed"
        expected_targets = {
            Path(str(item)).name for item in current_contract.get("files") or [filename]
        }
        actual_targets = {
            Path(str(item)).name for item in task.get("target_files") or []
        }
        if actual_targets != expected_targets:
            return f"{blocker}:{filename}:target_files_changed"
        return ""
    if blocker != "file_size":
        return ""

    saved = task.get("repair_contract") if isinstance(task.get("repair_contract"), dict) else {}
    saved_current = _int_or_none(saved.get("current_lines"))
    saved_limit = _int_or_none(saved.get("line_limit"))
    current_lines = _int_or_none(current_contract.get("current_lines"))
    line_limit = _int_or_none(current_contract.get("line_limit"))

    if line_limit is not None and saved_limit != line_limit:
        return f"{blocker}:{filename}:line_limit_changed"
    if current_lines is not None and saved_current != current_lines:
        return f"{blocker}:{filename}:current_lines_changed"

    prompt = str(task.get("worker_prompt", task.get("instruction", "")))
    if (
        current_lines is not None
        and line_limit is not None
        and current_lines - line_limit >= 200
        and "Large-overage requirement" not in prompt
    ):
        return f"{blocker}:{filename}:large_overage_prompt_outdated"
    return ""


def _is_file_size_repair_task(task):
    if not isinstance(task, dict):
        return False
    contract = task.get("repair_contract") if isinstance(task.get("repair_contract"), dict) else {}
    blocker = _normalize_repair_blocker(
        contract.get("blocker")
        or task.get("repair_blocker")
    )
    if blocker == "file_size":
        return True
    return _task_matches_quality_blocker(task, "size")


def _order_quality_repair_tasks(tasks):
    """Run semantic/protocol repairs before final file-size cleanup.

    Multiple quality blockers can target the same file. If a file-size cleanup
    runs first, a later semantic repair can add a line or two and re-break the
    size gate. Keep ordering stable except for moving file-size repairs to the
    end of the quality-rework batch.
    """
    indexed = list(enumerate(tasks or []))
    ordered = [
        task for _idx, task in sorted(
            indexed,
            key=lambda item: (1 if _is_file_size_repair_task(item[1]) else 0, item[0]),
        )
    ]
    return ordered


def _stale_quality_task_reason(tasks, ckpt, reviewer_feedback=""):
    """Return a refresh reason when saved quality tasks no longer match gate blockers."""
    if (
        not isinstance(ckpt, dict)
        or ckpt.get("stage") not in {"quality_failed", "repair_planned", "rework_running"}
    ):
        return ""
    current_contracts = {
        signature: contract
        for contract in _quality_repair_contracts(ckpt, reviewer_feedback)
        for signature in [_quality_contract_signature(contract)]
        if all(signature)
    }
    current = set(current_contracts)
    if not current:
        return ""
    task_signatures = _task_quality_contract_signatures(tasks)
    missing = sorted(current - task_signatures)
    extra = sorted(task_signatures - current)
    if extra and reviewer_feedback:
        return "stale current quality repair contract(s): extra stale task(s): " + ", ".join(
            f"{blocker}:{filename}" for blocker, filename in extra
        )
    if not missing:
        stale = []
        for task in tasks or []:
            for signature in sorted(_task_quality_contract_signatures([task]) & current):
                reason = _quality_task_contract_refresh_reason(task, current_contracts[signature])
                if reason:
                    stale.append(reason)
        if not stale:
            return ""
        return "stale current quality repair contract(s): " + ", ".join(sorted(set(stale)))
    return "missing current quality repair contract(s): " + ", ".join(
        f"{blocker}:{filename}" for blocker, filename in missing
    )


def _task_must_change_filenames(task):
    files = set()
    if not isinstance(task, dict):
        return files
    for key in ("must_change_files", "target_files"):
        for target in task.get(key, []) or []:
            name = Path(str(target)).name
            if name:
                files.add(name)
        if files:
            break
    return files


def _quality_failure_target_files(ckpt, reviewer_feedback=""):
    if reviewer_feedback:
        contracts = _quality_repair_contracts(ckpt, reviewer_feedback)
        if contracts:
            return {contract["file"] for contract in contracts if contract.get("file")}
    failures = [
        item for item in _quality_failure_items(ckpt)
        if not _is_declared_scope_failure_text(item)
    ]
    files = _extract_quality_failure_files(failures)
    if not files and reviewer_feedback and not _is_declared_scope_failure_text(reviewer_feedback):
        files = _extract_quality_failure_files([reviewer_feedback])
    return set(files)


def _quality_rework_skipper(
    next_dir,
    source_dir,
    next_v,
    source_v,
    *,
    expected_architecture_policy=None,
    master_plan=None,
):
    """Return a per-task skip callback for cheap quality-repair rechecks.

    Full quality validation remains owned by run_quality_gates. This callback
    only avoids wasting LLM calls for blockers that are already cleared by an
    earlier repair worker in the same rework batch.
    """
    def remaining_blockers():
        blockers = {}
        checked = set()
        try:
            _total, oversized = check_code_size(next_dir, source_dir=source_dir)
            checked.add("file_size")
            if oversized:
                blockers["file_size"] = {Path(name).name for name, _lines, _limit in oversized}
        except Exception:
            pass
        try:
            from tool_gates import detect_position_semantics_errors
            position_errors = detect_position_semantics_errors(next_dir)
            checked.add("position_semantics")
            if position_errors:
                files = _extract_quality_failure_files(position_errors)
                blockers["position_semantics"] = set(files)
        except Exception:
            pass
        try:
            from national_native import check_native_contract
            native_errors = check_native_contract(
                next_dir,
                require_current_stream_decoder=True,
                require_current_decision_runtime=True,
            )
            checked.add("national_native_contract")
            if native_errors:
                files = _extract_quality_failure_files(native_errors)
                blockers["national_native_contract"] = set(files or ["national_bot.py"])
        except Exception:
            pass
        try:
            from code_verification import detect_new_function_reachability_warnings
            changed = _py_files_changed_between(source_dir, next_dir)
            reachability = detect_new_function_reachability_warnings(
                source_dir,
                next_dir,
                changed_files=changed,
            )
            checked.add("reachability")
            if reachability:
                files = _extract_quality_failure_files(reachability)
                blockers["reachability"] = set(files)
        except Exception:
            pass
        try:
            from runtime_architecture_policy import (
                evaluate_architecture_transition,
                validate_runtime_contract_implementation,
            )

            transition = evaluate_architecture_transition(
                source_dir,
                next_dir,
                expected_policy=expected_architecture_policy,
            )
            contract_errors = validate_runtime_contract_implementation(
                master_plan if isinstance(master_plan, dict) else {},
                transition.get("candidate_capabilities") or {},
            )
            transition["runtime_contract_implementation_errors"] = contract_errors
            if contract_errors:
                transition["ok"] = False
            checked.add("runtime_architecture")
            if not transition.get("ok"):
                files = set(_architecture_transition_repair_files(transition, next_dir))
                blockers["runtime_architecture"] = files or {"policy.py"}
        except Exception:
            pass
        return blockers, checked

    def skipper(task):
        blockers, checked = remaining_blockers()
        task_blockers = _task_quality_recheck_blockers(task)
        if not task_blockers:
            return ""
        unchecked = task_blockers - checked
        if unchecked:
            return ""
        if not blockers:
            return "all cheap quality rework blockers already cleared by current code"
        task_files = _task_must_change_filenames(task)
        active_task_blockers = set(task_blockers) & set(blockers)
        if not active_task_blockers:
            return (
                "quality blocker(s) already cleared by current code: "
                + ", ".join(sorted(task_blockers))
            )
        if task_files:
            still_relevant = False
            for blocker in active_task_blockers:
                remaining_files = blockers.get(blocker) or set()
                if not remaining_files or task_files & remaining_files:
                    still_relevant = True
                    break
            if not still_relevant:
                return (
                    "quality blocker file(s) already cleared by current code: "
                    + ", ".join(sorted(task_files))
                )
        return ""

    return skipper


def _checkpoint_master_plan(ckpt):
    if not isinstance(ckpt, dict):
        return {}
    plan = ckpt.get("master_plan")
    return plan if isinstance(plan, dict) else {}


def _canonical_tasks_digest(tasks):
    return hashlib.sha256(
        json.dumps(
            tasks,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _frozen_rework_task_authority_errors(ckpt, tasks):
    """Validate persisted repair authority without regenerating prompt prose."""
    errors = _task_write_scope_errors(tasks, ckpt.get("next_v"))
    if not isinstance(tasks, list) or not tasks:
        return [*errors, "checkpoint_frozen_rework_tasks_missing_or_empty"]
    for index, task in enumerate(tasks):
        if not _repair_contract_signature(task, ckpt.get("next_v")):
            worker_id = task.get("worker_id") if isinstance(task, dict) else None
            errors.append(
                f"task[{index}]_repair_contract_signature_invalid:"
                f"{worker_id or 'unknown'}"
            )
    return errors


def _checkpoint_master_task_authority_errors(ckpt, authoritative_tasks):
    """Bind initial worker execution to the accepted checkpoint plan/ledger."""
    if not isinstance(authoritative_tasks, list) or not authoritative_tasks:
        return ["checkpoint_master_plan_tasks_missing_or_empty"]
    scope_errors = _task_write_scope_errors(
        authoritative_tasks,
        ckpt.get("next_v") if isinstance(ckpt, dict) else None,
    )
    if scope_errors:
        return scope_errors
    plan = _checkpoint_master_plan(ckpt)
    plan_ledger = plan.get("runtime_contract_ledger")
    checkpoint_ledger = ckpt.get("runtime_contract_ledger") if isinstance(ckpt, dict) else None
    has_runtime_contract = any(
        isinstance(task, dict) and isinstance(task.get("runtime_contract"), dict)
        for task in authoritative_tasks
    )
    ledger_required = has_runtime_contract or isinstance(plan.get("architecture_policy"), dict)
    if not ledger_required and plan_ledger is None and checkpoint_ledger is None:
        return []

    from runtime_architecture_policy import (
        build_runtime_contract_ledger,
        runtime_contract_ledger_digest,
        validate_runtime_contract_ledger,
    )

    errors = []
    if plan_ledger is None:
        errors.append("master_plan_runtime_contract_ledger_missing")
    else:
        errors.extend(
            f"master_plan:{error}"
            for error in validate_runtime_contract_ledger(plan_ledger)
        )
    if checkpoint_ledger is None:
        errors.append("checkpoint_runtime_contract_ledger_missing")
    else:
        errors.extend(
            f"checkpoint:{error}"
            for error in validate_runtime_contract_ledger(checkpoint_ledger)
        )
    if errors:
        return errors

    plan_digest = runtime_contract_ledger_digest(plan_ledger)
    checkpoint_digest = runtime_contract_ledger_digest(checkpoint_ledger)
    if plan_digest != checkpoint_digest:
        errors.append("checkpoint_master_plan_runtime_contract_ledger_mismatch")
    try:
        rebuilt = build_runtime_contract_ledger({"tasks": authoritative_tasks})
        rebuilt_digest = runtime_contract_ledger_digest(rebuilt)
    except Exception as exc:
        errors.append(
            "master_tasks_runtime_contract_ledger_rebuild_failed:"
            f"{type(exc).__name__}:{str(exc)[:240]}"
        )
    else:
        if rebuilt_digest != plan_digest:
            errors.append("master_tasks_runtime_contract_ledger_mismatch")
    return errors


def _checkpoint_work_item(ckpt):
    plan = _checkpoint_master_plan(ckpt)
    work_item = plan.get("work_item")
    return work_item if isinstance(work_item, dict) else {}


def _is_official_rework_checkpoint(ckpt):
    if not isinstance(ckpt, dict):
        return False
    if ckpt.get("stage") == "official_failed":
        return True
    work_item = _checkpoint_work_item(ckpt)
    route = work_item.get("route") if isinstance(work_item.get("route"), dict) else {}
    return (
        work_item.get("kind") == "official_repair"
        or work_item.get("source_stage") == "official_failed"
        or route.get("intent") == "official_rework"
    )


def _has_legacy_critic_repair_contract(ckpt, tasks=()):
    """Detect retired Critic-owned candidate mutation authority.

    A schema-valid Critic receipt is advisory evidence only.  Historical
    checkpoints/tasks may still carry ``critic_repair`` markers; recognizing
    those markers is solely a fail-closed migration guard and never permission
    to synthesize or execute a Worker task.
    """

    work_item = _checkpoint_work_item(ckpt)
    route = work_item.get("route") if isinstance(work_item.get("route"), dict) else {}
    markers = {
        str(work_item.get("kind") or "").lower(),
        str(work_item.get("repair_blocker") or "").lower(),
        str(route.get("intent") or "").lower(),
    }
    for task in tasks or ():
        if not isinstance(task, dict):
            continue
        markers.update({
            str(task.get("task_kind") or "").lower(),
            str(task.get("repair_blocker") or "").lower(),
            str((task.get("repair_contract") or {}).get("blocker") or "").lower()
            if isinstance(task.get("repair_contract"), dict)
            else "",
        })
    return any(
        "critic_repair" in marker
        or "critic_rework" in marker
        or marker == "critic_rejection"
        for marker in markers
    )


def _critic_advisory_rework_refusal(ckpt, tasks, next_v, source_v):
    """Return a fail-closed payload when Critic advice reaches Workers."""

    legacy_critic_repair = _has_legacy_critic_repair_contract(ckpt, tasks)
    critic_without_precommit_regression = (
        isinstance(ckpt, dict)
        and ckpt.get("stage") == "critic_checked"
        and not _is_precommit_rework_checkpoint(ckpt)
    )
    if not legacy_critic_repair and not critic_without_precommit_regression:
        return None
    return {
        "error": (
            "LEGACY_CRITIC_REPAIR_FORBIDDEN"
            if legacy_critic_repair
            else "CRITIC_ADVISORY_REWORK_FORBIDDEN"
        ),
        "next_v": next_v,
        "source_v": source_v,
        "stage": ckpt.get("stage") if isinstance(ckpt, dict) else None,
        "next_tool": (
            "abandon_generation"
            if legacy_critic_repair
            else "run_precommit_eval"
        ),
        "failure_class": (
            "contract_migration"
            if legacy_critic_repair
            else "route_violation"
        ),
        "safe_to_auto_execute": not legacy_critic_repair,
        "directive": (
            "This checkpoint carries retired Critic-owned Worker repair authority. "
            "Do not mutate the candidate; run controlled abandon/re-prepare recovery."
            if legacy_critic_repair
            else "Critic is advisory. Call run_precommit_eval for the unchanged candidate; "
            "only a measured native precommit regression can authorize Worker rework."
        ),
    }


def _is_review_rework_checkpoint(ckpt):
    """Whether the checkpoint represents a Lead Code Reviewer rejection.

    A candidate can have an old rejected critic gate in ``gate_results`` and
    later fail review after an in-place repair. The latest review rejection must
    own the next repair contract; otherwise stale critic/quality tasks can be
    reused against the wrong blocker.
    """
    if not isinstance(ckpt, dict):
        return False
    if ckpt.get("stage") not in {"repair_planned", "rework_running"}:
        return False
    if _is_precommit_rework_checkpoint(ckpt):
        return False
    if _is_official_rework_checkpoint(ckpt):
        return False
    review = (ckpt.get("gate_results") or {}).get("review") or {}
    if not isinstance(review, dict) or not review:
        return False
    if review.get("approved") is False:
        return True
    status = str(review.get("status") or "").lower()
    if status in {"rejected", "failed", "blocked"}:
        return True
    return False


def _review_repair_task_refresh_reason(tasks, ckpt, feedback=""):
    if not _is_review_rework_checkpoint(ckpt):
        return ""
    if not tasks:
        return "missing review repair task(s)"
    expected = set(_review_repair_target_files(ckpt, feedback))
    task_files = set(_task_target_filenames(tasks))
    task_kinds = {
        str(task.get("task_kind") or "").lower()
        for task in tasks or []
        if isinstance(task, dict)
    }
    task_text = " ".join(
        str(task.get("worker_id", "")) + " " + str(task.get("worker_prompt", ""))[:500]
        for task in tasks or []
        if isinstance(task, dict)
    ).lower()
    if not any("review_repair" in kind for kind in task_kinds) and "code reviewer" not in task_text:
        return "checkpoint task is not a review repair"
    if expected and task_files != expected:
        return "review repair targets are stale"
    if "quality_repair" in task_text or any("quality_repair" in kind for kind in task_kinds):
        return "review repair task still uses quality repair contract"
    return ""


def _checkpoint_rework_feedback(ckpt):
    if not isinstance(ckpt, dict):
        return ""
    if ckpt.get("reviewer_feedback"):
        return str(ckpt.get("reviewer_feedback") or "")
    stage = ckpt.get("stage")
    gates = ckpt.get("gate_results") or {}
    if _is_precommit_rework_checkpoint(ckpt):
        failed = _precommit_failure_items(ckpt)
        if failed:
            return "Precommit failed:\n- " + "\n- ".join(str(item) for item in failed[:20])
    if _is_official_rework_checkpoint(ckpt):
        failed = _official_failure_items(ckpt)
        if failed:
            return "Official EXE full certification failed:\n- " + "\n- ".join(str(item) for item in failed[:20])
    if _is_review_rework_checkpoint(ckpt):
        failed = _review_feedback_items(ckpt)
        if failed:
            return "Reviewer rejected:\n- " + "\n- ".join(str(item) for item in failed[:20])
    if stage in {"quality_failed", "repair_planned", "rework_running"}:
        failed = _quality_failure_items(ckpt)
        if failed:
            return "Quality gates failed:\n- " + "\n- ".join(str(item) for item in failed[:20])
    if stage == "precommit_failed":
        precommit = gates.get("precommit_eval") or {}
        blockers = precommit.get("blockers") or precommit.get("failures") or []
        if blockers:
            return "Precommit failed: " + json.dumps(blockers[:10], ensure_ascii=False)
    return ""


def _checkpoint_repair_baseline_fingerprint(ckpt) -> str:
    """Return the content identity that authorized the current repair route."""
    if not isinstance(ckpt, dict):
        return ""
    top_level = str(ckpt.get("repair_baseline_artifact_hash") or "")
    plan = ckpt.get("master_plan") if isinstance(ckpt.get("master_plan"), dict) else {}
    work_item = plan.get("work_item") if isinstance(plan.get("work_item"), dict) else {}
    bound = str(work_item.get("repair_baseline_artifact_hash") or "")
    stage = str(ckpt.get("stage") or "")
    if stage in {"quality_failed", "precommit_failed", "official_failed"} and top_level:
        return top_level
    if bound:
        return bound
    if top_level:
        return top_level

    gates = ckpt.get("gate_results") if isinstance(ckpt.get("gate_results"), dict) else {}
    if _is_official_rework_checkpoint(ckpt) or ckpt.get("stage") == "official_failed":
        official = gates.get("official_full") if isinstance(gates.get("official_full"), dict) else {}
        identities = [
            official.get("certification_identity"),
            (official.get("status") or {}).get("certification_identity")
            if isinstance(official.get("status"), dict)
            else None,
        ]
        for identity in identities:
            if isinstance(identity, dict) and identity.get("candidate_hash"):
                return str(identity["candidate_hash"])

    if _is_precommit_rework_checkpoint(ckpt) or ckpt.get("stage") == "precommit_failed":
        precommit = gates.get("precommit_eval") if isinstance(gates.get("precommit_eval"), dict) else {}
        if precommit.get("code_fingerprint"):
            return str(precommit["code_fingerprint"])

    quality = gates.get("quality") if isinstance(gates.get("quality"), dict) else {}
    if quality.get("code_fingerprint"):
        return str(quality["code_fingerprint"])

    # Official evidence is created only after the content-bound precommit gate;
    # retain that safe fallback for older official payload projections.
    precommit = gates.get("precommit_eval") if isinstance(gates.get("precommit_eval"), dict) else {}
    return str(precommit.get("code_fingerprint") or "")


def _quality_failure_items(ckpt):
    if not isinstance(ckpt, dict):
        return []
    quality = (ckpt.get("gate_results") or {}).get("quality") or {}
    items = []

    def add(value):
        if isinstance(value, dict):
            for key, val in value.items():
                if str(key).endswith(".py"):
                    items.append(f"{key}: {val}")
                else:
                    items.append(f"{key}={val}")
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                add(item)
        elif value is not None:
            text = str(value).strip()
            if text:
                items.append(text)

    add(quality.get("failed_gates"))
    add(quality.get("failures"))
    for key in (
        "compile_errors",
        "import_errors",
        "protected_contract_errors",
        "national_native_contract_errors",
        "smoke_errors",
        "national_protocol_errors",
        "national_acceptance_errors",
        "official_smoke_errors",
        "declared_scope_errors",
        "critical_failures",
        "position_semantics_errors",
        "reachability_warnings",
    ):
        add(quality.get(key))
    oversized = quality.get("oversized_files")
    if isinstance(oversized, dict):
        for filename, lines in oversized.items():
            add(f"file_size({filename}:{lines}L)")

    transition = quality.get("national_architecture_transition") or {}
    if isinstance(transition, dict) and not transition.get("ok", True):
        candidate_checks = (
            (transition.get("candidate_capabilities") or {}).get("checks_by_id") or {}
        )
        for error in transition.get("policy_identity_errors") or []:
            add(f"runtime_architecture_policy_identity: {error}")
        for regression in transition.get("regressions") or []:
            check_id = str(regression.get("check_id") or "unknown")
            guidance = regression.get("guidance") or (
                (candidate_checks.get(check_id) or {}).get("guidance")
                or "Restore the source capability."
            )
            add(f"runtime_architecture_regression:{check_id}: {guidance}")
        for failure in transition.get("runtime_floor_failures") or []:
            check_id = str(failure.get("check_id") or "unknown")
            check = candidate_checks.get(check_id) or {}
            add(
                f"runtime_architecture_floor:{check_id}: "
                f"{failure.get('guidance') or check.get('guidance') or 'Complete the mandatory runtime floor.'}"
            )
        for check_id in transition.get("unresolved_focus_checks") or []:
            check = candidate_checks.get(str(check_id)) or {}
            add(
                f"runtime_architecture_focus:{check_id}: "
                f"{check.get('guidance') or 'Complete the selected architecture focus.'}"
            )
        if transition.get("error"):
            add(f"runtime_architecture_error: {transition.get('error')}")

    deduped = []
    seen = set()
    for item in items:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped


def _extract_quality_failure_files(failures):
    files = []
    seen = set()
    for failure in failures or []:
        for match in re.finditer(r"([A-Za-z0-9_./-]+\.py)(?::\d+)?", str(failure)):
            rel = Path(match.group(1)).name
            if rel and rel not in seen:
                seen.add(rel)
                files.append(rel)
    return files


def _is_declared_scope_failure_text(item):
    text = str(item or "").lower()
    return (
        "declared_scope" in text
        or "declared scope" in text
        or "outside master plan target_files/files_allowed" in text
        or "outside declared target_files/files_allowed" in text
    )


def _is_national_native_contract_failure_text(item):
    text = str(item or "").lower()
    return (
        "national_native_contract" in text
        or "native national tcp contract" in text
        or "national_bot.py missing" in text
        or (
            "national_bot.py" in text
            and (
                "sanitizer failure" in text
                or "raw action" in text
                or "direct tcp" in text
                or "botzone integer" in text
            )
        )
    )


def _task_id_suffix(filename):
    return re.sub(r"[^a-z0-9]+", "_", Path(str(filename)).name.lower()).strip("_")


_ARCHITECTURE_CHECK_FILES = {
    "official_safe_wire_send": ["national_bot.py"],
    "clean_diagnostics_channel": ["national_bot.py"],
    "national_policy_module": ["policy.py"],
    "decision_context_v1": ["policy.py"],
    "typed_intent_v1": ["policy.py"],
    "policy_baseline_entrypoint": ["policy.py"],
    "policy_refinement_entrypoint": ["policy.py"],
    "decision_time_budget_visible": ["policy.py"],
    "killable_decision_runtime": ["national_bot.py"],
    "fast_policy_baseline": ["policy.py"],
    "incremental_refinement_protocol": ["policy.py"],
    "budget_scaled_refinement": ["policy.py"],
    "decision_path_no_external_io": ["policy.py"],
    "decision_path_no_full_history_scan": ["policy.py"],
    "decision_path_no_large_runtime_tables": ["policy.py"],
    "precompute_lookup_path": ["policy.py"],
    "persistent_match_memory": ["national_bot.py"],
    "terminal_response_memory": ["national_bot.py"],
    "showdown_range_posterior": ["national_bot.py"],
    "authoritative_hand_context": ["national_bot.py"],
    "incremental_opponent_model": ["policy.py"],
    "terminal_response_adaptation": ["policy.py"],
    "showdown_range_adaptation": ["policy.py"],
    "donk_line_reachability": ["policy.py"],
    "delayed_probe_line_reachability": ["policy.py"],
    "semantic_line_reachability": ["policy.py"],
}

def _architecture_transition_failure_ids(transition):
    candidate = transition.get("candidate_capabilities") or {}
    failing_ids = []
    for item in candidate.get("required_failures") or []:
        check_id = str(item.get("check_id") or item.get("name") or "")
        if check_id and check_id not in failing_ids:
            failing_ids.append(check_id)
    for item in transition.get("regressions") or []:
        check_id = str(item.get("check_id") or "")
        if check_id and check_id not in failing_ids:
            failing_ids.append(check_id)
    for item in transition.get("runtime_floor_failures") or []:
        check_id = str(item.get("check_id") or "")
        if check_id and check_id not in failing_ids:
            failing_ids.append(check_id)
    for check_id in transition.get("unresolved_focus_checks") or []:
        check_id = str(check_id)
        if check_id and check_id not in failing_ids:
            failing_ids.append(check_id)
    if transition.get("runtime_contract_implementation_errors"):
        failing_ids.append("runtime_contract_implementation")
    return failing_ids


def _architecture_transition_repair_files(transition, candidate_dir=None):
    candidate = transition.get("candidate_capabilities") or {}
    checks_by_id = candidate.get("checks_by_id") or {}
    policy = transition.get("policy") or {}
    focus = transition.get("selected_focus") or policy.get("selected_focus") or {}
    require_existing = bool(candidate_dir and Path(candidate_dir).is_dir())
    files = []

    def add_file(value):
        rel = Path(str(value)).name
        if rel not in _ACTIVE_CANDIDATE_WRITABLE_FILES or rel in files:
            return
        if require_existing and not (Path(candidate_dir) / rel).is_file():
            return
        files.append(rel)

    for check_id in _architecture_transition_failure_ids(transition):
        check = checks_by_id.get(check_id) or {}
        locations = [str(item) for item in (check.get("evidence") or {}).get("locations") or []]
        for rel in _extract_quality_failure_files(locations):
            add_file(rel)
        for rel in _ARCHITECTURE_CHECK_FILES.get(check_id, []):
            add_file(rel)
    if not files:
        for rel in focus.get("suggested_files") or []:
            add_file(rel)
    return files


def _format_position_details(details):
    lines = []
    for detail in details or []:
        line = detail.get("line")
        message = detail.get("message") or detail.get("evidence") or ""
        lines.append(f"- line {line}: {message}" if line else f"- {message}")
    return "\n".join(lines) if lines else "- gate reported a position_semantics violation in this file"


def _quality_contract_task(contract, ckpt, preservation, task_kind):
    next_v = ckpt.get("next_v")
    filename = contract["file"]
    if Path(str(filename)).name not in _ACTIVE_CANDIDATE_WRITABLE_FILES:
        raise ValueError(
            "quality repair cannot make a system or extra artifact writable: "
            f"{filename}"
        )
    suffix = _task_id_suffix(filename)
    blocker = contract.get("blocker")
    if blocker == "file_size":
        current = contract.get("current_lines")
        limit = contract.get("line_limit")
        overage = None
        required = (
            f"Reduce `{filename}` to <= {limit} lines."
            if limit else f"Reduce `{filename}` enough to clear the file_size gate."
        )
        if current is not None and limit is not None:
            try:
                overage = int(current) - int(limit)
            except (TypeError, ValueError):
                overage = None
            required += f" Current gate reading: {current}L/{limit}L."
        large_overage = ""
        if overage is not None and overage >= 200:
            target_removal = overage + 50
            large_overage = (
                "\nLarge-overage requirement:\n"
                f"- This file is {overage} lines over the gate. Do not spend the attempt "
                "on tiny comment trimming alone.\n"
                f"- Before editing, identify a removal/consolidation plan worth at least "
                f"{target_removal} lines so the final file has margin under the limit.\n"
                "- Remove whole dead/debug/self-test blocks, duplicated historical notes, "
                "and unreferenced helper wrappers first. If comments cannot meet the target, "
                "delete or consolidate unreachable helper code verified by local grep/references.\n"
                "- A script-based rewrite is acceptable when it writes only this assigned file; "
                "run `wc -l` early and again before finishing.\n"
            )
        prompt = (
            f"{preservation.format(next_v=next_v)}\n\n"
            f"Repair contract: file_size\n"
            f"- Target file: `{filename}`\n"
            f"- Evidence: {contract.get('evidence') or 'file_size gate failed'}\n"
            f"- Required outcome: {required}\n\n"
            f"{large_overage}"
            "Required method:\n"
            f"- Edit `{filename}`. This file is listed in `must_change_files`; a no-op or editing only other files is failure.\n"
            "- Prefer deleting duplicated/dead comments, stale historical notes, or redundant helper wrappers before touching active decisions.\n"
            "- Do not remove active strategy branches just to save lines.\n"
            f"- Verify with `wc -l bots/{bot_name(next_v)}/{filename}` before finishing.\n"
            "- End your output with the exact line count you observed."
        )
        return {
            "worker_id": f"auto_quality_repair_file_size_{suffix}",
            "role": "Algorithmic Logic Architect",
            "target_files": [filename],
            "must_change_files": [filename],
            "worker_prompt": prompt,
            "task_kind": task_kind,
            "repair_blocker": "file_size",
            "repair_contract": contract,
        }
    if blocker == "position_semantics":
        prompt = (
            f"{preservation.format(next_v=next_v)}\n\n"
            f"Repair contract: position_semantics\n"
            f"- Target file: `{filename}`\n"
            f"- Flagged locations:\n{_format_position_details(contract.get('details'))}\n\n"
            "Authoritative typed position contract:\n"
            "- Read `decision_context.hand.position` or `decision_context.line.position`; values are `small_blind` and `big_blind`.\n"
            "- Read `decision_context.hand.acts_first_postflop`; it is true only for `big_blind`.\n"
            "- Read `decision_context.line.hero_in_position_postflop`; it is true only for `small_blind`.\n"
            "- Read `decision_context.line.can_donk`, `can_delayed_probe`, and `responding_to_check` as system-derived facts.\n\n"
            "Required method:\n"
            f"- Edit `{filename}`. This file is listed in `must_change_files`; a no-op or editing only another file is failure.\n"
            "- Remove every candidate-side seat/action-order derivation and replace it with a direct read of the typed fields above.\n"
            "- Do not introduce alternate top-level context keys or inspect protocol/runtime internals.\n"
            "- If the flagged line is prose/comment/test text, update that text to the authoritative contract above.\n"
            "- Do not change card mapping, action protocol, or unrelated strategy behavior.\n"
            "- Before finishing, verify every position-dependent branch is sourced from `hand` or `line` in `decision_context`."
        )
        return {
            "worker_id": f"auto_quality_repair_position_{suffix}",
            "role": "Algorithmic Logic Architect",
            "target_files": [filename],
            "must_change_files": [filename],
            "worker_prompt": prompt,
            "task_kind": task_kind,
            "repair_blocker": "position_semantics",
            "repair_contract": contract,
        }
    if blocker in {"national_native_contract", "official_smoke"}:
        raise ValueError(
            f"{blocker} is a fail-closed system-runtime blocker, not a Worker repair"
        )
    if blocker == "runtime_architecture":
        targets = ["policy.py"]
        must_change = ["policy.py"]
        focus_id = str(contract.get("focus_id") or "")
        policy = contract.get("architecture_policy") or {}
        focus = policy.get("selected_focus") or {}
        required_checks = [str(item) for item in contract.get("required_checks") or []]
        preserve_checks = [str(item) for item in contract.get("preserve_checks") or []]
        skill_layer = str(contract.get("skill_layer") or "runtime_architecture")
        runtime_contract = contract.get("runtime_contract") or {}
        owner_files = []
        match_memory = runtime_contract.get("match_memory") or {}
        if isinstance(match_memory, dict) and match_memory.get("owner_file"):
            owner_files.append(Path(str(match_memory["owner_file"])).name)
        for artifact in runtime_contract.get("precompute_artifacts") or []:
            if isinstance(artifact, dict) and artifact.get("owner_file"):
                owner_files.append(Path(str(artifact["owner_file"])).name)
        state_learning = runtime_contract.get("state_learning") or {}
        if (
            state_learning.get("profile_dimensions")
            or state_learning.get("line_controls")
        ):
            owner_files.append("national_bot.py")
        read_only_dependencies = list(dict.fromkeys([
            "national_bot.py",
            "precompute.py",
            *(
                owner
                for owner in owner_files
                if owner in {"national_bot.py", "precompute.py"}
            ),
        ]))
        files_allowed = []
        try:
            selected_state = RuntimeContract.model_validate(runtime_contract).state_learning
            primary_innovation = (
                selected_state.primary_innovation() if selected_state is not None else ""
            )
        except Exception:
            primary_innovation = ""
        primary_guidance = {
            "sample_counted_candidate_batch": (
                "- Primary innovation: publish a sanitized legal baseline, then run real "
                "deadline-scaled candidate batches. Candidate-reported `sample_count`, "
                "`confidence`, and `complete` are diagnostic only; hard proof is system-trusted "
                "iterator steps, CPU/elapsed work, true StopIteration exhaustion, and the sanitized "
                "action trajectory. Stop early at low uncertainty. Design for the local 2-second "
                "strength envelope; the official 55-second ceiling is safety headroom, not a target "
                "to spend on every decision.\n"
            ),
            "action_profile": (
                "- Primary innovation: consume the `action_profile` fields from bounded "
                "`decision_context.opponent`, scale by confidence, and prove a typed-intent "
                "counterfactual plus telemetry.\n"
            ),
            "terminal_response": (
                "- Primary innovation: consume terminal-response fold-to-raise/fold-to-jam/"
                "river-overcall posteriors with confidence and prove a sanitized-action "
                "counterfactual plus telemetry.\n"
            ),
            "showdown_range": (
                "- Primary innovation: consume the selection-aware `showdown_range` posterior "
                "with confidence and prove a tight/loose sanitized-action counterfactual plus telemetry.\n"
            ),
            "donk": (
                "- Primary innovation: consume `decision_context.line.can_donk` and prove its "
                "one-predicate positive/control transcript changes a typed intent and telemetry.\n"
            ),
            "delayed_probe": (
                "- Primary innovation: consume `decision_context.line.can_delayed_probe` and prove "
                "its one-predicate positive/control transcript changes a typed intent and telemetry.\n"
            ),
        }.get(primary_innovation, "")
        if skill_layer in {"match_memory", "opponent_model"}:
            role = "Opponent Modeler"
        else:
            role = "Algorithmic Runtime Architect"
        primary_scope_line = f"- Typed primary innovation: `{primary_innovation or 'none'}`. Other policy dimensions are shadow/advisory unless listed in parent preservation checks.\n"
        prompt = (
            f"{preservation.format(next_v=next_v)}\n\n"
            "Repair contract: runtime_architecture\n"
            f"- Architecture focus: `{focus_id or 'parent_capability_regression'}`\n"
            f"- Focus rationale: {focus.get('rationale') or 'Restore evidence-backed runtime behavior.'}\n"
            f"- Required AST checks: {', '.join(required_checks)}\n"
            f"- Parent checks that must not regress: {', '.join(preserve_checks)}\n"
            "- Writable candidate file: `policy.py` only.\n"
            f"- Files that must change: {', '.join(f'`{item}`' for item in must_change)}\n"
            f"- Read-only system dependencies: {', '.join(f'`{item}`' for item in read_only_dependencies) or 'none'}; never edit these files.\n"
            f"{primary_scope_line}"
            f"- Detector evidence:\n{contract.get('evidence') or 'transition hard gate failed'}\n\n"
            "Executable RuntimeContract (implement it; do not merely copy its names):\n"
            f"```json\n{json.dumps(runtime_contract, ensure_ascii=False, indent=2)}\n```\n\n"
            "Required method:\n"
            "- Read every target plus the source-parent counterpart before editing. Preserve the legal fast baseline.\n"
            "- Implement the behavior in policy.get_baseline_decision and/or policy.iter_decisions over the schema-versioned decision_context. A class, cache, label, comment, or telemetry field that neither entrypoint consumes is failure.\n"
            f"{primary_guidance}"
            "- Treat decision_context.hand/betting/history/line/legal/opponent as the only authoritative decision input; never reconstruct another protocol history.\n"
            "- Do not weaken native TCP, official wire, card mapping, or any parent capability to make the selected check pass.\n"
            "- Run `evaluate_national_capabilities` on the candidate and report the required check states before finishing."
        )
        return {
            "worker_id": f"auto_runtime_architecture_{_task_id_suffix(focus_id or filename)}",
            "role": role,
            "target_files": targets,
            "files_allowed": files_allowed,
            "read_only_dependencies": read_only_dependencies,
            "must_change_files": must_change,
            "worker_prompt": prompt,
            "task_kind": task_kind,
            "repair_blocker": "runtime_architecture",
            "repair_contract": contract,
            "skill_layer": skill_layer,
            "architecture_focus_id": focus_id,
            "runtime_contract": runtime_contract,
            "checks_required": required_checks,
        }
    evidence = contract.get('evidence') or 'quality gate failed'
    if contract.get("role_hint") == "tuner":
        role = "Hyperparameter Tuner"
    elif contract.get("role_hint") == "scope_revert":
        role = "Scope Boundary Repair Architect"
    else:
        role = "Algorithmic Logic Architect"
    reachability_guidance = ""
    if "reachability" in str(evidence).lower():
        reachability_guidance = (
            "\nReachability-specific method:\n"
            "- If the flagged symbol is a top-level `_self_test_*` or probe helper, "
            "remove it or move the assertions under `if __name__ == \"__main__\":`.\n"
            "- If the helper is real runtime logic, wire it into the actual strategy "
            "dispatch path that consumes its result.\n"
            "- Do not add a dummy reference, unused import, or unreachable call just "
            "to silence the gate.\n"
        )
    role_guidance = ""
    if role == "Hyperparameter Tuner":
        role_guidance = (
            "\nConstants-only role method:\n"
            "- This repair is assigned to Hyperparameter Tuner because the reviewer "
            "evidence concerns an existing numeric constant/threshold in `policy.py`.\n"
            "- Edit only an existing numeric constant in `policy.py`; do not add imports, functions, classes, loops, "
            "or control flow.\n"
            "- Fix the exact reviewer evidence by reverting or retuning the named "
            "numeric constant as a Tuner-owned change, with adjacent rationale if needed.\n"
            "- Do not touch protocol/card mapping or non-constant strategy code.\n"
        )
    elif role == "Scope Boundary Repair Architect":
        role_guidance = (
            "\nScope-drift repair method:\n"
            "- The reviewer evidence says this file changed outside the approved worker scope.\n"
            "- Apply only the exact rollback described in the injected evidence; the source parent is not readable by the Worker.\n"
            "- Do not add strategy thresholds, protocol refactors, helper subsystems, or action-behavior changes.\n"
            "- Keep the repair limited to restoring the approved scope boundary; other candidate files are intentionally preserved.\n"
        )
    prompt = (
        f"{preservation.format(next_v=next_v)}\n\n"
        f"Repair contract: quality_gate\n"
        f"- Target file: `{filename}`\n"
        f"- Evidence:\n{evidence}\n\n"
        f"{reachability_guidance}"
        f"{role_guidance}"
        "Required method:\n"
        f"- Edit `{filename}`. This file is listed in `must_change_files`; a no-op or editing only another file is failure.\n"
        "- Fix only the listed gate blocker.\n"
        "- Preserve national protocol/card mapping and previously passing behavior.\n"
        "- Run `python -m py_compile` on the exact edited file before finishing; system gates own imports and execution."
    )
    return {
        "worker_id": f"auto_quality_repair_gate_{suffix}",
        "role": role,
        "target_files": [filename],
        "must_change_files": [filename],
        "worker_prompt": prompt,
        "task_kind": task_kind,
        "repair_blocker": "quality_gate",
        "repair_contract": contract,
    }


def _text_line_count(*args, **kwargs):
    """Delegate to tool_planning_quality_rework."""
    return _qc._text_line_count(*args, **kwargs)


def _docstring_line_ranges(*args, **kwargs):
    """Delegate to tool_planning_quality_rework."""
    return _qc._docstring_line_ranges(*args, **kwargs)


def _tokenized_comment_and_string_lines(*args, **kwargs):
    """Delegate to tool_planning_quality_rework."""
    return _qc._tokenized_comment_and_string_lines(*args, **kwargs)


def _mechanically_trim_python_text(*args, **kwargs):
    """Delegate to tool_planning_quality_rework."""
    return _qc._mechanically_trim_python_text(*args, **kwargs)


def _mechanical_trim_python_file(*args, **kwargs):
    """Delegate to tool_planning_quality_rework."""
    return _qc._mechanical_trim_python_file(*args, **kwargs)


def _apply_mechanical_file_size_trims(*args, **kwargs):
    """Delegate to tool_planning_quality_rework."""
    return _qc._apply_mechanical_file_size_trims(*args, **kwargs)


def _precommit_repair_task(*args, **kwargs):
    """Delegate to tool_planning_quality_rework."""
    return _qc._precommit_repair_task(*args, **kwargs)


def _precommit_repair_tasks(*args, **kwargs):
    """Delegate to tool_planning_quality_rework."""
    return _qc._precommit_repair_tasks(*args, **kwargs)


def _precommit_repair_task_refresh_reason(*args, **kwargs):
    """Delegate to tool_planning_quality_rework."""
    return _qc._precommit_repair_task_refresh_reason(*args, **kwargs)


def _synthesize_rework_tasks_from_checkpoint(*args, **kwargs):
    """Delegate to tool_planning_quality_rework."""
    return _qc._synthesize_rework_tasks_from_checkpoint(*args, **kwargs)


def _transport_equivalent_feedback(left, right):
    """Compare an MCP-carried feedback string without granting rewrite power.

    JSON/TCP transports may normalize line endings or omit one surrounding
    newline.  Those representations are equivalent; changing any non-boundary
    content is not.  In particular, whitespace inside paths/evidence remains
    significant so a caller cannot smuggle a second repair directive.
    """

    def normalize(value):
        if not isinstance(value, str):
            return None
        return value.replace("\r\n", "\n").replace("\r", "\n").strip()

    normalized_left = normalize(left)
    normalized_right = normalize(right)
    return (
        normalized_left is not None
        and normalized_right is not None
        and normalized_left == normalized_right
    )


def _repair_contract_signature(task, next_v):
    """Return a content signature for one system-owned repair contract.

    This is an integrity receipt, not caller authority.  It binds the gate
    contract to the writable task scope; execute_workers still compares the
    complete canonical task list before accepting a non-empty caller echo.
    """
    if not isinstance(task, dict):
        return ""
    contract = task.get("repair_contract")
    if not isinstance(contract, dict) or not str(contract.get("blocker") or "").strip():
        return ""

    raw_contract_files = contract.get("files")
    if raw_contract_files is None:
        raw_contract_files = [contract.get("file")]
    if not isinstance(raw_contract_files, (list, tuple)):
        return ""
    contract_files = set()
    for target in raw_contract_files:
        rel = _target_rel(target, next_v)
        if not rel:
            return ""
        contract_files.add(rel)

    writable_files = _task_declared_scope_files(task, next_v)
    # The contract's primary file(s) must be writable.  A system runtime
    # contract may derive additional files_allowed from typed owner_file fields;
    # those are bound by writable_files in the signature payload even when the
    # human-facing repair_contract keeps only its primary targets.
    if not contract_files or not writable_files or not contract_files.issubset(writable_files):
        return ""
    payload = {
        "repair_contract": contract,
        "writable_files": sorted(writable_files),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _authoritative_rework_tasks(ckpt, feedback):
    """Rebuild the only tasks authorized by immutable checkpoint/gate evidence."""
    tasks = _synthesize_rework_tasks_from_checkpoint(ckpt, feedback)
    errors = []
    if not tasks:
        errors.append("system_repair_task_synthesis_empty")
        return [], errors
    for index, task in enumerate(tasks):
        if not _repair_contract_signature(task, ckpt.get("next_v")):
            worker_id = task.get("worker_id") if isinstance(task, dict) else None
            errors.append(
                f"task[{index}]_repair_contract_signature_invalid:{worker_id or 'unknown'}"
            )
    return tasks, errors


def _should_reset_before_rework(ckpt, tasks):
    """Return False for in-place repairs that must preserve the current candidate."""
    if not isinstance(ckpt, dict):
        return True
    if _is_precommit_rework_checkpoint(ckpt):
        return False
    stage = ckpt.get("stage")
    if stage not in {"quality_failed", "repair_planned", "rework_running", "official_failed"}:
        return True
    master_plan = ckpt.get("master_plan") if isinstance(ckpt.get("master_plan"), dict) else {}
    work_item = master_plan.get("work_item") if isinstance(master_plan.get("work_item"), dict) else {}
    work_kind = str(work_item.get("kind") or "")
    task_kinds = {
        str(task.get("task_kind") or "")
        for task in tasks or []
        if isinstance(task, dict)
    }
    is_official_repair = (
        "official_repair" in work_kind
        or any("official_repair" in kind for kind in task_kinds)
        or _is_official_rework_checkpoint(ckpt)
    )
    if is_official_repair:
        return False
    is_review_repair = (
        "review_repair" in work_kind
        or any("review_repair" in kind for kind in task_kinds)
        or _is_review_rework_checkpoint(ckpt)
    )
    if is_review_repair:
        return False
    is_quality_repair = (
        stage == "quality_failed"
        or "quality_repair" in work_kind
        or work_kind == "crossover_gate_rework"
        or any("quality_repair" in kind for kind in task_kinds)
    )
    if is_quality_repair and "precommit" not in work_kind:
        return False
    is_crossover = (
        bool(ckpt.get("parent2_v"))
        or master_plan.get("strategy") == "crossover"
        or work_kind.startswith("crossover_")
    )
    if not is_crossover:
        return True
    return True

# Repair-target extraction + contract-emission subsystem extracted to
# tool_planning_quality_repair_targets.py. Imported last (after every helper,
# constant, and retained function above) so the companion's top-level
# ``import tool_planning_quality_contracts as _qc`` sees fully-defined symbols
# and there is no circular import at module load. ``# noqa: E402`` because this
# is intentionally at the bottom of the file; ``# noqa: F401`` because the
# symbols are re-exported, not used here.
from tool_planning_quality_repair_targets import (  # noqa: E402,F401
    _ARCHITECTURE_FOCUS_LAYERS,
    _PRECOMMIT_PROTOCOL_EVIDENCE_MARKERS,
    _PRECOMMIT_PROTOCOL_REPAIR_FILES,
    _PRECOMMIT_STRATEGY_REPAIR_FILES,
    _REVERT_FEEDBACK_MARKERS,
    _SCOPE_DRIFT_FEEDBACK_MARKERS,
    _STATE_LEARNING_ORACLE_REFS,
    _architecture_contracts,
    _architecture_default_runtime_contract,
    _architecture_repair_context,
    _candidate_consumed_precompute_contracts,
    _declared_scope_violation_files,
    _default_state_learning_contract,
    _detected_artifact_consumer,
    _feedback_quality_contracts,
    _flatten_text_items,
    _generic_quality_contracts,
    _has_scope_drift_marker,
    _is_official_smoke_protocol_failure_text,
    _is_position_semantics_failure_text,
    _is_precommit_rework_checkpoint,
    _is_runtime_architecture_failure_text,
    _limit_precommit_repair_targets,
    _line_count_contracts,
    _merge_runtime_contract_floor,
    _national_native_contracts,
    _official_deterministic_failure_items,
    _official_failure_is_protocol,
    _official_failure_items,
    _official_repair_target_files,
    _official_repair_tasks,
    _official_smoke_contracts,
    _position_contracts,
    _precommit_changed_python_files,
    _precommit_failure_items,
    _precommit_filter_repair_targets,
    _precommit_protocol_compliance_failure,
    _precommit_repair_target_files,
    _primary_feedback_file,
    _quality_repair_contracts,
    _review_feedback_items,
    _review_primary_feedback_text,
    _review_repair_target_files,
    _scope_drift_feedback_files,
    _split_reviewer_quality_feedback,
)
