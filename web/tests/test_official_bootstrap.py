from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import subprocess

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
        "web/core/official_bootstrap.py",
        "web/core/official_bootstrap_roots.json",
    }

    assert expected <= ALWAYS_CRITICAL_EXACT
    assert expected <= CRITICAL_EVALUATION_GATE_EXACT


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
    allowed = tmp_path / "allowed-signers"
    allowed.write_text(
        "pok-official-certifier namespaces=\"pok-official-cert-v4\" "
        + Path(str(key) + ".pub").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.setenv("POK_OFFICIAL_VERDICT_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("POK_OFFICIAL_SIGNING_KEY", str(key))
    monkeypatch.setattr(official_certificate_signing, "DEFAULT_ALLOWED_SIGNERS", allowed)
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
    monkeypatch.setattr(official_certification, "authoritative_verdict_status_issues", lambda _status: [])
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
