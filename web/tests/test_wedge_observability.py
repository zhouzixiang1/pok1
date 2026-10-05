"""Wedge observability: typed stop events + health last-stop reason (P6).

The 10:29 v511 wedge (``WORKER_AVAILABILITY_RESUME_RECEIPT_INVALID``) stopped
evolution for hours with the typed reason living only inside a JSON tool
result (tool_planning_worker_phases.py:368-389) — no
``pipeline.evolution_stopped_llm_availability_control`` event existed
(events.jsonl had ZERO hits for the code; the loop's
``LLMAvailabilityPauseError`` branch at orchestrator_loop_phases.py:1610-1616
emitted no log_system_event, unlike the crash branch's ``orchestrator.crashed``),
and ``/api/control/health`` only said ``evolution_not_running`` — operators had
to dig through journalctl to reconstruct why.

Contract:

* ``_raise_for_llm_availability_tool_result`` attaches the six-code
  ``reason_code`` and the payload's ``receipt_errors`` onto the raised
  ``LLMAvailabilityPauseError``.
* The loop's availability-control stop emits
  ``pipeline.evolution_stopped_llm_availability_control`` (error) carrying
  reason_code / receipt_errors / stage / workflow_run_id /
  ``operator_action_required=true``.
* ``/api/control/health`` issues, when ``evolution_not_running``, carry the
  most recent stop reason (reason code + timestamp) from the structured-event
  tail, plus a typed ``last_evolution_stop`` field.
"""

from __future__ import annotations

import json

import pytest

import orchestrator
import orchestrator_loop_phases as loop_phases
from llm_availability_store import LLMAvailabilityPauseError


_SIX_CONTROL_CODES = (
    "LLM_AVAILABILITY_STATE_INVALID",
    "LLM_AVAILABILITY_PAUSE_WAS_NOT_PERSISTED",
    "WORKER_AVAILABILITY_DEFER_FAILED",
    "WORKER_AVAILABILITY_RESUME_FAILED",
    "WORKER_AVAILABILITY_RESUME_INVARIANT_FAILED",
    "WORKER_AVAILABILITY_RESUME_RECEIPT_INVALID",
)


def test_six_control_codes_are_the_canonical_set():
    assert set(_SIX_CONTROL_CODES) == set(orchestrator._LLM_AVAILABILITY_CONTROL_ERRORS)


def test_availability_raise_attaches_reason_code_and_receipt_errors():
    payload = {
        "error": "WORKER_AVAILABILITY_RESUME_RECEIPT_INVALID",
        "receipt_errors": [
            "global_pause_resume_receipt_evidence_digest_mismatch",
            "global_pause_resume_receipt_no_archived_match",
        ],
    }
    with pytest.raises(LLMAvailabilityPauseError) as raised:
        orchestrator._raise_for_llm_availability_tool_result(payload)
    assert raised.value.reason_code == "WORKER_AVAILABILITY_RESUME_RECEIPT_INVALID"
    assert raised.value.receipt_errors == [
        "global_pause_resume_receipt_evidence_digest_mismatch",
        "global_pause_resume_receipt_no_archived_match",
    ]


def test_availability_raise_without_receipt_errors_defaults_empty():
    with pytest.raises(LLMAvailabilityPauseError) as raised:
        orchestrator._raise_for_llm_availability_tool_result(
            {"error": "LLM_AVAILABILITY_PAUSE_WAS_NOT_PERSISTED"}
        )
    assert raised.value.reason_code == "LLM_AVAILABILITY_PAUSE_WAS_NOT_PERSISTED"
    assert raised.value.receipt_errors == []


def test_stop_event_carries_reason_receipt_errors_and_stage(monkeypatch):
    events = []
    monkeypatch.setattr(
        orchestrator,
        "log_system_event",
        lambda event_type, severity, message, data=None: events.append(
            {"type": event_type, "severity": severity, "data": data or {}}
        ),
    )
    import evolution_core

    monkeypatch.setattr(
        evolution_core,
        "read_pipeline_checkpoint",
        lambda: {
            "stage": "rework_running",
            "workflow_run_id": "generation:511:workflow-v1",
            "next_v": 511,
            "source_v": 510,
        },
    )

    exc = LLMAvailabilityPauseError(
        "Worker LLM availability control failed closed: "
        "WORKER_AVAILABILITY_RESUME_RECEIPT_INVALID"
    )
    exc.reason_code = "WORKER_AVAILABILITY_RESUME_RECEIPT_INVALID"
    exc.receipt_errors = ["global_pause_resume_receipt_evidence_digest_mismatch"]

    loop_phases._emit_llm_availability_control_stop_event(exc)

    assert len(events) == 1
    event = events[0]
    assert event["type"] == "pipeline.evolution_stopped_llm_availability_control"
    assert event["severity"] == "error"
    assert event["data"]["reason_code"] == "WORKER_AVAILABILITY_RESUME_RECEIPT_INVALID"
    assert event["data"]["receipt_errors"] == [
        "global_pause_resume_receipt_evidence_digest_mismatch"
    ]
    assert event["data"]["stage"] == "rework_running"
    assert event["data"]["workflow_run_id"] == "generation:511:workflow-v1"
    assert event["data"]["operator_action_required"] is True


