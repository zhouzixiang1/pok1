"""Tests for RC3: daemon graceful-shutdown grace period + SIGKILL fallback.

RC3 root cause: stop_daemon gave the daemon only 3s to exit gracefully, but
graceful shutdown (cancel in-flight mirror battles + fcntl save_cycle of
ratings/h2h/stats) takes ~2-3s under load, so the daemon frequently hit SIGKILL
(rc=-9) on stop/restart — the monitor then logged it as "daemon.crashed" and
auto-restarted (benign but noisy + wasted in-flight battles).

Fix: grace 3s -> 8s (comfortable headroom over the ~2-3s graceful shutdown),
plus a warning log when the SIGKILL backstop actually fires so rc=-9 events
have explicit context instead of looking like an opaque crash.
"""

import logging
import signal
import subprocess

import pytest

from core import daemon_management


class _FakeProc:
    """Minimal subprocess.Popen stand-in for stop_daemon's kill/wait path."""

    def __init__(self, wait_timeout_expires_at=None):
        self.pid = 99999
        # If set, wait(timeout>=N) raises TimeoutExpired (simulates a daemon
        # that doesn't exit within the grace window).
        self._expires_at = wait_timeout_expires_at

    def poll(self):
        return None  # alive -> stop_daemon enters the terminate path

    def wait(self, timeout=None):
        if (self._expires_at is not None and timeout is not None
                and timeout >= self._expires_at):
            raise subprocess.TimeoutExpired(cmd="daemon", timeout=timeout)
        return 0  # graceful (or post-SIGKILL) exit

    def kill(self):
        pass

    def terminate(self):
        pass


@pytest.fixture
def _isolated(monkeypatch, tmp_path):
    """Isolate stop_daemon from the real OS + filesystem."""
    killpg_calls = []
    monkeypatch.setattr("os.getpgid", lambda pid: 99999)
    monkeypatch.setattr("os.killpg", lambda pgid, sig: killpg_calls.append((pgid, sig)))
    monkeypatch.setattr(daemon_management, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(daemon_management, "log_system_event", lambda *a, **k: None)
    return killpg_calls


def test_graceful_exit_no_sigkill(monkeypatch, _isolated):
    """Daemon exits within grace -> exactly one SIGTERM, no SIGKILL, handle cleared."""
    monkeypatch.setattr(daemon_management, "daemon_proc", _FakeProc())
    daemon_management.stop_daemon()
    sigs = [sig for _, sig in _isolated]
    assert sigs == [signal.SIGTERM]  # SIGTERM sent, then graceful wait — no force kill
    assert daemon_management.daemon_proc is None  # handle cleared after stop


def test_grace_expired_triggers_sigkill_and_log(monkeypatch, _isolated, caplog):
    """Daemon exceeds grace -> SIGTERM then SIGKILL + warning log naming the 8s window."""
    # wait(timeout>=8) raises -> simulates daemon not exiting within the 8s grace.
    monkeypatch.setattr(daemon_management, "daemon_proc",
                        _FakeProc(wait_timeout_expires_at=8))
    with caplog.at_level(logging.WARNING, logger="pok.infra"):
        daemon_management.stop_daemon()
    sigs = [sig for _, sig in _isolated]
    assert sigs == [signal.SIGTERM, signal.SIGKILL]  # graceful attempt, then force
    assert daemon_management.daemon_proc is None
    msg = " ".join(r.getMessage() or "" for r in caplog.records)
    assert "8s" in msg and "force killing" in msg, (
        "expected a SIGKILL warning naming the 8s grace window")
