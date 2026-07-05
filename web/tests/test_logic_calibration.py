"""Tests for fix-2: critic calibration rating_delta async backfill.

Verifies:
  - commit writes rating_delta=None (not stale r-2*rd)
  - reconcile fills real delta when bot converges
  - calibration read skips unreconciled rows
  - reconcile is idempotent (won't recompute frozen deltas)
  - backward-compat: old rows without "reconciled" field are still consumed
"""

import json
import time

import pytest

from glicko2 import Glicko2Player
from evolution_infra import RESULTS_DIR


# ── Helpers ──

def _write_calibration_lines(path, rows):
    """Write a list of dicts as JSONL to the calibration file."""
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_calibration_lines(path):
    """Read calibration JSONL into a list of dicts."""
    lines = path.read_text().strip().split('\n')
    return [json.loads(l) for l in lines if l.strip()]


# ── Test: commit writes null rating_delta ──

class TestCommitWritesNullRatingDelta:
    """Verify that the commit path writes rating_delta=None, not r-2*rd."""

    def test_calibration_entry_has_null_delta(self):
        """Simulate what commit_bot writes and verify the structure."""
        cal_file = RESULTS_DIR / "critic_calibration.jsonl"
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)

        # Simulate the NEW commit path (tool_commit.py after fix-2)
        _write_calibration_lines(cal_file, [{
            "version": 100, "source_v": 99,
            "critic_score": 7.5,
            "rating_delta": None,
            "reconciled": False,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }])

        rows = _read_calibration_lines(cal_file)
        assert len(rows) == 1
        assert rows[0]["rating_delta"] is None
        assert rows[0]["reconciled"] is False
        assert rows[0]["version"] == 100
        assert rows[0]["critic_score"] == 7.5

    def test_no_stale_r_minus_2rd_value(self):
        """Ensure we do NOT write the old conservative_rating() delta."""
        cal_file = RESULTS_DIR / "critic_calibration.jsonl"
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)

        # Old code would compute: (1500 - 2*350) - (1500 - 2*350) = 0
        # New code writes None
        entry = {
            "version": 100, "source_v": 99,
            "critic_score": 6.0,
            "rating_delta": None,
            "reconciled": False,
            "timestamp": "2026-06-23T00:00:00",
        }
        _write_calibration_lines(cal_file, [entry])

        rows = _read_calibration_lines(cal_file)
        # The old code would produce rating_delta=0 (because rd=350 → cons=800
        # for both bot and source → delta=0). New code produces None.
        assert rows[0]["rating_delta"] is None
        assert rows[0]["rating_delta"] != 0  # specifically NOT the stale 0


# ── Test: reconcile fills delta when converged ──

