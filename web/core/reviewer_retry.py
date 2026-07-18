"""Content-bound, bounded Reviewer verdict retries.

Reviewer output is a stochastic judgment over immutable inputs.  A single
negative verdict therefore schedules one independent same-stage judgment; it
does not erase a completed Master, Worker, or Quality phase.  This module owns
the append-only checkpoint projection and the conservative deterministic
adjudication rule used after that second judgment.

Provider/schema/transport failures are deliberately absent from this journal.
They remain typed infrastructure retries and never count as a negative code
verdict.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from workflow_kernel import content_digest


REVIEW_ATTEMPT_SCHEMA_VERSION = 1
REVIEW_ATTEMPT_KIND = "pipeline-review-verdict-attempt-v1"
REVIEW_ADJUDICATION_KIND = "pipeline-review-adjudication-v1"
MAX_REVIEW_VERDICT_ATTEMPTS = 2
REVIEW_AUTHORITY_SLOTS = ("review", "review:retry")

_HEX = frozenset("0123456789abcdef")
_ATTEMPT_KEYS = frozenset({
    "schema_version",
    "kind",
    "workflow_run_id",
    "next_v",
    "source_v",
    "parent2_v",
    "attempt",
    "cycle_digest",
    "authority_slot",
    "input_checkpoint_revision",
    "candidate_artifact_hash",
    "master_plan_digest",
    "quality_gate_digest",
    "review_semantic_contract_digest",
    "gate_payload",
    "gate_payload_digest",
    "role_result_digest",
    "approved",
    "consumed_infrastructure_failure_digest",
    "consumed_infrastructure_attempt",
    "receipt_digest",
})


class ReviewRetryError(RuntimeError):
    """The persisted Reviewer attempt history is malformed or drifted."""

    def __init__(self, errors: str | list[str]):
        if isinstance(errors, str):
            errors = [errors]
        self.errors = tuple(dict.fromkeys(str(item) for item in errors if item))
        super().__init__("; ".join(self.errors) or "review retry invalid")


def _plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _digest_ok(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in _HEX for char in value)
    )


def _candidate_hash(candidate_dir: str | Path) -> str:
    from bot_artifact import hash_path

    return hash_path(Path(candidate_dir))


def _role_result(gate: dict[str, Any]) -> dict[str, Any]:
    value = gate.get("llm_role_result")
    if isinstance(value, dict):
        return deepcopy(value)
    return {
        key: deepcopy(value)
        for key, value in gate.items()
        if key not in {
            "system_verifier_receipt",
            "llm_authority_receipt",
            "llm_execution_evidence",
            "llm_role_result",
        }
    }


def build_review_attempt_receipt(
    checkpoint: dict[str, Any],
    *,
    gate_payload: dict[str, Any],
    candidate_dir: str | Path,
    attempt: int,
    authority_slot: str,
    review_semantic_contract_digest: str,
    consumed_infrastructure_failure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind one schema-valid Reviewer verdict to its immutable inputs."""

    errors: list[str] = []
    if not isinstance(checkpoint, dict):
        errors.append("review_attempt_checkpoint_invalid")
    if not isinstance(gate_payload, dict) or not gate_payload:
        errors.append("review_attempt_gate_payload_invalid")
    if attempt not in (1, 2):
        errors.append("review_attempt_ordinal_invalid")
    if authority_slot != REVIEW_AUTHORITY_SLOTS[attempt - 1]:
        errors.append("review_attempt_authority_slot_invalid")
    workflow_run_id = str((checkpoint or {}).get("workflow_run_id") or "")
    if not workflow_run_id:
        errors.append("review_attempt_workflow_run_id_missing")
    for field in ("next_v", "source_v", "checkpoint_revision"):
        if not _plain_int((checkpoint or {}).get(field)):
            errors.append(f"review_attempt_{field}_invalid")
    if (checkpoint or {}).get("stage") != "quality_passed":
        errors.append("review_attempt_input_stage_invalid")
    if gate_payload.get("approved") not in {True, False}:
        errors.append("review_attempt_approved_invalid")
    if gate_payload.get("schema_valid") is not True:
        errors.append("review_attempt_schema_not_valid")
    if gate_payload.get("llm_invoked") is not True or gate_payload.get(
        "reviewer_llm_executed"
    ) is not True:
        errors.append("review_attempt_execution_markers_invalid")
    if not _digest_ok(review_semantic_contract_digest):
        errors.append("review_attempt_semantic_contract_digest_invalid")
    authority = gate_payload.get("llm_authority_receipt")
    if isinstance(authority, dict) and authority.get("slot") != authority_slot:
        errors.append("review_attempt_authority_receipt_slot_mismatch")
    if errors:
        raise ReviewRetryError(errors)

    role_result = _role_result(gate_payload)
    from pipeline_infrastructure import infrastructure_failure_digest

    consumed_infra_digest = infrastructure_failure_digest(
        consumed_infrastructure_failure
    )
    consumed_infra_attempt = (
        consumed_infrastructure_failure.get("attempt")
        if isinstance(consumed_infrastructure_failure, dict)
        else None
    )
    if consumed_infrastructure_failure is not None and (
        not consumed_infra_digest or not _plain_int(consumed_infra_attempt)
    ):
        raise ReviewRetryError("review_attempt_consumed_infrastructure_invalid")
    cycle_subject = {
        "workflow_run_id": workflow_run_id,
        "next_v": int(checkpoint["next_v"]),
        "source_v": int(checkpoint["source_v"]),
        "parent2_v": checkpoint.get("parent2_v"),
        "candidate_artifact_hash": _candidate_hash(candidate_dir),
        "master_plan_digest": content_digest(
            deepcopy(checkpoint.get("master_plan") or {})
        ),
        "quality_gate_digest": content_digest(
            deepcopy((checkpoint.get("gate_results") or {}).get("quality") or {})
        ),
        "review_semantic_contract_digest": review_semantic_contract_digest,
    }
    subject = {
        "schema_version": REVIEW_ATTEMPT_SCHEMA_VERSION,
        "kind": REVIEW_ATTEMPT_KIND,
        "workflow_run_id": workflow_run_id,
        "next_v": int(checkpoint["next_v"]),
        "source_v": int(checkpoint["source_v"]),
        "parent2_v": checkpoint.get("parent2_v"),
        "attempt": int(attempt),
        "cycle_digest": content_digest(cycle_subject),
        "authority_slot": authority_slot,
        "input_checkpoint_revision": int(checkpoint["checkpoint_revision"]),
        "candidate_artifact_hash": cycle_subject["candidate_artifact_hash"],
        "master_plan_digest": cycle_subject["master_plan_digest"],
        "quality_gate_digest": cycle_subject["quality_gate_digest"],
        "review_semantic_contract_digest": review_semantic_contract_digest,
        "gate_payload": deepcopy(gate_payload),
        "gate_payload_digest": content_digest(deepcopy(gate_payload)),
        "role_result_digest": content_digest(role_result),
        "approved": gate_payload.get("approved") is True,
        "consumed_infrastructure_failure_digest": (
            consumed_infra_digest or None
        ),
        "consumed_infrastructure_attempt": consumed_infra_attempt,
    }
    return {**subject, "receipt_digest": content_digest(subject)}


