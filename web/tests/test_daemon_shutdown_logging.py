import logging
import sys
from pathlib import Path
from types import ModuleType


def test_daemon_waits_through_zero_and_one_bot_without_creating_strength(
    monkeypatch,
):
    import elo_daemon
    import elo_daemon_persistence

    pools = [[], ["national_v143"], ["national_v143", "national_v144"]]
    heartbeats = []
    events = []
    monkeypatch.setattr(elo_daemon, "running", True)
    monkeypatch.setattr(elo_daemon, "RATING_POOL_IDLE_POLL_SEC", 0.0)
    monkeypatch.setattr(
        elo_daemon,
        "get_active_bots",
        lambda: pools.pop(0) if pools else ["national_v143", "national_v144"],
    )
    # The mocked bot names ("national_v143", "national_v144") do not exist as
    # strict published artifacts on disk, so _reconcile_rating_pool_membership
    # would drop them as rating-ineligible (bot_path() raises). Stub the
    # eligibility resolver so the mocked bots survive into the rating pool.
    monkeypatch.setattr(
        elo_daemon_persistence,
        "bot_path",
        lambda bot_name: f"/tmp/fake_bots/{bot_name}/run.sh",
    )
    monkeypatch.setattr(
        elo_daemon,
        "_write_heartbeat",
        lambda **kwargs: heartbeats.append(kwargs),
    )
    monkeypatch.setattr(
        elo_daemon,
        "log_system_event",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )
    ratings = {}

    active, h2h = elo_daemon._wait_for_minimum_rating_pool(
        [],
        ratings,
        {},
        {},
    )

    assert active == ["national_v143", "national_v144"]
    assert h2h == {}
    assert set(ratings) == set(active)
    assert [row["activity_state"] for row in heartbeats] == [
        "waiting_for_first_published_bot",
        "waiting_for_first_published_bot",
        "waiting_for_second_published_bot",
        "scheduling_matches",
    ]
    assert len([args for args, _ in events if args[0] == "daemon.rating_pool_idle"]) == 2


def test_daemon_once_exits_cleanly_when_pool_is_not_schedulable(monkeypatch):
    import elo_daemon

    monkeypatch.setattr(elo_daemon, "running", True)
    monkeypatch.setattr(elo_daemon, "_write_heartbeat", lambda **_kwargs: None)
    monkeypatch.setattr(elo_daemon, "log_system_event", lambda *_a, **_k: None)

    active, h2h = elo_daemon._wait_for_minimum_rating_pool(
        ["national_v143"],
        {"national_v143": elo_daemon.Glicko2Player()},
        {},
        {},
        once=True,
    )

    assert active == ["national_v143"]
    assert h2h == {}


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
