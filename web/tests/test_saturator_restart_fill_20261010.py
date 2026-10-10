"""Bounded saturator fill while an orchestrator revival is scheduled (w3 ①).

The 10-09 05:41-08:49 wedge (3h08m zero LLM dispatch) was amplified by the
hard coupling between the saturator and pipeline-task aliveness: every
crash-backoff sleep has a stale heartbeat by construction, so the
background lane parked for the whole crash-loop even though the restart
supervisor had a revival scheduled the entire time.

Contract (2026-10-10):

* A stale heartbeat with NO scheduled revival still parks hard
  (``pipeline_not_alive``) — operator stop / supervisor gone stays parked.
* A stale heartbeat while ``app_state.note_orchestrator_restart_pending``
  has a future deadline allows a BOUNDED fill: at most
  ``POK_LLM_SATURATOR_RESTART_FILL_INFLIGHT`` (default 1) in-flight
  packets; excess is refused with ``restart_fill_soft_cap``. All other
  gates (permits/RAM/cgroup/quota pacing) still apply on the way through.
* Cap 0, a cleared marker, or a marker long past its deadline (a missed
  clear) restores the hard park.
* The crash supervisor actually publishes the marker across its backoff
  sleep (fast lane) and clears it before/after the revival; the supervisor
  exit clears it defensively.
"""

from __future__ import annotations

import asyncio
import inspect
import time

import pytest

import llm_saturator
import server.state as state_module
from llm_saturator import saturator_may_launch
from server.state import app_state, run_evolution_task


@pytest.fixture(autouse=True)
def _reset_liveness_and_marker(monkeypatch):
    monkeypatch.setattr(
        app_state, "_pipeline_heartbeat_monotonic", None, raising=False
    )
    app_state.clear_orchestrator_restart_pending()
    llm_saturator._pipeline_park_announced = False
    yield
    app_state.clear_orchestrator_restart_pending()
    app_state.set_running(False)
    llm_saturator._pipeline_park_announced = False


def _beat_stale(age_seconds: float = 700.0) -> None:
    app_state._pipeline_heartbeat_monotonic = time.monotonic() - age_seconds


def test_stale_heartbeat_without_revival_still_parks():
    _beat_stale()
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert (ok, reason) == (False, "pipeline_not_alive")


def test_scheduled_revival_allows_bounded_fill():
    _beat_stale()
    app_state.note_orchestrator_restart_pending(30.0)
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert ok is True
    assert reason == "ok"


def test_bounded_cap_limits_inflight_packets():
    _beat_stale()
    app_state.note_orchestrator_restart_pending(30.0)
    ok, reason = saturator_may_launch(in_flight=1, soft_cap=4)
    assert (ok, reason) == (False, "restart_fill_soft_cap")


def test_cap_zero_restores_hard_park(monkeypatch):
    monkeypatch.setenv("POK_LLM_SATURATOR_RESTART_FILL_INFLIGHT", "0")
    _beat_stale()
    app_state.note_orchestrator_restart_pending(30.0)
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert (ok, reason) == (False, "pipeline_not_alive")


def test_cleared_marker_parks_again():
    _beat_stale()
    app_state.note_orchestrator_restart_pending(30.0)
    app_state.clear_orchestrator_restart_pending()
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert (ok, reason) == (False, "pipeline_not_alive")


def test_marker_past_grace_parks():
    """A deadline that expired long ago means a clear was missed — park."""

    _beat_stale()
    # Fake a marker whose deadline passed an hour ago.
    app_state._orchestrator_restart_deadline_monotonic = time.monotonic() - 3600.0
    pending = app_state.orchestrator_restart_pending_seconds()
    assert pending is not None and pending < -600.0
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert (ok, reason) == (False, "pipeline_not_alive")


def test_marker_just_expired_still_fills():
    """Deadline passed seconds ago (revival starting) — still pending."""

    _beat_stale()
    app_state.note_orchestrator_restart_pending(0.0)
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=4)
    assert ok is True
    assert reason == "ok"


def test_fresh_heartbeat_never_clamps_to_restart_cap():
    """Alive pipeline: the restart-fill cap does not apply at all."""

    app_state.note_pipeline_heartbeat()
    app_state.note_orchestrator_restart_pending(30.0)
    ok, reason = saturator_may_launch(in_flight=3, soft_cap=8)
    assert ok is True
    assert reason == "ok"


def test_supervisor_publishes_marker_across_backoff_and_clears_on_revival(
    monkeypatch,
):
    """Functional wiring: the fast lane notes the marker for the whole
    backoff sleep and clears it before the revival runs; the wrapper clears
    it defensively at exit."""

    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_INITIAL_BACKOFF_SEC", "30")
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_MAX_BACKOFF_SEC", "60")
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_MAX_BURST", "99")
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_WINDOW_SEC", "3600")
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_STABLE_RUN_SEC", "999999")

    observed: dict = {}
    real_sleep = state_module._restart_backoff_sleep

    async def spy_sleep(seconds: float) -> None:
        observed["sleep_seconds"] = seconds
        observed["pending_at_sleep"] = (
            app_state.orchestrator_restart_pending_seconds()
        )
        await real_sleep(0.0)  # do not actually sleep

    monkeypatch.setattr(state_module, "_restart_backoff_sleep", spy_sleep)

    async def scenario():
        owner_id = app_state.begin_runtime_owner()
        assert owner_id is not None
        pending_during_factory: list = []

        def factory():
            pending_during_factory.append(
                app_state.orchestrator_restart_pending_seconds()
            )

            async def body():
                return 0.0  # healthy terminal on first revival

            return body()

        async def crash_once():
            return -1.0

        task = asyncio.create_task(
            run_evolution_task(
                crash_once(), owner_id=owner_id, restart_factory=factory
            )
        )
        app_state.set_task(task, owner_id=owner_id)
        result = await task
        assert result == 0.0
        return pending_during_factory

    pending_during_factory = asyncio.run(scenario())

    # The backoff sleep ran with a live marker covering the whole sleep.
    assert observed["sleep_seconds"] == 30.0
    assert observed["pending_at_sleep"] is not None
    assert 0.0 <= observed["pending_at_sleep"] <= 30.0
    # The marker was cleared before the revival factory ran.
    assert pending_during_factory == [None]
    # And nothing stays scheduled after the supervisor exited.
    assert app_state.orchestrator_restart_pending_seconds() is None


def test_supervisor_slow_lane_also_publishes_marker(monkeypatch):
    """The parked slow-retry lane (the 05:28 restart_rate_limited wedge)
    sleeps with a scheduled revival too — its sleep must carry the marker."""

    source = inspect.getsource(state_module._supervise_orchestrator_crash_revival)
    # One note call per sleep site: fast lane + slow lane.
    assert source.count("note_orchestrator_restart_pending(") == 2
    # ...and a clear before each revival plus the wrapper's finally clear.
    assert "clear_orchestrator_restart_pending()" in source
    wrapper_source = inspect.getsource(run_evolution_task)
    assert "clear_orchestrator_restart_pending()" in wrapper_source
