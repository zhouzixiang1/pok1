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
        "provider_dispatch_required": False,
    }


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
        "provider_dispatch_required": False,
    }


__all__ = [
    "TerminalGateReconcileError",
    "inspect_completed_review_rejection",
    "reconcile_completed_review_rejection",
]
