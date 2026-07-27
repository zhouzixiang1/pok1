"""Workflow-v65 contract-failure diagnosis subsystem for bootstrap_contract_recovery_diagnosis.

Extracted as a cohesive business cluster; ``bootstrap_contract_recovery_diagnosis.py``
retains thin delegate shells so external ``from bootstrap_contract_recovery_diagnosis import <name>``
and ``monkeypatch.setattr(bootstrap_contract_recovery_diagnosis, "<name>", ...)``
keep resolving.

Business responsibility (single cohesive domain): the workflow-v65
four-live-race / two-THP-prefix contract-41 incident proof:
* ``_expected_v65_incident_identity``.
* ``_validate_v65_failure_diagnosis_envelope``.
* ``_v65_contract_failure_diagnosis`` (the proof builder).

Cross-references to symbols that remain in ``bootstrap_contract_recovery_diagnosis``
(the ``_V65_*`` / ``_CALLED_ALLIN_*`` constants, the ``_regular_json`` /
``_require_regular_directory`` / ``_sha256_bytes`` / ``_strict_artifact_bytes`` /
``_require_exact_round_job_envelope`` / ``_called_allin_authority_absence``
helpers, the ``canonical_digest`` / ``bot_name`` / ``BootstrapContractRecoveryError``
imports, and ``Path`` / ``PurePosixPath`` / ``Any`` / ``FIRST_STRICT_POLICY_VERSION``
re-exports) are reached through ``_bcd.<name>`` so that test monkeypatches on
``bootstrap_contract_recovery_diagnosis.<name>`` propagate.

CRITICAL (wave-3 lesson): EVERY intra-companion call to a moved symbol ALSO
routes through ``_bcd.<name>(...)`` so monkeypatches on
``bootstrap_contract_recovery_diagnosis.<name>`` propagate even when both
call sites now live in this companion.
"""
from __future__ import annotations

import json
import re

import bootstrap_contract_recovery_diagnosis as _bcd  # for cross-refs


def _expected_v65_incident_identity() -> dict[str, _bcd.Any]:
    return {
        "baseline_head": _bcd._V65_BASELINE_HEAD,
        "baseline_contract_version": _bcd._V65_BASELINE_CONTRACT_VERSION,
        "baseline_contract_hash": _bcd._V65_BASELINE_CONTRACT_HASH,
        "repair_contract_version": _bcd._V65_REPAIR_CONTRACT_VERSION,
        "workflow_run_id": _bcd._V65_WORKFLOW_RUN_ID,
        "checkpoint_revision": _bcd._V65_CHECKPOINT_REVISION,
        "candidate_artifact_hash": _bcd._V65_CANDIDATE_HASH,
        "job_id": _bcd._V65_JOB_ID,
        "job_result_digest": _bcd._V65_JOB_RESULT_DIGEST,
        "rounds_requested": 8,
        "rounds_completed": 8,
        "rounds_run": 8,
        "passed_rounds": 2,
        "failed_rounds": 6,
    }


