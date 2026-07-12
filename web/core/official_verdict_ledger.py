"""Crash-consistent signed history for formal official-EXE verdicts.

The signatures and hash chain detect byte drift relative to the currently
present signed head. They do not make the ledger rollback-resistant: this
single-user host has no independently anchored latest-head checkpoint, so a
same-uid actor that restores an older valid ledger/head pair is outside the
threat model. The ledger is therefore an operational serialization and crash
recovery boundary, not a transparency log.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import time
from typing import Any, Iterator

from bot_artifact import canonical_digest
from official_certificate_signing import (
    current_signer_record_fields,
    sign_certificate,
    verify_certificate_signature,
)


LEDGER_SCHEMA_VERSION = 1
LEDGER_ENTRY_KIND = "official-exe-verdict-ledger-entry"
LEDGER_HEAD_SCHEMA_VERSION = 1
LEDGER_HEAD_KIND = "official-exe-verdict-ledger-head"
DEFAULT_LEDGER_PATH = Path.home() / ".local" / "share" / "pok" / "official-verdict-ledger.jsonl"
AUTHORITATIVE_OUTCOMES = {"official-certified", "official-failed"}
LEDGER_THREAT_MODEL = {
    "scope": "operational-serialization-crash-recovery-and-chain-integrity",
    "same_uid_tamper_resistance": False,
    "rollback_resistance_without_external_anchor": False,
    "external_latest_head_anchor": "not-configured",
}


def ledger_path() -> Path:
    return Path(
        os.environ.get("POK_OFFICIAL_VERDICT_LEDGER", str(DEFAULT_LEDGER_PATH))
    ).expanduser()


def ledger_head_path(path: Path | None = None) -> Path:
    target = path or ledger_path()
    return target.with_suffix(target.suffix + ".head.json")


@contextmanager
def _locked_ledger() -> Iterator[Path]:
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(path.suffix + ".lock")
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield path
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _entry_digest(entry: dict[str, Any]) -> str:
    return canonical_digest({key: value for key, value in entry.items() if key != "entry_digest"})


def _head_payload(
    path: Path,
    entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry = entry or {}
    return {
        "schema_version": LEDGER_HEAD_SCHEMA_VERSION,
        "kind": LEDGER_HEAD_KIND,
        "sequence": int(entry.get("sequence", 0) or 0),
        "entry_digest": str(entry.get("entry_digest") or ""),
        "ledger_size_bytes": path.stat().st_size,
        **current_signer_record_fields(),
    }


def _write_signed_head(path: Path, entry: dict[str, Any] | None = None) -> None:
    head = ledger_head_path(path)
    payload = _head_payload(path, entry)
    wrapper = {
        "head": payload,
        "signature": sign_certificate(payload),
    }
    raw = (
        json.dumps(wrapper, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    tmp = head.with_name(f".{head.name}.tmp-{os.getpid()}-{time.time_ns()}")
    try:
        descriptor = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            _write_all(descriptor, raw)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(tmp, head)
        directory = os.open(head.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _write_all(descriptor: int, data: bytes) -> None:
    """Write every byte or raise without treating a short write as a commit."""

    view = memoryview(data)
    written = 0
    while written < len(view):
        count = os.write(descriptor, view[written:])
        if count <= 0:
            raise OSError("official verdict ledger write made no progress")
        written += int(count)


def _read_signed_head(path: Path) -> tuple[dict[str, Any] | None, list[str]]:
    head_path = ledger_head_path(path)
    try:
        if head_path.is_symlink() or not head_path.is_file():
            return None, ["official_verdict_ledger_head_missing"]
        wrapper = json.loads(head_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, [
            f"official_verdict_ledger_head_read_error:{type(exc).__name__}"
        ]
    head = wrapper.get("head") if isinstance(wrapper, dict) else None
    signature = wrapper.get("signature") if isinstance(wrapper, dict) else None
    if not isinstance(head, dict) or not isinstance(signature, str):
        return None, ["official_verdict_ledger_head_wrapper_invalid"]
    issues: list[str] = []
    if (
        head.get("schema_version") != LEDGER_HEAD_SCHEMA_VERSION
        or head.get("kind") != LEDGER_HEAD_KIND
    ):
        issues.append("official_verdict_ledger_head_schema_invalid")
    verification = verify_certificate_signature(head, signature)
    if not verification.get("valid"):
        issues.append("official_verdict_ledger_head_signature_invalid")
    return head, issues


def _head_binding_issues(
    head: dict[str, Any],
    entries: list[dict[str, Any]],
    *,
    ledger_size_bytes: int,
) -> list[str]:
    issues: list[str] = []
    latest = entries[-1] if entries else {}
    expected_sequence = int(latest.get("sequence", 0) or 0)
    expected_digest = str(latest.get("entry_digest") or "")
    if head.get("sequence") != expected_sequence:
        issues.append("official_verdict_ledger_truncated_sequence")
    if head.get("entry_digest") != expected_digest:
        issues.append("official_verdict_ledger_truncated_digest")
    try:
        if int(head.get("ledger_size_bytes", -1)) != int(ledger_size_bytes):
            issues.append("official_verdict_ledger_truncated_size")
    except (TypeError, ValueError):
        issues.append("official_verdict_ledger_head_size_invalid")
    return issues


def _validate_signed_head(path: Path, entries: list[dict[str, Any]]) -> list[str]:
    head, issues = _read_signed_head(path)
    if head is None:
        return issues
    return [
        *issues,
        *_head_binding_issues(
            head,
            entries,
            ledger_size_bytes=path.stat().st_size,
        ),
    ]


def _parse_ledger_bytes(raw: bytes) -> tuple[list[dict[str, Any]], list[str]]:
    if not raw:
        return [], []
    if not raw.endswith(b"\n"):
        return [], ["official_verdict_ledger_truncated_line"]
    entries: list[dict[str, Any]] = []
    issues: list[str] = []
    previous = ""
    try:
        lines = raw.decode("utf-8").splitlines()
    except Exception as exc:
        return [], [f"official_verdict_ledger_read_error:{type(exc).__name__}"]
    for index, line in enumerate(lines, start=1):
        try:
            wrapper = json.loads(line)
        except Exception:
            issues.append(f"official_verdict_ledger_json_invalid:{index}")
            break
        entry = wrapper.get("entry") if isinstance(wrapper, dict) else None
        signature = wrapper.get("signature") if isinstance(wrapper, dict) else None
        if not isinstance(entry, dict) or not isinstance(signature, str):
            issues.append(f"official_verdict_ledger_wrapper_invalid:{index}")
            break
        if entry.get("schema_version") != LEDGER_SCHEMA_VERSION or entry.get("kind") != LEDGER_ENTRY_KIND:
            issues.append(f"official_verdict_ledger_schema_invalid:{index}")
        if entry.get("sequence") != index:
            issues.append(f"official_verdict_ledger_sequence_invalid:{index}")
        if entry.get("previous_entry_digest") != previous:
            issues.append(f"official_verdict_ledger_chain_invalid:{index}")
        digest = str(entry.get("entry_digest") or "")
        if digest != _entry_digest(entry):
            issues.append(f"official_verdict_ledger_digest_invalid:{index}")
        verification = verify_certificate_signature(entry, signature)
        if not verification.get("valid"):
            issues.append(f"official_verdict_ledger_signature_invalid:{index}")
        if issues:
            break
        entries.append(entry)
        previous = digest
    return entries, issues


def _truncate_to_signed_prefix(path: Path, size: int) -> None:
    descriptor = os.open(path, os.O_WRONLY)
    try:
        os.ftruncate(descriptor, int(size))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _recover_locked_ledger(
    path: Path,
    raw: bytes,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Recover only a suffix that is unambiguously beyond a valid signed head.

    A complete, signature-valid chained suffix is rolled forward by signing a
    new head.  An incomplete final write is truncated exactly to the old head's
    signed byte boundary.  Complete but invalid data is never discarded as a
    "crash" because it may be evidence of tampering.
    """

    head, head_issues = _read_signed_head(path)
    if head is None or head_issues:
        return [], head_issues
    try:
        signed_size = int(head.get("ledger_size_bytes", -1))
    except (TypeError, ValueError, OverflowError):
        return [], ["official_verdict_ledger_head_size_invalid"]
    if signed_size < 0:
        return [], ["official_verdict_ledger_head_size_invalid"]
    if signed_size > len(raw):
        return [], ["official_verdict_ledger_truncated_size"]
    prefix_raw = raw[:signed_size]
    prefix_entries, prefix_issues = _parse_ledger_bytes(prefix_raw)
    if prefix_issues:
        return [], [
            "official_verdict_ledger_signed_prefix_invalid",
            *prefix_issues,
        ]
    binding_issues = _head_binding_issues(
        head,
        prefix_entries,
        ledger_size_bytes=signed_size,
    )
    if binding_issues:
        return [], binding_issues
    suffix = raw[signed_size:]
    if not suffix:
        return prefix_entries, []
    if not suffix.endswith(b"\n"):
        _truncate_to_signed_prefix(path, signed_size)
        return prefix_entries, []

    entries, suffix_issues = _parse_ledger_bytes(raw)
    if suffix_issues:
        return [], [
            "official_verdict_ledger_uncommitted_suffix_invalid",
            *suffix_issues,
        ]
    if len(entries) <= len(prefix_entries):
        return [], ["official_verdict_ledger_recovery_suffix_missing"]
    try:
        _write_signed_head(path, entries[-1])
    except Exception as exc:
        return [], [
            "official_verdict_ledger_head_rollforward_failed:"
            f"{type(exc).__name__}"
        ]
    final_head_issues = _validate_signed_head(path, entries)
    if final_head_issues:
        return [], final_head_issues
    return entries, []


