"""P6 (2026-10-05): graceful stop — daemon SIGTERM window + LLM-stream drain.

F11 evidence: 20:35:46 the stop path SIGKILLed the rating daemon after an
8s window ("did not exit gracefully in 8s — force killing") while 20:36-20:38
three saturator jobs kept completing after the HTTP 200 return — ~3 minutes /
~4M tokens of orphaned provider streams; 19:39:00 six daemon/bwrap/python3.12
processes escaped unit cleanup and shared the cgroup with the new service.

Contracts under test:

* ``stop_daemon`` grants the daemon a >= 30s SIGTERM window (env-tunable)
  before the SIGKILL backstop, on both the in-memory-handle path and the
  PID-file orphan path (``_terminate_verified_daemon_record``).
* ``_stop_evolution_transaction`` drains in-flight provider streams
  (``llm_concurrency.get_active_stream_count``) with a bounded timeout
  (default 120s) before returning, and reports (not raises) on timeout.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest


@pytest.fixture
def isolated_daemon_state(monkeypatch, tmp_path):
    import daemon_management as dm

    monkeypatch.setattr(dm, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(dm, "log_system_event", lambda *a, **k: None)
    monkeypatch.setattr(dm, "_daemon_shutting_down", False)
    with dm._daemon_lock:
        dm.daemon_proc = None
    yield dm
    with dm._daemon_lock:
        dm.daemon_proc = None


class _FakeDaemonProc:
    """Popen-shaped stand-in that exits after N wait() calls."""

    def __init__(self, exit_after_waits: int = 1):
        self.pid = 99999
        self._waits = 0
        self._exit_after = exit_after_waits
        self.returncode = None
        self.wait_timeouts = []
        self.signals = []

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        self._waits += 1
        if self._waits >= self._exit_after:
            # Graceful SIGTERM handling: the daemon finishes its shutdown
            # commit and exits zero.
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.signals.append("TERM")

    def kill(self):
        self.signals.append("KILL")


# ── a) SIGTERM grace window ───────────────────────────────────────────────


def test_stop_grace_constant_at_least_30s(isolated_daemon_state):
    dm = isolated_daemon_state
    assert dm._DAEMON_GRACEFUL_ORPHAN_TIMEOUT_SEC >= 30.0


def test_stop_daemon_waits_with_extended_grace(isolated_daemon_state):
    dm = isolated_daemon_state
    proc = _FakeDaemonProc(exit_after_waits=1)
    with dm._daemon_lock:
        dm.daemon_proc = proc
    dm.stop_daemon()
    assert proc.wait_timeouts, "stop_daemon must wait on the daemon process"
    assert max(proc.wait_timeouts) >= 30.0
    # Graceful path: SIGTERM window succeeded, no SIGKILL marker.
    assert proc.returncode == 0
    assert proc.signals == ["TERM"]


def test_orphan_termination_uses_extended_grace(isolated_daemon_state, monkeypatch):
    dm = isolated_daemon_state
    waits = []
    record = {"pid": 4242, "start_ticks": 0, "owner_token_digest": "x" * 64}
    monkey_state = {"signalled": 0}

    import signal as _signal

    def fake_getpgid(pid):
        return pid

    def fake_killpg(pgid, sig):
        monkey_state["signalled"] += 1
        if sig == _signal.SIGTERM:
            # The daemon dies inside the (extended) SIGTERM window. Route
            # the mid-test flip through monkeypatch too so teardown always
            # restores the REAL original (B4).
            monkeypatch.setattr(dm, "_pid_record_identity", lambda rec: "dead")
        return None

    # B4 (red-team follow-up): every daemon_management attribute override
    # goes through monkeypatch.setattr. The previous direct assignments
    # leaked the "dead" lambda into the module (the finally block restored
    # only getpgid/killpg/_wait_for_daemon_record_exit), reddening three
    # tests in alphabetical batch runs (test_rc3_daemon_grace.py::
    # test_pid_record_identity_rejects_pid_reuse / test_forged_live_pid_
    # record_cannot_signal_unrelated_process_group, test_routes_control.
    # py::TestStatus::test_daemon_pid_reuse_and_disabled_live_process_
    # fail_closed) even though each file passed in isolation.
    monkeypatch.setattr(dm, "_pid_record_identity", lambda rec: "match")
    monkeypatch.setattr(dm.os, "getpgid", fake_getpgid)
    monkeypatch.setattr(dm.os, "killpg", fake_killpg)

    def spying_wait(rec, timeout):
        waits.append(timeout)
        # First call: the full grace window elapses without a confirmed exit
        # identity change, then the identity flip above proves it on re-check.
        return len(waits) >= 2

    monkeypatch.setattr(dm, "_wait_for_daemon_record_exit", spying_wait)
    forced = dm._terminate_verified_daemon_record(record)
    # SIGTERM alone sufficed within the >= 30s window: no SIGKILL needed.
    assert forced is False
    assert monkey_state["signalled"] == 1
    assert waits and max(waits) >= 30.0


# ── b) bounded LLM-stream drain before stop returns ──────────────────────


@pytest.mark.asyncio
async def test_drain_live_llm_streams_waits_for_quiesce(monkeypatch):
    import llm_concurrency
    from server.routes import control

    counts = iter([3, 2, 1, 0])
    monkeypatch.setattr(
        llm_concurrency, "get_active_stream_count", lambda: next(counts, 0)
    )
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(control.asyncio, "sleep", fake_sleep)
    result = await control._drain_live_llm_streams()
    assert result["drained"] is True
    assert result["peak_active"] == 3
    assert slept  # actually polled on the drain cadence


@pytest.mark.asyncio
async def test_drain_live_llm_streams_is_bounded(monkeypatch):
    import llm_concurrency
    from server.routes import control

    monkeypatch.setattr(
        llm_concurrency, "get_active_stream_count", lambda: 4
    )
    monkeypatch.setattr(control, "_LLM_DRAIN_TIMEOUT_SEC", 0.05)
    clock = {"now": 100.0}
    monkeypatch.setattr(control.time, "monotonic", lambda: clock["now"])
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(control.asyncio, "sleep", fake_sleep)
    result = await control._drain_live_llm_streams()
    assert result["drained"] is False
    assert result["remaining"] == 4
    assert len(sleeps) <= 2  # bounded, not a busy spin


@pytest.mark.asyncio
async def test_stop_transaction_drains_llm_streams(monkeypatch, tmp_path):
    import inspect

    from server.routes import control

    drained = {"called": False}

    async def fake_drain(timeout_sec=None):
        drained["called"] = True
        return {"drained": True, "peak_active": 0}

    monkeypatch.setattr(control, "_drain_live_llm_streams", fake_drain)

    def fake_stop_daemon():
        return None

    def fake_bind(*args, **kwargs):
        return None

    async def fake_run_blocking(func, *args, thread_name_prefix=None):
        result = func(*args)
        if inspect.isawaitable(result):
            result = await result
        return result

    monkeypatch.setattr("evolution_core.stop_daemon", fake_stop_daemon,
                        raising=False)
    monkeypatch.setattr(control, "_bind_and_reset_stability", fake_bind)
    monkeypatch.setattr(control, "run_blocking_isolated", fake_run_blocking)
    monkeypatch.setattr(control.app_state, "request_shutdown", lambda: True)
    monkeypatch.setattr(control.app_state, "stop_running", lambda: None)
    monkeypatch.setattr(
        control, "_invalidate_observer_projection_cache", lambda: None
    )
    result = await control._stop_evolution_transaction()
    assert result == {"status": "stopped"}
    assert drained["called"] is True
