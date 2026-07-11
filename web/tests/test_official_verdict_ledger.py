from pathlib import Path
import subprocess
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

import official_certification
import official_certificate_signing
from official_verdict_ledger import (
    append_verdict,
    initialize_verdict_ledger,
    latest_authoritative_verdict,
    ledger_head_path,
    ledger_integrity,
    ledger_path,
)


def _signing_material(tmp_path: Path, monkeypatch, *, initialize: bool = True):
    key = tmp_path / "ledger-key"
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
    monkeypatch.setenv("POK_OFFICIAL_SIGNING_KEY", str(key))
    monkeypatch.setattr(official_certificate_signing, "DEFAULT_ALLOWED_SIGNERS", allowed)
    if initialize:
        initialize_verdict_ledger()


def _status(outcome: str, candidate_hash: str, *, certificate_digest: str = ""):
    blocking = outcome == "official-failed"
    return {
        "bot": "national_v143",
        "status": outcome,
        "mode": "full",
        "policy_id": "official-full-v5",
        "certification_identity": {"candidate_hash": candidate_hash},
        "certificate_digest": certificate_digest,
        "official_evidence_summary": {
            "blocking": blocking,
            "classification": "protocol" if blocking else "pass",
        },
        "official_deterministic_status_receipt": {"receipt_digest": "b" * 64},
        "official_job_envelope": {"envelope_digest": "c" * 64},
        "request_started_ns": 1,
        "request_completed_ns": 2,
    }


def _allow_synthetic_status(monkeypatch):
    monkeypatch.setattr(
        official_certification,
        "authoritative_verdict_status_issues",
        lambda _status: [],
    )


def test_latest_signed_authoritative_verdict_is_monotonic(tmp_path, monkeypatch):
    _signing_material(tmp_path, monkeypatch)
    _allow_synthetic_status(monkeypatch)
    candidate_hash = "a" * 64

    certified = append_verdict(
        _status("official-certified", candidate_hash, certificate_digest="d" * 64)
    )
    failed = append_verdict(_status("official-failed", candidate_hash))
    append_verdict(_status("official-inconclusive", candidate_hash))
    latest = latest_authoritative_verdict(candidate_hash)

    assert certified["sequence"] == 1
    assert failed["sequence"] == 2
    assert latest["valid"] is True
    assert latest["entry"]["outcome"] == "official-failed"
    assert latest["entry"]["entry_digest"] == failed["entry_digest"]


def test_ledger_tampering_fails_closed(tmp_path, monkeypatch):
    _signing_material(tmp_path, monkeypatch)
    _allow_synthetic_status(monkeypatch)
    candidate_hash = "a" * 64
    append_verdict(
        _status("official-certified", candidate_hash, certificate_digest="d" * 64)
    )
    path = ledger_path()
    payload = path.read_text(encoding="utf-8")
    path.write_text(payload.replace("official-certified", "official-failed"), encoding="utf-8")

    latest = latest_authoritative_verdict(candidate_hash)

    assert latest["valid"] is False
    assert latest["entry"] is None
    assert any("digest_invalid" in issue or "signature_invalid" in issue for issue in latest["issues"])


def test_missing_ledger_and_head_fail_closed(tmp_path, monkeypatch):
    _signing_material(tmp_path, monkeypatch, initialize=False)

    result = ledger_integrity()

    assert result["valid"] is False
    assert result["issues"] == ["official_verdict_ledger_missing"]


def test_signed_head_detects_complete_tail_truncation(tmp_path, monkeypatch):
    _signing_material(tmp_path, monkeypatch)
    _allow_synthetic_status(monkeypatch)
    candidate_hash = "a" * 64
    append_verdict(_status("official-certified", candidate_hash, certificate_digest="d" * 64))
    append_verdict(_status("official-failed", candidate_hash))
    path = ledger_path()
    first_line = path.read_text(encoding="utf-8").splitlines()[0]
    path.write_text(first_line + "\n", encoding="utf-8")

    result = ledger_integrity()

    assert result["valid"] is False
    assert any(issue.startswith("official_verdict_ledger_truncated_") for issue in result["issues"])


def test_missing_signed_head_fails_closed(tmp_path, monkeypatch):
    _signing_material(tmp_path, monkeypatch)
    _allow_synthetic_status(monkeypatch)
    append_verdict(_status("official-certified", "a" * 64, certificate_digest="d" * 64))
    ledger_head_path().unlink()

    result = ledger_integrity()

    assert result["valid"] is False
    assert result["issues"] == ["official_verdict_ledger_head_missing"]


def test_append_rejects_unverified_status_before_signing(tmp_path, monkeypatch):
    _signing_material(tmp_path, monkeypatch, initialize=False)

    with pytest.raises(ValueError, match="rejected unverified status"):
        append_verdict(
            _status("official-certified", "a" * 64, certificate_digest="d" * 64)
        )

    assert not ledger_path().exists()


def test_explicit_signed_genesis_allows_empty_healthy_ledger(tmp_path, monkeypatch):
    _signing_material(tmp_path, monkeypatch, initialize=False)

    initialized = initialize_verdict_ledger()
    result = ledger_integrity()

    assert initialized["initialized"] is True
    assert result["valid"] is True
    assert result["entry_count"] == 0


def test_concurrent_genesis_is_serialized_and_idempotent(tmp_path, monkeypatch):
    _signing_material(tmp_path, monkeypatch, initialize=False)
    barrier = threading.Barrier(2)

    def initialize_together():
        barrier.wait(timeout=5)
        return initialize_verdict_ledger()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: initialize_together(), range(2)))

    assert sorted(result["initialized"] for result in results) == [False, True]
    assert all(result["valid"] is True for result in results)
    assert ledger_integrity()["valid"] is True


def test_failed_genesis_does_not_strand_unsigned_empty_ledger(tmp_path, monkeypatch):
    import official_verdict_ledger

    _signing_material(tmp_path, monkeypatch, initialize=False)
    original_write_signed_head = official_verdict_ledger._write_signed_head
    monkeypatch.setattr(
        official_verdict_ledger,
        "_write_signed_head",
        lambda _path: (_ for _ in ()).throw(RuntimeError("transient signing failure")),
    )

    with pytest.raises(RuntimeError, match="transient signing failure"):
        initialize_verdict_ledger()

    assert not ledger_path().exists()
    assert not ledger_head_path().exists()

    monkeypatch.setattr(
        official_verdict_ledger,
        "_write_signed_head",
        original_write_signed_head,
    )
    assert initialize_verdict_ledger()["initialized"] is True
    assert ledger_integrity()["valid"] is True


def test_append_does_not_reinitialize_missing_history(tmp_path, monkeypatch):
    _signing_material(tmp_path, monkeypatch, initialize=False)
    _allow_synthetic_status(monkeypatch)

    with pytest.raises(RuntimeError, match="official_verdict_ledger_missing"):
        append_verdict(
            _status("official-certified", "a" * 64, certificate_digest="d" * 64)
        )

    assert not ledger_path().exists()
