from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess

import pytest

from bot_artifact import canonical_digest
import official_bootstrap
import official_certificate_signing
import official_certification
import official_verdict_ledger
from evaluation_contract import ALWAYS_CRITICAL_EXACT
from evolution_scope import CRITICAL_EVALUATION_GATE_EXACT


ROOT_ID = "national-v141-official-full-v5-signed-ledger-root"


def _root_and_entry():
    manifest = official_bootstrap.load_signed_v5_ledger_bootstrap_roots()
    root = next(item for item in manifest["roots"] if item["root_id"] == ROOT_ID)
    return root, dict(root["ledger_entry"])


def _identity(root, root_path: Path):
    return {
        "label": root["bot"],
        "version": root["version"],
        "path": str(root_path),
        "artifact_hash": root["artifact_hash"],
        "tag": root["tag"],
        "tag_object": root["tag_object"],
        "completion_tree_oid": root["completion_tree_oid"],
        "published": True,
        "issues": [],
    }


def _root_runtime(monkeypatch, tmp_path):
    root, entry = _root_and_entry()
    root_path = tmp_path / root["bot"]
    root_path.mkdir()
    (root_path / ".completed").write_text("ok\n", encoding="utf-8")
    (root_path / "national_bot.py").write_text("# native\n", encoding="utf-8")
    monkeypatch.setattr(official_bootstrap, "_root_path", lambda _root: root_path)
    monkeypatch.setattr(
        official_bootstrap,
        "published_bot_identity",
        lambda path: (
            _identity(root, root_path)
            if Path(path).resolve() == root_path.resolve()
            else {"published": False, "issues": ["missing_annotated_completion_tag"]}
        ),
    )
    monkeypatch.setattr(
        official_bootstrap,
        "epoch_lifecycle_eligibility",
        lambda version: {"eligible": True, "version": version},
    )
    monkeypatch.setattr(official_bootstrap, "_native_contract_errors", lambda _path: [])
    monkeypatch.setattr(official_bootstrap, "ledger_integrity", lambda: {"valid": True, "issues": []})
    monkeypatch.setattr(
        official_bootstrap,
        "_validated_ledger_entries",
        lambda: ([entry], []),
    )
    return root, entry


def test_configured_v141_root_selects_only_with_exact_signed_entry(monkeypatch, tmp_path):
    root, _entry = _root_runtime(monkeypatch, tmp_path)
    candidate = tmp_path / "national_v143"
    candidate.mkdir()
    (candidate / "national_bot.py").write_text("# native\n", encoding="utf-8")
    monkeypatch.setattr(official_bootstrap, "hash_path", lambda _path: "a" * 64)

    selected = official_bootstrap.select_signed_v5_ledger_bootstrap_root(
        root["root_id"], candidate
    )

    assert selected["selected"] is True
    assert selected["reason"] == "signed_v5_ledger_bootstrap_root"
    assert selected["opponent"]["bot"] == "national_v141"
    receipt = selected["bootstrap_root_receipt"]
    assert receipt["ledger_entry_digest"] == root["ledger_entry"]["entry_digest"]
    assert receipt["receipt_digest"]
    assert selected["candidate_binding"]["candidate_hash"] == "a" * 64


def test_locked_bootstrap_validator_uses_supplied_entries_without_relocking(
    monkeypatch, tmp_path
):
    root, entry = _root_runtime(monkeypatch, tmp_path)
    candidate = tmp_path / "national_v143"
    candidate.mkdir()
    (candidate / "national_bot.py").write_text("# native\n", encoding="utf-8")
    monkeypatch.setattr(official_bootstrap, "hash_path", lambda _path: "a" * 64)
    selected = official_bootstrap.select_signed_v5_ledger_bootstrap_root(
        root["root_id"], candidate
    )
    monkeypatch.setattr(
        official_bootstrap,
        "_validated_ledger_entries",
        lambda: (_ for _ in ()).throw(AssertionError("nested ledger lock")),
    )

    validation = (
        official_bootstrap.validate_signed_v5_ledger_bootstrap_selection_from_entries(
            selected,
            root["root_id"],
            candidate,
            [entry],
            allow_consumed=False,
        )
    )

    assert validation["valid"] is True
    assert validation["issues"] == []