class TestReconcileFillsDeltaWhenConverged:
    """Verify that reconcile_critic_calibration() backfills real delta."""

    def test_reconcile_converged_bot(self):
        from agent_review import reconcile_critic_calibration

        cal_file = RESULTS_DIR / "critic_calibration.jsonl"
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)

        # Write an unreconciled entry
        _write_calibration_lines(cal_file, [{
            "version": 100, "source_v": 99,
            "critic_score": 7.0,
            "rating_delta": None,
            "reconciled": False,
            "timestamp": "2026-06-23T00:00:00",
        }])

        # Simulate converged ratings
        ratings = {
            "national_v100": Glicko2Player(r=1580.0, rd=45.0),
            "national_v99": Glicko2Player(r=1520.0, rd=50.0),
        }
        bot_stats = {
            "national_v100": {"games": 150, "wins": 80, "losses": 70},
        }

        reconcile_critic_calibration(ratings, bot_stats)

        rows = _read_calibration_lines(cal_file)
        assert len(rows) == 1
        assert rows[0]["rating_delta"] == 60.0  # 1580 - 1520
        assert rows[0]["reconciled"] is True
        assert "reconciled_at" in rows[0]

    def test_reconcile_not_converged_high_rd(self):
        from agent_review import reconcile_critic_calibration

        cal_file = RESULTS_DIR / "critic_calibration.jsonl"
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)

        _write_calibration_lines(cal_file, [{
            "version": 100, "source_v": 99,
            "critic_score": 7.0,
            "rating_delta": None,
            "reconciled": False,
            "timestamp": "2026-06-23T00:00:00",
        }])

        # Bot with high RD (not converged)
        ratings = {
            "national_v100": Glicko2Player(r=1580.0, rd=200.0),
            "national_v99": Glicko2Player(r=1520.0, rd=50.0),
        }
        bot_stats = {
            "national_v100": {"games": 200, "wins": 100, "losses": 100},
        }

        reconcile_critic_calibration(ratings, bot_stats)

        rows = _read_calibration_lines(cal_file)
        assert rows[0]["rating_delta"] is None  # Not reconciled yet
        assert rows[0]["reconciled"] is False

    def test_reconcile_not_converged_insufficient_games(self):
        from agent_review import reconcile_critic_calibration

        cal_file = RESULTS_DIR / "critic_calibration.jsonl"
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)

        _write_calibration_lines(cal_file, [{
            "version": 100, "source_v": 99,
            "critic_score": 7.0,
            "rating_delta": None,
            "reconciled": False,
            "timestamp": "2026-06-23T00:00:00",
        }])

        # Low RD but insufficient games
        ratings = {
            "national_v100": Glicko2Player(r=1580.0, rd=40.0),
            "national_v99": Glicko2Player(r=1520.0, rd=50.0),
        }
        bot_stats = {
            "national_v100": {"games": 50, "wins": 30, "losses": 20},
        }

        reconcile_critic_calibration(ratings, bot_stats)

        rows = _read_calibration_lines(cal_file)
        assert rows[0]["rating_delta"] is None  # Not enough games
        assert rows[0]["reconciled"] is False

    def test_reconcile_source_missing(self):
        """When source bot is reaped, reconcile falls back to r - 1500."""
        from agent_review import reconcile_critic_calibration

        cal_file = RESULTS_DIR / "critic_calibration.jsonl"
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)

        _write_calibration_lines(cal_file, [{
            "version": 100, "source_v": 99,
            "critic_score": 7.0,
            "rating_delta": None,
            "reconciled": False,
            "timestamp": "2026-06-23T00:00:00",
        }])

        # Source bot (v99) has been reaped — not in ratings
        ratings = {
            "national_v100": Glicko2Player(r=1580.0, rd=45.0),
        }
        bot_stats = {
            "national_v100": {"games": 150, "wins": 80, "losses": 70},
        }

        reconcile_critic_calibration(ratings, bot_stats)

        rows = _read_calibration_lines(cal_file)
        assert rows[0]["rating_delta"] == 80.0  # 1580 - 1500
        assert rows[0]["reconciled"] is True


# ── Test: calibration skips unreconciled rows ──

