"""Pipeline tools: quality gates, code preparation, review, and critic."""

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path

from bot_namespace import bot_name, bot_tag
from tool_runtime_guard import tool

from logging_config import get_logger
_log = get_logger("gates")

from evolution_core import (
    get_bot_dir,
    get_logs_dir,
    find_current_v,
    verify_code,
    run_import_contract_test,
    check_code_size,
    run_smoke_test,
    run_decision_test_details,
    run_national_protocol_tests,
    parse_json_output,
    run_claude_query,
    _run_critic,
)
from tool_helpers import (
    _get_ui, _json_tool_result,
    _matching_checkpoint, _record_gate, _gate_payload, _state_blocked,
    _execute_exhausted_infrastructure_failure, _owned_infrastructure_failure,
    _record_infrastructure_failure,
    _prepare_official_profile_refresh,
    _quality_gate_ok, _review_gate_ok, _critic_gate_ok,
    _py_files_changed_between, _resolve_version_args, PROJECT_ROOT,
    _set_pipeline_status, read_pipeline_checkpoint,
)
from fix_verification import verify_fixes
from system_log import log_system_event
from llm_failure import is_llm_infra_error, infra_payload
from llm_query import llm_cancel_scope
import spot_analyzer
from pipeline_schema import GateResult, ScoreCard
from gate_execution import GateExecution
from pipeline_state import next_tool_for_checkpoint
from pipeline_infrastructure import (
    build_infrastructure_failure,
    infrastructure_attempt_key,
    infrastructure_failure_digest,
)
from workflow_profiles import get_workflow_profile
from worker_boundary import audit_changed_files_against_plan, hash_changed_files
from national_position_contract import detect_position_semantics_errors
from blocking_runtime import run_blocking_isolated

try:
    from candidate_store import append_candidate_event
except Exception:  # pragma: no cover - import fallback for unusual test paths
    append_candidate_event = None


# ── Phase 2: AgentAssay SPRT decision-test gate (feature-flagged) ──
# When True, the decision-test quality gate uses run_decision_tests_sprt_aggregate
# (per-scenario Wald SPRT with sequential early-stop) instead of the classic
# single-shot run_decision_test_details. SPRT gives tighter type-I control for
# stochastic LLM bots but has only been validated on synthetic unit tests, so the
# default is OFF (zero-regression: byte-for-byte the classic path). Flip to True
# only after a gray-run confirms the type-I rate on real generation traffic.
# Mirrors the PRECOMMIT_SEQUENTIAL_EARLY_STOP flag convention (module constant,
# not an env var).
DECISION_TEST_SPRT_ENABLED = False
DYNAMIC_TEST_LLM_TIMEOUT = int(os.environ.get("POK_DYNAMIC_TEST_LLM_TIMEOUT", "25"))
DYNAMIC_TEST_HEURISTIC_SUFFICIENT = int(os.environ.get("POK_DYNAMIC_TEST_HEURISTIC_SUFFICIENT", "4"))
QUALITY_INFRA_MAX_ATTEMPTS = max(
    1, int(os.environ.get("POK_QUALITY_INFRA_MAX_ATTEMPTS", "3"))
)
QUALITY_INFRA_CONTRACT_VERSION = 1
DYNAMIC_TEST_LLM_ENABLED = os.environ.get("POK_DYNAMIC_TEST_LLM_ENABLED", "0").strip().lower() in {
    "1", "true", "yes", "on"
}