def test_bootstrap_manifest_is_cross_bound_to_retired_signer_policy(monkeypatch, tmp_path):
    policy = official_certificate_signing.load_signer_trust_policy()
    policy["historical_signers"][0]["historical_chain"]["candidate_hash"] = "f" * 64
    policy["policy_digest"] = official_certificate_signing._policy_digest(policy)
    policy_path = tmp_path / "mismatched-policy.json"
    policy_path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
    monkeypatch.setattr(
        official_certificate_signing, "DEFAULT_TRUST_POLICY", policy_path
    )

    with pytest.raises(
        official_bootstrap.BootstrapRootConfigurationError,
        match="bootstrap root signer policy mismatch.*candidate_hash",
    ):
        official_bootstrap.load_signed_v5_ledger_bootstrap_roots()


def test_selector_fails_closed_on_published_tag_or_tree_mismatch(monkeypatch, tmp_path):
    root, _entry = _root_runtime(monkeypatch, tmp_path)
    root_path = tmp_path / root["bot"]
    bad_identity = _identity(root, root_path)
    bad_identity["tag_object"] = "b" * 40
    monkeypatch.setattr(official_bootstrap, "published_bot_identity", lambda _path: bad_identity)

    selected = official_bootstrap.select_signed_v5_ledger_bootstrap_root(root["root_id"])

    assert selected["selected"] is False
    assert selected["reason"] == "bootstrap_root_identity_mismatch:tag_object"


def test_selector_fails_closed_when_signed_ledger_is_not_healthy(monkeypatch, tmp_path):
    root, _entry = _root_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(
        official_bootstrap,
        "ledger_integrity",
        lambda: {"valid": False, "issues": ["official_verdict_ledger_head_missing"]},
    )

    selected = official_bootstrap.select_signed_v5_ledger_bootstrap_root(root["root_id"])

    assert selected["selected"] is False
    assert selected["reason"] == "bootstrap_root_signed_ledger_invalid"


def test_selector_refuses_a_completed_or_published_candidate(monkeypatch, tmp_path):
    root, _entry = _root_runtime(monkeypatch, tmp_path)
    candidate = tmp_path / "national_v143"
    candidate.mkdir()
    (candidate / "national_bot.py").write_text("# native\n", encoding="utf-8")
    (candidate / ".completed").write_text("completed\n", encoding="utf-8")

    selected = official_bootstrap.select_signed_v5_ledger_bootstrap_root(root["root_id"], candidate)

    assert selected["selected"] is False
    assert selected["reason"] == "bootstrap_candidate_already_completed"


def test_successful_receipt_bound_consumption_blocks_second_bootstrap(monkeypatch, tmp_path):
    root, entry = _root_runtime(monkeypatch, tmp_path)
    receipt = official_bootstrap.build_signed_v5_ledger_bootstrap_root_receipt(root["root_id"])
    assert receipt is not None
    consumed_entry = {
        **entry,
        "sequence": 2,
        "entry_digest": "c" * 64,
        "candidate_label": "national_v143",
        "candidate_hash": "d" * 64,
        "bootstrap_root_id": root["root_id"],
        "bootstrap_root_receipt_digest": receipt["receipt_digest"],
    }
    monkeypatch.setattr(
        official_bootstrap,
        "_validated_ledger_entries",
        lambda: ([entry, consumed_entry], []),
    )

    consumption = official_bootstrap.signed_v5_ledger_bootstrap_root_consumption(root["root_id"])
    selected = official_bootstrap.select_signed_v5_ledger_bootstrap_root(root["root_id"])

    assert consumption["valid"] is True
    assert consumption["consumed"] is True
    assert consumption["successful_count"] == 1
    assert selected["selected"] is False
    assert selected["reason"] == "bootstrap_root_already_consumed"


