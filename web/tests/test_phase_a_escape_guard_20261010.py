"""Phase-A / restart-factory escape guard (2026-10-10 red-team residual).

Phase A (``orchestrator_loop_phases._loop_phase_a_setup``) had ~11 LOCAL
tries but no overall net, and both ``await restart_factory()`` sites in
``server.state`` ran bare. An exception escaping any of those points
bypassed phase B's ``-1.0`` crash sentinel entirely: no
``orchestrator.crashed`` marker, no crash note, no auto-restart, no alert —
the wrapper task died silently and the saturator hard-parked on the dead
pipeline.

Contract:

* An exception escaping the phase-A body is routed into the SAME handling
  path as phase B's crash branch: ``orchestrator.crashed`` event + crash
  note on app_state + orchestrator-session clear + running=False, and the
  ``-1.0`` crash sentinel return so the EXISTING crash-revival supervisor
  auto-restarts under its bounded backoff.
* Legitimate phase-A early exits (``None`` / ``5``) and the ``(ctx,)``
  continuation tuple pass through unchanged; ``CancelledError`` still
  propagates (operator stop is not a crash).
* ``run_evolution_task`` converts an escaped exception (initial coro or a
  ``restart_factory`` re-entry) into the crash path ONLY when supervised
  (``restart_factory`` present); unsupervised callers keep the historical
  raise. No exception is swallowed: each conversion crash-marks first.
"""

from __future__ import annotations

import asyncio

import pytest

import epoch_authority
import orchestrator
import orchestrator_loop_phases as olp
import server.state as state_module
import stability_observation
import tools
from orchestrator_watchdog import _watchdog_coroutine
from server.state import (
    _await_restart_factory_crash_guarded,
    app_state,
    run_evolution_task,
)


_CAPTURED: list = []


@pytest.fixture(autouse=True)
def _quiesce(monkeypatch):
    app_state.set_running(False)
    app_state._last_orchestrator_crash = None
    # Capture (not perform) the guard's side-effect seams.
    _CAPTURED.clear()
    monkeypatch.setattr(
        orchestrator,
        "log_system_event",
        lambda name, level, msg, data=None, **kw: _CAPTURED.append(
            (name, level, msg, data or {})
        ),
        raising=True,
    )
    monkeypatch.setattr(
        orchestrator,
        "_clear_orchestrator_session",
        lambda *a, **kw: _CAPTURED.append(("session_cleared", None, "", {})),
        raising=True,
    )
    yield
    app_state.set_running(False)
    app_state._last_orchestrator_crash = None


def _events():
    return _CAPTURED


def _run(coro):
    return asyncio.run(coro)


# --- phase-A wrapper ----------------------------------------------------------


def _phase_a(monkeypatch, body):
    monkeypatch.setattr(olp, "_loop_phase_a_setup_body", body)
    return olp._loop_phase_a_setup(
        ui=None,
        shutdown_mgr=None,
        no_daemon=True,
        daemon_workers=1,
        daemon_pairs=1,
        startup_recovery=None,
    )


def test_phase_a_escape_returns_crash_sentinel_and_marks_crash(monkeypatch):
    async def exploding_body(*args, **kwargs):
        raise RuntimeError("phase-A startup blew up")

    result = _run(_phase_a(monkeypatch, exploding_body))
    assert result == -1.0

    crashed = [e for e in _events() if e[0] == "orchestrator.crashed"]
    assert crashed, "the crash branch must emit orchestrator.crashed"
    assert "phase-A startup blew up" in crashed[0][2]
    assert crashed[0][3].get("phase") == "a_setup"
    assert any(e[0] == "session_cleared" for e in _events())
    note = app_state.last_orchestrator_crash() or {}
    assert "phase-A startup blew up" in str(note.get("error"))
    assert app_state.to_dict()["running"] is False


def test_phase_a_normal_returns_pass_through_unchanged(monkeypatch):
    ctx = {"log_file": "x"}

    async def ctx_body(*args, **kwargs):
        return (ctx,)

    async def none_body(*args, **kwargs):
        return None

    async def five_body(*args, **kwargs):
        return 5

    assert _run(_phase_a(monkeypatch, ctx_body)) == (ctx,)
    assert _run(_phase_a(monkeypatch, none_body)) is None
    assert _run(_phase_a(monkeypatch, five_body)) == 5
    assert not [e for e in _events() if e[0] == "orchestrator.crashed"]
    assert app_state.last_orchestrator_crash() is None


