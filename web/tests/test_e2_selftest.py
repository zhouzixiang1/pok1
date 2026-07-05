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
