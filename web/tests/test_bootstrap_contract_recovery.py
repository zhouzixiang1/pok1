from __future__ import annotations

import copy
from contextlib import nullcontext
import json
from pathlib import Path
from types import SimpleNamespace

import bootstrap_contract_recovery as recovery
from bot_artifact import canonical_digest
import checkpoint_schema
import evaluation_contract
import evolution_core
import evolution_scope
import national_runtime_authority
import official_certification
import official_certification_job
import pytest


OLD_HEAD = "1" * 40
NEW_HEAD = "2" * 40
OLD_HASH = "3" * 64
CANDIDATE_HASH = "4" * 64
JOB_ID = "5" * 64
WORKFLOW = "generation:143:workflow-v62"


def _execution_scope():
    return {
        "workflow_run_id": WORKFLOW,
        "checkpoint_revision": 13,
        "candidate_version": 143,
        "candidate_label": "national_v143",
        "candidate_artifact_hash": CANDIDATE_HASH,
        "control_id": "first_strict_control_v1",
        "control_artifact_hash": "a" * 64,
        "control_receipt_digest": "b" * 64,
        "precommit_plan_digest": "c" * 64,
        "evaluation_contract_digest": "d" * 64,
        "native_match_timing_plan_digest": "e" * 64,
        "precommit_attempt": 1,
    }


def _execution_receipts():
    return [{"repeat": repeat} for repeat in range(1, 9)]


def _execution_terminal():
    scope = _execution_scope()
    return {
        "outcome": "succeeded",
        "scope_digest": canonical_digest(scope),
        "receipt_digest": "f" * 64,
    }


def _execution_success_proof():
    payload = {
        "scope": _execution_scope(),
        "expected_receipts": _execution_receipts(),
        "terminal_receipt": _execution_terminal(),
    }
    return {**payload, "proof_digest": canonical_digest(payload)}


def _checkpoint():
    parked_payload = {
        "schema_version": 1,
        "kind": "official-first-strict-bootstrap-parked-request",
        "workflow_run_id": WORKFLOW,
        "candidate_hash": CANDIDATE_HASH,
        "bootstrap_control_id": "first_strict_control_v1",
    }
    parked = {
        **parked_payload,
        "request_digest": canonical_digest(parked_payload),
    }
    return {
        "next_v": 143,
        "source_v": 142,
        "parent2_v": None,
        "stage": "official_bootstrap_required",
        "workflow_run_id": WORKFLOW,
        "checkpoint_revision": 22,
        "publication_intent": None,
        "official_job": None,
        "precommit_attempt": 1,
        "audit_context": {
            "official_bootstrap_request": parked,
            "first_strict_control_execution_scope": _execution_scope(),
            "precommit_eval_plan": {
                "opponents": [{
                    "authority": "system_first_strict_control",
                }]
            },
        },
        "gate_results": {
            "precommit_eval": {
                "passed": True,
                "control_execution_scope": _execution_scope(),
                "first_strict_execution_terminal_receipt": (
                    _execution_terminal()
                ),
                "national": {
                    "matchups": [{
                        "repeats": [
                            {"execution_receipt": receipt}
                            for receipt in _execution_receipts()
                        ]
                    }]
                },
            }
        },
        "repo_baseline": {
            "head": OLD_HEAD[:12],
            "evaluation_contract": {
                "version": 40,
                "stage": "official_bootstrap_required",
                "path_exact": ["web/core/official_platform_harness.py"],
                "path_prefixes": [],
                "runtime_prefixes": [],
                "non_contract_prefixes": [],
                "hash": OLD_HASH,
            },
        },
    }


def _contract_chain():
    parked = {
        "evaluation_contract_version": 40,
        "evaluation_contract_hash": OLD_HASH,
        "checkpoint_contract_digest": "a" * 64,
        "protocol_bootstrap_receipt_digest": "b" * 64,
        "first_strict_control_receipt_digest": "c" * 64,
        "bootstrap_policy_digest": "d" * 64,
    }
    authorization = {
        **parked,
        "bootstrap_control_receipt_digest": "e" * 64,
        "candidate_binding_digest": "f" * 64,
    }
    bootstrap_receipt = {
        "receipt_digest": "e" * 64,
        "bootstrap_policy": {"contract_digest": "d" * 64},
    }
    candidate_binding = {"candidate_binding_digest": "f" * 64}
    control_receipt = {"receipt_digest": "c" * 64}
    protocol_receipt = {"receipt_digest": "b" * 64, "kind": "protocol"}
    parked["protocol_bootstrap_receipt"] = protocol_receipt
    parked["first_strict_control_receipt"] = dict(control_receipt)
    return (
        parked,
        authorization,
        bootstrap_receipt,
        candidate_binding,
        control_receipt,
    )


def _legacy_false_illegal_call_events():
    events = []
    dt = 0.0

    def add(conn, direction, event_type, raw, messages, remaining):
        nonlocal dt
        dt += 0.05
        events.append({
            "ts": f"2026-07-19T00:00:{dt:06.3f}",
            "t": 1000.0 + dt,
            "dt": dt,
            "conn": conn,
            "direction": direction,
            "event_type": event_type,
            "raw_repr": raw,
            "raw_hex": raw.encode("utf-8").hex(),
            "messages": messages,
            "remaining": remaining,
            "details": {},
        })

    add("A", "server_to_bot", "data", "name", ["name"], "")
    add("B", "server_to_bot", "data", "name", ["name"], "")
    add("A", "bot_to_server", "data", "BotA", [], "BotA")
    add("B", "bot_to_server", "data", "BotB", [], "BotB")
    add("A", "bot_to_server", "idle_flush", "", ["BotA"], "")
    add("B", "bot_to_server", "idle_flush", "", ["BotB"], "")
    add(
        "A",
        "server_to_bot",
        "data",
        "preflop|BIGBLIND|<2,8><1,3>",
        ["preflop|BIGBLIND|<2,8><1,3>"],
        "",
    )
    add(
        "B",
        "server_to_bot",
        "data",
        "preflop|SMALLBLIND|<1,7><0,11>",
        ["preflop|SMALLBLIND|<1,7><0,11>"],
        "",
    )
    add("B", "bot_to_server", "data", "raise 300", [], "raise 300")
    add("A", "server_to_bot", "data", "raise 300", [], "raise 300")
    add("B", "bot_to_server", "idle_flush", "", ["raise 300"], "")
    add("A", "server_to_bot", "idle_flush", "", ["raise 300"], "")
    add("A", "bot_to_server", "data", "call", [], "call")
    add(
        "A",
        "server_to_bot",
        "data",
        "flop|<3,2><2,6><0,2>",
        ["flop|<3,2><2,6><0,2>"],
        "",
    )
    add(
        "B",
        "server_to_bot",
        "data",
        "flop|<3,2><2,6><0,2>",
        ["flop|<3,2><2,6><0,2>"],
        "",
    )
    add("A", "bot_to_server", "idle_flush", "", ["call"], "")
    add("A", "bot_to_server", "stream_eof", "", [], "")
    add("B", "bot_to_server", "stream_eof", "", [], "")
    return events


