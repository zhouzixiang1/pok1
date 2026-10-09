"""Crash-revival supervisor around ``run_evolution_task`` (P2, 2026-10-05).

The 06:12 v511 crash (``KeyError('bot_name(next_v)')``) left the one-shot
``run_evolution_task`` wrapper finished for ~2h while the service kept
burning saturator tokens, because nothing re-entered ``orchestrator_loop``
after its crash branch returned ``terminal_outcome == -1.0``.

Contract:

* Only a crash outcome (exactly ``-1.0``) re-enters the loop through the
  caller-provided ``restart_factory``. Normal terminals (operator stop,
  cost policy, manual pause, recovery blocked, LLM-availability stop — the
  ``ORCH_*_COST`` sentinels and ``0.0``) never restart.
* Bounded backoff: 30s initial, doubling per crash, capped at 30min.
* Sliding-window rate limit: at most 5 restarts per 30min window; beyond it
  a ``pipeline.orchestrator_auto_restart`` event with
  ``operator_action_required=true`` alarms app.log + the webui history and
  parks the supervisor in a slow-retry lane (one alarmed attempt per
  ``POK_ORCHESTRATOR_RESTART_SLOW_RETRY_SEC``, default 30min, still counted
  and window-guarded) instead of ending it (F-B, 2026-10-09).
* A stable run (>= 1h) before the crash resets the counter/backoff.
* Every retry emits ``pipeline.orchestrator_auto_restart`` with
  attempt/backoff_s/last_error/stage; cancellation and owner drift stop the
  supervisor cleanly with unchanged ownership cleanup.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import server.state as state_module
from server.state import app_state, run_evolution_task


@pytest.fixture(autouse=True)
def _quiesce_app_state():
    app_state.set_running(False)
    app_state._last_orchestrator_crash = None
    yield
    app_state.set_running(False)


@pytest.fixture
def restart_env(monkeypatch):
    """Small, fast supervisor knobs (production defaults are 30s/30min/5/30m/1h)."""

    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_INITIAL_BACKOFF_SEC", "0.001")
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_MAX_BACKOFF_SEC", "0.004")
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_MAX_BURST", "99")
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_WINDOW_SEC", "3600")
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_STABLE_RUN_SEC", "999999")
    return monkeypatch


def _run(coro):
    return asyncio.run(coro)


async def _drive(outcomes):
    """Run the supervisor over a factory producing the given outcomes."""

    calls = []
    owner_id = app_state.begin_runtime_owner()
    assert owner_id is not None

    def factory():
        calls.append(len(calls) + 1)

        async def body():
            return outcomes[len(calls) - 1] if len(calls) <= len(outcomes) else 0.0

        return body()

    task = asyncio.create_task(
        run_evolution_task(factory(), owner_id=owner_id, restart_factory=factory)
    )
    app_state.set_task(task, owner_id=owner_id)
    result = await task
    return result, calls


def test_crash_outcome_restarts_until_normal_terminal(restart_env):
    async def scenario():
        result, calls = await _drive([-1.0, -1.0, 7.5])
        assert result == 7.5
        assert calls == [1, 2, 3]
        assert app_state.to_dict()["running"] is False

    _run(scenario())


def test_normal_terminal_never_restarts(restart_env):
    from orchestrator import (
        ORCH_OPERATOR_ACTION_REQUIRED_COST,
    )

    async def scenario():
        for outcome in (
            0.0,
            ORCH_OPERATOR_ACTION_REQUIRED_COST,
            -99997.0,  # any other crash-family sentinel must differ from -1.0
        ):
            result, calls = await _drive([outcome])
            assert result == outcome
            assert calls == [1]

    _run(scenario())


def test_llm_availability_block_cost_restarts(restart_env):
    """P2 (2026-10-05): -99995.0 joins the restartable set (F9 double insurance).

    Previously this file asserted -99995.0 stays stopped; the F9 audit (four
    same-day silent exits, one 70min zero-flow) reclassified the
    waitable-pause sentinel as crash-restartable.  The primary fix is the
    bounded re-query loop in ``orchestrator_abandon_and_cost``; this keeps
    the supervisor as a second safety net when the loop leaks the sentinel
    anyway.
    """

    from orchestrator import ORCH_LLM_AVAILABILITY_BLOCKED_COST

    async def scenario():
        for outcome in (-99995.0, ORCH_LLM_AVAILABILITY_BLOCKED_COST):
            result, calls = await _drive([outcome, 7.5])
            assert result == 7.5
            assert calls == [1, 2]

    _run(scenario())


def test_backoff_doubles_per_crash_and_caps(restart_env):
    monkeypatch = restart_env
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_INITIAL_BACKOFF_SEC", "0.5")
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_MAX_BACKOFF_SEC", "2")

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(state_module, "_restart_backoff_sleep", fake_sleep)

    async def scenario():
        _, calls = await _drive([-1.0, -1.0, -1.0, -1.0, 0.0])
        assert calls == [1, 2, 3, 4, 5]
        assert sleeps == [0.5, 1.0, 2.0, 2.0]

    _run(scenario())


def test_sliding_window_rate_limit_alarms_then_slow_retries(restart_env, monkeypatch):
    """F-B (2026-10-09): the burst limit parks, it no longer terminates.

    Pre-FB this asserted ``result == -1.0`` with the supervisor finished;
    the 05:28:34 v532 stop then sat silently for 3.1h.  The branch now emits
    the same ``restart_rate_limited`` operator-action event AND recovers
    through the window-guarded slow-retry lane.
    """
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_MAX_BURST", "2")
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_WINDOW_SEC", "30")
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_SLOW_RETRY_SEC", "5")

    class _FakeClock:
        def __init__(self):
            self._now = 1000.0

        def monotonic(self):
            return self._now

        def time(self):
            return self._now

        def advance(self, seconds):
            self._now += seconds

    clock = _FakeClock()
    monkeypatch.setattr(state_module, "time", clock)

    async def fake_sleep(seconds):
        clock.advance(seconds)

    monkeypatch.setattr(state_module, "_restart_backoff_sleep", fake_sleep)

    events = []
    import system_log

    monkeypatch.setattr(
        system_log,
        "log_system_event",
        lambda event_type, severity, message, data=None: events.append(
            {"type": event_type, "severity": severity, "message": message, "data": data or {}}
        ),
    )

    async def scenario():
        result, calls = await _drive([-1.0, -1.0, -1.0, 0.0])
        # Two fast restarts were allowed; the third crash parks the
        # supervisor; the slow lane's guarded attempt then recovers.
        assert calls == [1, 2, 3, 4]
        assert result == 0.0
        assert app_state.to_dict()["running"] is False

    _run(scenario())
    restart_events = [e for e in events if e["type"] == "pipeline.orchestrator_auto_restart"]
    assert restart_events, events
    terminal = [e for e in restart_events if e["data"].get("status") == "restart_rate_limited"]
    assert terminal
    assert terminal[-1]["data"].get("operator_action_required") is True
    statuses = [e["data"].get("status") for e in restart_events]
    assert "slow_retry_scheduled" in statuses


def test_every_retry_emits_restart_event_with_stage(restart_env, monkeypatch, tmp_path):
    import evolution_infra

    state_file = tmp_path / "pipeline_state.json"
    state_file.write_text(
        json.dumps({"stage": "rework_running", "schema_version": 2}), encoding="utf-8"
    )
    # The primary checkpoint path is an import-time constant, so retarget the
    # constant itself (monkeypatching RESULTS_DIR alone has no effect here).
    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", state_file)

    events = []
    import system_log

    monkeypatch.setattr(
        system_log,
        "log_system_event",
        lambda event_type, severity, message, data=None: events.append(
            {"type": event_type, "data": data or {}}
        ),
    )

    async def scenario():
        await _drive([-1.0, 0.0])

    _run(scenario())
    retries = [
        e
        for e in events
        if e["type"] == "pipeline.orchestrator_auto_restart"
        and e["data"].get("status") != "restart_rate_limited"
    ]
    assert len(retries) == 1
    data = retries[0]["data"]
    assert data["attempt"] == 1
    assert "backoff_s" in data
    assert data["stage"] == "rework_running"
    assert "last_error" in data


def test_stable_run_resets_counters(restart_env):
    monkeypatch = restart_env
    # Every incarnation instantly counts as stable -> backoff never escalates.
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_STABLE_RUN_SEC", "0")
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_INITIAL_BACKOFF_SEC", "0.5")
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_MAX_BACKOFF_SEC", "8")

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(state_module, "_restart_backoff_sleep", fake_sleep)

    async def scenario():
        _, calls = await _drive([-1.0, -1.0, -1.0, -1.0, 0.0])
        assert calls == [1, 2, 3, 4, 5]
        assert sleeps == [0.5, 0.5, 0.5, 0.5]

    _run(scenario())


def test_cancel_during_backoff_stops_cleanly(restart_env, monkeypatch):
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_INITIAL_BACKOFF_SEC", "5")

    started = asyncio.Event()

    async def long_sleep(_seconds):
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(state_module, "_restart_backoff_sleep", long_sleep)

    async def scenario():
        def factory():
            async def body():
                return -1.0

            return body()

        owner_id = app_state.begin_runtime_owner()
        assert owner_id is not None
        task = asyncio.create_task(
            run_evolution_task(factory(), owner_id=owner_id, restart_factory=factory)
        )
        app_state.set_task(task, owner_id=owner_id)
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert app_state.to_dict()["running"] is False

    _run(scenario())


def test_owner_drift_stops_restart(restart_env):
    async def scenario():
        calls = []

        def factory():
            calls.append(len(calls) + 1)

            async def body():
                if len(calls) == 1:
                    # The owner fence changes while the crashed loop unwinds.
                    app_state.set_running(False)
                    app_state.begin_runtime_owner()
                    return -1.0
                return 0.0

            return body()

        owner_id = app_state.begin_runtime_owner()
        assert owner_id is not None
        result = await asyncio.wait_for(
            run_evolution_task(factory(), owner_id=owner_id, restart_factory=factory),
            timeout=5,
        )
        assert result == -1.0
        assert calls == [1]

    _run(scenario())
    app_state.set_running(False)


def test_owner_drift_does_not_emit_rate_limited_terminal_event(
    restart_env, monkeypatch
):
    """Owner drift is a fencing outcome, not a restart-storm stop (review).

    Pre-fix, the drift path shared the rate-limit branch and published a
    misleading ``status=restart_rate_limited`` / ``operator_action_required``
    terminal event even though zero restarts were attempted.
    """

    events = []
    import system_log

    monkeypatch.setattr(
        system_log,
        "log_system_event",
        lambda event_type, severity, message, data=None: events.append(
            {"type": event_type, "severity": severity, "data": data or {}}
        ),
    )

    async def scenario():
        calls = []

        def factory():
            calls.append(len(calls) + 1)

            async def body():
                if len(calls) == 1:
                    app_state.set_running(False)
                    app_state.begin_runtime_owner()
                    return -1.0
                return 0.0

            return body()

        owner_id = app_state.begin_runtime_owner()
        assert owner_id is not None
        result = await asyncio.wait_for(
            run_evolution_task(factory(), owner_id=owner_id, restart_factory=factory),
            timeout=5,
        )
        assert result == -1.0
        assert calls == [1]

    _run(scenario())
    app_state.set_running(False)
    restart_events = [
        e for e in events if e["type"] == "pipeline.orchestrator_auto_restart"
    ]
    assert not any(
        e["data"].get("status") == "restart_rate_limited" for e in restart_events
    )
    assert not any(
        e["data"].get("operator_action_required") for e in restart_events
    )