def validate_review_attempt_journal(
    checkpoint: dict[str, Any],
    *,
    candidate_dir: str | Path | None = None,
    review_semantic_contract_digest: str = "",
) -> list[str]:
    """Reprove every attempt and the append-only sequence from live inputs."""

    raw = checkpoint.get("review_attempt_journal") if isinstance(checkpoint, dict) else None
    if raw is None:
        return []
    if not isinstance(raw, list):
        return ["review_attempt_journal_not_list"]
    errors: list[str] = []
    # Resolve the candidate once so an unsafe/missing path fails before any
    # journal row is accepted.  Per-cycle hashes remain self-contained because
    # an earlier repair cycle's bytes are intentionally no longer live.
    if candidate_dir is not None:
        _candidate_hash(candidate_dir)
    prior_revision = -1
    seen_cycles: set[str] = set()
    prior_cycle = ""
    cycle_attempt = 0
    for index, row in enumerate(raw, start=1):
        if not isinstance(row, dict):
            errors.append(f"review_attempt_{index}_not_object")
            continue
        if set(row) != _ATTEMPT_KEYS:
            errors.append(f"review_attempt_{index}_fields_mismatch")
        subject = {key: deepcopy(value) for key, value in row.items() if key != "receipt_digest"}
        if row.get("receipt_digest") != content_digest(subject):
            errors.append(f"review_attempt_{index}_receipt_digest_invalid")
        cycle_digest = str(row.get("cycle_digest") or "")
        if cycle_digest != prior_cycle:
            if cycle_digest in seen_cycles:
                errors.append(f"review_attempt_{index}_cycle_reopened")
            seen_cycles.add(cycle_digest)
            prior_cycle = cycle_digest
            cycle_attempt = 1
        else:
            cycle_attempt += 1
        if cycle_attempt > MAX_REVIEW_VERDICT_ATTEMPTS:
            errors.append(f"review_attempt_{index}_cycle_exceeds_budget")
        expected = {
            "schema_version": REVIEW_ATTEMPT_SCHEMA_VERSION,
            "kind": REVIEW_ATTEMPT_KIND,
            "workflow_run_id": checkpoint.get("workflow_run_id"),
            "next_v": checkpoint.get("next_v"),
            "source_v": checkpoint.get("source_v"),
            "parent2_v": checkpoint.get("parent2_v"),
            "attempt": cycle_attempt,
            "authority_slot": (
                REVIEW_AUTHORITY_SLOTS[cycle_attempt - 1]
                if cycle_attempt <= MAX_REVIEW_VERDICT_ATTEMPTS
                else None
            ),
        }
        for field, value in expected.items():
            if row.get(field) != value:
                errors.append(f"review_attempt_{index}_{field}_mismatch")
        stored_cycle_subject = {
            "workflow_run_id": row.get("workflow_run_id"),
            "next_v": row.get("next_v"),
            "source_v": row.get("source_v"),
            "parent2_v": row.get("parent2_v"),
            "candidate_artifact_hash": row.get("candidate_artifact_hash"),
            "master_plan_digest": row.get("master_plan_digest"),
            "quality_gate_digest": row.get("quality_gate_digest"),
            "review_semantic_contract_digest": row.get(
                "review_semantic_contract_digest"
            ),
        }
        if cycle_digest != content_digest(stored_cycle_subject):
            errors.append(f"review_attempt_{index}_cycle_digest_invalid")
        revision = row.get("input_checkpoint_revision")
        if not _plain_int(revision) or int(revision) <= prior_revision:
            errors.append(f"review_attempt_{index}_revision_invalid")
        else:
            prior_revision = int(revision)
        gate = row.get("gate_payload")
        if not isinstance(gate, dict):
            errors.append(f"review_attempt_{index}_gate_payload_invalid")
            continue
        if row.get("gate_payload_digest") != content_digest(deepcopy(gate)):
            errors.append(f"review_attempt_{index}_gate_payload_digest_invalid")
        role_result = _role_result(gate)
        if row.get("role_result_digest") != content_digest(role_result):
            errors.append(f"review_attempt_{index}_role_result_digest_invalid")
        if gate.get("approved") is not row.get("approved"):
            errors.append(f"review_attempt_{index}_approved_mismatch")
        if gate.get("schema_valid") is not True or gate.get("llm_invoked") is not True:
            errors.append(f"review_attempt_{index}_execution_invalid")
        consumed_digest = row.get("consumed_infrastructure_failure_digest")
        consumed_attempt = row.get("consumed_infrastructure_attempt")
        if (consumed_digest is None) != (consumed_attempt is None):
            errors.append(f"review_attempt_{index}_consumed_infrastructure_shape_invalid")
        if consumed_digest is not None and (
            not _digest_ok(consumed_digest) or not _plain_int(consumed_attempt)
        ):
            errors.append(f"review_attempt_{index}_consumed_infrastructure_invalid")
        authority = gate.get("llm_authority_receipt")
        if isinstance(authority, dict) and authority.get("slot") != row.get(
            "authority_slot"
        ):
            errors.append(f"review_attempt_{index}_authority_slot_mismatch")
    cycles: dict[str, list[dict[str, Any]]] = {}
    for row in raw:
        if isinstance(row, dict):
            cycles.setdefault(str(row.get("cycle_digest") or ""), []).append(row)
    for rows in cycles.values():
        if rows and rows[0].get("approved") is True and len(rows) != 1:
            errors.append("review_attempt_after_initial_approval_forbidden")
        if len(rows) == 2 and rows[0].get("approved") is not False:
            errors.append("review_retry_requires_initial_rejection")
    return list(dict.fromkeys(errors))


