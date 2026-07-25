from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from bot_artifact import canonical_digest
from bot_namespace import bot_name
from conftest import STRICT_SOURCE_V, STRICT_TARGET_V, strict_bot_name
from evaluation_contract import ALWAYS_CRITICAL_EXACT, CONTRACT_VERSION
import official_bootstrap


CONTROL_ID = "first_strict_control_v1"


def _binding(tmp_path: Path) -> dict:
    payload = {
        "schema_version": 1,
        "kind": "official-first-strict-candidate-binding",
        "epoch": "national_tcp_policy_v1",
        "candidate": str((tmp_path / "bots" / strict_bot_name()).resolve()),
        "candidate_label": strict_bot_name(),
        "candidate_version": STRICT_TARGET_V,
        "candidate_hash": "a" * 64,
        "source_artifact_inherited": False,
    }
    return {**payload, "candidate_binding_digest": canonical_digest(payload)}


def _control_receipt() -> dict:
    # The receipt validator is tested in its owning module.  Bootstrap tests
    # use a compact digest-bound projection to isolate formal selection logic.
    payload = {
        "schema_version": 1,
        "kind": "system-first-strict-control-receipt",
        "candidate_version": STRICT_TARGET_V,
        "source_version": STRICT_SOURCE_V,
        "active_policy_bots": [],
        "control": {
            "identity_digest": "b" * 64,
            "artifact_hash": "c" * 64,
        },
    }
    return {**payload, "receipt_digest": canonical_digest(payload)}


def test_policy_admits_only_current_control_for_v143_and_retires_v141_execution():
    policy = official_bootstrap.load_first_strict_bootstrap_policy()

    assert policy["candidate"] == {
        "label": strict_bot_name(),
        "version": STRICT_TARGET_V,
        "source_version_authority": STRICT_SOURCE_V,
    }
    assert policy["control"]["control_id"] == CONTROL_ID
    assert policy["control"]["normal_official_opponent"] is False
    assert policy["control"]["strength_admitted"] is False
    assert policy["control"]["rating_eligible"] is False
    assert policy["historical_v141_root"] == {
        "status": "retired_validation_history_only",
        "executable": False,
        "selectable": False,
    }


def test_old_root_manifest_is_not_an_active_evaluation_input():
    assert CONTRACT_VERSION >= 21
    assert "web/core/official_bootstrap_control.json" in ALWAYS_CRITICAL_EXACT
    assert "web/core/official_bootstrap_roots.json" not in ALWAYS_CRITICAL_EXACT


def test_selection_is_content_bound_non_strength_and_one_time(tmp_path, monkeypatch):
    binding = _binding(tmp_path)
    receipt = _control_receipt()
    monkeypatch.setattr(
        official_bootstrap,
        "control_identity",
        lambda *_args, **_kwargs: {
            "path": str((tmp_path / "control").resolve()),
            "artifact_hash": "c" * 64,
        },
    )
    monkeypatch.setattr(
        official_bootstrap,
        "materialize_control",
        lambda: tmp_path / "control",
    )
    monkeypatch.setattr(
        official_bootstrap,
        "_policy_identity",
        lambda: {
            "path": "web/core/official_bootstrap_control.json",
            "file_sha256": "d" * 64,
            "contract_digest": "e" * 64,
            "policy_id": "official-first-strict-control-bootstrap-v1",
            "epoch": "national_tcp_policy_v1",
        },
    )

    selection = official_bootstrap._expected_selection(binding, receipt, [])

    assert selection["reason"] == "first_strict_control_bootstrap"
    assert selection["bootstrap_control_id"] == CONTROL_ID
    assert selection["opponent"]["normal_official_opponent"] is False
    assert selection["opponent"]["strength_admitted"] is False
    assert selection["opponent"]["rating_eligible"] is False
    auth = selection["bootstrap_control_receipt"]
    assert auth["candidate_binding"] == binding
    assert auth["control_artifact_hash"] == "c" * 64

    from official_certification import stable_official_opponent_selection

    stable = stable_official_opponent_selection(selection)
    assert stable["opponent"]["authority"] == "system_first_strict_control"
    assert stable["opponent"]["normal_official_opponent"] is False
    assert stable["opponent"]["strength_admitted"] is False
    assert stable["opponent"]["rating_eligible"] is False

    consumed_entry = {
        "entry_digest": "f" * 64,
        "bootstrap_control_id": CONTROL_ID,
        "bootstrap_control_receipt_digest": auth["receipt_digest"],
        "outcome": "official-certified",
        "policy_id": "official-full-v5",
        "mode": "full",
        "authoritative": True,
        "blocking": False,
        "classification": "pass",
    }
    replay = official_bootstrap._expected_selection(
        binding, receipt, [consumed_entry]
    )
    assert replay["consumption"]["consumed"] is True
    assert replay["consumption"]["successful_count"] == 1