def test_claimed_root_with_wrong_receipt_fails_closed(monkeypatch, tmp_path):
    root, entry = _root_runtime(monkeypatch, tmp_path)
    malformed = {
        **entry,
        "sequence": 2,
        "entry_digest": "e" * 64,
        "bootstrap_root_id": root["root_id"],
        "bootstrap_root_receipt_digest": "f" * 64,
    }
    monkeypatch.setattr(
        official_bootstrap,
        "_validated_ledger_entries",
        lambda: ([entry, malformed], []),
    )

    selected = official_bootstrap.select_signed_v5_ledger_bootstrap_root(root["root_id"])

    assert selected["selected"] is False
    assert selected["reason"] == "bootstrap_root_consumption_invalid"


def test_bootstrap_certificate_receipt_must_match_the_live_root_selector(monkeypatch, tmp_path):
    root, _entry = _root_runtime(monkeypatch, tmp_path)
    candidate = tmp_path / "national_v143"
    candidate.mkdir()
    (candidate / "national_bot.py").write_text("# native\n", encoding="utf-8")
    monkeypatch.setattr(official_bootstrap, "hash_path", lambda _path: "a" * 64)
    selected = official_bootstrap.select_signed_v5_ledger_bootstrap_root(root["root_id"], candidate)
    spec = official_certification.build_spec(
        "full",
        candidate,
        opponent=selected["opponent"]["path"],
        bootstrap_root_id=root["root_id"],
    )
    identity = {"opponent_hash": root["artifact_hash"]}
    stable = official_certification.stable_official_opponent_selection(selected)

    assert official_certification._opponent_selection_issues(stable, spec, identity) == []

    tampered = deepcopy(stable)
    tampered["bootstrap_root_receipt"]["artifact_hash"] = "f" * 64
    tampered["opponent"]["eligibility_receipt"] = tampered["bootstrap_root_receipt"]
    issues = official_certification._opponent_selection_issues(tampered, spec, identity)

    assert "certificate_bootstrap_root_selection_receipt_mismatch" in issues


def test_bootstrap_certificate_receipt_remains_verifiable_after_consumption(monkeypatch, tmp_path):
    root, entry = _root_runtime(monkeypatch, tmp_path)
    candidate = tmp_path / "national_v143"
    candidate.mkdir()
    (candidate / "national_bot.py").write_text("# native\n", encoding="utf-8")
    monkeypatch.setattr(official_bootstrap, "hash_path", lambda _path: "a" * 64)
    selected = official_bootstrap.select_signed_v5_ledger_bootstrap_root(root["root_id"], candidate)
    consumed_entry = {
        **entry,
        "sequence": 2,
        "entry_digest": "c" * 64,
        "candidate_label": "national_v143",
        "candidate_hash": "d" * 64,
        "bootstrap_root_id": root["root_id"],
        "bootstrap_root_receipt_digest": selected["bootstrap_root_receipt"]["receipt_digest"],
    }
    monkeypatch.setattr(
        official_bootstrap,
        "_validated_ledger_entries",
        lambda: ([entry, consumed_entry], []),
    )
    # This mirrors the post-commit certificate validation path: successful
    # bootstrap output is now published, but must remain historically valid.
    (candidate / ".completed").write_text("published\n", encoding="utf-8")
    spec = official_certification.build_spec(
        "full",
        candidate,
        opponent=selected["opponent"]["path"],
        bootstrap_root_id=root["root_id"],
    )

    issues = official_certification._opponent_selection_issues(
        official_certification.stable_official_opponent_selection(selected),
        spec,
        {"opponent_hash": root["artifact_hash"]},
        allow_consumed_bootstrap=True,
    )

    assert issues == []


def test_normal_spec_never_calls_bootstrap_selector(monkeypatch, tmp_path):
    candidate = tmp_path / "national_v143"
    opponent = tmp_path / "national_v142"
    candidate.mkdir()
    opponent.mkdir()
    spec = official_certification.build_spec("full", candidate, opponent=opponent)
    normal_selection = {
        "selected": True,
        "candidate": str(candidate.resolve()),
        "opponent": {"path": str(opponent.resolve()), "eligible": True, "reason": "official_certified"},
    }
    monkeypatch.setattr(
        official_bootstrap,
        "select_signed_v5_ledger_bootstrap_root",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("normal path used bootstrap")),
    )
    monkeypatch.setattr(
        official_certification,
        "select_official_opponent",
        lambda *_args, **_kwargs: normal_selection,
    )

    resolved, selection = official_certification.resolve_managed_certification_spec(spec)

    assert resolved is spec
    assert selection == normal_selection


