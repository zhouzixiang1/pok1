"""Certification runner & job-lifecycle cluster for official_certification.

Extracted as a cohesive business cluster; ``official_certification.py`` retains
thin delegate shells so external ``from official_certification import <name>``
and ``monkeypatch.setattr(official_certification, "<name>", ...)`` keep
resolving.

Business responsibility (single cohesive domain):
* Official platform lock-busy gate (``official_lock_busy``).
* Evidence/LLM-analysis path helpers (``_evidence_path_for_result``,
  ``_official_llm_analysis_enabled``, ``_short_text``).
* Signed certificate record writing (``_write_certificate_record``).
* Official EXE compliance feedback summary (``official_feedback_summary``).
* Status assembly for a certification result (``_status_for_result``).
* Certification run implementation & public entry points
  (``_run_certification_impl``, ``run_certification``,
  ``run_identity_bound_certification_job``, ``_run_production_certification``,
  ``_run_certification_with_runner_for_test``).
* Public status payload (``status_payload``).

Cross-references to symbols that remain in ``official_certification`` (the
status/schema constants, spec validator, certification identity / cache helpers,
file-hash/json helpers, status read/write, the production-runner sentinels, and
the authority-cluster delegates) are reached through ``_oc.<name>`` so that
test monkeypatches on ``official_certification.<name>`` propagate.

CRITICAL (wave-3 lesson): EVERY intra-companion call to a moved symbol ALSO
routes through ``_oc.<name>(...)`` (the parent delegate shell) so monkeypatches
on ``official_certification.<name>`` propagate even when both call sites now
live in this companion.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from bot_artifact import canonical_digest
from official_evidence_archive import validate_evidence_archive
from official_platform_harness import OfficialPlatformConfig, _copy_config

import official_certification as _oc  # for cross-refs


def official_lock_busy(config: OfficialPlatformConfig | None = None) -> bool:
    cfg = config or OfficialPlatformConfig()
    return _oc.official_platform_busy(cfg.lock_path)


def _evidence_path_for_result(spec: _oc.CertificationSpec, summary: dict[str, Any], cache_key_value: str) -> Path:
    suite_dir = summary.get("suite_dir") if isinstance(summary, dict) else None
    if suite_dir:
        suite_path = Path(str(suite_dir))
        if suite_path.exists():
            return suite_path / "official_evidence.json"
    safe_key = cache_key_value[:12] if cache_key_value else "uncached"
    return _oc.certification_root() / "evidence" / spec.mode / f"{_oc._safe_label(spec.candidate)}-{safe_key}.json"


def _official_llm_analysis_enabled() -> bool:
    return os.environ.get("POK_OFFICIAL_LLM_ANALYSIS", "1").strip().lower() in {"1", "true", "yes", "on"}


def _short_text(value: Any, limit: int = 1200) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _write_certificate_record(
    spec: _oc.CertificationSpec,
    identity: dict[str, Any],
    evidence_extra: dict[str, Any],
    cache_key_value: str,
    opponent_selection: dict[str, Any] | None = None,
    job_envelope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from official_certificate_signing import (
        sign_certificate,
        signing_identity,
        verify_certificate_signature,
    )

    stable_selection = _oc.stable_official_opponent_selection(opponent_selection)
    selection_issues = _oc._opponent_selection_issues(stable_selection, spec, identity)
    if selection_issues:
        raise RuntimeError(
            "official opponent authorization receipt is invalid: "
            + ", ".join(selection_issues)
        )
    from official_job_envelope import job_envelope_issues

    envelope_issues = job_envelope_issues(
        job_envelope,
        expected_candidate_hash=str(identity.get("candidate_hash") or ""),
        expected_opponent_hash=str(identity.get("opponent_hash") or ""),
    )
    if envelope_issues:
        raise RuntimeError(
            "official durable job envelope is invalid: "
            + ", ".join(envelope_issues)
        )
    evidence_path = Path(str(evidence_extra.get("official_evidence_path") or ""))
    payload = {
        "schema_version": _oc.CERTIFICATE_SCHEMA_VERSION,
        "kind": "official-exe-compliance-certificate",
        "candidate_label": _oc._safe_label(spec.candidate),
        "issuer": signing_identity(),
        "issued_at": _oc.now_iso(),
        "policy_id": spec.policy_id,
        "mode": spec.mode,
        "spec": _oc.spec_record(spec),
        "identity": identity,
        "cache_key": cache_key_value,
        "opponent_selection": stable_selection,
        "job_envelope": job_envelope,
        "evidence_archive": evidence_extra.get("official_evidence_archive"),
        "evidence": {
            **_oc._certificate_file_manifest(evidence_path, label="official evidence"),
            "summary": evidence_extra.get("official_evidence_summary") or {},
        },
        "deterministic_receipt": evidence_extra.get("official_deterministic_receipt"),
        "strength_evaluation": "not_applicable",
    }
    digest = canonical_digest(payload)
    path = (
        _oc.certificate_dir()
        / str(identity.get("candidate_hash") or "missing")
        / f"{digest}.json"
    )
    record = {**payload, "certificate_digest": digest}
    _oc._write_json(path, record)
    signature = sign_certificate(record)
    signature_path = path.with_suffix(".sig")
    signature_path.write_text(signature, encoding="utf-8")
    signature_validation = verify_certificate_signature(record, signature)
    if not signature_validation.get("valid"):
        raise RuntimeError(
            "official certificate signature self-check failed: "
            + ", ".join(signature_validation.get("issues") or [])
        )
    return {
        **record,
        "certificate_path": str(path),
        "certificate_signature_path": str(signature_path),
        "certificate_signature_sha256": _oc._file_sha256(signature_path),
    }


def official_feedback_summary(*, limit: int = 8, max_chars: int = 6000) -> str:
    """Return bounded official-EXE compliance feedback for planning prompts.

    This is compliance-only context.  Win/loss and score outcomes from the
    official EXE are intentionally excluded so the Master cannot treat the
    platform as a strength evaluator.
    """
    rows: list[dict[str, Any]] = []
    try:
        files = sorted(_oc.status_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        files = []
    for path in files:
        payload = _oc._read_json(path) or {}
        if not payload:
            continue
        verdict = _oc.official_compliance_verdict(payload)
        llm_summary = payload.get("official_llm_analysis_summary") or {}
        repair_guidance = payload.get("official_llm_repair_guidance") or llm_summary.get("repair_guidance")
        prompt_feedback = payload.get("official_llm_prompt_feedback") or llm_summary.get("prompt_feedback")
        issues = payload.get("issues") or []
        has_signal = (
            verdict.get("blocking")
            or verdict.get("inconclusive")
            or repair_guidance
            or prompt_feedback
            or payload.get("status") in {_oc.STATUS_FAILED, _oc.STATUS_INCONCLUSIVE}
        )
        if not has_signal:
            continue
        rows.append({
            "bot": payload.get("bot") or path.stem,
            "status": payload.get("status"),
            "mode": payload.get("mode"),
            "classification": verdict.get("classification"),
            "blocking": bool(verdict.get("blocking")),
            "inconclusive": bool(verdict.get("inconclusive")),
            "issues": issues[:5],
            "evidence_path": payload.get("official_evidence_path"),
            "repair_guidance": _oc._short_text(repair_guidance, 900),
            "prompt_feedback": _oc._short_text(prompt_feedback, 900),
        })
        if len(rows) >= limit:
            break
    if not rows:
        return "No official EXE compliance feedback recorded yet."

    lines = [
        "Official EXE feedback is compliance-only; do not use EXE wins/losses as strength evidence.",
    ]
    for row in rows:
        lines.append(
            f"- {row['bot']}: status={row['status']} mode={row['mode']} "
            f"classification={row['classification']} blocking={row['blocking']} "
            f"inconclusive={row['inconclusive']}"
        )
        if row["issues"]:
            lines.append("  issues: " + "; ".join(str(item)[:180] for item in row["issues"]))
        if row["repair_guidance"]:
            lines.append("  repair_guidance: " + row["repair_guidance"])
        if row["prompt_feedback"]:
            lines.append("  prompt_feedback: " + row["prompt_feedback"])
        if row["evidence_path"]:
            lines.append(f"  evidence: {row['evidence_path']}")
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[: max_chars - 3].rstrip() + "..."
    return text


def _status_for_result(
    spec: _oc.CertificationSpec,
    result: dict[str, Any],
    *,
    cache_hit: bool,
    cache_key_value: str,
    identity: dict[str, Any],
    request_started_ns: int,
    identity_issues: list[str] | None = None,
    opponent_selection: dict[str, Any] | None = None,
    job_envelope: dict[str, Any] | None = None,
    test_only: bool = False,
) -> dict[str, Any]:
    validation_issues = _oc.report_validation_issues(result, spec)
    valid = not validation_issues
    raw_result_issues = result.get("issues") or []
    if not isinstance(raw_result_issues, list):
        raw_result_issues = [raw_result_issues]
    issues = list(dict.fromkeys([
        str(issue)
        for issue in raw_result_issues + validation_issues + list(identity_issues or [])
    ]))
    if valid:
        if spec.mode == "full":
            status = _oc.STATUS_CERTIFIED
        elif spec.mode == "compliance":
            status = _oc.STATUS_COMPLIANCE_PASS
        else:
            status = _oc.STATUS_SMOKE_PASS
    elif _oc._issues_have_protocol_violation(issues):
        status = _oc.STATUS_FAILED
    else:
        status = _oc.STATUS_INCONCLUSIVE
    if identity_issues and status != _oc.STATUS_FAILED:
        status = _oc.STATUS_INCONCLUSIVE
    report = result.get("report", {}) if isinstance(result, dict) else {}
    summary = report.get("summary", {}) if isinstance(report, dict) else {}
    evidence_extra: dict[str, Any] = {}
    evidence: dict[str, Any] | None = None
    try:
        evidence_path = _oc._evidence_path_for_result(spec, summary, cache_key_value)
        evidence_result = dict(result)
        evidence_result["issues"] = issues
        evidence = _oc.build_official_evidence_bundle(evidence_result, output_path=evidence_path)
        deterministic = evidence.get("deterministic", {})
        evidence_issues = [
            str(issue)
            for issue in (deterministic.get("issues") or [])
            if str(issue)
        ]
        if evidence_issues:
            issues = list(dict.fromkeys([*issues, *evidence_issues]))
        if deterministic.get("blocking"):
            status = _oc.STATUS_FAILED
        elif deterministic.get("inconclusive"):
            status = _oc.STATUS_INCONCLUSIVE
        evidence_extra = {
            "official_evidence_path": str(evidence_path),
            "official_evidence_summary": {
                "schema_version": evidence.get("schema_version"),
                "classification": deterministic.get("classification"),
                "blocking": deterministic.get("blocking"),
                "inconclusive": deterministic.get("inconclusive"),
                "violation": deterministic.get("violation"),
                "issue_count": len(deterministic.get("issues") or []),
                "deterministic_issues": [
                    str(item) for item in (deterministic.get("issues") or [])
                ],
                "rounds_requested": deterministic.get("rounds_requested"),
                "rounds_run": deterministic.get("rounds_run"),
                "target_hands": deterministic.get("target_hands"),
                "strength_evaluation": "not_applicable",
            },
        }
        if spec.mode == "full":
            archive = _oc.build_evidence_archive(summary.get("suite_dir"))
            archive_validation = validate_evidence_archive(
                archive,
                expected_evidence_sha256=_oc._file_sha256(evidence_path),
            )
            if not archive_validation.get("valid"):
                issues = list(dict.fromkeys([
                    *issues,
                    *(archive_validation.get("issues") or ["official_evidence_archive_invalid"]),
                ]))
                status = _oc.STATUS_INCONCLUSIVE
            else:
                evidence_extra["official_evidence_archive"] = archive
                if status == _oc.STATUS_CERTIFIED:
                    deterministic_receipt = _oc._build_deterministic_receipt(
                        spec,
                        evidence,
                        evidence_path,
                        archive,
                    )
                    receipt_issues = _oc._deterministic_receipt_issues(
                        deterministic_receipt,
                        spec,
                        evidence_manifest={"sha256": _oc._file_sha256(evidence_path)},
                        archive_receipt=archive,
                    )
                    if receipt_issues:
                        issues = list(dict.fromkeys([*issues, *receipt_issues]))
                        status = _oc.STATUS_INCONCLUSIVE
                    else:
                        evidence_extra["official_deterministic_receipt"] = deterministic_receipt
        evidence_extra["official_deterministic_status_receipt"] = (
            _oc._build_deterministic_status_receipt(
                spec,
                identity,
                evidence_path,
                deterministic,
                cache_key_value,
                evidence_extra.get("official_evidence_archive"),
            )
        )
    except Exception as exc:
        issue = f"official_evidence_error: {type(exc).__name__}: {str(exc)[:300]}"
        issues = list(dict.fromkeys([*issues, issue]))
        status = _oc.STATUS_INCONCLUSIVE
        # Preserve a successfully-written evidence bundle when a later archive
        # or certificate step fails. This keeps the run diagnosable and avoids
        # dereferencing a removed evidence path in advisory analysis.
        evidence_extra = {
            **evidence_extra,
            "official_evidence_error": issue,
        }
    certificate_extra: dict[str, Any] = {}
    if status == _oc.STATUS_CERTIFIED and spec.mode == "full":
        try:
            record = _oc._write_certificate_record(
                spec,
                identity,
                evidence_extra,
                cache_key_value,
                opponent_selection,
                job_envelope,
            )
            certificate_extra = {
                "certificate_schema_version": record.get("schema_version"),
                "certificate_digest": record.get("certificate_digest"),
                "certificate_path": record.get("certificate_path"),
                "certificate_signature_path": record.get("certificate_signature_path"),
                "certificate_signature_sha256": record.get("certificate_signature_sha256"),
            }
        except Exception as exc:
            issues = list(dict.fromkeys([
                *issues,
                f"official_certificate_artifact_error:{type(exc).__name__}:{str(exc)[:240]}",
            ]))
            status = _oc.STATUS_INCONCLUSIVE
    if evidence is not None and evidence_extra.get("official_evidence_path"):
        evidence_sha256 = _oc._file_sha256(Path(evidence_extra["official_evidence_path"]))
        analysis_identity = canonical_digest({
            "evidence_sha256": evidence_sha256,
            "analysis_sha256": (
                _oc._file_sha256(_oc.LLM_ANALYSIS_PATH) if _oc.LLM_ANALYSIS_PATH.exists() else "missing"
            ),
            "prompt_sha256": (
                _oc._file_sha256(_oc.LLM_ANALYSIS_PROMPT_PATH)
                if _oc.LLM_ANALYSIS_PROMPT_PATH.exists()
                else "missing"
            ),
        })
        analysis_path = (
            _oc.certification_root()
            / "analysis"
            / _oc._safe_label(spec.candidate)
            / f"{analysis_identity}.json"
        )
        analysis_path.parent.mkdir(parents=True, exist_ok=True)
        analysis_issue = ""
        try:
            if _oc._official_llm_analysis_enabled():
                from official_llm_analysis import (
                    advisory_analysis_contract_issues,
                    run_official_llm_analysis_sync,
                )

                analysis = run_official_llm_analysis_sync(evidence, output_path=analysis_path)
                analysis_issues = advisory_analysis_contract_issues(analysis)
                if analysis_issues:
                    raise ValueError(";".join(analysis_issues))
                analysis.setdefault("analysis_source", "llm")
            else:
                from official_llm_analysis import safe_default_analysis

                analysis = safe_default_analysis(evidence, reason="llm_disabled")
                analysis["analysis_path"] = str(analysis_path)
                _oc._write_json(analysis_path, analysis)
        except Exception as exc:
            analysis_issue = f"{type(exc).__name__}: {str(exc)[:300]}"
            try:
                from official_llm_analysis import safe_default_analysis

                analysis = safe_default_analysis(
                    evidence,
                    reason=f"llm_unavailable:{type(exc).__name__}",
                )
            except Exception:
                analysis = {
                    "analysis_source": "unavailable",
                    "repair_guidance": "",
                    "prompt_feedback": "",
                    "confidence": 0.0,
                    "strength_evaluation": "not_applicable",
                }
            analysis["analysis_path"] = str(analysis_path)
            _oc._write_json(analysis_path, analysis)
        evidence_extra["official_llm_analysis_path"] = str(analysis_path)
        evidence_extra["official_llm_analysis_issue"] = analysis_issue
        evidence_extra["official_llm_analysis_summary"] = {
            "analysis_source": analysis.get("analysis_source"),
            "analysis_status": analysis.get("analysis_status"),
            "hypothesis_class": analysis.get("hypothesis_class"),
            "authority": analysis.get("authority"),
            "confidence": analysis.get("confidence"),
            "repair_guidance": _oc._short_text(analysis.get("repair_guidance"), 1200),
            "prompt_feedback": _oc._short_text(analysis.get("prompt_feedback"), 1200),
            "strength_evaluation": "not_applicable",
            "authoritative": False,
        }
        evidence_extra["official_llm_repair_guidance"] = _oc._short_text(
            analysis.get("repair_guidance"), 2000
        )
        evidence_extra["official_llm_prompt_feedback"] = _oc._short_text(
            analysis.get("prompt_feedback"), 2000
        )
    written = _oc.write_status(
        spec.candidate,
        status,
        mode=spec.mode,
        policy_id=spec.policy_id,
        cache_hit=cache_hit,
        cache_key=cache_key_value,
        certification_identity=identity,
        test_only=bool(test_only),
        authority_scope="test-only" if test_only else "production",
        summary=summary,
        issues=issues,
        result=result,
        opponent_selection=opponent_selection,
        official_job_envelope=job_envelope,
        request_started_ns=request_started_ns,
        request_completed_ns=time.time_ns(),
        **evidence_extra,
        **certificate_extra,
    )
    if (
        spec.mode == "full"
        and not test_only
        and written.get("request_started_ns") == request_started_ns
    ):
        try:
            from official_verdict_ledger import append_verdict

            ledger_entry = append_verdict(written)
            written = {
                **written,
                "official_verdict_ledger_entry": ledger_entry,
            }
        except Exception as exc:
            ledger_issue = (
                "official_verdict_ledger_error: "
                f"{type(exc).__name__}: {str(exc)[:240]}"
            )
            written = {
                **written,
                "status": _oc.STATUS_INCONCLUSIVE,
                "status_label": _oc.STATUS_INCONCLUSIVE,
                "issues": list(dict.fromkeys([
                    *(written.get("issues") or []),
                    ledger_issue,
                ])),
                "official_verdict_ledger_error": ledger_issue,
            }
        with _oc._status_lock(_oc._safe_label(spec.candidate)):
            current = _oc._read_json(_oc._status_path(_oc._safe_label(spec.candidate))) or {}
            if current.get("request_started_ns") == request_started_ns:
                _oc._write_json(_oc._status_path(_oc._safe_label(spec.candidate)), written)
    return written


def _run_certification_impl(
    spec: _oc.CertificationSpec,
    *,
    config: OfficialPlatformConfig | None = None,
    force: bool = False,
    runner: _oc.Runner,
    runner_provenance: str,
    enforce_opponent_selection: bool,
    request_started_ns: int | None = None,
    opponent_selection: dict[str, Any] | None = None,
    suite_dir: str | Path | None = None,
    job_envelope: dict[str, Any] | None = None,
    test_only: bool = False,
    _production_authority: object | None = None,
) -> dict[str, Any]:
    if (
        not isinstance(request_started_ns, int)
        or isinstance(request_started_ns, bool)
        or request_started_ns <= 0
    ):
        request_started_ns = time.time_ns()
    _oc.validate_spec(spec)
    if test_only:
        if "PYTEST_CURRENT_TEST" not in os.environ:
            raise RuntimeError("test-only certification runner is available only under pytest")
        if runner_provenance != _oc.TEST_ONLY_RUNNER_PROVENANCE:
            raise RuntimeError("test-only certification must use test-only runner provenance")
    elif runner_provenance != _oc.PRODUCTION_RUNNER_PROVENANCE:
        raise RuntimeError("production certification requires official-exe runner provenance")
    if spec.mode == "full":
        if not test_only and (
            runner is not _oc._PRODUCTION_CERTIFICATION_RUNNER
            or _production_authority is not _oc._PRODUCTION_FULL_AUTHORITY
        ):
            raise RuntimeError(
                "formal full certification requires the bound production official-EXE runner"
            )
        from official_job_envelope import job_envelope_issues

        envelope_issues = job_envelope_issues(job_envelope)
        if envelope_issues:
            raise RuntimeError(
                "formal full certification requires a valid durable job envelope: "
                + ", ".join(envelope_issues)
            )
        if not test_only:
            ledger_issues = _oc._official_verdict_ledger_issues()
            if ledger_issues:
                raise RuntimeError(
                    "official_verdict_ledger_preflight_failed: "
                    + "; ".join(ledger_issues)
                    + "; explicitly initialize genesis with "
                    "python3 scripts/official_certify.py init-ledger"
                )
    if enforce_opponent_selection:
        resolved_spec, opponent_selection = _oc.resolve_managed_certification_spec(spec)
        if resolved_spec is None:
            return {
                "bot": _oc._safe_label(spec.candidate),
                "status": "opponent-selection-blocked",
                "status_label": "opponent-selection-blocked",
                "mode": spec.mode,
                "updated_at": _oc.now_iso(),
                "issues": ["no_official_eligible_opponent"],
                "blocking": False,
                "inconclusive": True,
                "opponent_selection": opponent_selection,
            }
        spec = resolved_spec
    cfg = config or OfficialPlatformConfig()
    cfg = _copy_config(
        cfg,
        round_timeout_sec=spec.round_timeout_sec,
        no_progress_timeout_sec=spec.no_progress_timeout_sec,
        results_dir=_oc.certification_root() / spec.mode,
    )
    identity_before = _oc.certification_identity(
        spec,
        cfg,
        runner_provenance=runner_provenance,
        test_only=test_only,
    )
    key = (
        canonical_digest({
            "certification_identity": identity_before,
            "job_envelope": job_envelope,
        })
        if spec.mode == "full"
        else str(identity_before["identity_digest"])
    )
    if not force and spec.mode != "full":
        cached = _oc._cache_hit(
            spec,
            cfg,
            runner_provenance=runner_provenance,
            test_only=test_only,
        )
        if cached:
            return _oc._status_for_result(
                spec,
                cached["result"],
                cache_hit=True,
                cache_key_value=key,
                identity=identity_before,
                request_started_ns=request_started_ns,
                opponent_selection=opponent_selection,
                job_envelope=job_envelope,
                test_only=test_only,
            )
    if spec.mode == "full":
        from official_certificate_signing import signing_environment_report

        signing_report = signing_environment_report()
        if not signing_report.get("ok"):
            raise RuntimeError(
                "official_certificate_signing_preflight_failed: "
                + "; ".join(signing_report.get("issues") or ["unknown signing error"])
            )

    runner_kwargs = {
        "opponent": spec.opponent,
        "self_play_rounds": spec.self_play_rounds,
        "opponent_rounds": spec.opponent_rounds,
        "target_hands": spec.target_hands,
        "config": cfg,
    }
    if suite_dir is not None:
        runner_kwargs["suite_dir"] = Path(suite_dir).expanduser().resolve()
    if job_envelope is not None:
        runner_kwargs["job_envelope"] = job_envelope
    result_obj = runner(spec.candidate, **runner_kwargs)
    result = result_obj.model_dump() if hasattr(result_obj, "model_dump") else dict(result_obj)
    identity_after = _oc.certification_identity(
        spec,
        cfg,
        runner_provenance=runner_provenance,
        test_only=test_only,
    )
    identity_issues: list[str] = []
    if identity_after.get("candidate_hash") != identity_before.get("candidate_hash"):
        identity_issues.append("candidate_changed_during_official_certification")
    if identity_after.get("opponent_hash") != identity_before.get("opponent_hash"):
        identity_issues.append("opponent_changed_during_official_certification")
    if identity_after.get("platform_fingerprint") != identity_before.get("platform_fingerprint"):
        identity_issues.append("official_platform_policy_changed_during_certification")
    if spec.mode == "full":
        identity_issues.extend(_oc._job_envelope_report_issues(result, job_envelope))
    if spec.mode != "full" and _oc.report_valid_for_spec(result, spec) and not identity_issues:
        key = _oc._write_cache(spec, result, identity_before)
    return _oc._status_for_result(
        spec,
        result,
        cache_hit=False,
        cache_key_value=key,
        identity=identity_before,
        request_started_ns=request_started_ns,
        identity_issues=identity_issues,
        opponent_selection=opponent_selection,
        job_envelope=job_envelope,
        test_only=test_only,
    )


def run_certification(
    spec: _oc.CertificationSpec,
    *,
    config: OfficialPlatformConfig | None = None,
    force: bool = False,
    suite_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Run the production certification path with mandatory opponent governance."""
    if spec.mode == "full":
        raise RuntimeError(
            "formal full certification must run through official_certification_job"
        )
    return _oc._run_production_certification(
        spec,
        config=config,
        force=force,
        suite_dir=suite_dir,
    )


