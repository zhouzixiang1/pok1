import json


def _write_jsonl(path, rows):
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_70_hand_summary_uses_sign_first_and_amount_second():
    from strength_order import summarize_70_hand_net_chips

    summary = summarize_70_hand_net_chips([100, 1, -10_000, 0])

    assert summary["positive_matches"] == 2
    assert summary["negative_matches"] == 1
    assert summary["zero_matches"] == 1
    assert summary["primary_match_score"] == 0.625
    assert summary["secondary_net_chips_total"] == -9_899
    assert summary["secondary_net_chips_mean"] == -2_474.75


def test_equal_primary_strength_is_broken_by_70_hand_chip_amount(tmp_path):
    from rating_snapshot import build_strength_rows

    ratings = {
        "national_v1": {"r": 1500, "rd": 80, "sigma": 0.06},
        "national_v2": {"r": 1500, "rd": 80, "sigma": 0.06},
    }
    stats = {
        "national_v1": {"games": 2, "win_rate": 0.5},
        "national_v2": {"games": 2, "win_rate": 0.5},
    }
    h2h = {
        "national_v1 vs national_v2": {
            "games": 2,
            "a_wins": 1,
            "b_wins": 1,
            "draws": 0,
        },
    }
    history = tmp_path / "match_history.jsonl"
    _write_jsonl(history, [{
        "bot0": "national_v1",
        "bot1": "national_v2",
        "bot0_wins": 1,
        "bot1_wins": 1,
        "draws": 0,
        "strength_sample_unit": "70_hand_match",
        "net_chips_bot0": [500, -100],
    }])

    rows = build_strength_rows(
        ratings,
        stats,
        h2h,
        active_bots=list(ratings),
        match_history_path=history,
    )

    assert rows[0]["name"] == "national_v1"
    assert rows[0]["selection_score"] == rows[1]["selection_score"]
    assert rows[0]["secondary_net_chips_mean"] == 200.0
    assert rows[1]["secondary_net_chips_mean"] == -200.0
    assert rows[0]["strength_order_contract"] == [
        "70_hand_positive_result",
        "net_chips_magnitude",
    ]


def test_corrupt_chip_samples_do_not_enter_secondary_strength(tmp_path):
    from rating_snapshot import national_chip_metrics_from_match_history

    history = tmp_path / "match_history.jsonl"
    _write_jsonl(history, [{
        "bot0": "national_v1",
        "bot1": "national_v2",
        "bot0_wins": 2,
        "bot1_wins": 0,
        "draws": 0,
        "strength_sample_unit": "70_hand_match",
        "net_chips_bot0": [100, -50],
    }])

    assert national_chip_metrics_from_match_history(
        ["national_v1", "national_v2"],
        history,
    ) == {}


def test_match_replay_persists_primary_and_secondary_contract(tmp_path, monkeypatch):
    import elo_daemon
    import evaluation_data_identity

    replay_dir = tmp_path / "replays"
    results_dir = tmp_path / "results"
    history = results_dir / "match_history.jsonl"
    monkeypatch.setattr(elo_daemon, "REPLAY_DIR", replay_dir)
    monkeypatch.setattr(elo_daemon, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(elo_daemon, "MATCH_HISTORY_FILE", history)
    monkeypatch.setattr(
        evaluation_data_identity,
        "current_evaluation_digest",
        lambda _root: "evaluation-digest",
    )

    name = elo_daemon.save_match_replay(
        "national_v1",
        "national_v2",
        1,
        1,
        0,
        [],
        [500, -100],
        "70_hand_match",
    )

    replay = json.loads((replay_dir / name).read_text(encoding="utf-8"))
    summary = json.loads(history.read_text(encoding="utf-8"))
    assert replay["strength_order"]["primary_match_score"] == 0.5
    assert replay["strength_order"]["secondary_net_chips_mean"] == 200.0
    assert summary["net_chips_bot0"] == [500, -100]