def test_phase_a_cancellation_still_propagates(monkeypatch):
    async def cancelled_body(*args, **kwargs):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        _run(_phase_a(monkeypatch, cancelled_body))
    assert not [e for e in _events() if e[0] == "orchestrator.crashed"]


# --- restart-factory guard -----------------------------------------------------


def test_await_restart_factory_crash_guarded_converts_exceptions():
    async def raising_factory():
        raise RuntimeError("revival exploded")

    async def good_factory():
        return 7.5

    assert _run(_await_restart_factory_crash_guarded(good_factory)) == 7.5
    app_state._last_orchestrator_crash = None
    result = _run(_await_restart_factory_crash_guarded(raising_factory))
    assert result == state_module._ORCHESTRATOR_CRASH_OUTCOME
    note = app_state.last_orchestrator_crash() or {}
    assert "revival exploded" in str(note.get("error"))


def test_await_restart_factory_crash_guarded_propagates_cancel():
    async def cancelled_factory():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        _run(_await_restart_factory_crash_guarded(cancelled_factory))


# --- end-to-end through run_evolution_task + supervisor ------------------------


@pytest.fixture
def restart_env(monkeypatch):
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_INITIAL_BACKOFF_SEC", "0.001")
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_MAX_BACKOFF_SEC", "0.004")
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_MAX_BURST", "99")
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_WINDOW_SEC", "3600")
    monkeypatch.setenv("POK_ORCHESTRATOR_RESTART_STABLE_RUN_SEC", "999999")
    return monkeypatch


async def _drive_supervised(initial_outcome_or_error, factory_behaviors):
    """Run run_evolution_task over an initial coro + scripted factory.

    ``factory_behaviors`` is a list whose entries are either a return value
    or an Exception instance to raise. Returns the wrapper task's result
    plus the number of factory invocations.
    """

    calls = {"factory": 0}
    owner_id = app_state.begin_runtime_owner()
    assert owner_id is not None

    async def initial():
        if isinstance(initial_outcome_or_error, BaseException):
            raise initial_outcome_or_error
        return initial_outcome_or_error

    def factory():
        index = calls["factory"]
        calls["factory"] += 1
        behavior = (
            factory_behaviors[index]
            if index < len(factory_behaviors)
            else 0.0
        )

        async def body():
            if isinstance(behavior, BaseException):
                raise behavior
            return behavior

        return body()

    task = asyncio.create_task(
        run_evolution_task(initial(), owner_id=owner_id, restart_factory=factory)
    )
    app_state.set_task(task, owner_id=owner_id)
    result = await task
    return result, calls["factory"]


def test_initial_coro_exception_is_revived_not_silent(restart_env):
    """Pre-fix: this task DIED with RuntimeError (no crash note, no restart)."""

    async def scenario():
        result, calls = await _drive_supervised(
            RuntimeError("phase-A escape"), [7.5]
        )
        assert result == 7.5
        assert calls == 1
        note = app_state.last_orchestrator_crash() or {}
        assert "phase-A escape" in str(note.get("error"))
        assert app_state.to_dict()["running"] is False

    _run(scenario())


def test_raising_restart_factory_stays_in_the_bounded_loop(restart_env):
    async def scenario():
        result, calls = await _drive_supervised(
            -1.0,
            [RuntimeError("revival exploded"), 3.25],
        )
        assert result == 3.25
        assert calls == 2
        note = app_state.last_orchestrator_crash() or {}
        assert "revival exploded" in str(note.get("error"))

    _run(scenario())


def test_unsupervised_caller_keeps_the_historical_raise():
    async def scenario():
        async def boom():
            raise RuntimeError("unsupervised escape")

        task = asyncio.create_task(run_evolution_task(boom()))
        with pytest.raises(RuntimeError, match="unsupervised escape"):
            await task

    _run(scenario())


# --- 2026-10-10 red-team issue 2: SystemExit is a crash, never an escape ------
#
# SystemExit previously sailed through every ``except Exception`` net with
# zero crash events. It must now take the same crash path as any Exception;
# KeyboardInterrupt must keep propagating (operator interactive stop).