def run_identity_bound_certification_job(
    spec: _oc.CertificationSpec,
    *,
    expected_identity: dict[str, Any],
    expected_opponent_selection: dict[str, Any] | None,
    suite_dir: str | Path,
    job_envelope: dict[str, Any],
    force: bool = False,
) -> dict[str, Any]:
    """Run a durable job without allowing its evidence identity to drift.

    Opponent governance is revalidated immediately before EXE work. If live
    policy would select a different artifact, the old job must fail and a new
    identity-bound job must be created; evidence is never attached to the old
    job id under a silently changed opponent.
    """
    _oc.validate_spec(spec)
    current_identity = _oc.certification_identity(spec)
    if current_identity != expected_identity:
        raise RuntimeError("official_job_runtime_identity_changed")
    from official_job_envelope import job_envelope_issues

    envelope_issues = job_envelope_issues(
        job_envelope,
        expected_candidate_hash=str(expected_identity.get("candidate_hash") or ""),
        expected_opponent_hash=str(expected_identity.get("opponent_hash") or ""),
    )
    if envelope_issues:
        raise RuntimeError(
            "official_job_envelope_invalid: " + ", ".join(envelope_issues)
        )
    if _oc.normal_full_quality_admission_required(spec):
        if job_envelope.get("quality_admission") != spec.quality_admission:
            raise RuntimeError(
                "official_job_quality_admission_spec_envelope_mismatch"
            )
    resolved_spec, live_selection = _oc.resolve_managed_certification_spec(
        spec,
        exact_opponent_only=True,
    )
    if resolved_spec is None:
        failure = {
            "reason": (live_selection or {}).get("reason") or "selection_unavailable",
            "considered": (live_selection or {}).get("considered") or [],
        }
        raise RuntimeError(
            "official_job_opponent_no_longer_eligible: "
            + json.dumps(failure, ensure_ascii=True, sort_keys=True)[:2000]
        )
    if _oc.certification_identity(resolved_spec) != expected_identity:
        raise RuntimeError("official_job_opponent_selection_changed")
    expected_selection = _oc.stable_official_opponent_selection(expected_opponent_selection)
    current_selection = _oc.stable_official_opponent_selection(live_selection)
    expected_opponent = (expected_selection or {}).get("opponent") or {}
    live_opponent = (current_selection or {}).get("opponent") or {}
    if expected_opponent:
        expected_path = str(Path(str(expected_opponent.get("path") or "")).expanduser().resolve())
        live_path = str(Path(str(live_opponent.get("path") or "")).expanduser().resolve())
        if expected_path != live_path:
            raise RuntimeError("official_job_opponent_receipt_path_changed")
        expected_hash = str(expected_opponent.get("artifact_hash") or "")
        live_hash = str(live_opponent.get("artifact_hash") or "")
        if expected_hash and expected_hash != live_hash:
            raise RuntimeError("official_job_opponent_receipt_hash_changed")
        if expected_opponent.get("eligibility_receipt") != live_opponent.get("eligibility_receipt"):
            raise RuntimeError("official_job_opponent_eligibility_receipt_changed")
    if expected_selection != current_selection:
        raise RuntimeError("official_job_opponent_receipt_changed")
    return _oc._run_certification_impl(
        spec,
        force=force,
        runner=_oc._PRODUCTION_CERTIFICATION_RUNNER,
        runner_provenance=_oc.PRODUCTION_RUNNER_PROVENANCE,
        enforce_opponent_selection=False,
        opponent_selection=live_selection,
        suite_dir=suite_dir,
        job_envelope=job_envelope,
        _production_authority=_oc._PRODUCTION_FULL_AUTHORITY,
    )


