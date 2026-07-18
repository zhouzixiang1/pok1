from __future__ import annotations

from copy import deepcopy

import pytest


def _checkpoint(*, revision=8, quality_marker="q1"):
    return {
        "workflow_run_id": "generation:143:review-retry-test",
        "next_v": 143,
        "source_v": 142,
        "parent2_v": None,
        "stage": "quality_passed",
        "checkpoint_revision": revision,
        "master_plan": {"tasks": [], "plan": "frozen"},
        "gate_results": {
            "quality": {
                "all_passed": True,
                "critical_scenarios_passed": True,
                "marker": quality_marker,
            }
        },
    }


def _gate(*, approved, slot):
    role = {
        "approved": approved,
        "quality_score": 8 if approved else 4,
        "feedback": "pass" if approved else "repair dead branch",
        "change_summary": "reviewed",
        "risk_areas": [],
    }
    return {
        "passed": approved,
        "approved": approved,
        "llm_invoked": True,
        "reviewer_llm_executed": True,
        "schema_valid": True,
        "feedback": role["feedback"],
        "llm_role_result": role,
        "llm_authority_receipt": {"slot": slot},
        "llm_execution_evidence": {"invocation_id": slot},
    }


def test_first_negative_schedules_exactly_one_second_attempt(tmp_path):
    from reviewer_retry import (
        build_review_attempt_receipt,
        current_review_attempts,
        review_attempt_action,
        validate_review_attempt_journal,
    )

    candidate = tmp_path / "national_v143"
    candidate.mkdir()
    (candidate / "policy.py").write_text("def decide(_ctx):\n    return {'intent': 'fold'}\n")
    checkpoint = _checkpoint()
    first = build_review_attempt_receipt(
        checkpoint,
        gate_payload=_gate(approved=False, slot="review"),
        candidate_dir=candidate,
        attempt=1,
        authority_slot="review",
        review_semantic_contract_digest="a" * 64,
    )
    checkpoint["review_attempt_journal"] = [first]

    assert validate_review_attempt_journal(
        checkpoint,
        candidate_dir=candidate,
        review_semantic_contract_digest="a" * 64,
    ) == []
    current = current_review_attempts(
        checkpoint,
        candidate_dir=candidate,
        review_semantic_contract_digest="a" * 64,
    )
    assert review_attempt_action(current) == {
        "action": "dispatch",
        "attempt": 2,
        "consistency": "initial_reject",
    }


@pytest.mark.parametrize(
    ("second_approved", "consistency"),
    [(False, "consistent_reject"), (True, "conflict")],
)
def test_two_attempts_conservatively_route_to_repair(
    tmp_path,
    second_approved,
    consistency,
):
    from reviewer_retry import (
        build_review_adjudication,
        build_review_attempt_receipt,
        review_attempt_action,
    )

    candidate = tmp_path / "national_v143"
    candidate.mkdir()
    (candidate / "policy.py").write_text("VALUE = 1\n")
    checkpoint = _checkpoint()
    first = build_review_attempt_receipt(
        checkpoint,
        gate_payload=_gate(approved=False, slot="review"),
        candidate_dir=candidate,
        attempt=1,
        authority_slot="review",
        review_semantic_contract_digest="b" * 64,
    )
    checkpoint["checkpoint_revision"] = 9
    second = build_review_attempt_receipt(
        checkpoint,
        gate_payload=_gate(approved=second_approved, slot="review:retry"),
        candidate_dir=candidate,
        attempt=2,
        authority_slot="review:retry",
        review_semantic_contract_digest="b" * 64,
    )
    rows = [first, second]

    assert review_attempt_action(rows) == {
        "action": "repair",
        "attempt": 2,
        "consistency": consistency,
    }
    adjudication = build_review_adjudication(rows)
    assert adjudication["disposition"] == "repair"
    assert adjudication["consistency"] == consistency
    assert adjudication["attempt_receipt_digests"] == [
        first["receipt_digest"],
        second["receipt_digest"],
    ]


def test_attempt_journal_tamper_and_third_attempt_fail_closed(tmp_path):
    from reviewer_retry import (
        ReviewRetryError,
        build_review_attempt_receipt,
        review_attempt_action,
        validate_review_attempt_journal,
    )

    candidate = tmp_path / "national_v143"
    candidate.mkdir()
    (candidate / "policy.py").write_text("VALUE = 1\n")
    checkpoint = _checkpoint()
    first = build_review_attempt_receipt(
        checkpoint,
        gate_payload=_gate(approved=False, slot="review"),
        candidate_dir=candidate,
        attempt=1,
        authority_slot="review",
        review_semantic_contract_digest="c" * 64,
    )
    tampered = deepcopy(first)
    tampered["approved"] = True
    checkpoint["review_attempt_journal"] = [tampered]
    errors = validate_review_attempt_journal(
        checkpoint,
        candidate_dir=candidate,
        review_semantic_contract_digest="c" * 64,
    )
    assert "review_attempt_1_receipt_digest_invalid" in errors
    assert "review_attempt_1_approved_mismatch" in errors

    from checkpoint_schema import checkpoint_epoch_errors

    checkpoint_errors = checkpoint_epoch_errors(checkpoint)
    assert "checkpoint_review_attempt_1_receipt_digest_invalid" in checkpoint_errors

    with pytest.raises(ReviewRetryError, match="journal_invalid"):
        review_attempt_action([first, first, first])


def test_new_quality_cycle_preserves_old_receipts_but_starts_at_attempt_one(tmp_path):
    from reviewer_retry import (
        build_review_attempt_receipt,
        current_review_attempts,
        review_attempt_action,
    )

    candidate = tmp_path / "national_v143"
    candidate.mkdir()
    policy = candidate / "policy.py"
    policy.write_text("VALUE = 1\n")
    checkpoint = _checkpoint()
    first = build_review_attempt_receipt(
        checkpoint,
        gate_payload=_gate(approved=False, slot="review"),
        candidate_dir=candidate,
        attempt=1,
        authority_slot="review",
        review_semantic_contract_digest="d" * 64,
    )
    checkpoint["review_attempt_journal"] = [first]

    # Worker rework and a fresh Quality receipt create a new content cycle;
    # the old attempt stays immutable but cannot count against the new cycle.
    policy.write_text("VALUE = 2\n")
    checkpoint["gate_results"]["quality"]["marker"] = "q2"
    current = current_review_attempts(
        checkpoint,
        candidate_dir=candidate,
        review_semantic_contract_digest="d" * 64,
    )
    assert current == []
    assert review_attempt_action(current) == {"action": "dispatch", "attempt": 1}
