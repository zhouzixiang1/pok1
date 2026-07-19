from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from bot_artifact import canonical_digest
from scripts import recover_completed_first_strict_publication as recovery


def _snapshot(tmp_path: Path) -> dict:
    candidate = tmp_path / "bots" / "national_v143"
    candidate.mkdir(parents=True)
    parked_payload = {
        "schema_version": 1,
        "kind": "official-first-strict-control-parked-request",
        "candidate_path": str(candidate.resolve()),
        "candidate_label": "national_v143",
        "candidate_version": 143,
        "candidate_hash": "a" * 64,
        "source_v": 142,
        "workflow_run_id": "generation:143:workflow-v68",
        "checkpoint_contract_digest": "b" * 64,
        "evaluation_contract_version": 42,
        "evaluation_contract_hash": "c" * 64,
        "protocol_bootstrap_receipt": {"receipt_digest": "d" * 64},
        "protocol_bootstrap_receipt_digest": "d" * 64,
        "first_strict_control_receipt": {"receipt_digest": "e" * 64},
        "first_strict_control_receipt_digest": "e" * 64,
        "active_bots": [],
        "strict_published_bots": [],
        "bootstrap_control_id": "first_strict_control_v1",
        "bootstrap_policy_digest": "f" * 64,
    }
    parked = {
        **parked_payload,
        "request_digest": canonical_digest(parked_payload),
    }
    ledger_entry = {
        "entry_digest": "1" * 64,
        "certificate_digest": "2" * 64,
    }
    status = {
        "status": "official-certified",
        "mode": "full",
        "policy_id": "official-full-v5",
        "certificate_digest": "2" * 64,
        "certification_identity": {
            "candidate_hash": "a" * 64,
            "spec": {
                "candidate": str(candidate.resolve()),
                "bootstrap_control_id": "first_strict_control_v1",
            },
        },
        "official_verdict_ledger_entry": ledger_entry,
    }
    checkpoint = {
        "next_v": 143,
        "source_v": 142,
        "stage": "verified",
        "workflow_run_id": "generation:143:workflow-v68",
        "checkpoint_revision": 24,
        "publication_intent": None,
        "official_job": None,
        "audit_context": {"official_bootstrap_request": parked},
        "gate_results": {
            "official_full": {
                "passed": True,
                "status": deepcopy(status),
                "certificate_digest": "2" * 64,
                "certification_identity": deepcopy(
                    status["certification_identity"]
                ),
                "completed_bootstrap_authorization": {
                    "valid": True,
                    "candidate_hash": "a" * 64,
                    "certificate_digest": "2" * 64,
                    "ledger_entry_digest": "1" * 64,
                },
            }
        },
    }
    return {
        "checkpoint": checkpoint,
        "status": status,
        "candidate": candidate,
        "candidate_hash": "a" * 64,
        "certificate_digest": "2" * 64,
        "ledger_entry_digest": "1" * 64,
        "authorization": {
            "valid": True,
            "candidate_hash": "a" * 64,
            "certificate_digest": "2" * 64,
            "ledger_entry_digest": "1" * 64,
        },
        "gate_ledger": {
            "ok": True,
            "missing_gates": [],
            "failed_gates": [],
            "current_code_fingerprint": "a" * 64,
        },
        "active_bots": [],
        "strict_bots": [],
        "completion_tags": [],
        "completed": False,
        "official_jobs_active": False,
    }


def _issues(snapshot: dict) -> list[str]:
    return recovery.publication_recovery_snapshot_issues(**snapshot)


def test_verified_completed_certificate_snapshot_is_exact(tmp_path):
    assert _issues(_snapshot(tmp_path)) == []