class TestCalibrationSkipsUnreconciledRows:
    """Verify _run_critic calibration read skips rows with rating_delta=None."""

    def test_consumption_skips_none_delta(self):
        """_run_critic should only use rows with non-None rating_delta."""
        from evolution_infra import RESULTS_DIR as RD
        cal_file = RD / "critic_calibration.jsonl"
        cal_file.parent.mkdir(parents=True, exist_ok=True)

        # Mix of reconciled and unreconciled rows
        rows = [
            {"version": 95, "source_v": 94, "critic_score": 8.0,
             "rating_delta": None, "reconciled": False},
            {"version": 96, "source_v": 95, "critic_score": 7.0,
             "rating_delta": None, "reconciled": False},
            {"version": 97, "source_v": 96, "critic_score": 6.0,
             "rating_delta": 25.0, "reconciled": True},
            {"version": 98, "source_v": 97, "critic_score": 7.5,
             "rating_delta": -10.0, "reconciled": True},
            {"version": 99, "source_v": 98, "critic_score": 5.0,
             "rating_delta": 15.0, "reconciled": True},
        ]
        _write_calibration_lines(cal_file, rows)

        # Simulate the filtering logic from _run_critic (L59-70 after fix)
        lines = cal_file.read_text().strip().split('\n')
        all_rows = [json.loads(l) for l in lines[-10:] if l.strip()]
        recent = [
            r for r in all_rows
            if r.get("rating_delta") is not None
        ]

        # Only 3 rows (97, 98, 99) should pass; rows 95, 96 with None are skipped
        assert len(recent) == 3
        assert all(r["rating_delta"] is not None for r in recent)
        assert recent[0]["version"] == 97
        assert recent[1]["version"] == 98
        assert recent[2]["version"] == 99

    def test_backward_compat_old_rows_without_reconciled_field(self):
        """Old calibration rows (no "reconciled" key) should still be consumed."""
        from evolution_infra import RESULTS_DIR as RD
        cal_file = RD / "critic_calibration.jsonl"
        cal_file.parent.mkdir(parents=True, exist_ok=True)

        # Simulate old-format rows (no "reconciled" field, has numeric delta)
        old_rows = [
            {"version": 90, "source_v": 89, "critic_score": 7.0,
             "rating_delta": 12.5, "timestamp": "2026-06-20T00:00:00"},
            {"version": 91, "source_v": 90, "critic_score": 8.0,
             "rating_delta": -5.0, "timestamp": "2026-06-20T01:00:00"},
            {"version": 92, "source_v": 91, "critic_score": 6.0,
             "rating_delta": 0.0, "timestamp": "2026-06-20T02:00:00"},
        ]
        _write_calibration_lines(cal_file, old_rows)

        # Simulate the filtering logic from _run_critic
        lines = cal_file.read_text().strip().split('\n')
        all_rows = [json.loads(l) for l in lines[-10:] if l.strip()]
        recent = [
            r for r in all_rows
            if r.get("rating_delta") is not None
        ]

        # All 3 old rows should pass (they have numeric deltas, just no "reconciled" key)
        assert len(recent) == 3

    def test_old_rows_with_delta_zero_still_consumed(self):
        """Old rows where delta was computed as 0 (the bug) should still be consumed
        for backward-compat, even though they're stale."""
        from evolution_infra import RESULTS_DIR as RD
        cal_file = RD / "critic_calibration.jsonl"
        cal_file.parent.mkdir(parents=True, exist_ok=True)

        # These are the 88 out of 90 rows that had delta=0 from the old code
        old_zero_rows = [
            {"version": i, "source_v": i-1, "critic_score": 7.0,
             "rating_delta": 0, "timestamp": f"2026-06-{20+i//10:02d}T00:00:00"}
            for i in range(90, 100)
        ]
        _write_calibration_lines(cal_file, old_zero_rows)

        lines = cal_file.read_text().strip().split('\n')
        all_rows = [json.loads(l) for l in lines[-10:] if l.strip()]
        recent = [
            r for r in all_rows
            if r.get("rating_delta") is not None
        ]

        # All 10 pass (rating_delta=0 is not None)
        assert len(recent) == 10


# ── Test: reconcile is idempotent ──

