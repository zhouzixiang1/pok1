"""Receipt / report validation subsystem for official_certification.

Extracted as a cohesive business cluster; ``official_certification.py`` retains
thin delegate shells so external ``from official_certification import <name>``
and ``monkeypatch.setattr(official_certification, "<name>", ...)`` keep
resolving.

Business responsibility (single cohesive domain):
* Formal THP artifact / execution-issue extraction.
* Full-v5 completion and evidence-artifact issue extraction.
* Receipt validation (``receipt_validation_issues`` / ``receipt_valid_for_spec``).
* Report validation (``report_validation_issues`` / ``report_valid_for_spec``).
* Job-envelope report issue extraction.
* Deterministic status-receipt and deterministic-receipt issue extraction.
* Issue-marker test (``_issue_has_marker``).
* Official compliance verdict (``official_compliance_verdict``) and the
  ``official_failure_blocks_parent`` gate.

Pure validation logic: deterministic, side-effect-free receipt/report analysis.

Cross-references to symbols that remain in ``official_certification`` (the
status/schema/policy-id constants, the spec validator, the cache-key and
certification-identity helpers, the file-hash/json/safe-label helpers, the
deterministic status projection, and the official issue-string extractor) are
reached through ``_oc.<name>`` so that test monkeypatches on
``official_certification.<name>`` propagate.

CRITICAL (wave-3 lesson): EVERY intra-companion call to a moved symbol ALSO
routes through ``_oc.<name>(...)`` so monkeypatches on
``official_certification.<name>`` propagate even when both call sites now live
in this companion.
"""
from __future__ import annotations

from typing import Any

import official_certification as _oc  # for cross-refs


def _max_thp_hands(receipt: Any) -> int:
    if not isinstance(receipt, dict):
        return 0
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, dict):
        return 0
    summaries = artifacts.get("thp_summaries") or []
    if not isinstance(summaries, list):
        return 0
    values = []
    for item in summaries:
        if not isinstance(item, dict):
            values.append(0)
            continue
        try:
            values.append(int(item.get("hand_records", 0) or 0))
        except Exception:
            values.append(0)
    return max(values, default=0)