def test_recovery_additions_are_not_verified_evaluation_contract_inputs():
    from evaluation_contract import build_evaluation_contract, is_contract_path

    contract = build_evaluation_contract(
        recovery.ROOT,
        candidate_v=143,
        source_v=142,
        checkpoint={"stage": "verified"},
        stage="verified",
    )

    assert recovery.EXPECTED_CHANGED_PATHS == {
        recovery.RECOVERY_SCRIPT,
        recovery.RECOVERY_TEST,
    }
    assert all(
        not is_contract_path(path, contract)
        for path in recovery.EXPECTED_CHANGED_PATHS
    )


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (
            lambda value: value["checkpoint"].update(stage="reviewed"),
            "first_strict_publication_recovery_checkpoint_stage_mismatch",
        ),
        (
            lambda value: value["checkpoint"]["audit_context"].pop(
                "official_bootstrap_request"
            ),
            "first_strict_publication_recovery_parked_request_missing",
        ),
        (
            lambda value: value["checkpoint"]["gate_results"][
                "official_full"
            ].update(passed=False),
            "first_strict_publication_recovery_official_gate_not_passed",
        ),
        (
            lambda value: value["checkpoint"]["gate_results"][
                "official_full"
            ].update(certificate_digest="9" * 64),
            "first_strict_publication_recovery_gate_certificate_mismatch",
        ),
        (
            lambda value: value["checkpoint"]["gate_results"][
                "official_full"
            ].update(status={}),
            "first_strict_publication_recovery_gate_status_mismatch",
        ),
        (
            lambda value: value.update(active_bots=["national_v143"]),
            "first_strict_publication_recovery_active_pool_not_empty",
        ),
        (
            lambda value: value.update(strict_bots=["national_v143"]),
            "first_strict_publication_recovery_strict_pool_not_empty",
        ),
        (
            lambda value: value.update(
                completion_tags=["national-bot-v143"]
            ),
            "first_strict_publication_recovery_completion_tag_present",
        ),
    ],
)
def test_verified_recovery_snapshot_fails_closed(
    tmp_path,
    mutation,
    expected,
):
    snapshot = _snapshot(tmp_path)
    mutation(snapshot)
    assert expected in _issues(snapshot)


def test_publishing_recovery_requires_matching_receipt_and_intent(tmp_path):
    claim = {"schema_version": 1, "kind": recovery.CLAIM_KIND}
    claim["claim_digest"] = canonical_digest(claim)
    receipt_payload = {
        "schema_version": recovery.TERMINAL_RECEIPT_SCHEMA_VERSION,
        "kind": recovery.TERMINAL_RECEIPT_KIND,
        "claim_digest": claim["claim_digest"],
        "publication_id": "3" * 64,
    }
    receipt = {
        **receipt_payload,
        "receipt_digest": canonical_digest(receipt_payload),
    }
    checkpoint = {
        "next_v": 143,
        "source_v": 142,
        "stage": "publishing",
        "workflow_run_id": "generation:143:workflow-v68",
        "audit_context": {
            recovery.CHECKPOINT_RECEIPT_KEY: receipt,
        },
        "publication_intent": {
            "version": 143,
            "source_v": 142,
            "workflow_run_id": "generation:143:workflow-v68",
            "publication_id": "3" * 64,
            "strategy_tag": recovery.receipt_commit_line(
                claim["claim_digest"]
            ),
        },
    }

    assert recovery.publishing_recovery_issues(
        checkpoint, claim["claim_digest"]
    ) == []

    missing = deepcopy(checkpoint)
    missing["publication_intent"] = None
    assert (
        "first_strict_publication_recovery_intent_missing"
        in recovery.publishing_recovery_issues(
            missing, claim["claim_digest"]
        )
    )

    mismatched = deepcopy(checkpoint)
    mismatched["publication_intent"]["strategy_tag"] = "other"
    assert (
        "first_strict_publication_recovery_intent_receipt_mismatch"
        in recovery.publishing_recovery_issues(
            mismatched, claim["claim_digest"]
        )
    )


