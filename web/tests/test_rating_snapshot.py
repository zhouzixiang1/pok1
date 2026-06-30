"""Tests for unified rating/strength snapshots."""

import json


def _write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_rebuilds_active_h2h_from_match_history(tmp_path):
    from rating_snapshot import choose_h2h_source

    active = ["claude_v1", "claude_v2", "claude_v3"]
    stored = {
        "claude_v1 vs claude_v2": {"games": 50, "a_wins": 50, "b_wins": 0, "draws": 0},
    }
    history = tmp_path / "match_history.jsonl"
    _write_jsonl(history, [
        {"bot0": "claude_v1", "bot1": "claude_v2", "bot0_wins": 10, "bot1_wins": 40, "draws": 0},
        {"bot0": "claude_v1", "bot1": "claude_v3", "bot0_wins": 10, "bot1_wins": 40, "draws": 0},
        {"bot0": "claude_v2", "bot1": "claude_v3", "bot0_wins": 40, "bot1_wins": 10, "draws": 0},
    ])

    selected = choose_h2h_source(active, stored, history)

    assert selected["source"] == "match_history_rebuilt"
    assert selected["coverage"]["covered_pairs"] == 3
    assert selected["stored_coverage"]["covered_pairs"] == 1
    assert selected["h2h"]["claude_v1 vs claude_v2"]["a_wins"] == 10


def test_strength_rows_sort_by_rebuilt_active_pool_not_sparse_stored_h2h(tmp_path):
    from rating_snapshot import build_strength_rows

    ratings = {
        "claude_v1": {"r": 1500, "rd": 80, "sigma": 0.06},
        "claude_v2": {"r": 1500, "rd": 80, "sigma": 0.06},
        "claude_v3": {"r": 1500, "rd": 80, "sigma": 0.06},
    }
    stored = {
        "claude_v1 vs claude_v2": {"games": 10, "a_wins": 10, "b_wins": 0, "draws": 0},
    }
    stats = {name: {"games": 100, "win_rate": 0.5} for name in ratings}
    history = tmp_path / "match_history.jsonl"
    _write_jsonl(history, [
        {"bot0": "claude_v1", "bot1": "claude_v2", "bot0_wins": 10, "bot1_wins": 40, "draws": 0},
        {"bot0": "claude_v1", "bot1": "claude_v3", "bot0_wins": 10, "bot1_wins": 40, "draws": 0},
        {"bot0": "claude_v2", "bot1": "claude_v3", "bot0_wins": 40, "bot1_wins": 10, "draws": 0},
    ])

    rows = build_strength_rows(ratings, stats, stored, active_bots=list(ratings), match_history_path=history)

    assert rows[0]["name"] == "claude_v2"
    assert rows[0]["h2h_source"] == "match_history_rebuilt"
    assert rows[0]["h2h_coverage"] == 1.0
    assert rows[0]["h2h_avg_wr"] == 0.8
    assert rows[0]["rank_basis"] == "active_h2h_plus_conservative"


def test_h2h_winrate_counts_draws_as_half():
    from rating_snapshot import h2h_winrate_for_bot

    h2h = {
        "claude_v1 vs claude_v2": {"games": 10, "a_wins": 4, "b_wins": 3, "draws": 3},
    }

    assert h2h_winrate_for_bot("claude_v1", h2h) == 0.55
    assert h2h_winrate_for_bot("claude_v2", h2h) == 0.45


def test_low_confidence_rows_get_selection_penalty():
    from rating_snapshot import build_strength_rows

    ratings = {
        "claude_v1": {"r": 1600, "rd": 220, "sigma": 0.06},
        "claude_v2": {"r": 1500, "rd": 80, "sigma": 0.06},
    }
    h2h = {
        "claude_v1 vs claude_v2": {"games": 100, "a_wins": 50, "b_wins": 50, "draws": 0},
    }

    rows = {row["name"]: row for row in build_strength_rows(ratings, {}, h2h)}

    assert rows["claude_v1"]["strength_confidence"] == "low"
    assert rows["claude_v1"]["selection_penalty"] == 0.03
    assert rows["claude_v1"]["selection_score"] == round(rows["claude_v1"]["leaderboard_score"] - 0.03, 4)
    assert rows["claude_v2"]["selection_penalty"] == 0.0
