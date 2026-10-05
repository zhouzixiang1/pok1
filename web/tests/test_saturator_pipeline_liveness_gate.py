"""Saturator pipeline-liveness gate (P7, 2026-10-05).

The 06:12 orchestrator crash left the pipeline dead for ~2h while the
background saturator kept burning permits (B window 180 calls / 183.3M
tokens, C window 103 calls / 93.7M tokens — all SATURATOR).
``saturator_may_launch`` had six gates and none of them looked at whether
the pipeline was alive.

Contract:

* A pipeline-liveness heartbeat (``app_state.note_pipeline_heartbeat``,
  updated once per orchestrator cycle and once per watchdog tick) older than
  ``POK_LLM_SATURATOR_PIPELINE_LIVENESS_SEC`` (default 600s) parks new
  saturator launches with reason ``pipeline_not_alive``.
* A fresh (or never-recorded) heartbeat leaves launches unchanged
  (fail-open: a fresh web process without an orchestrator must not
  permanently park, but a dead pipeline must).
* The park emits ``pipeline.saturator_parked_no_pipeline`` exactly once per
  park episode (not per packet/tick) and re-arms when the pipeline returns.
"""

from __future__ import annotations

import inspect
import time

import pytest

import llm_saturator
from llm_saturator import saturator_may_launch
from server.state import app_state


@pytest.fixture(autouse=True)
def _reset_heartbeat_and_park(monkeypatch):
    monkeypatch.setattr(
        app_state, "_pipeline_heartbeat_monotonic", None, raising=False
    )
    llm_saturator._pipeline_park_announced = False
    yield
    llm_saturator._pipeline_park_announced = False


def _beat(age_seconds: float) -> None:
    app_state._pipeline_heartbeat_monotonic = time.monotonic() - age_seconds


def test_stale_heartbeat_parks_launch():
    _beat(700.0)
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert (ok, reason) == (False, "pipeline_not_alive")


def test_fresh_heartbeat_keeps_launch_path():
    _beat(5.0)
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert ok is True
    assert reason == "ok"


def test_no_heartbeat_fails_open():
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert ok is True
    assert reason == "ok"


def test_threshold_env_controls_the_boundary(monkeypatch):
    monkeypatch.setenv("POK_LLM_SATURATOR_PIPELINE_LIVENESS_SEC", "60")
    _beat(59.0)
    ok, _ = saturator_may_launch(in_flight=0, soft_cap=4)
    assert ok is True
    _beat(61.0)
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert (ok, reason) == (False, "pipeline_not_alive")


def test_liveness_gate_outranks_permit_accounting(monkeypatch):
    """A dead pipeline parks even with free permits/RAM (the wedge case)."""

    _beat(5000.0)
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=8)
    assert (ok, reason) == (False, "pipeline_not_alive")


def test_park_event_emitted_once_per_park_episode(monkeypatch):
    events = []
    import system_log

    monkeypatch.setattr(
        system_log,
        "log_system_event",
        lambda event_type, severity, message, data=None: events.append(
            {"type": event_type, "severity": severity, "data": data or {}}
        ),
    )

    llm_saturator._maybe_announce_saturator_pipeline_park(True, "heartbeat_stale")
    llm_saturator._maybe_announce_saturator_pipeline_park(True, "heartbeat_stale")
    llm_saturator._maybe_announce_saturator_pipeline_park(True, "heartbeat_stale")
    park_events = [
        e for e in events if e["type"] == "pipeline.saturator_parked_no_pipeline"
    ]
    assert len(park_events) == 1

    # Pipeline returns: the flag re-arms without emitting anything.
    llm_saturator._maybe_announce_saturator_pipeline_park(False)
    assert len(events) == 1

    # A later park is a NEW episode and announces again.
    llm_saturator._maybe_announce_saturator_pipeline_park(True, "heartbeat_stale")
    park_events = [
        e for e in events if e["type"] == "pipeline.saturator_parked_no_pipeline"
    ]
    assert len(park_events) == 2


def test_saturator_loop_wires_the_park_gate():
    import inspect

    source = inspect.getsource(llm_saturator.run_llm_saturator)
    assert "_maybe_announce_saturator_pipeline_park(" in source
    assert "pipeline_not_alive" in source


def test_orchestrator_loop_beats_the_heartbeat():
    """The heartbeat producer lives in the orchestrator cycle loop."""

    import orchestrator_loop_phases as loop_phases
    import orchestrator_watchdog

    cycle_source = inspect.getsource(loop_phases._loop_phase_b_generation_loop)
    watchdog_source = inspect.getsource(
        orchestrator_watchdog._watchdog_coroutine
    )
    assert "note_pipeline_heartbeat" in cycle_source
    assert "note_pipeline_heartbeat" in watchdog_source
