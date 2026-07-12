"""Self-tests for the diagnosis.md unfixed-bug fixes (C1, C3, C4, E1, E2).

These exercise the NEW branches added by the fixes so they are not left
uncovered. Pure-logic / data tests — no LLM, no subprocesses.
"""
import json, os, tempfile
from pathlib import Path


def test_C1_try_set_running_cancels_stale_task():
    """try_set_running(True) must cancel a leftover not-done task."""
    import asyncio
    from server.state import AppState

    class FakeTask:
        def __init__(self):
            self.cancelled = False
            self._done = False
        def cancel(self):
            self.cancelled = True
        def done(self):
            return self._done

    s = AppState()
    stale = FakeTask()
    s.set_task(stale)
    assert s.try_set_running(True) is True            # flip False->True
    assert stale.cancelled is True                     # C1: stale task cancelled
    # After flip, internal handle is cleared so set_task can replace cleanly
    assert s.try_set_running(True) is False            # already running -> no-op
    s.set_running(False)


def test_E1_isolated_recent_snaps_filters_cross_run():
    """_isolated_recent_snaps keeps only the latest run's contiguous block."""
    from combined_analyst import _isolated_recent_snaps
    from evolution_infra import RESULTS_DIR

    history = RESULTS_DIR / "rating_history.jsonl"
    backup = None
    created = not history.exists()
    try:
        if history.exists():
            backup = history.read_text()
        # Mix of run ids: older run A (periods 1-3), newer run B (periods 10-12)
        lines = []
        for p in (1, 2, 3):
            lines.append(json.dumps({"period": p, "daemon_run_id": "runA", "ratings": {}}))
        for p in (10, 11, 12):
            lines.append(json.dumps({"period": p, "daemon_run_id": "runB", "ratings": {}}))
        history.write_text("\n".join(lines) + "\n")

        snaps = _isolated_recent_snaps(max_n=10)
        periods = [s["period"] for s in snaps]
        assert periods == [10, 11, 12], periods          # E1: only run B kept
    finally:
        if backup is not None:
            history.write_text(backup)
        elif created:
            history.unlink(missing_ok=True)


def test_E1_isolated_legacy_fallback_no_runid():
    """Legacy snapshots without daemon_run_id fall back to last-N verbatim."""
    from combined_analyst import _isolated_recent_snaps
    from evolution_infra import RESULTS_DIR

    history = RESULTS_DIR / "rating_history.jsonl"
    backup = None
    created = not history.exists()
    try:
        if history.exists():
            backup = history.read_text()
        lines = [json.dumps({"period": i, "ratings": {}}) for i in range(1, 15)]
        history.write_text("\n".join(lines) + "\n")
        snaps = _isolated_recent_snaps(max_n=10)
        assert len(snaps) == 10
        assert [s["period"] for s in snaps] == list(range(5, 15))
    finally:
        if backup is not None:
            history.write_text(backup)
        elif created:
            history.unlink(missing_ok=True)


def test_combined_analysis_accepts_frozen_h2h_snapshot(monkeypatch):
    """The generation-scoped H2H path must execute before any analyst LLM call."""
    import asyncio
    import combined_analyst
    import rating_snapshot

    captured = {}

    def fake_strength_rows(
        ratings,
        bot_stats,
        h2h_data,
        active_bots,
        *,
        match_history_path,
    ):
        captured["ratings"] = ratings
        captured["h2h_data"] = h2h_data
        captured["active_bots"] = active_bots
        captured["match_history_path"] = match_history_path
        return [{
            "name": "national_v142",
            "selection_score": 0.5,
            "leaderboard_score": 0.5,
            "h2h_avg_wr": 0.5,
            "h2h_coverage": 1.0,
            "h2h_opponents": 1,
            "h2h_opponents_total": 1,
            "h2h_games": 20,
        }]

    monkeypatch.setattr(rating_snapshot, "build_strength_rows", fake_strength_rows)
    monkeypatch.setattr(
        combined_analyst,
        "_statistical_stagnation_check",
        lambda *_args, **_kwargs: (False, "high", 25.0),
    )

    frozen_h2h = {
        "national_v141 vs national_v142": {
            "games": 20,
            "a_wins": 9,
            "b_wins": 11,
            "draws": 0,
            "win_rate": 0.45,
        }
    }
    result = asyncio.run(combined_analyst._run_combined_analysis(
        source_v=142,
        active_bots=["national_v142"],
        ratings={},
        ui=None,
        h2h_data=frozen_h2h,
    ))

    assert result["trend"] == "improving"
    assert captured["h2h_data"] == frozen_h2h
    assert captured["active_bots"] == ["national_v142"]
    assert captured["match_history_path"] == Path("/dev/null")