def current_review_attempts(
    checkpoint: dict[str, Any],
    *,
    candidate_dir: str | Path,
    review_semantic_contract_digest: str,
) -> list[dict[str, Any]]:
    """Return only the current artifact/Quality cycle's contiguous suffix."""

    errors = validate_review_attempt_journal(
        checkpoint,
        candidate_dir=candidate_dir,
        review_semantic_contract_digest=review_semantic_contract_digest,
    )
    if errors:
        raise ReviewRetryError(errors)
    journal = checkpoint.get("review_attempt_journal") or []
    if not journal:
        return []
    candidate_hash = _candidate_hash(candidate_dir)
    subject = {
        "workflow_run_id": checkpoint.get("workflow_run_id"),
        "next_v": checkpoint.get("next_v"),
        "source_v": checkpoint.get("source_v"),
        "parent2_v": checkpoint.get("parent2_v"),
        "candidate_artifact_hash": candidate_hash,
        "master_plan_digest": content_digest(deepcopy(checkpoint.get("master_plan") or {})),
        "quality_gate_digest": content_digest(
            deepcopy((checkpoint.get("gate_results") or {}).get("quality") or {})
        ),
        "review_semantic_contract_digest": review_semantic_contract_digest,
    }
    cycle_digest = content_digest(subject)
    rows: list[dict[str, Any]] = []
    for row in reversed(journal):
        if row.get("cycle_digest") != cycle_digest:
            break
        rows.append(deepcopy(row))
    return list(reversed(rows))


