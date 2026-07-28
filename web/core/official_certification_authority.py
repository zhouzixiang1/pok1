"""Certificate authority & opponent-selection cluster for official_certification.

Extracted as a cohesive business cluster; ``official_certification.py`` retains
thin delegate shells so external ``from official_certification import <name>``
and ``monkeypatch.setattr(official_certification, "<name>", ...)`` keep
resolving.

Business responsibility (single cohesive domain):
* Certificate payload/receipt digest construction (``_certificate_payload_digest``,
  ``_build_deterministic_receipt``).
* Deterministic receipt issue extraction (``_deterministic_receipt_issues``).
* Spec reconstruction from a mapping (``_spec_from_mapping``).
* Identity integrity / opponent-selection receipt validation
  (``_identity_integrity_issues``, ``_opponent_selection_issues``).
* Portable file-manifest / published-attestation validation
  (``_validate_portable_file_manifest``, ``_validate_published_attestation_at_tag``).
* Formal certificate validation (``certificate_validation``).
* Certificate attestation publication (``publish_certificate_attestation``).
* Full-certified verdict authority (``official_full_certified``,
  ``official_certification_profile_projection``).
* Authoritative verdict-status issue extraction
  (``authoritative_verdict_status_issues``).
* Parent / active-pool eligibility gates (``parent_eligible``,
  ``active_pool_eligible``).
* Stable opponent selection receipt (``stable_official_opponent_selection``)
  and formal opponent eligibility (``official_opponent_eligibility``).
* Opponent path resolution & selection (``_bot_path_from_token``,
  ``_same_bot_path``, ``select_official_opponent``,
  ``resolve_managed_certification_spec``).

Pure validation/authority logic over already-built certification status and
published certificates.

Cross-references to symbols that remain in ``official_certification`` (the
status/schema/policy-id constants, the spec validator, the cache-key and
certification-identity helpers, the file-hash/json/safe-label helpers, the
certificate-container loader, the verdict authority, the eligibility resolvers,
``read_status``, ``hash_path`` and ``published_bot_identity``) are reached
through ``_oc.<name>`` so that test monkeypatches on
``official_certification.<name>`` propagate.

CRITICAL (wave-3 lesson): EVERY intra-companion call to a moved symbol ALSO
routes through ``_oc.<name>(...)`` so monkeypatches on
``official_certification.<name>`` propagate even when both call sites now live
in this companion.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from bot_artifact import canonical_digest
from bot_namespace import bot_name, parse_bot_version
from official_evidence_archive import (
    validate_evidence_archive,
    validate_evidence_archive_receipt,
)
from official_platform_harness import OfficialPlatformConfig, _copy_config

import official_certification as _oc  # for cross-refs


def _certificate_payload_digest(record: dict[str, Any]) -> str:
    payload = {
        key: value
        for key, value in record.items()
        if key not in {"certificate_digest", "certificate_path"}
    }
    return canonical_digest(payload)


def _build_deterministic_receipt(
    spec: _oc.CertificationSpec,
    evidence: dict[str, Any],
    evidence_path: Path,
    archive: dict[str, Any],
) -> dict[str, Any]:
    deterministic = evidence.get("deterministic") or {}
    rounds: list[dict[str, Any]] = []
    for item in evidence.get("rounds") or []:
        if not isinstance(item, dict):
            continue
        attribution = item.get("attribution") or {}
        log_summary = item.get("log_summary") or {}
        thp_summaries = item.get("thp_summaries") or []
        canonical_thp = item.get("canonical_thp") if isinstance(item.get("canonical_thp"), dict) else {}
        completion = (
            item.get("completion_evidence")
            if isinstance(item.get("completion_evidence"), dict)
            else {}
        )
        rounds.append({
            "round_kind": item.get("round_kind"),
            "round_index": item.get("round_index"),
            "target_hands": item.get("target_hands"),
            "passed": bool(item.get("passed")),
            "classification": item.get("classification"),
            "candidate_verdict": attribution.get("candidate_verdict"),
            "candidate_blocking": bool(attribution.get("candidate_blocking")),
            "countable": bool(attribution.get("countable")),
            "hands_started": int(log_summary.get("hands_started_min", 0) or 0),
            "settlements": int(log_summary.get("settlements_min", 0) or 0),
            "completed_hands": int(
                completion.get("completed_hands")
                or min(
                    int(log_summary.get("hands_started_min", 0) or 0),
                    int(log_summary.get("settlements_min", 0) or 0),
                )
            ),
            "thp_hands": int(canonical_thp.get("hand_records", 0) or 0),
            "thp_sha256": str(canonical_thp.get("sha256") or ""),
            "completion_kind": str(completion.get("kind") or "paired-tcp-settlements"),
            "completion_evidence_digest": str(completion.get("evidence_digest") or ""),
            "issue_count": len(item.get("issues") or []),
        })
    payload = {
        "schema_version": _oc.DETERMINISTIC_RECEIPT_SCHEMA_VERSION,
        "policy_id": spec.policy_id,
        "spec": {
            "self_play_rounds": spec.self_play_rounds,
            "opponent_rounds": spec.opponent_rounds,
            "target_hands": spec.target_hands,
        },
        "verdict": {
            "passed": bool(deterministic.get("passed")),
            "classification": deterministic.get("classification"),
            "blocking": bool(deterministic.get("blocking")),
            "inconclusive": bool(deterministic.get("inconclusive")),
            "candidate_verdict": deterministic.get("candidate_verdict"),
            "rounds_requested": (
                deterministic.get("rounds_requested")
                or spec.self_play_rounds + spec.opponent_rounds
            ),
            "rounds_run": (
                deterministic.get("rounds_run")
                or len(evidence.get("rounds") or [])
            ),
            "target_hands": deterministic.get("target_hands") or spec.target_hands,
            "issue_count": len(deterministic.get("issues") or []),
        },
        "rounds": rounds,
        "evidence_sha256": _oc._file_sha256(evidence_path),
        "archive_sha256": archive.get("archive_sha256"),
        "archive_manifest_digest": archive.get("manifest_digest"),
        "strength_evaluation": "not_applicable",
    }
    payload["receipt_digest"] = canonical_digest(payload)
    return payload


def _deterministic_receipt_issues(
    receipt: Any,
    spec: _oc.CertificationSpec,
    *,
    evidence_manifest: dict[str, Any],
    archive_receipt: dict[str, Any],
) -> list[str]:
    """Delegate to official_certification_receipt_validation."""
    return _oc._ocrv._deterministic_receipt_issues(receipt, spec, evidence_manifest=evidence_manifest, archive_receipt=archive_receipt)


def _spec_from_mapping(data: dict[str, Any]) -> _oc.CertificationSpec:
    retired = sorted(_oc.RETIRED_BOOTSTRAP_SPEC_FIELDS.intersection(data))
    if retired:
        raise ValueError(
            "retired signed-ledger bootstrap spec fields are forbidden: "
            + ", ".join(retired)
        )
    mode = str(data.get("mode") or "")
    spec = _oc.CertificationSpec(
        mode=mode,
        policy_id=str(
            data.get("policy_id")
            or (_oc.FULL_POLICY_ID if mode == "full" else f"official-{mode}-v1")
        ),
        candidate=str(data.get("candidate") or ""),
        opponent=str(data.get("opponent")) if data.get("opponent") else None,
        self_play_rounds=int(data.get("self_play_rounds", 0) or 0),
        opponent_rounds=int(data.get("opponent_rounds", 0) or 0),
        target_hands=int(data.get("target_hands", 0) or 0),
        round_timeout_sec=float(data.get("round_timeout_sec", 0.0) or 0.0),
        no_progress_timeout_sec=float(data.get("no_progress_timeout_sec", 0.0) or 0.0),
        bootstrap_control_id=(
            str(data.get("bootstrap_control_id")).strip()
            if data.get("bootstrap_control_id") is not None
            else None
        ),
        quality_admission=(
            dict(data.get("quality_admission"))
            if isinstance(data.get("quality_admission"), dict)
            else None
        ),
    )
    _oc.validate_spec(spec)
    return spec


def _config_for_spec(
    spec: _oc.CertificationSpec,
    config: OfficialPlatformConfig | None = None,
) -> OfficialPlatformConfig:
    return _copy_config(
        config or OfficialPlatformConfig(),
        round_timeout_sec=spec.round_timeout_sec,
        no_progress_timeout_sec=spec.no_progress_timeout_sec,
        results_dir=_oc.certification_root() / spec.mode,
    )


def _identity_integrity_issues(identity: Any, spec: _oc.CertificationSpec) -> list[str]:
    if not isinstance(identity, dict):
        return ["certificate_identity_missing"]
    issues: list[str] = []
    identity_payload = {
        key: value
        for key, value in identity.items()
        if key != "identity_digest"
    }
    if identity.get("identity_digest") != canonical_digest(identity_payload):
        issues.append("certificate_identity_digest_mismatch")
    platform = identity.get("platform")
    if not isinstance(platform, dict):
        issues.append("certificate_platform_identity_missing")
    elif identity.get("platform_fingerprint") != canonical_digest(platform):
        issues.append("certificate_platform_fingerprint_mismatch")
    if identity.get("policy_id") != spec.policy_id:
        issues.append("certificate_identity_policy_mismatch")
    if identity.get("spec") != _oc.spec_record(spec):
        issues.append("certificate_identity_spec_mismatch")
    return issues


def _opponent_selection_issues(
    selection: Any,
    spec: _oc.CertificationSpec,
    identity: dict[str, Any],
    *,
    allow_consumed_bootstrap: bool = False,
    candidate_path: str | Path | None = None,
    _validated_ledger_entries: list[dict[str, Any]] | None = None,
) -> list[str]:
    if spec.mode != "full":
        return []
    if not isinstance(selection, dict) or selection.get("selected") is not True:
        return ["certificate_official_opponent_selection_missing"]
    opponent = selection.get("opponent")
    if not isinstance(opponent, dict) or opponent.get("eligible") is not True:
        return ["certificate_official_opponent_selection_invalid"]
    issues: list[str] = []
    try:
        selected_path = Path(str(opponent.get("path") or "")).resolve()
        expected_path = Path(str(spec.opponent or "")).resolve()
        if selected_path != expected_path:
            issues.append("certificate_official_opponent_path_mismatch")
    except Exception:
        issues.append("certificate_official_opponent_path_invalid")
    if str(opponent.get("artifact_hash") or "") != str(identity.get("opponent_hash") or ""):
        issues.append("certificate_official_opponent_hash_mismatch")
    reason = opponent.get("reason")
    if spec.bootstrap_control_id is not None:
        # The first strict run uses current system-owned typed-policy bytes,
        # never an archived bot.  Every selection/receipt field is revalidated.
        if selection.get("bootstrap_control_id") != spec.bootstrap_control_id:
            issues.append("certificate_bootstrap_control_id_mismatch")
        if reason != "first_strict_control_bootstrap":
            issues.append("certificate_bootstrap_control_reason_invalid")
        try:
            if _validated_ledger_entries is None:
                from official_bootstrap import (
                    validate_first_strict_control_selection,
                )

                validation = validate_first_strict_control_selection(
                    selection,
                    spec.bootstrap_control_id,
                    candidate_path or spec.candidate,
                    allow_consumed=allow_consumed_bootstrap,
                    allow_published=allow_consumed_bootstrap,
                )
            else:
                from official_bootstrap import (
                    validate_first_strict_control_selection_from_entries,
                )

                validation = (
                    validate_first_strict_control_selection_from_entries(
                        selection,
                        spec.bootstrap_control_id,
                        candidate_path or spec.candidate,
                        _validated_ledger_entries,
                        allow_consumed=allow_consumed_bootstrap,
                        allow_published=allow_consumed_bootstrap,
                    )
                )
            if not validation.get("valid"):
                issues.extend(
                    f"certificate_{item}"
                    for item in (
                        validation.get("issues")
                        or ["bootstrap_control_selection_invalid"]
                    )
                )
        except Exception as exc:
            issues.append(
                "certificate_bootstrap_control_validation_error:"
                f"{type(exc).__name__}:{str(exc)[:160]}"
            )
        return list(dict.fromkeys(issues))

    if selection.get("bootstrap_control_id") is not None:
        issues.append("certificate_bootstrap_control_unexpected")
    # A full EXE certificate is the only production authorization for an
    # official opponent.  Content-bound migration grants remain available for
    # parent/rating-pool history, but must never validate a formal opponent
    # receipt (including a resumed durable job).
    if reason != "official_certified":
        issues.append("certificate_official_opponent_reason_invalid")
    receipt = opponent.get("eligibility_receipt")
    if not isinstance(receipt, dict):
        issues.append("certificate_official_opponent_eligibility_receipt_missing")
        return issues
    receipt_payload = {
        key: value
        for key, value in receipt.items()
        if key != "receipt_digest"
    }
    if receipt.get("receipt_digest") != canonical_digest(receipt_payload):
        issues.append("certificate_official_opponent_eligibility_receipt_digest_mismatch")
    if receipt.get("schema_version") != _oc.OPPONENT_ELIGIBILITY_RECEIPT_SCHEMA_VERSION:
        issues.append("certificate_official_opponent_eligibility_receipt_schema_mismatch")
    if receipt.get("role") != "official_opponent":
        issues.append("certificate_official_opponent_eligibility_receipt_role_mismatch")
    if str(receipt.get("bot") or "") != str(opponent.get("bot") or ""):
        issues.append("certificate_official_opponent_eligibility_receipt_bot_mismatch")
    if str(receipt.get("artifact_hash") or "") != str(opponent.get("artifact_hash") or ""):
        issues.append("certificate_official_opponent_eligibility_receipt_hash_mismatch")
    expected_kind = "official_full_certificate"
    if receipt.get("kind") != expected_kind:
        issues.append("certificate_official_opponent_eligibility_receipt_kind_mismatch")
    if receipt.get("policy_id") != _oc.FULL_POLICY_ID:
        issues.append("certificate_official_opponent_certificate_policy_mismatch")
    certificate_digest = str(receipt.get("certificate_digest") or "")
    if len(certificate_digest) != 64 or any(
        ch not in "0123456789abcdef" for ch in certificate_digest.lower()
    ):
        issues.append("certificate_official_opponent_certificate_digest_invalid")
    return issues


def _validate_portable_file_manifest(
    manifest: Any,
    *,
    label: str,
) -> tuple[Path | None, list[str], bool]:
    """Validate retained bytes when present, otherwise validate the hash receipt."""
    path, issues = _oc._validate_certificate_file_manifest(manifest, label=label)
    if not issues:
        return path, [], True
    if not isinstance(manifest, dict):
        return None, issues, False
    only_unretained = issues == [f"certificate_{label}_missing"]
    digest = str(manifest.get("sha256") or "")
    size = manifest.get("size_bytes")
    digest_ok = len(digest) == 64 and all(ch in "0123456789abcdef" for ch in digest.lower())
    try:
        size_ok = int(size) >= 0
    except (TypeError, ValueError):
        size_ok = False
    if only_unretained and digest_ok and size_ok:
        return None, [], False
    return None, issues, False


def _validate_published_attestation_at_tag(
    candidate_path: Path,
    published_identity: dict[str, Any],
    expected_certificate_digest: str,
) -> list[str]:
    path = _oc.published_certificate_path(candidate_path)
    record, attestation, issues = _oc._load_certificate_container(path)
    if not isinstance(record, dict) or not isinstance(attestation, dict):
        return [*issues, "published_attestation_missing"]
    if attestation.get("bot") != candidate_path.name:
        issues.append("published_attestation_bot_mismatch")
    if record.get("certificate_digest") != expected_certificate_digest:
        issues.append("published_attestation_certificate_mismatch")
    try:
        relative = path.relative_to(_oc.ROOT).as_posix()
    except ValueError:
        return [*issues, "published_attestation_outside_repository"]
    tag = str(published_identity.get("tag") or "")
    if not tag:
        return [*issues, "published_attestation_tag_missing"]
    try:
        result = subprocess.run(
            ["git", "show", f"{tag}:{relative}"],
            cwd=str(_oc.ROOT),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception as exc:
        return [
            *issues,
            f"published_attestation_git_read_error:{type(exc).__name__}:{str(exc)[:160]}",
        ]
    if result.returncode != 0:
        return [*issues, "completion_tag_missing_published_attestation"]
    try:
        tagged = json.loads(result.stdout)
    except Exception as exc:
        return [
            *issues,
            f"completion_tag_attestation_invalid_json:{type(exc).__name__}:{str(exc)[:120]}",
        ]
    if tagged != attestation:
        issues.append("working_attestation_differs_from_completion_tag")
    return issues


def certificate_validation(
    status: dict[str, Any],
    *,
    candidate: str | Path | None = None,
    config: OfficialPlatformConfig | None = None,
    require_published: bool = False,
    ledger_fresh: bool = True,
    _skip_ledger_check: bool = False,
    _validated_ledger_entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    issues: list[str] = []
    path_value = status.get("certificate_path")
    record_path = Path(str(path_value)) if path_value else Path()
    record, attestation, container_issues = (
        _oc._load_certificate_container(record_path)
        if path_value
        else (None, None, ["content_bound_certificate_missing"])
    )
    issues.extend(container_issues)
    if not isinstance(record, dict):
        return {"valid": False, "issues": list(dict.fromkeys(issues))}
    portable = isinstance(attestation, dict)
    if record.get("schema_version") != _oc.CERTIFICATE_SCHEMA_VERSION:
        issues.append("certificate_schema_version_mismatch")
    if record.get("kind") != "official-exe-compliance-certificate":
        issues.append("certificate_kind_mismatch")
    digest = str(record.get("certificate_digest") or "")
    if not digest or digest != _oc._certificate_payload_digest(record):
        issues.append("certificate_digest_mismatch")
    if digest != str(status.get("certificate_digest") or ""):
        issues.append("status_certificate_digest_mismatch")
    issuer = record.get("issuer") if isinstance(record.get("issuer"), dict) else {}
    from official_certificate_signing import (
        SIGNATURE_NAMESPACE,
        SIGNER_PRINCIPAL,
        verify_certificate_signature,
    )

    if issuer.get("principal") != SIGNER_PRINCIPAL:
        issues.append("official_certificate_issuer_principal_mismatch")
    if issuer.get("namespace") != SIGNATURE_NAMESPACE:
        issues.append("official_certificate_issuer_namespace_mismatch")
    signature = ""
    if portable:
        signature = str((attestation or {}).get("signature") or "")
    else:
        signature_path_value = status.get("certificate_signature_path")
        signature_path = (
            Path(str(signature_path_value))
            if signature_path_value
            else record_path.with_suffix(".sig")
        )
        try:
            if signature_path.is_symlink() or not signature_path.is_file():
                issues.append("official_certificate_signature_missing")
            else:
                signature = signature_path.read_text(encoding="utf-8")
                expected_signature_sha = str(status.get("certificate_signature_sha256") or "")
                if expected_signature_sha and _oc._file_sha256(signature_path) != expected_signature_sha:
                    issues.append("official_certificate_signature_sha256_mismatch")
        except OSError as exc:
            issues.append(f"official_certificate_signature_read_error:{type(exc).__name__}")
    signature_validation = verify_certificate_signature(record, signature)
    issues.extend(signature_validation.get("issues") or [])
    if (
        signature_validation.get("valid")
        and issuer.get("key_fingerprint")
        != signature_validation.get("key_fingerprint")
    ):
        issues.append("official_certificate_issuer_fingerprint_mismatch")
    try:
        spec = _oc._spec_from_mapping(record.get("spec") or {})
    except Exception as exc:
        return {
            "valid": False,
            "issues": list(dict.fromkeys([
                *issues,
                f"certificate_spec_invalid:{type(exc).__name__}:{str(exc)[:200]}",
            ])),
        }
    spec_candidate_label = _oc._safe_label(spec.candidate)
    requested_candidate_label = _oc._safe_label(candidate) if candidate is not None else spec_candidate_label
    candidate_path = (
        Path(candidate).expanduser().resolve()
        if candidate is not None
        else Path(spec.candidate).expanduser().resolve()
    )
    if record.get("candidate_label") != spec_candidate_label:
        issues.append("certificate_candidate_label_missing_or_mismatch")
    if requested_candidate_label != spec_candidate_label:
        issues.append("certificate_candidate_version_mismatch")
    if portable and (attestation or {}).get("bot") != requested_candidate_label:
        issues.append("published_attestation_bot_mismatch")
    record_identity = record.get("identity") or {}
    if record_identity.get("runner_provenance") != _oc.PRODUCTION_RUNNER_PROVENANCE:
        issues.append("certificate_runner_provenance_not_production_official_exe")
    if record_identity.get("authority_scope") != "production":
        issues.append("certificate_authority_scope_not_production")
    if record_identity.get("test_only") is not False:
        issues.append("certificate_test_only_authority_forbidden")
    if status.get("test_only") is True:
        issues.append("status_test_only_authority_forbidden")
    if portable:
        issues.extend(_oc._identity_integrity_issues(record_identity, spec))
        current_identity = record_identity
    else:
        current_identity = _oc.certification_identity(spec, _oc._config_for_spec(spec, config))
        if record_identity != current_identity:
            issues.append("certificate_identity_stale")
    status_identity = status.get("certification_identity") or {}
    if status_identity != current_identity:
        issues.append("status_identity_stale")
    issues.extend(
        _oc._opponent_selection_issues(
            record.get("opponent_selection"),
            spec,
            current_identity,
            allow_consumed_bootstrap=not _skip_ledger_check,
            candidate_path=candidate_path,
            _validated_ledger_entries=_validated_ledger_entries,
        )
    )
    if spec.mode == "full":
        from official_job_envelope import job_envelope_issues

        issues.extend(job_envelope_issues(
            record.get("job_envelope"),
            expected_candidate_hash=str(current_identity.get("candidate_hash") or ""),
            expected_opponent_hash=str(current_identity.get("opponent_hash") or ""),
        ))
    evidence_record = record.get("evidence") if isinstance(record.get("evidence"), dict) else {}
    if portable:
        archive_validation = validate_evidence_archive_receipt(
            record.get("evidence_archive"),
            expected_evidence_sha256=str(evidence_record.get("sha256") or ""),
        )
        retained_archive_validation = validate_evidence_archive(
            record.get("evidence_archive"),
            expected_evidence_sha256=str(evidence_record.get("sha256") or ""),
        )
    else:
        archive_validation = validate_evidence_archive(
            record.get("evidence_archive"),
            expected_evidence_sha256=str(evidence_record.get("sha256") or ""),
        )
        retained_archive_validation = archive_validation
    if spec.mode == "full":
        issues.extend(archive_validation.get("issues") or [])
    try:
        if _oc.hash_path(candidate_path) != current_identity.get("candidate_hash"):
            issues.append("candidate_artifact_hash_mismatch")
    except Exception as exc:
        issues.append(
            f"candidate_artifact_integrity_error:{type(exc).__name__}:{str(exc)[:160]}"
        )
    if portable:
        evidence_path, evidence_issues, evidence_retained = _oc._validate_portable_file_manifest(
            record.get("evidence"), label="evidence"
        )
    else:
        evidence_path, evidence_issues = _oc._validate_certificate_file_manifest(
            record.get("evidence"), label="evidence"
        )
        evidence_retained = evidence_path is not None and not evidence_issues
    issues.extend(evidence_issues)
    if evidence_path is not None and not evidence_issues:
        issues.extend(_oc._validate_retained_evidence_artifacts(evidence_path))
    try:
        issues.extend(_oc._deterministic_receipt_issues(
            record.get("deterministic_receipt"),
            spec,
            evidence_manifest=evidence_record,
            archive_receipt=(
                record.get("evidence_archive")
                if isinstance(record.get("evidence_archive"), dict)
                else {}
            ),
        ))
    except Exception as exc:
        issues.append(
            f"certificate_deterministic_receipt_invalid:{type(exc).__name__}:{str(exc)[:160]}"
        )
    if require_published:
        published = _oc.published_bot_identity(candidate_path)
        if not published.get("published"):
            issues.append("certificate_candidate_not_published")
        metadata = published.get("tag_metadata") or {}
        if metadata.get("official-certificate") != digest:
            issues.append("completion_tag_certificate_digest_mismatch")
        if metadata.get("official-candidate-hash") != current_identity.get("candidate_hash"):
            issues.append("completion_tag_candidate_hash_mismatch")
        if metadata.get("official-policy") != spec.policy_id:
            issues.append("completion_tag_policy_mismatch")
        issues.extend(
            _oc._validate_published_attestation_at_tag(
                candidate_path,
                published,
                digest,
            )
        )
    if spec.mode == "full" and not _skip_ledger_check:
        try:
            from official_verdict_ledger import latest_authoritative_verdict

            ledger = latest_authoritative_verdict(
                str(current_identity.get("candidate_hash") or ""),
                fresh=ledger_fresh,
            )
            if not ledger.get("valid"):
                issues.extend(ledger.get("issues") or ["official_verdict_ledger_invalid"])
            else:
                ledger_entry = ledger.get("entry")
                if not isinstance(ledger_entry, dict):
                    issues.append("official_verdict_ledger_certificate_entry_missing")
                elif ledger_entry.get("outcome") != _oc.STATUS_CERTIFIED:
                    issues.append("official_verdict_ledger_latest_outcome_not_certified")
                elif ledger_entry.get("certificate_digest") != digest:
                    issues.append("official_verdict_ledger_certificate_digest_mismatch")
        except Exception as exc:
            issues.append(
                f"official_verdict_ledger_validation_error:{type(exc).__name__}:{str(exc)[:160]}"
            )
    return {
        "valid": not issues,
        "issues": list(dict.fromkeys(issues)),
        "certificate_digest": digest,
        "spec": _oc.spec_record(spec),
        "identity": current_identity,
        "published_attestation": portable or require_published,
        "evidence_retained": evidence_retained,
        "standalone_evidence_retained": evidence_retained,
        "llm_analysis_retained": False,
        "evidence_archive_retained": bool(retained_archive_validation.get("valid")),
        "archive_evidence_available": bool(retained_archive_validation.get("valid")),
        "raw_evidence_available": bool(
            evidence_retained or retained_archive_validation.get("valid")
        ),
        "signature_valid": bool(signature_validation.get("valid")),
    }


def publish_certificate_attestation(
    status: dict[str, Any],
    candidate: str | Path,
    *,
    config: OfficialPlatformConfig | None = None,
) -> dict[str, Any]:
    """Write the compact certificate receipt that must ship with the bot commit."""
    identity = (
        status.get("certification_identity")
        if isinstance(status.get("certification_identity"), dict)
        else {}
    )
    if status.get("test_only") is True or identity.get("test_only") is not False:
        raise RuntimeError("cannot publish test-only official certificate")
    validation = _oc.certificate_validation(status, candidate=candidate, config=config)
    if not validation.get("valid"):
        raise RuntimeError(
            "cannot publish invalid official certificate: "
            + ", ".join(validation.get("issues") or [])
        )
    source = Path(str(status.get("certificate_path") or ""))
    record, _attestation, issues = _oc._load_certificate_container(source)
    if issues or not isinstance(record, dict):
        raise RuntimeError(
            "cannot read official certificate for publication: "
            + ", ".join(issues or ["missing_record"])
        )
    signature_path = Path(str(status.get("certificate_signature_path") or source.with_suffix(".sig")))
    if signature_path.is_symlink() or not signature_path.is_file():
        raise RuntimeError("cannot publish official certificate without detached signature")
    signature = signature_path.read_text(encoding="utf-8")
    destination = _oc.published_certificate_path(candidate)
    portable_record = json.loads(json.dumps(record, ensure_ascii=False))
    try:
        relative_destination = destination.relative_to(_oc.ROOT).as_posix()
    except ValueError:
        relative_destination = str(destination)
    payload = {
        "schema_version": _oc.PUBLISHED_ATTESTATION_SCHEMA_VERSION,
        "kind": "official-platform-compliance-attestation",
        "bot": _oc._safe_label(candidate),
        "published_at": _oc.now_iso(),
        "certificate_digest": record.get("certificate_digest"),
        "signature": signature,
        "signature_sha256": hashlib.sha256(signature.encode("utf-8")).hexdigest(),
        "issuer": record.get("issuer"),
        "raw_evidence_retention": "content-addressed-local-archive",
        "certificate": portable_record,
    }
    payload["attestation_digest"] = _oc._attestation_payload_digest(payload)
    _oc._write_json(destination, payload)
    return {
        "path": str(destination),
        "relative_path": relative_destination,
        "attestation_digest": payload["attestation_digest"],
        "certificate_digest": record.get("certificate_digest"),
    }


def official_full_certified(
    status: dict[str, Any],
    candidate: str | Path | None = None,
    *,
    config: OfficialPlatformConfig | None = None,
    require_published: bool = False,
    ledger_fresh: bool = True,
) -> bool:
    status_identity = (
        status.get("certification_identity")
        if isinstance(status.get("certification_identity"), dict)
        else {}
    )
    if (
        status.get("test_only") is True
        or status_identity.get("test_only") is not False
        or status_identity.get("authority_scope") != "production"
        or status_identity.get("runner_provenance") != _oc.PRODUCTION_RUNNER_PROVENANCE
    ):
        return False
    verdict = _oc.official_compliance_verdict(status)
    if not (
        status.get("status") == _oc.STATUS_CERTIFIED
        and status.get("mode") == "full"
        and status.get("policy_id") == _oc.FULL_POLICY_ID
        and bool(verdict.get("ok"))
        and not bool(verdict.get("inconclusive"))
        and not bool(verdict.get("blocking"))
    ):
        return False
    validation = _oc.certificate_validation(
        status,
        candidate=candidate,
        config=config,
        require_published=require_published,
        ledger_fresh=ledger_fresh,
    )
    if not validation.get("valid"):
        return False
    identity = validation.get("identity") if isinstance(validation.get("identity"), dict) else {}
    from official_verdict_ledger import latest_authoritative_verdict

    ledger = latest_authoritative_verdict(
        str(identity.get("candidate_hash") or ""),
        fresh=ledger_fresh,
    )
    entry = ledger.get("entry") if ledger.get("valid") else None
    return bool(
        isinstance(entry, dict)
        and entry.get("outcome") == _oc.STATUS_CERTIFIED
        and entry.get("certificate_digest") == status.get("certificate_digest")
    )


def official_certification_profile_projection(
    status: dict[str, Any],
    candidate: str | Path,
    *,
    require_published: bool = False,
) -> dict[str, Any]:
    """Project the formal profile only after reopening the signed certificate.

    HTTP/UI consumers must not infer the first-strict exception from ``v143``
    or trust profile-looking fields copied into mutable status JSON.  The
    signed certificate spec and its validated opponent selection are the sole
    authority once publication has cleared the parked checkpoint.
    """

    status_identity = (
        status.get("certification_identity")
        if isinstance(status.get("certification_identity"), dict)
        else {}
    )
    verdict = _oc.official_compliance_verdict(status)
    if (
        status.get("status") != _oc.STATUS_CERTIFIED
        or status.get("mode") != "full"
        or status.get("policy_id") != _oc.FULL_POLICY_ID
        or status.get("test_only") is True
        or status_identity.get("test_only") is not False
        or status_identity.get("authority_scope") != "production"
        or status_identity.get("runner_provenance") != _oc.PRODUCTION_RUNNER_PROVENANCE
        or verdict.get("ok") is not True
        or verdict.get("blocking") is not False
        or verdict.get("inconclusive") is not False
    ):
        return {}
    validation = _oc.certificate_validation(
        status,
        candidate=candidate,
        require_published=require_published,
    )
    if validation.get("valid") is not True:
        return {}
    spec = validation.get("spec")
    if not isinstance(spec, dict):
        return {}
    if (
        spec.get("mode") != "full"
        or spec.get("policy_id") != _oc.FULL_POLICY_ID
        or spec.get("self_play_rounds") != 5
        or spec.get("opponent_rounds") != 3
        or spec.get("target_hands") != 70
    ):
        return {}

    bootstrap_control_id = spec.get("bootstrap_control_id")
    if bootstrap_control_id is None:
        certification_profile = _oc.FULL_POLICY_ID
        opponent_authority = "strict_published_pool"
    else:
        from first_strict_control import CONTROL_ID

        if bootstrap_control_id != CONTROL_ID:
            return {}
        certification_profile = CONTROL_ID
        opponent_authority = "system_control"

    return {
        "certification_profile": certification_profile,
        "opponent_authority": opponent_authority,
        # Official EXE evidence is compliance-only and never contributes to
        # strategy selection or the native strength pool.
        "strength_evidence_weight": 0,
        "strategy_evidence_weight": 0,
        # Certificate validation has already proven the deterministic receipt
        # contains all eight passing rounds.  Reconstructing this from mutable
        # summaries would create a weaker public authority.
        "formal_summary": {
            "self_play_rounds": 5,
            "opponent_rounds": 3,
            "target_hands": 70,
            "rounds_requested": 8,
            "rounds_run": 8,
            "passed_rounds": 8,
            "failed_rounds": 0,
        },
    }


def authoritative_verdict_status_issues(
    status: Any,
    *,
    _validated_ledger_entries: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Validate a status before the certifier signs it into the verdict ledger."""
    if not isinstance(status, dict):
        return [f"official_verdict_status_invalid_type:{type(status).__name__}"]
    issues: list[str] = []
    outcome = str(status.get("status") or "")
    if outcome not in {_oc.STATUS_CERTIFIED, _oc.STATUS_FAILED, _oc.STATUS_INCONCLUSIVE}:
        issues.append("official_verdict_status_outcome_not_formal")
    if status.get("mode") != "full" or status.get("policy_id") != _oc.FULL_POLICY_ID:
        issues.append("official_verdict_status_policy_not_full")
    identity = (
        status.get("certification_identity")
        if isinstance(status.get("certification_identity"), dict)
        else {}
    )
    if status.get("test_only") is True or identity.get("test_only") is not False:
        issues.append("official_verdict_status_test_only")
    if identity.get("authority_scope") != "production":
        issues.append("official_verdict_status_authority_scope_invalid")
    if identity.get("runner_provenance") != _oc.PRODUCTION_RUNNER_PROVENANCE:
        issues.append("official_verdict_status_runner_provenance_invalid")
    try:
        spec = _oc._spec_from_mapping(identity.get("spec") or {})
    except Exception as exc:
        return list(dict.fromkeys([
            *issues,
            f"official_verdict_status_spec_invalid:{type(exc).__name__}:{str(exc)[:160]}",
        ]))
    if spec.mode != "full" or spec.policy_id != _oc.FULL_POLICY_ID:
        issues.append("official_verdict_status_spec_not_full")
    issues.extend(_oc._identity_integrity_issues(identity, spec))
    candidate_hash = str(identity.get("candidate_hash") or "")
    try:
        if len(candidate_hash) != 64 or _oc.hash_path(spec.candidate) != candidate_hash:
            issues.append("official_verdict_status_candidate_identity_invalid")
    except Exception as exc:
        issues.append(
            f"official_verdict_status_candidate_identity_error:{type(exc).__name__}:{str(exc)[:120]}"
        )
    if status.get("bot") != _oc._safe_label(spec.candidate):
        issues.append("official_verdict_status_candidate_label_mismatch")
    envelope = (
        status.get("official_job_envelope")
        if isinstance(status.get("official_job_envelope"), dict)
        else None
    )
    try:
        from official_job_envelope import job_envelope_issues

        issues.extend(job_envelope_issues(
            envelope,
            expected_candidate_hash=candidate_hash,
            expected_opponent_hash=str(identity.get("opponent_hash") or ""),
        ))
    except Exception as exc:
        issues.append(
            f"official_verdict_status_job_envelope_error:{type(exc).__name__}:{str(exc)[:120]}"
        )
    result = status.get("result") if isinstance(status.get("result"), dict) else {}
    issues.extend(_oc._job_envelope_report_issues(result, envelope))
    started = status.get("request_started_ns")
    completed = status.get("request_completed_ns")
    if (
        not isinstance(started, int)
        or isinstance(started, bool)
        or not isinstance(completed, int)
        or isinstance(completed, bool)
        or started <= 0
        or completed < started
    ):
        issues.append("official_verdict_status_request_interval_invalid")
    if outcome == _oc.STATUS_CERTIFIED:
        validation = _oc.certificate_validation(
            status,
            candidate=spec.candidate,
            _skip_ledger_check=True,
            _validated_ledger_entries=_validated_ledger_entries,
        )
        issues.extend(validation.get("issues") or [])
    else:
        issues.extend(_oc._deterministic_status_receipt_issues(
            status,
            candidate=spec.candidate,
        ))
        receipt = status.get("official_deterministic_status_receipt")
        verdict = receipt.get("verdict") if isinstance(receipt, dict) else {}
        verdict = verdict if isinstance(verdict, dict) else {}
        if outcome == _oc.STATUS_FAILED and not (
            verdict.get("blocking") is True
            and verdict.get("inconclusive") is False
        ):
            issues.append("official_verdict_status_failure_not_deterministically_blocking")
        if outcome == _oc.STATUS_INCONCLUSIVE and verdict.get("inconclusive") is not True:
            issues.append("official_verdict_status_inconclusive_not_deterministic")
    return list(dict.fromkeys(str(issue) for issue in issues if str(issue)))


