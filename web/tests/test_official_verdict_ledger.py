from pathlib import Path
from copy import deepcopy
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from bot_artifact import canonical_digest
import official_certification
import official_certificate_signing
import official_verdict_ledger
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
    monkeypatch.setenv("POK_OFFICIAL_SIGNING_KEY", str(key))
    monkeypatch.setattr(official_certificate_signing, "DEFAULT_ALLOWED_SIGNERS", allowed)
    monkeypatch.setattr(official_certificate_signing, "DEFAULT_TRUST_POLICY", policy)
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


def _bootstrap_status(outcome: str, candidate_hash: str, *, bot: str):
    control_id = "first_strict_control_v1"
    receipt_payload = {
        "kind": "official-first-strict-control-authorization-receipt",
        "bootstrap_control_id": control_id,
    }
    receipt = {
        **receipt_payload,
        "receipt_digest": canonical_digest(receipt_payload),
    }
    status = _status(
        outcome,
        candidate_hash,
        certificate_digest="d" * 64 if outcome == "official-certified" else "",
    )
    status["bot"] = bot
    status["certification_identity"]["spec"] = {
        "bootstrap_control_id": control_id
    }
    status["opponent_selection"] = {
        "bootstrap_control_id": control_id,
        "bootstrap_control_receipt": receipt,
        "opponent": {"eligibility_receipt": receipt},
    }
    return status


def _allow_synthetic_status(monkeypatch):
    monkeypatch.setattr(
        official_certification,
        "authoritative_verdict_status_issues",
        lambda _status, **_kwargs: [],
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
    assert certified["signer_epoch"] == 2
    assert certified["signer_key_fingerprint"].startswith("SHA256:")
    head_wrapper = json.loads(ledger_head_path().read_text(encoding="utf-8"))
    assert head_wrapper["head"]["signer_epoch"] == 2
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
    assert result["threat_model"] == {
        "scope": "operational-serialization-crash-recovery-and-chain-integrity",
        "same_uid_tamper_resistance": False,
        "rollback_resistance_without_external_anchor": False,
        "external_latest_head_anchor": "not-configured",
    }


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


def test_concurrent_bootstrap_append_consumes_control_exactly_once(
    tmp_path, monkeypatch
):
    _signing_material(tmp_path, monkeypatch)
    _allow_synthetic_status(monkeypatch)
    barrier = threading.Barrier(2)
    statuses = (
        _bootstrap_status("official-certified", "a" * 64, bot="national_v200"),
        _bootstrap_status("official-certified", "b" * 64, bot="national_v201"),
    )

    def append_together(status):
        barrier.wait(timeout=5)
        try:
            return ("ok", append_verdict(status))
        except ValueError as exc:
            return ("rejected", str(exc))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(append_together, statuses))

    assert sorted(result[0] for result in results) == ["ok", "rejected"]
    rejected = next(result[1] for result in results if result[0] == "rejected")
    assert "already successfully consumed" in rejected
    health = ledger_integrity()
    assert health["valid"] is True
    assert health["entry_count"] == 1


def test_failed_bootstrap_append_does_not_consume_control(tmp_path, monkeypatch):
    _signing_material(tmp_path, monkeypatch)
    _allow_synthetic_status(monkeypatch)

    failed = append_verdict(
        _bootstrap_status("official-failed", "a" * 64, bot="national_v200")
    )
    certified = append_verdict(
        _bootstrap_status("official-certified", "b" * 64, bot="national_v201")
    )

    assert "bootstrap_control_id" not in failed
    assert certified["bootstrap_control_id"] == "first_strict_control_v1"
    assert ledger_integrity()["entry_count"] == 2