def _causal_failure_diagnosis():
    issue_counts = (1, 2, 1, 1, 1, 1, 2, 1)
    slots = [
        *(f"self_play_{index:02d}" for index in range(1, 6)),
        *(f"opponent_{index:02d}" for index in range(1, 4)),
    ]
    rounds = []
    for offset, (slot, count) in enumerate(zip(slots, issue_counts), 1):
        issue = "illegal_call" if offset == 1 else "unsolicited_client_action"
        rounds.append({
            "slot": slot,
            "round_id": f"{slot}_20260719_000000",
            "receipt_sha256": f"{offset:x}" * 64,
            "wire_events_sha256": f"{offset + 1:x}" * 64,
            "replay_summary_sha256": f"{offset + 2:x}" * 64,
            "event_count": recovery._LEGACY_INCIDENT_EVENT_COUNTS[offset - 1],
            "stored_events_seen": recovery._LEGACY_INCIDENT_STORED_COUNTS[offset - 1],
            "legacy_issue_kinds": [issue] * count,
            "deferred_observation_bindings_digest": f"{offset + 3:x}" * 64,
            "legacy_summary_digest": f"{offset + 4:x}" * 64,
            "corrected_summary_digest": f"{offset + 5:x}" * 64,
            "max_pending_wait_sec": 1.04,
            "corrected_hands_started": recovery._LEGACY_INCIDENT_HANDS[offset - 1],
            "corrected_settlements": recovery._LEGACY_INCIDENT_SETTLEMENTS[offset - 1],
            "corrected_pending_count": 1,
        })
    payload = {
        "schema_version": 1,
        "kind": recovery._CAUSAL_FAILURE_DIAGNOSIS_KIND,
        "defect_id": recovery._CAUSAL_FAILURE_DEFECT_ID,
        "baseline_wire_probe_sha256": "a" * 64,
        "repair_wire_probe_sha256": "b" * 64,
        "evidence_sha256": "c" * 64,
        "evidence_archive_sha256": "d" * 64,
        "evidence_archive_manifest_digest": "e" * 64,
        "suite_summary_sha256": "f" * 64,
        "attribution_digest": "1" * 64,
        "original_issue_kinds": sorted(recovery._LEGACY_FALSE_WIRE_ISSUES),
        "original_issue_count": 10,
        "rounds": rounds,
        "strength_evaluation": "not_applicable",
        "disposition": "abandon_and_reprepare_only_without_evidence_reuse",
    }
    return {**payload, "proof_digest": canonical_digest(payload)}


def test_legacy_idle_flush_false_call_is_rebuilt_from_raw_causally():
    from official_wire_probe import replay_events

    events = _legacy_false_illegal_call_events()
    legacy = replay_events(events, now=max(item["t"] for item in events))

    assert [item["kind"] for item in legacy["issues"]] == ["illegal_call"]
    causal, bindings = recovery._legacy_wire_causalize(events)
    corrected = replay_events(
        causal,
        now=max(item["t"] for item in events),
        finalized=False,
    )

    assert corrected["issues"] == []
    assert corrected["warnings"] == []
    call_raw = next(
        index for index, item in enumerate(events, 1)
        if item["event_type"] == "data"
        and item["direction"] == "bot_to_server"
        and item["raw_repr"] == "call"
    )
    call_flush = next(
        index for index, item in enumerate(events, 1)
        if item["event_type"] == "idle_flush"
        and item["messages"] == ["call"]
    )
    assert {
        "flush_record_seq": call_flush,
        "source_record_seq": call_raw,
        "observation_seq": causal[call_raw - 1]["observation_seq"],
    } in bindings
    assert causal[call_flush - 1]["observation_t"] == events[call_raw - 1]["t"]


def test_legacy_stored_replay_allows_only_full_or_last_eof_omission():
    from official_wire_probe import replay_events

    events = _legacy_false_illegal_call_events()
    stored = replay_events(events, now=max(item["t"] for item in events))
    assert recovery._legacy_replay_matches_stored(events, stored)

    forged = copy.deepcopy(stored)
    forged["events_seen"] = len(events) - 2
    with pytest.raises(ValueError, match="event count is invalid"):
        recovery._legacy_replay_matches_stored(events, forged)


@pytest.mark.parametrize(
    "mutation",
    (
        lambda events: events[0].update(raw_hex="ff"),
        lambda events: events[6].update(t=events[5]["t"] - 0.01),
        lambda events: events[0].update(observation_seq=1),
        lambda events: events[15].update(conn="B"),
        lambda events: events.pop(),
    ),
)
def test_legacy_causalizer_fails_closed_on_raw_time_shape_source_or_eof(
    mutation,
):
    events = copy.deepcopy(_legacy_false_illegal_call_events())
    mutation(events)

    with pytest.raises((ValueError, UnicodeDecodeError)):
        recovery._legacy_wire_causalize(events)


def test_causal_failure_diagnosis_is_tagged_exact_and_cannot_be_resigned_wider():
    diagnosis = _causal_failure_diagnosis()

    assert recovery._validate_causal_failure_diagnosis_envelope(
        diagnosis
    ) == diagnosis

    for mutation in (
        lambda value: value.update(original_issue_count=9),
        lambda value: value["rounds"][0].update(legacy_issue_kinds=[]),
        lambda value: value["rounds"][0].update(event_count=19),
        lambda value: value["rounds"][2].update(corrected_hands_started=1),
        lambda value: value["rounds"][0].update(corrected_pending_count=0),
        lambda value: value.update(strength_evaluation="candidate_strength"),
        lambda value: value.update(
            disposition="reuse_as_official_certification"
        ),
    ):
        forged = copy.deepcopy(diagnosis)
        mutation(forged)
        forged["proof_digest"] = canonical_digest({
            key: item for key, item in forged.items()
            if key != "proof_digest"
        })
        with pytest.raises(recovery.BootstrapContractRecoveryError):
            recovery._validate_causal_failure_diagnosis_envelope(forged)