def test_double_validation_uses_real_verified_checkpoint_persistence(
    tmp_path,
    monkeypatch,
):
    """Regression for parked validation -> persisted pass -> verified rebind."""

    import checkpoint_schema
    import evolution_infra
    import official_bootstrap
    import tool_commit

    state_path = tmp_path / "pipeline_state.json"
    parked = _snapshot(tmp_path)["checkpoint"]
    parked["stage"] = "official_bootstrap_required"
    parked["checkpoint_revision"] = 23
    parked.update({
        "checkpoint_schema_version": 2,
        "evaluation_epoch": "national_tcp_policy_v1",
        "epoch_binding": {},
        "repo_baseline": {},
        "workflow_profile_id": "national_tcp_policy_v1",
        "national_execution_mode": "native_tcp",
    })
    state_path.write_text(json.dumps(parked), encoding="utf-8")
    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", state_path)
    monkeypatch.setattr(
        evolution_infra,
        "checkpoint_allocation_authority",
        lambda **_kwargs: {
            "published_high_water": 142,
            "abandoned_receipt_floor": 0,
            "abandoned_receipt_head_digest": None,
            "allocation_floor": 142,
        },
    )
    monkeypatch.setattr(
        evolution_infra,
        "_capture_repo_baseline",
        lambda stage, **_kwargs: {
            "head": "baseline",
            "evaluation_contract": {"stage": stage, "hash": "c" * 64},
        },
    )
    monkeypatch.setattr(checkpoint_schema, "checkpoint_epoch_errors", lambda *_a, **_k: [])
    monkeypatch.setattr(
        checkpoint_schema,
        "live_checkpoint_allocation_authority_errors",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        checkpoint_schema,
        "live_checkpoint_parent_authority_errors",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        tool_commit,
        "write_pipeline_checkpoint",
        evolution_infra.write_pipeline_checkpoint,
    )

    facts = {}

    def stage_bound_facts(_candidate, _control, *, checkpoint, expected_stage, **_kwargs):
        if checkpoint.get("stage") != expected_stage:
            return facts, [
                "official_bootstrap_checkpoint_stage_mismatch:"
                f"expected={expected_stage}:actual={checkpoint.get('stage')}"
            ]
        return facts, []

    monkeypatch.setattr(
        official_bootstrap,
        "_current_operator_bootstrap_facts",
        stage_bound_facts,
    )
    monkeypatch.setattr(official_bootstrap, "_parked_request_issues", lambda *_a: [])
    monkeypatch.setattr(
        official_bootstrap,
        "_operator_authorization",
        lambda *_a: {},
    )
    monkeypatch.setattr(
        official_bootstrap,
        "validate_first_strict_control_selection",
        lambda *_a, **_k: {"valid": True, "issues": []},
    )
    monkeypatch.setattr(
        official_bootstrap,
        "_validated_ledger_entries",
        lambda: ([], []),
    )

    assert tool_commit._record_official_full_pass_checkpoint(
        143,
        142,
        parked,
        {
            "passed": True,
            "bootstrap_certificate": True,
            "status": {"status": "official-certified"},
        },
    ) is True
    verified = evolution_infra.read_pipeline_checkpoint()
    assert verified["stage"] == "verified"
    assert verified["checkpoint_revision"] == 24

    calls = []

    def completed_validator(_status, _candidate, *, checkpoint):
        calls.append(checkpoint["stage"])
        return {
            "valid": checkpoint["stage"] == "official_bootstrap_required",
            "issues": [] if checkpoint["stage"] == "official_bootstrap_required" else ["stage"],
        }

    monkeypatch.setattr(
        official_bootstrap,
        "validate_completed_operator_bootstrap_authorization",
        completed_validator,
    )
    result = recovery.validate_completed_at_parked_authority(
        {}, tmp_path / "bots" / "national_v143", verified
    )

    assert result["valid"] is True
    assert calls == ["official_bootstrap_required"]
    assert evolution_infra.read_pipeline_checkpoint()["stage"] == "verified"
