"""T1 (2026-10-15): quality-event payload dedup in the events.jsonl writer.

``run_quality_gates`` persisted every ``pipeline.quality_passed`` /
``pipeline.quality_failed`` / ``pipeline.quality_infra_*`` event with the
full ``scorecard`` — whose gate artifacts already embed the national
capability contract, architecture transition, national acceptance report,
and smoke report — PLUS the same payloads expanded at the top of
``quality_detail``; ``smoke`` and ``native_tcp_smoke`` were byte-identical
in native TCP mode (both sourced from ``smoke_payload``).  Measured on the
live ledger this near double-write made each row ~1.7 MB.

These tests pin the writer contract:

  - new rows are slim: one authoritative copy per payload (the scorecard
    gate artifacts plus the top-level ``smoke`` field), no duplicated
    top-level blobs;
  - the dedup helper is a pure copy — ``quality_detail`` still feeds the
    full-shape checkpoint gate payload (``_gate_payload``) afterwards;
  - the ``/api/logs/system-events`` reader returns and parses BOTH legacy
    fat rows and new slim rows unchanged (zero reader-side dependency on
    the removed copies; old events are never rewritten).
"""

import json

import pytest

pytestmark = pytest.mark.usefixtures("synthetic_checkpoint_authority")

import event_bus
import tool_gates
from pipeline_schema import GateResult, ScoreCard
from system_log import log_system_event


def _filler(prefix: str, count: int = 400) -> list[str]:
    return [f"{prefix} evidence line {i}: detail bytes" for i in range(count)]


def _capability_contract() -> dict:
    return {
        "detector_version": "national_capability_detector_v9",
        "required_failures": [],
        "advisory_warnings": [],
        "dynamic_runtime_probe": {
            "managed_isolation_digest": "a" * 64,
            "repeatability": _filler("probe"),
        },
        "static_checks": _filler("static"),
    }


def _architecture_transition() -> dict:
    return {
        "policy_version": "runtime_architecture_policy_v7",
        "regressions": [],
        "unresolved_focus_checks": [],
        "selected_dynamic_checks": _filler("dynamic"),
        "frozen_capabilities": _filler("capability"),
    }


def _acceptance_report() -> dict:
    return {
        "executed": True,
        "skipped": False,
        "passed": True,
        "conclusive": True,
        "summary": {"expected_hands": 70, "observed_hands": [70, 70]},
        "hand_reports": _filler("hand"),
    }


def _smoke_report() -> dict:
    return {
        "passed": True,
        "skipped": False,
        "execution_mode": "native_tcp",
        "hands": 4,
        "hand_details": _filler("smoke"),
    }


def _scorecard() -> dict:
    """ScoreCard dump mirroring _build_quality_scorecard's artifact wiring."""

    card = ScoreCard(name="quality")
    card.add(GateResult.from_bool(
        "national_capability_contract",
        True,
        artifacts={
            "contract": _capability_contract(),
            "transition": _architecture_transition(),
        },
    ))
    card.add(GateResult(
        name="national_acceptance",
        status="passed",
        artifacts={"report": _acceptance_report()},
    ))
    card.add(GateResult.from_bool(
        "smoke",
        True,
        artifacts={"report": _smoke_report()},
    ))
    card.add(GateResult.from_bool("compile", True))
    return card.model_dump()


def _legacy_quality_event_data() -> dict:
    """The pre-T1 full shape: duplicated top-level blobs + full scorecard."""

    return {
        "version": 601,
        "pass_rate": 1.0,
        "all_passed": True,
        "smoke_ok": True,
        "smoke_errors": [],
        "smoke": _smoke_report(),
        "native_tcp_smoke_ok": True,
        # byte-identical to "smoke" in native TCP mode
        "native_tcp_smoke": _smoke_report(),
        "national_capability_contract_ok": True,
        "national_capability_contract": _capability_contract(),
        "national_architecture_transition": _architecture_transition(),
        "national_acceptance_ok": True,
        "national_acceptance": _acceptance_report(),
        "failed_gates": [],
        "scorecard": _scorecard(),
    }


def _read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ── writer: new rows are slim ────────────────────────────────────────────────