def review_attempt_action(journal: Any) -> dict[str, Any]:
    """Return the sole deterministic action for a validated attempt list."""

    if not isinstance(journal, list) or len(journal) > MAX_REVIEW_VERDICT_ATTEMPTS:
        raise ReviewRetryError("review_attempt_action_journal_invalid")
    if not journal:
        return {"action": "dispatch", "attempt": 1}
    decisions = [row.get("approved") for row in journal if isinstance(row, dict)]
    if len(decisions) != len(journal) or any(value not in {True, False} for value in decisions):
        raise ReviewRetryError("review_attempt_action_decision_invalid")
    if len(decisions) == 1:
        if decisions[0] is True:
            return {"action": "approve", "attempt": 1, "consistency": "initial_approve"}
        return {"action": "dispatch", "attempt": 2, "consistency": "initial_reject"}
    if decisions == [False, False]:
        return {"action": "repair", "attempt": 2, "consistency": "consistent_reject"}
    if decisions == [False, True]:
        # One approval cannot erase a content-bound rejection.  This is the
        # conservative adjudication rule: conflicting judgments require code
        # repair and a new post-quality review cycle, never silent approval.
        return {"action": "repair", "attempt": 2, "consistency": "conflict"}
    raise ReviewRetryError("review_attempt_action_sequence_invalid")


def build_review_adjudication(journal: list[dict[str, Any]]) -> dict[str, Any]:
    action = review_attempt_action(journal)
    if action["action"] not in {"approve", "repair"}:
        raise ReviewRetryError("review_adjudication_attempts_incomplete")
    subject = {
        "schema_version": 1,
        "kind": REVIEW_ADJUDICATION_KIND,
        "attempt_receipt_digests": [row["receipt_digest"] for row in journal],
        "attempt_decisions": [bool(row["approved"]) for row in journal],
        "disposition": action["action"],
        "consistency": action["consistency"],
        "rule": "one_rejection_blocks_approval_v1",
    }
    return {**subject, "receipt_digest": content_digest(subject)}


def validate_strict_review_attempt_authority(
    checkpoint: dict[str, Any],
    *,
    journal: list[dict[str, Any]],
    candidate_dir: str | Path,
) -> list[str]:
    """Reopen every strict provider/evidence receipt for the current cycle."""

    if not journal:
        return ["strict_review_attempt_journal_empty"]
    try:
        from strict_authority_workflow import (
            MASTER_SLOTS,
            StrictAuthorityError,
            authority_summary,
            expected_master_contexts,
            gate_call_context,
        )

        slots = tuple(str(row.get("authority_slot") or "") for row in journal)
        expected_results = {
            slot: _role_result(row.get("gate_payload") or {})
            for slot, row in zip(slots, journal)
        }
        expected_evidence = {
            slot: deepcopy((row.get("gate_payload") or {}).get("llm_execution_evidence"))
            for slot, row in zip(slots, journal)
        }
        contexts = expected_master_contexts(checkpoint.get("master_plan") or {})
        contexts.update({
            slot: gate_call_context(
                checkpoint,
                gate_name=slot,
                candidate_dir=candidate_dir,
            )
            for slot in slots
        })
        authority_summary(
            checkpoint,
            required_slots=MASTER_SLOTS + slots,
            expected_role_results=expected_results,
            expected_context_bindings=contexts,
            expected_invocation_evidence=expected_evidence,
            require_no_other_accepted=True,
        )
    except Exception as exc:
        errors = getattr(exc, "errors", None)
        if errors:
            return list(errors)
        return [
            "strict_review_attempt_authority_unavailable:"
            f"{type(exc).__name__}:{str(exc)[:240]}"
        ]
    return []


__all__ = [
    "MAX_REVIEW_VERDICT_ATTEMPTS",
    "REVIEW_AUTHORITY_SLOTS",
    "ReviewRetryError",
    "build_review_adjudication",
    "build_review_attempt_receipt",
    "current_review_attempts",
    "review_attempt_action",
    "validate_strict_review_attempt_authority",
    "validate_review_attempt_journal",
]
