from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from bot_artifact import canonical_digest
from evaluation_contract import ALWAYS_CRITICAL_EXACT, CONTRACT_VERSION
import official_bootstrap


CONTROL_ID = "first_strict_control_v1"


def _binding(tmp_path: Path) -> dict:
    payload = {
        "schema_version": 1,
        "kind": "official-first-strict-candidate-binding",
        "epoch": "national_tcp_policy_v1",
        "candidate": str((tmp_path / "bots" / "national_v143").resolve()),
        "candidate_label": "national_v143",
        "candidate_version": 143,
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
        "candidate_version": 143,
        "source_version": 142,
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
        "label": "national_v143",
        "version": 143,
        "source_version_authority": 142,
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
        tmp_path / "bots" / "national_v143",
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