def _env_enabled(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _official_gate_enabled(name: str, *, include_required: bool = True) -> bool:
    return (include_required and _env_enabled("POK_OFFICIAL_REQUIRED")) or _env_enabled(name)


async def _request_official_smoke_status(bot_dir: Path, mode: str) -> dict:
    """Request smoke evidence using only a policy-eligible official opponent."""
    from official_certification import (
        STATUS_FAILED,
        STATUS_INCONCLUSIVE,
        STATUS_PENDING,
        build_spec,
        official_compliance_verdict,
        select_official_opponent,
    )
    from official_certification_job import start_or_poll_job

    preferred = os.environ.get("POK_OFFICIAL_OPPONENT", "").strip() or None
    selection = select_official_opponent(
        bot_dir,
        preferred=preferred,
        allow_bootstrap_grandfather=False,
    )
    if not selection.get("selected"):
        return {
            "status": STATUS_INCONCLUSIVE,
            "mode": "smoke",
            "issues": ["official_smoke_no_eligible_opponent"],
            "blocking": False,
            "inconclusive": True,
            "classification": "inconclusive",
            "opponent_selection": selection,
        }

    opponent = selection["opponent"]["path"]
    spec = build_spec("smoke", bot_dir, opponent=opponent)
    job = await run_blocking_isolated(
        start_or_poll_job,
        spec,
        thread_name_prefix="official-smoke",
        opponent_selection=selection,
    )
    status = (
        job.get("status")
        if job.get("state") == "completed" and isinstance(job.get("status"), dict)
        else {
            "status": STATUS_PENDING,
            "mode": "smoke",
            "queued": job.get("state") == "queued",
            "pending": bool(job.get("pending")),
            "issues": list(job.get("issues") or []),
            "official_job": job,
            "summary": {
                "self_play_rounds": (
                    spec.get("self_play_rounds") if isinstance(spec, dict) else spec.self_play_rounds
                ),
                "opponent_rounds": (
                    spec.get("opponent_rounds") if isinstance(spec, dict) else spec.opponent_rounds
                ),
                "target_hands": (
                    spec.get("target_hands") if isinstance(spec, dict) else spec.target_hands
                ),
            },
        }
    )

    verdict = official_compliance_verdict(status)
    return {
        **status,
        "blocking": bool(verdict.get("blocking")),
        "inconclusive": bool(verdict.get("inconclusive")),
        "classification": str(verdict.get("classification") or "passed_or_pending"),
        "opponent_selection": status.get("opponent_selection") or selection,
        "request_opponent_selection": selection,
        "official_job": job,
    }


async def _run_workflow_smoke_gate(
    *,
    bot_dir: Path,
    source_v: int | None,
    native_tcp_mode: bool,
    compile_errors: list,
    import_errors: list,
    protected_contract_errors: list,
    native_contract_errors: list,
    embedded_selftest_errors: list,
) -> tuple[list[str], dict]:
    """Run the smoke gate that matches the active workflow backend."""
    if not native_tcp_mode:
        try:
            return run_smoke_test(bot_dir), {"execution_mode": "local_json"}
        except Exception as exc:
            issue = f"local_smoke_exception={type(exc).__name__}: {str(exc)[:500]}"
            return [issue], {
                "passed": False,
                "execution_mode": "local_json",
                "failure_class": "infrastructure",
                "outcome": "infrastructure_failure",
                "failure_side": "harness",
                "issues": [issue],
            }

    blocking_prereqs = (
        list(compile_errors or [])
        + list(import_errors or [])
        + list(protected_contract_errors or [])
        + list(native_contract_errors or [])
        + list(embedded_selftest_errors or [])
    )
    if blocking_prereqs:
        return [], {
            "execution_mode": "native_tcp",
            "skipped": True,
            "reason": "prerequisite_gate_failed",
        }

    hands = int(os.environ.get("POK_NATIVE_SMOKE_HANDS", "1"))
    timeout_sec = float(os.environ.get("POK_NATIVE_SMOKE_TIMEOUT_SEC", "90"))
    try:
        from national_native import run_native_tcp_smoke
        report = await run_native_tcp_smoke(
            bot_dir,
            source_v=source_v,
            hands=hands,
            timeout_sec=timeout_sec,
        )
    except Exception as exc:
        report = {
            "passed": False,
            "execution_mode": "native_tcp",
            "failure_class": "infrastructure",
            "outcome": "infrastructure_failure",
            "failure_side": "harness",
            "issues": [f"native_smoke_exception={type(exc).__name__}: {str(exc)[:500]}"],
        }
    errors = list(report.get("issues") or []) if not report.get("passed") else []
    return errors, report


def _declared_scope_tasks_from_plan(
    master_plan,
    checkpoint=None,
    *,
    include_prepare_scope=True,
):
    tasks = []
    if isinstance(master_plan, dict):
        raw_tasks = master_plan.get("tasks", []) or []
        if isinstance(raw_tasks, list):
            tasks.extend(raw_tasks)
        raw_repair_scope = master_plan.get("repair_scope_files", []) or []
        if not isinstance(raw_repair_scope, list):
            raw_repair_scope = []
        repair_scope_files = [
            str(item).strip()
            for item in raw_repair_scope
            if str(item).strip()
        ]
        if repair_scope_files:
            tasks.append({
                "worker_id": "repair_scope_history",
                "role": "Scope Ledger",
                "target_files": [],
                "files_allowed": sorted(set(repair_scope_files)),
            })
    if include_prepare_scope and isinstance(checkpoint, dict):
        raw_prepare_scope = checkpoint.get("prepare_scope_files", []) or []
        if not isinstance(raw_prepare_scope, list):
            raw_prepare_scope = []
        prepare_scope_files = [
            str(item).strip()
            for item in raw_prepare_scope
            if str(item).strip()
        ]
        if prepare_scope_files:
            tasks.append({
                "worker_id": "prepare_scope_history",
                "role": "Prepare Scope Ledger",
                "target_files": [],
                "files_allowed": sorted(set(prepare_scope_files)),
            })
    return tasks


def _is_crossover_scope_checkpoint(ckpt, master_plan):
    if not isinstance(ckpt, dict):
        ckpt = {}
    if not isinstance(master_plan, dict):
        master_plan = {}
    work_item = master_plan.get("work_item") if isinstance(master_plan.get("work_item"), dict) else {}
    return (
        bool(ckpt.get("parent2_v"))
        or master_plan.get("strategy") == "crossover"
        or str(work_item.get("kind", "")).startswith("crossover_")
    )


def _master_plan_with_crossover_scope(master_plan, ckpt, changed_files):
    """Return the declared plan without deriving authority from the diff.

    Crossover preparation files are already frozen in the checkpoint's
    ``prepare_scope_files`` ledger and appended by
    :func:`_declared_scope_tasks_from_plan`.  Promoting every observed changed
    file into ``repair_scope_files`` made the final scope audit tautological:
    an out-of-band or recovery-time edit authorized itself merely by appearing
    in the diff.  Keep this compatibility helper side-effect free; authority is
    the prepared ledger plus explicit Master/repair task scope only.
    """
    return master_plan


def _crossover_post_master_delta(checkpoint, candidate_artifact_hash):
    """Verify Workers changed the common frozen prepared artifact."""
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    audit_context = checkpoint.get("audit_context") or {}
    prepared = (
        audit_context.get("prepared_artifact_contract")
        if isinstance(audit_context, dict)
        else None
    )
    if not isinstance(prepared, dict):
        crossover_baseline = (
            audit_context.get("prepared_baseline_contract")
            if isinstance(audit_context, dict)
            else None
        )
        if isinstance(crossover_baseline, dict):
            prepared = crossover_baseline.get("prepared_artifact_contract")
    required = bool(
        checkpoint.get("next_v") is not None
        and checkpoint.get("source_v") is not None
    )
    prepared = prepared if isinstance(prepared, dict) else {}
    prepared_hash = str(
        prepared.get("prepared_artifact_hash") or ""
        if isinstance(prepared, dict)
        else ""
    )
    candidate_hash = str(candidate_artifact_hash or "")
    ok = bool(
        not required
        or (prepared_hash and candidate_hash and candidate_hash != prepared_hash)
    )
    return required, ok, prepared_hash


def _prepared_artifact_delta_files(checkpoint, candidate_dir):
    """Diff the frozen prepared manifest against the final complete artifact."""
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    audit_context = checkpoint.get("audit_context") or {}
    contract = (
        audit_context.get("prepared_artifact_contract")
        if isinstance(audit_context, dict)
        else None
    )
    if not isinstance(contract, dict):
        crossover_baseline = (
            audit_context.get("prepared_baseline_contract")
            if isinstance(audit_context, dict)
            else None
        )
        if isinstance(crossover_baseline, dict):
            contract = crossover_baseline.get("prepared_artifact_contract")
    if not isinstance(contract, dict):
        return [], ["prepared_artifact_contract_missing_for_scope"]
    try:
        from prepared_baseline_contract import validate_prepared_artifact_contract

        contract_errors = validate_prepared_artifact_contract(
            contract,
            source_v=checkpoint.get("source_v"),
            next_v=checkpoint.get("next_v"),
            verify_live_content=False,
        )
    except Exception as exc:
        return [], [
            "prepared_baseline_contract_scope_validation_error:"
            f"{type(exc).__name__}: {str(exc)[:200]}"
        ]
    if contract_errors:
        return [], [f"prepared_scope:{error}" for error in contract_errors]
    prepared_manifest = contract.get("prepared_artifact_manifest")
    if not isinstance(prepared_manifest, dict):
        return [], ["prepared_artifact_manifest_missing_for_scope"]
    try:
        from bot_artifact import artifact_manifest

        current_manifest = artifact_manifest(candidate_dir)
    except Exception as exc:
        return [], [
            f"candidate_artifact_manifest_error:{type(exc).__name__}: {str(exc)[:200]}"
        ]

    def _entries(manifest):
        entries = {}
        for item in manifest.get("entries") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "file":
                continue
            path = str(item.get("path") or "")
            if not path or path == ".":
                continue
            entries[path] = {
                key: item.get(key)
                for key in ("type", "size", "sha256")
                if key in item
            }
        return entries

    prepared_entries = _entries(prepared_manifest)
    current_entries = _entries(current_manifest)
    changed = sorted(
        path
        for path in set(prepared_entries) | set(current_entries)
        if prepared_entries.get(path) != current_entries.get(path)
    )
    return changed, []


def _prepared_artifact_change_status(checkpoint, candidate_dir, candidate_artifact_hash):
    """Return the blocking post-prepare file-delta verdict and evidence."""
    required, hash_delta_ok, prepared_hash = _crossover_post_master_delta(
        checkpoint,
        candidate_artifact_hash,
    )
    changed_files, scope_errors = _prepared_artifact_delta_files(
        checkpoint,
        candidate_dir,
    )
    # Full hashes include directory entries, but empty directory churn is not
    # a decision innovation.  For a real generation, regular-file delta is the
    # only authoritative verdict.  The fallback preserves legacy no-source
    # diagnostic callers where no prepared contract is required.
    changed_ok = (
        bool(changed_files) and not scope_errors
        if required
        else bool(hash_delta_ok)
    )
    return {
        "required": required,
        "changed_ok": changed_ok,
        "prepared_artifact_hash": prepared_hash,
        "changed_files": changed_files,
        "scope_errors": scope_errors,
    }

def _record_quality_failure(gen, worker_id, role, error, **extra):
    """Record a quality gate rejection (reviewer/critic) to worker_failures.jsonl.

    RC5: category="gate" separates these strategic rejections from real
    worker-exec failures (_record_worker_failure writes category="worker") so
    the Worker Failures view can filter out the 49 critic / 9 reviewer noise
    and surface only genuine compile/timeout crashes.
    """
    from evolution_core import WORKER_FAILURES_FILE, locked_file
    entry = {"gen": gen, "worker_id": worker_id, "role": role, "error": error,
             "timestamp": time.time(), "category": "gate"}
    entry.update({k: v for k, v in extra.items() if v is not None and v is not False})
    with locked_file(WORKER_FAILURES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _idempotency_check(v, source_v, stage_set, gate_name, approval_key="approved",
                       extra_ok_keys=(), directive="", cache_validator=None):
    """Check if a pipeline stage has already been completed; return cached result or None.

    Args:
        v: Bot version.
        source_v: Parent version.
        stage_set: Tuple/list of stage strings that mean "this stage passed".
        gate_name: Key inside gate_results (e.g. "quality", "review", "critic").
        approval_key: The key to check for truthiness (default "approved").
        extra_ok_keys: Additional keys that count as truthy (e.g. ("force_advanced",)).
        directive: Message to include when returning cached result.

    Returns:
        An MCP-formatted result dict if the stage already passed, or None.
    """
    ckpt = _matching_checkpoint(v, source_v)
    if not ckpt or ckpt.get("stage") not in stage_set:
        return None
    gate = ckpt.get("gate_results", {}).get(gate_name, {})
    if gate.get(approval_key) is True or any(gate.get(k) is True for k in extra_ok_keys):
        if cache_validator is not None and not cache_validator(gate):
            return None
        gate["idempotent_cache"] = True
        gate["checkpoint_recorded"] = True
        gate["directive"] = directive
        return _json_tool_result(gate)
    return None


def _bot_code_fingerprint(bot_dir):
    """Content hash of the complete decision artifact for gate cache validity.

    The persisted field name predates data/model-backed bots, but its value must
    cover every artifact file that can affect a decision.  ``hash_path`` uses
    the shared deterministic manifest and intentionally excludes only runtime
    completion/cache artifacts such as ``.completed`` and ``__pycache__``.
    """
    root = Path(bot_dir)
    if not root.exists():
        return ""
    try:
        from bot_artifact import hash_path

        return hash_path(root)
    except Exception:
        # Callers treat an empty fingerprint as unavailable and final commit
        # fails closed.  Do not bless a partial manifest after an I/O race or an
        # unsafe artifact entry.
        return ""


def _transient_task_context_errors(bot_dir):
    """Reject unpublished compiler briefs before quality/certification."""
    from candidate_hygiene import transient_control_artifact_errors

    return transient_control_artifact_errors(bot_dir)


def _llm_gate_infrastructure_identity(
    *,
    component,
    role,
    candidate_dir,
    source_dir,
    prompt_text,
    checkpoint,
):
    """Bind LLM-gate retries to code, prompt, backend, and runtime contract."""
    prompt_digest = hashlib.sha256(str(prompt_text).encode("utf-8")).hexdigest()
    ledger = (checkpoint or {}).get("runtime_contract_ledger") or {}
    contract_digest = str(ledger.get("ledger_digest") or "")
    backend_contract = {
        key: os.environ.get(key, "")
        for key in (
            "ANTHROPIC_MODEL",
            "CLAUDE_MODEL",
            "POK_LLM_MODEL",
            "ANTHROPIC_BASE_URL",
        )
    }
    attempt_key = infrastructure_attempt_key(
        component=component,
        candidate_fingerprint=_bot_code_fingerprint(candidate_dir),
        source_fingerprint=_bot_code_fingerprint(source_dir),
        harness_identity=prompt_digest,
        contract_identity=contract_digest,
        extra={"role": role, "backend_contract": backend_contract},
    )
    return attempt_key, {
        "role": role,
        "prompt_digest": prompt_digest,
        "candidate_fingerprint": _bot_code_fingerprint(candidate_dir),
        "source_fingerprint": _bot_code_fingerprint(source_dir),
        "runtime_contract_ledger_digest": contract_digest,
        "backend_contract": backend_contract,
    }


# ──────────────────────────────────────────────
# Quality Gates
# ──────────────────────────────────────────────

@tool("run_quality_gates", "Run all quality gates on a bot: code_changed, declared_scope, compile/runtime import, protected contracts, smoke, national protocol/acceptance, decision, size, fix verification, telemetry fidelity, and reachability.", {"version": int, "source_v": int})
async def run_quality_gates(args):
    _t0 = time.time()
    v, source_v = _resolve_version_args(args)
    if v is None:
        return _json_tool_result({"error": "Missing version and no active pipeline checkpoint"})
    v = int(v)
    source_v = int(source_v) if source_v is not None else None
    active_ckpt = _matching_checkpoint(v, source_v) if source_v is not None else _matching_checkpoint(v)
    profile_refresh = _prepare_official_profile_refresh(active_ckpt, "run_quality_gates")
    if not profile_refresh.get("ok"):
        return _state_blocked(
            str(profile_refresh.get("error") or "official profile refresh preparation failed"),
            v,
            source_v,
            active_ckpt,
        )
    existing_infra, infra_error = _owned_infrastructure_failure(
        active_ckpt,
        "run_quality_gates",
    )
    if infra_error:
        return _state_blocked(infra_error, v, source_v, active_ckpt)
    exhausted_result = await _execute_exhausted_infrastructure_failure(
        v,
        source_v,
        owner_tool="run_quality_gates",
    )
    if exhausted_result is not None:
        exhausted_result["all_passed"] = False
        return _json_tool_result(exhausted_result)
    quality_infra_issues: list[GateExecution] = []

    def mark_quality_infrastructure(component, phase, issue):
        quality_infra_issues.append(GateExecution.infrastructure(
            str(component),
            str(phase),
            [issue],
            side="system",
        ))
    if active_ckpt and active_ckpt.get("stage") in {"quality_failed", "repair_planned", "rework_running"}:
        next_tool = next_tool_for_checkpoint(active_ckpt)
        message = (
            f"run_quality_gates is not valid while checkpoint stage is "
            f"{active_ckpt.get('stage')}; next tool is {next_tool}. "
            "Call execute_workers with the saved repair tasks and exact quality failures first."
        )
        log_system_event(
            "pipeline.quality_gate_blocked_rework",
            "warn",
            message,
            {
                "version": v,
                "source_v": source_v,
                "stage": active_ckpt.get("stage"),
                "next_tool": next_tool,
            },
        )
        return _state_blocked(message, v, source_v, checkpoint=active_ckpt)
    bot_dir = get_bot_dir(v)
    try:
        from candidate_hygiene import cleanup_transient_candidate_artifacts

        cleanup_transient_candidate_artifacts(
            bot_dir,
            include_task_context=False,
        )
    except Exception as exc:
        return _json_tool_result({
            "error": "CANDIDATE_TRANSIENT_ARTIFACT_CLEANUP_FAILED",
            "version": v,
            "source_v": source_v,
            "failure_class": "candidate_integrity",
            "message": f"{type(exc).__name__}: {str(exc)[:300]}",
        })
    workflow_profile = get_workflow_profile()
    native_tcp_mode = getattr(workflow_profile, "national_execution_mode", "adapter") == "native_tcp"
    if native_tcp_mode and not (bot_dir / "national_bot.py").exists():
        try:
            from national_native import ensure_native_entry
            ensure_native_entry(bot_dir)
            log_system_event(
                "pipeline.native_entry_recovered",
                "info",
                f"Recovered missing native national TCP entry for v{v} before quality gates",
                {
                    "version": v,
                    "source_v": source_v,
                    "workflow_profile_id": workflow_profile.profile_id,
                },
            )
        except Exception as exc:
            mark_quality_infrastructure(
                "native_entry_recovery",
                "candidate_hygiene",
                f"{type(exc).__name__}: {str(exc)[:300]}",
            )
            log_system_event(
                "pipeline.native_entry_recovery_failed",
                "error",
                f"Failed to recover native national TCP entry for v{v}: {type(exc).__name__}: {str(exc)[:200]}",
                {
                    "version": v,
                    "source_v": source_v,
                    "workflow_profile_id": workflow_profile.profile_id,
                },
            )
    candidate_id = f"{bot_name(v)}_from_{bot_name(source_v)}" if source_v is not None else bot_name(v)
    if append_candidate_event:
        try:
            append_candidate_event(
                "quality_started",
                version=v,
                source_v=source_v,
                candidate_id=candidate_id,
                profile_id=workflow_profile.profile_id,
                workflow_profile_id=workflow_profile.profile_id,
                run_id=f"{v}#0",
                stage="quality",
                parent_ids=[bot_name(source_v)] if source_v is not None else [],
            )
        except Exception as e:
            _log.warning("candidate ledger quality_started write failed: %s", e)

    _set_pipeline_status(f"Running quality gates for v{v}")

    # Keep the source-to-candidate Python diff for AST reachability and dynamic
    # scenario generation.  It is telemetry/coverage input, not authority for
    # the blocking innovation gate: crossover preparation may already differ
    # from the source, and decision assets may be non-Python.
    source_python_changed = True
    changed_files_list = []
    source_dir = None
    if source_v is not None:
        source_dir = get_bot_dir(source_v)
        changed_files_list = [p for p in _py_files_changed_between(source_dir, bot_dir) if 'backup' not in p]
        source_python_changed = len(changed_files_list) > 0
    code_fingerprint = _bot_code_fingerprint(bot_dir)

    declared_scope_ok = True
    declared_scope_errors = []
    declared_scope_metrics = {}
    declared_skill_layers = []
    _quality_ckpt_for_scope = _matching_checkpoint(v, source_v) if source_v is not None else _matching_checkpoint(v)
    _master_plan_for_scope = (_quality_ckpt_for_scope or {}).get("master_plan", {})
    _master_plan_for_scope = _master_plan_with_crossover_scope(
        _master_plan_for_scope,
        _quality_ckpt_for_scope,
        changed_files_list,
    )
    _artifact_change = _prepared_artifact_change_status(
        _quality_ckpt_for_scope,
        bot_dir,
        code_fingerprint,
    )
    post_master_delta_required = _artifact_change["required"]
    post_master_delta_ok = _artifact_change["changed_ok"]
    prepared_artifact_hash = _artifact_change["prepared_artifact_hash"]
    post_master_changed_files = _artifact_change["changed_files"]
    post_master_scope_errors = _artifact_change["scope_errors"]
    # A directory-only hash change is not a strategy innovation.  Require at
    # least one changed regular artifact file after the frozen prepared state;
    # this also makes declared binary/model/table assets first-class changes.
    code_changed = (
        post_master_delta_ok
        if post_master_delta_required
        else source_python_changed
    )
    diff_hash = (
        hash_changed_files(bot_dir, post_master_changed_files)
        if post_master_changed_files
        else ""
    )
    if not code_changed:
        log_system_event(
            "pipeline.quality_no_changes",
            "error",
            f"Quality gates: v{v} has no changed decision artifact file after the frozen prepared baseline",
            {
                "version": v,
                "source_v": source_v,
                "prepared_artifact_hash": prepared_artifact_hash,
                "candidate_artifact_hash": code_fingerprint,
                "scope_errors": post_master_scope_errors[:6],
            },
        )
    scope_changed_files = post_master_changed_files
    if post_master_scope_errors:
        declared_scope_ok = False
        declared_scope_errors.extend(post_master_scope_errors)
    runtime_contract_identity_errors = []
    runtime_contract_ledger_digest = ""
    if native_tcp_mode:
        try:
            from runtime_architecture_policy import (
                RUNTIME_ARCHITECTURE_POLICY_VERSION,
                runtime_contract_ledger_digest as _ledger_digest,
                validate_runtime_contract_ledger,
            )

            checkpoint_ledger = (_quality_ckpt_for_scope or {}).get("runtime_contract_ledger")
            plan_ledger = (
                _master_plan_for_scope.get("runtime_contract_ledger")
                if isinstance(_master_plan_for_scope, dict)
                else None
            )
            active_policy_version = str(
                ((_master_plan_for_scope or {}).get("architecture_policy") or {}).get("policy_version")
            )
            if active_policy_version == RUNTIME_ARCHITECTURE_POLICY_VERSION:
                for label, ledger in (("checkpoint", checkpoint_ledger), ("master_plan", plan_ledger)):
                    for error in validate_runtime_contract_ledger(ledger):
                        runtime_contract_identity_errors.append(f"{label}:{error}")
                checkpoint_digest = _ledger_digest(checkpoint_ledger)
                plan_digest = _ledger_digest(plan_ledger)
                if checkpoint_digest and plan_digest and checkpoint_digest != plan_digest:
                    runtime_contract_identity_errors.append(
                        f"checkpoint_master_plan_digest_mismatch:{checkpoint_digest}:{plan_digest}"
                    )
                runtime_contract_ledger_digest = checkpoint_digest or plan_digest
            else:
                runtime_contract_ledger_digest = _ledger_digest(checkpoint_ledger or plan_ledger)
        except Exception as exc:
            mark_quality_infrastructure(
                "runtime_contract_identity",
                "contract_identity",
                f"{type(exc).__name__}: {str(exc)[:300]}",
            )
    runtime_contract_identity_ok = not runtime_contract_identity_errors
    _plan_tasks = _declared_scope_tasks_from_plan(
        _master_plan_for_scope,
        _quality_ckpt_for_scope,
        include_prepare_scope=False,
    )
    if _plan_tasks and scope_changed_files and not post_master_scope_errors:
        try:
            declared_skill_layers = sorted({
                str(task.get("skill_layer", "")).strip()
                for task in _plan_tasks
                if str(task.get("skill_layer", "")).strip()
            })
            _scope_audit = audit_changed_files_against_plan(
                scope_changed_files,
                _plan_tasks,
                next_v=v,
            )
            declared_scope_ok = _scope_audit.passed
            declared_scope_errors = _scope_audit.violations
            declared_scope_metrics = _scope_audit.to_gate_metrics()
            if not declared_scope_ok:
                log_system_event(
                    "pipeline.declared_scope_failed",
                    "error",
                    f"Declared scope gate failed for v{v}: {len(declared_scope_errors)} undeclared file change(s)",
                    {
                        "version": v,
                        "source_v": source_v,
                        "changed_files": scope_changed_files[:20],
                        "allowed_files": _scope_audit.allowed_files[:30],
                        "violations": declared_scope_errors[:10],
                    },
                )
        except Exception as e:
            declared_scope_ok = False
            declared_scope_errors = [f"declared_scope_check_error: {type(e).__name__}: {str(e)[:200]}"]
            mark_quality_infrastructure(
                "declared_scope_validator",
                "declared_scope",
                declared_scope_errors[0],
            )
    elif scope_changed_files:
        declared_scope_ok = False
        declared_scope_errors = [
            "master_plan_tasks_unavailable_for_changed_artifacts"
        ]
        declared_scope_metrics = {
            "skipped": False,
            "reason": "master_plan_tasks_unavailable",
            "changed_files": scope_changed_files[:20],
        }

    def _quality_cache_current(gate):
        if _transient_task_context_errors(bot_dir):
            return False
        try:
            from candidate_hygiene import forbidden_runtime_dependency_errors

            if forbidden_runtime_dependency_errors(bot_dir):
                return False
        except Exception:
            return False
        cached_profile_id = str(gate.get("workflow_profile_id") or gate.get("profile_id") or "")
        cached_execution_mode = str(gate.get("national_execution_mode") or "")
        expected_execution_mode = "native_tcp" if native_tcp_mode else "adapter"
        if cached_profile_id != workflow_profile.profile_id or cached_execution_mode != expected_execution_mode:
            log_system_event(
                "pipeline.quality_cache_profile_stale",
                "warn",
                f"Quality gate cache stale for v{v}; cached workflow "
                f"{cached_profile_id or 'unknown'}/{cached_execution_mode or 'unknown'} "
                f"does not match active workflow {workflow_profile.profile_id}/{expected_execution_mode}.",
                {
                    "version": v,
                    "source_v": source_v,
                    "cached_workflow_profile_id": cached_profile_id,
                    "cached_execution_mode": cached_execution_mode,
                    "active_workflow_profile_id": workflow_profile.profile_id,
                    "active_execution_mode": expected_execution_mode,
                },
            )
            return False
        if native_tcp_mode and gate.get("national_native_contract_ok") is not True:
            log_system_event(
                "pipeline.quality_cache_native_contract_stale",
                "warn",
                f"Quality gate cache stale for v{v}; native TCP contract was not recorded as passed.",
                {
                    "version": v,
                    "source_v": source_v,
                    "cached_native_contract_ok": gate.get("national_native_contract_ok"),
                },
            )
            return False
        if native_tcp_mode:
            try:
                from national_capability_contract import NATIONAL_CAPABILITY_DETECTOR_VERSION
                from national_runtime_probe import (
                    RUNTIME_PROBE_LIMITS_DIGEST,
                    RUNTIME_PROBE_IDENTITY_DIGEST,
                    RUNTIME_PROBE_ORCHESTRATOR_VERSION,
                    RUNTIME_PROBE_SCENARIO_DIGEST,
                    RUNTIME_PROBE_SCHEMA_VERSION,
                )
                from runtime_architecture_policy import RUNTIME_ARCHITECTURE_POLICY_VERSION
            except Exception:
                return False
            cached_capability = gate.get("national_capability_contract") or {}
            cached_transition = gate.get("national_architecture_transition") or {}
            if (
                cached_capability.get("detector_version") != NATIONAL_CAPABILITY_DETECTOR_VERSION
                or cached_transition.get("policy_version") != RUNTIME_ARCHITECTURE_POLICY_VERSION
            ):
                log_system_event(
                    "pipeline.quality_cache_architecture_policy_stale",
                    "warn",
                    f"Quality gate cache stale for v{v}; runtime architecture detector/policy changed.",
                    {
                        "version": v,
                        "source_v": source_v,
                        "cached_detector_version": cached_capability.get("detector_version"),
                        "current_detector_version": NATIONAL_CAPABILITY_DETECTOR_VERSION,
                        "cached_policy_version": cached_transition.get("policy_version"),
                        "current_policy_version": RUNTIME_ARCHITECTURE_POLICY_VERSION,
                    },
                )
                return False
            expected_probe_identity = {
                "runtime_probe_schema_version": RUNTIME_PROBE_SCHEMA_VERSION,
                "runtime_probe_orchestrator_version": RUNTIME_PROBE_ORCHESTRATOR_VERSION,
                "runtime_probe_scenario_digest": RUNTIME_PROBE_SCENARIO_DIGEST,
                "runtime_probe_limits_digest": RUNTIME_PROBE_LIMITS_DIGEST,
                "runtime_probe_identity_digest": RUNTIME_PROBE_IDENTITY_DIGEST,
                "runtime_contract_ledger_digest": runtime_contract_ledger_digest,
            }
            if any(gate.get(key) != value for key, value in expected_probe_identity.items()):
                log_system_event(
                    "pipeline.quality_cache_runtime_probe_stale",
                    "warn",
                    f"Quality gate cache stale for v{v}; runtime probe or contract identity changed.",
                    {
                        "version": v,
                        "source_v": source_v,
                        "expected": expected_probe_identity,
                        "cached": {key: gate.get(key) for key in expected_probe_identity},
                    },
                )
                return False
        if gate.get("embedded_selftests_ok") is not True:
            log_system_event(
                "pipeline.quality_cache_embedded_selftests_stale",
                "warn",
                f"Quality gate cache stale for v{v}; embedded self-tests were not recorded as passed.",
                {
                    "version": v,
                    "source_v": source_v,
                    "cached_embedded_selftests_ok": gate.get("embedded_selftests_ok"),
                },
            )
            return False
        cached_fingerprint = gate.get("code_fingerprint")
        if cached_fingerprint and cached_fingerprint == code_fingerprint:
            return True
        log_system_event(
            "pipeline.quality_cache_stale", "warn",
            f"Quality gate cache stale for v{v}; bot code changed since cached gate, rerunning quality gates.",
            {
                "version": v,
                "source_v": source_v,
                "cached_fingerprint": cached_fingerprint,
                "current_fingerprint": code_fingerprint,
            },
        )
        return False

    # Idempotency guard: skip if quality gates already passed for this version
    _cached = _idempotency_check(
        v, source_v,
        stage_set=("quality_passed", "reviewed", "critic_checked", "verified", "archived"),
        gate_name="quality",
        approval_key="all_passed",
        directive="Quality gates ALREADY PASSED. Call run_review next.",
        cache_validator=_quality_cache_current,
    )
    if _cached:
        return _cached

    try:
        compile_errors = verify_code(bot_dir)
    except Exception as exc:
        compile_errors = [f"compile_runner_exception: {type(exc).__name__}: {str(exc)[:200]}"]
        mark_quality_infrastructure("compile_runner", "compile", compile_errors[0])
    compile_errors.extend(_transient_task_context_errors(bot_dir))
    try:
        import_errors = run_import_contract_test(bot_dir)
    except Exception as exc:
        import_errors = [{
            "module": "quality_harness",
            "exception": type(exc).__name__,
            "message": str(exc)[:200],
        }]
        mark_quality_infrastructure(
            "import_contract_runner",
            "runtime_import",
            f"{type(exc).__name__}: {str(exc)[:300]}",
        )
    try:
        from protected_contracts import check_bot_protocol_contract
        protected_contract_errors = check_bot_protocol_contract(bot_dir)
    except Exception as e:
        protected_contract_errors = [f"protected_contract_check_error: {type(e).__name__}: {str(e)[:200]}"]
        mark_quality_infrastructure(
            "protected_contract_validator",
            "protected_contract",
            protected_contract_errors[0],
        )
    try:
        from bot_artifact import publication_shape_errors

        protected_contract_errors.extend(publication_shape_errors(bot_dir))
    except Exception as exc:
        protected_contract_errors.append(
            "publication_shape_check_error:"
            f"{type(exc).__name__}:{str(exc)[:180]}"
        )
    try:
        from candidate_hygiene import forbidden_runtime_dependency_errors

        protected_contract_errors.extend(
            forbidden_runtime_dependency_errors(bot_dir)
        )
    except Exception as exc:
        protected_contract_errors.append(
            "transient_runtime_dependency_check_error:"
            f"{type(exc).__name__}:{str(exc)[:180]}"
        )
    native_contract_errors = []
    if native_tcp_mode:
        try:
            from national_native import check_native_contract
            native_contract_errors = check_native_contract(
                bot_dir,
                require_current_stream_decoder=True,
                require_current_decision_runtime=True,
            )
        except Exception as e:
            native_contract_errors = [f"native_contract_check_error: {type(e).__name__}: {str(e)[:200]}"]
            mark_quality_infrastructure(
                "native_contract_validator",
                "native_contract",
                native_contract_errors[0],
            )
    national_capability_contract = {
        "schema_version": 2,
        "ok": True,
        "required_failures": [],
        "advisory_warnings": [],
        "checks": [],
        "skipped": not native_tcp_mode,
    }
    national_architecture_transition = {
        "schema_version": 1,
        "ok": True,
        "skipped": not native_tcp_mode,
        "regressions": [],
        "runtime_floor_failures": [],
        "unresolved_focus_checks": [],
        "policy_identity_errors": [],
    }
    national_capability_ok = True
    national_capability_required = bool(native_tcp_mode)
    if native_tcp_mode:
        try:
            from runtime_architecture_policy import (
                evaluate_architecture_transition,
                validate_runtime_contract_implementation,
            )

            if source_dir is None:
                raise RuntimeError("native candidate has no source directory for architecture transition")
            expected_architecture_policy = (
                _master_plan_for_scope.get("architecture_policy")
                if isinstance(_master_plan_for_scope, dict)
                else None
            )
            national_architecture_transition = evaluate_architecture_transition(
                source_dir,
                bot_dir,
                expected_policy=expected_architecture_policy,
            )
            national_capability_contract = national_architecture_transition["candidate_capabilities"]
            transition_infrastructure = (
                national_architecture_transition.get("outcome") == "infrastructure_failure"
            )
            runtime_contract_implementation_errors = (
                []
                if transition_infrastructure
                else validate_runtime_contract_implementation(
                    _master_plan_for_scope if isinstance(_master_plan_for_scope, dict) else {},
                    national_capability_contract,
                )
            )
            national_architecture_transition["runtime_contract_implementation_errors"] = (
                runtime_contract_implementation_errors
            )
            if runtime_contract_implementation_errors:
                national_architecture_transition["ok"] = False
            national_capability_ok = bool(national_architecture_transition.get("ok"))
        except Exception as e:
            national_capability_ok = False
            national_capability_contract = {
                "schema_version": 2,
                "ok": False,
                "error": f"national_capability_contract_error: {type(e).__name__}: {str(e)[:200]}",
                "required_failures": [],
                "advisory_warnings": [],
                "checks": [],
            }
            national_architecture_transition = {
                "schema_version": 1,
                "ok": False,
                "conclusive": False,
                "outcome": "infrastructure_failure",
                "failure_class": "infrastructure",
                "error": f"national_architecture_transition_error: {type(e).__name__}: {str(e)[:200]}",
                "infrastructure_failures": [{
                    "side": "quality_pipeline",
                    "component": "runtime_architecture_policy",
                    "failure_class": "internal_infrastructure",
                    "issues": [
                        f"national_architecture_transition_error: {type(e).__name__}: {str(e)[:200]}"
                    ],
                }],
                "runtime_probe_infra": [{
                    "side": "quality_pipeline",
                    "component": "runtime_architecture_policy",
                    "failure_class": "internal_infrastructure",
                    "issues": [
                        f"national_architecture_transition_error: {type(e).__name__}: {str(e)[:200]}"
                    ],
                }],
                "regressions": [],
                "runtime_floor_failures": [],
                "unresolved_focus_checks": [],
                "policy_identity_errors": [],
            }
    national_capability_blockers = list(
        national_capability_contract.get("required_failures") or []
    )
    for error in national_architecture_transition.get("policy_identity_errors") or []:
        national_capability_blockers.append({
            "check_id": "architecture_policy_identity",
            "name": "architecture_policy_identity",
            "guidance": str(error),
        })
    quality_infra_issues.extend([
        GateExecution.infrastructure(
            str(probe_infra.get("component") or "national_runtime_probe"),
            "runtime_architecture",
            [str(item) for item in (probe_infra.get("issues") or [])[:8]],
            side=(
                str(probe_infra.get("side"))
                if str(probe_infra.get("side")) in {"candidate", "opponent", "server", "harness", "system"}
                else "system"
            ),
        )
        for probe_infra in national_architecture_transition.get("infrastructure_failures")
        or national_architecture_transition.get("runtime_probe_infra")
        or []
        if isinstance(probe_infra, dict)
    ])
    quality_infrastructure = {
        "active": False,
        "attempt": 0,
        "max_attempts": QUALITY_INFRA_MAX_ATTEMPTS,
        "retryable": False,
        "exhausted": False,
        "action": "",
        "issues": [],
    }
    for regression in national_architecture_transition.get("regressions") or []:
        national_capability_blockers.append({
            "check_id": f"architecture_regression:{regression.get('check_id')}",
            "name": f"architecture_regression:{regression.get('check_id')}",
            "guidance": regression.get("guidance") or "Restore the capability already present in the source bot.",
        })
    for failure in national_architecture_transition.get("runtime_floor_failures") or []:
        check_id = str(failure.get("check_id") or "unknown")
        check = (national_capability_contract.get("checks_by_id") or {}).get(check_id, {})
        national_capability_blockers.append({
            "check_id": f"runtime_floor:{check_id}",
            "name": f"runtime_floor:{check_id}",
            "guidance": (
                failure.get("guidance")
                or check.get("guidance")
                or f"Satisfy mandatory national runtime floor check {check_id}."
            ),
        })
    for check_id in national_architecture_transition.get("unresolved_focus_checks") or []:
        check = (national_capability_contract.get("checks_by_id") or {}).get(check_id, {})
        national_capability_blockers.append({
            "check_id": f"architecture_focus:{check_id}",
            "name": f"architecture_focus:{check_id}",
            "guidance": check.get("guidance") or f"Close selected architecture focus check {check_id}.",
        })
    for error in national_architecture_transition.get("runtime_contract_implementation_errors") or []:
        national_capability_blockers.append({
            "check_id": "runtime_contract_implementation",
            "name": "runtime_contract_implementation",
            "guidance": str(error),
        })
    if national_architecture_transition.get("error"):
        national_capability_blockers.append({
            "check_id": "architecture_transition_error",
            "name": "architecture_transition_error",
            "guidance": national_architecture_transition["error"],
        })
    embedded_selftest_errors = []
    try:
        from code_verification import run_bot_embedded_self_tests_execution
        embedded_selftest_execution = run_bot_embedded_self_tests_execution(bot_dir)
        embedded_selftest_errors = embedded_selftest_execution.issues
        if embedded_selftest_execution.is_infrastructure:
            quality_infra_issues.append(embedded_selftest_execution)
    except Exception as e:
        embedded_selftest_errors = [f"embedded_selftest_check_error: {type(e).__name__}: {str(e)[:200]}"]
        mark_quality_infrastructure(
            "embedded_selftest_runner",
            "embedded_selftest",
            embedded_selftest_errors[0],
        )
    if embedded_selftest_errors:
        log_system_event(
            "pipeline.embedded_selftests_failed",
            "error",
            f"Embedded bot self-tests failed for v{v}: {len(embedded_selftest_errors)} issue(s)",
            {"version": v, "source_v": source_v, "errors": embedded_selftest_errors[:5]},
        )
    else:
        log_system_event(
            "pipeline.embedded_selftests_passed",
            "info",
            f"Embedded bot self-tests passed for v{v}",
            {"version": v, "source_v": source_v},
        )
    if import_errors:
        log_system_event(
            "pipeline.import_contract_failed", "error",
            f"Runtime import contract failed for v{v}: "
            f"{import_errors[0].get('module')} {import_errors[0].get('exception')}: "
            f"{import_errors[0].get('message')}",
            {"version": v, "source_v": source_v, "errors": import_errors[:3]},
        )
    else:
        log_system_event(
            "pipeline.import_contract_passed", "info",
            f"Runtime import contract passed for v{v}",
            {"version": v, "source_v": source_v},
        )
    # A3 (evolution-plan-refresh-jun21): placement-shadow advisory (NON-blocking).
    # Flags detector call-sites placed after a to_call>=my_chips early-return — the
    # INERTNESS root cause that recurred v138-v143 (guards wired at strategy.py:1041
    # after the allin-cover early-return at :1018 = unreachable for stack-off spots).
    placement_shadow_warnings = []
    try:
        from code_verification import detect_placement_shadow_warnings
        placement_shadow_warnings = detect_placement_shadow_warnings(bot_dir)
        if placement_shadow_warnings:
            log_system_event(
                "pipeline.placement_shadow", "warn",
                f"Placement-shadow detectors in v{v}: {len(placement_shadow_warnings)} "
                f"call-site(s) after to_call>=my_chips early-return (INERTNESS risk — "
                f"relocate call-site, do not re-tune)",
                {"version": v, "warnings": placement_shadow_warnings[:6]},
            )
    except Exception as e:
        _log.warning("placement_shadow check error: %s", e)
        mark_quality_infrastructure(
            "placement_shadow_validator",
            "placement_shadow",
            f"{type(e).__name__}: {str(e)[:300]}",
        )
    # M6 (b057ead follow-up): telemetry-fidelity AST gate — BLOCKING.
    # Flags multi-arm margin/delta detectors whose stderr.write telemetry is nested
    # inside a bucket/signal If-gate (sub-arm-scoped) instead of hoisted to function
    # scope. Sub-arm-only telemetry yields a false-INERT verdict on daemon grep (v154
    # 99.98%-delta=+0 artifact → v155 Master misread the LIVE framework as dead and
    # listed it in do_not_touch). Unlike placement_shadow (advisory — its warnings
    # never reach reviewer_prompt, so it has zero enforcement), M6 is BLOCKING:
    # master_prompt.md M6 explicitly acknowledges "Reviewer MUST reject" clauses are
    # NON-enforceable (Reviewer only receives {master_plan} JSON), so only a hard
    # precommit gate can stop the 9-gen INERTNESS loop (M5 advisory precedent failed).
    telemetry_fidelity_warnings = []
    try:
        from code_verification import detect_telemetry_fidelity_warnings
        telemetry_fidelity_warnings = detect_telemetry_fidelity_warnings(bot_dir)
        if telemetry_fidelity_warnings:
            log_system_event(
                "pipeline.telemetry_fidelity", "error",
                f"Telemetry-fidelity violations in v{v}: {len(telemetry_fidelity_warnings)} "
                f"multi-arm detector(s) with sub-arm-scoped stderr.write (false-INERT risk)",
                {"version": v, "warnings": telemetry_fidelity_warnings[:6]},
            )
    except Exception as e:
        _log.warning("telemetry_fidelity check error: %s", e)
        mark_quality_infrastructure(
            "telemetry_fidelity_validator",
            "telemetry_fidelity",
            f"{type(e).__name__}: {str(e)[:300]}",
        )
    telemetry_fidelity_ok = len(telemetry_fidelity_warnings) == 0

    # R1 (2026-07-01): newly-added top-level helpers must be wired into the bot.
    # This blocks the observed v239 failure mode where a Worker appended a good
    # postflop helper but never imported/called it; compile, smoke, and decision
    # scenarios all passed because the change was inert.
    reachability_warnings = []
    if source_dir is not None and changed_files_list:
        try:
            from code_verification import detect_new_function_reachability_warnings
            reachability_warnings = detect_new_function_reachability_warnings(
                source_dir, bot_dir, changed_files_list
            )
            if reachability_warnings:
                log_system_event(
                    "pipeline.reachability_gate", "error",
                    f"Reachability violations in v{v}: {len(reachability_warnings)} "
                    "new function(s) have no non-import references (dead-code risk)",
                    {"version": v, "warnings": reachability_warnings[:6]},
                )
        except Exception as e:
            _log.warning("reachability check error: %s", e)
            mark_quality_infrastructure(
                "reachability_validator",
                "reachability",
                f"{type(e).__name__}: {str(e)[:300]}",
            )
    reachability_ok = len(reachability_warnings) == 0

    try:
        position_semantics_errors = detect_position_semantics_errors(bot_dir)
    except Exception as exc:
        position_semantics_errors = [
            f"position_semantics_check_error: {type(exc).__name__}: {str(exc)[:200]}"
        ]
        mark_quality_infrastructure(
            "position_semantics_validator",
            "position_semantics",
            position_semantics_errors[0],
        )
    position_semantics_ok = len(position_semantics_errors) == 0
    if position_semantics_errors:
        log_system_event(
            "pipeline.position_semantics_failed",
            "error",
            f"Position semantics violations in v{v}: {len(position_semantics_errors)} issue(s)",
            {"version": v, "errors": position_semantics_errors[:10]},
        )

    smoke_errors, smoke_payload = await _run_workflow_smoke_gate(
        bot_dir=bot_dir,
        source_v=source_v,
        native_tcp_mode=native_tcp_mode,
        compile_errors=compile_errors,
        import_errors=import_errors,
        protected_contract_errors=protected_contract_errors,
        native_contract_errors=native_contract_errors,
        embedded_selftest_errors=embedded_selftest_errors,
    )
    if (
        smoke_payload.get("failure_class") == "infrastructure"
        or smoke_payload.get("outcome") == "infrastructure_failure"
    ):
        mark_quality_infrastructure(
            "native_smoke_harness" if native_tcp_mode else "local_smoke_harness",
            "workflow_smoke",
            "; ".join(str(item) for item in smoke_errors[:3]),
        )
    try:
        national_protocol_errors = run_national_protocol_tests(
            native_tcp_mode=native_tcp_mode
        )
    except Exception as exc:
        national_protocol_errors = [
            f"national_protocol_runner_exception: {type(exc).__name__}: {str(exc)[:200]}"
        ]
    if national_protocol_errors:
        mark_quality_infrastructure(
            "national_protocol_test_shard",
            "national_protocol",
            "; ".join(str(item) for item in national_protocol_errors[:3]),
        )
    national_acceptance_ok = True
    national_acceptance_errors = []
    national_acceptance_payload = {}
    national_acceptance_enabled = os.environ.get("POK_NATIONAL_ACCEPTANCE_GATE", "1") != "0"
    national_acceptance_hands = int(
        os.environ.get(
            "POK_NATIONAL_ACCEPTANCE_HANDS",
            str(workflow_profile.national_acceptance_hands),
        )
    )
    national_acceptance_timeout_sec = float(
        os.environ.get(
            "POK_NATIONAL_ACCEPTANCE_TIMEOUT_SEC",
            str(workflow_profile.national_acceptance_timeout_sec),
        )
    )
    if national_acceptance_enabled and source_v is not None:
        try:
            _bot_under_project_bots = bot_dir.resolve().is_relative_to((PROJECT_ROOT / "bots").resolve())
        except AttributeError:
            _bot_under_project_bots = str(bot_dir.resolve()).startswith(str((PROJECT_ROOT / "bots").resolve()))
        can_run_national_acceptance = (
            len(compile_errors) == 0
            and len(import_errors) == 0
            and len(protected_contract_errors) == 0
            and len(native_contract_errors) == 0
            and len(embedded_selftest_errors) == 0
            and len(smoke_errors) == 0
            and (
                (bot_dir / "national_bot.py").exists()
                if native_tcp_mode
                else (bot_dir / "main.py").exists()
            )
            and _bot_under_project_bots
        )
        if can_run_national_acceptance:
            try:
                if native_tcp_mode:
                    from national_native import run_native_acceptance_for_candidate
                    _acceptance = await run_native_acceptance_for_candidate(
                        bot_dir,
                        source_v=source_v,
                        hands=national_acceptance_hands,
                        max_opponents=2,
                        timeout_sec=national_acceptance_timeout_sec,
                    )
                else:
                    from national_acceptance import run_acceptance_for_candidate
                    _acceptance = await run_acceptance_for_candidate(
                        bot_dir,
                        source_v=source_v,
                        hands=national_acceptance_hands,
                        max_opponents=2,
                        strict=bool(workflow_profile.national_acceptance_hard),
                        timeout_sec=national_acceptance_timeout_sec,
                    )
                national_acceptance_ok = bool(_acceptance.passed)
                national_acceptance_errors = _acceptance.issues[:5]
                national_acceptance_payload = _acceptance.model_dump()
                if getattr(_acceptance, "outcome", "") == "infrastructure_failure":
                    mark_quality_infrastructure(
                        "national_acceptance_harness",
                        "national_acceptance",
                        "; ".join(str(item) for item in _acceptance.issues[:5]),
                    )
                log_system_event(
                    "pipeline.national_acceptance_passed" if national_acceptance_ok else "pipeline.national_acceptance_failed",
                    "success" if national_acceptance_ok else "error",
                    f"National {'native TCP ' if native_tcp_mode else ''}acceptance "
                    f"{'passed' if national_acceptance_ok else 'failed'} for v{v} "
                    f"({national_acceptance_hands} hands/pair)",
                    {
                        "version": v,
                        "source_v": source_v,
                        "execution_mode": "native_tcp" if native_tcp_mode else "adapter",
                        "hands": national_acceptance_hands,
                        "timeout_sec": national_acceptance_timeout_sec,
                        "opponents": _acceptance.opponents,
                        "issues": national_acceptance_errors,
                        "summary": _acceptance.summary,
                    },
                )
            except Exception as e:
                national_acceptance_ok = False
                national_acceptance_errors = [f"national_acceptance_exception: {type(e).__name__}: {str(e)[:200]}"]
                national_acceptance_payload = {"error": national_acceptance_errors[0]}
                mark_quality_infrastructure(
                    "national_acceptance_harness",
                    "national_acceptance",
                    national_acceptance_errors[0],
                )
                _log.warning("national acceptance gate failed to run for v%s: %s", v, e)
        else:
            national_acceptance_ok = True
            national_acceptance_errors = ["national_acceptance_skipped_due_to_failed_prerequisites"]
            national_acceptance_payload = {"skipped": True, "reason": national_acceptance_errors[0]}

    official_smoke_ok = True
    official_smoke_errors: list[str] = []
    official_smoke_payload: dict = {}
    official_smoke_blocking = False
    official_smoke_inconclusive = False
    official_smoke_classification = "not_run"
    official_smoke_mode = os.environ.get("POK_OFFICIAL_SMOKE_GATE", "queue").strip().lower()
    official_smoke_enabled = native_tcp_mode and official_smoke_mode not in {"0", "false", "off", "disabled", "none"}
    if official_smoke_enabled:
        try:
            _official_bot_under_project_bots = bot_dir.resolve().is_relative_to((PROJECT_ROOT / "bots").resolve())
        except AttributeError:
            _official_bot_under_project_bots = str(bot_dir.resolve()).startswith(str((PROJECT_ROOT / "bots").resolve()))
        can_request_official_smoke = (
            len(compile_errors) == 0
            and len(import_errors) == 0
            and len(protected_contract_errors) == 0
            and len(native_contract_errors) == 0
            and len(embedded_selftest_errors) == 0
            and len(smoke_errors) == 0
            and len(national_protocol_errors) == 0
            and national_acceptance_ok
            and (bot_dir / "national_bot.py").exists()
            and _official_bot_under_project_bots
        )
        if can_request_official_smoke:
            try:
                official_smoke_payload = await _request_official_smoke_status(
                    bot_dir,
                    official_smoke_mode,
                )
                official_smoke_blocking = bool(official_smoke_payload.get("blocking"))
                official_smoke_inconclusive = bool(official_smoke_payload.get("inconclusive"))
                official_smoke_ok = not official_smoke_blocking
                official_smoke_classification = str(
                    official_smoke_payload.get("classification") or "passed_or_pending"
                )
                official_smoke_errors = list(official_smoke_payload.get("issues") or [])
                opponent_selection = official_smoke_payload.get("opponent_selection") or {}
                official_opponent = (opponent_selection.get("opponent") or {}).get("path")
                if not opponent_selection.get("selected"):
                    log_system_event(
                        "pipeline.official_smoke_opponent_unavailable",
                        "warn",
                        f"Official smoke has no eligible opponent for v{v}",
                        {
                            "version": v,
                            "source_v": source_v,
                            "preferred_opponent": os.environ.get("POK_OFFICIAL_OPPONENT", "").strip() or None,
                            "opponent_selection": opponent_selection,
                        },
                    )
                log_system_event(
                    (
                        "pipeline.official_smoke_failed"
                        if official_smoke_blocking
                        else "pipeline.official_smoke_inconclusive"
                        if official_smoke_inconclusive
                        else "pipeline.official_smoke_status"
                    ),
                    "error" if official_smoke_blocking else "warn" if official_smoke_inconclusive else "info",
                    f"Official smoke status for v{v}: {official_smoke_payload.get('status', 'unknown')}",
                    {
                        "version": v,
                        "source_v": source_v,
                        "mode": official_smoke_mode,
                        "opponent": official_opponent,
                        "opponent_selection": opponent_selection,
                        "status": official_smoke_payload.get("status"),
                        "issues": official_smoke_errors,
                        "blocking": official_smoke_blocking,
                        "inconclusive": official_smoke_inconclusive,
                        "classification": official_smoke_classification,
                    },
                )
            except Exception as e:
                official_smoke_ok = False
                official_smoke_blocking = False
                official_smoke_inconclusive = True
                official_smoke_classification = "inconclusive"
                official_smoke_errors = [f"official_smoke_exception: {type(e).__name__}: {str(e)[:200]}"]
                official_smoke_payload = {
                    "error": official_smoke_errors[0],
                    "blocking": False,
                    "inconclusive": True,
                    "classification": "inconclusive",
                }
                mark_quality_infrastructure(
                    "official_smoke_harness",
                    "official_smoke",
                    official_smoke_errors[0],
                )
        else:
            official_smoke_payload = {
                "skipped": True,
                "reason": "official_smoke_skipped_due_to_failed_prerequisites",
            }
            official_smoke_classification = "skipped"

    # --- B3: Heuristic Dynamic Regression Tests from Diff ---
    # Deterministic coverage runs first. The LLM generator is now an augmenting
    # source, not the gatekeeper for dynamic coverage, so a timeout cannot leave
    # changed branches completely untested.
    dynamic_test_meta = {
        "heuristic_count": 0,
        "llm_count": 0,
        "llm_status": "not_run",
        "llm_timeout_sec": DYNAMIC_TEST_LLM_TIMEOUT,
        "llm_enabled": DYNAMIC_TEST_LLM_ENABLED,
    }
    heuristic_scenarios = []
    existing_ids = []
    if source_v is not None and changed_files_list:
        try:
            import difflib as _difflib
            from decision_tester import (
                SCENARIOS_FILE,
                generate_scenarios_from_diff,
                save_dynamic_scenarios,
                load_dynamic_scenarios,
            )
            if SCENARIOS_FILE.exists():
                with open(SCENARIOS_FILE) as _f:
                    for s in json.load(_f):
                        existing_ids.append(s.get("id", ""))
            _src_dir = get_bot_dir(source_v)
            _dst_dir = get_bot_dir(v)

            _diff_parts = []
            for _rel in changed_files_list:
                _src_file = _src_dir / _rel
                _dst_file = _dst_dir / _rel
                _before = _src_file.read_text() if _src_file.exists() else ""
                _after = _dst_file.read_text() if _dst_file.exists() else ""
                if _before != _after:
                    _diff = _difflib.unified_diff(
                        _before.splitlines(keepends=True),
                        _after.splitlines(keepends=True),
                        fromfile=f"v{source_v}/{_rel}", tofile=f"v{v}/{_rel}",
                        n=2,
                    )
                    _diff_text = "".join(_diff)
                    if _diff_text:
                        _diff_parts.append(_diff_text)

            if _diff_parts:
                _full_diff = "\n".join(_diff_parts)[-8000:]
                heuristic_scenarios = generate_scenarios_from_diff(
                    _full_diff, str(_src_dir), str(_dst_dir)
                )
                dynamic_test_meta["heuristic_count"] = len(heuristic_scenarios)
                if heuristic_scenarios:
                    _existing = load_dynamic_scenarios()
                    _existing_ids = {s.get("id") for s in _existing}
                    _new_to_save = [s for s in heuristic_scenarios
                                    if s.get("id") not in _existing_ids]
                    save_dynamic_scenarios(_existing + _new_to_save)
                    _log.info(
                        "B3: Generated %d heuristic scenarios from diff for v%d",
                        len(heuristic_scenarios), v
                    )
        except Exception as e:
            dynamic_test_meta["heuristic_error"] = str(e)[:300]
            _log.warning("B3 heuristic scenario generation error: %s", e)

    # --- P0-3: LLM-Generated Dynamic Decision Tests (augment only) ---
    dynamic_scenarios = []
    if source_v is not None and changed_files_list:
        try:
            from audit_agents import _generate_dynamic_tests
            if not DYNAMIC_TEST_LLM_ENABLED:
                dynamic_test_meta["llm_status"] = "skipped_disabled"
                log_system_event(
                    "pipeline.dynamic_test_gen_skipped",
                    "info",
                    f"v{v}: skipped LLM dynamic test generation; "
                    "POK_DYNAMIC_TEST_LLM_ENABLED is off",
                    {"version": v, "source_v": source_v,
                     "heuristic_count": len(heuristic_scenarios)},
                )
            elif len(heuristic_scenarios) >= DYNAMIC_TEST_HEURISTIC_SUFFICIENT:
                dynamic_test_meta["llm_status"] = "skipped_heuristic_sufficient"
                log_system_event(
                    "pipeline.dynamic_test_gen_skipped",
                    "info",
                    f"v{v}: skipped LLM dynamic test generation; "
                    f"{len(heuristic_scenarios)} deterministic scenarios already generated",
                    {"version": v, "source_v": source_v,
                     "heuristic_count": len(heuristic_scenarios)},
                )
            else:
                ckpt_dt = _matching_checkpoint(v, source_v)
                master_plan_dt = ckpt_dt.get("master_plan", {}) if ckpt_dt else {}
                ui = _get_ui()
                with llm_cancel_scope(
                    "dynamic_test_gen",
                    reason="parent_timeout",
                    timeout_sec=DYNAMIC_TEST_LLM_TIMEOUT,
                ):
                    dynamic_scenarios = await asyncio.wait_for(
                        _generate_dynamic_tests(
                            v, source_v, changed_files_list, master_plan_dt, existing_ids, ui
                        ),
                        timeout=DYNAMIC_TEST_LLM_TIMEOUT,
                    )
                dynamic_test_meta["llm_status"] = "ok"
                dynamic_test_meta["llm_count"] = len(dynamic_scenarios or [])
        except asyncio.TimeoutError:
            dynamic_test_meta["llm_status"] = "timeout"
            log_system_event(
                "pipeline.dynamic_test_gen_timeout",
                "warn",
                f"v{v}: LLM dynamic test generation timed out after "
                f"{DYNAMIC_TEST_LLM_TIMEOUT}s; using deterministic scenarios",
                {"version": v, "source_v": source_v,
                 "timeout_s": DYNAMIC_TEST_LLM_TIMEOUT,
                 "heuristic_count": len(heuristic_scenarios)},
            )
        except Exception as e:
            dynamic_test_meta["llm_status"] = "error"
            dynamic_test_meta["llm_error"] = str(e)[:300]
            _log.warning("Dynamic test generation error: %s", e)

    # Combine both dynamic sources
    _all_dynamic = (dynamic_scenarios or []) + heuristic_scenarios
    dynamic_test_meta["combined_count"] = len(_all_dynamic)

    # Decision-test gate: classic single-shot path by default; optional Phase-2
    # per-scenario SPRT aggregation when DECISION_TEST_SPRT_ENABLED. The SPRT
    # path returns the SAME dict shape (pass_rate/total/critical_*/failures), so
    # the downstream gate logic below is unchanged — only the per-scenario
    # verdict source differs.
    try:
        if DECISION_TEST_SPRT_ENABLED:
            from decision_tester import run_decision_tests_sprt_aggregate
            decision_detail = run_decision_tests_sprt_aggregate(
                bot_dir, extra_scenarios=_all_dynamic or None
            )
        else:
            decision_detail = run_decision_test_details(
                bot_dir, extra_scenarios=_all_dynamic or None
            )
    except Exception as exc:
        decision_detail = {
            "pass_rate": 0.0,
            "total": 0,
            "critical_failures": [],
            "failures": [],
            "infrastructure_error": f"{type(exc).__name__}: {str(exc)[:300]}",
        }
        mark_quality_infrastructure(
            "decision_test_runner",
            "decision_tests",
            decision_detail["infrastructure_error"],
        )
    decision_rate = decision_detail.get("pass_rate", 0.0)
    decision_total = decision_detail.get("total", 0)
    decision_skill_layers = decision_detail.get("skill_layers", {})
    critical_failures = decision_detail.get("critical_failures", [])
    critical_ok = len(critical_failures) == 0
    try:
        total_lines, oversized = check_code_size(bot_dir, source_dir=source_dir)
    except Exception as exc:
        total_lines, oversized = 0, []
        mark_quality_infrastructure(
            "code_size_validator",
            "code_size",
            f"{type(exc).__name__}: {str(exc)[:300]}",
        )
    decision_ok = decision_rate >= 0.7 and critical_ok and decision_total > 0

    # --- P1-3: Structural fix-verification gate (authoritative fix-present judgment) ---
    # fix_injection.py uses substring matching which silently misses when a worker
    # refactors the target function. verify_fixes() runs STRUCTURAL/RUNTIME checks in
    # subprocess isolation so a confirmed invariant violation blocks the pipeline
    # regardless of how the code was written. A verifier exception is trusted
    # infrastructure failure and must never silently pass or become bot debt.
    try:
        fix_results = verify_fixes(bot_dir)
    except Exception as exc:
        fix_results = {}
        mark_quality_infrastructure(
            "fix_verification_runner",
            "fix_verification",
            f"{type(exc).__name__}: {str(exc)[:300]}",
        )
    for fix_id, verification in fix_results.items():
        if verification.get("outcome") == "infrastructure_failure":
            mark_quality_infrastructure(
                "fix_verification_runner",
                "fix_verification",
                f"{fix_id}: {verification.get('reason', 'verifier unavailable')}",
            )
    fix_ok = all(r.get("ok", False) for r in fix_results.values())
    fix_failed = {
        fid: r for fid, r in fix_results.items()
        if not r.get("ok", False) and r.get("outcome") != "infrastructure_failure"
    }

    # fix-3: TRUE-SHADOW placement is a blocking gate (INERTNESS root cause).
    # v156-v165 produced 10 generations of TRUE-SHADOW `_river_stackoff_guard`
    # that passed quality gates because placement_shadow was advisory-only.
    true_shadows = [w for w in placement_shadow_warnings if 'TRUE SHADOW' in w]
    if true_shadows:
        for w in true_shadows:
            compile_errors.append(f"BLOCKING: {w}")

    candidate_gate_checks_passed = (
        len(compile_errors) == 0
        and len(import_errors) == 0
        and len(protected_contract_errors) == 0
        and len(native_contract_errors) == 0
        and len(embedded_selftest_errors) == 0
        and len(smoke_errors) == 0
        and len(national_protocol_errors) == 0
        and national_acceptance_ok
        and official_smoke_ok
        and decision_ok
        and len(oversized) == 0
        and code_changed
        and post_master_delta_ok
        and declared_scope_ok
        and fix_ok
        and telemetry_fidelity_ok
        and reachability_ok
        and position_semantics_ok
        and national_capability_ok
        and runtime_contract_identity_ok
    )
    official_local_status = None
    if candidate_gate_checks_passed and native_tcp_mode and not quality_infra_issues:
        try:
            from official_certification import record_local_pass

            official_local_status = record_local_pass(bot_dir)
        except Exception as exc:
            official_local_status = {
                "error": f"{type(exc).__name__}: {str(exc)[:200]}"
            }
            mark_quality_infrastructure(
                "official_status_store",
                "official_status_persistence",
                official_local_status["error"],
            )

    if quality_infra_issues:
        try:
            from national_runtime_probe import RUNTIME_PROBE_IDENTITY_DIGEST
        except Exception:
            RUNTIME_PROBE_IDENTITY_DIGEST = "unavailable"
        components = sorted({str(item.get("component")) for item in quality_infra_issues})
        phases = sorted({str(item.get("phase") or "unknown") for item in quality_infra_issues})
        infra_component = components[0] if len(components) == 1 else "quality_harness_bundle"
        source_fingerprint = _bot_code_fingerprint(source_dir) if source_dir else ""
        harness_identity = hashlib.sha256(json.dumps({
            "quality_infra_contract_version": QUALITY_INFRA_CONTRACT_VERSION,
            "components": components,
            "runtime_probe_identity": RUNTIME_PROBE_IDENTITY_DIGEST,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        attempt_key = infrastructure_attempt_key(
            component=infra_component,
            candidate_fingerprint=code_fingerprint,
            source_fingerprint=source_fingerprint,
            harness_identity=harness_identity,
            contract_identity=runtime_contract_ledger_digest,
            extra={
                "phases": phases,
                "workflow_profile_id": workflow_profile.profile_id,
                "national_execution_mode": "native_tcp" if native_tcp_mode else "adapter",
            },
        )
        issue_strings = [
            f"{item.get('phase', 'unknown')}:{item.get('component')}: "
            + ", ".join(item.get("issues") or [])
            for item in quality_infra_issues
        ]
        overlay = build_infrastructure_failure(
            existing_infra,
            component=infra_component,
            code="quality_harness_infrastructure_failure",
            owner_tool="run_quality_gates",
            resume_stage="workers_done",
            attempt_key=attempt_key,
            issues=issue_strings,
            max_attempts=QUALITY_INFRA_MAX_ATTEMPTS,
            metadata={
                "candidate_fingerprint": code_fingerprint,
                "source_fingerprint": source_fingerprint,
                "harness_identity": harness_identity,
                "components": components,
                "phases": phases,
                "runtime_contract_ledger_digest": runtime_contract_ledger_digest,
            },
        )
        quality_infrastructure = {"active": True, **overlay}

    all_passed = candidate_gate_checks_passed and not quality_infrastructure["active"]

    result = {
        "version": v,
        "code_changed": code_changed,
        "post_master_delta_ok": post_master_delta_ok,
        "post_master_delta_required": post_master_delta_required,
        "prepared_artifact_hash": prepared_artifact_hash,
        "post_master_changed_files": post_master_changed_files,
        "post_master_scope_errors": post_master_scope_errors,
        "changed_files": post_master_changed_files,
        "source_python_changed": source_python_changed,
        "source_python_changed_files": changed_files_list,
        "compile_ok": len(compile_errors) == 0,
        "compile_errors": compile_errors[:3] if compile_errors else [],
        "import_ok": len(import_errors) == 0,
        "import_errors": import_errors[:3] if import_errors else [],
        "protected_contract_ok": len(protected_contract_errors) == 0,
        "protected_contract_errors": protected_contract_errors[:3] if protected_contract_errors else [],
        "national_execution_mode": "native_tcp" if native_tcp_mode else "adapter",
        "national_native_contract_ok": len(native_contract_errors) == 0,
        "national_native_contract_errors": native_contract_errors[:5] if native_contract_errors else [],
        "national_capability_contract_ok": national_capability_ok,
        "national_capability_contract_required": national_capability_required,
        "national_capability_contract": national_capability_contract,
        "national_architecture_transition": national_architecture_transition,
        "runtime_contract_identity_ok": runtime_contract_identity_ok,
        "runtime_contract_identity_errors": runtime_contract_identity_errors[:10],
        "runtime_contract_ledger_digest": runtime_contract_ledger_digest,
        "quality_infrastructure": quality_infrastructure,
        "runtime_probe_schema_version": (
            national_capability_contract.get("dynamic_runtime_probe") or {}
        ).get("schema_version"),
        "runtime_probe_orchestrator_version": (
            national_capability_contract.get("dynamic_runtime_probe") or {}
        ).get("orchestrator_version"),
        "runtime_probe_scenario_digest": (
            national_capability_contract.get("dynamic_runtime_probe") or {}
        ).get("scenario_digest"),
        "runtime_probe_limits_digest": (
            national_capability_contract.get("dynamic_runtime_probe") or {}
        ).get("limits_digest"),
        "runtime_probe_identity_digest": (
            national_capability_contract.get("dynamic_runtime_probe") or {}
        ).get("probe_identity_digest"),
        "embedded_selftests_ok": len(embedded_selftest_errors) == 0,
        "embedded_selftest_errors": embedded_selftest_errors[:5] if embedded_selftest_errors else [],
        "smoke_ok": len(smoke_errors) == 0,
        "smoke_errors": smoke_errors[:3] if smoke_errors else [],
        "smoke": smoke_payload,
        "native_tcp_smoke_ok": bool(smoke_payload.get("passed")) if native_tcp_mode and not smoke_payload.get("skipped") else None,
        "native_tcp_smoke": smoke_payload if native_tcp_mode else {},
        "national_protocol_ok": len(national_protocol_errors) == 0,
        "national_protocol_errors": national_protocol_errors[:3] if national_protocol_errors else [],
        "national_acceptance_ok": national_acceptance_ok,
        "national_acceptance_errors": national_acceptance_errors[:5] if national_acceptance_errors else [],
        "national_acceptance": national_acceptance_payload,
        "official_smoke_ok": official_smoke_ok,
        "official_smoke_errors": official_smoke_errors[:5] if official_smoke_errors else [],
        "official_smoke": official_smoke_payload,
        "official_smoke_blocking": official_smoke_blocking,
        "official_smoke_inconclusive": official_smoke_inconclusive,
        "official_smoke_classification": official_smoke_classification,
        "decision_pass_rate": round(decision_rate, 2),
        "decision_ok": decision_ok,
        "critical_scenarios_passed": critical_ok,
        "critical_passed": decision_detail.get("critical_passed", 0),
        "critical_total": decision_detail.get("critical_total", 0),
        "critical_failures": critical_failures,
        "decision_failures": decision_detail.get("failures", []),
        "scenario_results": decision_detail.get("scenarios", []),
        "decision_skill_layers": decision_skill_layers,
        "dynamic_test_generation": dynamic_test_meta,
        "total_lines": total_lines,
        "oversized_files": {name: lines for name, lines, _ in oversized} if oversized else {},
        "size_ok": len(oversized) == 0,
        "fix_verification": fix_results,
        "fix_ok": fix_ok,
        "all_passed": all_passed,
        # A3/fix-3: TRUE-SHADOW is BLOCKING (added to compile_errors above);
        # "review" level warnings remain advisory. Reviewer/critic/orchestrator
        # can read these to distinguish blocking vs advisory.
        "placement_shadow_warnings": placement_shadow_warnings,
        # M6: BLOCKING (unlike placement_shadow "review" level which is advisory).
        # detector whose stderr.write is nested in a bucket If-gate yields sub-arm-only
        # telemetry → false-INERT on daemon grep (v154 99.98%-delta=+0 artifact).
        "telemetry_fidelity_warnings": telemetry_fidelity_warnings,
        "telemetry_fidelity_ok": telemetry_fidelity_ok,
        "reachability_warnings": reachability_warnings,
        "reachability_ok": reachability_ok,
        "position_semantics_ok": position_semantics_ok,
        "position_semantics_errors": position_semantics_errors[:10],
        "code_fingerprint": code_fingerprint,
        "diff_hash": diff_hash,
        "declared_scope_ok": declared_scope_ok,
        "declared_scope_errors": declared_scope_errors[:10],
        "declared_scope": declared_scope_metrics,
        "skill_layers": declared_skill_layers,
    }

    # Build list of which specific gates failed (for diagnostics)
    failed_gates_detail = []
    if compile_errors:
        failed_gates_detail.append("compile")
    if import_errors:
        first_import = import_errors[0]
        failed_gates_detail.append(
            f"runtime_import({first_import.get('module')}: "
            f"{first_import.get('exception')} {first_import.get('message')})"
        )
    if protected_contract_errors:
        failed_gates_detail.append("protected_contract")
    if native_contract_errors:
        failed_gates_detail.append(f"national_native_contract({'; '.join(native_contract_errors[:3])})")
        for err in (native_contract_errors[:6] if not quality_infrastructure["active"] else []):
            _record_quality_failure(
                v,
                "national_native_contract",
                "native_tcp",
                f"Native national TCP contract violation: {err}",
            )
    if quality_infrastructure["active"]:
        issue_text = "; ".join(str(item) for item in quality_infrastructure["issues"][:3])
        failed_gates_detail.append(
            f"runtime_probe_infrastructure({issue_text[:500]})"
        )
    if not national_capability_ok and not quality_infrastructure["active"]:
        failures = national_capability_blockers
        failed_gates_detail.append(
            f"national_capability_contract({'; '.join(str(item.get('name', item))[:120] for item in failures[:3])})"
        )
        for item in failures[:6]:
            _record_quality_failure(
                v,
                "national_capability_contract",
                str(item.get("name", "runtime_architecture")),
                f"National runtime architecture contract issue: {item.get('guidance') or item}",
            )
    if not runtime_contract_identity_ok:
        failed_gates_detail.append(
            "runtime_contract_identity(" + "; ".join(runtime_contract_identity_errors[:3]) + ")"
        )
    if embedded_selftest_errors:
        failed_gates_detail.append(
            f"embedded_selftests({'; '.join(e[:120] for e in embedded_selftest_errors[:3])})"
        )
        for err in (embedded_selftest_errors[:6] if not quality_infrastructure["active"] else []):
            _record_quality_failure(
                v,
                "embedded_selftests",
                "bot_selftest",
                f"Embedded bot self-test failure: {err[:2000]}",
            )
    if true_shadows:
        failed_gates_detail.append(
            f"placement_shadow({'; '.join(w[:120] for w in true_shadows[:3])})"
        )
        for warning in (true_shadows if not quality_infrastructure["active"] else []):
            _record_quality_failure(
                v,
                "placement_shadow",
                "placement_shadow",
                "TRUE-SHADOW detector call-site unreachable for stack-covering all-ins. "
                + warning,
            )
    if smoke_errors:
        failed_gates_detail.append("smoke_test")
    if national_protocol_errors:
        failed_gates_detail.append("national_protocol_tests")
    if not national_acceptance_ok:
        failed_gates_detail.append("national_acceptance")
    if not official_smoke_ok and official_smoke_blocking:
        failed_gates_detail.append("official_smoke")
    if not decision_ok:
        failed_gates_detail.append(f"decision_tests({decision_rate:.0%})")
    if not code_changed:
        failed_gates_detail.append(
            f"no_code_changes(v{v} has no decision-artifact file delta after prepared baseline)"
        )
    if not post_master_delta_ok:
        failed_gates_detail.append(
            "no_post_master_delta(candidate has no file delta after frozen prepared baseline)"
        )
    if not declared_scope_ok:
        failed_gates_detail.append(f"declared_scope({'; '.join(declared_scope_errors[:3])})")
    if oversized:
        failed_gates_detail.append(f"file_size({', '.join(f'{n}:{l}L/{lim}L' for n, l, lim in oversized)})")
    if not fix_ok:
        detail_parts = [f"{fid}: {r.get('reason', 'unknown')[:160]}" for fid, r in fix_failed.items()]
        failed_gates_detail.append(f"missing_fix({'; '.join(detail_parts)})")
        # Record to worker_failures.jsonl so future worker prompts see the missing fix
        # (this is the primary feedback path into workers; reviewer_feedback injection
        # is intentionally omitted to avoid an out-of-order _ckpt reference here).
        for fid, r in (fix_failed.items() if not quality_infrastructure["active"] else []):
            _record_quality_failure(
                v, "fix_verifier", fid,
                f"Mandatory fix {fid} NOT present: {r.get('reason', '')[:2000]}",
            )
    if not telemetry_fidelity_ok:
        failed_gates_detail.append(
            f"telemetry_fidelity({'; '.join(w[:120] for w in telemetry_fidelity_warnings[:3])})"
        )
        # Record the M6 telemetry-fidelity violation to worker_failures so the next
        # worker attempt sees the hoist recipe (function-scope stderr.write + fixture).
        for w in (telemetry_fidelity_warnings if not quality_infrastructure["active"] else []):
            _record_quality_failure(
                v, "telemetry_fidelity", "multi_arm_detector",
                f"M6 telemetry-fidelity violation (false-INERT risk): {w[:2000]}",
            )
    if not reachability_ok:
        failed_gates_detail.append(
            f"reachability({'; '.join(w[:120] for w in reachability_warnings[:3])})"
        )
        for w in (reachability_warnings if not quality_infrastructure["active"] else []):
            _record_quality_failure(
                v, "reachability", "dead_code",
                f"R1 reachability violation: {w[:2000]}",
            )
    if not position_semantics_ok:
        failed_gates_detail.append(
            f"position_semantics({'; '.join(e[:120] for e in position_semantics_errors[:3])})"
        )
        for err in (position_semantics_errors[:6] if not quality_infrastructure["active"] else []):
            _record_quality_failure(
                v, "position_semantics", "national_rules",
                f"Position semantics violation: {err}",
            )

    if quality_infrastructure["active"]:
        failed_gates_detail = [
            "quality_infrastructure("
            + "; ".join(str(item) for item in quality_infrastructure["issues"][:3])[:800]
            + ")"
        ]
    result["failed_gates"] = failed_gates_detail if not all_passed else []
    quality_detail = {
        "all_passed": all_passed,
        "workflow_profile_id": workflow_profile.profile_id,
        "critical_scenarios_passed": critical_ok,
        "decision_pass_rate": round(decision_rate, 4),
        "decision_ok": decision_ok,
        "failed_gates": result["failed_gates"],
        "compile_ok": result["compile_ok"],
        "compile_errors": result["compile_errors"],
        "import_ok": result["import_ok"],
        "import_errors": result["import_errors"],
        "protected_contract_ok": result["protected_contract_ok"],
        "protected_contract_errors": result["protected_contract_errors"],
        "national_execution_mode": result["national_execution_mode"],
        "national_native_contract_ok": result["national_native_contract_ok"],
        "national_native_contract_errors": result["national_native_contract_errors"],
        "national_capability_contract_ok": result["national_capability_contract_ok"],
        "national_capability_contract_required": result["national_capability_contract_required"],
        "national_capability_contract": result["national_capability_contract"],
        "national_architecture_transition": result["national_architecture_transition"],
        "runtime_contract_identity_ok": result["runtime_contract_identity_ok"],
        "runtime_contract_identity_errors": result["runtime_contract_identity_errors"],
        "runtime_contract_ledger_digest": result["runtime_contract_ledger_digest"],
        "quality_infrastructure": result["quality_infrastructure"],
        "runtime_probe_schema_version": result["runtime_probe_schema_version"],
        "runtime_probe_orchestrator_version": result["runtime_probe_orchestrator_version"],
        "runtime_probe_scenario_digest": result["runtime_probe_scenario_digest"],
        "runtime_probe_limits_digest": result["runtime_probe_limits_digest"],
        "runtime_probe_identity_digest": result["runtime_probe_identity_digest"],
        "national_capability_required_failure_count": len(
            result["national_capability_contract"].get("required_failures") or []
        ),
        "national_capability_advisory_count": len(
            result["national_capability_contract"].get("advisory_warnings") or []
        ),
        "embedded_selftests_ok": result["embedded_selftests_ok"],
        "embedded_selftest_errors": result["embedded_selftest_errors"],
        "smoke_ok": result["smoke_ok"],
        "smoke_errors": result["smoke_errors"],
        "smoke": smoke_payload,
        "native_tcp_smoke_ok": result["native_tcp_smoke_ok"],
        "native_tcp_smoke": result["native_tcp_smoke"],
        "national_protocol_ok": result["national_protocol_ok"],
        "national_protocol_errors": result["national_protocol_errors"],
        "national_acceptance_ok": result["national_acceptance_ok"],
        "national_acceptance_errors": result["national_acceptance_errors"],
        "national_acceptance": national_acceptance_payload,
        "official_smoke_ok": result["official_smoke_ok"],
        "official_smoke_errors": result["official_smoke_errors"],
        "official_smoke": official_smoke_payload,
        "official_smoke_blocking": result["official_smoke_blocking"],
        "official_smoke_inconclusive": result["official_smoke_inconclusive"],
        "official_smoke_classification": result["official_smoke_classification"],
        "official_certification_status": official_local_status or {},
        "dynamic_test_generation": dynamic_test_meta,
        "size_ok": result["size_ok"],
        "oversized_files": result["oversized_files"],
        "code_changed": code_changed,
        "post_master_delta_ok": post_master_delta_ok,
        "post_master_delta_required": post_master_delta_required,
        "prepared_artifact_hash": prepared_artifact_hash,
        "post_master_changed_files": post_master_changed_files[:20],
        "post_master_scope_errors": post_master_scope_errors[:6],
        "changed_files": post_master_changed_files[:20],
        "source_python_changed": source_python_changed,
        "source_python_changed_files": changed_files_list[:20],
        "declared_scope_ok": declared_scope_ok,
        "declared_scope_errors": declared_scope_errors[:6],
        "declared_scope": declared_scope_metrics,
        "skill_layers": declared_skill_layers,
        "diff_hash": diff_hash,
        "fix_ok": fix_ok,
        "placement_shadow_warnings": placement_shadow_warnings[:10],
        "placement_shadow_review_count": len([w for w in placement_shadow_warnings if "TRUE SHADOW" not in w]),
        "telemetry_fidelity_ok": telemetry_fidelity_ok,
        "reachability_ok": reachability_ok,
        "reachability_warnings": reachability_warnings[:6],
        "position_semantics_ok": position_semantics_ok,
        "position_semantics_errors": position_semantics_errors[:10],
        "code_fingerprint": code_fingerprint,
        "critical_failures": critical_failures[:3],
        "decision_skill_layers": decision_skill_layers,
    }
    scorecard = ScoreCard(name="quality")
    scorecard.add(GateResult.from_bool(
        "code_changed",
        code_changed,
        failures=(
            []
            if code_changed
            else ["no decision-artifact file changed after the frozen prepared baseline"]
        ),
        metrics={
            "prepared_artifact_hash": prepared_artifact_hash,
            "candidate_artifact_hash": code_fingerprint,
            "changed_files": post_master_changed_files[:20],
            "source_python_changed": source_python_changed,
        },
    ))
    scorecard.add(GateResult.from_bool(
        "post_master_delta",
        post_master_delta_ok,
        blocking=post_master_delta_required,
        hidden=not post_master_delta_required,
        failures=(
            []
            if post_master_delta_ok
            else ["candidate has no file delta after the frozen prepared baseline"]
        ),
        metrics={
            "required": post_master_delta_required,
            "prepared_artifact_hash": prepared_artifact_hash,
            "candidate_artifact_hash": code_fingerprint,
        },
    ))
    scorecard.add(GateResult.from_bool(
        "declared_scope",
        declared_scope_ok,
        metrics=declared_scope_metrics,
        failures=declared_scope_errors[:6],
    ))
    scorecard.add(GateResult.from_bool("compile", len(compile_errors) == 0, failures=compile_errors[:3]))
    scorecard.add(GateResult.from_bool("runtime_import", len(import_errors) == 0, failures=[str(e) for e in import_errors[:3]]))
    scorecard.add(GateResult.from_bool("protected_contract", len(protected_contract_errors) == 0, failures=protected_contract_errors[:3]))
    scorecard.add(GateResult.from_bool(
        "national_native_contract",
        len(native_contract_errors) == 0,
        failures=native_contract_errors[:5],
        metrics={"execution_mode": "native_tcp" if native_tcp_mode else "adapter"},
        blocking=native_tcp_mode,
        hidden=not native_tcp_mode,
    ))
    capability_required_failures = national_capability_contract.get("required_failures") or []
    capability_advisory_warnings = national_capability_contract.get("advisory_warnings") or []
    capability_failures = national_capability_blockers
    scorecard.add(GateResult.from_bool(
        "national_capability_contract",
        national_capability_ok,
        failures=[
            str(item.get("name", item))[:300] if isinstance(item, dict) else str(item)[:300]
            for item in capability_failures[:8]
        ],
        metrics={
            "execution_mode": "native_tcp" if native_tcp_mode else "adapter",
            "required": national_capability_required,
            "required_failure_count": len(capability_required_failures),
            "advisory_warning_count": len(capability_advisory_warnings),
            "regression_count": len(national_architecture_transition.get("regressions") or []),
            "unresolved_focus_count": len(
                national_architecture_transition.get("unresolved_focus_checks") or []
            ),
        },
        artifacts={
            "contract": national_capability_contract,
            "transition": national_architecture_transition,
        },
        blocking=native_tcp_mode,
        hidden=not native_tcp_mode,
    ))
    scorecard.add(GateResult.from_bool(
        "runtime_probe_infrastructure",
        not quality_infrastructure["active"],
        failures=[str(item)[:300] for item in quality_infrastructure["issues"][:8]],
        metrics={
            "attempt": quality_infrastructure["attempt"],
            "max_attempts": quality_infrastructure["max_attempts"],
            "retryable": quality_infrastructure["retryable"],
            "failure_class": quality_infrastructure.get("failure_class", ""),
        },
        blocking=native_tcp_mode,
        hidden=not native_tcp_mode,
    ))
    scorecard.add(GateResult.from_bool(
        "runtime_contract_identity",
        runtime_contract_identity_ok,
        failures=runtime_contract_identity_errors[:8],
        metrics={"ledger_digest": runtime_contract_ledger_digest},
        blocking=native_tcp_mode,
        hidden=not native_tcp_mode,
    ))
    scorecard.add(GateResult.from_bool(
        "embedded_selftests",
        len(embedded_selftest_errors) == 0,
        failures=embedded_selftest_errors[:5],
    ))
    scorecard.add(GateResult.from_bool(
        "smoke",
        len(smoke_errors) == 0,
        failures=smoke_errors[:3],
        metrics={
            "execution_mode": smoke_payload.get("execution_mode", "local_json"),
            "hands": smoke_payload.get("hands"),
        },
        artifacts={"report": smoke_payload} if smoke_payload else {},
    ))
    scorecard.add(GateResult.from_bool(
        "placement_shadow_review",
        not bool([w for w in placement_shadow_warnings if "TRUE SHADOW" not in w]),
        blocking=False,
        failures=[w for w in placement_shadow_warnings if "TRUE SHADOW" not in w][:6],
        metrics={"review_count": len([w for w in placement_shadow_warnings if "TRUE SHADOW" not in w])},
    ))
    scorecard.add(GateResult.from_bool("national_protocol", len(national_protocol_errors) == 0, failures=national_protocol_errors[:3]))
    scorecard.add(GateResult.from_bool(
        "national_acceptance",
        national_acceptance_ok,
        failures=national_acceptance_errors[:5],
        metrics=national_acceptance_payload.get("summary", {}) if isinstance(national_acceptance_payload, dict) else {},
        artifacts={"report": national_acceptance_payload} if national_acceptance_payload else {},
    ))
    scorecard.add(GateResult.from_bool(
        "official_smoke",
        official_smoke_ok,
        failures=official_smoke_errors[:5],
        metrics={
            "status": official_smoke_payload.get("status"),
            "mode": official_smoke_payload.get("mode"),
            "queued": official_smoke_payload.get("queued"),
            "cache_hit": official_smoke_payload.get("cache_hit"),
            "blocking": official_smoke_blocking,
            "inconclusive": official_smoke_inconclusive,
            "classification": official_smoke_classification,
        } if isinstance(official_smoke_payload, dict) else {},
        artifacts={"report": official_smoke_payload} if official_smoke_payload else {},
        blocking=official_smoke_blocking,
    ))
    official_status_error = (
        str(official_local_status.get("error"))
        if isinstance(official_local_status, dict) and official_local_status.get("error")
        else ""
    )
    scorecard.add(GateResult.from_bool(
        "official_status_persistence",
        not bool(official_status_error),
        failures=[official_status_error] if official_status_error else [],
        blocking=native_tcp_mode,
        hidden=not native_tcp_mode or not candidate_gate_checks_passed,
    ))
    scorecard.add(GateResult.from_bool(
        "decision",
        decision_ok,
        metrics={"pass_rate": round(decision_rate, 4), "total": decision_total},
        artifacts={"skill_layers": decision_skill_layers} if decision_skill_layers else {},
        failures=[str(f)[:300] for f in (decision_detail.get("failures", []) or [])[:5]],
    ))
    scorecard.add(GateResult.from_bool("size", len(oversized) == 0, failures=[f"{n}:{l}/{lim}" for n, l, lim in oversized]))
    scorecard.add(GateResult.from_bool("fix_verification", fix_ok, failures=[f"{fid}: {r.get('reason', '')[:160]}" for fid, r in fix_failed.items()]))
    scorecard.add(GateResult.from_bool("telemetry_fidelity", telemetry_fidelity_ok, failures=telemetry_fidelity_warnings[:6]))
    scorecard.add(GateResult.from_bool("reachability", reachability_ok, failures=reachability_warnings[:6]))
    scorecard.add(GateResult.from_bool("position_semantics", position_semantics_ok, failures=position_semantics_errors[:6]))
    if quality_infrastructure["active"]:
        phase_gate_names = {
            "candidate_hygiene": {"national_native_contract"},
            "contract_identity": {"runtime_contract_identity"},
            "declared_scope": {"declared_scope"},
            "compile": {"compile"},
            "runtime_import": {"runtime_import"},
            "protected_contract": {"protected_contract"},
            "native_contract": {"national_native_contract"},
            "runtime_architecture": {"national_capability_contract"},
            "embedded_selftest": {"embedded_selftests"},
            "telemetry_fidelity": {"telemetry_fidelity"},
            "reachability": {"reachability"},
            "position_semantics": {"position_semantics"},
            "workflow_smoke": {"smoke"},
            "national_protocol": {"national_protocol"},
            "national_acceptance": {"national_acceptance"},
            "official_smoke": {"official_smoke"},
            "official_status_persistence": {"official_status_persistence"},
            "decision_tests": {"decision"},
            "code_size": {"size"},
            "fix_verification": {"fix_verification"},
        }
        infra_failures_by_gate: dict[str, list[str]] = {}
        for item in quality_infra_issues:
            for gate_name in phase_gate_names.get(str(item.get("phase") or ""), set()):
                infra_failures_by_gate.setdefault(gate_name, []).extend(
                    str(issue) for issue in item.get("issues") or []
                )
        for gate in scorecard.gates:
            if gate.name == "runtime_probe_infrastructure":
                gate.status = "error"
                gate.metrics = {**gate.metrics, "failure_class": "infrastructure"}
            if gate.name in infra_failures_by_gate:
                gate.status = "error"
                gate.failures = infra_failures_by_gate[gate.name][:6]
                gate.metrics = {**gate.metrics, "failure_class": "infrastructure"}
    result["scorecard"] = scorecard.model_dump()
    quality_detail["scorecard"] = result["scorecard"]
    quality_stage = (
        "workers_done"
        if quality_infrastructure["active"]
        else ("quality_passed" if all_passed else "quality_failed")
    )
    result["checkpoint_stage"] = quality_stage
    result["action"] = quality_infrastructure.get("action", "")
    quality_event = (
        "pipeline.quality_infra_retry"
        if quality_infrastructure["active"] and not quality_infrastructure["exhausted"]
        else "pipeline.quality_infra_exhausted"
        if quality_infrastructure["active"]
        else "pipeline.quality_passed"
        if all_passed
        else "pipeline.quality_failed"
    )
    quality_severity = "success" if all_passed else "warn" if quality_infrastructure["active"] else "error"
    quality_verb = "passed" if all_passed else "inconclusive" if quality_infrastructure["active"] else "failed"
    log_system_event(
        quality_event,
        quality_severity,
        f"Quality gates {quality_verb} for v{v}: {', '.join(failed_gates_detail) or 'all checks passed'}",
        {"version": v, "pass_rate": round(decision_rate, 2), **quality_detail},
    )

    _ckpt = _matching_checkpoint(v, source_v) if source_v is not None else _matching_checkpoint(v)
    if _ckpt:
        source_v = _ckpt["source_v"]
        gate = _gate_payload(
            v,
            source_v,
            all_passed,
            **quality_detail,
        )
        recorded = _record_gate(
            v,
            source_v,
            "quality",
            gate,
            stage=quality_stage,
            infra_failure=(
                {key: value for key, value in quality_infrastructure.items() if key != "active"}
                if quality_infrastructure["active"]
                else None
            ),
            clear_infra_failure=(
                not quality_infrastructure["active"] and existing_infra is not None
            ),
            infra_failure_owner=(
                "run_quality_gates"
                if not quality_infrastructure["active"] and existing_infra is not None
                else None
            ),
            expected_infra_failure_digest=(
                infrastructure_failure_digest(existing_infra)
                if quality_infrastructure["active"] or existing_infra is not None
                else None
            ),
            record_gate=not quality_infrastructure["active"],
        )
        result["checkpoint_recorded"] = bool(recorded)
        result["source_v"] = source_v
    else:
        result["checkpoint_recorded"] = False

    if append_candidate_event:
        try:
            append_candidate_event(
                "quality_finished",
                version=v,
                source_v=source_v,
                candidate_id=candidate_id,
                profile_id=workflow_profile.profile_id,
                workflow_profile_id=workflow_profile.profile_id,
                run_id=f"{v}#0",
                stage=quality_stage,
                parent_ids=[bot_name(source_v)] if source_v is not None else [],
                changed_files=post_master_changed_files,
                skill_layers=declared_skill_layers,
                diff_hash=diff_hash,
                gate="quality",
                scorecard=scorecard,
                gate_results=scorecard.gates,
                metrics={
                    "all_passed": all_passed,
                    "decision_pass_rate": round(decision_rate, 4),
                    "decision_skill_layers": decision_skill_layers,
                    "national_acceptance_ok": national_acceptance_ok,
                    "declared_scope_ok": declared_scope_ok,
                },
                failures=failed_gates_detail if not all_passed else [],
                failure_class=(
                    "quality_infrastructure"
                    if quality_infrastructure["active"]
                    else "quality_gate"
                    if not all_passed
                    else ""
                ),
                artifacts={"national_acceptance": national_acceptance_payload} if national_acceptance_payload else {},
            )
        except Exception as e:
            _log.warning("candidate ledger quality_finished write failed: %s", e)

    if (
        quality_infrastructure["active"]
        and quality_infrastructure["exhausted"]
        and result.get("checkpoint_recorded")
    ):
        from tool_bot_management import _do_abandon_generation

        abandon_result = await _do_abandon_generation(
            reason=f"infrastructure_exhausted:{quality_infrastructure.get('component')}"
        )
        result["abandon_result"] = abandon_result
        result["abandoned"] = bool(abandon_result.get("abandoned"))
        if result["abandoned"]:
            result["checkpoint_stage"] = "abandoned"

    try:
        log_system_event("pipeline.quality_gates", "info",
                         f"Quality gates finished for v{v} in {time.time() - _t0:.1f}s",
                         {"version": v, "all_passed": all_passed, "elapsed_sec": round(time.time() - _t0, 2)})
    except Exception:
        pass

    return _json_tool_result(result)


# ──────────────────────────────────────────────
# Prepare Next Gen
# ──────────────────────────────────────────────

@tool("prepare_next_gen", "Prepare the next generation directory by copying from source bot.", {"source_v": int, "next_v": int})
async def prepare_next_gen(args):
    _t0 = time.time()
    source_v = args.get("source_v")
    next_v = args.get("next_v")
    active_ckpt = read_pipeline_checkpoint()
    using_active_checkpoint = False
    if active_ckpt and active_ckpt.get("next_v") is not None and active_ckpt.get("source_v") is not None:
        active_stage = active_ckpt.get("stage")
        active_next_v = int(active_ckpt.get("next_v"))
        active_source_v = int(active_ckpt.get("source_v"))
        active_next_tool = next_tool_for_checkpoint(active_ckpt)
        if active_stage == "selected" and active_ckpt.get("parent2_v") is not None:
            return _json_tool_result({
                "blocked": True,
                "error": (
                    f"Active generation v{active_next_v} is a crossover from "
                    f"v{active_source_v} x v{active_ckpt.get('parent2_v')}; "
                    "call run_crossover instead of prepare_next_gen."
                ),
                "next_v": active_next_v,
                "source_v": active_source_v,
                "stage": active_stage,
                "next_tool": "run_crossover",
                "required_args": {
                    "version": active_next_v,
                    "parent_a": active_source_v,
                    "parent_b": active_ckpt.get("parent2_v"),
                },
            })
        requested_source = int(source_v) if source_v is not None else None
        requested_next = int(next_v) if next_v is not None else None
        if requested_source is None or requested_next is None:
            source_v = active_source_v
            next_v = active_next_v
            using_active_checkpoint = True
        elif requested_source != active_source_v or requested_next != active_next_v:
            if active_stage in {"selected", "preparing", "prepared", "timed_out"}:
                log_system_event(
                    "pipeline.prepare_args_overridden",
                    "warn",
                    (
                        f"prepare_next_gen ignored stale args v{requested_next}/source v{requested_source}; "
                        f"using active v{active_next_v}/source v{active_source_v}"
                    ),
                    {
                        "requested_next_v": requested_next,
                        "requested_source_v": requested_source,
                        "next_v": active_next_v,
                        "source_v": active_source_v,
                        "stage": active_stage,
                        "next_tool": "prepare_next_gen",
                    },
                )
                source_v = active_source_v
                next_v = active_next_v
                using_active_checkpoint = True
            else:
                return _json_tool_result({
                    "blocked": True,
                    "error": (
                        f"Active pipeline is v{active_next_v}/source v{active_source_v} "
                        f"at stage {active_stage}; refusing stale prepare request "
                        f"v{requested_next}/source v{requested_source}."
                    ),
                    "next_v": active_next_v,
                    "source_v": active_source_v,
                    "stage": active_stage,
                    "next_tool": active_next_tool,
                })
        else:
            using_active_checkpoint = True
    if source_v is None or next_v is None:
        _v, source_v = _resolve_version_args(args)
        next_v = next_v or _v
    if source_v is None or next_v is None:
        return _json_tool_result({"error": "Missing source_v/next_v and no active checkpoint"})

    _set_pipeline_status(f"Preparing v{next_v}")

    if next_v <= source_v:
        return _json_tool_result({"error": f"next_v ({next_v}) must be greater than source_v ({source_v})"})

    # Guard against clearly invalid version numbers (test artifacts)
    if next_v >= 900:
        return _json_tool_result({"error": f"next_v ({next_v}) is invalid. Version numbers must be < 900."})

    current_v = find_current_v()
    if not using_active_checkpoint and next_v > current_v + 10:
        return _json_tool_result({"error": f"next_v ({next_v}) is too far ahead of current_v ({current_v}). Use next_v = {current_v + 1}."})

    source_dir = get_bot_dir(source_v)
    next_dir = get_bot_dir(next_v)

    if not source_dir.exists():
        return _json_tool_result({"error": f"Source bot v{source_v} not found"})

    # Guard: warn if source bot is not completed (may be broken)
    if not (source_dir / ".completed").exists():
        return _json_tool_result({"error": f"Source bot v{source_v} is not marked completed. Cannot use incomplete code as source."})

    # Guard: verify git tag exists for source bot (authoritative commit proof)
    from evolution_infra import copy_bot_tree_for_candidate, git_has_tag, git_dir_is_committed
    if not git_has_tag(source_v):
        return _json_tool_result({"error": f"Source bot v{source_v} has .completed but no git tag '{bot_tag(source_v)}'. Cannot evolve from uncommitted code. Try a different source version."})
    from evolution_infra import get_active_bots

    if bot_name(source_v) not in set(get_active_bots()):
        return _json_tool_result({
            "error": (
                f"Source bot v{source_v} is not eligible for the active national pool "
                "(reaped, protocol-invalid, uncertified, or grandfather grant expired)."
            )
        })

    # Guard: refuse to overwrite a completed bot
    if next_dir.exists() and (next_dir / ".completed").exists():
        return _json_tool_result({"error": f"Target v{next_v} already exists and is completed. Refusing to overwrite."})

    # Guard: refuse to overwrite a bare-committed target (root-cause fix for the
    # v117 repeated-regeneration loop, 2026-06-18; mirrors run_crossover).
    if next_dir.exists() and git_dir_is_committed(next_v) and not git_has_tag(next_v):
        return _json_tool_result({
            "error": f"Target v{next_v} is git-committed but has no {bot_tag(next_v)} tag (bare commit bypassing commit_bot). "
                     f"Refusing to overwrite — re-preparing here causes infinite regeneration. "
                     f"Run commit_bot for v{next_v} to finalize it, or abandon/clear the untagged dir first."
        })

    # Guard: refuse to re-prepare if pipeline has already progressed past "prepared"
    _ckpt = _matching_checkpoint(next_v, source_v)
    if _ckpt and _ckpt.get("stage") not in (None, "selected", "preparing", "prepared", "timed_out"):
        return _json_tool_result({"error": f"Pipeline for v{next_v} already at stage '{_ckpt['stage']}'. Refusing to overwrite worker output. Call abandon_generation first if you want to restart."})

    from evolution_infra import write_pipeline_checkpoint
    if not write_pipeline_checkpoint(next_v, source_v, "preparing", worker_failure_count=0):
        return _json_tool_result({
            "error": f"Failed to persist preparing checkpoint for v{next_v}; refusing to mutate bot directory."
        })

    if next_dir.exists():
        # Guard against silent cross-source overwrite. v107 (2026-06-16) was
        # repeatedly re-prepared from DIFFERENT ancestors (106/105/102) because
        # each crashed cycle reset the checkpoint, _matching_checkpoint(next_v,
        # source_v) returned None for the new source, the stage guard was
        # bypassed, and this rmtree silently destroyed the previous attempt's
        # worker output. Refuse unless the existing dir was prepared from the
        # SAME source (a legitimate same-generation retry) or explicitly cleared.
        prior_ckpt = read_pipeline_checkpoint() or {}
        prior_source = prior_ckpt.get("source_v")
        if prior_source is not None and prior_source != source_v:
            log_system_event(
                "pipeline.prepare_cross_source_refused", "error",
                f"Refusing to overwrite v{next_v}: dir was prepared from "
                f"v{prior_source} but request is from v{source_v}. "
                f"Call abandon_generation first to clear it.",
                {"version": next_v, "source_v": source_v, "prior_source_v": prior_source},
            )
            return _json_tool_result({"error": f"Target v{next_v} already exists, prepared from v{prior_source} (not v{source_v}). Refusing silent cross-source overwrite. Call abandon_generation first."})
        shutil.rmtree(next_dir)
    copy_bot_tree_for_candidate(source_dir, next_dir)

    # Apply known critical fixes regardless of source bot state
    from fix_injection import apply_known_fixes, log_fix_application
    applied, skipped = apply_known_fixes(next_dir)
    if applied or skipped:
        log_fix_application(applied, skipped, next_dir, source_v)
    if skipped:
        _log.info("Fix patches skipped for v%d: %s", next_v, skipped)

    workflow_profile = get_workflow_profile()
    native_tcp = getattr(workflow_profile, "national_execution_mode", "adapter") == "native_tcp"
    from candidate_hygiene import sanitize_candidate_dir
    # Preserve a compliant source national_bot.py, but refresh stale/unsafe
    # entrypoints during hygiene. Old parents can lack the current official EXE
    # allin/check guards; letting that launcher inherit into every child makes
    # protocol failures look like strategy regressions.
    hygiene = sanitize_candidate_dir(
        next_dir,
        require_native_tcp=native_tcp,
        overwrite_native_entry=False,
    )
    prepare_scope_files = [
        p for p in _py_files_changed_between(source_dir, next_dir) if 'backup' not in p
    ]
    if prepare_scope_files:
        log_system_event(
            "pipeline.prepare_scope_captured",
            "info",
            f"Prepare baseline for v{next_v} changed {len(prepare_scope_files)} file(s)",
            {
                "next_v": next_v,
                "source_v": source_v,
                "prepare_scope_files": prepare_scope_files[:20],
            },
        )
    if native_tcp:
        log_system_event(
            "pipeline.native_entry_prepared",
            "info",
            f"Prepared native national TCP entry for v{next_v}",
            {"next_v": next_v, "source_v": source_v, "entry": hygiene.get("native_entry")},
        )

    try:
        from prepared_baseline_contract import build_prepared_artifact_contract

        prepared_artifact_contract = build_prepared_artifact_contract(
            next_dir,
            source_v=source_v,
            next_v=next_v,
        )
    except Exception as exc:
        return _json_tool_result({
            "error": "PREPARED_ARTIFACT_CONTRACT_BUILD_FAILED",
            "next_v": next_v,
            "source_v": source_v,
            "detail": f"{type(exc).__name__}: {str(exc)[:300]}",
            "directive": "Do not run Direction Audit or Master without a frozen prepared artifact.",
        })

    # Write "prepared" checkpoint so a kill+restart shows "Workers not yet run → call run_direction_audit"
    if not write_pipeline_checkpoint(
        next_v,
        source_v,
        "prepared",
        worker_failure_count=0,
        prepare_scope_files=prepare_scope_files,
        audit_context={
            "prepared_artifact_contract": prepared_artifact_contract,
        },
    ):
        return _json_tool_result({
            "error": f"Failed to persist prepared checkpoint for v{next_v}; generation recovery remains at preparing."
        })

    log_system_event("pipeline.prepare_done", "info", f"Prepared v{next_v} from v{source_v}",
                     {"next_v": next_v, "source_v": source_v, "elapsed_sec": round(time.time() - _t0, 2)})
    try:
        from repo_state import log_git_worktree_snapshot
        log_git_worktree_snapshot(
            "repo.worktree_snapshot",
            f"Worktree snapshot after preparing v{next_v}",
            next_v=next_v,
            source_v=source_v,
            stage="prepared",
            emit_delta=True,
        )
    except Exception:
        pass

    return _json_tool_result({"prepared": True, "next_v": next_v, "source_v": source_v})


# ──────────────────────────────────────────────
# Review Stage
# ──────────────────────────────────────────────

@tool("run_review", "Run Lead Code Reviewer on the bot changes. Returns approval decision with quality score.", {"version": int, "source_v": int, "plan": list})
async def run_review(args):
    _t0 = time.time()
    v, source_v = _resolve_version_args(args)
    if v is None or source_v is None:
        return _json_tool_result({"error": "Missing version/source_v and no active pipeline checkpoint"})
    v = int(v)
    source_v = int(source_v)
    supplied_plan = args.get("plan", [])

    _set_pipeline_status(f"Reviewing v{v}")

    ckpt = _matching_checkpoint(v, source_v)
    _review_infra, _review_infra_error = _owned_infrastructure_failure(
        ckpt,
        "run_review",
    )
    if _review_infra_error:
        return _state_blocked(_review_infra_error, v, source_v, ckpt)
    _review_exhausted = await _execute_exhausted_infrastructure_failure(
        v,
        source_v,
        owner_tool="run_review",
    )
    if _review_exhausted is not None:
        return _json_tool_result(_review_exhausted)

    # Idempotency guard: skip if review already approved
    _cached = _idempotency_check(
        v, source_v,
        stage_set=("reviewed", "critic_checked", "verified", "archived"),
        gate_name="review",
        directive="Review ALREADY PASSED. Call run_critic next.",
    )
    if _cached:
        return _cached

    if not _quality_gate_ok(ckpt):
        return _state_blocked(
            "run_review requires run_quality_gates all_passed=true and critical_scenarios_passed=true for the same version/source_v.",
            v,
            source_v,
            ckpt,
        )

    authoritative_plan = (
        ckpt.get("master_plan")
        if isinstance(ckpt, dict) and isinstance(ckpt.get("master_plan"), dict)
        else {"tasks": supplied_plan if isinstance(supplied_plan, list) else []}
    )
    if supplied_plan and supplied_plan != authoritative_plan.get("tasks"):
        log_system_event(
            "pipeline.review_plan_argument_ignored",
            "warn",
            f"run_review v{v} ignored a plan argument that differed from checkpoint authority",
            {"version": v, "source_v": source_v},
        )

    prompts_dir = PROJECT_ROOT / "web" / "core" / "prompts"
    reviewer_prompt = (prompts_dir / "reviewer_prompt.md").read_text()
    reviewer_prompt = reviewer_prompt.replace(
        "{master_plan}",
        json.dumps(authoritative_plan, indent=2, ensure_ascii=False),
    )
    reviewer_prompt = reviewer_prompt.replace("{version}", str(v))
    reviewer_prompt = reviewer_prompt.replace("{parent_version}", str(source_v))

    # Inject Worker CoT audit_focus_areas into reviewer prompt
    _review_ckpt = _matching_checkpoint(v, source_v)
    if _review_ckpt:
        _audit_context = _review_ckpt.get("audit_context", {}) or {}
        _focus_areas = _audit_context.get("worker_cot_focus_areas", [])
        if not _focus_areas:
            # Also check gate_results for audit_focus_areas stored by execute_workers
            _worker_gate = _review_ckpt.get("gate_results", {}).get("workers", {})
            _focus_areas = _worker_gate.get("audit_focus_areas", [])
        if _focus_areas:
            _focus_block = (
                "\n\n# Worker CoT Audit Findings (from execute_workers)\n"
                "The Worker Chain-of-Thought audit detected these concerns.\n"
                "Pay EXTRA attention to these areas during your review:\n"
            )
            for _fa in _focus_areas:
                _focus_block += f"- {_fa}\n"
            _focus_block += "\n"
            reviewer_prompt += _focus_block

    _review_attempt_key, _review_infra_metadata = _llm_gate_infrastructure_identity(
        component="reviewer_llm",
        role="LEAD CODE REVIEWER",
        candidate_dir=get_bot_dir(v),
        source_dir=get_bot_dir(source_v),
        prompt_text=reviewer_prompt,
        checkpoint=ckpt,
    )

    log_file = get_logs_dir(v) / "reviewer_io.txt"

    ui = _get_ui()
    try:
        output, _, _ = await run_claude_query(
            reviewer_prompt, [], ui, "LEAD CODE REVIEWER", log_file, tools=["Bash", "Read"]
        )
    except Exception as e:
        issue = f"{type(e).__name__}: {str(e)[:500]}"
        infra_result = await _record_infrastructure_failure(
            v,
            source_v,
            owner_tool="run_review",
            resume_stage="quality_passed",
            component="reviewer_llm",
            code="reviewer_llm_unavailable",
            attempt_key=_review_attempt_key,
            issues=[issue],
            max_attempts=3,
            metadata=_review_infra_metadata,
            master_plan=authoritative_plan,
        )
        attempt = (infra_result.get("infra_failure") or {}).get("attempt")
        log_system_event(
            "pipeline.review_infra_error",
            "error" if infra_result.get("action") == "abandon_generation" else "warn",
            f"Reviewer v{v} unavailable (infrastructure attempt {attempt or '?'}/3)",
            {"version": v, "source_v": source_v, "issue": issue, **infra_result},
        )
        ui.log_history(f"Reviewer infrastructure failure (not a code rejection): {issue}", "warn")
        return _json_tool_result({
            **infra_result,
            "llm_failed": True,
            "approved": None,
            "directive": (
                "Reviewer infrastructure retry exhausted; generation was safely abandoned."
                if infra_result.get("action") == "abandon_generation"
                else "Retry run_review for the same candidate; do not run workers or Master."
            ),
            "logs": ui.get_output(),
        })
    from llm_query import parse_json_output_with_mode
    data, _review_mode = parse_json_output_with_mode(output)
    _review_schema_errors = []
    if data and isinstance(data, dict) and "approved" in data:
        from output_schema import validate_agent_output

        data, _review_schema_errors = validate_agent_output("reviewer", data)

    if not (data and "approved" in data) or _review_schema_errors:
        error_msg = (
            "Reviewer schema validation failed: " + "; ".join(_review_schema_errors[:5])
            if _review_schema_errors
            else
            "Reviewer returned valid JSON but missing 'approved' field"
            if data and isinstance(data, dict)
            else f"Reviewer failed to produce valid JSON (mode={_review_mode})"
        )
        infra_result = await _record_infrastructure_failure(
            v,
            source_v,
            owner_tool="run_review",
            resume_stage="quality_passed",
            component="reviewer_llm",
            code="reviewer_llm_unavailable",
            attempt_key=_review_attempt_key,
            issues=[error_msg],
            max_attempts=3,
            metadata={
                **_review_infra_metadata,
                "parse_mode": _review_mode,
                "raw_output_digest": hashlib.sha256(
                    (output or "").encode("utf-8")
                ).hexdigest(),
            },
            master_plan=authoritative_plan,
        )
        attempt = (infra_result.get("infra_failure") or {}).get("attempt")
        log_system_event(
            "pipeline.review_parse_error",
            "error" if infra_result.get("action") == "abandon_generation" else "warn",
            f"Reviewer v{v} output was unusable (infrastructure attempt {attempt or '?'}/3)",
            {
                "version": v,
                "source_v": source_v,
                "mode": _review_mode,
                "error": error_msg,
                **infra_result,
            },
        )
        ui.log_history(f"Reviewer output parse error (NOT a code rejection): {error_msg}", "warn")
        result = {
            **infra_result,
            "directive": (
                "Reviewer output remained unusable and the generation was safely abandoned."
                if infra_result.get("action") == "abandon_generation"
                else "Call run_review again for the same candidate; do not run workers or Master."
            ),
            "llm_failed": True,
            "parse_error": True,
            "approved": None,
            "logs": ui.get_output(),
        }
        try:
            log_system_event(
                "pipeline.review_done",
                "info",
                f"Review finished for v{v} in {time.time() - _t0:.1f}s",
                {
                    "version": v,
                    "approved": False,
                    "parse_error": True,
                    "elapsed_sec": round(time.time() - _t0, 2),
                },
            )
        except Exception:
            pass
        return _json_tool_result(result)

    if data and "approved" in data:
        approved = data["approved"] is True
        feedback = data.get("feedback", "")
        try:
            log_system_event(
                "pipeline.review_passed" if approved else "pipeline.review_rejected",
                "success" if approved else "warn",
                f"Review {'approved' if approved else 'rejected'} v{v} (score={data.get('quality_score', 0)})",
                {"version": v, "score": data.get("quality_score", 0), "approved": approved},
            )
        except Exception:
            pass
        gate = _gate_payload(
            v,
            source_v,
            approved,
            approved=approved,
            quality_score=data.get("quality_score", 0),
            feedback=feedback,
            change_summary=data.get("change_summary", ""),
            risk_areas=data.get("risk_areas", []),
        )
        checkpoint_recorded = _record_gate(
            v,
            source_v,
            "review",
            gate,
            stage="reviewed" if approved else "repair_planned",
            master_plan=authoritative_plan,
            reviewer_feedback=feedback,
            clear_infra_failure=_review_infra is not None,
            infra_failure_owner="run_review" if _review_infra is not None else None,
            expected_infra_failure_digest=(
                infrastructure_failure_digest(_review_infra)
                if _review_infra is not None
                else None
            ),
        )
        if not approved:
            _record_quality_failure(v, "reviewer", "Code Reviewer",
                                    f"Rejected (score={data.get('quality_score', 0)}): {feedback[:2000]}")
        result = {
            "approved": approved,
            "quality_score": data.get("quality_score", 0),
            "change_summary": data.get("change_summary", ""),
            "risk_areas": data.get("risk_areas", []),
            "feedback": feedback,
            "checkpoint_recorded": checkpoint_recorded,
            "logs": ui.get_output(),
        }
    try:
        log_system_event("pipeline.review_done", "info",
                         f"Review finished for v{v} in {time.time() - _t0:.1f}s",
                         {"version": v, "approved": result.get("approved", False),
                          "score": result.get("quality_score", 0), "elapsed_sec": round(time.time() - _t0, 2)})
    except Exception:
        pass

    return _json_tool_result(result)


# ──────────────────────────────────────────────
# Critic Stage
# ──────────────────────────────────────────────

def _critic_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "approved", "approve"}
    return bool(value)


@tool("run_critic", "Run Poker Strategy Critic on bot changes. Returns advisory score and strategic feedback; native TCP precommit remains the final strategy gate.", {"version": int, "source_v": int, "plan": list, "reviewer_feedback": str, "force_advance": bool})
async def run_critic(args):
    _t0 = time.time()
    v, source_v = _resolve_version_args(args)
    if v is None or source_v is None:
        return _json_tool_result({"error": "Missing version/source_v and no active pipeline checkpoint"})
    v = int(v)
    source_v = int(source_v)
    supplied_plan = args.get("plan", [])
    reviewer_feedback = args.get("reviewer_feedback", "")
    force_advance = bool(args.get("force_advance", False))

    _set_pipeline_status(f"Critic evaluating v{v}")

    ckpt = _matching_checkpoint(v, source_v)
    _critic_infra, _critic_infra_error = _owned_infrastructure_failure(
        ckpt,
        "run_critic",
    )
    if _critic_infra_error:
        return _state_blocked(_critic_infra_error, v, source_v, ckpt)
    _critic_exhausted = await _execute_exhausted_infrastructure_failure(
        v,
        source_v,
        owner_tool="run_critic",
    )
    if _critic_exhausted is not None:
        return _json_tool_result(_critic_exhausted)

    # Idempotency guard: skip when the advisory role already completed.
    _cached = _idempotency_check(
        v, source_v,
        stage_set=("critic_checked", "verified", "archived"),
        gate_name="critic",
        directive="Critic ALREADY PASSED. Call run_precommit_eval next.",
        cache_validator=lambda gate: _critic_gate_ok({"gate_results": {"critic": gate}}),
    )
    if _cached:
        return _cached

    if not _quality_gate_ok(ckpt) or not _review_gate_ok(ckpt):
        return _state_blocked(
            "run_critic requires passing quality gates and reviewer approval for the same version/source_v.",
            v,
            source_v,
            ckpt,
        )

    authoritative_plan = (
        ckpt.get("master_plan")
        if isinstance(ckpt, dict) and isinstance(ckpt.get("master_plan"), dict)
        else {"tasks": supplied_plan if isinstance(supplied_plan, list) else []}
    )
    if supplied_plan and supplied_plan != authoritative_plan.get("tasks"):
        log_system_event(
            "pipeline.critic_plan_argument_ignored",
            "warn",
            f"run_critic v{v} ignored a plan argument that differed from checkpoint authority",
            {"version": v, "source_v": source_v},
        )
    master_plan_str = json.dumps(authoritative_plan, indent=2, ensure_ascii=False)
    prev_critic = ckpt.get("gate_results", {}).get("critic", {}).get("prev_critic") if ckpt else None
    critic_prompt_source = PROJECT_ROOT / "web" / "core" / "prompts" / "critic_prompt.md"
    critic_prompt_identity = (
        critic_prompt_source.read_text(encoding="utf-8")
        if critic_prompt_source.exists()
        else "critic_prompt_missing"
    )
    critic_prompt_identity += "\n" + master_plan_str + "\n" + json.dumps(
        prev_critic or {}, sort_keys=True, ensure_ascii=False
    )
    _critic_attempt_key, _critic_infra_metadata = _llm_gate_infrastructure_identity(
        component="critic_llm",
        role="STRATEGY CRITIC",
        candidate_dir=get_bot_dir(v),
        source_dir=get_bot_dir(source_v),
        prompt_text=critic_prompt_identity,
        checkpoint=ckpt,
    )
    ui = _get_ui()
    data = await _run_critic(v, source_v, master_plan_str, ui, prev_critic_result=prev_critic)

    # No strategic verdict exists when the role call or its schema collapses.
    # Persist an infrastructure overlay instead of manufacturing score=0 debt.
    if not isinstance(data, dict) or data.get("llm_failed") or data.get("parse_failed"):
        issue = (
            str((data or {}).get("error") or (data or {}).get("feedback") or "critic output unavailable")
            if isinstance(data, dict)
            else f"critic_result_not_object:{type(data).__name__}"
        )
        infra_result = await _record_infrastructure_failure(
            v,
            source_v,
            owner_tool="run_critic",
            resume_stage="reviewed",
            component="critic_llm",
            code="critic_llm_unavailable",
            attempt_key=_critic_attempt_key,
            issues=[issue],
            max_attempts=3,
            metadata=_critic_infra_metadata,
            master_plan=authoritative_plan,
            reviewer_feedback=reviewer_feedback,
        )
        attempt = (infra_result.get("infra_failure") or {}).get("attempt")
        log_system_event(
            "pipeline.critic_infra_error",
            "error" if infra_result.get("action") == "abandon_generation" else "warn",
            f"Critic v{v} unavailable (infrastructure attempt {attempt or '?'}/3)",
            {"version": v, "source_v": source_v, "issue": issue[:500], **infra_result},
        )
        return _json_tool_result({
            **infra_result,
            "llm_failed": True,
            "approved": None,
            "score": None,
            "directive": (
                "Critic infrastructure retry exhausted; generation was safely abandoned."
                if infra_result.get("action") == "abandon_generation"
                else "Retry run_critic for the same candidate; do not run workers or Master."
            ),
            "logs": ui.get_output(),
        })

    if not isinstance(data, dict):
        data = {}
    score = data.get("score", 0)
    try:
        score_num = float(score)
    except (TypeError, ValueError):
        score_num = 0.0
    raw_approved = data.get("approved", score_num >= 6)
    advisory_approved = _critic_bool(raw_approved) and score_num >= 6
    # Successful schema-valid execution completes the role. The raw verdict is
    # retained as advice; it cannot replace the native-TCP statistical gate.
    approved = True
    force_advanced = bool(force_advance)
    gate = _gate_payload(
        v,
        source_v,
        approved,
        approved=approved,
        raw_approved=raw_approved,
        advisory_approved=advisory_approved,
        advisory_score=score_num,
        score=score_num,
        feedback=data.get("feedback", ""),
        strategic_assessment=data.get("strategic_assessment", ""),
        local_optima_warning=data.get("local_optima_warning", False),
        force_advanced=force_advanced,
    )

    current_attempt = (ckpt.get("generation_attempt", 0) or 0) if ckpt else 0
    next_attempt = current_attempt

    checkpoint_recorded = _record_gate(
        v,
        source_v,
        "critic",
        gate,
        stage="critic_checked",
        master_plan=authoritative_plan,
        reviewer_feedback=reviewer_feedback,
        generation_attempt=next_attempt,
        clear_infra_failure=_critic_infra is not None,
        infra_failure_owner="run_critic" if _critic_infra is not None else None,
        expected_infra_failure_digest=(
            infrastructure_failure_digest(_critic_infra)
            if _critic_infra is not None
            else None
        ),
    )
    guardian_diagnosis = None
    if not advisory_approved:
        _record_quality_failure(v, "critic", "Strategy Critic",
                                f"Advisory concern (score={score_num}): {data.get('feedback', '')[:2000]}",
                                local_optima_warning=data.get("local_optima_warning", False),
                                local_optima_reason=data.get("local_optima_reason"))
        # Meta-2: Trigger Regression Guardian on very low critic score.
        # Run synchronously so the diagnosis is visible to the Orchestrator.
        # _run_regression_guardian has a safe_default so it never throws.
        if score_num < 4:
            try:
                import audit_agents as _aa
                _c = _matching_checkpoint(v, source_v)
                _history = {
                    "score": score_num,
                    "feedback": data.get("feedback", "")[:500],
                    "strategic_assessment": data.get("strategic_assessment", "")[:500],
                    "master_plan": _c.get("master_plan", {}) if _c else {},
                    "gate_results": _c.get("gate_results", {}) if _c else {},
                }
                guardian_diagnosis = await _aa._run_regression_guardian(
                    v, source_v, _history,
                    f"Critic score {score_num} < 4: {data.get('feedback', '')[:200]}",
                    ui,
                )
            except Exception as e:
                _log.warning("Regression guardian dispatch failed for v%s: %s", v, e)

    try:
        # LOG GAP FIX (2026-06-30): enrich critic event with feedback/reasoning so
        # the reject rationale is visible in the event stream (not just worker_failures.jsonl).
        _critic_payload = {
            "version": v,
            "score": score_num,
            "approved": approved,
            "advisory_approved": advisory_approved,
            "generation_attempt": next_attempt,
        }
        if not advisory_approved:
            _critic_payload["feedback"] = str(data.get("feedback", ""))[:500] if isinstance(data, dict) else ""
            _critic_payload["local_optima_warning"] = data.get("local_optima_warning") if isinstance(data, dict) else None
            _critic_payload["strategic_assessment"] = str(data.get("strategic_assessment", ""))[:300] if isinstance(data, dict) else ""
        log_system_event(
            "pipeline.critic_advisory_completed",
            "success" if advisory_approved else "warn",
            f"Critic advisory completed for v{v} (score={score_num})",
            _critic_payload,
        )
    except Exception:
        pass

    # Extract Critic evidence and append to experience pool
    evidence = data.get("evidence") if isinstance(data, dict) else None
    if evidence:
        try:
            from tool_commit import _append_experience_updates
            ev_parts = []
            h2h_w = evidence.get("h2h_weaknesses", [])
            if h2h_w:
                ev_parts.append(f"H2H weaknesses: {', '.join(str(w) for w in h2h_w[:5])}")
            ep_refs = evidence.get("experience_pool_refs", [])
            if ep_refs:
                ev_parts.append(f"Experience pool refs: {', '.join(str(r) for r in ep_refs[:3])}")
            diff_refs = evidence.get("diff_refs", [])
            if diff_refs:
                ev_parts.append(f"Diff refs: {', '.join(str(r) for r in diff_refs[:3])}")
            if ev_parts:
                evidence_summary = "; ".join(ev_parts)
                _append_experience_updates(
                    version=v,
                    updates=[f"Critic evidence: {evidence_summary}"],
                    strategic_advice="",
                    generation_assessment="info",
                )
        except Exception:
            pass  # Non-critical: evidence write failure should not block pipeline

    # fix-8: check for fabricated replay citations in critic output
    critic_citation_errors = []
    try:
        from tool_planning import _check_citations, _load_replay_anchor_map
        if evidence:
            critic_texts = evidence.get("h2h_weaknesses", []) + evidence.get("diff_refs", [])
            critic_citation_errors = _check_citations(
                [str(t) for t in critic_texts], _load_replay_anchor_map()
            )
        if critic_citation_errors:
            log_system_event("fabricated_citation", "warn",
                             f"Critic cited {len(critic_citation_errors)} fabricated replay(s)",
                             {"version": v, "errors": critic_citation_errors})
    except Exception:
        pass  # Non-critical: citation check should not block pipeline

    result = {
        **data,
        "approved": approved,
        "raw_approved": raw_approved,
        "score": score_num,
        "advisory_score": score_num,
        "advisory_approved": advisory_approved,
        "citation_penalties": len(critic_citation_errors),
        "logs": ui.get_output(),
        "action": "proceed_to_precommit",
        "directive": (
            "Critic advisory completed. Call run_precommit_eval next regardless "
            "of advisory score; native TCP precommit is the final strategy gate."
        ),
        "reviewer_feedback": reviewer_feedback,
        "generation_attempt": next_attempt,
        "force_advanced": force_advanced,
        "checkpoint_recorded": checkpoint_recorded,
    }
    if guardian_diagnosis:
        result["regression_guardian"] = {
            "severity": guardian_diagnosis.get("severity", "minor"),
            "failure_stage": guardian_diagnosis.get("failure_stage", "unknown"),
            "recovery_recommendation": guardian_diagnosis.get("recovery_recommendation", ""),
            "diagnosis": guardian_diagnosis.get("diagnosis", ""),
            "root_cause": guardian_diagnosis.get("root_cause", ""),
            "confidence": guardian_diagnosis.get("confidence", "low"),
        }
        # fix-9: surface guardian_diagnosis to experience_pool for next-gen Master.
        _guardian_text = guardian_diagnosis.get("diagnosis", "")
        if _guardian_text:
            try:
                log_system_event("regression_guardian", "info",
                                 f"Critic score<4 guardian diagnosis: {_guardian_text[:200]}",
                                 {"version": v, "source_v": source_v, "score": score_num})
            except Exception:
                pass
            try:
                import evolution_infra as _ei
                _rd = _ei.RESULTS_DIR
                os.makedirs(_rd, exist_ok=True)
                _gf_path = _rd / "regression_guardian.jsonl"
                _entry = json.dumps({
                    "version": v, "source_v": source_v, "score": score_num,
                    "diagnosis": _guardian_text,
                    "root_cause": guardian_diagnosis.get("root_cause", ""),
                    "severity": guardian_diagnosis.get("severity", "minor"),
                    "recovery_recommendation": guardian_diagnosis.get("recovery_recommendation", ""),
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }, ensure_ascii=False)
                import fcntl as _fl
                with open(_gf_path, "a", encoding="utf-8") as _fh:
                    _fl.flock(_fh, _fl.LOCK_EX)
                    _fh.write(_entry + "\n")
                    _fl.flock(_fh, _fl.LOCK_UN)
            except Exception:
                pass
        result["fabricated_citations"] = critic_citation_errors
    try:
        log_system_event("pipeline.critic_done", "info",
                         f"Critic finished for v{v} in {time.time() - _t0:.1f}s",
                         {"version": v, "approved": approved, "score": score_num,
                          "elapsed_sec": round(time.time() - _t0, 2)})
    except Exception:
        pass
    return _json_tool_result(result)


# ──────────────────────────────────────────────
# Spot Check Stage
# ──────────────────────────────────────────────

@tool("run_spot_check", "Run spot check on changed functions: parse diff, generate scenarios, run bot, verify behavior.", {"parent_version": int, "current_version": int, "master_plan": dict})
async def run_spot_check(args):
    parent_version = args.get("parent_version")
    current_version = args.get("current_version")
    master_plan = args.get("master_plan", {})

    if parent_version is None or current_version is None:
        return _json_tool_result({"error": "Missing parent_version or current_version"})

    parent_dir = str(get_bot_dir(int(parent_version)))
    current_dir = str(get_bot_dir(int(current_version)))

    changed_functions = spot_analyzer.parse_diff(parent_dir, current_dir)

    bot_code = {}
    for change in changed_functions:
        fp = change.get("file")
        if fp and Path(fp).exists():
            bot_code[fp] = Path(fp).read_text()

    scenarios = spot_analyzer.generate_test_scenarios(changed_functions, bot_code)

    bot_main = Path(current_dir) / "main.py"
    actual_actions = []
    for scenario in scenarios:
        result = spot_analyzer.run_bot_scenario(str(bot_main), scenario)
        actual_actions.append(result)

    verification = spot_analyzer.verify_behavior(master_plan, scenarios, actual_actions)

    result = {
        "status": "success",
        "result": {
            "passed": verification.get("passed", False),
            "assessment": f"Spot check {verification.get('passed_count', 0)}/{verification.get('total', 0)} passed, confidence={verification.get('confidence', 'unknown')}",
            "details": verification,
            "changed_functions": changed_functions,
            "scenarios_count": len(scenarios),
        },
    }
    return _json_tool_result(result)
