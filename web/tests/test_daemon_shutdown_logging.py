import logging
from pathlib import Path


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


def test_daemon_final_save_failure_is_structured_event():
    src = (Path(__file__).resolve().parent.parent / "core" / "elo_daemon.py").read_text()
    assert "daemon.final_save_failed" in src
    assert "Final save failed" in src