def _official_verdict_ledger_issues() -> list[str]:
    try:
        from official_verdict_ledger import ledger_integrity

        validation = ledger_integrity(fresh=True)
    except Exception as exc:
        return [
            f"official_verdict_ledger_validation_error:{type(exc).__name__}:{str(exc)[:160]}"
        ]
    if validation.get("valid"):
        return []
    return list(validation.get("issues") or ["official_verdict_ledger_invalid"])


def parent_eligible(candidate: str | Path) -> bool:
    return bool(_oc.strict_role_eligibility(candidate, "parent_source").get("eligible"))


def active_pool_eligible(candidate: str | Path) -> bool:
    parent = _oc.strict_role_eligibility(candidate, "parent_source")
    rating = _oc.strict_role_eligibility(candidate, "rating_pool")
    return bool(parent.get("eligible") and rating.get("eligible"))


def _digest_bound_receipt(payload: dict[str, Any]) -> dict[str, Any]:
    return {**payload, "receipt_digest": canonical_digest(payload)}


def _official_certificate_opponent_receipt(
    candidate: str | Path,
    status: dict[str, Any],
) -> dict[str, Any]:
    identity = _oc.published_bot_identity(candidate)
    return _oc._digest_bound_receipt({
        "schema_version": _oc.OPPONENT_ELIGIBILITY_RECEIPT_SCHEMA_VERSION,
        "kind": "official_full_certificate",
        "role": "official_opponent",
        "bot": str(identity.get("label") or Path(candidate).name),
        "artifact_hash": str(identity.get("artifact_hash") or ""),
        "policy_id": str(status.get("policy_id") or ""),
        "certificate_digest": str(status.get("certificate_digest") or ""),
    })


