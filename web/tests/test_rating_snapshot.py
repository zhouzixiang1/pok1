"""Tests for unified rating/strength snapshots."""

import json

import pytest


IDENTITY = "a" * 64


def _write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _history_row(
    bot0,
    bot1,
    bot0_wins,
    bot1_wins,
    draws=0,
    *,
    identity=IDENTITY,
    epoch="national_tcp_policy_v1",
    execution_mode="native_tcp",
):
    samples = [100] * bot0_wins + [-100] * bot1_wins + [0] * draws
    return {
        "bot0": bot0,
        "bot1": bot1,
        "bot0_wins": bot0_wins,
        "bot1_wins": bot1_wins,
        "draws": draws,
        "evaluation_epoch": epoch,
        "execution_mode": execution_mode,
        "evaluation_identity_digest": identity,
        "strength_sample_unit": "70_hand_match",
        "hands_per_strength_sample": 70,
        "strength_admitted": True,
        "strength_complete": True,
        "strength_compliance_passed": True,
        "strength_sample_count": len(samples),
        "net_chips_bot0": samples,
    }


def test_rebuilds_active_h2h_from_match_history(tmp_path):
    from rating_snapshot import choose_h2h_source

    active = ["national_v143", "national_v144", "national_v145"]
    stored = {
        "national_v143 vs national_v144": {"games": 50, "a_wins": 50, "b_wins": 0, "draws": 0},
    }
    history = tmp_path / "match_history.jsonl"
    _write_jsonl(history, [
        _history_row("national_v143", "national_v144", 10, 40),
        _history_row("national_v143", "national_v145", 10, 40),
        _history_row("national_v144", "national_v145", 40, 10),
    ])

    selected = choose_h2h_source(
        active,
        stored,
        history,
        expected_evaluation_identity_digest=IDENTITY,
    )

    assert selected["source"] == "match_history_rebuilt"
    assert selected["coverage"]["covered_pairs"] == 3
    assert selected["stored_coverage"]["covered_pairs"] == 1
    assert selected["h2h"]["national_v143 vs national_v144"]["a_wins"] == 10


def test_strength_rows_sort_by_rebuilt_active_pool_not_sparse_stored_h2h(tmp_path):
    from rating_snapshot import build_strength_rows

    ratings = {
        "national_v143": {"r": 1500, "rd": 80, "sigma": 0.06},
        "national_v144": {"r": 1500, "rd": 80, "sigma": 0.06},
        "national_v145": {"r": 1500, "rd": 80, "sigma": 0.06},
    }
    stored = {
        "national_v143 vs national_v144": {"games": 10, "a_wins": 10, "b_wins": 0, "draws": 0},
    }
    stats = {name: {"games": 100, "win_rate": 0.5} for name in ratings}
    history = tmp_path / "match_history.jsonl"
    _write_jsonl(history, [
        _history_row("national_v143", "national_v144", 10, 40),
        _history_row("national_v143", "national_v145", 10, 40),
        _history_row("national_v144", "national_v145", 40, 10),
    ])

    rows = build_strength_rows(
        ratings,
        stats,
        stored,
        active_bots=list(ratings),
        match_history_path=history,
        expected_evaluation_identity_digest=IDENTITY,
    )

    assert rows[0]["name"] == "national_v144"
    assert rows[0]["h2h_source"] == "match_history_rebuilt"
    assert rows[0]["h2h_coverage"] == 1.0
    assert rows[0]["h2h_avg_wr"] == 0.8
    assert rows[0]["rank_basis"] == "active_h2h_plus_conservative"


def test_inactive_h2h_rows_cannot_change_active_selection_scores(tmp_path):
    from rating_snapshot import build_strength_rows, choose_h2h_source

    active = ["national_v143", "national_v144"]
    ratings = {
        name: {"r": 1500, "rd": 80, "sigma": 0.06} for name in active
    }
    stats = {name: {"games": 100, "win_rate": 0.5} for name in active}
    active_h2h = {
        "national_v143 vs national_v144": {
            "games": 100,
            "a_wins": 50,
            "b_wins": 50,
            "draws": 0,
        }
    }
    polluted = {
        **active_h2h,
        "national_v143 vs national_v999": {
            "games": 1000,
            "a_wins": 1000,
            "b_wins": 0,
            "draws": 0,
        },
    }
    history = tmp_path / "match_history.jsonl"
    history.write_text("", encoding="utf-8")

    clean_rows = {
        row["name"]: row
        for row in build_strength_rows(
            ratings, stats, active_h2h, active_bots=active, match_history_path=history
        )
    }
    polluted_rows = {
        row["name"]: row
        for row in build_strength_rows(
            ratings, stats, polluted, active_bots=active, match_history_path=history
        )
    }
    selected = choose_h2h_source(
        active,
        polluted,
        history,
        expected_evaluation_identity_digest=IDENTITY,
    )

    assert polluted_rows["national_v143"]["selection_score"] == clean_rows["national_v143"]["selection_score"]
    assert polluted_rows["national_v143"]["h2h_games"] == 100
    assert "national_v143 vs national_v999" not in selected["h2h"]


