import asyncio
import json
from types import SimpleNamespace

from core import evolution_infra


def test_wait_for_daemon_eval_emits_start_and_ready(monkeypatch, tmp_path):
    stats_file = tmp_path / "bot_stats.json"
    stats_file.write_text(json.dumps({"claude_v9": {"games": 100}}), encoding="utf-8")
    monkeypatch.setattr(evolution_infra, "BOT_STATS_FILE", stats_file)

    events = []
    monkeypatch.setattr(
        evolution_infra,
        "_log_eval_wait_event",
        lambda event_type, severity, message, **data: events.append(
            (event_type, severity, message, data)
        ),
    )

    ready = asyncio.run(
        evolution_infra.wait_for_daemon_eval("claude_v9", timeout=1, min_games=100)
    )

    assert ready is True
    assert [event[0] for event in events] == [
        "pipeline.eval_wait_start",
        "pipeline.eval_wait_ready",
    ]
    _event_type, severity, _message, data = events[-1]
    assert severity == "success"
    assert data["bot"] == "claude_v9"
    assert data["games"] == 100
    assert data["reason"] == "min_games"


def test_wait_for_daemon_eval_emits_progress_and_timeout(monkeypatch, tmp_path):
    stats_file = tmp_path / "bot_stats.json"
    stats_file.write_text(json.dumps({"claude_v9": {"games": 12}}), encoding="utf-8")
    monkeypatch.setattr(evolution_infra, "BOT_STATS_FILE", stats_file)
    monkeypatch.setattr(evolution_infra, "EVAL_WAIT_PROGRESS_INTERVAL_SEC", 1)

    tick = {"now": 0}

    def fake_time():
        tick["now"] += 1
        return tick["now"]

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(evolution_infra.time, "time", fake_time)
    monkeypatch.setattr(evolution_infra.asyncio, "sleep", no_sleep)

    events = []
    monkeypatch.setattr(
        evolution_infra,
        "_log_eval_wait_event",
        lambda event_type, severity, message, **data: events.append(
            (event_type, severity, message, data)
        ),
    )

    ready = asyncio.run(
        evolution_infra.wait_for_daemon_eval("claude_v9", timeout=8, min_games=100)
    )

    assert ready is False
    event_types = [event[0] for event in events]
    assert "pipeline.eval_wait_start" in event_types
    assert "pipeline.eval_wait_progress" in event_types
    assert event_types[-1] == "pipeline.eval_wait_timeout"

    _event_type, severity, _message, data = events[-1]
    assert severity == "warn"
    assert data["bot"] == "claude_v9"
    assert data["games"] == 12
    assert data["min_games"] == 100


def test_wait_for_daemon_eval_uses_custom_rd_gate(monkeypatch, tmp_path):
    stats_file = tmp_path / "bot_stats.json"
    stats_file.write_text(json.dumps({"claude_v9": {"games": 12}}), encoding="utf-8")
    ratings_file = tmp_path / "ratings.json"
    ratings_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(evolution_infra, "BOT_STATS_FILE", stats_file)
    monkeypatch.setattr(evolution_infra, "RATINGS_FILE", ratings_file)
    monkeypatch.setattr(
        evolution_infra,
        "load_ratings",
        lambda: {"claude_v9": SimpleNamespace(rd=100.0)},
    )

    events = []
    monkeypatch.setattr(
        evolution_infra,
        "_log_eval_wait_event",
        lambda event_type, severity, message, **data: events.append(
            (event_type, severity, message, data)
        ),
    )

    ready = asyncio.run(
        evolution_infra.wait_for_daemon_eval(
            "claude_v9",
            timeout=1,
            min_games=24,
            rd_threshold=110,
            rd_min_games=12,
        )
    )

    assert ready is True
    _event_type, severity, _message, data = events[-1]
    assert severity == "success"
    assert data["reason"] == "rd_threshold"
    assert data["games"] == 12
    assert data["rd_threshold"] == 110