def test_stop_event_derives_reason_code_from_message(monkeypatch):
    events = []
    monkeypatch.setattr(
        orchestrator,
        "log_system_event",
        lambda event_type, severity, message, data=None: events.append(
            {"type": event_type, "data": data or {}}
        ),
    )
    import evolution_core

    monkeypatch.setattr(
        evolution_core, "read_pipeline_checkpoint", lambda: None
    )

    # A control error raised without the structured attribute still names its
    # six-code reason in the message.
    exc = LLMAvailabilityPauseError(
        "Worker LLM availability control failed closed: "
        "WORKER_AVAILABILITY_DEFER_FAILED"
    )
    loop_phases._emit_llm_availability_control_stop_event(exc)
    assert events[0]["data"]["reason_code"] == "WORKER_AVAILABILITY_DEFER_FAILED"
    assert events[0]["data"]["receipt_errors"] == []
    assert events[0]["data"]["operator_action_required"] is True


def test_loop_availability_branch_calls_the_stop_event(monkeypatch):
    import inspect

    source = inspect.getsource(loop_phases._loop_phase_b_generation_loop)
    assert "_emit_llm_availability_control_stop_event(" in source


def _events_line(event_type: str, ts: float, **data):
    return json.dumps({
        "ts": ts,
        "type": event_type,
        "severity": "error",
        "message": "m",
        "data": data,
    })


def test_health_issues_carry_last_stop_reason(monkeypatch):
    from server.routes import control

    import orchestrator_stage_routing

    lines = [
        _events_line("pipeline.some_other_event", 1.0),
        _events_line("orchestrator.crashed", 2.0, error="KeyError: bot_name"),
        _events_line(
            "pipeline.evolution_stopped_llm_availability_control",
            3.0,
            reason_code="WORKER_AVAILABILITY_RESUME_RECEIPT_INVALID",
            receipt_errors=["x"],
            stage="rework_running",
            operator_action_required=True,
        ),
    ]
    monkeypatch.setattr(
        orchestrator_stage_routing,
        "_read_structured_events_tail",
        lambda *_a, **_k: lines,
    )

    status = {
        "running": False,
        "daemon_enabled": False,
        "epoch_initialized": True,
    }
    summary = control._health_summary(status)
    assert "evolution_not_running" in summary["issues"]
    last_stop = summary["last_evolution_stop"]
    assert last_stop["reason_code"] == "WORKER_AVAILABILITY_RESUME_RECEIPT_INVALID"
    assert last_stop["ts"] == 3.0
    assert last_stop["stage"] == "rework_running"
    assert any(
        "WORKER_AVAILABILITY_RESUME_RECEIPT_INVALID" in issue
        for issue in summary["issues"]
    )


def test_health_last_stop_absent_without_stop_events(monkeypatch):
    from server.routes import control

    import orchestrator_stage_routing

    monkeypatch.setattr(
        orchestrator_stage_routing,
        "_read_structured_events_tail",
        lambda *_a, **_k: [_events_line("pipeline.noise", 1.0)],
    )
    status = {"running": False, "daemon_enabled": False, "epoch_initialized": True}
    summary = control._health_summary(status)
    assert "evolution_not_running" in summary["issues"]
    assert summary["last_evolution_stop"] is None
    assert not any("last_stop" in issue for issue in summary["issues"])


def test_health_running_does_not_report_stop_reason(monkeypatch):
    from server.routes import control

    import orchestrator_stage_routing

    monkeypatch.setattr(
        orchestrator_stage_routing,
        "_read_structured_events_tail",
        lambda *_a, **_k: [
            _events_line("orchestrator.crashed", 5.0, error="old crash")
        ],
    )
    status = {"running": True, "daemon_enabled": False, "epoch_initialized": True}
    summary = control._health_summary(status)
    assert "evolution_not_running" not in summary["issues"]
    assert summary.get("last_evolution_stop", "absent") in (None, "absent")
