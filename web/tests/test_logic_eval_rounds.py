"""Logic tests for eval_rounds.py — EvalRoundManager deterministic evaluation rounds."""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Import pair_key logic inline (mirrors evolution_infra.pair_key) so tests
# can validate behavior without importing the full evolution_infra module,
# which has heavy side-effects (logging, file I/O, git operations).
def _pair_key(a, b):
    return f"{a} vs {b}" if a < b else f"{b} vs {a}"


def _locked_file(path, mode="r", **kwargs):
    """Simplified locked_file that just opens without fcntl (test-safe)."""
    class _Ctx:
        def __init__(self, p, m):
            self._path = p
            self._mode = m
            self._f = None
        def __enter__(self):
            self._f = open(self._path, self._mode)
            return self._f
        def __exit__(self, *exc):
            if self._f:
                self._f.close()
    return _Ctx(path, mode)


# Patch evolution_infra BEFORE importing eval_rounds, so the module-level
# constants and function references are resolved against our mocks.
# We do this with a manual sys.modules injection.


@pytest.fixture(autouse=True)
def _patch_evolution_infra(tmp_path):
    """Inject a fake evolution_infra module so eval_rounds can import it."""
    import types
    # Save the REAL module so we can restore it
    real_ei = sys.modules.get("evolution_infra")
    real_eval = sys.modules.get("eval_rounds")

    fake = types.ModuleType("evolution_infra")
    fake.RESULTS_DIR = tmp_path / "results"
    fake.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    fake.pair_key = _pair_key
    fake.locked_file = _locked_file

    # Inject fake module
    sys.modules["evolution_infra"] = fake
    sys.modules.pop("eval_rounds", None)

    yield

    # Cleanup: restore the REAL modules
    sys.modules.pop("eval_rounds", None)
    sys.modules.pop("evolution_infra", None)
    if real_ei is not None:
        sys.modules["evolution_infra"] = real_ei
    if real_eval is not None:
        sys.modules["eval_rounds"] = real_eval


@pytest.fixture
def mgr():
    """Create a fresh EvalRoundManager with patched evolution_infra."""
    import eval_rounds
    return eval_rounds.EvalRoundManager()


@pytest.fixture
def results_dir(tmp_path):
    return tmp_path / "results"


# ── count_game ──

class TestCountGame:

    def test_below_threshold(self, mgr):
        import eval_rounds
        # Default threshold is 500 — count a few and expect False
        for _ in range(10):
            assert mgr.count_game() is False

    def test_hits_threshold(self, mgr):
        import eval_rounds
        assert mgr.count_game(n_games=499) is False
        assert mgr.count_game(n_games=1) is True

    def test_exceeds_threshold(self, mgr):
        import eval_rounds
        assert mgr.count_game(n_games=600) is True
        # Counter should have been incremented even though it exceeded
        assert mgr.games_since_last_round == 600

    def test_returns_false_during_active_round(self, mgr):
        import eval_rounds
        mgr.start_round(["a", "b"])
        assert mgr.count_game(n_games=9999) is False

    def test_resets_after_round_start(self, mgr):
        import eval_rounds
        mgr.count_game(n_games=500)
        assert mgr.games_since_last_round == 500
        mgr.start_round(["a", "b"])
        assert mgr.games_since_last_round == 0


# ── start_round ──

class TestStartRound:

    def test_deterministic_pair_order(self, mgr):
        pairs = mgr.start_round(["charlie", "alice", "bob"])
        names = [p[0] for p in pairs]
        assert names == sorted(names)

    def test_pair_count(self, mgr):
        bots = ["a", "b", "c", "d"]
        pairs = mgr.start_round(bots)
        assert len(pairs) == 6  # C(4,2) = 6

    def test_single_bot_no_pairs(self, mgr):
        pairs = mgr.start_round(["solo"])
        assert pairs == []

    def test_active_after_start(self, mgr):
        mgr.start_round(["a", "b"])
        assert mgr.is_active is True


# ── record_result ──

class TestRecordResult:

    def test_noop_when_no_round(self, mgr):
        # Should not raise
        mgr.record_result("a", "b", 5, 3, 2)

    def test_records_data(self, mgr):
        mgr.start_round(["a", "b"])
        mgr.record_result("a", "b", 7, 3, 0)
        k = _pair_key("a", "b")
        assert mgr.round_data[k]["wins_a"] == 7
        assert mgr.round_data[k]["wins_b"] == 3
        assert mgr.round_data[k]["games"] == 10

    def test_accumulates_multiple_records(self, mgr):
        mgr.start_round(["a", "b"])
        mgr.record_result("a", "b", 5, 3, 2)
        mgr.record_result("a", "b", 4, 6, 0)
        k = _pair_key("a", "b")
        assert mgr.round_data[k]["games"] == 20
        assert mgr.round_data[k]["wins_a"] == 9

    def test_zero_total_ignored(self, mgr):
        mgr.start_round(["a", "b"])
        mgr.record_result("a", "b", 0, 0, 0)
        assert len(mgr.round_data) == 0