def test_normal_full_identity_omits_the_none_bootstrap_field(tmp_path):
    candidate = tmp_path / "national_v143"
    opponent = tmp_path / "national_v142"
    candidate.mkdir()
    opponent.mkdir()
    spec = official_certification.build_spec("full", candidate, opponent=opponent)

    record = official_certification.spec_record(spec)

    assert spec.bootstrap_root_id is None
    assert "bootstrap_root_id" not in record


def test_bootstrap_authority_files_are_exact_evaluation_contract_inputs():
    expected = {
        "scripts/official_certify.py",
        "web/core/official_bootstrap.py",
        "web/core/official_bootstrap_roots.json",
    }

    assert expected <= ALWAYS_CRITICAL_EXACT
    assert expected <= CRITICAL_EVALUATION_GATE_EXACT

    from evaluation_contract import CONTRACT_VERSION
    from official_certification_job import JOB_SCHEMA_VERSION
    from official_execution_profile import load_execution_profile
    from official_job_envelope import JOB_ENVELOPE_SCHEMA_VERSION

    profile = load_execution_profile()
    assert CONTRACT_VERSION == 12
    assert JOB_SCHEMA_VERSION == 4
    assert JOB_ENVELOPE_SCHEMA_VERSION == 3
    assert profile["schema_version"] == 3
    assert profile["profile_id"].endswith("-v5")


def test_operator_bootstrap_facts_reject_stage_bot_hash_and_strict_pool_drift(
    monkeypatch, tmp_path
):
    import evolution_infra
    import national_protocol_quarantine as quarantine
    import tool_commit

    candidate = tmp_path / "national_v150"
    candidate.mkdir()
    (candidate / "national_bot.py").write_text("# native\n", encoding="utf-8")
    wrong_bot = tmp_path / "national_v151"
    wrong_bot.mkdir()
    (wrong_bot / "national_bot.py").write_text("# native\n", encoding="utf-8")
    candidate_hash = "a" * 64
    receipt = {
        "mode": "legacy_strategy_migration",
        "receipt_digest": "b" * 64,
    }
    checkpoint = {
        "next_v": 150,
        "source_v": 142,
        "stage": "official_bootstrap_required",
        "workflow_run_id": "generation:150:test",
        "audit_context": {"protocol_bootstrap": receipt},
        "gate_results": {"precommit_eval": {"passed": True}},
    }
    strict = []
    monkeypatch.setattr(official_bootstrap, "BOTS_DIR", tmp_path)
    monkeypatch.setattr(
        official_bootstrap,
        "_configured_root",
        lambda _root_id: ({"root_id": ROOT_ID}, None),
    )
    monkeypatch.setattr(official_bootstrap, "_completion_tag_exists", lambda _v: False)
    monkeypatch.setattr(
        official_bootstrap,
        "hash_path",
        lambda path: candidate_hash if Path(path).name == "national_v150" else "c" * 64,
    )
    monkeypatch.setattr(evolution_infra, "get_active_bots_read_only", lambda: [])
    monkeypatch.setattr(quarantine, "strict_published_bot_names", lambda: tuple(strict))
    monkeypatch.setattr(
        quarantine,
        "validate_protocol_bootstrap_receipt",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        quarantine,
        "select_protocol_bootstrap_source",
        lambda *_args, **_kwargs: {
            "available": True,
            "reason": "legacy_strategy_migration",
            "source_v": 142,
            "receipt": receipt,
        },
    )
    monkeypatch.setattr(
        tool_commit,
        "validate_commit_gate_ledger",
        lambda *_args, **_kwargs: {
            "ok": True,
            "missing_gates": [],
            "failed_gates": [],
            "current_code_fingerprint": candidate_hash,
        },
    )

    facts, issues = official_bootstrap._current_operator_bootstrap_facts(
        candidate,
        ROOT_ID,
        checkpoint=checkpoint,
        expected_stage="official_bootstrap_required",
        expected_candidate_hash=candidate_hash,
    )
    assert issues == []
    assert facts["candidate_hash"] == candidate_hash

    wrong_stage = {**checkpoint, "stage": "verified"}
    assert any(
        "checkpoint_stage_mismatch" in issue
        for issue in official_bootstrap._current_operator_bootstrap_facts(
            candidate,
            ROOT_ID,
            checkpoint=wrong_stage,
            expected_stage="official_bootstrap_required",
            expected_candidate_hash=candidate_hash,
        )[1]
    )
    assert "official_bootstrap_candidate_version_mismatch" in (
        official_bootstrap._current_operator_bootstrap_facts(
            wrong_bot,
            ROOT_ID,
            checkpoint=checkpoint,
            expected_stage="official_bootstrap_required",
            expected_candidate_hash="c" * 64,
        )[1]
    )
    assert "official_bootstrap_candidate_hash_mismatch" in (
        official_bootstrap._current_operator_bootstrap_facts(
            candidate,
            ROOT_ID,
            checkpoint=checkpoint,
            expected_stage="official_bootstrap_required",
            expected_candidate_hash="d" * 64,
        )[1]
    )
    strict.append("national_v149")
    assert "official_bootstrap_strict_publication_exists" in (
        official_bootstrap._current_operator_bootstrap_facts(
            candidate,
            ROOT_ID,
            checkpoint=checkpoint,
            expected_stage="official_bootstrap_required",
            expected_candidate_hash=candidate_hash,
        )[1]
    )