def test_phase_a_system_exit_is_classified_as_crash(monkeypatch):
    async def exiting_body(*args, **kwargs):
        raise SystemExit(9)

    result = _run(_phase_a(monkeypatch, exiting_body))
    assert result == -1.0

    crashed = [e for e in _events() if e[0] == "orchestrator.crashed"]
    assert crashed, "SystemExit must crash-mark, not escape"
    assert crashed[0][3].get("phase") == "a_setup"
    note = app_state.last_orchestrator_crash() or {}
    assert "9" in str(note.get("error"))


def test_phase_a_keyboard_interrupt_still_propagates(monkeypatch):
    async def interrupted_body(*args, **kwargs):
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        _run(_phase_a(monkeypatch, interrupted_body))
    assert not [e for e in _events() if e[0] == "orchestrator.crashed"]
    assert app_state.last_orchestrator_crash() is None


def test_await_restart_factory_crash_guarded_converts_system_exit():
    async def exiting_factory():
        raise SystemExit(3)

    result = _run(_await_restart_factory_crash_guarded(exiting_factory))
    assert result == state_module._ORCHESTRATOR_CRASH_OUTCOME
    note = app_state.last_orchestrator_crash() or {}
    assert "3" in str(note.get("error"))


def test_supervised_system_exit_is_revived_not_propagated(restart_env):
    async def scenario():
        result, calls = await _drive_supervised(SystemExit(9), [7.5])
        assert result == 7.5
        assert calls == 1
        note = app_state.last_orchestrator_crash() or {}
        assert "9" in str(note.get("error"))

    _run(scenario())


# --- 2026-10-10 red-team issue 1: crash-path resource reclamation -------------
#
# A phase-A crash AFTER background-task creation / daemon start used to leak
# the branch-guard + stability + watchdog tasks, leave the daemon monitor
# thread polling, keep the daemon subprocess alive, and let the leaked
# watchdog keep heartbeating the saturator's liveness gate. The body now
# reclaims everything phase A owns (finally-style) before the ``-1.0``
# sentinel reaches the revival supervisor.


def test_phase_a_crash_reclaims_background_tasks_and_daemon(
    monkeypatch, tmp_path
):
    import evolution_core

    orch = orchestrator
    cancelled = []
    daemon = {"starts": 0, "stops": 0}
    stop_events = []

    async def bg_coro():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    # Phase-A startup seams (mirror the red-team script, hermetic).
    monkeypatch.setattr(orch, "LOGS_DIR", tmp_path)
    monkeypatch.setattr(orch, "_rotate_orchestrator_logs", lambda *a, **kw: None)
    monkeypatch.setattr(
        orch, "_runtime_git_identity", lambda *a, **kw: {"branch": "b", "head": "h"}
    )
    monkeypatch.setattr(orch, "_set_runtime_expected_head", lambda *a, **kw: "")
    monkeypatch.setattr(orch, "register_eval_wait_draft_hook", lambda *a, **kw: None)
    monkeypatch.setattr(orch, "_runtime_branch_guard_enabled", lambda *a, **kw: True)
    monkeypatch.setattr(
        orch, "_runtime_branch_guard_coroutine", lambda *a, **kw: bg_coro()
    )
    monkeypatch.setattr(
        orch,
        "_stability_projection_maintenance_coroutine",
        lambda *a, **kw: bg_coro(),
    )
    monkeypatch.setattr(orch, "_watchdog_coroutine", lambda *a, **kw: bg_coro())
    monkeypatch.setattr(orch, "_resolve_daemon_workers", lambda w=None: 2)

    class _Policy:
        def receipt(self):
            return {}

    monkeypatch.setattr(orch, "load_operator_generation_cost_policy", lambda *a, **kw: None)
    monkeypatch.setattr(orch, "configure_runtime_cost_policy", lambda *a, **kw: _Policy())
    monkeypatch.setattr(orch, "load_llm_pause", lambda *a, **kw: None)
    monkeypatch.setattr(orch, "consume_operator_resume_ack_from_env", lambda *a, **kw: None)
    monkeypatch.setattr(orch, "_startup_recovery", lambda *a, **kw: None)
    monkeypatch.setattr(orch, "_startup_recovery_terminal_cost", lambda r: None)
    monkeypatch.setattr(orch, "run_blocking_isolated", lambda fn, *a, **kw: fn())
    monkeypatch.setattr(
        epoch_authority, "require_policy_epoch_initialized", lambda *a, **kw: None
    )
    monkeypatch.setattr(
        stability_observation, "bind_runtime_configuration", lambda *a, **kw: None
    )
    monkeypatch.setattr(tools, "inject_ui", lambda *a, **kw: None)
    monkeypatch.setattr(orch, "set_system_log_ui", lambda *a, **kw: None)

    def fake_start_daemon(workers=None, pairs=5):
        daemon["starts"] += 1

    def fake_stop_daemon():
        daemon["stops"] += 1

    def fake_monitor(ui, stop_event, workers, pairs):
        stop_events.append(stop_event)

    monkeypatch.setattr(evolution_core, "start_daemon", fake_start_daemon)
    monkeypatch.setattr(evolution_core, "stop_daemon", fake_stop_daemon)
    monkeypatch.setattr(evolution_core, "daemon_monitor_thread", fake_monitor)

    created = []
    real_create_task = asyncio.create_task

    def exploding_create_task(coro, **kw):
        task = real_create_task(coro, **kw)
        created.append(task)
        if len(created) == 3:  # the watchdog spawn itself explodes
            raise RuntimeError("create_task exploded on 3rd background task")
        return task

    monkeypatch.setattr(asyncio, "create_task", exploding_create_task)

    async def main():
        result = await olp._loop_phase_a_setup(
            ui=None,
            shutdown_mgr=None,
            no_daemon=False,
            daemon_workers=1,
            daemon_pairs=1,
            startup_recovery=orchestrator._STARTUP_RECOVERY_UNSET,
        )
        await asyncio.sleep(0.05)  # let cancellations settle
        return result

    try:
        result = _run(main())
    finally:
        monkeypatch.undo()

    assert result == -1.0
    crashed = [e for e in _events() if e[0] == "orchestrator.crashed"]
    assert crashed and crashed[0][3].get("phase") == "a_setup"
    # All three background tasks (branch guard, stability, watchdog) are
    # reclaimed — no PENDING-alive leaks past the crash.
    assert len(created) == 3
    assert all(task.done() for task in created), [
        t.cancelled() for t in created
    ]
    # Daemon teardown actually ran: stop_daemon called, monitor stop event set.
    assert daemon["starts"] == 1
    assert daemon["stops"] >= 1
    assert stop_events and all(event.is_set() for event in stop_events)


