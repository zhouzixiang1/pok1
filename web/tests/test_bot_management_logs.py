import asyncio
import json


def _setup_reap_case(tmp_path, monkeypatch, max_active=1):
    import tool_bot_management as tbm

    root = tmp_path
    bots_dir = root / "bots"
    for name in ("national_v1", "national_v2", "national_v3"):
        (bots_dir / name).mkdir(parents=True)
        (bots_dir / name / ".completed").touch()
    results_dir = root / "web" / "core" / "results"
    results_dir.mkdir(parents=True)
    (results_dir / "bot_stats.json").write_text(
        json.dumps({
            "national_v1": {"games": 700},
            "national_v2": {"games": 700},
            "national_v3": {"games": 700},
        }),
        encoding="utf-8",
    )

    events = []
    monkeypatch.setattr(tbm, "PROJECT_ROOT", root)
    monkeypatch.setattr(tbm, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(tbm, "REPLAY_DIR", results_dir / "replays")
    monkeypatch.setattr(tbm, "MAX_ACTIVE_BOTS", 1)
    monkeypatch.setattr(tbm, "get_active_bots", lambda: ["national_v1", "national_v2", "national_v3"])
    monkeypatch.setattr(tbm, "find_latest_active_v", lambda: 3)
    monkeypatch.setattr(tbm, "load_ratings", lambda: {
        # v1 has stronger displayed context metrics below, but weaker true cull key.
        "national_v1": tbm.Glicko2Player(r=1300, rd=100),
        "national_v2": tbm.Glicko2Player(r=1400, rd=100),
        "national_v3": tbm.Glicko2Player(r=1500, rd=50),
    })
    monkeypatch.setattr(tbm, "load_h2h_avg_winrates", lambda: {
        "national_v1": 0.75,
        "national_v2": 0.10,
    })
    monkeypatch.setattr(tbm, "load_strength_scores", lambda: {
        "national_v1": 0.90,
        "national_v2": 0.10,
    })
    monkeypatch.setattr(tbm, "log_system_event", lambda *args: events.append(args))

    return tbm, bots_dir, events


def test_reap_event_reports_conservative_glicko_selection_key(tmp_path, monkeypatch):
    tbm, bots_dir, events = _setup_reap_case(tmp_path, monkeypatch)

    result = asyncio.run(tbm._do_reap_weakest())

    assert result["reaped"] is True
    assert result["culled"] == "national_v1"
    assert result["selection_key"] == "conservative_glicko"
    assert result["conservative_rating"] == 1100
    assert (bots_dir / "national_v1").exists()
    assert not (bots_dir / "national_v1" / ".completed").exists()
    assert not (bots_dir / "graveyard" / "national_v1").exists()

    assert events
    event_type, level, message, data = events[0]
    assert event_type == "bot.reaped"
    assert level == "warn"
    assert "r-2rd=1100.0" in message
    assert data["selection_key"] == "conservative_glicko"
    assert data["conservative_rating"] == 1100
    assert data["leaderboard_score"] == 0.9
    assert data["h2h_avg_wr"] == 0.75
    assert data["quiet"] is False


def test_quiet_reap_still_emits_structured_bot_event(tmp_path, monkeypatch):
    tbm, _bots_dir, events = _setup_reap_case(tmp_path, monkeypatch)

    result = asyncio.run(tbm._do_reap_weakest(quiet=True))

    assert result["reaped"] is True
    assert result["culled"] == "national_v1"
    assert events
    event_type, level, message, data = events[0]
    assert event_type == "bot.reaped"
    assert level == "info"
    assert message.startswith("Auto-reaped national_v1")
    assert data["culled"] == "national_v1"
    assert data["quiet"] is True