def test_old_valid_ledger_and_head_pair_is_explicitly_outside_threat_model(
    tmp_path, monkeypatch
):
    """Do not accidentally describe the local history as an anti-rollback log."""

    _signing_material(tmp_path, monkeypatch)
    _allow_synthetic_status(monkeypatch)
    append_verdict(
        _status("official-certified", "a" * 64, certificate_digest="d" * 64)
    )
    old_ledger = ledger_path().read_bytes()
    old_head = ledger_head_path().read_bytes()

    append_verdict(_status("official-failed", "b" * 64))
    assert ledger_integrity()["entry_count"] == 2

    # Both objects are same-uid writable and there is no independently
    # protected latest-head checkpoint. Restoring a previously valid pair is
    # internally consistent and therefore intentionally accepted.
    ledger_path().write_bytes(old_ledger)
    ledger_head_path().write_bytes(old_head)
    health = ledger_integrity()

    assert health["valid"] is True
    assert health["entry_count"] == 1
    assert health["threat_model"][
        "rollback_resistance_without_external_anchor"
    ] is False


def test_complete_signed_suffix_rolls_head_forward_after_crash(
    tmp_path, monkeypatch
):
    _signing_material(tmp_path, monkeypatch)
    _allow_synthetic_status(monkeypatch)
    original = official_verdict_ledger._write_signed_head

    def crash_before_head(path, entry=None):
        if entry is not None:
            raise RuntimeError("crash-before-head")
        return original(path, entry)

    monkeypatch.setattr(
        official_verdict_ledger,
        "_write_signed_head",
        crash_before_head,
    )
    with pytest.raises(RuntimeError, match="crash-before-head"):
        append_verdict(_status("official-certified", "a" * 64, certificate_digest="d" * 64))
    assert ledger_path().stat().st_size > 0
    assert json.loads(ledger_head_path().read_text(encoding="utf-8"))["head"][
        "sequence"
    ] == 0

    monkeypatch.setattr(official_verdict_ledger, "_write_signed_head", original)
    health = ledger_integrity()
    assert health["valid"] is True
    assert health["entry_count"] == 1
    assert json.loads(ledger_head_path().read_text(encoding="utf-8"))["head"][
        "sequence"
    ] == 1


