"""Pipeline tools: quality gates, code preparation, review, and critic."""

import asyncio
import hashlib
import json
import os
import re
import time
from copy import deepcopy
from pathlib import Path

from bot_namespace import bot_name, bot_tag, strict_lineage_parent_versions
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
    _critic_result_to_preserve,
    _py_files_changed_between, _resolve_version_args, PROJECT_ROOT,
    _set_pipeline_status, read_pipeline_checkpoint,
    write_pipeline_checkpoint,
)
from system_log import log_system_event
from llm_failure import is_llm_infra_error, infra_payload
from llm_availability import LLMAvailabilityBlocked
from pipeline_schema import GateResult, ScoreCard
from gate_execution import GateExecution
from pipeline_state import next_tool_for_checkpoint
from pipeline_infrastructure import (
    build_infrastructure_failure,
    infrastructure_attempt_key,
    infrastructure_failure_digest,
)
from workflow_profiles import get_workflow_profile
from worker_boundary import (
    audit_strict_policy_artifact_delta_against_plan,
    hash_changed_files,
)
from national_position_contract import detect_position_semantics_errors
from blocking_runtime import run_blocking_isolated

import tool_gates_critic_review as _cr  # critic + review stage runner subsystem

import tool_gates_prepare
import tool_gates_native_smoke
import tool_gates_artifact_scope
import tool_gates_internals
import tool_gates_quality_projection


_REVIEW_SEMANTIC_MODES = {
    "fixed_blueprint_capability_audit": "fixed_blueprint_capability_audit_v1",
    "strategy_implementation": "strategy_implementation_v1",
}
_SELECTED_CAPABILITY_EVIDENCE_SCOPE = (
    "reachable_symbol_delta_plus_typed_capability_only;"
    "not_full_counterfactual_or_strength_proof"
)


_canonical_digest = tool_gates_internals._canonical_digest  # delegate: extracted to tool_gates_internals.py


def _review_semantic_contract(master_plan, quality_gate):
    """Delegate to tool_gates_critic_review."""
    return _cr._review_semantic_contract(master_plan, quality_gate)


def _quality_review_evidence_projection(quality_result):
    """Delegate to tool_gates_critic_review."""
    return _cr._quality_review_evidence_projection(quality_result)


def _render_reviewer_provider_prompt(inputs):
    """Delegate to tool_gates_critic_review."""
    return _cr._render_reviewer_provider_prompt(inputs)


async def _abandon_strict_gate_authority(
    checkpoint,
    *,
    gate_name,
    error,
):
    """Canonically abandon first-strict gate authority drift without retries."""

    from system_strict_bootstrap import abandon_rejected_blueprint

    validation_errors = list(
        getattr(error, "errors", ()) or (str(error),)
    )
    label = "REVIEW" if gate_name == "review" else "CRITIC"
    gate_payload = {
        "passed": False,
        "approved": False,
        "schema_valid": False,
        "terminal_control_failure": True,
        "failure_class": "control_plane",
        "validation_errors": validation_errors,
        "error": f"SYSTEM_STRICT_BOOTSTRAP_{label}_AUTHORITY_INVALID",
    }
    return await abandon_rejected_blueprint(
        checkpoint,
        reason=f"system_strict_bootstrap_{gate_name}_authority_invalid",
        result={
            "error": f"SYSTEM_STRICT_BOOTSTRAP_{label}_AUTHORITY_INVALID",
            "approved": False,
            "success": False,
            "failure_class": "control_plane",
            "validation_errors": validation_errors,
            "terminal_gate_name": gate_name,
            "terminal_reason_code": f"{gate_name}_authority_invalid",
            "terminal_gate_payload": gate_payload,
            "directive": (
                f"The first-strict {gate_name} authority context, prompt, or "
                "journal drifted. The generation was canonically abandoned; "
                "do not consume an LLM infrastructure retry."
            ),
        },
    )


try:
    from candidate_store import (
        append_candidate_event,
        candidate_observability_identity,
    )
except Exception:  # pragma: no cover - import fallback for unusual test paths
    append_candidate_event = None
    candidate_observability_identity = None


QUALITY_INFRA_MAX_ATTEMPTS = max(
    1, int(os.environ.get("POK_QUALITY_INFRA_MAX_ATTEMPTS", "3"))
)
QUALITY_INFRA_CONTRACT_VERSION = 2


