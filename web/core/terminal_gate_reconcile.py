"""Operator reconciliation for a completed strict Reviewer rejection.

This path exists for the narrow crash window where the provider effect is
durably completed but old code attempted cleanup before accepting/projecting
the role result.  It never dispatches a model and never reconciles approval.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any


class TerminalGateReconcileError(RuntimeError):
    pass


_HEX = frozenset("0123456789abcdef")
_LEGACY_TERMINAL_INPUT_REVISION = 8
_LEGACY_TERMINAL_PROJECTED_REVISION = 9
_TERMINAL_MIGRATION_KEYS = frozenset({
    "schema_version",
    "kind",
    "disposition",
    "semantic_upgrade_status",
    "recorded_semantic_inputs_digest",
    "current_semantic_inputs_digest",
    "legacy_projection_digest",
    "current_review_semantic_contract_digest",
    "legacy_quality_gate_digest",
    "recorded_renderer_contract_digest",
    "recorded_renderer_static_identity_digest",
    "recorded_producer_file_sha256",
    "recorded_producer_function_sha256",
    "recorded_template_digests_digest",
    "migration_digest",
})
_MIGRATION_REQUIRED_DIGESTS = (
    "recorded_semantic_inputs_digest",
    "current_semantic_inputs_digest",
    "legacy_projection_digest",
    "recorded_renderer_contract_digest",
    "recorded_renderer_static_identity_digest",
    "recorded_producer_file_sha256",
    "recorded_producer_function_sha256",
    "recorded_template_digests_digest",
)
_OPERATOR_REVIEW_GATE_KEYS = frozenset({
    "version",
    "source_v",
    "passed",
    "approved",
    "llm_invoked",
    "reviewer_llm_executed",
    "schema_valid",
    "quality_score",
    "feedback",
    "change_summary",
    "risk_areas",
    "llm_role_result",
    "llm_authority_receipt",
    "llm_execution_evidence",
    "terminal_authority_context_binding",
    "operator_reconciled_completed_effect",
    "terminal_semantic_migration",
})


def _plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in _HEX for char in value)
    )


def _migration_errors(
    migration: Any,
    *,
    quality_gate: Any,
) -> list[str]:
    from strict_authority_workflow import (
        LEGACY_REVIEW_TERMINAL_MIGRATION_KIND,
    )
    from workflow_kernel import content_digest

    if not isinstance(migration, dict):
        return ["terminal_review_abandon_migration_missing"]
    errors: list[str] = []
    if set(migration) != _TERMINAL_MIGRATION_KEYS:
        errors.append("terminal_review_abandon_migration_fields_invalid")
    if migration.get("schema_version") != 1:
        errors.append("terminal_review_abandon_migration_schema_invalid")
    if migration.get("kind") != LEGACY_REVIEW_TERMINAL_MIGRATION_KIND:
        errors.append("terminal_review_abandon_migration_kind_invalid")
    if migration.get("disposition") != "terminal_rejection_only":
        errors.append("terminal_review_abandon_migration_disposition_invalid")
    if any(
        not _valid_digest(migration.get(field))
        for field in _MIGRATION_REQUIRED_DIGESTS
    ):
        errors.append("terminal_review_abandon_migration_identity_invalid")
    upgrade_status = migration.get("semantic_upgrade_status")
    if upgrade_status == "current_review_contract_available":
        if not _valid_digest(
            migration.get("current_review_semantic_contract_digest")
        ) or migration.get("legacy_quality_gate_digest") is not None:
            errors.append("terminal_review_abandon_migration_mode_invalid")
    elif upgrade_status == "unavailable_from_legacy_quality_gate":
        if (
            migration.get("current_review_semantic_contract_digest") is not None
            or not _valid_digest(migration.get("legacy_quality_gate_digest"))
        ):
            errors.append("terminal_review_abandon_migration_mode_invalid")
        if (
            migration.get("recorded_semantic_inputs_digest")
            != migration.get("current_semantic_inputs_digest")
            or migration.get("recorded_semantic_inputs_digest")
            != migration.get("legacy_projection_digest")
            or not isinstance(quality_gate, dict)
            or migration.get("legacy_quality_gate_digest")
            != content_digest(quality_gate)
        ):
            errors.append("terminal_review_abandon_migration_projection_invalid")
    else:
        errors.append("terminal_review_abandon_migration_mode_invalid")
    subject = {
        key: deepcopy(value)
        for key, value in migration.items()
        if key != "migration_digest"
    }
    if (
        not _valid_digest(migration.get("migration_digest"))
        or migration.get("migration_digest") != content_digest(subject)
    ):
        errors.append("terminal_review_abandon_migration_digest_invalid")
    return list(dict.fromkeys(errors))


def _inspect_projected_terminal_review_abandon(
    checkpoint: dict[str, Any],
) -> dict[str, Any]:
    """Validate the exact post-projection crash state without journal writes."""

    from bot_artifact import hash_path
    from evolution_core import get_bot_dir
    from gate_outcome import (
        TERMINAL_GATE_DISPOSITION,
        TERMINAL_GATE_OUTCOME_KIND,
        TERMINAL_GATE_OUTCOME_SCHEMA_VERSION,
        validate_terminal_gate_outcome,
    )
    from pipeline_state import route_policy
    from system_strict_bootstrap import is_declared_native_bootstrap
    from workflow_kernel import content_digest

    errors: list[str] = []
    if checkpoint.get("stage") != "review_rejected":
        errors.append("terminal_review_abandon_stage_invalid")
    if (
        not _plain_int(checkpoint.get("checkpoint_revision"))
        or checkpoint.get("checkpoint_revision")
        != _LEGACY_TERMINAL_PROJECTED_REVISION
    ):
        errors.append("terminal_review_abandon_revision_invalid")
    if not is_declared_native_bootstrap(checkpoint):
        errors.append("terminal_review_abandon_bootstrap_identity_invalid")
    for field in ("next_v", "source_v"):
        if not _plain_int(checkpoint.get(field)):
            errors.append(f"terminal_review_abandon_{field}_invalid")
    workflow_run_id = str(
        checkpoint.get("workflow_run_id") or checkpoint.get("run_id") or ""
    )
    if not workflow_run_id:
        errors.append("terminal_review_abandon_workflow_run_id_missing")

    gates = checkpoint.get("gate_results")
    gate = gates.get("review") if isinstance(gates, dict) else None
    if not isinstance(gates, dict) or set(gates) != {"quality", "review"}:
        errors.append("terminal_review_abandon_gate_set_invalid")
    if not isinstance(gate, dict):
        errors.append("terminal_review_abandon_gate_missing")
        gate = {}
    gate_keys = frozenset(gate)
    if gate_keys != _OPERATOR_REVIEW_GATE_KEYS:
        errors.append("terminal_review_abandon_gate_fields_invalid")
    role_result = gate.get("llm_role_result")
    if (
        gate.get("passed") is not False
        or gate.get("approved") is not False
        or gate.get("llm_invoked") is not True
        or gate.get("reviewer_llm_executed") is not True
        or gate.get("schema_valid") is not True
        or not isinstance(role_result, dict)
        or role_result.get("approved") is not False
        or gate.get("operator_reconciled_completed_effect") is not True
    ):
        errors.append("terminal_review_abandon_gate_semantics_invalid")
    # The projected v52 gate predates a persisted provider flag.  Only that
    # exact producer shape is accepted; any added provider field is drift.
    if "provider_dispatch_required" in gate:
        errors.append("terminal_review_abandon_provider_dispatch_invalid")
    if gate.get("version") != checkpoint.get("next_v") or gate.get(
        "source_v"
    ) != checkpoint.get("source_v"):
        errors.append("terminal_review_abandon_gate_identity_invalid")
    migration = gate.get("terminal_semantic_migration")
    quality_gate = gates.get("quality") if isinstance(gates, dict) else None
    errors.extend(_migration_errors(migration, quality_gate=quality_gate))

    outcome = checkpoint.get("terminal_gate_outcome")
    if not isinstance(outcome, dict):
        errors.append("terminal_review_abandon_outcome_missing")
        outcome = {}
    expected_outcome_fields = {
        "schema_version": TERMINAL_GATE_OUTCOME_SCHEMA_VERSION,
        "kind": TERMINAL_GATE_OUTCOME_KIND,
        "disposition": TERMINAL_GATE_DISPOSITION,
        "gate_name": "review",
        "terminal_stage": "review_rejected",
        "reason_code": "review_rejected",
        "failure_class": "strategy_review",
        "workflow_run_id": workflow_run_id,
        "next_v": checkpoint.get("next_v"),
        "source_v": checkpoint.get("source_v"),
        "parent2_v": checkpoint.get("parent2_v"),
        "evaluation_epoch": str(checkpoint.get("evaluation_epoch") or ""),
        "input_checkpoint_stage": "quality_passed",
        "input_checkpoint_revision": _LEGACY_TERMINAL_INPUT_REVISION,
        "projected_checkpoint_revision": _LEGACY_TERMINAL_PROJECTED_REVISION,
    }
    for field, expected in expected_outcome_fields.items():
        if outcome.get(field) != expected:
            errors.append(f"terminal_review_abandon_outcome_{field}_invalid")
    if outcome.get("gate_payload_digest") != content_digest(gate):
        errors.append("terminal_review_abandon_gate_digest_invalid")
    outcome_subject = {
        key: deepcopy(value)
        for key, value in outcome.items()
        if key != "receipt_digest"
    }
    if (
        not _valid_digest(outcome.get("receipt_digest"))
        or outcome.get("receipt_digest") != content_digest(outcome_subject)
    ):
        errors.append("terminal_review_abandon_outcome_digest_invalid")

    candidate_dir = None
    candidate_hash = None
    if _plain_int(checkpoint.get("next_v")):
        try:
            candidate_dir = Path(get_bot_dir(int(checkpoint["next_v"])))
            candidate_hash = hash_path(candidate_dir)
        except Exception as exc:
            errors.append(
                "terminal_review_abandon_candidate_unavailable:"
                + type(exc).__name__
            )
        else:
            if (
                not _valid_digest(candidate_hash)
                or outcome.get("candidate_artifact_hash") != candidate_hash
            ):
                errors.append("terminal_review_abandon_candidate_identity_invalid")

    if candidate_dir is not None:
        pre_projection = deepcopy(checkpoint)
        pre_projection["stage"] = "quality_passed"
        pre_projection["checkpoint_revision"] = (
            _LEGACY_TERMINAL_INPUT_REVISION
        )
        pre_projection.pop("terminal_gate_outcome", None)
        pre_gates = deepcopy(pre_projection.get("gate_results") or {})
        pre_gates.pop("review", None)
        pre_projection["gate_results"] = pre_gates
        try:
            from strict_authority_workflow import (
                bound_invocation_evidence,
                recover_terminal_gate_rejection_call,
            )

            recovered = recover_terminal_gate_rejection_call(
                pre_projection,
                gate_name="review",
                candidate_dir=candidate_dir,
            )
            if not isinstance(recovered, dict):
                raise TerminalGateReconcileError(
                    "terminal_review_abandon_authority_recovery_missing"
                )
            recovered_role_result = deepcopy(
                recovered.get("accepted_role_result")
                or recovered.get("projected_role_result")
            )
            recovered_receipt = recovered.get("accepted_receipt")
            recovered_context = recovered.get("context_binding")
            recovered_migration = recovered.get("terminal_semantic_migration")
            recovered_evidence = bound_invocation_evidence(recovered)
        except Exception as exc:
            errors.append(
                "terminal_review_abandon_authority_unavailable:"
                f"{type(exc).__name__}:{str(exc)[:240]}"
            )
            recovered = {}
            recovered_role_result = None
            recovered_receipt = None
            recovered_context = None
            recovered_migration = None
            recovered_evidence = None
        if (
            recovered_role_result != role_result
            or not isinstance(recovered_role_result, dict)
            or recovered_role_result.get("approved") is not False
        ):
            errors.append("terminal_review_abandon_role_result_mismatch")
        persisted_receipt = gate.get("llm_authority_receipt")
        if (
            not isinstance(recovered_receipt, dict)
            or persisted_receipt != recovered_receipt
        ):
            errors.append("terminal_review_abandon_authority_receipt_mismatch")
        if (
            not isinstance(recovered_context, dict)
            or gate.get("terminal_authority_context_binding")
            != recovered_context
        ):
            errors.append("terminal_review_abandon_authority_context_mismatch")
        if (
            not isinstance(recovered_migration, dict)
            or migration != recovered_migration
        ):
            errors.append("terminal_review_abandon_migration_authority_mismatch")
        if (
            not isinstance(recovered_evidence, dict)
            or gate.get("llm_execution_evidence") != recovered_evidence
        ):
            errors.append("terminal_review_abandon_execution_evidence_mismatch")
        persisted_receipt = (
            persisted_receipt if isinstance(persisted_receipt, dict) else {}
        )
        if (
            recovered.get("effect_id")
            != persisted_receipt.get("effect_id")
            or recovered.get("invocation_id")
            != persisted_receipt.get("invocation_id")
        ):
            errors.append("terminal_review_abandon_provider_identity_mismatch")
        expected_gate = (
            {
                "version": checkpoint.get("next_v"),
                "source_v": checkpoint.get("source_v"),
                "passed": False,
                "approved": False,
                "llm_invoked": True,
                "reviewer_llm_executed": True,
                "schema_valid": True,
                "quality_score": recovered_role_result.get(
                    "quality_score", 0
                ),
                "feedback": recovered_role_result.get("feedback", ""),
                "change_summary": recovered_role_result.get(
                    "change_summary", ""
                ),
                "risk_areas": recovered_role_result.get("risk_areas", []),
                "llm_role_result": deepcopy(recovered_role_result),
                "llm_authority_receipt": deepcopy(recovered_receipt),
                "llm_execution_evidence": deepcopy(recovered_evidence),
                "terminal_authority_context_binding": deepcopy(
                    recovered_context
                ),
                "operator_reconciled_completed_effect": True,
                "terminal_semantic_migration": deepcopy(recovered_migration),
            }
            if isinstance(recovered_role_result, dict)
            else None
        )
        if gate != expected_gate:
            errors.append("terminal_review_abandon_gate_projection_mismatch")
        try:
            outcome_errors = validate_terminal_gate_outcome(
                checkpoint,
                outcome,
                candidate_dir=candidate_dir,
            )
        except Exception as exc:
            outcome_errors = [
                "terminal_outcome_validation_error:" + type(exc).__name__
            ]
        if outcome_errors:
            errors.extend(
                "terminal_review_abandon_outcome_invalid:" + str(error)
                for error in outcome_errors
            )

    try:
        route = route_policy(checkpoint)
    except Exception as exc:
        route = {}
        errors.append(
            "terminal_review_abandon_route_unavailable:" + type(exc).__name__
        )
    if (
        route.get("intent") != "terminal_gate_abandon"
        or route.get("next_tool") != "abandon_generation"
        or route.get("allowed_tools") != ["abandon_generation"]
        or route.get("terminal_gate_outcome_digest")
        != outcome.get("receipt_digest")
    ):
        errors.append("terminal_review_abandon_route_invalid")

    authority_receipt = gate.get("llm_authority_receipt")
    effect_id = (
        authority_receipt.get("effect_id")
        if isinstance(authority_receipt, dict)
        else None
    )
    invocation_id = (
        authority_receipt.get("invocation_id")
        if isinstance(authority_receipt, dict)
        else None
    )
    if (
        not isinstance(effect_id, str)
        or not effect_id
        or not isinstance(invocation_id, str)
        or not invocation_id
    ):
        errors.append("terminal_review_abandon_provider_identity_invalid")
    if errors:
        raise TerminalGateReconcileError(
            "terminal_review_abandon_invalid:" + ";".join(
                dict.fromkeys(errors)
            )
        )
    return {
        "status": "reconcilable_terminal_review_abandon",
        "checkpoint": checkpoint,
        "gate": gate,
        "outcome": outcome,
        "route": route,
        "workflow_run_id": workflow_run_id,
        "next_v": checkpoint["next_v"],
        "source_v": checkpoint["source_v"],
        "parent2_v": checkpoint.get("parent2_v"),
        "checkpoint_stage": checkpoint["stage"],
        "checkpoint_revision": checkpoint["checkpoint_revision"],
        "candidate_dir": str(candidate_dir),
        "candidate_artifact_hash": candidate_hash,
        "effect_id": effect_id,
        "invocation_id": invocation_id,
        "terminal_gate_outcome_digest": outcome["receipt_digest"],
        "terminal_semantic_migration": deepcopy(migration),
        "terminal_semantic_migration_digest": migration["migration_digest"],
        "route_intent": route["intent"],
        "next_tool": route["next_tool"],
        "provider_dispatch_required": False,
    }


def inspect_completed_review_rejection() -> dict[str, Any]:
    from evolution_core import get_bot_dir
    from evolution_infra import read_pipeline_checkpoint
    from strict_authority_workflow import recover_terminal_gate_rejection_call
    from system_strict_bootstrap import is_declared_native_bootstrap

    checkpoint = read_pipeline_checkpoint()
    if not isinstance(checkpoint, dict):
        raise TerminalGateReconcileError("active_checkpoint_missing")
    if checkpoint.get("stage") != "quality_passed":
        raise TerminalGateReconcileError(
            "terminal_review_reconcile_requires_quality_passed"
        )
    if not is_declared_native_bootstrap(checkpoint):
        raise TerminalGateReconcileError(
            "terminal_review_reconcile_requires_declared_first_strict"
        )
    call = recover_terminal_gate_rejection_call(
        checkpoint,
        gate_name="review",
        candidate_dir=get_bot_dir(int(checkpoint["next_v"])),
    )
    if call is None:
        raise TerminalGateReconcileError(
            "completed_schema_valid_review_rejection_missing"
        )
    role_result = deepcopy(
        call.get("accepted_role_result") or call.get("projected_role_result")
    )
    if not isinstance(role_result, dict) or role_result.get("approved") is not False:
        raise TerminalGateReconcileError(
            "terminal_review_reconcile_refuses_non_rejection"
        )
    return {
        "status": "reconcilable_terminal_review_rejection",
        "checkpoint": checkpoint,
        "call": call,
        "role_result": role_result,
        "workflow_run_id": checkpoint["workflow_run_id"],
        "checkpoint_revision": checkpoint["checkpoint_revision"],
        "candidate_artifact_hash": (
            call.get("context_binding") or {}
        ).get("candidate_artifact_hash"),
        "effect_id": call.get("effect_id"),
        "invocation_id": call.get("invocation_id"),
        "terminal_semantic_migration": deepcopy(
            call.get("terminal_semantic_migration")
        ),
        "provider_dispatch_required": False,
    }


def inspect_terminal_gate_reconciliation() -> dict[str, Any]:
    """Inspect either side of the one-way projection crash boundary."""

    from evolution_infra import read_pipeline_checkpoint

    checkpoint = read_pipeline_checkpoint()
    if not isinstance(checkpoint, dict):
        raise TerminalGateReconcileError("active_checkpoint_missing")
    stage = checkpoint.get("stage")
    if stage == "quality_passed":
        return inspect_completed_review_rejection()
    if stage == "review_rejected":
        return _inspect_projected_terminal_review_abandon(checkpoint)
    raise TerminalGateReconcileError(
        "terminal_review_reconcile_stage_not_supported:" + str(stage or "")
    )


async def reconcile_completed_review_rejection() -> dict[str, Any]:
    from evolution_core import get_logs_dir
    from output_schema import validate_agent_output
    from strict_authority_workflow import (
        accept_role_result,
        record_bound_invocation_evidence,
        strict_invocation_log_path,
    )
    from system_strict_bootstrap import abandon_rejected_blueprint

    inspected = inspect_completed_review_rejection()
    checkpoint = inspected["checkpoint"]
    call = inspected["call"]
    role_result, schema_errors = validate_agent_output(
        "reviewer", inspected["role_result"]
    )
    if schema_errors or role_result.get("approved") is not False:
        raise TerminalGateReconcileError(
            "terminal_review_reconcile_schema_invalid:"
            + ";".join(schema_errors[:5])
        )
    log_file = strict_invocation_log_path(
        call,
        logs_dir=get_logs_dir(int(checkpoint["next_v"])),
        basename="reviewer_io.txt",
    )
    authority_receipt = accept_role_result(
        call,
        role_result=role_result,
        parse_contract="reviewer-output-schema-v1",
    )
    execution_evidence = record_bound_invocation_evidence(
        call,
        log_file=Path(log_file),
    )
    gate = {
        "version": int(checkpoint["next_v"]),
        "source_v": int(checkpoint["source_v"]),
        "passed": False,
        "approved": False,
        "llm_invoked": True,
        "reviewer_llm_executed": True,
        "schema_valid": True,
        "quality_score": role_result.get("quality_score", 0),
        "feedback": role_result.get("feedback", ""),
        "change_summary": role_result.get("change_summary", ""),
        "risk_areas": role_result.get("risk_areas", []),
        "llm_role_result": role_result,
        "llm_authority_receipt": authority_receipt,
        "llm_execution_evidence": execution_evidence,
        "terminal_authority_context_binding": deepcopy(
            call.get("context_binding")
        ),
        "operator_reconciled_completed_effect": True,
    }
    if isinstance(call.get("terminal_semantic_migration"), dict):
        gate["terminal_semantic_migration"] = deepcopy(
            call["terminal_semantic_migration"]
        )
    result = await abandon_rejected_blueprint(
        checkpoint,
        reason="operator_terminal_review_reconciliation",
        result={
            "error": "SYSTEM_STRICT_BOOTSTRAP_REVIEW_REJECTED",
            "approved": False,
            "success": False,
            "failure_class": "strategy_review",
            "feedback": role_result.get("feedback", ""),
            "terminal_gate_name": "review",
            "terminal_reason_code": "review_rejected",
            "terminal_gate_payload": gate,
            "provider_dispatch_required": False,
            "operator_reconciliation": True,
        },
    )
    return {
        **result,
        "effect_id": inspected["effect_id"],
        "invocation_id": inspected["invocation_id"],
        "terminal_semantic_migration": deepcopy(
            inspected.get("terminal_semantic_migration")
        ),
        "provider_dispatch_required": False,
    }


async def resume_terminal_review_abandon() -> dict[str, Any]:
    """Finish only the canonical abandon after a durable terminal projection."""

    from gate_outcome import terminal_outcome_abandon_reason
    from tool_bot_management import (
        _do_abandon_generation,
        expected_abandon_identity,
    )

    inspected = inspect_terminal_gate_reconciliation()
    if inspected.get("status") != "reconcilable_terminal_review_abandon":
        raise TerminalGateReconcileError(
            "terminal_review_abandon_requires_projected_outcome"
        )
    checkpoint = inspected["checkpoint"]
    outcome = inspected["outcome"]
    result = await _do_abandon_generation(
        reason=terminal_outcome_abandon_reason(outcome),
        _bypass_rate_limit=True,
        expected_terminal_gate_outcome_digest=outcome["receipt_digest"],
        **expected_abandon_identity(checkpoint),
    )
    completed = result.get("abandoned") is True
    return {
        **result,
        "status": (
            "terminal_review_abandon_executed"
            if completed
            else "terminal_review_abandon_failed"
        ),
        "workflow_run_id": inspected["workflow_run_id"],
        "next_v": inspected["next_v"],
        "source_v": inspected["source_v"],
        "parent2_v": inspected["parent2_v"],
        "checkpoint_stage": inspected["checkpoint_stage"],
        "checkpoint_revision": inspected["checkpoint_revision"],
        "candidate_artifact_hash": inspected["candidate_artifact_hash"],
        "effect_id": inspected["effect_id"],
        "invocation_id": inspected["invocation_id"],
        "terminal_gate_outcome_digest": inspected[
            "terminal_gate_outcome_digest"
        ],
        "terminal_semantic_migration_digest": inspected[
            "terminal_semantic_migration_digest"
        ],
        "provider_dispatch_required": False,
    }


async def reconcile_terminal_gate() -> dict[str, Any]:
    """Execute the exact mutation still required by the inspected stage."""

    inspected = inspect_terminal_gate_reconciliation()
    if inspected.get("status") == "reconcilable_terminal_review_rejection":
        return await reconcile_completed_review_rejection()
    if inspected.get("status") == "reconcilable_terminal_review_abandon":
        return await resume_terminal_review_abandon()
    raise TerminalGateReconcileError("terminal_review_reconcile_state_invalid")


__all__ = [
    "TerminalGateReconcileError",
    "inspect_completed_review_rejection",
    "inspect_terminal_gate_reconciliation",
    "reconcile_completed_review_rejection",
    "reconcile_terminal_gate",
    "resume_terminal_review_abandon",
]