def stable_official_opponent_selection(
    selection: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return the immutable authorization receipt used by jobs and certificates."""
    if not isinstance(selection, dict):
        return None
    opponent = selection.get("opponent") if isinstance(selection.get("opponent"), dict) else {}
    stable = {
        "selected": bool(selection.get("selected")),
        "candidate": str(selection.get("candidate") or ""),
        "opponent": {
            key: opponent.get(key)
            for key in (
                "bot",
                "path",
                "artifact_hash",
                "tag",
                "tag_object",
                "eligible",
                "reason",
                "eligibility_receipt",
            )
        },
    }
    # Keep ordinary v5 selection records byte-compatible.  Bootstrap fields
    # exist only on the explicit one-time path and are part of its job/cert
    # identity, not a fallback for normal opponent selection.
    bootstrap_control_id = selection.get("bootstrap_control_id")
    if bootstrap_control_id is not None:
        stable["eligible"] = bool(selection.get("eligible"))
        stable["reason"] = selection.get("reason")
        stable["kind"] = selection.get("kind")
        stable["bootstrap_control_id"] = str(bootstrap_control_id or "")
        stable["bootstrap_control_receipt"] = selection.get(
            "bootstrap_control_receipt"
        )
        stable["candidate_binding"] = selection.get("candidate_binding")
        stable["operator_bootstrap_authorization"] = selection.get(
            "operator_bootstrap_authorization"
        )
        # These negative-authority flags are part of the control receipt, not
        # optional diagnostics.  Dropping them while freezing a durable job
        # would make the later exact selector validator compare ``None`` with
        # ``False`` and, more importantly, would lose the zero-strength role
        # boundary from the certificate identity.
        for key in (
            "authority",
            "normal_official_opponent",
            "strength_admitted",
            "rating_eligible",
        ):
            stable["opponent"][key] = opponent.get(key)
    return stable


def official_opponent_eligibility(
    candidate: str | Path,
    *,
    allow_bootstrap_grandfather: bool = False,
    target_version: int | None = None,
    certified_alternatives: int | None = None,
) -> dict[str, Any]:
    """Return formal official-EXE opponent eligibility.

    A published signed full certificate is the sole production authorization.
    ``allow_bootstrap_grandfather`` is retained for API compatibility only;
    it never authorizes a content-bound migration grant in this path.
    """
    version = parse_bot_version(Path(candidate).name)
    lifecycle = (
        _oc.epoch_lifecycle_eligibility(version)
        if version is not None
        else {"eligible": False, "reason": "invalid_national_bot_label"}
    )
    if not lifecycle.get("eligible"):
        return {
            "eligible": False,
            "reason": lifecycle.get("reason") or "national_epoch_ineligible",
            "lifecycle": lifecycle,
        }
    status = _oc.read_status(candidate)
    verdict = _oc.official_compliance_verdict(status)
    if bool(verdict.get("blocking")):
        return {
            "eligible": False,
            "reason": "blocking_official_failure",
            "status": status.get("status"),
            "mode": status.get("mode"),
            "verdict": verdict,
        }
    authorization = _oc.strict_role_eligibility(candidate, "official_opponent")
    if not authorization.get("eligible"):
        issues = list(authorization.get("issues") or [])
        certificate_missing = any(
            issue in {
                "signed_full_official_certificate_required",
                "official_certificate_digest_invalid",
            }
            for issue in issues
        )
        return {
            "eligible": False,
            "reason": (
                "official_full_certificate_required"
                if certificate_missing
                else authorization.get("reason") or "strict_national_bot_spec_required"
            ),
            "authorization": authorization,
            "bootstrap_requested_but_disabled": bool(allow_bootstrap_grandfather),
            "grandfathered": False,
        }
    if _oc.official_full_certified(status, candidate, require_published=True):
        reason = "official_certified"
        priority = 0
        eligibility_receipt = _oc._official_certificate_opponent_receipt(candidate, status)
    else:
        return {
            "eligible": False,
            "reason": "official_full_certificate_required",
            "status": status.get("status"),
            "mode": status.get("mode"),
            "verdict": verdict,
            "bootstrap_requested_but_disabled": bool(allow_bootstrap_grandfather),
            "grandfathered": False,
        }
    return {
        "eligible": True,
        "reason": reason,
        "priority": priority,
        "status": status.get("status"),
        "mode": status.get("mode"),
        "verdict": verdict,
        "grandfathered": False,
        "eligibility_receipt": eligibility_receipt,
    }


def _bot_path_from_token(token: str | Path) -> Path:
    raw = Path(token).expanduser()
    if raw.is_absolute() or len(raw.parts) > 1:
        return raw.resolve()
    version = parse_bot_version(str(token))
    if version is not None:
        return (_oc.ROOT / "bots" / bot_name(version)).resolve()
    return (_oc.ROOT / "bots" / str(token)).resolve()


def _same_bot_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except Exception:
        return str(a) == str(b)


def select_official_opponent(
    candidate: str | Path,
    active_bots: list[str] | tuple[str, ...] | None = None,
    *,
    preferred: str | Path | None = None,
    allow_bootstrap_grandfather: bool = False,
) -> dict[str, Any]:
    candidate_path = _oc._bot_path_from_token(candidate)
    try:
        candidate_artifact_hash = _oc.hash_path(candidate_path)
    except Exception:
        candidate_artifact_hash = ""
    target_version = parse_bot_version(candidate_path.name)
    try:
        from evolution_infra import get_active_bots, load_reaped_bot_versions

        reaped_versions = load_reaped_bot_versions()
        if active_bots is None:
            active_bots = get_active_bots()
        lifecycle_error = ""
    except Exception as exc:
        reaped_versions = None
        if active_bots is None:
            active_bots = []
        lifecycle_error = f"{type(exc).__name__}: {str(exc)[:200]}"
    raw_tokens: list[str | Path] = []
    if preferred:
        raw_tokens.append(preferred)
    raw_tokens.extend(active_bots or [])

    unique_paths: list[Path] = []
    unique_seen: set[str] = set()
    for token in raw_tokens:
        path = _oc._bot_path_from_token(token)
        if str(path) not in unique_seen and not _oc._same_bot_path(path, candidate_path):
            unique_seen.add(str(path))
            unique_paths.append(path)
    certified_alternative_artifacts: set[str] = set()
    for path in unique_paths:
        try:
            alternative_status = _oc.read_status(path)
            if _oc.official_full_certified(
                alternative_status,
                path,
                require_published=True,
            ):
                artifact_hash = str(
                    (alternative_status.get("certification_identity") or {}).get("candidate_hash")
                    or ""
                )
                if artifact_hash:
                    if artifact_hash == candidate_artifact_hash:
                        continue
                    certified_alternative_artifacts.add(artifact_hash)
        except Exception:
            continue
    certified_alternatives = len(certified_alternative_artifacts)

    considered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for token in raw_tokens:
        path = _oc._bot_path_from_token(token)
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        name = path.name
        if _oc._same_bot_path(path, candidate_path):
            considered.append({"bot": name, "path": str(path), "eligible": False, "reason": "candidate_self"})
            continue
        if not path.exists() or not (path / "national_bot.py").exists():
            considered.append({"bot": name, "path": str(path), "eligible": False, "reason": "missing_native_entry"})
            continue
        if not (path / ".completed").exists():
            considered.append({"bot": name, "path": str(path), "eligible": False, "reason": "missing_completed_sentinel"})
            continue
        if reaped_versions is None:
            considered.append({
                "bot": name,
                "path": str(path),
                "eligible": False,
                "reason": "lifecycle_ledger_unavailable",
                "error": lifecycle_error,
            })
            continue
        identity = _oc.published_bot_identity(path)
        if not identity.get("published"):
            considered.append({
                "bot": name,
                "path": str(path),
                "eligible": False,
                "reason": "not_published_artifact",
                "identity_issues": identity.get("issues") or [],
            })
            continue
        if (
            candidate_artifact_hash
            and identity.get("artifact_hash") == candidate_artifact_hash
        ):
            considered.append({
                "bot": name,
                "path": str(path),
                "artifact_hash": identity.get("artifact_hash"),
                "eligible": False,
                "reason": "candidate_artifact_clone",
            })
            continue
        version = parse_bot_version(name)
        if version is None or version in reaped_versions:
            considered.append({
                "bot": name,
                "path": str(path),
                "eligible": False,
                "reason": "reaped_or_invalid_version",
            })
            continue
        try:
            from national_native import check_native_contract
            from national_runtime_authority import (
                current_system_native_runtime_errors,
            )

            native_errors = [
                *current_system_native_runtime_errors(path),
                *check_native_contract(path),
            ]
        except Exception as exc:
            native_errors = [f"native_contract_check_error:{type(exc).__name__}:{str(exc)[:200]}"]
        if native_errors:
            considered.append({
                "bot": name,
                "path": str(path),
                "eligible": False,
                "reason": "native_contract_failed",
                "native_errors": native_errors[:5],
            })
            continue
        eligibility = _oc.official_opponent_eligibility(
            path,
            allow_bootstrap_grandfather=allow_bootstrap_grandfather,
            target_version=target_version,
            certified_alternatives=certified_alternatives,
        )
        if (
            eligibility.get("eligible")
            and eligibility.get("reason") != "official_certified"
        ):
            # Keep diagnostics about a stale/injected authorization, but never
            # expose it as an eligible formal opponent to downstream callers.
            eligibility = {
                **eligibility,
                "eligible": False,
                "reason": "official_full_certificate_required",
                "rejected_authorization_reason": eligibility.get("reason"),
                "bootstrap_requested_but_disabled": bool(
                    allow_bootstrap_grandfather
                ),
                "grandfathered": False,
            }
        item = {
            "bot": name,
            "path": str(path),
            "artifact_hash": identity.get("artifact_hash"),
            "tag": identity.get("tag"),
            "tag_object": identity.get("tag_object"),
            **eligibility,
        }
        considered.append(item)

    # Defense in depth: even a stale/injected helper result must not make a
    # content-bound grandfather receipt selectable for a formal EXE job.
    eligible = [
        item
        for item in considered
        if item.get("eligible") and item.get("reason") == "official_certified"
    ]
    if not eligible:
        return {
            "selected": False,
            "reason": "no_official_eligible_opponent",
            "candidate": str(candidate_path),
            "considered": considered,
            "readiness": {
                "certified_alternatives": certified_alternatives,
                "minimum_certified_alternatives": 2,
            },
        }
    selected = sorted(
        eligible,
        key=lambda item: (int(item.get("priority", 99)), -(parse_bot_version(item.get("bot")) or 0)),
    )[0]
    return {
        "selected": True,
        "candidate": str(candidate_path),
        "opponent": selected,
        "considered": considered,
        "readiness": {
            "certified_alternatives": certified_alternatives,
            "minimum_certified_alternatives": 2,
        },
    }


def resolve_managed_certification_spec(
    spec: _oc.CertificationSpec,
    *,
    exact_opponent_only: bool = False,
) -> tuple[_oc.CertificationSpec | None, dict[str, Any] | None]:
    """Revalidate/reselect the opponent immediately before formal EXE work.

    Durable jobs have already frozen an opponent path and its authorization
    receipt into their identity.  Their worker must revalidate that exact
    artifact without depending on an unrelated scan of the mutable active
    pool; all identity and receipt fields are compared again by the caller.
    """
    if spec.opponent_rounds <= 0:
        return spec, None
    if spec.bootstrap_control_id is not None:
        # Bootstrap is opt-in and never falls back to an active or archived bot.
        from official_bootstrap import (
            authorize_operator_bootstrap_selection,
            select_first_strict_control,
        )

        selection = select_first_strict_control(
            spec.bootstrap_control_id,
            spec.candidate,
        )
        if selection.get("selected"):
            authorization = authorize_operator_bootstrap_selection(
                selection,
                spec.bootstrap_control_id,
                spec.candidate,
            )
            if authorization.get("valid") is not True:
                return None, authorization
            selection = authorization["selection"]
    else:
        selection = _oc.select_official_opponent(
            spec.candidate,
            active_bots=(spec.opponent,) if exact_opponent_only else None,
            preferred=spec.opponent,
            allow_bootstrap_grandfather=False,
        )
    if not selection.get("selected"):
        return None, selection
    selected_path = str(Path(selection["opponent"]["path"]).resolve())
    if spec.opponent == selected_path:
        return spec, selection
    resolved = _oc.build_spec(
        spec.mode,
        spec.candidate,
        opponent=selected_path,
        self_play_rounds=spec.self_play_rounds,
        opponent_rounds=spec.opponent_rounds,
        target_hands=spec.target_hands,
        round_timeout_sec=spec.round_timeout_sec,
        no_progress_timeout_sec=spec.no_progress_timeout_sec,
        bootstrap_control_id=spec.bootstrap_control_id,
        quality_admission=spec.quality_admission,
    )
    return resolved, selection