def test_operator_bootstrap_correct_parked_request_authorizes_exact_selection(
    monkeypatch, tmp_path
):
    candidate = tmp_path / "national_v150"
    candidate.mkdir()
    (candidate / "national_bot.py").write_text("# native\n", encoding="utf-8")
    candidate_hash = "a" * 64
    receipt = {"receipt_digest": "b" * 64}
    base_facts = {
        "candidate_path": str(candidate.resolve()),
        "candidate_label": "national_v150",
        "candidate_version": 150,
        "candidate_hash": candidate_hash,
        "source_v": 142,
        "workflow_run_id": "generation:150:test",
        "checkpoint_contract_digest": "c" * 64,
        "evaluation_contract_version": 11,
        "evaluation_contract_hash": "f" * 64,
        "protocol_bootstrap_receipt": receipt,
        "protocol_bootstrap_receipt_digest": receipt["receipt_digest"],
        "transition_receipt_digest": receipt["receipt_digest"],
        "active_bots": [],
        "strict_published_bots": [],
        "root_id": ROOT_ID,
    }
    monkeypatch.setattr(
        official_bootstrap,
        "_current_operator_bootstrap_facts",
        lambda *_args, **_kwargs: (dict(base_facts), []),
    )
    verified = {"stage": "verified", "audit_context": {}}
    parked_result = official_bootstrap.build_operator_bootstrap_parked_request(
        candidate,
        verified,
        candidate_hash=candidate_hash,
    )
    parked = parked_result["request"]
    checkpoint = {
        "stage": "official_bootstrap_required",
        "audit_context": {"official_bootstrap_request": parked},
    }
    selection = {
        "selected": True,
        "root_id": ROOT_ID,
        "bootstrap_root_receipt": {"receipt_digest": "d" * 64},
        "candidate_binding": {"candidate_binding_digest": "e" * 64},
        "opponent": {"path": "bots/national_v141"},
    }
    monkeypatch.setattr(
        official_bootstrap,
        "validate_signed_v5_ledger_bootstrap_selection",
        lambda *_args, **_kwargs: {"valid": True, "issues": []},
    )

    authorized = official_bootstrap.authorize_operator_bootstrap_selection(
        selection,
        ROOT_ID,
        candidate,
        checkpoint=checkpoint,
    )

    assert authorized["valid"] is True
    authorization = authorized["authorization"]
    assert authorization["parked_request_digest"] == parked["request_digest"]
    assert authorization["candidate_hash"] == candidate_hash
    assert len(authorization["authorization_digest"]) == 64

    base_facts["evaluation_contract_hash"] = "0" * 64
    drifted = official_bootstrap.validate_operator_bootstrap_authorized_selection(
        authorized["selection"],
        ROOT_ID,
        candidate,
        checkpoint=checkpoint,
    )
    assert drifted["valid"] is False
    assert "official_bootstrap_parked_request_evaluation_contract_hash_mismatch" in (
        drifted["issues"]
    )