def _run_production_certification(
    spec: _oc.CertificationSpec,
    *,
    config: OfficialPlatformConfig | None = None,
    force: bool = False,
    request_started_ns: int | None = None,
    suite_dir: str | Path | None = None,
) -> dict[str, Any]:
    runner = (
        _oc._PRODUCTION_CERTIFICATION_RUNNER
        if spec.mode == "full"
        else _oc.run_official_acceptance_sync
    )
    return _oc._run_certification_impl(
        spec,
        config=config,
        force=force,
        runner=runner,
        runner_provenance=_oc.PRODUCTION_RUNNER_PROVENANCE,
        enforce_opponent_selection=True,
        request_started_ns=request_started_ns,
        suite_dir=suite_dir,
        _production_authority=_oc._PRODUCTION_FULL_AUTHORITY,
    )


def _run_certification_with_runner_for_test(
    spec: _oc.CertificationSpec,
    *,
    runner: _oc.Runner,
    config: OfficialPlatformConfig | None = None,
    force: bool = False,
    request_started_ns: int | None = None,
    opponent_selection: dict[str, Any] | None = None,
    job_envelope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Inject a fake harness in unit tests without weakening the public API."""
    if "PYTEST_CURRENT_TEST" not in os.environ:
        raise RuntimeError("test certification runner is available only under pytest")
    if spec.mode == "full" and job_envelope is None:
        from official_job_envelope import build_job_envelope

        identity = _oc.certification_identity(
            spec,
            config,
            runner_provenance=_oc.TEST_ONLY_RUNNER_PROVENANCE,
            test_only=True,
        )
        request = {
            "job_id": "1" * 64,
            "request_digest": "2" * 64,
            "manager_sha256": "3" * 64,
            "spec": _oc.spec_record(spec),
            "identity": identity,
            "opponent_selection": _oc.stable_official_opponent_selection(opponent_selection),
            "source_v": None,
        }
        job_envelope = build_job_envelope(
            request,
            attempt=1,
            attempt_nonce="4" * 64,
            suite_dir=_oc.certification_root() / "pytest-suite",
        )
    def bound_test_runner(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raw = runner(*args, **kwargs)
        payload = raw.model_dump() if hasattr(raw, "model_dump") else dict(raw)
        if spec.mode == "full":
            report = payload.get("report") if isinstance(payload.get("report"), dict) else {}
            report["job_envelope"] = job_envelope
            for receipt in report.get("rounds") or []:
                if isinstance(receipt, dict):
                    receipt["job_envelope"] = job_envelope
            payload["report"] = report
        return payload

    return _oc._run_certification_impl(
        spec,
        config=config,
        force=force,
        runner=bound_test_runner,
        runner_provenance=_oc.TEST_ONLY_RUNNER_PROVENANCE,
        enforce_opponent_selection=False,
        request_started_ns=request_started_ns,
        opponent_selection=opponent_selection,
        job_envelope=job_envelope,
        test_only=True,
    )


def status_payload(candidate: str | Path) -> dict[str, Any]:
    payload = _oc.read_status(candidate)
    payload["compliance_verdict"] = _oc.official_compliance_verdict(payload)
    payload["certification_root"] = str(_oc.certification_root())
    return payload