def test_new_quality_event_rows_carry_one_authoritative_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(event_bus, "EVENTS_FILE", tmp_path / "events.jsonl")
    event_bus.reset_for_test()
    try:
        legacy = _legacy_quality_event_data()
        slim = tool_gates._quality_event_payload(legacy)
        log_system_event(
            "pipeline.quality_passed",
            "success",
            "Quality gates passed for v601: all checks passed",
            slim,
        )
    finally:
        event_bus.reset_for_test()

    rows = _read_jsonl(tmp_path / "events.jsonl")
    assert len(rows) == 1 and rows[0]["type"] == "pipeline.quality_passed"
    data = rows[0]["data"]

    # The duplicated top-level copies are gone from the persisted row.
    for key in tool_gates._QUALITY_EVENT_DEDUPED_FIELDS:
        assert key not in data, f"duplicated payload {key!r} still emitted"

    # One authoritative copy of each payload survives…
    assert data["smoke"]["passed"] is True
    gates = {gate["name"]: gate for gate in data["scorecard"]["gates"]}
    capability_artifacts = gates["national_capability_contract"]["artifacts"]
    assert (
        capability_artifacts["contract"]["detector_version"]
        == "national_capability_detector_v9"
    )
    assert (
        capability_artifacts["transition"]["policy_version"]
        == "runtime_architecture_policy_v7"
    )
    assert gates["national_acceptance"]["artifacts"]["report"]["conclusive"] is True
    assert gates["smoke"]["artifacts"]["report"] == data["smoke"]

    # …together with every sibling scalar/diagnostic field.
    for key in (
        "version",
        "pass_rate",
        "all_passed",
        "failed_gates",
        "smoke_ok",
        "smoke_errors",
        "native_tcp_smoke_ok",
        "national_capability_contract_ok",
        "national_acceptance_ok",
        "scorecard",
    ):
        assert key in data, f"non-duplicated field {key!r} dropped"

    # Materially smaller than the legacy shape with identical content.
    slim_bytes = len(json.dumps(slim).encode("utf-8"))
    legacy_bytes = len(json.dumps(legacy).encode("utf-8"))
    assert slim_bytes < 0.6 * legacy_bytes


def test_dedup_helper_is_a_pure_copy():
    legacy = _legacy_quality_event_data()
    snapshot = json.dumps(legacy, sort_keys=True)

    slim = tool_gates._quality_event_payload(legacy)

    # Input untouched: quality_detail still feeds the full-shape checkpoint
    # gate payload via _gate_payload after the event is emitted.
    assert json.dumps(legacy, sort_keys=True) == snapshot
    assert slim is not legacy
    assert set(slim) == set(legacy) - set(tool_gates._QUALITY_EVENT_DEDUPED_FIELDS)
    for key, value in slim.items():
        assert value == legacy[key]

    # A payload without duplicates passes through unchanged.
    clean = {"version": 1, "scorecard": _scorecard(), "smoke": _smoke_report()}
    assert tool_gates._quality_event_payload(clean) == clean


# ── reader: legacy fat rows and new slim rows both parse ────────────────────


def test_system_events_reader_tolerates_legacy_and_slim_rows(client, tmp_path, monkeypatch):
    from server.routes import logs

    identity = {
        "evaluation_epoch": "national_tcp_policy_v1",
        "epoch_reset_receipt_digest": "a" * 64,
    }
    legacy_row = {
        "ts": 1.0,
        "type": "pipeline.quality_passed",
        "severity": "success",
        "message": "legacy fat row",
        "data": {
            **identity,
            **_legacy_quality_event_data(),
            "category": "pipeline.quality_passed",
            "run_id": "601#0",
            "stage": "quality_passed",
        },
    }
    slim_row = {
        "ts": 2.0,
        "type": "pipeline.quality_failed",
        "severity": "error",
        "message": "new slim row",
        "data": {
            **identity,
            **tool_gates._quality_event_payload(_legacy_quality_event_data()),
            "category": "pipeline.quality_failed",
            "run_id": "602#0",
            "stage": "quality_failed",
        },
    }
    (tmp_path / "events.jsonl").write_text(
        json.dumps(legacy_row) + "\n" + json.dumps(slim_row) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(logs, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(logs, "_current_event_epoch_identity", lambda: identity)

    response = client.get("/api/logs/system-events?source=structured&type=pipeline.quality_")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    by_type = {event["type"]: event for event in body["events"]}

    # Old events are never rewritten: the legacy full shape round-trips intact.
    legacy_data = by_type["pipeline.quality_passed"]["data"]
    for key in (
        "smoke",
        "native_tcp_smoke",
        "national_architecture_transition",
        "national_acceptance",
        "national_capability_contract",
        "scorecard",
    ):
        assert key in legacy_data, f"legacy row lost {key!r}"

    # The slim shape round-trips equally well: the reader never touches the
    # deduped copies.
    slim_data = by_type["pipeline.quality_failed"]["data"]
    for key in tool_gates._QUALITY_EVENT_DEDUPED_FIELDS:
        assert key not in slim_data
    assert "scorecard" in slim_data
    assert slim_data["smoke"]["passed"] is True