def test_completed_bootstrap_rebinds_certificate_envelope_consumption_and_checkpoint(
    monkeypatch, tmp_path
):
    import official_certification
    import official_job_envelope

    candidate = tmp_path / "national_v150"
    candidate.mkdir()
    (candidate / "national_bot.py").write_text("# native\n", encoding="utf-8")
    candidate_hash = "a" * 64
    root_receipt = {"receipt_digest": "b" * 64}
    facts = {
        "candidate_path": str(candidate.resolve()),
        "candidate_label": candidate.name,
        "candidate_version": 150,
        "candidate_hash": candidate_hash,
        "source_v": 142,
        "workflow_run_id": "generation:150:test",
        "checkpoint_contract_digest": "c" * 64,
        "evaluation_contract_version": 11,
        "evaluation_contract_hash": "d" * 64,
        "protocol_bootstrap_receipt": {"receipt_digest": "e" * 64},
        "protocol_bootstrap_receipt_digest": "e" * 64,
        "transition_receipt_digest": "e" * 64,
        "active_bots": [],
        "strict_published_bots": [],
        "root_id": ROOT_ID,
    }
    parked = {
        "schema_version": official_bootstrap.PARKED_REQUEST_SCHEMA_VERSION,
        "kind": official_bootstrap.PARKED_REQUEST_KIND,
        **facts,
    }
    parked["request_digest"] = canonical_digest(parked)
    selection = {
        "selected": True,
        "bootstrap_root_id": ROOT_ID,
        "bootstrap_root_receipt": root_receipt,
        "candidate_binding": {"candidate_binding_digest": "f" * 64},
        "opponent": {"eligible": True, "path": "bots/national_v141"},
    }
    selection["operator_bootstrap_authorization"] = (
        official_bootstrap._operator_bootstrap_authorization(
            selection,
            ROOT_ID,
            parked,
            facts,
        )
    )
    envelope = {
        "opponent_selection": selection,
        "envelope_digest": "1" * 64,
    }
    identity = {
        "candidate_hash": candidate_hash,
        "opponent_hash": "2" * 64,
        "spec": {
            "candidate": str(candidate.resolve()),
            "bootstrap_root_id": ROOT_ID,
        },
    }
    deterministic = {"receipt_digest": "3" * 64}
    certificate_digest = "4" * 64
    record = {
        "certificate_digest": certificate_digest,
        "identity": identity,
        "opponent_selection": selection,
        "job_envelope": envelope,
    }
    entry = {
        "entry_digest": "5" * 64,
        "candidate_label": candidate.name,
        "candidate_hash": candidate_hash,
        "policy_id": "official-full-v5",
        "mode": "full",
        "outcome": "official-certified",
        "authoritative": True,
        "blocking": False,
        "classification": "pass",
        "certificate_digest": certificate_digest,
        "deterministic_status_receipt_digest": deterministic["receipt_digest"],
        "job_envelope_digest": envelope["envelope_digest"],
        "request_started_ns": 10,
        "request_completed_ns": 20,
        "bootstrap_root_id": ROOT_ID,
        "bootstrap_root_receipt_digest": root_receipt["receipt_digest"],
    }
    status = {
        "bot": candidate.name,
        "status": "official-certified",
        "mode": "full",
        "policy_id": "official-full-v5",
        "certificate_digest": certificate_digest,
        "certificate_path": str(tmp_path / "certificate.json"),
        "certification_identity": identity,
        "opponent_selection": selection,
        "official_job_envelope": envelope,
        "official_deterministic_status_receipt": deterministic,
        "request_started_ns": 10,
        "request_completed_ns": 20,
        "official_verdict_ledger_entry": entry,
    }
    checkpoint = {
        "stage": "official_bootstrap_required",
        "audit_context": {"official_bootstrap_request": parked},
    }
    current_facts = {"value": facts}
    current_entries = {"value": [entry]}
    current_consumption = {
        "value": {
            "valid": True,
            "consumed": True,
            "successful_count": 1,
            "successful_entry_digests": [entry["entry_digest"]],
        }
    }
    monkeypatch.setattr(
        official_bootstrap,
        "_current_operator_bootstrap_facts",
        lambda *_args, **_kwargs: (dict(current_facts["value"]), []),
    )
    monkeypatch.setattr(
        official_bootstrap,
        "validate_signed_v5_ledger_bootstrap_selection",
        lambda *_args, **_kwargs: {"valid": True, "issues": []},
    )
    monkeypatch.setattr(
        official_bootstrap,
        "_validated_ledger_entries",
        lambda: (deepcopy(current_entries["value"]), []),
    )
    monkeypatch.setattr(
        official_bootstrap,
        "signed_v5_ledger_bootstrap_root_consumption",
        lambda _root_id: deepcopy(current_consumption["value"]),
    )
    monkeypatch.setattr(
        official_certification,
        "official_full_certified",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        official_certification,
        "_load_certificate_container",
        lambda _path: (record, {}, []),
    )
    monkeypatch.setattr(
        official_job_envelope,
        "job_envelope_issues",
        lambda *_args, **_kwargs: [],
    )

    valid = official_bootstrap.validate_completed_operator_bootstrap_authorization(
        status,
        candidate,
        checkpoint=checkpoint,
    )
    assert valid["valid"] is True
    assert valid["ledger_entry_digest"] == entry["entry_digest"]

    current_facts["value"] = {**facts, "workflow_run_id": "generation:150:drift"}
    workflow_drift = (
        official_bootstrap.validate_completed_operator_bootstrap_authorization(
            status, candidate, checkpoint=checkpoint
        )
    )
    assert workflow_drift["valid"] is False
    assert "official_bootstrap_completed_authorization_drift" in workflow_drift["issues"]

    current_facts["value"] = {
        **facts,
        "evaluation_contract_hash": "0" * 64,
    }
    contract_drift = (
        official_bootstrap.validate_completed_operator_bootstrap_authorization(
            status, candidate, checkpoint=checkpoint
        )
    )
    assert contract_drift["valid"] is False
    assert (
        "official_bootstrap_parked_request_evaluation_contract_hash_mismatch"
        in contract_drift["issues"]
    )

    current_facts["value"] = {**facts, "candidate_hash": "0" * 64}
    candidate_drift = (
        official_bootstrap.validate_completed_operator_bootstrap_authorization(
            status, candidate, checkpoint=checkpoint
        )
    )
    assert candidate_drift["valid"] is False
    assert "official_bootstrap_parked_request_candidate_hash_mismatch" in (
        candidate_drift["issues"]
    )

    current_facts["value"] = facts
    envelope_drift_status = deepcopy(status)
    envelope_drift_status["official_job_envelope"]["envelope_digest"] = "0" * 64
    envelope_drift = (
        official_bootstrap.validate_completed_operator_bootstrap_authorization(
            envelope_drift_status, candidate, checkpoint=checkpoint
        )
    )
    assert envelope_drift["valid"] is False
    assert "official_bootstrap_completed_certificate_envelope_mismatch" in (
        envelope_drift["issues"]
    )

    current_consumption["value"] = {
        **current_consumption["value"],
        "successful_count": 2,
    }
    duplicate_consumption = (
        official_bootstrap.validate_completed_operator_bootstrap_authorization(
            status, candidate, checkpoint=checkpoint
        )
    )
    assert duplicate_consumption["valid"] is False
    assert "official_bootstrap_completed_root_consumption_invalid" in (
        duplicate_consumption["issues"]
    )