# ── is_round_complete ──

class TestIsRoundComplete:

    def test_false_when_no_round(self, mgr):
        assert mgr.is_round_complete() is False

    def test_false_when_pairs_remaining(self, mgr):
        mgr.start_round(["a", "b"])
        assert mgr.is_round_complete() is False

    def test_true_after_enough_games(self, mgr):
        import eval_rounds
        mgr.start_round(["a", "b"])
        # Default min games per pair = 10
        mgr.record_result("a", "b", 6, 4, 0)  # 10 games total
        assert mgr.is_round_complete() is True


# ── finish_round ──

class TestFinishRound:

    def test_returns_none_when_no_round(self, mgr):
        assert mgr.finish_round() is None

    def test_returns_summary(self, mgr, results_dir):
        mgr.start_round(["a", "b"])
        mgr.record_result("a", "b", 6, 4, 0)
        summary = mgr.finish_round()
        assert summary is not None
        assert "round_id" in summary
        assert "bot_deltas" in summary
        assert "a" in summary["bot_deltas"]
        assert "b" in summary["bot_deltas"]

    def test_resets_state_after_finish(self, mgr, results_dir):
        mgr.start_round(["a", "b"])
        mgr.record_result("a", "b", 6, 4, 0)
        mgr.finish_round()
        assert mgr.is_active is False
        assert mgr.round_data == {}
        assert mgr.current_round_id is None

    def test_persists_to_jsonl(self, mgr, results_dir):
        import eval_rounds
        mgr.start_round(["a", "b"])
        mgr.record_result("a", "b", 6, 4, 0)
        mgr.finish_round()

        jsonl_path = results_dir / "eval_rounds.jsonl"
        assert jsonl_path.exists()
        lines = jsonl_path.read_text().strip().split("\n")
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert "round_id" in data

    def test_delta_computation(self, mgr, results_dir):
        """With historical H2H data passed in, deltas should be computed."""
        mgr.start_round(["a", "b"])
        mgr.record_result("a", "b", 7, 3, 0)

        # Fake H2H data: a historically won 50% vs b
        h2h = {_pair_key("a", "b"): {"games": 100, "a_wins": 50, "b_wins": 50}}
        summary = mgr.finish_round(h2h_data=h2h)

        # Round WR for a = 7/10 = 0.7, historical = 0.5, delta = +0.2
        assert summary["bot_deltas"]["a"]["avg_delta"] == pytest.approx(0.2, abs=0.01)
        assert summary["bot_deltas"]["b"]["avg_delta"] == pytest.approx(-0.2, abs=0.01)

    def test_total_rounds_completed_increments(self, mgr, results_dir):
        mgr.start_round(["a", "b"])
        mgr.record_result("a", "b", 6, 4, 0)
        mgr.finish_round()
        assert mgr.total_rounds_completed == 1


# ── cancel_round ──

class TestCancelRound:

    def test_cancels_active_round(self, mgr):
        mgr.start_round(["a", "b"])
        assert mgr.is_active is True
        mgr.cancel_round()
        assert mgr.is_active is False
        assert mgr.round_data == {}

    def test_noop_when_no_round(self, mgr):
        mgr.cancel_round()  # Should not raise


# ── get_last_round_summary ──

class TestGetLastRoundSummary:

    def test_empty_when_no_rounds(self, mgr):
        assert mgr.get_last_round_summary("a") == ""

    def test_returns_summary_for_bot(self, mgr, results_dir):
        mgr.start_round(["alpha", "beta"])
        mgr.record_result("alpha", "beta", 7, 3, 0)
        mgr.finish_round()

        summary = mgr.get_last_round_summary("alpha")
        assert "alpha" in summary
        assert "avg_wr=" in summary
        assert "delta=" in summary

    def test_empty_for_unknown_bot(self, mgr, results_dir):
        mgr.start_round(["alpha", "beta"])
        mgr.record_result("alpha", "beta", 7, 3, 0)
        mgr.finish_round()

        assert mgr.get_last_round_summary("unknown") == ""

    def test_max_chars_truncation(self, mgr, results_dir):
        mgr.start_round(["alpha", "beta"])
        mgr.record_result("alpha", "beta", 7, 3, 0)
        mgr.finish_round()

        summary = mgr.get_last_round_summary("alpha", max_chars=20)
        assert len(summary) <= 20
        assert summary.endswith("...") or len(summary) <= 20