_run_workflow_decision_tests = tool_gates_native_smoke._run_workflow_decision_tests  # delegate: extracted to tool_gates_native_smoke.py


def _env_enabled(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


_national_acceptance_not_run = tool_gates_native_smoke._national_acceptance_not_run  # delegate: extracted to tool_gates_native_smoke.py


_national_acceptance_executed = tool_gates_native_smoke._national_acceptance_executed  # delegate: extracted to tool_gates_native_smoke.py


_bind_quality_native_timing_plan = tool_gates_native_smoke._bind_quality_native_timing_plan  # delegate: extracted to tool_gates_native_smoke.py


_official_gate_enabled = tool_gates_native_smoke._official_gate_enabled  # delegate: extracted to tool_gates_native_smoke.py


_request_official_smoke_status = tool_gates_native_smoke._request_official_smoke_status  # delegate: extracted to tool_gates_native_smoke.py


_run_workflow_smoke_gate = tool_gates_native_smoke._run_workflow_smoke_gate  # delegate: extracted to tool_gates_native_smoke.py


_declared_scope_tasks_from_plan = tool_gates_artifact_scope._declared_scope_tasks_from_plan  # delegate: extracted to tool_gates_artifact_scope.py


_is_crossover_scope_checkpoint = tool_gates_artifact_scope._is_crossover_scope_checkpoint  # delegate: extracted to tool_gates_artifact_scope.py


_master_plan_with_crossover_scope = tool_gates_artifact_scope._master_plan_with_crossover_scope  # delegate: extracted to tool_gates_artifact_scope.py


_crossover_post_master_delta = tool_gates_artifact_scope._crossover_post_master_delta  # delegate: extracted to tool_gates_artifact_scope.py


_prepared_artifact_delta_files = tool_gates_artifact_scope._prepared_artifact_delta_files  # delegate: extracted to tool_gates_artifact_scope.py


_prepared_artifact_change_status = tool_gates_artifact_scope._prepared_artifact_change_status  # delegate: extracted to tool_gates_artifact_scope.py

_record_quality_failure = tool_gates_internals._record_quality_failure  # delegate: extracted to tool_gates_internals.py


_idempotency_check = tool_gates_internals._idempotency_check  # delegate: extracted to tool_gates_internals.py


_bot_code_fingerprint = tool_gates_internals._bot_code_fingerprint  # delegate: extracted to tool_gates_internals.py


_transient_task_context_errors = tool_gates_internals._transient_task_context_errors  # delegate: extracted to tool_gates_internals.py


_llm_gate_infrastructure_identity = tool_gates_internals._llm_gate_infrastructure_identity  # delegate: extracted to tool_gates_internals.py


_strict_review_infrastructure_harness_identity = tool_gates_internals._strict_review_infrastructure_harness_identity  # delegate: extracted to tool_gates_internals.py


# ──────────────────────────────────────────────
# Quality Gates
# ──────────────────────────────────────────────


_selected_proposal_quality_evidence = tool_gates_internals._selected_proposal_quality_evidence  # delegate: extracted to tool_gates_internals.py

_finalize_strict_blueprint_quality_rejection = tool_gates_internals._finalize_strict_blueprint_quality_rejection  # delegate: extracted to tool_gates_internals.py

_quality_source_dir = tool_gates_internals._quality_source_dir  # delegate: extracted to tool_gates_internals.py


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
    if getattr(workflow_profile, "national_execution_mode", None) != "native_tcp":
        return _json_tool_result({
            "error": "WORKFLOW_CONFIGURATION_ERROR",
            "message": "only national native_tcp quality evaluation is supported",
        })
    native_tcp_mode = True
    first_strict_control_receipt = None
    first_strict_control_path = None
    first_strict_control_required = False
    declared_first_strict = False
    protocol_bootstrap = (
        (active_ckpt.get("audit_context") or {}).get("protocol_bootstrap")
        if isinstance(active_ckpt, dict)
        else None
    )
    fresh_numeric_lineage = bool(
        isinstance(protocol_bootstrap, dict)
        and protocol_bootstrap.get("mode")
        == "fresh_national_policy_bootstrap"
        and protocol_bootstrap.get("source_artifact_inherited") is False
    )
    if native_tcp_mode and active_ckpt:
        try:
            from system_strict_bootstrap import is_declared_native_bootstrap

            declared_first_strict = is_declared_native_bootstrap(active_ckpt)
        except Exception:
            declared_first_strict = False
        if declared_first_strict:
            first_strict_control_required = True
            try:
                from first_strict_control import build_control_receipt

                first_strict_control_receipt = build_control_receipt(active_ckpt)
                first_strict_control_path = str(
                    (first_strict_control_receipt.get("control") or {}).get("path")
                    or ""
                )
            except Exception as exc:
                # Empty string is an explicit (invalid) token to smoke, so its
                # resolver fails closed instead of falling back to source_v142.
                first_strict_control_path = ""
                mark_quality_infrastructure(
                    "first_strict_control",
                    "quality_opponent",
                    f"{type(exc).__name__}: {str(exc)[:500]}",
                )
    candidate_observability = (
        candidate_observability_identity(v, source_v)
        if candidate_observability_identity is not None
        else {
            "candidate_id": bot_name(v),
            "parent_ids": [],
            "lineage_kind": "unavailable",
        }
    )
    candidate_id = str(candidate_observability["candidate_id"])
    candidate_parent_ids = list(candidate_observability["parent_ids"])
    candidate_lineage_metrics = {
        key: candidate_observability[key]
        for key in (
            "lineage_kind",
            "numeric_high_water_version",
            "source_artifact_inherited",
        )
        if key in candidate_observability
    }
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
                parent_ids=candidate_parent_ids,
                metrics=candidate_lineage_metrics,
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
    source_dir = _quality_source_dir(
        source_v,
        numeric_lineage_only=fresh_numeric_lineage,
    )
    if source_dir is not None:
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
            lineage_parents = strict_lineage_parent_versions(
                int(v),
                int(source_v) if source_v is not None else None,
                (_quality_ckpt_for_scope or {}).get("parent2_v"),
            )
            _scope_audit = audit_strict_policy_artifact_delta_against_plan(
                scope_changed_files,
                _plan_tasks,
                candidate_dir=bot_dir,
                version=int(v),
                parent_versions=lineage_parents,
                identity_refresh_receipt=(
                    ((_quality_ckpt_for_scope or {}).get("audit_context") or {})
                    .get("strict_policy_identity_refresh")
                ),
                durable_worker_output=(
                    ((_quality_ckpt_for_scope or {}).get("audit_context") or {})
                    .get("durable_worker_output")
                ),
                require_identity_refresh_receipt=True,
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
        # Delegate to the extracted module-level helper (closure deps captured here).
        return _quality_cache_current_impl(
            gate,
            bot_dir=bot_dir,
            code_fingerprint=code_fingerprint,
            native_tcp_mode=native_tcp_mode,
            runtime_contract_ledger_digest=runtime_contract_ledger_digest,
            source_v=source_v,
            v=v,
            workflow_profile=workflow_profile,
            _master_plan_for_scope=_master_plan_for_scope,
        )

    # Idempotency guard: skip if quality gates already passed for this version
    _cached = _idempotency_check(
        v, source_v,
        stage_set=("quality_passed", "reviewed", "critic_checked", "verified", "archived"),
        gate_name="quality",
        approval_key="all_passed",
        directive="Quality gates ALREADY PASSED. Call run_review next.",
        cache_validator=lambda _checkpoint, gate: _quality_cache_current(gate),
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
    protected_contract_errors = []
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
        "schema_version": 3,
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

            if source_dir is None and not declared_first_strict:
                raise RuntimeError("native candidate has no source directory for architecture transition")
            expected_architecture_policy = (
                _master_plan_for_scope.get("architecture_policy")
                if isinstance(_master_plan_for_scope, dict)
                else None
            )
            transition_kwargs = {
                "expected_policy": expected_architecture_policy,
                "runtime_contract_ledger": (
                    _master_plan_for_scope.get("runtime_contract_ledger")
                    if isinstance(_master_plan_for_scope, dict)
                    else None
                ),
            }
            if declared_first_strict:
                transition_kwargs["lineage_source_bot"] = bot_name(source_v)
            national_architecture_transition = evaluate_architecture_transition(
                source_dir,
                bot_dir,
                **transition_kwargs,
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
                "schema_version": 3,
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
    selected_proposal_quality_evidence = _selected_proposal_quality_evidence(
        _master_plan_for_scope,
        national_architecture_transition,
        candidate_dir=bot_dir,
    )
    selected_proposal_quality_ok = bool(
        selected_proposal_quality_evidence.get("ok")
    )
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
    for check_id in (
        national_architecture_transition.get("selected_dynamic_failures")
        or national_architecture_transition.get("blocking_focus_checks")
        or []
    ):
        check = (national_capability_contract.get("checks_by_id") or {}).get(check_id, {})
        national_capability_blockers.append({
            "check_id": f"runtime_contract_primary:{check_id}",
            "name": f"runtime_contract_primary:{check_id}",
            "guidance": check.get("guidance") or (
                "Close the frozen RuntimeContract primary check " f"{check_id}."
            ),
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
    # New policy helpers must be referenced by the typed policy dispatch.
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
        opponent_token=None,
        self_play=first_strict_control_required,
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
    (
        national_acceptance_ok,
        national_acceptance_errors,
        national_acceptance_payload,
    ) = _national_acceptance_not_run("national_acceptance_not_started")
    national_acceptance_enabled = _env_enabled(
        "POK_NATIONAL_ACCEPTANCE_GATE",
        "1",
    )
    national_acceptance_contract_error = ""
    # Strict 70-hand acceptance is a fixed system contract.  Environment
    # overrides would create an unrecorded timeout identity between quality,
    # heartbeat, replay and precommit, so they are intentionally ignored.
    strict_native_profile = get_workflow_profile("national_native")
    national_acceptance_hands = int(strict_native_profile.national_acceptance_hands)
    national_acceptance_timeout_sec = float(
        strict_native_profile.national_acceptance_timeout_sec
    )
    expected_acceptance_hands = national_acceptance_hands
    if national_acceptance_timeout_sec <= 0.0:
        national_acceptance_contract_error = "national_acceptance_timeout_not_positive"
    national_acceptance_timing_plan = None
    national_acceptance_progress_callback = None
    if not national_acceptance_contract_error:
        try:
            from national_native import build_native_match_timing_plan

            national_acceptance_timing_plan = build_native_match_timing_plan(
                hands=national_acceptance_hands,
                requested_timeout_sec=national_acceptance_timeout_sec,
            )
            active_ckpt = _bind_quality_native_timing_plan(
                active_ckpt,
                national_acceptance_timing_plan,
            )
            from pipeline_state import make_native_match_heartbeat_reporter

            national_acceptance_progress_callback = (
                make_native_match_heartbeat_reporter(
                    active_ckpt,
                    owner_tool="run_quality_gates",
                )
            )
        except Exception as exc:
            national_acceptance_contract_error = (
                "national_acceptance_timing_plan_invalid:"
                f"{type(exc).__name__}"
            )
    if (
        not national_acceptance_contract_error
        and (
            expected_acceptance_hands != 70
            or national_acceptance_hands != expected_acceptance_hands
            or strict_native_profile.national_acceptance_hard is not True
        )
    ):
        national_acceptance_contract_error = (
            "national_acceptance_strict_contract_mismatch:"
            f"hands={national_acceptance_hands}:"
            f"expected={expected_acceptance_hands}:"
            f"hard={strict_native_profile.national_acceptance_hard}"
        )
    if not national_acceptance_enabled:
        (
            national_acceptance_ok,
            national_acceptance_errors,
            national_acceptance_payload,
        ) = _national_acceptance_not_run(
            "national_acceptance_disabled_in_strict_native_mode"
        )
    elif national_acceptance_contract_error:
        (
            national_acceptance_ok,
            national_acceptance_errors,
            national_acceptance_payload,
        ) = _national_acceptance_not_run(national_acceptance_contract_error)
    elif source_v is None:
        (
            national_acceptance_ok,
            national_acceptance_errors,
            national_acceptance_payload,
        ) = _national_acceptance_not_run(
            "national_acceptance_source_identity_missing"
        )
    else:
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
            and (bot_dir / "national_bot.py").exists()
            and _bot_under_project_bots
        )
        if can_run_national_acceptance:
            try:
                if first_strict_control_required:
                    # The first strict artifact has no published strict
                    # opponent.  A complete 70-hand candidate self-play is a
                    # compliance-only acceptance sample (zero strength), so
                    # v143 does not bypass the hard native gate.
                    from national_native import run_native_tcp_smoke

                    _acceptance_report = await run_native_tcp_smoke(
                        bot_dir,
                        source_v=source_v,
                        self_play=True,
                        hands=national_acceptance_hands,
                        timeout_sec=national_acceptance_timeout_sec,
                        timing_plan=national_acceptance_timing_plan,
                        progress_callback=national_acceptance_progress_callback,
                    )
                    _acceptance_report = {
                        **_acceptance_report,
                        "acceptance_kind": "first_strict_self_play_compliance",
                        "strength_weight": 0,
                        "summary": {
                            "self_play": True,
                            "hands": national_acceptance_hands,
                            "passed_compliance": bool(
                                _acceptance_report.get("passed")
                            ),
                        },
                    }
                else:
                    from national_native import run_native_acceptance_for_candidate

                    _acceptance = await run_native_acceptance_for_candidate(
                        bot_dir,
                        source_v=source_v,
                        hands=national_acceptance_hands,
                        max_opponents=2,
                        timeout_sec=national_acceptance_timeout_sec,
                        timing_plan=national_acceptance_timing_plan,
                        progress_callback=national_acceptance_progress_callback,
                    )
                    _acceptance_report = _acceptance.model_dump()
                (
                    national_acceptance_ok,
                    national_acceptance_errors,
                    national_acceptance_payload,
                ) = _national_acceptance_executed(
                    _acceptance_report,
                    expected_hands=national_acceptance_hands,
                    expected_timing_plan=national_acceptance_timing_plan,
                )
                if not national_acceptance_payload.get("conclusive"):
                    mark_quality_infrastructure(
                        "national_acceptance_harness",
                        "national_acceptance",
                        "; ".join(
                            str(item) for item in national_acceptance_errors[:5]
                        )
                        or "national acceptance infrastructure failure",
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
                        "execution_mode": "native_tcp",
                        "hands": national_acceptance_hands,
                        "timeout_sec": national_acceptance_timeout_sec,
                        "opponents": national_acceptance_payload.get(
                            "opponents",
                            [],
                        ),
                        "issues": national_acceptance_errors,
                        "summary": national_acceptance_payload.get(
                            "summary",
                            {},
                        ),
                        "executed": True,
                        "conclusive": national_acceptance_payload.get(
                            "conclusive"
                        ),
                    },
                )
            except Exception as e:
                national_acceptance_ok = False
                national_acceptance_errors = [f"national_acceptance_exception: {type(e).__name__}: {str(e)[:200]}"]
                national_acceptance_payload = {
                    "executed": True,
                    "skipped": False,
                    "passed": False,
                    "conclusive": False,
                    "outcome": "infrastructure_failure",
                    "error": national_acceptance_errors[0],
                    "issues": list(national_acceptance_errors),
                }
                mark_quality_infrastructure(
                    "national_acceptance_harness",
                    "national_acceptance",
                    national_acceptance_errors[0],
                )
                _log.warning("national acceptance gate failed to run for v%s: %s", v, e)
        else:
            (
                national_acceptance_ok,
                national_acceptance_errors,
                national_acceptance_payload,
            ) = _national_acceptance_not_run(
                "national_acceptance_skipped_due_to_failed_prerequisites"
            )

    if national_acceptance_payload.get("skipped"):
        log_system_event(
            "pipeline.national_acceptance_skipped",
            "warn",
            f"National native TCP acceptance was not executed for v{v}; "
            "the hard quality gate remains failed.",
            {
                "version": v,
                "source_v": source_v,
                "reason": national_acceptance_payload.get("reason"),
                "executed": False,
                "passed": False,
                "conclusive": False,
            },
        )

    official_smoke_ok = True
    official_smoke_errors: list[str] = []
    official_smoke_payload: dict = {}
    official_smoke_blocking = False
    official_smoke_inconclusive = False
    official_smoke_classification = "not_run"
    official_smoke_mode = os.environ.get("POK_OFFICIAL_SMOKE_GATE", "1").strip().lower()
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
                official_smoke_payload = await _request_official_smoke_status(bot_dir)
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
                            "mode": "smoke",
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
        "llm_status": (
            "disabled_national_native"
            if native_tcp_mode
            else "not_run"
        ),
        "llm_timeout_sec": 0,
        "llm_enabled": False,
        "fixture_protocol": (
            "official_raw_tcp_transcript_v1"
            if native_tcp_mode
            else "archived_local_fixture"
        ),
        "external_scenario_sidecars_loaded": False,
    }
    heuristic_scenarios = []
    existing_ids = []
    dynamic_scenarios = []
    _all_dynamic = []
    dynamic_test_meta["combined_count"] = len(_all_dynamic)

    # Candidate policy and system reducer assertions run only over raw TCP.
    try:
        decision_detail, decision_meta = _run_workflow_decision_tests(
            bot_dir,
            native_tcp_mode=native_tcp_mode,
            extra_scenarios=_all_dynamic,
        )
        dynamic_test_meta.update(decision_meta)
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
    required_host_fixture_ids = {
        "runtime_postflop_first_pass_maps_to_check",
        "runtime_postflop_facing_check_pass_maps_to_call",
    }
    observed_host_fixture_ids = {
        str(item.get("id") or "")
        for item in (decision_detail.get("scenarios") or [])
        if isinstance(item, dict)
    }
    decision_fixture_contract_ok = bool(
        native_tcp_mode
        and decision_detail.get("schema_version") == 2
        and decision_detail.get("protocol") == "official_raw_tcp_transcript_v1"
        and required_host_fixture_ids.issubset(observed_host_fixture_ids)
    )
    if not decision_fixture_contract_ok:
        critical_ok = False
    try:
        total_lines, oversized = check_code_size(bot_dir, source_dir=source_dir)
    except Exception as exc:
        total_lines, oversized = 0, []
        mark_quality_infrastructure(
            "code_size_validator",
            "code_size",
            f"{type(exc).__name__}: {str(exc)[:300]}",
        )
    decision_ok = (
        decision_rate >= 0.7
        and critical_ok
        and decision_total > 0
        and decision_fixture_contract_ok
    )

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
        and reachability_ok
        and position_semantics_ok
        and national_capability_ok
        and runtime_contract_identity_ok
        and selected_proposal_quality_ok
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
        source_fingerprint = (
            hashlib.sha256(
                f"numeric-high-water-lineage-only:v{int(source_v)}".encode(
                    "ascii"
                )
            ).hexdigest()
            if fresh_numeric_lineage and source_v is not None
            else _bot_code_fingerprint(source_dir)
            if source_dir
            else ""
        )
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
                "national_execution_mode": "native_tcp",
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
        "national_execution_mode": "native_tcp",
        "national_native_contract_ok": len(native_contract_errors) == 0,
        "national_native_contract_errors": native_contract_errors[:5] if native_contract_errors else [],
        "national_capability_contract_ok": national_capability_ok,
        "national_capability_contract_required": national_capability_required,
        "national_capability_contract": national_capability_contract,
        "national_architecture_transition": national_architecture_transition,
        "runtime_contract_identity_ok": runtime_contract_identity_ok,
        "runtime_contract_identity_errors": runtime_contract_identity_errors[:10],
        "runtime_contract_ledger_digest": runtime_contract_ledger_digest,
        "selected_proposal_quality_evidence": selected_proposal_quality_evidence,
        "selected_proposal_quality_ok": selected_proposal_quality_ok,
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
        "native_runtime_template_identity": (
            national_capability_contract.get("dynamic_runtime_probe") or {}
        ).get("native_runtime_template_identity"),
        "native_runtime_template_digest": (
            national_capability_contract.get("dynamic_runtime_probe") or {}
        ).get("native_runtime_template_digest"),
        "runtime_probe_managed_isolation_digest": (
            national_capability_contract.get("dynamic_runtime_probe") or {}
        ).get("managed_isolation_digest"),
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
        "national_acceptance_executed": (
            national_acceptance_payload.get("executed") is True
        ),
        "national_acceptance_conclusive": (
            national_acceptance_payload.get("conclusive") is True
        ),
        "national_acceptance_errors": national_acceptance_errors[:5] if national_acceptance_errors else [],
        "national_acceptance": national_acceptance_payload,
        "first_strict_control_receipt": first_strict_control_receipt,
        "official_smoke_ok": official_smoke_ok,
        "official_smoke_errors": official_smoke_errors[:5] if official_smoke_errors else [],
        "official_smoke": official_smoke_payload,
        "official_smoke_blocking": official_smoke_blocking,
        "official_smoke_inconclusive": official_smoke_inconclusive,
        "official_smoke_classification": official_smoke_classification,
        "decision_pass_rate": round(decision_rate, 2),
        "decision_ok": decision_ok,
        "decision_fixture_contract_ok": decision_fixture_contract_ok,
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
        "all_passed": all_passed,
        # A3/fix-3: TRUE-SHADOW is BLOCKING (added to compile_errors above);
        # "review" level warnings remain advisory. Reviewer/critic/orchestrator
        # can read these to distinguish blocking vs advisory.
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
    failed_gates_detail = _build_failed_gates_detail(
        code_changed=code_changed,
        compile_errors=compile_errors,
        decision_ok=decision_ok,
        decision_rate=decision_rate,
        declared_scope_errors=declared_scope_errors,
        declared_scope_ok=declared_scope_ok,
        embedded_selftest_errors=embedded_selftest_errors,
        import_errors=import_errors,
        national_acceptance_ok=national_acceptance_ok,
        national_capability_blockers=national_capability_blockers,
        national_capability_ok=national_capability_ok,
        national_protocol_errors=national_protocol_errors,
        native_contract_errors=native_contract_errors,
        official_smoke_blocking=official_smoke_blocking,
        official_smoke_ok=official_smoke_ok,
        position_semantics_errors=position_semantics_errors,
        position_semantics_ok=position_semantics_ok,
        post_master_delta_ok=post_master_delta_ok,
        protected_contract_errors=protected_contract_errors,
        quality_infrastructure=quality_infrastructure,
        reachability_ok=reachability_ok,
        reachability_warnings=reachability_warnings,
        runtime_contract_identity_errors=runtime_contract_identity_errors,
        runtime_contract_identity_ok=runtime_contract_identity_ok,
        selected_proposal_quality_evidence=selected_proposal_quality_evidence,
        selected_proposal_quality_ok=selected_proposal_quality_ok,
        v=v,
    )
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
        **_quality_review_evidence_projection(result),
        "quality_infrastructure": result["quality_infrastructure"],
        "runtime_probe_schema_version": result["runtime_probe_schema_version"],
        "runtime_probe_orchestrator_version": result["runtime_probe_orchestrator_version"],
        "runtime_probe_scenario_digest": result["runtime_probe_scenario_digest"],
        "runtime_probe_limits_digest": result["runtime_probe_limits_digest"],
        "runtime_probe_identity_digest": result["runtime_probe_identity_digest"],
        "native_runtime_template_identity": result[
            "native_runtime_template_identity"
        ],
        "native_runtime_template_digest": result[
            "native_runtime_template_digest"
        ],
        "runtime_probe_managed_isolation_digest": result[
            "runtime_probe_managed_isolation_digest"
        ],
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
        "national_acceptance_executed": result[
            "national_acceptance_executed"
        ],
        "national_acceptance_conclusive": result[
            "national_acceptance_conclusive"
        ],
        "national_acceptance_errors": result["national_acceptance_errors"],
        "national_acceptance": national_acceptance_payload,
        "first_strict_control_receipt": result.get(
            "first_strict_control_receipt"
        ),
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
        "reachability_ok": reachability_ok,
        "reachability_warnings": reachability_warnings[:6],
        "position_semantics_ok": position_semantics_ok,
        "position_semantics_errors": position_semantics_errors[:10],
        "code_fingerprint": code_fingerprint,
        "critical_failures": critical_failures[:3],
        "decision_skill_layers": decision_skill_layers,
    }
    scorecard = _build_quality_scorecard(
        candidate_gate_checks_passed=candidate_gate_checks_passed,
        code_changed=code_changed,
        code_fingerprint=code_fingerprint,
        compile_errors=compile_errors,
        decision_detail=decision_detail,
        decision_ok=decision_ok,
        decision_rate=decision_rate,
        decision_skill_layers=decision_skill_layers,
        decision_total=decision_total,
        declared_scope_errors=declared_scope_errors,
        declared_scope_metrics=declared_scope_metrics,
        declared_scope_ok=declared_scope_ok,
        embedded_selftest_errors=embedded_selftest_errors,
        import_errors=import_errors,
        issue=issue,
        national_acceptance_errors=national_acceptance_errors,
        national_acceptance_ok=national_acceptance_ok,
        national_acceptance_payload=national_acceptance_payload,
        national_architecture_transition=national_architecture_transition,
        national_capability_blockers=national_capability_blockers,
        national_capability_contract=national_capability_contract,
        national_capability_ok=national_capability_ok,
        national_capability_required=national_capability_required,
        national_protocol_errors=national_protocol_errors,
        native_contract_errors=native_contract_errors,
        native_tcp_mode=native_tcp_mode,
        official_local_status=official_local_status,
        official_smoke_blocking=official_smoke_blocking,
        official_smoke_classification=official_smoke_classification,
        official_smoke_errors=official_smoke_errors,
        official_smoke_inconclusive=official_smoke_inconclusive,
        official_smoke_ok=official_smoke_ok,
        official_smoke_payload=official_smoke_payload,
        position_semantics_errors=position_semantics_errors,
        position_semantics_ok=position_semantics_ok,
        post_master_changed_files=post_master_changed_files,
        post_master_delta_ok=post_master_delta_ok,
        post_master_delta_required=post_master_delta_required,
        prepared_artifact_hash=prepared_artifact_hash,
        protected_contract_errors=protected_contract_errors,
        quality_infra_issues=quality_infra_issues,
        quality_infrastructure=quality_infrastructure,
        reachability_ok=reachability_ok,
        reachability_warnings=reachability_warnings,
        runtime_contract_identity_errors=runtime_contract_identity_errors,
        runtime_contract_identity_ok=runtime_contract_identity_ok,
        runtime_contract_ledger_digest=runtime_contract_ledger_digest,
        source_python_changed=source_python_changed,
    )
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
                parent_ids=candidate_parent_ids,
                changed_files=post_master_changed_files,
                skill_layers=declared_skill_layers,
                diff_hash=diff_hash,
                gate="quality",
                scorecard=scorecard,
                gate_results=scorecard.gates,
                metrics={
                    **candidate_lineage_metrics,
                    "all_passed": all_passed,
                    "decision_pass_rate": round(decision_rate, 4),
                    "decision_skill_layers": decision_skill_layers,
                    "national_acceptance_ok": national_acceptance_ok,
                    "national_acceptance_executed": (
                        national_acceptance_payload.get("executed") is True
                    ),
                    "national_acceptance_conclusive": (
                        national_acceptance_payload.get("conclusive") is True
                    ),
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

    result = await _finalize_strict_blueprint_quality_rejection(
        required=first_strict_control_required,
        infrastructure_active=quality_infrastructure["active"],
        all_passed=all_passed,
        checkpoint=_matching_checkpoint(v, source_v) or active_ckpt,
        result=result,
    )

    if (
        quality_infrastructure["active"]
        and quality_infrastructure["exhausted"]
        and result.get("checkpoint_recorded")
    ):
        from tool_bot_management import (
            _do_abandon_generation,
            expected_abandon_identity,
        )

        abandon_checkpoint = _matching_checkpoint(v, source_v)
        abandon_result = await _do_abandon_generation(
            reason=f"infrastructure_exhausted:{quality_infrastructure.get('component')}",
            **expected_abandon_identity(abandon_checkpoint),
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
    """Delegate to tool_gates_prepare; @tool wrapper stays on the parent so the
    decorated object (with .handler) and the orchestrator route resolve here."""
    return await tool_gates_prepare.prepare_next_gen(args)


# ──────────────────────────────────────────────
# Review Stage
# ──────────────────────────────────────────────

@tool("run_review", "Run the mandatory schema-valid Lead Code Reviewer. The exact first strict migration additionally binds its real LLM result to a system content-chain receipt.", {"version": int, "source_v": int, "plan": list})
async def run_review(args):
    """Delegate to tool_gates_critic_review."""
    return await _cr.run_review(args)


# ──────────────────────────────────────────────
# Critic Stage
# ──────────────────────────────────────────────

def _critic_bool(value) -> bool:
    """Delegate to tool_gates_critic_review."""
    return _cr._critic_bool(value)


@tool("run_critic", "Run the mandatory schema-valid advisory Poker Strategy Critic. Its score is advisory; successful execution is required and native TCP precommit remains the strategy gate.", {"version": int, "source_v": int, "plan": list, "reviewer_feedback": str, "force_advance": bool})
async def run_critic(args):
    """Delegate to tool_gates_critic_review."""
    return await _cr.run_critic(args)