def test_bootstrap_job_revalidation_replays_exact_root_selector(monkeypatch, tmp_path):
    root, _entry = _root_runtime(monkeypatch, tmp_path)
    candidate = tmp_path / "national_v143"
    candidate.mkdir()
    (candidate / "national_bot.py").write_text("# native\n", encoding="utf-8")
    monkeypatch.setattr(official_bootstrap, "hash_path", lambda _path: "a" * 64)
    selected = official_bootstrap.select_signed_v5_ledger_bootstrap_root(root["root_id"], candidate)
    spec = official_certification.build_spec(
        "full",
        candidate,
        opponent=selected["opponent"]["path"],
        bootstrap_root_id=root["root_id"],
    )
    calls = []
    original = official_bootstrap.select_signed_v5_ledger_bootstrap_root

    def replayed(root_id, candidate_path=None):
        calls.append((root_id, str(Path(candidate_path).resolve())))
        return original(root_id, candidate_path=candidate_path)

    monkeypatch.setattr(official_bootstrap, "select_signed_v5_ledger_bootstrap_root", replayed)

    resolved, live = official_certification.resolve_managed_certification_spec(
        spec,
        exact_opponent_only=True,
    )

    assert resolved is spec
    assert official_certification.stable_official_opponent_selection(live) == (
        official_certification.stable_official_opponent_selection(selected)
    )
    assert calls == [(root["root_id"], str(candidate.resolve()))]


