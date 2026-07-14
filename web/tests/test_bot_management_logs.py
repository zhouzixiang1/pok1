import asyncio
import json


def _setup_reap_case(tmp_path, monkeypatch, max_active=1):
    import tool_bot_management as tbm

    root = tmp_path
    bots_dir = root / "bots"
    for name in ("national_v143", "national_v144", "national_v145"):
        (bots_dir / name).mkdir(parents=True)
        (bots_dir / name / ".completed").touch()
    results_dir = root / "web" / "core" / "results"
    results_dir.mkdir(parents=True)
    (results_dir / "bot_stats.json").write_text(
        json.dumps({
            "national_v143": {"games": 700},
            "national_v144": {"games": 700},
            "national_v145": {"games": 700},
        }),
        encoding="utf-8",
    )

    events = []
    monkeypatch.setattr(tbm, "PROJECT_ROOT", root)
    monkeypatch.setattr(tbm, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(tbm, "REPLAY_DIR", results_dir / "replays")
    monkeypatch.setattr(tbm, "MAX_ACTIVE_BOTS", 1)
    monkeypatch.setattr(tbm, "get_active_bots", lambda: ["national_v143", "national_v144", "national_v145"])
    monkeypatch.setattr(tbm, "find_latest_active_v", lambda: 145)
    monkeypatch.setattr(tbm, "load_ratings", lambda: {
        # v143 has stronger context metrics below, but weaker true cull key.
        "national_v143": tbm.Glicko2Player(r=1300, rd=100),
        "national_v144": tbm.Glicko2Player(r=1400, rd=100),
        "national_v145": tbm.Glicko2Player(r=1500, rd=50),
    })
    monkeypatch.setattr(tbm, "load_h2h_avg_winrates", lambda: {
        "national_v143": 0.75,
        "national_v144": 0.10,
    })
    monkeypatch.setattr(tbm, "load_strength_scores", lambda: {
        "national_v143": 0.90,
        "national_v144": 0.10,
    })
    monkeypatch.setattr(tbm, "record_reaped_bot", lambda *args, **kwargs: None)
    monkeypatch.setattr(tbm, "log_system_event", lambda *args: events.append(args))

    return tbm, bots_dir, events


def test_reap_event_reports_conservative_glicko_selection_key(tmp_path, monkeypatch):
    tbm, bots_dir, events = _setup_reap_case(tmp_path, monkeypatch)

    result = asyncio.run(tbm._do_reap_weakest())

    assert result["reaped"] is True
    assert result["culled"] == "national_v143"
    assert result["selection_key"] == "conservative_glicko"
    assert result["conservative_rating"] == 1100
    assert (bots_dir / "national_v143").exists()
    assert not (bots_dir / "national_v143" / ".completed").exists()
    assert {path.name for path in bots_dir.iterdir()} == {"national_v143", "national_v144", "national_v145"}

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
    assert result["culled"] == "national_v143"
    assert events
    event_type, level, message, data = events[0]
    assert event_type == "bot.reaped"
    assert level == "info"
    assert message.startswith("Auto-reaped national_v143")
    assert data["culled"] == "national_v143"
    assert data["quiet"] is True


def test_hard_overflow_reaps_old_zero_game_before_strong_evaluated_baseline(tmp_path, monkeypatch):
    import tool_bot_management as tbm

    root = tmp_path
    bots_dir = root / "bots"
    active = [f"national_v{i}" for i in range(143, 178)]
    for name in active:
        bot_dir = bots_dir / name
        bot_dir.mkdir(parents=True)
        (bot_dir / ".completed").touch()

    results_dir = root / "web" / "core" / "results"
    results_dir.mkdir(parents=True)
    (results_dir / "bot_stats.json").write_text(
        json.dumps({"national_v152": {"games": 1000}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(tbm, "PROJECT_ROOT", root)
    monkeypatch.setattr(tbm, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(tbm, "REPLAY_DIR", results_dir / "replays")
    monkeypatch.setattr(tbm, "MAX_ACTIVE_BOTS", 30)
    monkeypatch.setattr(tbm, "get_active_bots", lambda: active)
    monkeypatch.setattr(tbm, "find_latest_active_v", lambda: 177)
    monkeypatch.setattr(
        tbm,
        "load_ratings",
        lambda: {"national_v152": tbm.Glicko2Player(r=3000, rd=350)},
    )
    monkeypatch.setattr(tbm, "load_h2h_avg_winrates", lambda: {})
    monkeypatch.setattr(tbm, "load_strength_scores", lambda: {})
    monkeypatch.setattr(tbm, "record_reaped_bot", lambda *args, **kwargs: None)
    monkeypatch.setattr(tbm, "log_system_event", lambda *args, **kwargs: None)

    result = asyncio.run(tbm._do_reap_weakest(quiet=True))

    assert result["reaped"] is True
    assert result["culled"] == "national_v143"
    assert (bots_dir / "national_v152" / ".completed").exists()
    assert not (bots_dir / "national_v143" / ".completed").exists()
