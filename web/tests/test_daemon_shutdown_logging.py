import logging
import sys
from pathlib import Path
from types import ModuleType


def test_pool_break_during_shutdown_is_info_not_error(monkeypatch, caplog):
    import elo_daemon

    events = []
    monkeypatch.setattr(elo_daemon, "running", False)
    monkeypatch.setattr(
        elo_daemon,
        "log_system_event",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )

    with caplog.at_level(logging.INFO, logger="pok.daemon"):
        handled = elo_daemon._handle_pool_break_for_shutdown(RuntimeError("pool gone"))

    assert handled is True
    assert events
    assert all(record.levelno < logging.ERROR for record in caplog.records)
    assert "ProcessPool interrupted during daemon shutdown" in caplog.text


def test_pool_break_while_running_uses_recovery_path(monkeypatch):
    import elo_daemon

    monkeypatch.setattr(elo_daemon, "running", True)

    assert elo_daemon._handle_pool_break_for_shutdown(RuntimeError("pool gone")) is False


def test_daemon_signal_handler_emits_structured_signal_event(monkeypatch):
    import signal
    import elo_daemon

    events = []
    old_running = elo_daemon.running
    monkeypatch.setattr(
        elo_daemon,
        "log_system_event",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )
    try:
        elo_daemon.running = True
        elo_daemon.handle_signal(signal.SIGTERM, None)
        assert elo_daemon.running is False
    finally:
        elo_daemon.running = old_running

    event = next(args for args, _kwargs in events if args[0] == "daemon.signal_received")
    assert event[1] == "warn"
    assert event[3]["signal"] == "SIGTERM"
    assert event[3]["shutdown_requested"] is True


def test_daemon_official_certification_worker_processes_queue(monkeypatch):
    import elo_daemon

    calls = []
    events = []
    fake_cert = ModuleType("official_certification")

    def fake_process_certification_queue(limit=1):
        calls.append(limit)
        elo_daemon.running = False
        return {
            "processed": 1,
            "remaining": 0,
            "lock_busy": False,
            "results": [{"candidate": "bot", "status": "official-smoke-pass"}],
            "errors": [],
        }

    fake_cert.process_certification_queue = fake_process_certification_queue
    monkeypatch.setitem(sys.modules, "official_certification", fake_cert)
    monkeypatch.setattr(elo_daemon, "running", True)
    monkeypatch.setattr(elo_daemon, "OFFICIAL_CERT_QUEUE_INTERVAL_SEC", 5.0)
    monkeypatch.setattr(elo_daemon, "OFFICIAL_CERT_QUEUE_LIMIT", 1)
    monkeypatch.setattr(
        elo_daemon,
        "log_system_event",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )

    thread = elo_daemon.start_official_certification_thread()
    assert thread is not None
    thread.join(timeout=2.0)

    assert calls == [1]
    assert not thread.is_alive()
    assert any(args[0] == "official_certification.queue_processed" for args, _ in events)


def test_daemon_exit_metadata_classifies_signal():
    import daemon_management

    meta = daemon_management._daemon_exit_metadata(-9)

    assert meta["exit_cause"] == "signal"
    assert meta["signal"] == "SIGKILL"
    assert meta["killer_known"] is False


def test_daemon_monitor_classifies_stop_sigkill_as_stop_not_crash(monkeypatch):
    import daemon_management

    class FakeProc:
        pid = 12345

        def poll(self):
            daemon_management._daemon_shutting_down = True
            return -9

    class FakeUI:
        def __init__(self):
            self.history = []
            self.status_updates = []

        def log_history(self, message, level):
            self.history.append((level, message))

        def update_daemon_status(self, stats, ratings):
            self.status_updates.append((stats, ratings))

    class FakeStopEvent:
        def is_set(self):
            return False

        def wait(self, _seconds):
            return True

    events = []
    monkeypatch.setattr(
        daemon_management,
        "log_system_event",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )
    old_proc = daemon_management.daemon_proc
    old_shutdown = daemon_management._daemon_shutting_down
    try:
        daemon_management.daemon_proc = FakeProc()
        daemon_management._daemon_shutting_down = False

        daemon_management.daemon_monitor_thread(
            FakeUI(), FakeStopEvent(), daemon_workers=1, daemon_pairs=1
        )
    finally:
        daemon_management.daemon_proc = old_proc
        daemon_management._daemon_shutting_down = old_shutdown

    event_types = [args[0] for args, _kwargs in events]
    assert "daemon.exited_after_stop" in event_types
    assert "daemon.crashed" not in event_types


def test_daemon_monitor_crash_event_includes_exit_metadata(monkeypatch):
    import daemon_management

    class FakeProc:
        pid = 23456

        def poll(self):
            return -9

    class FakeUI:
        def __init__(self):
            self.history = []

        def log_history(self, message, level):
            self.history.append((level, message))

        def update_daemon_status(self, stats, ratings):
            pass

    class FakeStopEvent:
        def is_set(self):
            return False

        def wait(self, _seconds):
            return True

    events = []
    monkeypatch.setattr(
        daemon_management,
        "log_system_event",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )
    old_proc = daemon_management.daemon_proc
    old_shutdown = daemon_management._daemon_shutting_down
    try:
        daemon_management.daemon_proc = FakeProc()
        daemon_management._daemon_shutting_down = False

        daemon_management.daemon_monitor_thread(
            FakeUI(), FakeStopEvent(), daemon_workers=1, daemon_pairs=1
        )
    finally:
        daemon_management.daemon_proc = old_proc
        daemon_management._daemon_shutting_down = old_shutdown

    crashed = [args for args, _kwargs in events if args[0] == "daemon.crashed"]
    assert len(crashed) == 1
    payload = crashed[0][3]
    assert payload["returncode"] == -9
    assert payload["exit_cause"] == "signal"
    assert payload["signal"] == "SIGKILL"
    assert payload["killer_known"] is False


def test_daemon_final_save_failure_is_structured_event():
    src = (Path(__file__).resolve().parent.parent / "core" / "elo_daemon.py").read_text()
    assert "daemon.final_save_failed" in src
    assert "Final save failed" in src
