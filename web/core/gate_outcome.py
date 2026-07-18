"""Content-bound terminal gate outcomes for canonical pipeline cleanup.

An LLM message, an exception string, or an ``action=abandon_generation`` field
is not cleanup authority.  A terminal gate first projects an immutable outcome
into the checkpoint.  The outcome binds the workflow CAS identity, candidate
bytes, prerequisite gates, and (when present) the strict provider authority
and execution evidence.  Only that projected receipt may authorize abandon.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from workflow_kernel import content_digest


TERMINAL_GATE_OUTCOME_SCHEMA_VERSION = 1
TERMINAL_GATE_OUTCOME_KIND = "pipeline-terminal-gate-outcome-v1"
TERMINAL_GATE_DISPOSITION = "abandon_generation"

TERMINAL_STAGE_BY_GATE = {
    "quality": "quality_rejected",
    "review": "review_rejected",
    "critic": "critic_rejected",
}

_TERMINAL_SEMANTICS_BY_GATE = {
    "quality": frozenset({
        ("quality_gate_rejected", "quality_gate"),
        ("quality_receipt_invalid", "control_plane"),
    }),
    "review": frozenset({
        ("review_rejected", "strategy_review"),
        ("review_receipt_invalid", "control_plane"),
        ("review_authority_invalid", "control_plane"),
    }),
    "critic": frozenset({
        ("critic_receipt_invalid", "control_plane"),
        ("critic_authority_invalid", "control_plane"),
    }),
}
_REASON_CODES = frozenset(
    reason_code
    for allowed in _TERMINAL_SEMANTICS_BY_GATE.values()
    for reason_code, _failure_class in allowed
)
_FAILURE_CLASSES = frozenset(
    failure_class
    for allowed in _TERMINAL_SEMANTICS_BY_GATE.values()
    for _reason_code, failure_class in allowed
)
_OUTCOME_SUBJECT_KEYS = frozenset({
    "schema_version",
    "kind",
    "disposition",
    "gate_name",
    "terminal_stage",
    "reason_code",
    "failure_class",
    "workflow_run_id",
    "next_v",
    "source_v",
    "parent2_v",
    "evaluation_epoch",
    "epoch_binding_digest",
    "input_checkpoint_stage",
    "input_checkpoint_revision",
    "projected_checkpoint_revision",
    "candidate_artifact_hash",
    "master_plan_digest",
    "audit_context_digest",
    "prerequisite_gate_digests",
    "gate_payload_digest",
    "role_result_digest",
    "llm_authority_receipt_digest",
    "llm_execution_evidence_digest",
})
_OUTCOME_KEYS = _OUTCOME_SUBJECT_KEYS | {"receipt_digest"}
_HEX = frozenset("0123456789abcdef")


class TerminalGateOutcomeError(RuntimeError):
    """A terminal outcome is malformed or no longer matches its subject."""

    def __init__(self, errors: str | list[str]):
        if isinstance(errors, str):
            errors = [errors]
        self.errors = tuple(dict.fromkeys(str(item) for item in errors if item))
        super().__init__("; ".join(self.errors) or "terminal gate outcome invalid")


def _plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in _HEX for char in value)
    )


def _candidate_hash(candidate_dir: str | Path) -> str:
    from bot_artifact import hash_path

    return hash_path(Path(candidate_dir))


def _terminal_semantics_valid(
    gate_name: Any,
    reason_code: Any,
    failure_class: Any,
) -> bool:
    allowed = _TERMINAL_SEMANTICS_BY_GATE.get(gate_name)
    return allowed is not None and (reason_code, failure_class) in allowed


def _dependencies(checkpoint: dict[str, Any], gate_name: str) -> dict[str, str]:
    gates = checkpoint.get("gate_results") or {}
    if not isinstance(gates, dict):
        gates = {}
    required = {
        "quality": (),
        "review": ("quality",),
        "critic": ("quality", "review"),
    }[gate_name]
    return {
        name: content_digest(deepcopy(gates.get(name) or {}))
        for name in required
    }


def build_terminal_gate_outcome(
    checkpoint: dict[str, Any],
    *,
    gate_name: str,
    gate_payload: dict[str, Any],
    candidate_dir: str | Path,
    reason_code: str,
    failure_class: str,
) -> dict[str, Any]:
    """Build the deterministic receipt projected by one terminal gate."""

    errors: list[str] = []
    if not isinstance(checkpoint, dict):
        raise TerminalGateOutcomeError("terminal_outcome_checkpoint_invalid")
    if gate_name not in TERMINAL_STAGE_BY_GATE:
        errors.append("terminal_outcome_gate_invalid")
    if not isinstance(gate_payload, dict) or not gate_payload:
        errors.append("terminal_outcome_gate_payload_invalid")
    reason_known = isinstance(reason_code, str) and reason_code in _REASON_CODES
    failure_known = (
        isinstance(failure_class, str) and failure_class in _FAILURE_CLASSES
    )
    if not reason_known:
        errors.append("terminal_outcome_reason_code_invalid")
    if not failure_known:
        errors.append("terminal_outcome_failure_class_invalid")
    if (
        gate_name in TERMINAL_STAGE_BY_GATE
        and reason_known
        and failure_known
        and not _terminal_semantics_valid(
            gate_name,
            reason_code,
            failure_class,
        )
    ):
        errors.append("terminal_outcome_gate_semantics_invalid")
    workflow_run_id = str(
        checkpoint.get("workflow_run_id") or checkpoint.get("run_id") or ""
    )
    if not workflow_run_id:
        errors.append("terminal_outcome_workflow_run_id_missing")
    if not str(checkpoint.get("evaluation_epoch") or ""):
        errors.append("terminal_outcome_evaluation_epoch_missing")
    for field in ("next_v", "source_v", "checkpoint_revision"):
        if not _plain_int(checkpoint.get(field)):
            errors.append(f"terminal_outcome_{field}_invalid")
    input_stage = str(checkpoint.get("stage") or "")
    expected_input_stage = {
        "quality": {"workers_done", "quality_failed"},
        "review": {"quality_passed"},
        "critic": {"reviewed"},
    }.get(gate_name, set())
    if input_stage not in expected_input_stage:
        errors.append("terminal_outcome_input_stage_invalid")
    authority = gate_payload.get("llm_authority_receipt")
    evidence = gate_payload.get("llm_execution_evidence")
    role_result = gate_payload.get("llm_role_result")
    role_bound = isinstance(role_result, dict)
    if role_bound and (
        not isinstance(authority, dict) or not isinstance(evidence, dict)
    ):
        errors.append("terminal_outcome_role_authority_incomplete")
    if errors:
        raise TerminalGateOutcomeError(errors)

    subject = {
        "schema_version": TERMINAL_GATE_OUTCOME_SCHEMA_VERSION,
        "kind": TERMINAL_GATE_OUTCOME_KIND,
        "disposition": TERMINAL_GATE_DISPOSITION,
        "gate_name": gate_name,
        "terminal_stage": TERMINAL_STAGE_BY_GATE[gate_name],
        "reason_code": reason_code,
        "failure_class": failure_class,
        "workflow_run_id": workflow_run_id,
        "next_v": int(checkpoint["next_v"]),
        "source_v": int(checkpoint["source_v"]),
        "parent2_v": checkpoint.get("parent2_v"),
        "evaluation_epoch": str(checkpoint.get("evaluation_epoch") or ""),
        "epoch_binding_digest": content_digest(
            deepcopy(checkpoint.get("epoch_binding") or {})
        ),
        "input_checkpoint_stage": input_stage,
        "input_checkpoint_revision": int(checkpoint["checkpoint_revision"]),
        "projected_checkpoint_revision": int(checkpoint["checkpoint_revision"]) + 1,
        "candidate_artifact_hash": _candidate_hash(candidate_dir),
        "master_plan_digest": content_digest(
            deepcopy(checkpoint.get("master_plan") or {})
        ),
        "audit_context_digest": content_digest(
            deepcopy(checkpoint.get("audit_context") or {})
        ),
        "prerequisite_gate_digests": _dependencies(checkpoint, gate_name),
        "gate_payload_digest": content_digest(deepcopy(gate_payload)),
        "role_result_digest": (
            content_digest(deepcopy(role_result)) if role_bound else None
        ),
        "llm_authority_receipt_digest": (
            content_digest(deepcopy(authority))
            if isinstance(authority, dict)
            else None
        ),
        "llm_execution_evidence_digest": (
            content_digest(deepcopy(evidence))
            if isinstance(evidence, dict)
            else None
        ),
    }
    if not _valid_digest(subject["candidate_artifact_hash"]):
        raise TerminalGateOutcomeError(
            "terminal_outcome_candidate_artifact_hash_invalid"
        )
    if frozenset(subject) != _OUTCOME_SUBJECT_KEYS:  # pragma: no cover
        raise TerminalGateOutcomeError("terminal_outcome_schema_keys_invalid")
    return {**subject, "receipt_digest": content_digest(subject)}


def validate_terminal_gate_outcome(
    checkpoint: dict[str, Any],
    outcome: dict[str, Any] | None = None,
    *,
    candidate_dir: str | Path,
) -> list[str]:
    """Rebuild the receipt subject from a terminal checkpoint, fail closed."""

    if not isinstance(checkpoint, dict):
        return ["terminal_outcome_checkpoint_invalid"]
    outcome = outcome if outcome is not None else checkpoint.get(
        "terminal_gate_outcome"
    )
    if not isinstance(outcome, dict):
        return ["terminal_outcome_missing"]
    errors: list[str] = []
    gate_name = str(outcome.get("gate_name") or "")
    terminal_stage = TERMINAL_STAGE_BY_GATE.get(gate_name)
    if frozenset(outcome) != _OUTCOME_KEYS:
        errors.append("terminal_outcome_schema_keys_invalid")
    if outcome.get("schema_version") != TERMINAL_GATE_OUTCOME_SCHEMA_VERSION:
        errors.append("terminal_outcome_schema_version_invalid")
    if outcome.get("kind") != TERMINAL_GATE_OUTCOME_KIND:
        errors.append("terminal_outcome_kind_invalid")
    if outcome.get("disposition") != TERMINAL_GATE_DISPOSITION:
        errors.append("terminal_outcome_disposition_invalid")
    if terminal_stage is None or outcome.get("terminal_stage") != terminal_stage:
        errors.append("terminal_outcome_stage_binding_invalid")
    if checkpoint.get("stage") != terminal_stage:
        errors.append("terminal_outcome_checkpoint_stage_mismatch")
    reason_code = outcome.get("reason_code")
    failure_class = outcome.get("failure_class")
    reason_known = isinstance(reason_code, str) and reason_code in _REASON_CODES
    failure_known = (
        isinstance(failure_class, str) and failure_class in _FAILURE_CLASSES
    )
    if not reason_known:
        errors.append("terminal_outcome_reason_code_invalid")
    if not failure_known:
        errors.append("terminal_outcome_failure_class_invalid")
    if (
        gate_name in TERMINAL_STAGE_BY_GATE
        and reason_known
        and failure_known
        and not _terminal_semantics_valid(
            gate_name,
            reason_code,
            failure_class,
        )
    ):
        errors.append("terminal_outcome_gate_semantics_invalid")
    workflow = str(
        checkpoint.get("workflow_run_id") or checkpoint.get("run_id") or ""
    )
    for field, expected in {
        "workflow_run_id": workflow,
        "next_v": checkpoint.get("next_v"),
        "source_v": checkpoint.get("source_v"),
        "parent2_v": checkpoint.get("parent2_v"),
        "evaluation_epoch": str(checkpoint.get("evaluation_epoch") or ""),
        "projected_checkpoint_revision": checkpoint.get("checkpoint_revision"),
    }.items():
        if outcome.get(field) != expected:
            errors.append(f"terminal_outcome_{field}_mismatch")
    input_revision = outcome.get("input_checkpoint_revision")
    if not _plain_int(input_revision) or outcome.get(
        "projected_checkpoint_revision"
    ) != int(input_revision or 0) + 1:
        errors.append("terminal_outcome_revision_chain_invalid")
    expected_input_stage = {
        "quality": {"workers_done", "quality_failed"},
        "review": {"quality_passed"},
        "critic": {"reviewed"},
    }.get(gate_name, set())
    if outcome.get("input_checkpoint_stage") not in expected_input_stage:
        errors.append("terminal_outcome_input_stage_invalid")

    gates = checkpoint.get("gate_results") or {}
    gate_payload = gates.get(gate_name) if isinstance(gates, dict) else None
    if not isinstance(gate_payload, dict):
        errors.append("terminal_outcome_gate_payload_missing")
        gate_payload = {}
    if outcome.get("gate_payload_digest") != content_digest(gate_payload):
        errors.append("terminal_outcome_gate_payload_digest_mismatch")
    if outcome.get("master_plan_digest") != content_digest(
        deepcopy(checkpoint.get("master_plan") or {})
    ):
        errors.append("terminal_outcome_master_plan_digest_mismatch")
    if outcome.get("audit_context_digest") != content_digest(
        deepcopy(checkpoint.get("audit_context") or {})
    ):
        errors.append("terminal_outcome_audit_context_digest_mismatch")
    if outcome.get("epoch_binding_digest") != content_digest(
        deepcopy(checkpoint.get("epoch_binding") or {})
    ):
        errors.append("terminal_outcome_epoch_binding_digest_mismatch")
    if gate_name in TERMINAL_STAGE_BY_GATE and outcome.get(
        "prerequisite_gate_digests"
    ) != _dependencies(checkpoint, gate_name):
        errors.append("terminal_outcome_prerequisite_gate_digest_mismatch")
    try:
        candidate_hash = _candidate_hash(candidate_dir)
    except Exception as exc:
        errors.append(
            "terminal_outcome_candidate_unavailable:" + type(exc).__name__
        )
    else:
        if not _valid_digest(candidate_hash):
            errors.append("terminal_outcome_candidate_artifact_hash_invalid")
        elif outcome.get("candidate_artifact_hash") != candidate_hash:
            errors.append("terminal_outcome_candidate_artifact_hash_mismatch")

    role_result = gate_payload.get("llm_role_result")
    authority = gate_payload.get("llm_authority_receipt")
    evidence = gate_payload.get("llm_execution_evidence")
    expected_role_digest = (
        content_digest(role_result) if isinstance(role_result, dict) else None
    )
    expected_authority_digest = (
        content_digest(authority) if isinstance(authority, dict) else None
    )
    expected_evidence_digest = (
        content_digest(evidence) if isinstance(evidence, dict) else None
    )
    for field, expected in {
        "role_result_digest": expected_role_digest,
        "llm_authority_receipt_digest": expected_authority_digest,
        "llm_execution_evidence_digest": expected_evidence_digest,
    }.items():
        if outcome.get(field) != expected:
            errors.append(f"terminal_outcome_{field}_mismatch")
    if isinstance(role_result, dict) and (
        expected_authority_digest is None or expected_evidence_digest is None
    ):
        errors.append("terminal_outcome_role_authority_incomplete")
    elif isinstance(role_result, dict) and gate_name in {"review", "critic"}:
        try:
            from strict_authority_workflow import (
                MASTER_SLOTS,
                authority_summary,
                expected_master_contexts,
                gate_call_context,
            )

            required_slots = MASTER_SLOTS + (
                ("review",)
                if gate_name == "review"
                else ("review", "critic")
            )
            expected_evidence = {gate_name: deepcopy(evidence)}
            if gate_name == "critic":
                expected_evidence["review"] = deepcopy(
                    ((gates.get("review") or {}).get("llm_execution_evidence"))
                )
            authority_summary(
                checkpoint,
                required_slots=required_slots,
                expected_role_results={gate_name: deepcopy(role_result)},
                expected_context_bindings={
                    **expected_master_contexts(
                        checkpoint.get("master_plan") or {}
                    ),
                    gate_name: deepcopy(
                        gate_payload.get("terminal_authority_context_binding")
                        if isinstance(
                            gate_payload.get(
                                "terminal_authority_context_binding"
                            ),
                            dict,
                        )
                        else gate_call_context(
                            checkpoint,
                            gate_name=gate_name,
                            candidate_dir=Path(candidate_dir),
                        )
                    ),
                },
                expected_invocation_evidence=expected_evidence,
                require_no_other_accepted=True,
            )
        except Exception as exc:
            errors.append(
                "terminal_outcome_strict_authority_invalid:"
                f"{type(exc).__name__}:{str(exc)[:240]}"
            )

    subject = {key: value for key, value in outcome.items() if key != "receipt_digest"}
    if not _valid_digest(outcome.get("receipt_digest")) or outcome.get(
        "receipt_digest"
    ) != content_digest(subject):
        errors.append("terminal_outcome_receipt_digest_invalid")
    return list(dict.fromkeys(errors))


def terminal_outcome_abandon_reason(outcome: dict[str, Any]) -> str:
    """Return the bounded ledger reason derived from the receipt identity."""

    digest = str((outcome or {}).get("receipt_digest") or "")
    if not _valid_digest(digest):
        raise TerminalGateOutcomeError("terminal_outcome_receipt_digest_invalid")
    return f"terminal_gate_outcome:{digest}"


__all__ = [
    "TERMINAL_GATE_DISPOSITION",
    "TERMINAL_GATE_OUTCOME_KIND",
    "TERMINAL_GATE_OUTCOME_SCHEMA_VERSION",
    "TERMINAL_STAGE_BY_GATE",
    "TerminalGateOutcomeError",
    "build_terminal_gate_outcome",
    "terminal_outcome_abandon_reason",
    "validate_terminal_gate_outcome",
]