def test_tampered_selection_receipt_is_rejected(tmp_path, monkeypatch):
    binding = _binding(tmp_path)
    receipt = _control_receipt()
    monkeypatch.setattr(
        official_bootstrap,
        "_candidate_binding",
        lambda *_args, **_kwargs: (binding, []),
    )
    monkeypatch.setattr(
        official_bootstrap,
        "load_first_strict_bootstrap_policy",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        official_bootstrap,
        "validate_control_receipt",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        official_bootstrap,
        "control_identity",
        lambda *_args, **_kwargs: {
            "path": str((tmp_path / "control").resolve()),
            "artifact_hash": "c" * 64,
        },
    )
    monkeypatch.setattr(
        official_bootstrap,
        "materialize_control",
        lambda: tmp_path / "control",
    )
    monkeypatch.setattr(
        official_bootstrap,
        "_policy_identity",
        lambda: {
            "path": "policy",
            "file_sha256": "d" * 64,
            "contract_digest": "e" * 64,
            "policy_id": "official-first-strict-control-bootstrap-v1",
            "epoch": "national_tcp_policy_v1",
        },
    )
    selection = official_bootstrap._expected_selection(binding, receipt, [])
    from official_certification import stable_official_opponent_selection

    stable = stable_official_opponent_selection(selection)
    stable_result = official_bootstrap.validate_first_strict_control_selection_from_entries(
        stable,
        CONTROL_ID,
        binding["candidate"],
        [],
    )
    assert stable_result["valid"] is True

    tampered = deepcopy(selection)
    tampered["opponent"]["artifact_hash"] = "0" * 64

    result = official_bootstrap.validate_first_strict_control_selection_from_entries(
        tampered,
        CONTROL_ID,
        binding["candidate"],
        [],
    )

    assert result["valid"] is False
    assert "official_bootstrap_control_selection_receipt_mismatch" in result["issues"]


def test_unknown_or_historical_control_id_is_never_selectable(tmp_path):
    result = official_bootstrap.select_first_strict_control(
        "national-v141-official-full-v5-signed-ledger-root",
        tmp_path / "bots" / strict_bot_name(),
        checkpoint={},
    )
    assert result["selected"] is False
    assert result["reason"] == "official_bootstrap_control_unknown"


def test_active_module_contains_no_archive_bot_resolution():
    source = Path(official_bootstrap.__file__).read_text(encoding="utf-8")
    assert "published_bot_identity" not in source
    assert "historical_bootstrap_root_binding" not in source
    assert "national_protocol_quarantine" not in source
    assert "ROOT / \"bots\" / str(root" not in source


