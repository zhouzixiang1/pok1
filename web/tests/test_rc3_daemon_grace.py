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

import hashlib
import logging
import json
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

import daemon_management


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


def test_pid_record_identity_rejects_pid_reuse(monkeypatch):
    pid = os.getpid()
    start_ticks = daemon_management._proc_start_ticks(pid)
    monkeypatch.setattr(
        daemon_management,
        "_daemon_owner_contract_identity",
        lambda _pid, _record: "match",
    )

    assert start_ticks is not None
    assert daemon_management._pid_record_identity({
        "pid": pid,
        "start_ticks": start_ticks,
    }) == "match"
    assert daemon_management._pid_record_identity({
        "pid": pid,
        "start_ticks": start_ticks + 1,
    }) == "reused"


def test_daemon_owner_contract_requires_token_exact_argv_and_group_leader(
    monkeypatch,
):
    pid = 4242
    token = b"daemon-owner-token"
    record = {
        "owner_token_digest": hashlib.sha256(token).hexdigest(),
    }
    expected_script = Path(daemon_management.__file__).resolve().with_name(
        "elo_daemon.py"
    )
    environ = b"POK_PROC=daemon\0POK_DAEMON_OWNER_TOKEN=" + token + b"\0"
    argv = [
        str(Path(sys.executable).resolve()),
        str(expected_script),
        "--workers",
        "4",
        "--pairs",
        "5",
    ]

    def proc_bytes(path):
        if path.name == "environ":
            return environ
        if path.name == "cmdline":
            return b"\0".join(item.encode() for item in argv) + b"\0"
        raise AssertionError(path)

    monkeypatch.setattr(Path, "read_bytes", proc_bytes)
    monkeypatch.setattr(daemon_management.os, "getpgid", lambda _pid: pid)

    assert daemon_management._daemon_owner_contract_identity(pid, record) == "match"

    argv[5] = "8"
    assert daemon_management._daemon_owner_contract_identity(pid, record) == "match"
    argv[5] = "9"
    assert (
        daemon_management._daemon_owner_contract_identity(pid, record)
        == "command_mismatch"
    )
    argv[5] = "5"

    argv.append("--unexpected")
    assert (
        daemon_management._daemon_owner_contract_identity(pid, record)
        == "command_mismatch"
    )
    argv.pop()
    environ = b"POK_DAEMON_OWNER_TOKEN=forged\0"
    assert (
        daemon_management._daemon_owner_contract_identity(pid, record)
        == "owner_mismatch"
    )
    environ = b"POK_DAEMON_OWNER_TOKEN=" + token + b"\0"
    monkeypatch.setattr(daemon_management.os, "getpgid", lambda _pid: pid + 1)
    assert (
        daemon_management._daemon_owner_contract_identity(pid, record)
        == "group_mismatch"
    )


def test_forged_live_pid_record_cannot_signal_unrelated_process_group(
    monkeypatch,
    tmp_path,
):
    pid = os.getpid()
    start_ticks = daemon_management._proc_start_ticks(pid)
    path = tmp_path / ".daemon_pid"
    path.write_text(
        json.dumps({"pid": pid, "start_ticks": start_ticks}),
        encoding="utf-8",
    )
    signals = []
    monkeypatch.setattr(daemon_management, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(
        daemon_management.os,
        "killpg",
        lambda pgid, sig: signals.append((pgid, sig)),
    )

    with pytest.raises(RuntimeError, match="daemon_pid_identity_unverifiable"):
        daemon_management._kill_orphan_from_pid_file()

    assert signals == []
    assert path.exists()


def test_verified_orphan_must_be_proven_gone_after_force_kill(monkeypatch):
    pid = 4242
    record = {
        "pid": pid,
        "start_ticks": 777,
        "owner_token_digest": "a" * 64,
    }
    signals = []
    monkeypatch.setattr(
        daemon_management,
        "_pid_record_identity",
        lambda _record: "match",
    )
    monkeypatch.setattr(daemon_management.os, "getpgid", lambda _pid: pid)
    monkeypatch.setattr(
        daemon_management.os,
        "killpg",
        lambda pgid, sig: signals.append((pgid, sig)),
    )
    monkeypatch.setattr(
        daemon_management,
        "_DAEMON_GRACEFUL_ORPHAN_TIMEOUT_SEC",
        0.0,
    )
    monkeypatch.setattr(
        daemon_management,
        "_DAEMON_FORCE_ORPHAN_TIMEOUT_SEC",
        0.0,
    )

    with pytest.raises(RuntimeError, match="daemon_orphan_did_not_exit"):
        daemon_management._terminate_verified_daemon_record(record)

    assert signals == [
        (pid, signal.SIGTERM),
        (pid, signal.SIGKILL),
    ]


def test_malformed_orphan_record_is_not_deleted_or_reported_stopped(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(daemon_management, "RESULTS_DIR", tmp_path)
    path = tmp_path / ".daemon_pid"
    path.write_text("not-a-pid-record", encoding="utf-8")

    with pytest.raises(RuntimeError, match="daemon_pid_record_invalid"):
        daemon_management._kill_orphan_from_pid_file()

    assert path.read_text(encoding="utf-8") == "not-a-pid-record"