def _formal_thp_artifact_issues(
    receipt: dict[str, Any],
    *,
    expected_hands: int,
) -> list[str]:
    artifacts = receipt.get("artifacts") if isinstance(receipt.get("artifacts"), dict) else {}
    canonical = artifacts.get("canonical_thp")
    if not isinstance(canonical, dict):
        return ["canonical_thp_missing_for_full_certification"]
    issues: list[str] = []
    path_value = canonical.get("path")
    try:
        path = Path(str(path_value)) if path_value else None
        regular = bool(path) and path.is_file() and not path.is_symlink()
    except Exception:
        path = None
        regular = False
    if not regular or path is None:
        return ["canonical_thp_artifact_missing"]
    listed = artifacts.get("thp_files") or []
    if not isinstance(listed, list):
        listed = [listed]
    try:
        listed_paths = {
            Path(str(value)).expanduser().resolve()
            for value in listed
            if value
        }
        if path.expanduser().resolve() not in listed_paths:
            issues.append("canonical_thp_not_in_artifact_list")
    except Exception:
        issues.append("canonical_thp_artifact_list_invalid")
    try:
        raw = path.read_bytes()
        actual_sha256 = hashlib.sha256(raw).hexdigest()
        text = raw.decode("gb2312", errors="replace")
        actual_indices = [
            int(value)
            for value in re.findall(r"\bSTATE:(\d+):", text)
        ]
        actual_hands = len(actual_indices)
    except Exception as exc:
        return [f"canonical_thp_read_error:{type(exc).__name__}"]
    if canonical.get("sha256") != actual_sha256:
        issues.append("canonical_thp_sha256_mismatch")
    try:
        claimed_hands = int(canonical.get("hand_records", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        claimed_hands = 0
    if claimed_hands != actual_hands:
        issues.append(
            "canonical_thp_summary_count_mismatch: "
            f"claimed={claimed_hands} actual={actual_hands}"
        )
    if actual_hands != expected_hands:
        issues.append(
            "thp_hand_count_mismatch_for_full_certification: "
            f"hands={actual_hands} expected={expected_hands}"
        )
    if actual_indices != list(range(expected_hands)):
        issues.append(
            "thp_hand_index_sequence_mismatch_for_full_certification: "
            f"expected=0..{max(0, expected_hands - 1)}"
        )
    summaries = artifacts.get("thp_summaries") or []
    if not isinstance(summaries, list) or not summaries:
        issues.append("thp_summaries_missing_for_full_certification")
    else:
        digests = {
            str(item.get("sha256") or "")
            for item in summaries
            if isinstance(item, dict) and item.get("exists") is True and not item.get("issue")
        }
        if digests != {actual_sha256}:
            issues.append("thp_outputs_not_single_content_identity")
    return issues



def _formal_execution_issues(receipt: dict[str, Any]) -> list[str]:
    from managed_bot_executor import IsolationIdentity
    from official_execution_profile import (
        execution_profile_identity,
        load_execution_profile,
    )

    expected_profile = execution_profile_identity()
    execution = (
        receipt.get("formal_execution")
        if isinstance(receipt.get("formal_execution"), dict)
        else {}
    )
    issues: list[str] = []
    if execution.get("sandboxed") is not True:
        issues.append("official_formal_bot_sandbox_missing")
    for key, value in expected_profile.items():
        if execution.get(key) != value:
            issues.append(f"official_formal_execution_{key}_mismatch")
    bot_a = receipt.get("bot_a") if isinstance(receipt.get("bot_a"), dict) else {}
    bot_b = receipt.get("bot_b") if isinstance(receipt.get("bot_b"), dict) else {}
    expected_hashes: dict[str, str] = {}
    for label, launch in (("a", bot_a), ("b", bot_b)):
        launch_path = launch.get("path")
        if launch_path:
            try:
                expected_hash = hash_path(str(launch_path))
            except Exception:
                expected_hash = ""
        else:
            expected_hash = ""
        expected_hashes[label.upper()] = expected_hash
        if not expected_hash or execution.get(f"bot_{label}_artifact_hash") != expected_hash:
            issues.append(f"official_formal_bot_{label}_sealed_identity_mismatch")

    isolation_receipt = (
        execution.get("bot_isolation")
        if isinstance(execution.get("bot_isolation"), dict)
        else {}
    )
    if isolation_receipt.get("schema_version") != 1:
        issues.append("official_formal_bot_isolation_schema_mismatch")
    if isolation_receipt.get("authority") != (
        "central-managed-executor-process-observation"
    ):
        issues.append("official_formal_bot_isolation_authority_mismatch")
    connections = (
        isolation_receipt.get("connections")
        if isinstance(isolation_receipt.get("connections"), dict)
        else {}
    )
    if set(connections) != {"A", "B"}:
        issues.append("official_formal_bot_isolation_connections_mismatch")

    profile = load_execution_profile()
    managed_identity = (
        profile.get("managed_executor")
        if isinstance(profile.get("managed_executor"), dict)
        else {}
    )
    seccomp = (
        managed_identity.get("seccomp")
        if isinstance(managed_identity.get("seccomp"), dict)
        else {}
    )
    expected_isolation = asdict(IsolationIdentity(
        policy_sha256=str(seccomp.get("policy_sha256") or ""),
        bpf_sha256=str(seccomp.get("bpf_sha256") or ""),
        bpf_size=int(seccomp.get("bpf_size", 0) or 0),
    ))
    expected_source_sha256 = str(
        ((managed_identity.get("source") or {}).get("sha256") or "")
    )
    instance_ids: list[str] = []
    for connection, launch in (("A", bot_a), ("B", bot_b)):
        row = connections.get(connection)
        if not isinstance(row, dict):
            issues.append(f"official_formal_bot_isolation_{connection}_missing")
            continue
        expected_scalars = {
            "connection": connection,
            "name": str(launch.get("name") or ""),
            "role": str(launch.get("role") or ""),
            "instance_id": str(launch.get("instance_id") or ""),
            "seat": str(launch.get("seat") or ""),
            "artifact_hash": expected_hashes.get(connection, ""),
            "managed_executor_source_sha256": expected_source_sha256,
        }
        for key, value in expected_scalars.items():
            if not value or row.get(key) != value:
                issues.append(
                    f"official_formal_bot_isolation_{connection}_{key}_mismatch"
                )
        if not _oc._same_resolved_path(row.get("path"), str(launch.get("path") or "")):
            issues.append(f"official_formal_bot_isolation_{connection}_path_mismatch")
        if row.get("endpoint_lease") != {"consumed": True, "closed": True}:
            issues.append(
                f"official_formal_bot_isolation_{connection}_endpoint_lease_mismatch"
            )
        if row.get("execution_profile") != expected_profile:
            issues.append(
                f"official_formal_bot_isolation_{connection}_profile_mismatch"
            )
        isolation = row.get("isolation")
        if not isinstance(isolation, dict) or canonical_digest({
            "isolation": isolation,
        }) != canonical_digest({"isolation": expected_isolation}):
            issues.append(
                f"official_formal_bot_isolation_{connection}_policy_mismatch"
            )
        instance_ids.append(str(row.get("instance_id") or ""))
    if len(instance_ids) != 2 or len(set(instance_ids)) != 2:
        issues.append("official_formal_bot_isolation_instance_ids_not_unique")
    environment = receipt.get("environment") if isinstance(receipt.get("environment"), dict) else {}
    observed_profile = (
        environment.get("execution_profile")
        if isinstance(environment.get("execution_profile"), dict)
        else {}
    )
    if observed_profile.get("ok") is not True or observed_profile.get("issues"):
        issues.append("official_formal_execution_profile_not_verified")
    for key, value in expected_profile.items():
        if observed_profile.get(key) != value:
            issues.append(f"official_formal_observed_{key}_mismatch")
    return issues



def _full_v5_completion_issues(receipt: dict[str, Any]) -> list[str]:
    """Require the fixed EXE's natural 70/69 wire boundary for full-v5.

    Generic diagnostic acceptance may exercise another server that emits all
    70 settlement pairs.  That shape must never acquire normal full-v5
    certification authority: the pinned 2021 EXE proves hands 1..69 on the
    wire and hand 70 independently through the strict THP artifact/footer.
    """
    summary = receipt.get("log_summary") if isinstance(receipt, dict) else None
    wire = receipt.get("wire_replay_summary") if isinstance(receipt, dict) else None
    if not isinstance(summary, dict) or not isinstance(wire, dict):
        return ["official_full_v5_natural_terminal_boundary_missing"]
    try:
        shape = (
            int(summary.get("hands_started_min", 0) or 0),
            int(summary.get("settlements_min", 0) or 0),
            int(wire.get("hands_started_min", 0) or 0),
            int(wire.get("settlements_min", 0) or 0),
        )
    except (TypeError, ValueError, OverflowError):
        shape = (0, 0, 0, 0)
    if shape != (70, 69, 70, 69):
        return [
            "official_full_v5_natural_terminal_boundary_required: "
            f"log_hands={shape[0]} log_settlements={shape[1]} "
            f"wire_hands={shape[2]} wire_settlements={shape[3]} "
            "expected=70/69/70/69"
        ]
    if wire.get("issues"):
        return ["official_full_v5_wire_replay_issues_present"]
    completion = receipt.get("completion_evidence")
    if not isinstance(completion, dict) or completion.get("kind") != "official-thp-terminal-settlement":
        return ["official_full_v5_terminal_completion_evidence_required"]
    return round_completion_issues(receipt, 70, natural_terminal_only=True)



def _full_evidence_artifact_issues(receipt: dict[str, Any]) -> list[str]:
    probe = receipt.get("wire_probe")
    if not isinstance(probe, dict) or not bool(probe.get("enabled")):
        return ["full_wire_probe_missing_or_disabled"]
    if not (
        type(probe.get("causal_order_schema_version")) is int
        and probe.get("causal_order_schema_version") == 1
        and probe.get("finalized_replay_required") is True
    ):
        return ["full_wire_probe_causal_contract_missing_or_invalid"]
    artifacts = receipt.get("artifacts") if isinstance(receipt.get("artifacts"), dict) else {}
    issues: list[str] = []
    required_files = (
        "receipt",
        "platform_log",
        "bot_a_log",
        "bot_b_log",
        "bot_a_stdout",
        "bot_a_stderr",
        "bot_b_stdout",
        "bot_b_stderr",
        "wire_events",
        "replay_summary",
    )
    for key in required_files:
        value = artifacts.get(key)
        try:
            path = Path(str(value)) if value else None
            exists = bool(path) and path.is_file() and not path.is_symlink()
        except Exception:
            exists = False
        if not exists:
            issues.append(f"full_evidence_artifact_missing:{key}")
    for key in ("thp_files", "screenshots"):
        values = artifacts.get(key) or []
        if not isinstance(values, list):
            values = [values]
        try:
            retained = any(
                Path(str(value)).is_file() and not Path(str(value)).is_symlink()
                for value in values
                if value
            )
        except Exception:
            retained = False
        if not retained:
            issues.append(f"full_evidence_artifact_missing:{key}")
    return issues



def receipt_validation_issues(
    receipt: Any,
    spec: CertificationSpec,
    *,
    expected_kind: str | None = None,
    expected_index: int | None = None,
) -> list[str]:
    if not isinstance(receipt, dict):
        return [f"receipt_invalid_type:{type(receipt).__name__}"]
    issues: list[str] = []
    if receipt.get("passed") is not True:
        issues.append("receipt_not_passed")
    receipt_issues = receipt.get("issues") or []
    if isinstance(receipt_issues, list):
        issues.extend(str(issue) for issue in receipt_issues)
    elif receipt_issues:
        issues.append(f"receipt_issues_invalid_type:{type(receipt_issues).__name__}")
    try:
        receipt_target_hands = int(receipt.get("target_hands", 0) or 0)
    except Exception:
        receipt_target_hands = 0
    if receipt_target_hands != spec.target_hands:
        issues.append(f"target_hands_mismatch: receipt={receipt_target_hands} spec={spec.target_hands}")

    if expected_kind is not None and receipt.get("round_kind") != expected_kind:
        issues.append(
            f"round_kind_mismatch: receipt={receipt.get('round_kind')} expected={expected_kind}"
        )
    if expected_index is not None:
        try:
            actual_index = int(receipt.get("round_index", 0) or 0)
        except Exception:
            actual_index = 0
        if actual_index != expected_index:
            issues.append(
                f"round_index_mismatch: receipt={actual_index} expected={expected_index}"
            )

    bot_a = receipt.get("bot_a") if isinstance(receipt.get("bot_a"), dict) else {}
    bot_b = receipt.get("bot_b") if isinstance(receipt.get("bot_b"), dict) else {}
    if expected_kind == "self_play":
        if not _oc._same_resolved_path(bot_a.get("path"), spec.candidate):
            issues.append("self_play_bot_a_candidate_identity_mismatch")
        if not _oc._same_resolved_path(bot_b.get("path"), spec.candidate):
            issues.append("self_play_bot_b_candidate_identity_mismatch")
    elif expected_kind == "opponent":
        topology = round_topology(receipt)
        launches = {"A": bot_a, "B": bot_b}
        candidate_launches = [
            launches[label]
            for label, item in (topology.get("connections") or {}).items()
            if label in launches and item.get("role") == "candidate"
        ]
        opponent_launches = [
            launches[label]
            for label, item in (topology.get("connections") or {}).items()
            if label in launches and item.get("role") == "opponent"
        ]
        if len(candidate_launches) != 1 or not _oc._same_resolved_path(
            (candidate_launches[0] if candidate_launches else {}).get("path"),
            spec.candidate,
        ):
            issues.append("opponent_round_candidate_identity_mismatch")
        if len(opponent_launches) != 1 or not _oc._same_resolved_path(
            (opponent_launches[0] if opponent_launches else {}).get("path"),
            spec.opponent,
        ):
            issues.append("opponent_round_opponent_identity_mismatch")

    thp_hands = _oc._max_thp_hands(receipt)
    if spec.mode == "full" or spec.target_hands >= 70:
        completion_issues = (
            _oc._full_v5_completion_issues(receipt)
            if spec.policy_id == _oc.FULL_POLICY_ID
            else round_completion_issues(receipt, spec.target_hands)
        )
        if completion_issues:
            issues.extend(completion_issues)
            summary = receipt.get("log_summary") or {}
            issues.append(
                "official_full_settlement_incomplete: "
                f"hands_started={summary.get('hands_started_min', 0)} "
                f"settlements={summary.get('settlements_min', 0)} "
                f"target={spec.target_hands}"
            )
        issues.extend(_oc._full_evidence_artifact_issues(receipt))
        issues.extend(_oc._formal_execution_issues(receipt))
        formal_thp_issues = _oc._formal_thp_artifact_issues(
            receipt,
            expected_hands=spec.target_hands,
        )
        issues.extend(formal_thp_issues)
        if formal_thp_issues:
            issues.append(
                "thp_incomplete_for_full_certification: "
                f"exact_canonical_thp_required target={spec.target_hands}"
            )
            summary = receipt.get("log_summary") or {}
            try:
                hands_started = int(summary.get("hands_started_min", 0) or 0)
                settlements = int(summary.get("settlements_min", 0) or 0)
            except Exception:
                hands_started = 0
                settlements = 0
            if hands_started > 0 and hands_started < spec.target_hands:
                issues.append(
                    "official_full_round_incomplete_after_progress: "
                    f"hands_started={hands_started} settlements={settlements} "
                    f"target={spec.target_hands}"
                )
            elif hands_started == 0:
                issues.append(
                    "official_full_round_no_game_progress: "
                    f"target={spec.target_hands}"
                )
    elif thp_hands < spec.target_hands and not _oc._log_target_reached(receipt, spec.target_hands):
        # Short smoke stops the official EXE before its natural 70-hand THP export.
        # Use bot/platform logs as smoke evidence; full certification still requires THP.
        issues.append(f"smoke_progress_incomplete: thp_hands={thp_hands} target={spec.target_hands}")
    return issues



def receipt_valid_for_spec(receipt: dict[str, Any], spec: CertificationSpec) -> bool:
    return not _oc.receipt_validation_issues(receipt, spec)



def report_validation_issues(report: Any, spec: CertificationSpec) -> list[str]:
    try:
        _oc.validate_spec(spec)
    except Exception as exc:
        return [f"invalid_certification_spec:{type(exc).__name__}:{str(exc)[:300]}"]
    if not isinstance(report, dict):
        return [f"report_invalid_type:{type(report).__name__}"]
    issues: list[str] = []
    if report.get("passed") is not True:
        issues.append("report_not_passed")
    report_issues = report.get("issues") or []
    if isinstance(report_issues, list):
        issues.extend(str(issue) for issue in report_issues)
    elif report_issues:
        issues.append(f"report_issues_invalid_type:{type(report_issues).__name__}")
    report_payload = report.get("report")
    if not isinstance(report_payload, dict):
        issues.append(f"report_payload_invalid_type:{type(report_payload).__name__}")
        report_payload = {}
    rounds = report_payload.get("rounds") or []
    if not isinstance(rounds, list):
        issues.append(f"report_rounds_invalid_type:{type(rounds).__name__}")
        rounds = []
    expected = spec.self_play_rounds + spec.opponent_rounds
    if len(rounds) != expected:
        issues.append(f"round_count_mismatch: rounds={len(rounds)} expected={expected}")
    if spec.mode == "full":
        from official_execution_profile import execution_profile_identity

        suite_execution = (
            report_payload.get("formal_execution")
            if isinstance(report_payload.get("formal_execution"), dict)
            else {}
        )
        if suite_execution.get("ok") is not True or suite_execution.get("issues"):
            issues.append("official_formal_suite_execution_not_verified")
        for key, value in execution_profile_identity().items():
            if suite_execution.get(key) != value:
                issues.append(f"official_formal_suite_{key}_mismatch")
        expected_rounds = [
            *(("self_play", index) for index in range(1, spec.self_play_rounds + 1)),
            *(("opponent", index) for index in range(1, spec.opponent_rounds + 1)),
        ]
    else:
        expected_rounds = [(None, None)] * expected
    for index, receipt in enumerate(rounds, start=1):
        expected_kind, expected_index = (
            expected_rounds[index - 1]
            if index <= len(expected_rounds)
            else (None, None)
        )
        receipt_issues = _oc.receipt_validation_issues(
            receipt,
            spec,
            expected_kind=expected_kind,
            expected_index=expected_index,
        )
        issues.extend(f"round_{index}: {issue}" for issue in receipt_issues)
    return issues



def report_valid_for_spec(report: dict[str, Any], spec: CertificationSpec) -> bool:
    return not _oc.report_validation_issues(report, spec)



def _job_envelope_report_issues(
    report: dict[str, Any],
    job_envelope: dict[str, Any] | None,
) -> list[str]:
    payload = report.get("report") if isinstance(report, dict) else None
    if not isinstance(payload, dict):
        return ["official_job_envelope_report_missing"]
    issues: list[str] = []
    if payload.get("job_envelope") != job_envelope:
        issues.append("official_job_envelope_suite_mismatch")
    for index, receipt in enumerate(payload.get("rounds") or [], start=1):
        if not isinstance(receipt, dict) or receipt.get("job_envelope") != job_envelope:
            issues.append(f"official_job_envelope_round_mismatch:{index}")
    return issues



def _deterministic_status_receipt_issues(
    status: dict[str, Any],
    *,
    candidate: str | Path | None = None,
) -> list[str]:
    receipt = status.get("official_deterministic_status_receipt")
    if not isinstance(receipt, dict):
        return ["official_deterministic_status_receipt_missing"]
    issues: list[str] = []
    if receipt.get("schema_version") != _oc.DETERMINISTIC_STATUS_RECEIPT_SCHEMA_VERSION:
        issues.append("official_deterministic_status_receipt_schema_mismatch")
    digest = str(receipt.get("receipt_digest") or "")
    expected_digest = canonical_digest({
        key: value for key, value in receipt.items() if key != "receipt_digest"
    })
    if not digest or digest != expected_digest:
        issues.append("official_deterministic_status_receipt_digest_mismatch")
    expected_label = _oc._safe_label(candidate) if candidate is not None else str(status.get("bot") or "")
    if not expected_label or receipt.get("candidate_label") != expected_label:
        issues.append("official_deterministic_status_candidate_label_mismatch")
    if status.get("bot") and receipt.get("candidate_label") != status.get("bot"):
        issues.append("official_deterministic_status_bot_mismatch")
    identity = status.get("certification_identity") or {}
    candidate_hash = str(identity.get("candidate_hash") or "")
    if not candidate_hash or receipt.get("candidate_hash") != candidate_hash:
        issues.append("official_deterministic_status_candidate_hash_mismatch")
    if receipt.get("policy_id") != status.get("policy_id"):
        issues.append("official_deterministic_status_policy_mismatch")
    if receipt.get("mode") != status.get("mode"):
        issues.append("official_deterministic_status_mode_mismatch")
    if receipt.get("cache_key") != status.get("cache_key"):
        issues.append("official_deterministic_status_cache_key_mismatch")
    summary = status.get("official_evidence_summary")
    summary = summary if isinstance(summary, dict) else {}
    verdict = receipt.get("verdict")
    if not isinstance(verdict, dict):
        issues.append("official_deterministic_status_verdict_missing")
    else:
        for key in ("classification", "blocking", "inconclusive", "violation"):
            if verdict.get(key) != summary.get(key):
                issues.append(f"official_deterministic_status_{key}_mismatch")
        receipt_issues = [str(item) for item in (verdict.get("issues") or [])]
        summary_issues = summary.get("deterministic_issues")
        if not isinstance(summary_issues, list):
            issues.append("official_deterministic_status_issues_binding_missing")
        elif receipt_issues != [str(item) for item in summary_issues]:
            issues.append("official_deterministic_status_issues_mismatch")
        if summary.get("issue_count") != len(receipt_issues):
            issues.append("official_deterministic_status_issue_count_mismatch")
    evidence_path_value = status.get("official_evidence_path")
    evidence_payload = None
    try:
        evidence_path = Path(str(evidence_path_value or ""))
        if not evidence_path_value or evidence_path.is_symlink() or not evidence_path.is_file():
            issues.append("official_deterministic_status_evidence_missing")
        elif _oc._file_sha256(evidence_path) != receipt.get("evidence_sha256"):
            issues.append("official_deterministic_status_evidence_digest_mismatch")
        else:
            evidence_payload = _oc._read_json(evidence_path)
    except Exception as exc:
        issues.append(
            f"official_deterministic_status_evidence_error:{type(exc).__name__}:{str(exc)[:120]}"
        )
    if isinstance(verdict, dict):
        deterministic = (
            evidence_payload.get("deterministic")
            if isinstance(evidence_payload, dict)
            else None
        )
        if not isinstance(deterministic, dict):
            issues.append("official_deterministic_status_evidence_verdict_missing")
        elif verdict != _oc._deterministic_status_projection(deterministic):
            issues.append("official_deterministic_status_evidence_verdict_mismatch")
    archive = status.get("official_evidence_archive")
    archive = archive if isinstance(archive, dict) else {}
    if receipt.get("archive_sha256") != str(archive.get("archive_sha256") or ""):
        issues.append("official_deterministic_status_archive_mismatch")
    if receipt.get("archive_manifest_digest") != str(archive.get("manifest_digest") or ""):
        issues.append("official_deterministic_status_archive_manifest_mismatch")
    if receipt.get("mode") == "full" and isinstance(verdict, dict) and verdict.get("blocking"):
        archive_validation = validate_evidence_archive(
            archive,
            expected_evidence_sha256=str(receipt.get("evidence_sha256") or ""),
        )
        if not archive_validation.get("valid"):
            issues.extend(archive_validation.get("issues") or ["official_deterministic_status_archive_invalid"])
    if candidate is not None:
        try:
            if hash_path(Path(candidate).expanduser().resolve()) != candidate_hash:
                issues.append("official_deterministic_status_live_artifact_mismatch")
        except Exception as exc:
            issues.append(
                f"official_deterministic_status_artifact_error:{type(exc).__name__}:{str(exc)[:120]}"
            )
    if receipt.get("strength_evaluation") != "not_applicable":
        issues.append("official_deterministic_status_strength_scope_invalid")
    return list(dict.fromkeys(issues))



def _issue_has_marker(issue: str, markers: tuple[str, ...]) -> bool:
    lower = issue.lower()
    return any(marker in lower for marker in markers)



def official_compliance_verdict(status: dict[str, Any]) -> dict[str, Any]:
    """Classify official-platform evidence as a compliance oracle.

    The Windows EXE is used to catch explicit protocol/illegal-action violations.
    Harness problems such as Wine startup, occupied ports, missing THP export, or
    progress timeouts are evidence gaps, not bot-compliance failures.
    """
    status_value = str(status.get("status") or "")
    issues = _oc._official_issue_strings(status)
    evidence_summary = status.get("official_evidence_summary") if isinstance(status, dict) else {}
    evidence_summary = evidence_summary if isinstance(evidence_summary, dict) else {}
    evidence_class = str(evidence_summary.get("classification") or "")
    if bool(evidence_summary.get("inconclusive")) and status_value in {
        _oc.STATUS_CERTIFIED,
        _oc.STATUS_COMPLIANCE_PASS,
        _oc.STATUS_SMOKE_PASS,
    }:
        return {
            "ok": True,
            "blocking": False,
            "classification": evidence_class or "inconclusive",
            "inconclusive": True,
            "violation": False,
            "issues": issues,
            "inconclusive_issues": issues,
            "official_evidence_summary": evidence_summary,
        }
    if status_value == _oc.STATUS_UNCERTIFIED:
        return {
            "ok": True,
            "blocking": False,
            "classification": "uncertified",
            "inconclusive": True,
            "violation": False,
            "issues": issues,
            "inconclusive_issues": issues,
        }
    if status_value == _oc.STATUS_LOCAL_PASS:
        return {
            "ok": True,
            "blocking": False,
            "classification": "local_pass",
            "inconclusive": False,
            "violation": False,
            "issues": issues,
        }
    if status_value == _oc.STATUS_INCONCLUSIVE:
        return {
            "ok": True,
            "blocking": False,
            "classification": "inconclusive",
            "inconclusive": True,
            "violation": False,
            "issues": issues,
            "inconclusive_issues": issues,
        }
    if status_value != _oc.STATUS_FAILED:
        return {
            "ok": True,
            "blocking": False,
            "classification": "passed_or_pending",
            "inconclusive": False,
            "violation": False,
            "issues": issues,
        }

    identity = status.get("certification_identity")
    identity = identity if isinstance(identity, dict) else {}
    candidate_hash = str(identity.get("candidate_hash") or "")
    if candidate_hash:
        try:
            from official_verdict_ledger import latest_authoritative_verdict

            ledger = latest_authoritative_verdict(
                candidate_hash,
                fresh=True,
            )
            entry = ledger.get("entry") if ledger.get("valid") else None
            if (
                isinstance(entry, dict)
                and entry.get("outcome") == _oc.STATUS_FAILED
                and entry.get("blocking") is True
            ):
                classification = str(entry.get("classification") or "deterministic_blocking")
                return {
                    "ok": False,
                    "blocking": True,
                    "classification": classification,
                    "inconclusive": False,
                    "violation": classification == "protocol",
                    "issues": issues,
                    "violation_issues": issues,
                    "official_evidence_summary": evidence_summary,
                    "signed_verdict_ledger_valid": True,
                }
        except Exception:
            pass

    receipt_issues = _oc._deterministic_status_receipt_issues(status)
    receipt = status.get("official_deterministic_status_receipt") or {}
    receipt_verdict = receipt.get("verdict") if isinstance(receipt, dict) else {}
    receipt_verdict = receipt_verdict if isinstance(receipt_verdict, dict) else {}
    if not receipt_issues and bool(receipt_verdict.get("blocking")) and not bool(receipt_verdict.get("inconclusive")):
        verdict_class = str(receipt_verdict.get("classification") or "deterministic_blocking")
        if verdict_class == "protocol":
            verdict_class = "protocol_violation"
        elif verdict_class == "obvious_decision_error":
            verdict_class = "official_full_incomplete"
        return {
            "ok": False,
            "blocking": True,
            "classification": verdict_class,
            "inconclusive": False,
            "violation": bool(receipt_verdict.get("violation")),
            "issues": issues,
            "violation_issues": list(receipt_verdict.get("issues") or []),
            "official_evidence_summary": evidence_summary,
            "deterministic_receipt_valid": True,
        }
    inconclusive_issues = [
        issue for issue in issues
        if _oc._issue_has_marker(issue, _oc.COMPLIANCE_INCONCLUSIVE_FAILURE_MARKERS)
    ]
    return {
        "ok": True,
        "blocking": False,
        "classification": "inconclusive",
        "inconclusive": True,
        "violation": False,
        "issues": issues,
        "inconclusive_issues": inconclusive_issues or issues or receipt_issues,
        "deterministic_receipt_issues": receipt_issues,
    }



def official_failure_blocks_parent(status: dict[str, Any]) -> bool:
    return bool(_oc.official_compliance_verdict(status).get("blocking"))



def _deterministic_receipt_issues(
    receipt: Any,
    spec: CertificationSpec,
    *,
    evidence_manifest: dict[str, Any],
    archive_receipt: dict[str, Any],
) -> list[str]:
    if not isinstance(receipt, dict):
        return ["certificate_deterministic_receipt_missing"]
    issues: list[str] = []
    if receipt.get("schema_version") != _oc.DETERMINISTIC_RECEIPT_SCHEMA_VERSION:
        issues.append("certificate_deterministic_receipt_schema_mismatch")
    digest = str(receipt.get("receipt_digest") or "")
    expected_digest = canonical_digest({
        key: value for key, value in receipt.items() if key != "receipt_digest"
    })
    if digest != expected_digest:
        issues.append("certificate_deterministic_receipt_digest_mismatch")
    if receipt.get("policy_id") != spec.policy_id:
        issues.append("certificate_deterministic_receipt_policy_mismatch")
    if receipt.get("spec") != {
        "self_play_rounds": spec.self_play_rounds,
        "opponent_rounds": spec.opponent_rounds,
        "target_hands": spec.target_hands,
    }:
        issues.append("certificate_deterministic_receipt_spec_mismatch")
    verdict = receipt.get("verdict") if isinstance(receipt.get("verdict"), dict) else {}
    if verdict != {
        "passed": True,
        "classification": "pass",
        "blocking": False,
        "inconclusive": False,
        "candidate_verdict": "pass",
        "rounds_requested": spec.self_play_rounds + spec.opponent_rounds,
        "rounds_run": spec.self_play_rounds + spec.opponent_rounds,
        "target_hands": spec.target_hands,
        "issue_count": 0,
    }:
        issues.append("certificate_deterministic_verdict_not_full_pass")
    rounds = receipt.get("rounds") if isinstance(receipt.get("rounds"), list) else []
    expected_rounds = [
        ("self_play", index) for index in range(1, spec.self_play_rounds + 1)
    ] + [
        ("opponent", index) for index in range(1, spec.opponent_rounds + 1)
    ]
    actual_rounds: list[tuple[str, int]] = []
    for item in rounds:
        if not isinstance(item, dict):
            issues.append("certificate_deterministic_round_invalid")
            continue
        try:
            round_index = int(item.get("round_index"))
            target_hands = int(item.get("target_hands", 0) or 0)
            thp_hands = int(item.get("thp_hands", 0) or 0)
            hands_started = int(item.get("hands_started", 0) or 0)
            settlements = int(item.get("settlements", 0) or 0)
            completed_hands = int(item.get("completed_hands", 0) or 0)
            issue_count = int(item.get("issue_count", -1) or 0)
        except (TypeError, ValueError, OverflowError):
            issues.append("certificate_deterministic_round_identity_invalid")
            continue
        actual_rounds.append((str(item.get("round_kind")), round_index))
        paired_completion = (
            hands_started >= target_hands
            and settlements >= target_hands
            and item.get("completion_kind") == "paired-tcp-settlements"
        )
        thp_terminal_completion = (
            target_hands == 70
            and hands_started == 70
            and settlements == 69
            and item.get("completion_kind") == "official-thp-terminal-settlement"
            and len(str(item.get("completion_evidence_digest") or "")) == 64
        )
        if not (
            item.get("passed") is True
            and item.get("classification") == "pass"
            and item.get("candidate_verdict") == "pass"
            and item.get("candidate_blocking") is False
            and item.get("countable") is True
            and target_hands == spec.target_hands
            and thp_hands == spec.target_hands
            and completed_hands == spec.target_hands
            and len(str(item.get("thp_sha256") or "")) == 64
            and (
                thp_terminal_completion
                if spec.policy_id == _oc.FULL_POLICY_ID
                else (paired_completion or thp_terminal_completion)
            )
            and issue_count == 0
        ):
            issues.append(
                f"certificate_deterministic_round_not_passed:{item.get('round_kind')}:{item.get('round_index')}"
            )
    if actual_rounds != expected_rounds:
        issues.append("certificate_deterministic_round_set_mismatch")
    if receipt.get("evidence_sha256") != evidence_manifest.get("sha256"):
        issues.append("certificate_deterministic_evidence_digest_mismatch")
    if receipt.get("archive_sha256") != archive_receipt.get("archive_sha256"):
        issues.append("certificate_deterministic_archive_digest_mismatch")
    if receipt.get("archive_manifest_digest") != archive_receipt.get("manifest_digest"):
        issues.append("certificate_deterministic_archive_manifest_mismatch")
    if receipt.get("strength_evaluation") != "not_applicable":
        issues.append("certificate_deterministic_strength_scope_invalid")
    return issues