def _ledger_signing_material(tmp_path, monkeypatch):
    key = tmp_path / "bootstrap-ledger-key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
    )
    pending = deepcopy(official_certificate_signing.load_signer_trust_policy())
    pending["current_signer"] = {
        "epoch": pending["current_epoch"],
        "state": "rotation-required",
        "key_fingerprint": None,
        "public_key_sha256": None,
    }
    pending["policy_digest"] = official_certificate_signing._policy_digest(pending)
    pending_path = tmp_path / "pending-signer-policy.json"
    pending_path.write_text(json.dumps(pending, indent=2) + "\n", encoding="utf-8")
    policy_payload, allowed_payload = (
        official_certificate_signing.build_signer_rotation_material(
            Path(str(key) + ".pub"), trust_policy=pending_path
        )
    )
    allowed = tmp_path / "allowed-signers"
    allowed.write_text(allowed_payload, encoding="utf-8")
    policy = tmp_path / "signer-policy.json"
    policy.write_text(
        json.dumps(policy_payload, indent=2) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("POK_OFFICIAL_VERDICT_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("POK_OFFICIAL_SIGNING_KEY", str(key))
    monkeypatch.setattr(official_certificate_signing, "DEFAULT_ALLOWED_SIGNERS", allowed)
    monkeypatch.setattr(official_certificate_signing, "DEFAULT_TRUST_POLICY", policy)
    official_verdict_ledger.initialize_verdict_ledger()


def _synthetic_bootstrap_status(root_id: str, *, outcome: str):
    receipt_payload = {"root_id": root_id, "kind": "signed-v5-ledger-bootstrap-root-receipt"}
    receipt = {**receipt_payload, "receipt_digest": canonical_digest(receipt_payload)}
    return {
        "bot": "national_v200",
        "status": outcome,
        "mode": "full",
        "policy_id": "official-full-v5",
        "certification_identity": {
            "candidate_hash": "a" * 64,
            "spec": {"bootstrap_root_id": root_id},
        },
        "certificate_digest": "b" * 64,
        "official_evidence_summary": {"blocking": outcome == "official-failed", "classification": "pass"},
        "official_deterministic_status_receipt": {"receipt_digest": "c" * 64},
        "official_job_envelope": {"envelope_digest": "d" * 64},
        "opponent_selection": {
            "bootstrap_root_id": root_id,
            "bootstrap_root_receipt": receipt,
            "opponent": {"eligibility_receipt": receipt},
        },
        "request_started_ns": 1,
        "request_completed_ns": 2,
    }


def test_signed_ledger_records_bootstrap_consumption_only_for_success(monkeypatch, tmp_path):
    _ledger_signing_material(tmp_path, monkeypatch)
    monkeypatch.setattr(
        official_certification,
        "authoritative_verdict_status_issues",
        lambda _status, **_kwargs: [],
    )
    root_id = ROOT_ID

    certified = official_verdict_ledger.append_verdict(
        _synthetic_bootstrap_status(root_id, outcome="official-certified")
    )
    failed = official_verdict_ledger.append_verdict(
        _synthetic_bootstrap_status(root_id, outcome="official-failed")
    )

    assert certified["bootstrap_root_id"] == root_id
    assert len(certified["bootstrap_root_receipt_digest"]) == 64
    assert "bootstrap_root_id" not in failed
    assert "bootstrap_root_receipt_digest" not in failed
