import json


def _write_jsonl(path, rows):
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )


def _admitted_history_row(**overrides):
    row = {
        "bot0": "national_v1",
        "bot1": "national_v2",
        "bot0_wins": 1,
        "bot1_wins": 1,
        "draws": 0,
        "strength_sample_unit": "70_hand_match",
        "hands_per_strength_sample": 70,
        "strength_admitted": True,
        "strength_complete": True,
        "strength_compliance_passed": True,
        "strength_sample_count": 2,
        "net_chips_bot0": [500, -100],
    }
    row.update(overrides)
    return row


def test_70_hand_summary_uses_sign_first_and_amount_second():
    from strength_order import summarize_70_hand_net_chips

    summary = summarize_70_hand_net_chips([100, 1, -10_000, 0])

    assert summary["positive_matches"] == 2
    assert summary["negative_matches"] == 1
    assert summary["zero_matches"] == 1
    assert summary["primary_match_score"] == 0.625
    assert summary["secondary_net_chips_total"] == -9_899
    assert summary["secondary_net_chips_mean"] == -2_474.75


def test_match_score_counts_every_draw_as_half_a_point():
    from strength_order import match_score

    assert match_score(0, 8, 8) == 0.5
    assert match_score(2, 2, 5) == 0.6
    assert match_score(0, 0, 0) is None


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
    _write_jsonl(history, [_admitted_history_row()])

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
    _write_jsonl(history, [_admitted_history_row(
        bot0_wins=2,
        bot1_wins=0,
        strength_sample_count=2,
        net_chips_bot0=[100, -50],
    )])

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
        [
            {"hands_played": 70, "passed_compliance": True},
            {"hands_played": 70, "passed_compliance": True},
        ],
        [500, -100],
        "70_hand_match",
    )

    replay = json.loads((replay_dir / name).read_text(encoding="utf-8"))
    summary = json.loads(history.read_text(encoding="utf-8"))
    assert replay["strength_order"]["primary_match_score"] == 0.5
    assert replay["strength_order"]["secondary_net_chips_mean"] == 200.0
    assert summary["net_chips_bot0"] == [500, -100]
    assert summary["strength_admitted"] is True
    assert summary["hands_per_strength_sample"] == 70


def test_precommit_outcome_gate_rejects_tiny_0w_8l_collapse():
    from strength_order import precommit_outcome_blockers

    blockers, summary = precommit_outcome_blockers([{
        "opponent": "national_v1",
        "wins": 0,
        "losses": 8,
        "draws": 0,
        "net_chips": [-1] * 8,
    }], parent_label="national_v1")

    assert summary["primary_match_score"] == 0.0
    assert {row["reason"] for row in blockers} == {
        "lost_to_parent",
        "aggregate_native_regression",
    }


def test_precommit_outcome_gate_keeps_9w_7l_despite_huge_negative_chips():
    from strength_order import precommit_outcome_blockers

    blockers, summary = precommit_outcome_blockers([{
        "opponent": "national_v1",
        "wins": 9,
        "losses": 7,
        "draws": 0,
        "net_chips": [1] * 9 + [-100_000] * 7,
    }], parent_label="national_v1")

    assert summary["primary_match_score"] == 9 / 16
    assert blockers == []


def test_non_strength_nemesis_probe_cannot_change_primary_outcome_gate():
    from strength_order import precommit_outcome_blockers

    blockers, summary = precommit_outcome_blockers([
        {
            "opponent": "national_v1",
            "reason": "parent",
            "wins": 8,
            "losses": 0,
            "draws": 0,
        },
        {
            "opponent": "national_v2",
            "reason": "nemesis_probe",
            "wins": 0,
            "losses": 100,
            "draws": 0,
        },
    ], parent_label="national_v1")

    assert blockers == []
    assert summary["wins"] == 8
    assert summary["losses"] == 0
    assert summary["samples"] == 8


def test_draws_score_half_in_bot_stats():
    from evolution_infra import update_bot_stats

    stats = {}
    update_bot_stats(stats, "national_v1", wins=1, losses=1, draws=2)

    assert stats["national_v1"] == {
        "wins": 1,
        "losses": 1,
        "draws": 2,
        "games": 4,
        "win_rate": 0.5,
    }


def test_history_reconstruction_rejects_unproven_incomplete_or_failed_rows(tmp_path):
    from rating_snapshot import reconstruct_h2h_from_match_history

    history = tmp_path / "match_history.jsonl"
    _write_jsonl(history, [
        _admitted_history_row(strength_complete=False),
        _admitted_history_row(strength_compliance_passed=False),
        _admitted_history_row(hands_per_strength_sample=69),
        _admitted_history_row(strength_admitted=False),
        _admitted_history_row(),
    ])

    rebuilt = reconstruct_h2h_from_match_history(
        ["national_v1", "national_v2"],
        history,
    )
    assert rebuilt["national_v1 vs national_v2"] == {
        "games": 2,
        "a_wins": 1,
        "b_wins": 1,
        "draws": 0,
        "win_rate": 0.5,
    }


def test_exploitability_probe_treats_all_draws_as_neutral(tmp_path, monkeypatch):
    import exploitability_prober

    monkeypatch.setattr(
        exploitability_prober,
        "PROBES",
        [("draw_probe", "draw.py", "min_bet_defense")],
    )
    monkeypatch.setattr(
        exploitability_prober,
        "_run_probe_battle_import",
        lambda *_args: (0, 0, 10, 10),
    )
    monkeypatch.setattr(exploitability_prober, "_select_adaptive_opponents", lambda *_: [])
    monkeypatch.setattr(exploitability_prober, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(
        exploitability_prober,
        "EXPLOITABILITY_FILE",
        tmp_path / "exploitability.json",
    )

    result = exploitability_prober.run_exploitability_probes(
        tmp_path / "national_v1" / "main.py",
        num_hands=10,
        workers=1,
    )

    assert result["min_bet_defense"]["win_rate"] == 0.5
    assert result["min_bet_defense"]["exploitable"] is False


def test_status_h2h_treats_all_draws_as_neutral(tmp_path, monkeypatch):
    import asyncio
    import evolution_infra
    import tool_status

    h2h_file = tmp_path / "head_to_head.json"
    h2h_file.write_text(json.dumps({
        "national_v1 vs national_v2": {
            "games": 10,
            "a_wins": 0,
            "b_wins": 0,
            "draws": 10,
            "win_rate": 0.0,
        },
    }), encoding="utf-8")
    monkeypatch.setattr(tool_status, "_infra_path", lambda _name: h2h_file)
    monkeypatch.setattr(evolution_infra, "read_pipeline_checkpoint", lambda: {})

    wrapped = asyncio.run(tool_status.get_h2h.handler({"bot_name": "national_v1"}))
    result = json.loads(wrapped["content"][0]["text"])

    assert result["opponents"]["national_v2"]["win_rate"] == 0.5
    assert result["opponents"]["national_v2"]["draws"] == 10
    assert result["opponents"]["national_v2"]["tag"] == "neutral"