def test_combined_analysis_frozen_rows_preserve_real_low_coverage(monkeypatch):
    """Canonical h2h_* fields must not degrade to the old 0/0=100% default."""
    import asyncio
    import combined_analyst
    from glicko2 import Glicko2Player

    async def must_not_call_llm(*_args, **_kwargs):
        raise AssertionError("low-coverage frozen evidence must stop before the LLM")

    monkeypatch.setattr(combined_analyst, "run_claude_query", must_not_call_llm)
    active = ["national_v140", "national_v141", "national_v142"]
    ratings = {
        name: Glicko2Player(r=1500.0, rd=90.0, sigma=0.06) for name in active
    }
    frozen_h2h = {
        "national_v140 vs national_v142": {
            "games": 15,
            "a_wins": 7,
            "b_wins": 8,
            "draws": 0,
        }
    }
    frozen_stats = {
        "national_v142": {"games": 15, "win_rate": 8 / 15},
    }

    result = asyncio.run(
        combined_analyst._run_combined_analysis(
            source_v=142,
            active_bots=active,
            ratings=ratings,
            ui=None,
            h2h_data=frozen_h2h,
            bot_stats_data=frozen_stats,
        )
    )

    assert result["confidence"] == "low"
    assert "Insufficient opponent coverage: 1/2 (50%)" in result["reason"]


class _FakeRating:
    def __init__(self, cons):
        self._cons = cons
    def conservative_rating(self):
        return self._cons


def test_E2_oscillation_suppressed_when_leader_in_set(monkeypatch):
    """If the Glicko leader is within the oscillating set, do NOT force crossover."""
    import generation_scheduler as gs

    # Pretend last 8 sources all came from {30, 31, 32} (<=3 unique -> would oscillate)
    monkeypatch.setattr(gs, "_read_source_v_history",
                        lambda: [30, 31, 32, 30, 31, 32, 30, 31])
    ratings = {
        "national_v30": _FakeRating(1500.0),   # leader (highest cons)
        "national_v31": _FakeRating(1400.0),
        "national_v32": _FakeRating(1350.0),
    }
    combined = {"is_stagnant": False, "confidence": "low",
                "recommended_source": "", "branch_from": None}
    result = gs._decide_strategy(combined, current_v=33, ratings=ratings)
    # E2: leader v30 is in the oscillating set -> suppressed, NOT crossover
    assert result[0] != "crossover", f"expected no crossover, got {result}"


def test_E2_oscillation_forces_crossover_when_leader_outside_set(monkeypatch):
    """If the leader is NOT in the oscillating set, crossover still fires."""
    import generation_scheduler as gs

    monkeypatch.setattr(gs, "_read_source_v_history",
                        lambda: [30, 31, 32, 30, 31, 32, 30, 31])
    monkeypatch.setattr(gs, "_active_source_versions", lambda *_args: {30, 31, 32, 40})
    monkeypatch.setattr(gs, "_get_unified_leader_v", lambda *_args: 40)
    monkeypatch.setattr(gs, "_pick_oscillation_breakout_source", lambda *_args: None)
    ratings = {
        "national_v30": _FakeRating(1300.0),
        "national_v31": _FakeRating(1400.0),
        "national_v32": _FakeRating(1350.0),
        "national_v40": _FakeRating(1600.0),   # leader, NOT in oscillating set
    }
    combined = {"is_stagnant": False, "confidence": "low",
                "recommended_source": "", "branch_from": None}
    result = gs._decide_strategy(combined, current_v=41, ratings=ratings)
    assert result[0] == "crossover", f"expected crossover, got {result}"