def _read_validated(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Read and, while the caller owns the ledger lock, recover crash suffixes."""

    if not path.exists():
        return [], ["official_verdict_ledger_missing"]
    try:
        if path.is_symlink() or not path.is_file():
            return [], ["official_verdict_ledger_not_regular"]
        raw = path.read_bytes()
    except Exception as exc:
        return [], [f"official_verdict_ledger_read_error:{type(exc).__name__}"]
    entries, entry_issues = _parse_ledger_bytes(raw)
    if not entry_issues:
        head_issues = _validate_signed_head(path, entries)
        if not head_issues:
            return entries, []
    recovered, recovery_issues = _recover_locked_ledger(path, raw)
    if recovery_issues:
        return [], list(dict.fromkeys([*entry_issues, *recovery_issues]))
    return recovered, []


def _successful_bootstrap_consumption_fields(
    status: dict[str, Any],
    outcome: str,
    validated_entries: list[dict[str, Any]],
) -> dict[str, str]:
    """Extract the one-time root receipt only for a successful full verdict.

    The normal certificate validator performs the full selector comparison
    before ``append_verdict`` is reached.  This second local boundary prevents
    a malformed caller from smuggling an unbound root marker into the signed
    ledger, which would otherwise make the bootstrap root look consumed.
    """
    if outcome != "official-certified":
        return {}
    identity = status.get("certification_identity")
    identity = identity if isinstance(identity, dict) else {}
    spec = identity.get("spec")
    spec = spec if isinstance(spec, dict) else {}
    root_id = spec.get("bootstrap_root_id")
    if not isinstance(root_id, str) or not root_id.strip():
        return {}
    selection = status.get("opponent_selection")
    if not isinstance(selection, dict):
        raise ValueError("bootstrap root consumption requires opponent selection")
    if selection.get("bootstrap_root_id", selection.get("root_id")) != root_id:
        raise ValueError("bootstrap root consumption id does not match spec")
    receipt = selection.get("bootstrap_root_receipt")
    if not isinstance(receipt, dict):
        raise ValueError("bootstrap root consumption receipt is missing")
    payload = {key: value for key, value in receipt.items() if key != "receipt_digest"}
    receipt_digest = str(receipt.get("receipt_digest") or "")
    if receipt_digest != canonical_digest(payload):
        raise ValueError("bootstrap root consumption receipt digest is invalid")
    if receipt.get("root_id") != root_id:
        raise ValueError("bootstrap root consumption receipt id does not match spec")
    opponent = selection.get("opponent")
    opponent = opponent if isinstance(opponent, dict) else {}
    if opponent.get("eligibility_receipt") != receipt:
        raise ValueError("bootstrap root consumption opponent receipt does not match")
    for entry in validated_entries:
        prior_root_id = str(entry.get("bootstrap_root_id") or "")
        if prior_root_id != root_id:
            continue
        prior_receipt = str(entry.get("bootstrap_root_receipt_digest") or "")
        if prior_receipt != receipt_digest:
            raise ValueError(
                "bootstrap root has a prior mismatched signed consumption marker"
            )
        if (
            entry.get("outcome") == "official-certified"
            and entry.get("policy_id") == "official-full-v5"
            and entry.get("mode") == "full"
            and entry.get("authoritative") is True
            and entry.get("blocking") is False
            and entry.get("classification") == "pass"
        ):
            raise ValueError("bootstrap root was already successfully consumed")
    return {
        "bootstrap_root_id": root_id,
        "bootstrap_root_receipt_digest": receipt_digest,
    }


def append_verdict(status: dict[str, Any]) -> dict[str, Any]:
    from official_certification import authoritative_verdict_status_issues
    identity = status.get("certification_identity") if isinstance(status.get("certification_identity"), dict) else {}
    candidate_hash = str(identity.get("candidate_hash") or "")
    if len(candidate_hash) != 64:
        raise ValueError("official verdict ledger requires candidate artifact hash")
    outcome = str(status.get("status") or "")
    if outcome not in {"official-certified", "official-failed", "official-inconclusive"}:
        raise ValueError(f"unsupported official verdict ledger outcome: {outcome}")
    summary = status.get("official_evidence_summary") if isinstance(status.get("official_evidence_summary"), dict) else {}
    deterministic = status.get("official_deterministic_status_receipt")
    deterministic = deterministic if isinstance(deterministic, dict) else {}
    envelope = status.get("official_job_envelope")
    envelope = envelope if isinstance(envelope, dict) else {}
    preflight_issues = authoritative_verdict_status_issues(status)
    if preflight_issues:
        raise ValueError(
            "official verdict ledger rejected unverified status: "
            + ", ".join(preflight_issues)
        )
    with _locked_ledger() as path:
        entries, issues = _read_validated(path)
        if issues:
            raise RuntimeError("official verdict ledger is invalid: " + ", ".join(issues))
        status_issues = authoritative_verdict_status_issues(
            status,
            _validated_ledger_entries=entries,
        )
        if status_issues:
            raise ValueError(
                "official verdict ledger rejected unverified status: "
                + ", ".join(status_issues)
            )
        bootstrap_consumption = _successful_bootstrap_consumption_fields(
            status,
            outcome,
            entries,
        )
        payload = {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "kind": LEDGER_ENTRY_KIND,
            "sequence": len(entries) + 1,
            "previous_entry_digest": str(entries[-1].get("entry_digest") or "") if entries else "",
            "recorded_at_ns": time.time_ns(),
            "candidate_label": str(status.get("bot") or ""),
            "candidate_hash": candidate_hash,
            "policy_id": str(status.get("policy_id") or ""),
            "mode": str(status.get("mode") or ""),
            "outcome": outcome,
            "authoritative": outcome in AUTHORITATIVE_OUTCOMES,
            "blocking": bool(summary.get("blocking")),
            "classification": str(summary.get("classification") or ""),
            "certificate_digest": str(status.get("certificate_digest") or ""),
            "deterministic_status_receipt_digest": str(deterministic.get("receipt_digest") or ""),
            "job_envelope_digest": str(envelope.get("envelope_digest") or ""),
            "request_started_ns": status.get("request_started_ns"),
            "request_completed_ns": status.get("request_completed_ns"),
            "strength_evaluation": "not_applicable",
            **bootstrap_consumption,
            **current_signer_record_fields(),
        }
        entry = {**payload, "entry_digest": canonical_digest(payload)}
        wrapper = {
            "entry": entry,
            "signature": sign_certificate(entry),
        }
        line = json.dumps(wrapper, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            _write_all(descriptor, line.encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _write_signed_head(path, entry)
        return entry


def initialize_verdict_ledger() -> dict[str, Any]:
    """Create the explicit signed genesis required before the first append."""
    with _locked_ledger() as path:
        head = ledger_head_path(path)
        if path.exists() or head.exists():
            entries, issues = _read_validated(path)
            if issues:
                raise RuntimeError(
                    "official verdict ledger already exists but is invalid: "
                    + ", ".join(issues)
                )
            return {
                "initialized": False,
                "valid": True,
                "entry_count": len(entries),
            }
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            _write_signed_head(path)
        except Exception:
            # Genesis is one operator action.  A transient signing/write error
            # must not strand an empty ledger without its signed head and make
            # the idempotent init command permanently reject the retry.
            ledger_head_path(path).unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            raise
        return {
            "initialized": True,
            "valid": True,
            "entry_count": 0,
        }


def ledger_integrity() -> dict[str, Any]:
    with _locked_ledger() as path:
        entries, issues = _read_validated(path)
    return {
        "valid": not issues,
        "issues": issues,
        "entry_count": len(entries),
        "head": entries[-1] if entries else None,
        "threat_model": dict(LEDGER_THREAT_MODEL),
    }


def latest_authoritative_verdict(candidate_hash: str) -> dict[str, Any]:
    with _locked_ledger() as path:
        entries, issues = _read_validated(path)
    if issues:
        return {"valid": False, "issues": issues, "entry": None}
    matching = [
        entry
        for entry in entries
        if entry.get("candidate_hash") == candidate_hash
        and entry.get("authoritative") is True
    ]
    return {
        "valid": True,
        "issues": [],
        "entry": matching[-1] if matching else None,
    }
