import asyncio
import json
from types import SimpleNamespace

import evolution_infra as evolution_infra_abs
from core import evolution_infra
from core import generation_scheduler


def test_prepare_priority_eval_signal_written(monkeypatch, tmp_path):
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(evolution_infra_abs, "RESULTS_DIR", results_dir)

    events = []
    monkeypatch.setattr(
        generation_scheduler,
        "log_system_event",
        lambda *args: events.append(args),
    )

    generation_scheduler._ensure_priority_eval_signal("national_v85", 24)

    payload = json.loads((results_dir / "priority_eval.json").read_text())
    assert payload["bot"] == "national_v85"
    assert payload["min_games"] == 24
    assert payload["source"] == "prepare_eval_wait"
    assert events[0][0] == "pipeline.eval_wait_priority_set"


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


def _evaluation_snapshot_bundle(active, *, games=15, rd=108.12):
    rows = [{
        "name": name,
        "selection_score": 0.5,
        "leaderboard_score": 0.5,
        "h2h_avg_wr": 0.5,
        "h2h_games": games,
        "h2h_opponents": max(0, len(active) - 1),
        "h2h_opponents_total": max(0, len(active) - 1),
        "h2h_coverage": 1.0,
        "strength_confidence": "medium",
    } for name in active]
    return {
        "available": True,
        "manifest": {
            "manifest_digest": "g" * 64,
            "cycle": {
                "manifest_digest": "c" * 64,
                "save_num": 7,
                "daemon_run_id": "run-1",
                "active_bots": list(active),
            },
        },
        "h2h": {},
        "bot_stats": {
            name: {"games": games, "win_rate": 0.5} for name in active
        },
        "ratings": {
            name: {"r": 1460.0, "rd": rd, "sigma": 0.06} for name in active
        },
        "selection": {
            "rows": rows,
            "rating_history_tail": [],
        },
    }


def test_post_wait_evidence_uses_manifest_bound_ratings_and_stats(monkeypatch):
    active = ["national_v111", "national_v142"]
    monkeypatch.setattr(evolution_infra_abs, "get_active_bots", lambda: list(active))
    monkeypatch.setattr(evolution_infra_abs, "find_latest_active_v", lambda: 142)
    events = []
    monkeypatch.setattr(
        generation_scheduler,
        "log_system_event",
        lambda *args: events.append(args),
    )

    evidence = generation_scheduler._load_post_wait_evaluation_evidence(
        active_v=142,
        active_bot_name="national_v142",
        min_games=24,
        rd_threshold=110,
        rd_min_games=12,
        expected_active_bots=active,
        snapshot_bundle=_evaluation_snapshot_bundle(active),
    )

    assert evidence is not None
    assert evidence.ratings["national_v142"].rd == 108.12
    assert evidence.bot_stats["national_v142"]["games"] == 15
    assert evidence.rd == 108.12
    assert evidence.readiness_reason == "rd_threshold"
    assert events[-1][0] == "pipeline.eval_evidence_frozen"


def test_post_wait_evidence_rejects_published_cycle_that_is_not_ready(monkeypatch):
    active = ["national_v142"]
    monkeypatch.setattr(evolution_infra_abs, "get_active_bots", lambda: ["national_v142"])
    monkeypatch.setattr(evolution_infra_abs, "find_latest_active_v", lambda: 142)
    events = []
    monkeypatch.setattr(
        generation_scheduler,
        "log_system_event",
        lambda *args: events.append(args),
    )

    evidence = generation_scheduler._load_post_wait_evaluation_evidence(
        active_v=142,
        active_bot_name="national_v142",
        min_games=24,
        rd_threshold=110,
        rd_min_games=12,
        expected_active_bots=active,
        snapshot_bundle=_evaluation_snapshot_bundle(active, games=15, rd=350.0),
    )

    assert evidence is None
    assert events[-1][0] == "pipeline.eval_evidence_incoherent"
    assert "post_wait_readiness_not_reproducible" in events[-1][3]["issues"]


def test_post_wait_evidence_rejects_active_pool_change_while_loading(monkeypatch):
    active = ["national_v142"]
    pools = iter([active, ["national_v141", "national_v142"]])
    monkeypatch.setattr(evolution_infra_abs, "get_active_bots", lambda: list(next(pools)))
    monkeypatch.setattr(evolution_infra_abs, "find_latest_active_v", lambda: 142)
    monkeypatch.setattr(generation_scheduler, "log_system_event", lambda *_args: None)

    evidence = generation_scheduler._load_post_wait_evaluation_evidence(
        active_v=142,
        active_bot_name="national_v142",
        min_games=24,
        rd_threshold=110,
        rd_min_games=12,
        expected_active_bots=active,
        snapshot_bundle=_evaluation_snapshot_bundle(active),
    )

    assert evidence is None