def test_round_job_envelope_must_equal_status_including_opponent_bindings():
    status_envelope = {
        "job_id": JOB_ID,
        "attempt": 1,
        "candidate_hash": CANDIDATE_HASH,
        "opponent_hash": "6" * 64,
        "opponent_selection_digest": "7" * 64,
    }
    assert recovery._require_exact_round_job_envelope(
        dict(status_envelope),
        status_envelope,
        job_id=JOB_ID,
        candidate_hash=CANDIDATE_HASH,
    ) == status_envelope

    for field in ("opponent_hash", "opponent_selection_digest"):
        drifted = dict(status_envelope)
        drifted[field] = "8" * 64
        with pytest.raises(ValueError, match="not exact"):
            recovery._require_exact_round_job_envelope(
                drifted,
                status_envelope,
                job_id=JOB_ID,
                candidate_hash=CANDIDATE_HASH,
            )
    with pytest.raises(ValueError, match="status official job envelope is missing"):
        recovery._require_exact_round_job_envelope(
            status_envelope,
            {},
            job_id=JOB_ID,
            candidate_hash=CANDIDATE_HASH,
        )


def test_job_owned_intermediate_directory_symlink_fails_closed(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    assert recovery._require_regular_directory(real) == real
    with pytest.raises(ValueError, match="directory is unsafe"):
        recovery._require_regular_directory(linked)


def test_historical_causal_failure_reopens_raw_diagnosis_and_signed_failure(
    tmp_path,
    monkeypatch,
):
    import official_bootstrap

    directory = tmp_path / JOB_ID
    directory.mkdir()
    diagnosis = _causal_failure_diagnosis()
    entry = {
        "entry_digest": "9" * 64,
        "sequence": 3,
        "outcome": "official-failed",
        "classification": "protocol",
        "authoritative": True,
        "blocking": True,
        "certificate_digest": "",
        "strength_evaluation": "not_applicable",
    }
    status = {
        "status": "official-failed",
        "summary": {"rounds_run": 8},
        "official_verdict_ledger_entry": entry,
    }
    request = {"request_digest": "6" * 64}
    state = {"revision": 98, "attempt": 1}
    result = {"result_digest": "7" * 64, "status": status}
    public = {
        "state": "completed",
        "pending": False,
        "progress": {"rounds_requested": 8, "rounds_completed": 8},
    }
    expected = {
        "job_id": JOB_ID,
        "request_digest": request["request_digest"],
        "state_revision": state["revision"],
        "result_digest": result["result_digest"],
        "status_digest": canonical_digest(status),
        "rounds_requested": 8,
        "rounds_completed": 8,
        "rounds_run": 8,
        "ledger_entry_digest": entry["entry_digest"],
        "ledger_sequence": entry["sequence"],
        "contract_failure_diagnosis": diagnosis,
    }
    claim = {
        "terminal_job": expected,
        "candidate": {"artifact_hash": CANDIDATE_HASH},
        "git_contract_migration": {
            "baseline_head": OLD_HEAD,
            "current_head": NEW_HEAD,
        },
    }
    monkeypatch.setattr(official_certification_job, "_job_lock", lambda *_a: nullcontext())
    monkeypatch.setattr(
        official_certification_job,
        "_read_json",
        lambda path: request if path.name == "request.json" else state,
    )
    monkeypatch.setattr(official_certification_job, "_validate_request", lambda _r: [])
    monkeypatch.setattr(official_certification_job, "_public_state", lambda *_a: public)
    monkeypatch.setattr(official_certification_job, "_result_payload", lambda *_a: result)
    monkeypatch.setattr(
        official_bootstrap,
        "_validated_ledger_entries",
        lambda: ([entry], []),
    )
    observed = {"value": diagnosis, "kwargs": None}

    def rebuild_diagnosis(*_args, **kwargs):
        observed["kwargs"] = kwargs
        return observed["value"]

    monkeypatch.setattr(
        recovery,
        "_legacy_causal_failure_diagnosis",
        rebuild_diagnosis,
    )

    assert recovery._historical_terminal_job_matches(
        claim,
        directory,
        root=tmp_path,
    )
    assert observed["kwargs"]["require_live_repair_source"] is False
    observed["value"] = {
        **diagnosis,
        "proof_digest": "0" * 64,
    }
    assert not recovery._historical_terminal_job_matches(
        claim,
        directory,
        root=tmp_path,
    )


def test_historical_terminal_job_rejects_symlinked_job_directory(tmp_path):
    real = tmp_path / "real-job"
    real.mkdir()
    linked = tmp_path / JOB_ID
    linked.symlink_to(real, target_is_directory=True)

    assert not recovery._historical_terminal_job_matches(
        {"terminal_job": {"job_id": JOB_ID}},
        linked,
        root=tmp_path,
    )


def test_bootstrap_contract_chain_binds_baseline_parked_authorization_and_control():
    (
        parked,
        authorization,
        bootstrap_receipt,
        candidate_binding,
        control_receipt,
    ) = _contract_chain()

    assert recovery._bootstrap_contract_chain_issues(
        parked,
        authorization,
        bootstrap_receipt,
        candidate_binding,
        control_receipt,
        expected_evaluation_contract_version=40,
        expected_evaluation_contract_hash=OLD_HASH,
        expected_checkpoint_contract_digest="a" * 64,
        expected_protocol_bootstrap_receipt_digest="b" * 64,
        expected_first_strict_control_receipt_digest="c" * 64,
        expected_protocol_bootstrap_receipt=parked[
            "protocol_bootstrap_receipt"
        ],
        expected_first_strict_control_receipt=control_receipt,
    ) == []


def test_bootstrap_contract_chain_rejects_self_consistent_old_contract_version():
    (
        parked,
        authorization,
        bootstrap_receipt,
        candidate_binding,
        control_receipt,
    ) = _contract_chain()
    parked["evaluation_contract_version"] = 39
    authorization["evaluation_contract_version"] = 39

    issues = recovery._bootstrap_contract_chain_issues(
        parked,
        authorization,
        bootstrap_receipt,
        candidate_binding,
        control_receipt,
        expected_evaluation_contract_version=39,
        expected_evaluation_contract_hash=OLD_HASH,
        expected_checkpoint_contract_digest="a" * 64,
        expected_protocol_bootstrap_receipt_digest="b" * 64,
        expected_first_strict_control_receipt_digest="c" * 64,
        expected_protocol_bootstrap_receipt=parked[
            "protocol_bootstrap_receipt"
        ],
        expected_first_strict_control_receipt=control_receipt,
    )

    assert "bootstrap_contract_evaluation_contract_chain_mismatch" in issues


@pytest.mark.parametrize(
    ("target", "field", "issue"),
    [
        ("parked", "evaluation_contract_version", "bootstrap_contract_evaluation_contract_chain_mismatch"),
        ("authorization", "evaluation_contract_version", "bootstrap_contract_evaluation_contract_chain_mismatch"),
        ("parked", "evaluation_contract_hash", "bootstrap_contract_evaluation_contract_chain_mismatch"),
        ("authorization", "evaluation_contract_hash", "bootstrap_contract_evaluation_contract_chain_mismatch"),
        ("parked", "checkpoint_contract_digest", "bootstrap_contract_checkpoint_contract_chain_mismatch"),
        ("authorization", "checkpoint_contract_digest", "bootstrap_contract_checkpoint_contract_chain_mismatch"),
        ("parked", "protocol_bootstrap_receipt_digest", "bootstrap_contract_control_receipt_chain_mismatch"),
        ("authorization", "protocol_bootstrap_receipt_digest", "bootstrap_contract_control_receipt_chain_mismatch"),
        ("parked", "first_strict_control_receipt_digest", "bootstrap_contract_control_receipt_chain_mismatch"),
        ("authorization", "first_strict_control_receipt_digest", "bootstrap_contract_control_receipt_chain_mismatch"),
        ("control", "receipt_digest", "bootstrap_contract_control_receipt_chain_mismatch"),
        ("authorization", "bootstrap_control_receipt_digest", "bootstrap_contract_embedded_binding_chain_mismatch"),
        ("bootstrap", "receipt_digest", "bootstrap_contract_embedded_binding_chain_mismatch"),
        ("authorization", "candidate_binding_digest", "bootstrap_contract_embedded_binding_chain_mismatch"),
        ("binding", "candidate_binding_digest", "bootstrap_contract_embedded_binding_chain_mismatch"),
        ("parked", "bootstrap_policy_digest", "bootstrap_contract_embedded_binding_chain_mismatch"),
        ("parked_protocol", "kind", "bootstrap_contract_embedded_protocol_receipt_mismatch"),
        ("parked_control", "kind", "bootstrap_contract_embedded_control_receipt_mismatch"),
    ],
)
def test_bootstrap_contract_chain_rejects_each_spliced_identity(
    target,
    field,
    issue,
):
    (
        parked,
        authorization,
        bootstrap_receipt,
        candidate_binding,
        control_receipt,
    ) = _contract_chain()
    mapping = {
        "parked": parked,
        "authorization": authorization,
        "bootstrap": bootstrap_receipt,
        "binding": candidate_binding,
        "control": control_receipt,
        "parked_protocol": parked["protocol_bootstrap_receipt"],
        "parked_control": parked["first_strict_control_receipt"],
    }[target]
    mapping[field] = "0" * 64 if field != "evaluation_contract_version" else 39

    issues = recovery._bootstrap_contract_chain_issues(
        parked,
        authorization,
        bootstrap_receipt,
        candidate_binding,
        control_receipt,
        expected_evaluation_contract_version=40,
        expected_evaluation_contract_hash=OLD_HASH,
        expected_checkpoint_contract_digest="a" * 64,
        expected_protocol_bootstrap_receipt_digest="b" * 64,
        expected_first_strict_control_receipt_digest="c" * 64,
        expected_protocol_bootstrap_receipt={
            "receipt_digest": "b" * 64,
            "kind": "protocol",
        },
        expected_first_strict_control_receipt=control_receipt,
    )

    assert issue in issues


@pytest.mark.parametrize(
    ("embedded_field", "issue"),
    [
        (
            "protocol_bootstrap_receipt",
            "bootstrap_contract_embedded_protocol_receipt_mismatch",
        ),
        (
            "first_strict_control_receipt",
            "bootstrap_contract_embedded_control_receipt_mismatch",
        ),
    ],
)
def test_rehashed_parked_request_cannot_splice_embedded_receipt(
    embedded_field,
    issue,
):
    (
        parked,
        authorization,
        bootstrap_receipt,
        candidate_binding,
        control_receipt,
    ) = _contract_chain()
    expected_protocol = dict(parked["protocol_bootstrap_receipt"])
    parked[embedded_field] = {
        **parked[embedded_field],
        "spliced": True,
    }
    parked["request_digest"] = canonical_digest({
        key: value for key, value in parked.items() if key != "request_digest"
    })

    issues = recovery._bootstrap_contract_chain_issues(
        parked,
        authorization,
        bootstrap_receipt,
        candidate_binding,
        control_receipt,
        expected_evaluation_contract_version=40,
        expected_evaluation_contract_hash=OLD_HASH,
        expected_checkpoint_contract_digest="a" * 64,
        expected_protocol_bootstrap_receipt_digest="b" * 64,
        expected_first_strict_control_receipt_digest="c" * 64,
        expected_protocol_bootstrap_receipt=expected_protocol,
        expected_first_strict_control_receipt=control_receipt,
    )

    assert issue in issues


def _configure_claim(monkeypatch, root: Path):
    root.mkdir(parents=True)
    (root / "bots" / "national_v143").mkdir(parents=True)
    for name in recovery._STRICT_FILES:
        (root / "bots" / "national_v143" / name).write_text("x", encoding="utf-8")

    def git(_root, *args, binary=False):
        if args[:2] == ("rev-parse", "--verify"):
            value = args[2].split("^", 1)[0]
            return OLD_HEAD if value in {OLD_HEAD, OLD_HEAD[:12]} else NEW_HEAD
        if args == ("rev-parse", "HEAD") or args == ("rev-parse", "origin/main"):
            return NEW_HEAD
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return "main"
        if args == ("status", "--porcelain", "--untracked-files=no"):
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(recovery, "_git", git)
    monkeypatch.setattr(
        recovery,
        "_git_absence",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        recovery,
        "_read_succeeded_first_strict_execution",
        lambda _scope, *, expected_receipts, expected_terminal_receipt: (
            expected_terminal_receipt
        ),
    )
    monkeypatch.setattr(recovery, "_contract_hash_at_head", lambda *_a: OLD_HASH)
    monkeypatch.setattr(
        recovery,
        "_safe_candidate",
        lambda *_a, **_k: {
            "path": "bots/national_v143",
            "artifact_hash": CANDIDATE_HASH,
            "files": sorted(recovery._STRICT_FILES),
        },
    )
    monkeypatch.setattr(
        recovery,
        "_terminal_job_facts",
        lambda *_a, **_k: {
            "job_id": JOB_ID,
            "request_digest": "6" * 64,
            "state_revision": 4,
            "result_digest": "7" * 64,
            "status_digest": "8" * 64,
            "rounds_requested": 8,
            "rounds_completed": 0,
            "rounds_run": 0,
            "ledger_entry_digest": "9" * 64,
            "ledger_sequence": 2,
            "control_consumption": {
                "valid": True,
                "successful_count": 0,
                "max_successful_consumptions": 1,
            },
        },
    )
    monkeypatch.setattr(
        checkpoint_schema,
        "strict_checkpoint_event_identity",
        lambda *_a, **_k: {"gen": 143},
    )
    monkeypatch.setattr(
        evaluation_contract,
        "build_evaluation_contract",
        lambda *_a, **_k: {
            "version": 40,
            "stage": "official_bootstrap_required",
            "path_exact": ["web/core/official_platform_harness.py"],
            "path_prefixes": [],
            "runtime_prefixes": [],
            "non_contract_prefixes": [],
            "hash": "a" * 64,
        },
    )
    monkeypatch.setattr(
        evaluation_contract,
        "classify_contract_paths",
        lambda paths, _contract: {
            "contract_paths": list(paths),
            "external_paths": [],
        },
    )
    monkeypatch.setattr(
        evolution_scope,
        "changed_paths_between_heads",
        lambda *_a: ["web/core/official_platform_harness.py"],
    )
    monkeypatch.setattr(
        recovery.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(
        official_certification,
        "status_payload",
        lambda *_a: {"status": "official-inconclusive"},
    )
    monkeypatch.setattr(
        official_certification,
        "official_full_certified",
        lambda *_a: False,
    )
    monkeypatch.setattr(
        official_certification_job,
        "job_snapshot",
        lambda: {"pending": 0, "running": 0, "jobs": []},
    )
    monkeypatch.setattr(evolution_core, "get_active_bots", lambda: [])
    monkeypatch.setattr(
        national_runtime_authority,
        "strict_published_bot_names",
        lambda: (),
    )


def _build(root: Path, checkpoint=None):
    return recovery.build_claim(
        root,
        checkpoint=checkpoint or _checkpoint(),
        expected_baseline_head=OLD_HEAD,
        expected_baseline_contract_hash=OLD_HASH,
        expected_current_head=NEW_HEAD,
        expected_workflow_run_id=WORKFLOW,
        expected_checkpoint_revision=22,
        expected_candidate_hash=CANDIDATE_HASH,
        expected_terminal_job_id=JOB_ID,
    )


def test_build_claim_binds_exact_parked_contract_and_terminal_job(tmp_path, monkeypatch):
    root = tmp_path / ".evolution_pok"
    _configure_claim(monkeypatch, root)

    claim = _build(root)

    assert claim["old_checkpoint"]["checkpoint_revision"] == 22
    assert claim["git_contract_migration"]["baseline_head"] == OLD_HEAD
    assert claim["git_contract_migration"]["contract_paths"] == [
        "web/core/official_platform_harness.py"
    ]
    assert claim["terminal_job"]["rounds_completed"] == 0
    assert claim["terminal_job"]["control_consumption"]["successful_count"] == 0
    assert claim["schema_version"] == 2
    assert claim["first_strict_execution_success"] == (
        _execution_success_proof()
    )
    assert claim["claim_digest"] == canonical_digest({
        key: value for key, value in claim.items() if key != "claim_digest"
    })


def test_build_claim_rejects_seven_of_eight_first_strict_receipts(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / ".evolution_pok"
    _configure_claim(monkeypatch, root)
    checkpoint = _checkpoint()
    checkpoint["gate_results"]["precommit_eval"]["national"][
        "matchups"
    ][0]["repeats"].pop()

    with pytest.raises(recovery.BootstrapContractRecoveryError) as exc:
        _build(root, checkpoint)

    assert (
        "bootstrap_contract_first_strict_success_checkpoint_invalid"
        in exc.value.issues
    )


@pytest.mark.parametrize(
    ("mutation", "issue"),
    [
        (lambda ckpt: ckpt.update(stage="official_certifying"), "bootstrap_contract_stage_not_parked"),
        (lambda ckpt: ckpt.update(checkpoint_revision=23), "bootstrap_contract_checkpoint_identity_mismatch"),
        (lambda ckpt: ckpt.update(publication_intent={"id": "x"}), "bootstrap_contract_publication_intent_present"),
        (lambda ckpt: ckpt.update(official_job={"state": "running"}), "bootstrap_contract_attached_official_job_present"),
    ],
)
def test_build_claim_rejects_wrong_stage_cas_or_publication_state(
    tmp_path, monkeypatch, mutation, issue,
):
    root = tmp_path / ".evolution_pok"
    _configure_claim(monkeypatch, root)
    checkpoint = _checkpoint()
    mutation(checkpoint)

    with pytest.raises(recovery.BootstrapContractRecoveryError) as exc:
        _build(root, checkpoint)

    assert issue in exc.value.issues


def test_build_claim_rejects_self_consistent_non_v40_baseline(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / ".evolution_pok"
    _configure_claim(monkeypatch, root)
    checkpoint = _checkpoint()
    checkpoint["repo_baseline"]["evaluation_contract"]["version"] = 39

    with pytest.raises(recovery.BootstrapContractRecoveryError) as exc:
        _build(root, checkpoint)

    assert "bootstrap_contract_baseline_contract_invalid" in exc.value.issues


def test_publish_and_reload_external_claim_is_idempotent_and_content_bound(
    tmp_path, monkeypatch,
):
    root = tmp_path / ".evolution_pok"
    _configure_claim(monkeypatch, root)
    results = root / "web" / "core" / "results"
    results.mkdir(parents=True)
    claim = _build(root)

    first = recovery.publish_claim(root, claim)
    second = recovery.publish_claim(root, claim)

    assert first == second
    assert recovery.load_claim(root, claim["claim_digest"]) == claim
    tampered = json.loads(first.read_text(encoding="utf-8"))
    tampered["terminal_job"]["rounds_completed"] = 1
    first.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(recovery.BootstrapContractRecoveryError):
        recovery.load_claim(root, claim["claim_digest"])


def test_external_claim_reopens_success_journal_and_old_schema_is_inert(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / ".evolution_pok"
    _configure_claim(monkeypatch, root)
    (root / "web" / "core" / "results").mkdir(parents=True)
    claim = _build(root)
    recovery.publish_claim(root, claim)

    monkeypatch.setattr(
        recovery,
        "_read_succeeded_first_strict_execution",
        lambda *_a, **_k: (_ for _ in ()).throw(
            RuntimeError("terminal drift")
        ),
    )
    with pytest.raises(
        recovery.BootstrapContractRecoveryError,
        match="first_strict_success_unverifiable",
    ):
        recovery.load_claim(root, claim["claim_digest"])

    old = json.loads(json.dumps(claim))
    old["schema_version"] = 1
    old.pop("first_strict_execution_success")
    old["claim_digest"] = canonical_digest({
        key: value for key, value in old.items() if key != "claim_digest"
    })
    with pytest.raises(
        recovery.BootstrapContractRecoveryError,
        match="bootstrap_contract_claim_invalid",
    ):
        recovery.publish_claim(root, old)


@pytest.mark.parametrize("field", ("workflow_run_id", "candidate_artifact_hash"))
def test_external_claim_rejects_resigned_success_crossbinding(
    tmp_path,
    monkeypatch,
    field,
):
    root = tmp_path / ".evolution_pok"
    _configure_claim(monkeypatch, root)
    claim = _build(root)
    forged = json.loads(json.dumps(claim))
    proof = forged["first_strict_execution_success"]
    proof["scope"][field] = (
        "generation:143:workflow-spliced"
        if field == "workflow_run_id"
        else "0" * 64
    )
    proof["terminal_receipt"]["scope_digest"] = canonical_digest(
        proof["scope"]
    )
    proof["proof_digest"] = canonical_digest({
        key: proof[key]
        for key in ("scope", "expected_receipts", "terminal_receipt")
    })
    forged["claim_digest"] = canonical_digest({
        key: value for key, value in forged.items()
        if key != "claim_digest"
    })

    with pytest.raises(
        recovery.BootstrapContractRecoveryError,
        match="claim_crossbinding_invalid",
    ):
        recovery.publish_claim(root, forged)


def test_private_fence_preserves_exact_success_but_generic_path_still_rejects(
    tmp_path,
    monkeypatch,
):
    import first_strict_execution_journal as journal
    import precommit_eval_contract
    import tool_bot_management as management
    import tool_eval
    import tool_gates

    checkpoint = _checkpoint()
    monkeypatch.setattr(
        recovery,
        "_read_succeeded_first_strict_execution",
        lambda _scope, *, expected_receipts, expected_terminal_receipt: (
            expected_terminal_receipt
        ),
    )
    monkeypatch.setattr(management, "get_bot_dir", lambda _v: tmp_path)
    monkeypatch.setattr(
        tool_gates,
        "_bot_code_fingerprint",
        lambda _path: CANDIDATE_HASH,
    )
    monkeypatch.setattr(
        precommit_eval_contract,
        "opponents_from_plan",
        lambda _plan: [],
    )
    monkeypatch.setattr(
        precommit_eval_contract,
        "build_evaluation_contract",
        lambda *_a, **_k: {},
    )
    monkeypatch.setattr(
        tool_eval,
        "_validate_first_strict_control_execution_scope",
        lambda *_a, **_k: (_execution_scope(), None),
    )
    called = {"abandon": 0}

    def reject_abandon(*_a, **_k):
        called["abandon"] += 1
        raise journal.FirstStrictExecutionJournalError(
            "first_strict_execution_abandon_terminal_conflict"
        )

    monkeypatch.setattr(journal, "abandon_control_execution", reject_abandon)

    preserved = management._fence_first_strict_control_execution(
        checkpoint,
        reason="official_bootstrap_contract_change:" + "a" * 64,
        preserved_success=_execution_success_proof(),
    )
    assert preserved["abandoned"] is False
    assert preserved["terminal_receipt"]["outcome"] == "succeeded"
    assert called["abandon"] == 0

    with pytest.raises(RuntimeError, match="abandon_terminal_conflict"):
        management._fence_first_strict_control_execution(
            checkpoint,
            reason="abandon_generation",
        )
    assert called["abandon"] == 1


def test_canonical_reason_reopens_external_proof_and_crossbinds_checkpoint(
    tmp_path,
    monkeypatch,
):
    import tool_bot_management as management

    digest = "a" * 64
    old = {
        "digest": "b" * 64,
        "workflow_run_id": WORKFLOW,
        "checkpoint_revision": 22,
        "next_v": 143,
        "source_v": 142,
        "stage": "official_bootstrap_required",
    }
    source = tmp_path / "bots" / "national_v143"
    source.mkdir(parents=True)
    for name in recovery._STRICT_FILES:
        (source / name).write_text("x", encoding="utf-8")
    artifact_hash = __import__("bot_artifact").hash_path(source)
    monkeypatch.setattr(management, "PROJECT_ROOT", tmp_path)
    external = {
        "old_checkpoint": old,
        "git_contract_migration": {"current_head": NEW_HEAD},
        "candidate": {
            "path": "bots/national_v143",
            "artifact_hash": artifact_hash,
        },
    }
    monkeypatch.setattr(recovery, "load_claim", lambda *_a: external)
    canonical = {
        "schema_version": 2,
        "abandon_reason": recovery.abandon_reason(digest),
        "checkpoint": dict(old),
        "git_head": NEW_HEAD,
        "git_state": {"head": NEW_HEAD},
        "candidate": {"path": "bots/national_v143", "present": True},
        "transaction_id": "c" * 64,
    }

    assert (
        management._validate_external_bootstrap_contract_abandon_proof(
            canonical
        )
        == external
    )
    canonical["checkpoint"]["digest"] = "c" * 64
    with pytest.raises(
        recovery.BootstrapContractRecoveryError,
        match="canonical_crossbinding_invalid",
    ):
        management._validate_external_bootstrap_contract_abandon_proof(
            canonical
        )
    monkeypatch.setattr(
        recovery,
        "load_claim",
        lambda *_a: (_ for _ in ()).throw(FileNotFoundError()),
    )
    with pytest.raises(FileNotFoundError):
        management._validate_external_bootstrap_contract_abandon_proof({
            "schema_version": 2,
            "abandon_reason": recovery.abandon_reason(digest),
            "checkpoint": dict(old),
            "git_head": NEW_HEAD,
            "git_state": {"head": NEW_HEAD},
            "candidate": {
                "path": "bots/national_v143",
                "present": True,
            },
            "transaction_id": "c" * 64,
        })


@pytest.mark.parametrize(
    ("mutation"),
    (
        lambda claim: claim.update(git_head=OLD_HEAD),
        lambda claim: claim["git_state"].update(head=OLD_HEAD),
        lambda claim: claim["candidate"].update(path="bots/national_v144"),
    ),
)
def test_canonical_external_binding_rejects_head_or_path_splice(
    tmp_path,
    monkeypatch,
    mutation,
):
    digest = "a" * 64
    old = {
        "digest": "b" * 64,
        "workflow_run_id": WORKFLOW,
        "checkpoint_revision": 22,
        "next_v": 143,
        "source_v": 142,
        "stage": "official_bootstrap_required",
    }
    source = tmp_path / "bots" / "national_v143"
    source.mkdir(parents=True)
    for name in recovery._STRICT_FILES:
        (source / name).write_text("x", encoding="utf-8")
    artifact_hash = __import__("bot_artifact").hash_path(source)
    monkeypatch.setattr(recovery, "load_claim", lambda *_a: {
        "old_checkpoint": old,
        "git_contract_migration": {"current_head": NEW_HEAD},
        "candidate": {
            "path": "bots/national_v143",
            "artifact_hash": artifact_hash,
        },
    })
    canonical = {
        "schema_version": 2,
        "abandon_reason": recovery.abandon_reason(digest),
        "checkpoint": dict(old),
        "git_head": NEW_HEAD,
        "git_state": {"head": NEW_HEAD},
        "candidate": {"path": "bots/national_v143", "present": True},
        "transaction_id": "c" * 64,
    }
    mutation(canonical)

    with pytest.raises(
        recovery.BootstrapContractRecoveryError,
        match="canonical_crossbinding_invalid",
    ):
        recovery.validate_canonical_abandon_external_binding(
            tmp_path,
            canonical,
        )


def test_canonical_external_binding_rehashes_source_and_quarantine(
    tmp_path,
    monkeypatch,
):
    digest = "a" * 64
    transaction_id = "c" * 64
    source = tmp_path / "bots" / "national_v143"
    source.mkdir(parents=True)
    for name in recovery._STRICT_FILES:
        (source / name).write_text("x", encoding="utf-8")
    artifact_hash = __import__("bot_artifact").hash_path(source)
    old = {
        "digest": "b" * 64,
        "workflow_run_id": WORKFLOW,
        "checkpoint_revision": 22,
        "next_v": 143,
        "source_v": 142,
        "stage": "official_bootstrap_required",
    }
    external = {
        "old_checkpoint": old,
        "git_contract_migration": {"current_head": NEW_HEAD},
        "candidate": {
            "path": "bots/national_v143",
            "artifact_hash": artifact_hash,
        },
    }
    monkeypatch.setattr(recovery, "load_claim", lambda *_a: external)
    canonical = {
        "schema_version": 2,
        "abandon_reason": recovery.abandon_reason(digest),
        "checkpoint": old,
        "git_head": NEW_HEAD,
        "git_state": {"head": NEW_HEAD},
        "candidate": {"path": "bots/national_v143", "present": True},
        "transaction_id": transaction_id,
    }

    assert recovery.validate_canonical_abandon_external_binding(
        tmp_path, canonical
    ) == external
    (source / "policy.py").write_text("drift", encoding="utf-8")
    with pytest.raises(
        recovery.BootstrapContractRecoveryError,
        match="candidate_hash_mismatch",
    ):
        recovery.validate_canonical_abandon_external_binding(
            tmp_path, canonical
        )

    (source / "policy.py").write_text("x", encoding="utf-8")
    quarantine = (
        tmp_path
        / "web/core/results/policy_epoch_abandon_transactions"
        / transaction_id
        / "candidate"
    )
    quarantine.parent.mkdir(parents=True)
    source.rename(quarantine)
    assert recovery.validate_canonical_abandon_external_binding(
        tmp_path, canonical
    ) == external
    (quarantine / "policy.py").write_text("drift", encoding="utf-8")
    with pytest.raises(
        recovery.BootstrapContractRecoveryError,
        match="candidate_hash_mismatch",
    ):
        recovery.validate_canonical_abandon_external_binding(
            tmp_path, canonical
        )


def test_claim_directory_symlink_fails_closed(tmp_path, monkeypatch):
    root = tmp_path / ".evolution_pok"
    _configure_claim(monkeypatch, root)
    results = root / "web" / "core" / "results"
    results.mkdir(parents=True)
    claim = _build(root)
    path = recovery.publish_claim(root, claim)
    real_directory = path.parent.with_name(path.parent.name + "-real")
    path.parent.rename(real_directory)
    path.parent.symlink_to(real_directory, target_is_directory=True)

    with pytest.raises(recovery.BootstrapContractRecoveryError):
        recovery.load_claim(root, claim["claim_digest"])


def test_generic_abandon_remains_blocked_without_external_authority():
    from pipeline_state import generic_abandon_block

    blocked = generic_abandon_block(_checkpoint())

    assert blocked["blocked"] is True
    assert blocked["stage"] == "official_bootstrap_required"
    assert blocked["next_tool"] is None


def test_private_authority_rebuilds_exact_external_claim(tmp_path, monkeypatch):
    import tool_bot_management as management

    root = tmp_path / ".evolution_pok"
    _configure_claim(monkeypatch, root)
    results = root / "web" / "core" / "results"
    results.mkdir(parents=True)
    checkpoint = _checkpoint()
    claim = _build(root, checkpoint)
    recovery.publish_claim(root, claim)
    monkeypatch.setattr(management, "PROJECT_ROOT", root)

    validated = management._bootstrap_contract_change_abandon_authority(
        checkpoint,
        reason=recovery.abandon_reason(claim["claim_digest"]),
        claim_digest=claim["claim_digest"],
    )

    assert validated == claim
    with pytest.raises(RuntimeError, match="reason_mismatch"):
        management._bootstrap_contract_change_abandon_authority(
            checkpoint,
            reason="abandon_generation",
            claim_digest=claim["claim_digest"],
        )


@pytest.mark.parametrize(
    ("install_failure", "issue"),
    [
        (
            lambda monkeypatch: monkeypatch.setattr(
                official_certification_job,
                "job_snapshot",
                lambda: {"pending": 1, "running": 1, "jobs": []},
            ),
            "bootstrap_contract_official_job_active",
        ),
        (
            lambda monkeypatch: monkeypatch.setattr(
                official_certification,
                "official_full_certified",
                lambda *_a: True,
            ),
            "bootstrap_contract_valid_certificate_present",
        ),
        (
            lambda monkeypatch: monkeypatch.setattr(
                recovery,
                "_terminal_job_facts",
                lambda *_a, **_k: (_ for _ in ()).throw(
                    recovery.BootstrapContractRecoveryError([
                        "bootstrap_contract_job_progress_not_zero_of_eight"
                    ])
                ),
            ),
            "bootstrap_contract_job_progress_not_zero_of_eight",
        ),
        (
            lambda monkeypatch: monkeypatch.setattr(
                recovery,
                "_safe_candidate",
                lambda *_a, **_k: (_ for _ in ()).throw(
                    recovery.BootstrapContractRecoveryError([
                        "bootstrap_contract_candidate_completed"
                    ])
                ),
            ),
            "bootstrap_contract_candidate_completed",
        ),
    ],
)
def test_claim_fails_closed_on_active_job_rounds_consumption_or_publication(
    tmp_path,
    monkeypatch,
    install_failure,
    issue,
):
    root = tmp_path / ".evolution_pok"
    _configure_claim(monkeypatch, root)
    install_failure(monkeypatch)

    with pytest.raises(recovery.BootstrapContractRecoveryError) as exc:
        _build(root)

    assert issue in exc.value.issues


def test_historical_job_requires_unique_finalized_claim_and_transaction(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / ".evolution_pok"
    _configure_claim(monkeypatch, root)
    results = root / "web" / "core" / "results"
    results.mkdir(parents=True)
    job = tmp_path / JOB_ID
    job.mkdir()
    claim = _build(root)
    recovery.publish_claim(root, claim)
    monkeypatch.setattr(
        recovery,
        "_historical_terminal_job_matches",
        lambda *_a, **_k: True,
    )
    monkeypatch.setattr(
        recovery,
        "_finalized_canonical_abandon_matches",
        lambda *_a: True,
    )

    assert recovery.is_finalized_historical_bootstrap_job(
        root,
        current_workflow_run_id="generation:143:workflow-v63",
        job_directory=job,
    )
    assert not recovery.is_finalized_historical_bootstrap_job(
        root,
        current_workflow_run_id=WORKFLOW,
        job_directory=job,
    )


@pytest.mark.parametrize(
    "mutation",
    (
        None,
        lambda canonical: canonical.update(git_head=OLD_HEAD),
        lambda canonical: canonical["checkpoint"].update(next_v=144),
        lambda canonical: canonical["checkpoint"].update(source_v=141),
        lambda canonical: canonical["checkpoint"].update(stage="verified"),
        lambda canonical: canonical["candidate"].update(
            path="bots/national_v144"
        ),
        lambda canonical: canonical.update(transaction_id="d" * 64),
    ),
)
def test_finalized_bootstrap_scanner_uses_full_external_crossbinding(
    tmp_path,
    monkeypatch,
    mutation,
):
    import epoch_authority
    import evolution_infra

    root = tmp_path / ".evolution_pok"
    transaction_id = "c" * 64
    directory = (
        root
        / "web/core/results/policy_epoch_abandon_transactions"
        / transaction_id
    )
    quarantine = directory / "candidate"
    quarantine.mkdir(parents=True)
    for name in recovery._STRICT_FILES:
        (quarantine / name).write_text("x", encoding="utf-8")
    artifact_hash = __import__("bot_artifact").hash_path(quarantine)
    old = {
        "digest": "b" * 64,
        "workflow_run_id": WORKFLOW,
        "checkpoint_revision": 22,
        "next_v": 143,
        "source_v": 142,
        "stage": "official_bootstrap_required",
    }
    external = {
        "claim_digest": "a" * 64,
        "old_checkpoint": old,
        "git_contract_migration": {"current_head": NEW_HEAD},
        "candidate": {
            "path": "bots/national_v143",
            "artifact_hash": artifact_hash,
        },
    }
    canonical = {
        "schema_version": 2,
        "abandon_reason": recovery.abandon_reason(
            external["claim_digest"]
        ),
        "checkpoint": dict(old),
        "git_head": NEW_HEAD,
        "git_state": {"head": NEW_HEAD},
        "candidate": {"path": "bots/national_v143", "present": True},
        "transaction_id": transaction_id,
    }
    if mutation is not None:
        mutation(canonical)
    (directory / "claim.json").write_text(
        json.dumps(canonical), encoding="utf-8"
    )
    (directory / "receipt.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(recovery, "load_claim", lambda *_a: external)
    monkeypatch.setattr(
        evolution_infra,
        "load_abandoned_version_receipts",
        lambda **_k: [],
    )
    monkeypatch.setattr(
        epoch_authority,
        "validate_abandon_finalize_receipt",
        lambda _claim, receipt, _rows: receipt,
    )

    observed = recovery._finalized_canonical_abandon(root, external)

    if mutation is None:
        assert observed is not None
        assert observed["transaction_id"] == transaction_id
    else:
        assert observed is None


def test_operator_cli_replays_completed_claim_after_checkpoint_clear(
    tmp_path,
    monkeypatch,
    capsys,
):
    from scripts import abandon_parked_bootstrap_contract_change as cli

    monkeypatch.setattr(cli, "ROOT", tmp_path / ".evolution_pok")
    monkeypatch.setattr(cli, "_runtime_checkout_identity_errors", lambda: [])
    monkeypatch.setattr(cli, "_runtime_process_errors", lambda: [])
    monkeypatch.setattr(cli, "read_pipeline_checkpoint", lambda: None)
    completed = {
        "status": "already_abandoned",
        "claim_digest": "a" * 64,
        "transaction_id": "b" * 64,
    }
    monkeypatch.setattr(cli, "finalized_claim_result", lambda *_a: completed)

    result = cli.main([
        "--execute",
        "--acknowledge-runtime-checkout",
        "--claim-digest", "a" * 64,
        "--expected-baseline-head", OLD_HEAD,
        "--expected-baseline-contract-hash", OLD_HASH,
        "--expected-current-head", NEW_HEAD,
        "--expected-workflow-run-id", WORKFLOW,
        "--expected-checkpoint-revision", "22",
        "--expected-candidate-hash", CANDIDATE_HASH,
        "--expected-terminal-job-id", JOB_ID,
    ])

    assert result == 0
    assert json.loads(capsys.readouterr().out) == completed


def test_operator_cli_resumes_clear_before_finalize_crash_prefix(
    tmp_path,
    monkeypatch,
    capsys,
):
    from scripts import abandon_parked_bootstrap_contract_change as cli

    monkeypatch.setattr(cli, "ROOT", tmp_path / ".evolution_pok")
    monkeypatch.setattr(cli, "_runtime_checkout_identity_errors", lambda: [])
    monkeypatch.setattr(cli, "_runtime_process_errors", lambda: [])
    monkeypatch.setattr(cli, "read_pipeline_checkpoint", lambda: None)
    monkeypatch.setattr(cli, "finalized_claim_result", lambda *_a: None)
    monkeypatch.setattr(cli, "_reconciliation_lock", nullcontext)
    monkeypatch.setattr(cli, "_index_lock", nullcontext)
    monkeypatch.setattr(cli, "incomplete_claim_resume_identity", lambda *_a: {
        "workflow_run_id": WORKFLOW,
        "next_v": 143,
        "source_v": 142,
        "stage": "official_bootstrap_required",
        "checkpoint_revision": 22,
    })

    async def abandon(**kwargs):
        assert kwargs["_operator_bootstrap_contract_change_claim_digest"] == "a" * 64
        return {"abandoned": True, "abandon_transaction_id": "b" * 64}

    monkeypatch.setattr(cli, "_do_abandon_generation", abandon)

    result = cli.main([
        "--execute",
        "--acknowledge-runtime-checkout",
        "--claim-digest", "a" * 64,
        "--expected-baseline-head", OLD_HEAD,
        "--expected-baseline-contract-hash", OLD_HASH,
        "--expected-current-head", NEW_HEAD,
        "--expected-workflow-run-id", WORKFLOW,
        "--expected-checkpoint-revision", "22",
        "--expected-candidate-hash", CANDIDATE_HASH,
        "--expected-terminal-job-id", JOB_ID,
    ])

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "abandoned_after_crash_resume"
