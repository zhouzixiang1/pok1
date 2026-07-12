"""Immutable durable-job identity for formal official EXE certification."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bot_artifact import canonical_digest


JOB_ENVELOPE_SCHEMA_VERSION = 3
JOB_ENVELOPE_KIND = "official-exe-durable-job-envelope"


def build_job_envelope(
    request: dict[str, Any],
    *,
    attempt: int,
    attempt_nonce: str,
    suite_dir: str | Path,
) -> dict[str, Any]:
    identity = request.get("identity") if isinstance(request.get("identity"), dict) else {}
    selection = (
        request.get("opponent_selection")
        if isinstance(request.get("opponent_selection"), dict)
        else None
    )
    operator_authorization = (
        selection.get("operator_bootstrap_authorization")
        if isinstance(selection, dict)
        and isinstance(selection.get("operator_bootstrap_authorization"), dict)
        else None
    )
    payload = {
        "schema_version": JOB_ENVELOPE_SCHEMA_VERSION,
        "kind": JOB_ENVELOPE_KIND,
        "job_id": str(request.get("job_id") or ""),
        "request_digest": str(request.get("request_digest") or ""),
        "attempt": int(attempt),
        "attempt_nonce": str(attempt_nonce or ""),
        "manager_sha256": str(request.get("manager_sha256") or ""),
        "certification_identity_digest": str(identity.get("identity_digest") or ""),
        "candidate_hash": str(identity.get("candidate_hash") or ""),
        "opponent_hash": str(identity.get("opponent_hash") or ""),
        "opponent_selection_digest": (
            canonical_digest(selection) if selection is not None else None
        ),
        "opponent_selection": selection,
        "operator_bootstrap_authorization_digest": (
            str(operator_authorization.get("authorization_digest") or "")
            if operator_authorization is not None
            else None
        ),
        "bootstrap_root_id": (
            str(selection.get("bootstrap_root_id") or selection.get("root_id") or "")
            if selection is not None
            and (selection.get("bootstrap_root_id") or selection.get("root_id"))
            else None
        ),
        "source_v": request.get("source_v"),
        "suite_path_digest": canonical_digest({
            "suite_dir": str(Path(suite_dir).expanduser().resolve()),
        }),
    }
    return {**payload, "envelope_digest": canonical_digest(payload)}


def job_envelope_issues(
    envelope: Any,
    *,
    expected_job_id: str | None = None,
    expected_request_digest: str | None = None,
    expected_attempt: int | None = None,
    expected_candidate_hash: str | None = None,
    expected_opponent_hash: str | None = None,
) -> list[str]:
    if not isinstance(envelope, dict):
        return ["official_job_envelope_missing"]
    issues: list[str] = []
    if envelope.get("schema_version") != JOB_ENVELOPE_SCHEMA_VERSION:
        issues.append("official_job_envelope_schema_mismatch")
    if envelope.get("kind") != JOB_ENVELOPE_KIND:
        issues.append("official_job_envelope_kind_mismatch")
    payload = {
        key: value
        for key, value in envelope.items()
        if key != "envelope_digest"
    }
    if envelope.get("envelope_digest") != canonical_digest(payload):
        issues.append("official_job_envelope_digest_mismatch")
    selection = envelope.get("opponent_selection")
    if selection is not None and not isinstance(selection, dict):
        issues.append("official_job_envelope_opponent_selection_invalid")
        selection = None
    expected_selection_digest = (
        canonical_digest(selection) if selection is not None else None
    )
    if envelope.get("opponent_selection_digest") != expected_selection_digest:
        issues.append("official_job_envelope_opponent_selection_digest_mismatch")
    operator_authorization = (
        selection.get("operator_bootstrap_authorization")
        if isinstance(selection, dict)
        and isinstance(selection.get("operator_bootstrap_authorization"), dict)
        else None
    )
    expected_authorization_digest = (
        str(operator_authorization.get("authorization_digest") or "")
        if operator_authorization is not None
        else None
    )
    if (
        envelope.get("operator_bootstrap_authorization_digest")
        != expected_authorization_digest
    ):
        issues.append(
            "official_job_envelope_operator_bootstrap_authorization_digest_mismatch"
        )
    expected_bootstrap_root_id = (
        str(selection.get("bootstrap_root_id") or selection.get("root_id") or "")
        if selection is not None
        and (selection.get("bootstrap_root_id") or selection.get("root_id"))
        else None
    )
    if envelope.get("bootstrap_root_id") != expected_bootstrap_root_id:
        issues.append("official_job_envelope_bootstrap_root_id_mismatch")
    for key in (
        "job_id",
        "request_digest",
        "attempt_nonce",
        "manager_sha256",
        "certification_identity_digest",
        "candidate_hash",
        "suite_path_digest",
    ):
        value = str(envelope.get(key) or "")
        if key == "attempt_nonce":
            valid = len(value) >= 32
        else:
            valid = len(value) == 64 and all(
                char in "0123456789abcdef" for char in value.lower()
            )
        if not valid:
            issues.append(f"official_job_envelope_{key}_invalid")
    try:
        if int(envelope.get("attempt", 0) or 0) < 1:
            issues.append("official_job_envelope_attempt_invalid")
    except (TypeError, ValueError):
        issues.append("official_job_envelope_attempt_invalid")
    expected = {
        "job_id": expected_job_id,
        "request_digest": expected_request_digest,
        "attempt": expected_attempt,
        "candidate_hash": expected_candidate_hash,
        "opponent_hash": expected_opponent_hash,
    }
    for key, value in expected.items():
        if value is not None and envelope.get(key) != value:
            issues.append(f"official_job_envelope_{key}_mismatch")
    return list(dict.fromkeys(issues))