def test_h2h_winrate_counts_draws_as_half():
    from rating_snapshot import h2h_winrate_for_bot

    h2h = {
        "national_v143 vs national_v144": {"games": 10, "a_wins": 4, "b_wins": 3, "draws": 3},
    }

    assert h2h_winrate_for_bot("national_v143", h2h) == 0.55
    assert h2h_winrate_for_bot("national_v144", h2h) == 0.45


def test_low_confidence_rows_get_selection_penalty():
    from rating_snapshot import build_strength_rows

    ratings = {
        "national_v143": {"r": 1600, "rd": 220, "sigma": 0.06},
        "national_v144": {"r": 1500, "rd": 80, "sigma": 0.06},
    }
    h2h = {
        "national_v143 vs national_v144": {"games": 100, "a_wins": 50, "b_wins": 50, "draws": 0},
    }

    rows = {row["name"]: row for row in build_strength_rows(ratings, {}, h2h)}

    assert rows["national_v143"]["strength_confidence"] == "low"
    assert rows["national_v143"]["selection_penalty"] == 0.03
    assert rows["national_v143"]["selection_score"] == round(rows["national_v143"]["leaderboard_score"] - 0.03, 4)
    assert rows["national_v144"]["selection_penalty"] == 0.0


@pytest.mark.parametrize(
    ("override", "value"),
    [
        ("identity", None),
        ("identity", "b" * 64),
        ("epoch", "national_native_v1"),
        ("execution_mode", "official_exe"),
        ("execution_mode", "national_arena"),
    ],
)
def test_foreign_history_cannot_change_h2h_chips_or_selection(
    tmp_path,
    override,
    value,
):
    from rating_snapshot import (
        build_strength_rows,
        national_chip_metrics_from_match_history,
        reconstruct_h2h_from_match_history,
    )

    active = ["national_v143", "national_v144"]
    valid = _history_row(active[0], active[1], 1, 0)
    poison_kwargs = {override: value}
    poison = _history_row(active[0], active[1], 0, 20, **poison_kwargs)
    clean_history = tmp_path / "clean.jsonl"
    polluted_history = tmp_path / "polluted.jsonl"
    _write_jsonl(clean_history, [valid])
    _write_jsonl(polluted_history, [valid, poison])

    clean_h2h = reconstruct_h2h_from_match_history(
        active,
        clean_history,
        expected_evaluation_identity_digest=IDENTITY,
    )
    polluted_h2h = reconstruct_h2h_from_match_history(
        active,
        polluted_history,
        expected_evaluation_identity_digest=IDENTITY,
    )
    clean_chips = national_chip_metrics_from_match_history(
        active,
        clean_history,
        expected_evaluation_identity_digest=IDENTITY,
    )
    polluted_chips = national_chip_metrics_from_match_history(
        active,
        polluted_history,
        expected_evaluation_identity_digest=IDENTITY,
    )
    ratings = {
        name: {"r": 1500, "rd": 80, "sigma": 0.06} for name in active
    }
    clean_rows = build_strength_rows(
        ratings,
        {},
        {},
        active_bots=active,
        match_history_path=clean_history,
        expected_evaluation_identity_digest=IDENTITY,
    )
    polluted_rows = build_strength_rows(
        ratings,
        {},
        {},
        active_bots=active,
        match_history_path=polluted_history,
        expected_evaluation_identity_digest=IDENTITY,
    )

    assert polluted_h2h == clean_h2h
    assert polluted_chips == clean_chips
    assert polluted_rows == clean_rows


def test_missing_expected_identity_disables_all_history_influence(tmp_path):
    from rating_snapshot import build_strength_rows

    active = ["national_v143", "national_v144"]
    history = tmp_path / "match_history.jsonl"
    _write_jsonl(history, [_history_row(active[0], active[1], 20, 0)])
    ratings = {
        name: {"r": 1500, "rd": 80, "sigma": 0.06} for name in active
    }

    rows = build_strength_rows(
        ratings,
        {},
        {},
        active_bots=active,
        match_history_path=history,
    )

    assert all(row["h2h_games"] == 0 for row in rows)
    assert all(row["strength_sample_count"] == 0 for row in rows)
    assert all(
        row["h2h_source"] == "head_to_head_history_identity_unavailable"
        for row in rows
    )