def _validate_v65_failure_diagnosis_envelope(
    value: _bcd.Any,
) -> dict[str, _bcd.Any]:
    """Validate only the exact workflow-v65 Contract-41 incident proof."""

    if not isinstance(value, dict) or set(value) != _bcd._V65_DIAGNOSIS_FIELDS:
        raise _bcd.BootstrapContractRecoveryError([
            "bootstrap_contract_v65_diagnosis_fields_invalid"
        ])
    payload = {
        key: item for key, item in value.items() if key != "proof_digest"
    }
    incident = value.get("incident_identity")
    rounds = value.get("round_receipts")
    live_failures = value.get("live_deferred_failures")
    thp_failures = value.get("thp_prefix_failures")
    digest_fields = (
        "baseline_wire_probe_sha256",
        "repair_wire_probe_sha256",
        "baseline_harness_sha256",
        "repair_harness_sha256",
        "baseline_oracle_document_sha256",
        "repair_oracle_document_sha256",
        "baseline_oracle_fixture_sha256",
        "repair_oracle_fixture_sha256",
        "evidence_sha256",
        "evidence_archive_sha256",
        "evidence_archive_manifest_digest",
        "suite_summary_sha256",
        "attribution_digest",
    )
    invalid = bool(
        value.get("schema_version") != 1
        or value.get("kind") != _bcd._V65_DIAGNOSIS_KIND
        or value.get("profile_id") != _bcd._V65_PROFILE_ID
        or tuple(value.get("defect_ids") or ()) != _bcd._V65_DEFECT_IDS
        or not isinstance(incident, dict)
        or set(incident) != _bcd._V65_INCIDENT_IDENTITY_FIELDS
        or incident != _bcd._expected_v65_incident_identity()
        or value.get("proof_digest") != _bcd.canonical_digest(payload)
        or value.get("strength_evaluation") != "not_applicable"
        or value.get("disposition")
        != "abandon_and_reprepare_only_without_evidence_reuse"
        or value.get("authority_absence") != _bcd._CALLED_ALLIN_AUTHORITY_ABSENCE
        or any(
            not _bcd._HEX64.fullmatch(str(value.get(field) or ""))
            for field in digest_fields
        )
        or value.get("baseline_wire_probe_sha256")
        != _bcd._V65_BASELINE_WIRE_PROBE_SHA256
        or value.get("baseline_harness_sha256")
        != _bcd._V65_BASELINE_HARNESS_SHA256
        or value.get("baseline_oracle_document_sha256")
        != _bcd._V65_BASELINE_ORACLE_DOC_SHA256
        or value.get("baseline_oracle_fixture_sha256")
        != _bcd._V65_BASELINE_ORACLE_FIXTURE_SHA256
        or value.get("repair_oracle_document_sha256")
        != _bcd._V65_REPAIR_ORACLE_DOC_SHA256
        or value.get("repair_oracle_fixture_sha256")
        != _bcd._V65_REPAIR_ORACLE_FIXTURE_SHA256
        or value.get("repair_wire_probe_sha256")
        == value.get("baseline_wire_probe_sha256")
        or value.get("repair_harness_sha256")
        == value.get("baseline_harness_sha256")
        or value.get("repair_oracle_document_sha256")
        == value.get("baseline_oracle_document_sha256")
        or value.get("repair_oracle_fixture_sha256")
        == value.get("baseline_oracle_fixture_sha256")
        or not isinstance(rounds, list)
        or len(rounds) != len(_bcd._V65_ROUND_IDENTITIES)
        or any(
            not isinstance(item, dict)
            or set(item) != _bcd._V65_ROUND_RECEIPT_FIELDS
            for item in (rounds or [])
        )
        or not isinstance(live_failures, list)
        or len(live_failures) != len(_bcd._V65_LIVE_RACE_FAILURES)
        or any(
            not isinstance(item, dict)
            or set(item) != _bcd._V65_LIVE_FAILURE_FIELDS
            for item in (live_failures or [])
        )
        or not isinstance(thp_failures, list)
        or len(thp_failures) != len(_bcd._V65_THP_PREFIX_FAILURES)
        or any(
            not isinstance(item, dict)
            or set(item) != _bcd._V65_THP_FAILURE_FIELDS
            for item in (thp_failures or [])
        )
    )
    if not invalid:
        for observed, expected in zip(rounds, _bcd._V65_ROUND_IDENTITIES):
            if any(observed.get(key) != expected[key] for key in expected):
                invalid = True
                break
    if not invalid:
        round_ids = {item["slot"]: item["round_id"] for item in rounds}
        for observed, expected in zip(
            live_failures,
            _bcd._V65_LIVE_RACE_FAILURES,
        ):
            if (
                observed.get("round_id") != round_ids[expected["slot"]]
                or any(
                    observed.get(key) != expected[key]
                    for key in expected
                )
                or any(
                    not _bcd._HEX64.fullmatch(str(observed.get(field) or ""))
                    for field in (
                        "stored_summary_digest",
                        "finalized_summary_digest",
                        "provisional_summary_digest",
                    )
                )
            ):
                invalid = True
                break
    if not invalid:
        round_ids = {item["slot"]: item["round_id"] for item in rounds}
        for observed, expected in zip(
            thp_failures,
            _bcd._V65_THP_PREFIX_FAILURES,
        ):
            if (
                observed.get("round_id") != round_ids[expected["slot"]]
                or any(
                    observed.get(key) != expected[key]
                    for key in expected
                )
                or any(
                    not _bcd._HEX64.fullmatch(str(observed.get(field) or ""))
                    for field in (
                        "wire_omissions_digest",
                        "strict_match_digest",
                        "prefix_binding_digest",
                    )
                )
            ):
                invalid = True
                break
    if invalid:
        raise _bcd.BootstrapContractRecoveryError([
            "bootstrap_contract_v65_diagnosis_invalid"
        ])
    return value