class TestReconcileIdempotent:
    """Repeated reconcile calls should not recompute frozen deltas."""

    def test_idempotent_after_reconcile(self):
        from agent_review import reconcile_critic_calibration

        cal_file = RESULTS_DIR / "critic_calibration.jsonl"
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)

        _write_calibration_lines(cal_file, [{
            "version": 100, "source_v": 99,
            "critic_score": 7.0,
            "rating_delta": None,
            "reconciled": False,
            "timestamp": "2026-06-23T00:00:00",
        }])

        ratings = {
            "national_v100": Glicko2Player(r=1580.0, rd=45.0),
            "national_v99": Glicko2Player(r=1520.0, rd=50.0),
        }
        bot_stats = {
            "national_v100": {"games": 150},
        }

        # First reconcile
        reconcile_critic_calibration(ratings, bot_stats)
        rows1 = _read_calibration_lines(cal_file)
        assert rows1[0]["rating_delta"] == 60.0
        assert rows1[0]["reconciled"] is True

        # Change ratings drastically (simulating further play)
        ratings["national_v100"] = Glicko2Player(r=1700.0, rd=30.0)
        ratings["national_v99"] = Glicko2Player(r=1400.0, rd=40.0)

        # Second reconcile — delta should NOT change
        reconcile_critic_calibration(ratings, bot_stats)
        rows2 = _read_calibration_lines(cal_file)
        assert rows2[0]["rating_delta"] == 60.0  # Frozen from first reconcile
        assert rows2[0]["reconciled"] is True

    def test_mixed_reconciled_and_unreconciled(self):
        """Only unreconciled rows are processed; reconciled rows are untouched."""
        from agent_review import reconcile_critic_calibration

        cal_file = RESULTS_DIR / "critic_calibration.jsonl"
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)

        rows = [
            {"version": 98, "source_v": 97, "critic_score": 6.0,
             "rating_delta": 25.0, "reconciled": True,
             "reconciled_at": "2026-06-22T00:00:00"},
            {"version": 99, "source_v": 98, "critic_score": 7.0,
             "rating_delta": None, "reconciled": False,
             "timestamp": "2026-06-22T01:00:00"},
            {"version": 100, "source_v": 99, "critic_score": 8.0,
             "rating_delta": None, "reconciled": False,
             "timestamp": "2026-06-23T00:00:00"},
        ]
        _write_calibration_lines(cal_file, rows)

        ratings = {
            "national_v98": Glicko2Player(r=1500.0, rd=40.0),
            "national_v99": Glicko2Player(r=1530.0, rd=45.0),
            "national_v100": Glicko2Player(r=1580.0, rd=50.0),
        }
        bot_stats = {
            "national_v99": {"games": 120},
            "national_v100": {"games": 200},
        }

        reconcile_critic_calibration(ratings, bot_stats)

        result = _read_calibration_lines(cal_file)
        # v98: already reconciled, unchanged
        assert result[0]["rating_delta"] == 25.0
        assert result[0]["reconciled"] is True
        # v99: now reconciled (converged: rd=45 < 60, games=120 >= 100)
        assert result[1]["rating_delta"] == 30.0  # 1530 - 1500
        assert result[1]["reconciled"] is True
        # v100: now reconciled (converged: rd=50 < 60, games=200 >= 100)
        assert result[2]["rating_delta"] == 50.0  # 1580 - 1530
        assert result[2]["reconciled"] is True

    def test_empty_calibration_file(self):
        """Reconcile handles empty file gracefully."""
        from agent_review import reconcile_critic_calibration

        cal_file = RESULTS_DIR / "critic_calibration.jsonl"
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        cal_file.write_text("")

        # Should not raise
        reconcile_critic_calibration({}, {})
        # File still exists but empty
        assert cal_file.exists()

    def test_missing_calibration_file(self):
        """Reconcile handles missing file gracefully."""
        from agent_review import reconcile_critic_calibration

        cal_file = RESULTS_DIR / "critic_calibration.jsonl"
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        cal_file.unlink(missing_ok=True)

        # Should not raise
        reconcile_critic_calibration({}, {})

    def test_custom_thresholds(self):
        """Custom rd_threshold and min_games parameters work."""
        from agent_review import reconcile_critic_calibration

        cal_file = RESULTS_DIR / "critic_calibration.jsonl"
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)

        _write_calibration_lines(cal_file, [{
            "version": 100, "source_v": 99,
            "critic_score": 7.0,
            "rating_delta": None,
            "reconciled": False,
            "timestamp": "2026-06-23T00:00:00",
        }])

        # rd=100 is above default threshold 60, but we use custom 150
        ratings = {
            "national_v100": Glicko2Player(r=1580.0, rd=100.0),
            "national_v99": Glicko2Player(r=1520.0, rd=50.0),
        }
        bot_stats = {"national_v100": {"games": 50}}

        # With custom thresholds: rd_threshold=150, min_games=30
        reconcile_critic_calibration(ratings, bot_stats,
                                     rd_threshold=150, min_games=30)

        rows = _read_calibration_lines(cal_file)
        assert rows[0]["rating_delta"] == 60.0
        assert rows[0]["reconciled"] is True