def test_completed_authorization_accepts_production_normalized_selection(
    tmp_path,
    monkeypatch,
):
    candidate = (tmp_path / "bots" / strict_bot_name()).resolve()
    authorization = {
        "kind": "official-first-strict-control-operator-authorization",
        "authorization_digest": "d" * 64,
    }
    stable_selection = {
        "selected": True,
        "eligible": True,
        "reason": "first_strict_control_bootstrap",
        "kind": "official-first-strict-control-selection",
        "bootstrap_control_id": CONTROL_ID,
        "candidate": str(candidate),
        "candidate_binding": {"candidate_hash": "a" * 64},
        "bootstrap_control_receipt": {"receipt_digest": "b" * 64},
        "operator_bootstrap_authorization": authorization,
        "opponent": {
            "bot": CONTROL_ID,
            "path": str((tmp_path / "control").resolve()),
            "artifact_hash": "c" * 64,
            "tag": None,
            "tag_object": None,
            "eligible": True,
            "reason": "first_strict_control_bootstrap",
            "eligibility_receipt": {"receipt_digest": "e" * 64},
            "authority": "system-owned-first-strict-control",
            "normal_official_opponent": False,
            "strength_admitted": False,
            "rating_eligible": False,
        },
    }
    production_selection = deepcopy(stable_selection)
    production_selection["consumption"] = {
        "consumed": True,
        "successful_count": 1,
    }
    production_selection["considered"] = [{"eligible": True}]
    ledger_entry = {
        "entry_digest": "f" * 64,
        "bootstrap_control_id": CONTROL_ID,
        "bootstrap_control_receipt_digest": "b" * 64,
        "outcome": "official-certified",
        "policy_id": "official-full-v5",
        "mode": "full",
        "authoritative": True,
        "blocking": False,
        "classification": "pass",
        "certificate_digest": "1" * 64,
    }
    status = {
        "status": "official-certified",
        "mode": "full",
        "policy_id": "official-full-v5",
        "certificate_digest": "1" * 64,
        "certification_identity": {
            "schema_version": 1,
            "candidate_hash": "a" * 64,
            "spec": {
                "bootstrap_control_id": CONTROL_ID,
                "candidate": str(candidate),
            },
        },
        "opponent_selection": production_selection,
        "official_job_envelope": {"opponent_selection": stable_selection},
        "official_verdict_ledger_entry": ledger_entry,
    }
    checkpoint = {
        "stage": "official_bootstrap_required",
        "audit_context": {"official_bootstrap_request": {}},
    }
    observed_fact_stages = []

    def current_facts(*_args, checkpoint, expected_stage, **_kwargs):
        observed_fact_stages.append((checkpoint.get("stage"), expected_stage))
        return {}, []

    monkeypatch.setattr(
        official_bootstrap,
        "_current_operator_bootstrap_facts",
        current_facts,
    )
    monkeypatch.setattr(
        official_bootstrap,
        "_parked_request_issues",
        lambda *_a, **_k: [],
    )
    monkeypatch.setattr(
        official_bootstrap,
        "_operator_authorization",
        lambda *_a, **_k: authorization,
    )
    monkeypatch.setattr(
        official_bootstrap,
        "validate_first_strict_control_selection",
        lambda *_a, **_k: {"valid": True, "issues": []},
    )
    monkeypatch.setattr(
        official_bootstrap,
        "_validated_ledger_entries",
        lambda: ([ledger_entry], []),
    )

    result = official_bootstrap.validate_completed_operator_bootstrap_authorization(
        status,
        candidate,
        checkpoint=checkpoint,
    )

    assert result["valid"] is True
    assert result["issues"] == []
    assert checkpoint == {
        "stage": "official_bootstrap_required",
        "audit_context": {"official_bootstrap_request": {}},
    }
    assert observed_fact_stages[-1] == (
        "official_bootstrap_required",
        "official_bootstrap_required",
    )

    persisted_by_stage = {}
    for post_certificate_stage in ("verified", "publishing"):
        persisted = deepcopy(checkpoint)
        persisted["stage"] = post_certificate_stage
        persisted["gate_results"] = {
            "official_full": {
                "passed": True,
                "bootstrap_certificate": True,
                "status": deepcopy(status),
                "certificate_digest": status["certificate_digest"],
                "certification_identity": deepcopy(
                    status["certification_identity"]
                ),
                "completed_bootstrap_authorization": deepcopy(result),
            }
        }
        before_rebind = deepcopy(persisted)
        rebound = (
            official_bootstrap.validate_completed_operator_bootstrap_authorization(
                status,
                candidate,
                checkpoint=persisted,
            )
        )
        assert rebound == result
        assert persisted == before_rebind
        persisted_by_stage[post_certificate_stage] = persisted
        assert observed_fact_stages[-1] == (
            "official_bootstrap_required",
            "official_bootstrap_required",
        )

    unbound_verified = deepcopy(checkpoint)
    unbound_verified["stage"] = "verified"
    rejected_unbound = (
        official_bootstrap.validate_completed_operator_bootstrap_authorization(
            status,
            candidate,
            checkpoint=unbound_verified,
        )
    )
    assert rejected_unbound["valid"] is False
    assert (
        "official_bootstrap_completed_checkpoint_gate_missing"
        in rejected_unbound["issues"]
    )

    drifted_verified = deepcopy(persisted_by_stage["verified"])
    drifted_verified["gate_results"]["official_full"][
        "certificate_digest"
    ] = "9" * 64
    rejected_drift = (
        official_bootstrap.validate_completed_operator_bootstrap_authorization(
            status,
            candidate,
            checkpoint=drifted_verified,
        )
    )
    assert rejected_drift["valid"] is False
    assert (
        "official_bootstrap_completed_checkpoint_gate_certificate_mismatch"
        in rejected_drift["issues"]
    )

    authorization_drift = deepcopy(persisted_by_stage["verified"])
    authorization_drift["gate_results"]["official_full"][
        "completed_bootstrap_authorization"
    ]["ledger_entry_digest"] = "8" * 64
    rejected_authorization = (
        official_bootstrap.validate_completed_operator_bootstrap_authorization(
            status,
            candidate,
            checkpoint=authorization_drift,
        )
    )
    assert rejected_authorization["valid"] is False
    assert (
        "official_bootstrap_completed_checkpoint_authorization_mismatch"
        in rejected_authorization["issues"]
    )

    def reject_gate_drift(
        drifted_checkpoint,
        expected_issue,
    ):
        unchanged = deepcopy(drifted_checkpoint)
        rejected = (
            official_bootstrap.validate_completed_operator_bootstrap_authorization(
                status,
                candidate,
                checkpoint=drifted_checkpoint,
            )
        )
        assert rejected["valid"] is False
        assert expected_issue in rejected["issues"]
        assert drifted_checkpoint == unchanged

    not_passed = deepcopy(persisted_by_stage["verified"])
    not_passed["gate_results"]["official_full"]["passed"] = False
    reject_gate_drift(
        not_passed,
        "official_bootstrap_completed_checkpoint_gate_not_passed",
    )

    not_bootstrap = deepcopy(persisted_by_stage["verified"])
    not_bootstrap["gate_results"]["official_full"][
        "bootstrap_certificate"
    ] = False
    reject_gate_drift(
        not_bootstrap,
        "official_bootstrap_completed_checkpoint_gate_not_bootstrap",
    )

    status_drift = deepcopy(persisted_by_stage["verified"])
    status_drift["gate_results"]["official_full"]["status"][
        "certificate_digest"
    ] = "2" * 64
    reject_gate_drift(
        status_drift,
        "official_bootstrap_completed_checkpoint_gate_status_mismatch",
    )

    identity_drift = deepcopy(persisted_by_stage["verified"])
    identity_drift["gate_results"]["official_full"][
        "certification_identity"
    ]["candidate_hash"] = "2" * 64
    reject_gate_drift(
        identity_drift,
        "official_bootstrap_completed_checkpoint_gate_identity_mismatch",
    )

    status_boolean_type_drift = deepcopy(persisted_by_stage["verified"])
    status_boolean_type_drift["gate_results"]["official_full"]["status"][
        "opponent_selection"
    ]["selected"] = 1
    reject_gate_drift(
        status_boolean_type_drift,
        "official_bootstrap_completed_checkpoint_gate_status_mismatch",
    )

    identity_number_type_drift = deepcopy(persisted_by_stage["verified"])
    identity_number_type_drift["gate_results"]["official_full"][
        "certification_identity"
    ]["schema_version"] = 1.0
    reject_gate_drift(
        identity_number_type_drift,
        "official_bootstrap_completed_checkpoint_gate_identity_mismatch",
    )

    malformed_gate_results = deepcopy(persisted_by_stage["verified"])
    malformed_gate_results["gate_results"] = []
    reject_gate_drift(
        malformed_gate_results,
        "official_bootstrap_completed_checkpoint_gate_results_malformed",
    )

    malformed_gate = deepcopy(persisted_by_stage["verified"])
    malformed_gate["gate_results"]["official_full"] = []
    reject_gate_drift(
        malformed_gate,
        "official_bootstrap_completed_checkpoint_gate_malformed",
    )

    authorization_unknown_field = deepcopy(persisted_by_stage["verified"])
    authorization_unknown_field["gate_results"]["official_full"][
        "completed_bootstrap_authorization"
    ]["unknown_field"] = "must-fail-closed"
    reject_gate_drift(
        authorization_unknown_field,
        "official_bootstrap_completed_checkpoint_authorization_mismatch",
    )

    authorization_missing_field = deepcopy(persisted_by_stage["verified"])
    authorization_missing_field["gate_results"]["official_full"][
        "completed_bootstrap_authorization"
    ].pop("reason")
    reject_gate_drift(
        authorization_missing_field,
        "official_bootstrap_completed_checkpoint_authorization_mismatch",
    )

    authorization_type_drift = deepcopy(persisted_by_stage["verified"])
    authorization_type_drift["gate_results"]["official_full"][
        "completed_bootstrap_authorization"
    ]["valid"] = 1
    reject_gate_drift(
        authorization_type_drift,
        "official_bootstrap_completed_checkpoint_authorization_mismatch",
    )

    malformed_identity_status = deepcopy(status)
    malformed_identity_status["certification_identity"] = []
    malformed_identity = (
        official_bootstrap.validate_completed_operator_bootstrap_authorization(
            malformed_identity_status,
            candidate,
            checkpoint=checkpoint,
        )
    )
    assert malformed_identity["valid"] is False
    assert (
        "official_bootstrap_completed_identity_malformed"
        in malformed_identity["issues"]
    )

    for malformed_spec in (["not-an-object"], 1):
        malformed_spec_status = deepcopy(status)
        malformed_spec_status["certification_identity"]["spec"] = (
            malformed_spec
        )
        before_malformed_spec = deepcopy(malformed_spec_status)
        malformed_spec_result = (
            official_bootstrap.validate_completed_operator_bootstrap_authorization(
                malformed_spec_status,
                candidate,
                checkpoint=checkpoint,
            )
        )
        assert malformed_spec_result["valid"] is False
        assert (
            "official_bootstrap_completed_identity_spec_malformed"
            in malformed_spec_result["issues"]
        )
        assert malformed_spec_status == before_malformed_spec

    wrong_stage = deepcopy(checkpoint)
    wrong_stage["stage"] = "reviewed"
    rejected_stage = (
        official_bootstrap.validate_completed_operator_bootstrap_authorization(
            status,
            candidate,
            checkpoint=wrong_stage,
        )
    )
    assert rejected_stage["valid"] is False
    assert any(
        issue.startswith(
            "official_bootstrap_completed_checkpoint_stage_mismatch:"
        )
        for issue in rejected_stage["issues"]
    )

    envelope_drift = deepcopy(status)
    envelope_drift["official_job_envelope"]["opponent_selection"] = (
        production_selection
    )

    rejected = official_bootstrap.validate_completed_operator_bootstrap_authorization(
        envelope_drift,
        candidate,
        checkpoint=checkpoint,
    )

    assert rejected["valid"] is False
    assert "official_bootstrap_completed_envelope_selection_mismatch" in rejected[
        "issues"
    ]