def _v65_contract_failure_diagnosis(
    root: _bcd.Path,
    directory: _bcd.Path,
    *,
    request: dict[str, _bcd.Any],
    state: dict[str, _bcd.Any],
    status: dict[str, _bcd.Any],
    candidate_hash: str,
    workflow_run_id: str,
    checkpoint_revision: int,
    job_result_digest: str,
    expected_evaluation_contract_version: int,
    expected_evaluation_contract_hash: str,
    expected_repair_contract_version: int,
    expected_baseline_head: str,
    expected_repair_head: str,
    control_consumption: dict[str, _bcd.Any],
    require_live_repair_source: bool = True,
) -> dict[str, _bcd.Any]:
    """Reopen only the v65 four-live-race/two-THP-prefix incident."""
    # Lazy import: read the *current* attribute on the main module so that
    # monkeypatch.setattr(recovery, "_git", ...) and "_read_regular_exact" in
    # tests remain effective. Do NOT hoist these to top-level imports.
    from bootstrap_contract_recovery import _git as _git  # noqa: E402
    from bootstrap_contract_recovery import _read_regular_exact as _read_regular_exact  # noqa: E402

    from official_evidence_archive import validate_evidence_archive
    from official_platform_harness import (
        THP_RECORD_RE,
        _omitted_allin_thp_bindings,
        _parse_thp_card_payload,
        _strict_thp_match,
        _wire_settlement_prefix,
    )
    from official_wire_probe import replay_events

    incident_identity = {
        "baseline_head": expected_baseline_head,
        "baseline_contract_version": expected_evaluation_contract_version,
        "baseline_contract_hash": expected_evaluation_contract_hash,
        "repair_contract_version": expected_repair_contract_version,
        "workflow_run_id": workflow_run_id,
        "checkpoint_revision": checkpoint_revision,
        "candidate_artifact_hash": candidate_hash,
        "job_id": directory.name,
        "job_result_digest": job_result_digest,
        "rounds_requested": 8,
        "rounds_completed": 8,
        "rounds_run": 8,
        "passed_rounds": 2,
        "failed_rounds": 6,
    }
    if incident_identity != _bcd._expected_v65_incident_identity():
        raise ValueError("v65 incident identity is not exact")
    if (
        state.get("attempt") != 1
        or state.get("revision") != 948
        or state.get("result_digest") != _bcd._V65_JOB_RESULT_DIGEST
        or state.get("worker_restart_count") != 0
    ):
        raise ValueError("v65 job attempt/result identity changed")

    identity = request.get("identity")
    identity = identity if isinstance(identity, dict) else {}
    platform = identity.get("platform")
    platform = platform if isinstance(platform, dict) else {}
    if (
        request.get("job_id") != _bcd._V65_JOB_ID
        or identity.get("candidate_hash") != _bcd._V65_CANDIDATE_HASH
        or identity.get("opponent_hash") != _bcd._V65_CONTROL_HASH
        or platform.get("exe_sha256") != _bcd._CALLED_ALLIN_EXE_SHA256
    ):
        raise ValueError("v65 request identity changed")

    source_specs = (
        (
            "wire_probe",
            "web/core/official_wire_probe.py",
            _bcd._V65_BASELINE_WIRE_PROBE_SHA256,
            None,
        ),
        (
            "harness",
            "web/core/official_platform_harness.py",
            _bcd._V65_BASELINE_HARNESS_SHA256,
            None,
        ),
        (
            "oracle_document",
            _bcd._CALLED_ALLIN_ORACLE_DOC,
            _bcd._V65_BASELINE_ORACLE_DOC_SHA256,
            _bcd._V65_REPAIR_ORACLE_DOC_SHA256,
        ),
        (
            "oracle_fixture",
            _bcd._CALLED_ALLIN_ORACLE_FIXTURE,
            _bcd._V65_BASELINE_ORACLE_FIXTURE_SHA256,
            _bcd._V65_REPAIR_ORACLE_FIXTURE_SHA256,
        ),
    )
    source_identities: dict[str, str] = {}
    for (
        label,
        relative,
        expected_baseline_sha256,
        expected_repair_sha256,
    ) in source_specs:
        baseline_raw = _git(
            root,
            "show",
            f"{expected_baseline_head}:{relative}",
            binary=True,
        )
        repair_raw = _git(
            root,
            "show",
            f"{expected_repair_head}:{relative}",
            binary=True,
        )
        if not isinstance(baseline_raw, bytes) or not isinstance(
            repair_raw, bytes
        ):
            raise ValueError(f"v65 {label} source is unavailable")
        baseline_sha256 = _bcd._sha256_bytes(baseline_raw)
        repair_sha256 = _bcd._sha256_bytes(repair_raw)
        if (
            baseline_sha256 != expected_baseline_sha256
            or repair_sha256 == baseline_sha256
            or (
                expected_repair_sha256 is not None
                and repair_sha256 != expected_repair_sha256
            )
        ):
            raise ValueError(f"v65 {label} contract change is unproven")
        if require_live_repair_source:
            max_bytes = 4 * 1024 * 1024
            live_raw = _read_regular_exact(
                root / relative,
                max_bytes=max_bytes,
            )
            if live_raw != repair_raw:
                raise ValueError(f"live {label} is not the reviewed repair")
        source_identities[f"baseline_{label}_sha256"] = baseline_sha256
        source_identities[f"repair_{label}_sha256"] = repair_sha256

    candidate = root / "bots" / _bcd.bot_name(_bcd.FIRST_STRICT_POLICY_VERSION)
    authority_absence = _bcd._called_allin_authority_absence(
        root,
        candidate=candidate,
        control_consumption=control_consumption,
        require_live=require_live_repair_source,
    )

    suite = directory / "suite_attempt_01"
    _bcd._require_regular_directory(suite)
    status_summary = status.get("summary")
    status_summary = status_summary if isinstance(status_summary, dict) else {}
    if _bcd.Path(str(status_summary.get("suite_dir") or "")) != suite:
        raise ValueError("v65 suite path is not job-owned")
    evidence_path = suite / "official_evidence.json"
    if _bcd.Path(str(status.get("official_evidence_path") or "")) != evidence_path:
        raise ValueError("v65 evidence path is not canonical")
    summary_raw, suite_report = _bcd._regular_json(
        suite / "summary.json",
        max_bytes=4 * 1024 * 1024,
    )
    evidence_raw, evidence = _bcd._regular_json(
        evidence_path,
        max_bytes=4 * 1024 * 1024,
    )
    evidence_sha256 = _bcd._sha256_bytes(evidence_raw)
    deterministic = status.get("official_deterministic_status_receipt")
    deterministic = deterministic if isinstance(deterministic, dict) else {}
    archive = status.get("official_evidence_archive")
    archive = archive if isinstance(archive, dict) else {}
    archive_validation = validate_evidence_archive(
        archive,
        expected_evidence_sha256=evidence_sha256,
    )
    if (
        deterministic.get("evidence_sha256") != evidence_sha256
        or archive.get("evidence_sha256") != evidence_sha256
        or archive_validation.get("valid") is not True
        or evidence.get("schema_version") != 1
        or evidence.get("purpose") != "official_platform_compliance"
        or evidence.get("strength_evaluation") != "not_applicable"
    ):
        raise ValueError("v65 evidence/archive identity changed")

    expected_summary = {
        "self_play_rounds": 5,
        "opponent_rounds": 3,
        "target_hands": 70,
        "rounds_requested": 8,
        "rounds_run": 8,
        "passed_rounds": 2,
        "failed_rounds": 6,
        "resumed_rounds": 0,
        "official_platform": True,
    }
    report_summary = suite_report.get("summary")
    report_summary = report_summary if isinstance(report_summary, dict) else {}
    evidence_summary = evidence.get("summary")
    evidence_summary = evidence_summary if isinstance(evidence_summary, dict) else {}
    if any(
        status_summary.get(key) != expected
        or report_summary.get(key) != expected
        or evidence_summary.get(key) != expected
        for key, expected in expected_summary.items()
    ):
        raise ValueError("v65 suite is not exact 2-pass/6-fail")
    attribution = status_summary.get("attribution")
    attribution = attribution if isinstance(attribution, dict) else {}
    attribution_rounds = attribution.get("rounds")
    if (
        status.get("status") != "official-failed"
        or report_summary.get("attribution") != attribution
        or evidence_summary.get("attribution") != attribution
        or report_summary.get("formal_execution")
        != status_summary.get("formal_execution")
        or evidence_summary.get("formal_execution")
        != status_summary.get("formal_execution")
        or not isinstance(status_summary.get("formal_execution"), dict)
        or status_summary["formal_execution"].get("ok") is not True
        or status_summary["formal_execution"].get("issues") != []
        or evidence_summary.get("passed") is not False
        or evidence_summary.get("raw_passed") is not False
        or attribution.get("schema_version") != 1
        or attribution.get("policy_id") != "official-attribution-v1"
        or attribution.get("candidate_verdict") != "fail"
        or attribution.get("candidate_blocking") is not True
        or attribution.get("inconclusive") is not False
        or attribution.get("countable_rounds") != 2
        or not isinstance(attribution_rounds, list)
        or len(attribution_rounds) != 8
    ):
        raise ValueError("v65 suite/attribution crossbinding changed")

    report_rounds = suite_report.get("rounds")
    evidence_rounds = evidence.get("rounds")
    if (
        not isinstance(report_rounds, list)
        or len(report_rounds) != 8
        or not isinstance(evidence_rounds, list)
        or len(evidence_rounds) != 8
    ):
        raise ValueError("v65 suite round set is incomplete")

    expected_by_slot = {
        item["slot"]: item for item in _bcd._V65_ROUND_IDENTITIES
    }
    race_by_slot = {
        item["slot"]: item for item in _bcd._V65_LIVE_RACE_FAILURES
    }
    thp_by_slot = {
        item["slot"]: item for item in _bcd._V65_THP_PREFIX_FAILURES
    }
    round_receipts: list[dict[str, _bcd.Any]] = []
    live_failures: list[dict[str, _bcd.Any]] = []
    thp_failures: list[dict[str, _bcd.Any]] = []

    for offset, slot in enumerate(_bcd._V65_EXPECTED_SLOTS):
        expected = expected_by_slot[slot]
        receipt = report_rounds[offset]
        evidence_round = evidence_rounds[offset]
        attribution_round = attribution_rounds[offset]
        if not all(
            isinstance(item, dict)
            for item in (receipt, evidence_round, attribution_round)
        ):
            raise ValueError("v65 round evidence shape is invalid")
        kind = "self_play" if slot.startswith("self_play") else "opponent"
        index = int(slot.rsplit("_", 1)[1])
        if (
            receipt.get("round_id") != expected["round_id"]
            or receipt.get("round_kind") != kind
            or receipt.get("round_index") != index
            or receipt.get("target_hands") != 70
            or receipt.get("passed") is not expected["passed"]
            or evidence_round.get("round_id") != expected["round_id"]
            or evidence_round.get("round_kind") != kind
            or evidence_round.get("round_index") != index
            or evidence_round.get("passed") is not expected["passed"]
            or attribution_round.get("countable") is not expected["passed"]
        ):
            raise ValueError("v65 round outcome identity changed")
        _bcd._require_exact_round_job_envelope(
            receipt.get("job_envelope"),
            status.get("official_job_envelope"),
            job_id=directory.name,
            candidate_hash=candidate_hash,
        )
        wire_probe = receipt.get("wire_probe")
        wire_probe = wire_probe if isinstance(wire_probe, dict) else {}
        if wire_probe.get("enabled") is not True or wire_probe.get("issues") != []:
            raise ValueError("v65 wire probe failed independently")

        artifacts = evidence_round.get("artifacts")
        artifacts = artifacts if isinstance(artifacts, dict) else {}
        receipt_item = artifacts.get("receipt")
        archive_path = str((receipt_item or {}).get("archive_path") or "")
        pure_receipt = _bcd.PurePosixPath(archive_path)
        if (
            len(pure_receipt.parts) != 4
            or pure_receipt.parts[0] != slot
            or pure_receipt.parts[1] != "executions"
            or re.fullmatch(
                r"run_[0-9]+_[0-9]+",
                pure_receipt.parts[2],
            ) is None
            or pure_receipt.parts[3] != "receipt.json"
        ):
            raise ValueError("v65 round execution path is invalid")
        execution_prefix = "/".join(pure_receipt.parts[:-1])
        receipt_raw = _bcd._strict_artifact_bytes(
            suite,
            receipt_item,
            expected_archive_path=f"{execution_prefix}/receipt.json",
            max_bytes=2 * 1024 * 1024,
        )
        wire_raw = _bcd._strict_artifact_bytes(
            suite,
            artifacts.get("wire_events"),
            expected_archive_path=f"{execution_prefix}/wire_events.jsonl",
            max_bytes=2 * 1024 * 1024,
        )
        replay_raw = _bcd._strict_artifact_bytes(
            suite,
            artifacts.get("replay_summary"),
            expected_archive_path=f"{execution_prefix}/replay_summary.json",
            max_bytes=2 * 1024 * 1024,
        )
        if (
            json.loads(receipt_raw.decode("utf-8")) != receipt
            or _bcd._sha256_bytes(receipt_raw) != expected["receipt_sha256"]
            or _bcd._sha256_bytes(wire_raw) != expected["wire_events_sha256"]
            or _bcd._sha256_bytes(replay_raw)
            != expected["replay_summary_sha256"]
        ):
            raise ValueError("v65 exact round bytes changed")
        slot_dir = suite / slot
        executions = slot_dir / "executions"
        execution_dir = executions / pure_receipt.parts[2]
        for owned_directory in (slot_dir, executions, execution_dir):
            _bcd._require_regular_directory(owned_directory)
        if (
            sorted(item.name for item in slot_dir.iterdir())
            != ["executions", "receipt.json"]
            or sorted(item.name for item in executions.iterdir())
            != [pure_receipt.parts[2]]
            or _read_regular_exact(
                slot_dir / "receipt.json",
                max_bytes=2 * 1024 * 1024,
            ) != receipt_raw
        ):
            raise ValueError("v65 round was resumed or duplicated")

        stored_replay = receipt.get("wire_replay_summary")
        if (
            not isinstance(stored_replay, dict)
            or evidence_round.get("wire_replay_summary") != stored_replay
            or json.loads(replay_raw.decode("utf-8")) != stored_replay
        ):
            raise ValueError("v65 stored replay is not cross-bound")
        events = [
            json.loads(line)
            for line in wire_raw.decode("utf-8").splitlines()
            if line.strip()
        ]
        if (
            any(not isinstance(event, dict) for event in events)
            or len(events) != expected["event_count"]
            or stored_replay.get("events_seen") != len(events)
            or stored_replay.get("hands_started_min")
            != expected["hands_started"]
            or stored_replay.get("settlements_min")
            != expected["settlements"]
        ):
            raise ValueError("v65 raw event vector changed")
        finalized = replay_events(events, finalized=True)
        if finalized != stored_replay or finalized.get("issues") != []:
            raise ValueError("v65 finalized causal replay changed")
        round_receipts.append({
            key: expected[key]
            for key in _bcd._V65_ROUND_RECEIPT_FIELDS
        })

        if expected["passed"]:
            if (
                receipt.get("issues") != []
                or stored_replay.get("warnings") != []
                or not isinstance(receipt.get("completion_evidence"), dict)
            ):
                raise ValueError("v65 passing round is not intact")
            continue

        race = race_by_slot.get(slot)
        if race is not None:
            expected_wire_issue = (
                "wire_street_boundary_unproved: "
                f"conn={race['conn']} hand={race['hand']} "
                f"stage={race['stage']} msg={race['boundary_message']!r} "
                "reason=next public street requires an exact completed prior "
                "street or a previously proved called-all-in runout"
            )
            if receipt.get("issues") != [
                expected_wire_issue,
                "thp_missing_for_full_70_hand_round",
                "official_terminal_socket_boundary_invalid",
            ]:
                raise ValueError("v65 live-race receipt has another failure")
            source = events[race["source_record_seq"] - 1]
            boundary = events[race["boundary_record_seq"] - 1]
            flush = events[race["flush_record_seq"] - 1]
            if (
                source.get("record_seq") != race["source_record_seq"]
                or source.get("observation_seq")
                != race["source_observation_seq"]
                or source.get("conn") != race["conn"]
                or source.get("direction") != "bot_to_server"
                or source.get("raw_repr") != race["action"]
                or source.get("remaining") != race["action"]
                or source.get("messages") != []
                or boundary.get("record_seq")
                != race["boundary_record_seq"]
                or boundary.get("observation_seq")
                != race["boundary_observation_seq"]
                or boundary.get("conn") != race["conn"]
                or boundary.get("direction") != "server_to_bot"
                or boundary.get("messages") != [race["boundary_message"]]
                or flush.get("record_seq") != race["flush_record_seq"]
                or flush.get("observation_seq")
                != race["flush_observation_seq"]
                or flush.get("conn") != race["conn"]
                or flush.get("event_type") != "idle_flush"
                or flush.get("messages") != [race["action"]]
                or not (
                    race["source_record_seq"]
                    < race["boundary_record_seq"]
                    < race["flush_record_seq"]
                )
            ):
                raise ValueError("v65 live-race causal envelope changed")
            provisional = replay_events(
                events[: race["boundary_record_seq"]],
                now=float(boundary["observation_t"]),
                finalized=False,
            )
            warnings = provisional.get("warnings")
            warnings = warnings if isinstance(warnings, list) else []
            matching_warnings = [
                item for item in warnings
                if isinstance(item, dict)
                and item.get("kind")
                == "provisional_street_boundary_unproved"
                and item.get("strict_issue_kind")
                == "street_boundary_unproved"
                and item.get("conn") == race["conn"]
                and item.get("hand") == race["hand"]
                and item.get("stage") == race["stage"]
            ]
            if provisional.get("issues") != [] or len(matching_warnings) != 1:
                raise ValueError("v65 live-race repair is not exact")
            live_failures.append({
                **race,
                "round_id": expected["round_id"],
                "stored_summary_digest": _bcd.canonical_digest(stored_replay),
                "finalized_summary_digest": _bcd.canonical_digest(finalized),
                "provisional_summary_digest": _bcd.canonical_digest(provisional),
            })
            continue

        thp_expected = thp_by_slot.get(slot)
        if thp_expected is None:
            raise ValueError("v65 unsupported failed-round slot")
        expected_timeout = (
            "terminal_thp_timeout: waited=20s detail="
            "omitted_allin_runout_thp_board_incomplete:"
            f"{thp_expected['hand']}"
        )
        if receipt.get("issues") != [
            expected_timeout,
            "official_terminal_completion_evidence_missing",
        ] or receipt.get("completion_evidence") is not None:
            raise ValueError("v65 THP-prefix receipt has another failure")
        thp_items = artifacts.get("thp_files")
        if not isinstance(thp_items, list) or len(thp_items) != 1:
            raise ValueError("v65 THP-prefix artifact set changed")
        thp_item = thp_items[0]
        thp_archive_path = str((thp_item or {}).get("archive_path") or "")
        thp_pure = _bcd.PurePosixPath(thp_archive_path)
        if (
            len(thp_pure.parts) != 5
            or "/".join(thp_pure.parts[:3]) != execution_prefix
            or thp_pure.parts[3] != "thp"
        ):
            raise ValueError("v65 THP-prefix archive path changed")
        thp_raw = _bcd._strict_artifact_bytes(
            suite,
            thp_item,
            expected_archive_path=thp_archive_path,
            max_bytes=512 * 1024,
        )
        if (
            _bcd._sha256_bytes(thp_raw) != thp_expected["thp_sha256"]
            or len(thp_raw) != thp_expected["thp_bytes"]
        ):
            raise ValueError("v65 THP-prefix bytes changed")
        thp_text = thp_raw.decode("gb2312", errors="replace")
        expected_names = (
            str((receipt.get("bot_a") or {}).get("name") or ""),
            str((receipt.get("bot_b") or {}).get("name") or ""),
        )
        strict_match, strict_issues = _strict_thp_match(
            thp_text,
            expected_hands=70,
            expected_names=expected_names,
        )
        if strict_match is None or strict_issues:
            raise ValueError("v65 THP-prefix strict match changed")
        matches = [
            match for match in THP_RECORD_RE.finditer(thp_text)
            if int(match.group(1)) == thp_expected["thp_record_index"]
        ]
        if (
            len(matches) != 1
            or matches[0].group(3) != thp_expected["thp_cards_payload"]
        ):
            raise ValueError("v65 THP-prefix state identity changed")
        parsed_cards, card_issue = _parse_thp_card_payload(
            matches[0].group(3)
        )
        if (
            card_issue
            or parsed_cards is None
            or len(parsed_cards["public_cards"])
            != thp_expected["public_cards_observed"]
        ):
            raise ValueError("v65 THP-prefix card shape changed")
        omissions = stored_replay.get("omitted_allin_runout_boundaries")
        if (
            not isinstance(omissions, list)
            or len(omissions) != 2
            or {item.get("conn") for item in omissions} != {"A", "B"}
            or any(
                item.get("hand") != thp_expected["hand"]
                or item.get("stage") != thp_expected["stage"]
                or item.get("public_cards_observed")
                != thp_expected["public_cards_observed"]
                for item in omissions
            )
        ):
            raise ValueError("v65 wire omission identity changed")
        bindings, binding_issues = _omitted_allin_thp_bindings(
            strict_match,
            stored_replay,
            expected_hands=70,
            expected_names=expected_names,
        )
        wire_prefix, wire_prefix_issues = _wire_settlement_prefix(
            stored_replay,
            expected_hands=70,
            expected_names=expected_names,
        )
        thp_prefix = [
            {
                "hand": record["index"] + 1,
                "earnings_by_player": {
                    name: record["earnings_by_player"][name]
                    for name in expected_names
                },
            }
            for record in strict_match["records"][:-1]
        ]
        if (
            bindings is None
            or binding_issues
            or len(bindings) != 1
            or bindings[0].get("hand") != thp_expected["hand"]
            or bindings[0].get("thp_board_scope")
            != "observed_wire_prefix"
            or bindings[0].get("thp_public_card_count")
            != thp_expected["public_cards_observed"]
            or wire_prefix is None
            or wire_prefix_issues
            or wire_prefix != thp_prefix
        ):
            raise ValueError("v65 THP-prefix repair is not exact")
        thp_failures.append({
            **thp_expected,
            "round_id": expected["round_id"],
            "wire_omissions_digest": _bcd.canonical_digest(omissions),
            "strict_match_digest": _bcd.canonical_digest(strict_match),
            "prefix_binding_digest": _bcd.canonical_digest(bindings),
        })

    payload = {
        "schema_version": 1,
        "kind": _bcd._V65_DIAGNOSIS_KIND,
        "profile_id": _bcd._V65_PROFILE_ID,
        "defect_ids": list(_bcd._V65_DEFECT_IDS),
        "incident_identity": incident_identity,
        **source_identities,
        "evidence_sha256": evidence_sha256,
        "evidence_archive_sha256": archive["archive_sha256"],
        "evidence_archive_manifest_digest": archive["manifest_digest"],
        "suite_summary_sha256": _bcd._sha256_bytes(summary_raw),
        "attribution_digest": _bcd.canonical_digest(attribution),
        "round_receipts": round_receipts,
        "live_deferred_failures": live_failures,
        "thp_prefix_failures": thp_failures,
        "authority_absence": authority_absence,
        "strength_evaluation": "not_applicable",
        "disposition": "abandon_and_reprepare_only_without_evidence_reuse",
    }
    return _bcd._validate_v65_failure_diagnosis_envelope({
        **payload,
        "proof_digest": _bcd.canonical_digest(payload),
    })