def test_partial_append_is_truncated_to_exact_signed_head_boundary(
    tmp_path, monkeypatch
):
    _signing_material(tmp_path, monkeypatch)
    _allow_synthetic_status(monkeypatch)
    original = official_verdict_ledger._write_all

    def partial_then_crash(descriptor, data):
        os_written = official_verdict_ledger.os.write(
            descriptor,
            bytes(data[: max(1, len(data) // 2)]),
        )
        assert os_written > 0
        raise RuntimeError("crash-during-line")

    monkeypatch.setattr(official_verdict_ledger, "_write_all", partial_then_crash)
    with pytest.raises(RuntimeError, match="crash-during-line"):
        append_verdict(_status("official-certified", "a" * 64, certificate_digest="d" * 64))
    assert ledger_path().stat().st_size > 0

    monkeypatch.setattr(official_verdict_ledger, "_write_all", original)
    health = ledger_integrity()
    assert health["valid"] is True
    assert health["entry_count"] == 0
    assert ledger_path().stat().st_size == 0


def test_head_committed_before_exception_remains_a_valid_commit(
    tmp_path, monkeypatch
):
    _signing_material(tmp_path, monkeypatch)
    _allow_synthetic_status(monkeypatch)
    original = official_verdict_ledger._write_signed_head

    def crash_after_head(path, entry=None):
        original(path, entry)
        if entry is not None:
            raise RuntimeError("crash-after-head")

    monkeypatch.setattr(
        official_verdict_ledger,
        "_write_signed_head",
        crash_after_head,
    )
    with pytest.raises(RuntimeError, match="crash-after-head"):
        append_verdict(_status("official-certified", "a" * 64, certificate_digest="d" * 64))

    health = ledger_integrity()
    assert health["valid"] is True
    assert health["entry_count"] == 1


def test_write_all_retries_short_os_writes(tmp_path, monkeypatch):
    target = tmp_path / "short-write.bin"
    payload = b"signed-ledger-write" * 17
    real_write = official_verdict_ledger.os.write
    calls = []

    def short_write(descriptor, data):
        chunk = bytes(data[:3])
        calls.append(len(chunk))
        return real_write(descriptor, chunk)

    with target.open("wb") as handle:
        monkeypatch.setattr(official_verdict_ledger.os, "write", short_write)
        official_verdict_ledger._write_all(handle.fileno(), payload)

    assert len(calls) > 1
    assert target.read_bytes() == payload


def test_observer_validation_is_single_flight_and_returns_deep_copies(
    tmp_path,
    monkeypatch,
):
    _signing_material(tmp_path, monkeypatch)
    _allow_synthetic_status(monkeypatch)
    append_verdict(
        _status(
            "official-certified",
            "a" * 64,
            certificate_digest="d" * 64,
        )
    )
    official_verdict_ledger._invalidate_validated_snapshot_cache()

    original = official_verdict_ledger._validate_captured_snapshot
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def slow_validate(captured):
        nonlocal calls
        with calls_lock:
            calls += 1
        entered.set()
        assert release.wait(timeout=5)
        return original(captured)

    monkeypatch.setattr(
        official_verdict_ledger,
        "_validate_captured_snapshot",
        slow_validate,
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(lambda: ledger_integrity(fresh=False))
            for _index in range(8)
        ]
        assert entered.wait(timeout=5)
        time.sleep(0.05)
        release.set()
        results = [future.result(timeout=10) for future in futures]

    assert calls == 1
    assert all(result["valid"] is True for result in results)
    assert all(result["entry_count"] == 1 for result in results)
    results[0]["head"]["sequence"] = 999
    assert results[1]["head"]["sequence"] == 1
    assert ledger_integrity(fresh=False)["head"]["sequence"] == 1


def test_healthy_observer_uses_shared_lock_and_fresh_bypasses_cache(
    tmp_path,
    monkeypatch,
):
    _signing_material(tmp_path, monkeypatch)
    _allow_synthetic_status(monkeypatch)
    append_verdict(
        _status(
            "official-certified",
            "a" * 64,
            certificate_digest="d" * 64,
        )
    )
    official_verdict_ledger._invalidate_validated_snapshot_cache()

    validation_calls = 0
    original_validation = official_verdict_ledger._validate_captured_snapshot

    def counted_validation(captured):
        nonlocal validation_calls
        validation_calls += 1
        return original_validation(captured)

    lock_modes = []
    original_flock = official_verdict_ledger.fcntl.flock

    def recorded_flock(descriptor, operation):
        if operation in {
            official_verdict_ledger.fcntl.LOCK_SH,
            official_verdict_ledger.fcntl.LOCK_EX,
        }:
            lock_modes.append(operation)
        return original_flock(descriptor, operation)

    monkeypatch.setattr(
        official_verdict_ledger,
        "_validate_captured_snapshot",
        counted_validation,
    )
    monkeypatch.setattr(official_verdict_ledger.fcntl, "flock", recorded_flock)

    assert ledger_integrity(fresh=False)["valid"] is True
    assert ledger_integrity(fresh=False)["valid"] is True
    assert validation_calls == 1
    assert official_verdict_ledger.fcntl.LOCK_SH in lock_modes
    assert official_verdict_ledger.fcntl.LOCK_EX not in lock_modes

    # The public default is fresh for mutation/publication admission.  Only
    # explicitly read-only observers opt into the content-bound cache.
    assert ledger_integrity()["valid"] is True
    assert ledger_integrity(fresh=True)["valid"] is True
    assert validation_calls == 3


def test_observer_cache_key_binds_ledger_head_and_signer_inputs(
    tmp_path,
    monkeypatch,
):
    _signing_material(tmp_path, monkeypatch)
    _allow_synthetic_status(monkeypatch)
    append_verdict(
        _status(
            "official-certified",
            "a" * 64,
            certificate_digest="d" * 64,
        )
    )
    official_verdict_ledger._invalidate_validated_snapshot_cache()

    calls = 0
    original = official_verdict_ledger._validate_captured_snapshot

    def counted(captured):
        nonlocal calls
        calls += 1
        return original(captured)

    monkeypatch.setattr(
        official_verdict_ledger,
        "_validate_captured_snapshot",
        counted,
    )

    def bump_identity(path: Path) -> None:
        metadata = path.stat()
        os.utime(
            path,
            ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000),
        )

    assert ledger_integrity(fresh=False)["valid"] is True
    assert ledger_integrity(fresh=False)["valid"] is True
    assert calls == 1

    bound_paths = (
        ledger_path(),
        ledger_head_path(),
        official_certificate_signing.allowed_signers_path(),
        official_certificate_signing.signer_trust_policy_path(),
    )
    for expected_calls, path in enumerate(bound_paths, start=2):
        bump_identity(path)
        assert ledger_integrity(fresh=False)["valid"] is True
        assert calls == expected_calls


def test_cached_observer_fails_closed_after_same_path_content_tamper(
    tmp_path,
    monkeypatch,
):
    _signing_material(tmp_path, monkeypatch)
    _allow_synthetic_status(monkeypatch)
    append_verdict(
        _status(
            "official-certified",
            "a" * 64,
            certificate_digest="d" * 64,
        )
    )

    assert ledger_integrity(fresh=False)["valid"] is True
    path = ledger_path()
    original = path.read_text(encoding="utf-8")
    path.write_text(
        original.replace("official-certified", "official-failed"),
        encoding="utf-8",
    )

    result = ledger_integrity(fresh=False)

    assert result["valid"] is False
    assert any(
        "digest_invalid" in issue or "signature_invalid" in issue
        for issue in result["issues"]
    )


def test_observer_capture_failure_is_fail_closed(monkeypatch):
    official_verdict_ledger._invalidate_validated_snapshot_cache()
    monkeypatch.setattr(
        official_verdict_ledger,
        "_capture_validation_inputs",
        lambda: (_ for _ in ()).throw(OSError("capture failed")),
    )

    result = ledger_integrity(fresh=False)

    assert result["valid"] is False
    assert result["entry_count"] == 0
    assert result["issues"] == [
        "official_verdict_ledger_validation_capture_error:OSError"
    ]


def test_partial_suffix_observer_escalates_to_exclusive_recovery(
    tmp_path,
    monkeypatch,
):
    _signing_material(tmp_path, monkeypatch)
    _allow_synthetic_status(monkeypatch)
    original_write = official_verdict_ledger._write_all

    def partial_then_crash(descriptor, data):
        assert official_verdict_ledger.os.write(
            descriptor,
            bytes(data[: max(1, len(data) // 2)]),
        ) > 0
        raise RuntimeError("crash-during-line")

    monkeypatch.setattr(
        official_verdict_ledger,
        "_write_all",
        partial_then_crash,
    )
    with pytest.raises(RuntimeError, match="crash-during-line"):
        append_verdict(
            _status(
                "official-certified",
                "a" * 64,
                certificate_digest="d" * 64,
            )
        )
    monkeypatch.setattr(
        official_verdict_ledger,
        "_write_all",
        original_write,
    )

    lock_modes = []
    original_flock = official_verdict_ledger.fcntl.flock

    def recorded_flock(descriptor, operation):
        if operation in {
            official_verdict_ledger.fcntl.LOCK_SH,
            official_verdict_ledger.fcntl.LOCK_EX,
        }:
            lock_modes.append(operation)
        return original_flock(descriptor, operation)

    monkeypatch.setattr(official_verdict_ledger.fcntl, "flock", recorded_flock)
    result = ledger_integrity(fresh=False)

    assert result["valid"] is True
    assert result["entry_count"] == 0
    assert lock_modes[0] == official_verdict_ledger.fcntl.LOCK_SH
    assert official_verdict_ledger.fcntl.LOCK_EX in lock_modes
    assert ledger_path().stat().st_size == 0
