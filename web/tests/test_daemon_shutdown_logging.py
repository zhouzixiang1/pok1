import logging


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