# --- 2026-10-10 red-team issue 1: watchdog heartbeat bound to runtime state ---
#
# The watchdog tick used to heartbeat unconditionally; a leaked tick kept a
# dead pipeline looking alive to the saturator. The beat is now gated on the
# owning loop task being alive AND the runtime running flag.


def test_watchdog_heartbeat_bound_to_running_state(monkeypatch):
    beats = {"n": 0}

    def fake_note():
        beats["n"] += 1

    monkeypatch.setattr(app_state, "note_pipeline_heartbeat", fake_note)
    monkeypatch.setattr(
        orchestrator, "_orchestrator_provider_stream_active", False
    )

    async def scenario():
        owner = app_state.begin_runtime_owner()
        assert owner is not None
        app_state.set_task(asyncio.current_task(), owner_id=owner)
        watchdog = asyncio.ensure_future(
            _watchdog_coroutine(None, None, check_interval=0.01)
        )
        registered = {"task": asyncio.current_task()}
        try:
            # Running + live registered loop task -> beats flow.
            app_state.running = True
            await asyncio.sleep(0.15)
            alive_beats = beats["n"]
            assert alive_beats >= 2

            # Runtime running flag cleared (crash guard / backoff window)
            # -> no more beats.
            app_state.running = False
            await asyncio.sleep(0.15)
            assert beats["n"] == alive_beats

            # Flag re-armed but the registered loop task is DONE (the loop
            # task died) -> still no beats.
            app_state.clear_task_if(
                registered["task"], owner_id=owner
            )
            finished = asyncio.ensure_future(asyncio.sleep(0))
            await finished
            app_state.set_task(finished, owner_id=owner)
            registered["task"] = finished
            app_state.running = True
            await asyncio.sleep(0.15)
            assert beats["n"] == alive_beats
        finally:
            app_state.running = False
            app_state.clear_task_if(registered["task"], owner_id=owner)
            watchdog.cancel()
            try:
                await watchdog
            except asyncio.CancelledError:
                pass

    _run(scenario())
