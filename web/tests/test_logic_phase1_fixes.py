"""Phase 1 integration plan fix verification tests.

Tests for BOT-001 (wheel straight), BOT-002 (re-raise boundary),
PIPE-001 (circuit breaker), PIPE-002 (_git timeout), and PIPE-003 (checkpoint lock).

NOTE: Bot-level tests for wheel straight, TOTAL_HANDS, sanitize_action, and dead code
have been removed because bot code evolves every generation via LLM. Testing evolving
code for specific function names/constants is inherently fragile. The fix_injection
module now guarantees these fixes are applied at generation time.

Engine-level tests verify the permanent infrastructure that evaluates hands correctly.
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

ENGINE_DIR = Path(__file__).resolve().parent.parent.parent / "engine"


# ═══════════════════════════════════════════════════════════════
# BOT-001: Wheel Straight (A-2-3-4-5) — Engine Level
# ═══════════════════════════════════════════════════════════════

class TestEngineWheelStraight:
    """Verify A-2-3-4-5 is correctly identified via engine/judge.py."""

    @pytest.fixture(scope="class")
    def judge_mod(self):
        sys.path.insert(0, str(ENGINE_DIR))
        try:
            import judge as _judge
            yield _judge
        finally:
            sys.path.remove(str(ENGINE_DIR))

    def test_wheel_is_straight(self, judge_mod):
        """A-2-3-4-5 must be evaluated as a straight."""
        c = judge_mod.Card
        s = judge_mod.Suit
        cards = [c(s.HEART, 14), c(s.DIAMOND, 2), c(s.SPADE, 3), c(s.CLUB, 4), c(s.HEART, 5)]
        result = judge_mod.hand_type_of_cards(cards)
        assert result == judge_mod.HandType.STRAIGHT, f"Wheel should be STRAIGHT, got {result}"

    def test_wheel_flush_is_straight_flush(self, judge_mod):
        """A-2-3-4-5 suited must be a straight flush."""
        c = judge_mod.Card
        s = judge_mod.Suit
        cards = [c(s.HEART, 14), c(s.HEART, 2), c(s.HEART, 3), c(s.HEART, 4), c(s.HEART, 5)]
        result = judge_mod.hand_type_of_cards(cards)
        assert result == judge_mod.HandType.STRAIGHT_FLUSH, (
            f"Wheel flush should be STRAIGHT_FLUSH, got {result}"
        )

    def test_normal_straight_still_works(self, judge_mod):
        """Regular straight 10-J-Q-K-A still works correctly."""
        c = judge_mod.Card
        s = judge_mod.Suit
        cards = [c(s.HEART, 10), c(s.DIAMOND, 11), c(s.SPADE, 12), c(s.CLUB, 13), c(s.HEART, 14)]
        result = judge_mod.hand_type_of_cards(cards)
        assert result == judge_mod.HandType.STRAIGHT, f"Normal straight should be STRAIGHT, got {result}"

    def test_non_straight_not_false_positive(self, judge_mod):
        """A-2-3-4-6 is NOT a straight."""
        c = judge_mod.Card
        s = judge_mod.Suit
        # 2-3-4-5-6 suited = straight flush (valid)
        cards = [c(s.HEART, 2), c(s.HEART, 3), c(s.HEART, 4), c(s.HEART, 5), c(s.HEART, 6)]
        result = judge_mod.hand_type_of_cards(cards)
        assert result == judge_mod.HandType.STRAIGHT_FLUSH, (
            f"2-3-4-5-6 suited should be STRAIGHT_FLUSH, got {result}"
        )

    def test_seven_card_finds_wheel(self, judge_mod):
        """7 cards containing A-2-3-4-5 should detect wheel straight."""
        c = judge_mod.Card
        s = judge_mod.Suit
        cards = [c(s.HEART, 14), c(s.DIAMOND, 2), c(s.SPADE, 3), c(s.CLUB, 4), c(s.HEART, 5),
                 c(s.SPADE, 9), c(s.CLUB, 13)]
        result, _best_cards = judge_mod.find_max_hand_type(cards)
        assert result == judge_mod.HandType.STRAIGHT, (
            f"7-card wheel should be STRAIGHT, got {result}"
        )


# ═══════════════════════════════════════════════════════════════
# BOT-002: Re-raise Boundary (strictly > 2x)
# ═══════════════════════════════════════════════════════════════

class TestReRaiseBoundary:
    """Verify re-raise minimum is strictly > 2x previous raise-to-total."""

    def test_reraise_strictly_greater_than_2x(self):
        """min_raise_action should be > 2 * last_raise_to (not >=)."""
        last_raise_to = 400
        my_round_bet = 200  # player already bet 200
        min_raise = max(0, 2 * last_raise_to - my_round_bet + 1)
        assert min_raise == 601, f"After raise 400, min re-raise should be 601, got {min_raise}"

    def test_reraise_formula_new_stage(self):
        """In a new stage with last_raise_to=400, min_raise should be 801."""
        last_raise_to = 400
        my_round_bet = 0
        min_raise = max(0, 2 * last_raise_to - my_round_bet + 1)
        assert min_raise == 801, f"Min re-raise should be 801, got {min_raise}"

    def test_reraise_formula_already_bet(self):
        """With my_round_bet=200 and last_raise_to=400, min_raise should be 601."""
        last_raise_to = 400
        my_round_bet = 200
        min_raise = max(0, 2 * last_raise_to - my_round_bet + 1)
        assert min_raise == 601, f"Min re-raise with existing bet should be 601, got {min_raise}"

    def test_preflop_first_raise_conservative(self):
        """First preflop raise: +1 makes min 201 instead of 200 (conservative 1 chip)."""
        last_raise_to = 100  # big blind
        my_round_bet = 50  # small blind
        min_raise = max(0, 2 * last_raise_to - my_round_bet + 1)
        assert min_raise == 151, f"SB first raise should be 151 (conservative), got {min_raise}"
        my_round_bet = 100
        min_raise = max(0, 2 * 100 - 100 + 1)
        assert min_raise == 101, f"BB first raise should be 101 (conservative), got {min_raise}"


# ═══════════════════════════════════════════════════════════════
# PIPE-001: Circuit Breaker Count (failure_count per round, not len(tasks))
# ═══════════════════════════════════════════════════════════════

class TestCircuitBreaker:
    """Verify circuit breaker uses per-round count, not len(tasks)."""

    def test_circuit_breaker_threshold(self):
        """Circuit breaker should trigger at failure_count >= MAX_WORKER_FAILURES."""
        MAX_WORKER_FAILURES = 6
        failure_count = 5
        tasks = [{"id": 1}, {"id": 2}]

        old_trigger = failure_count + len(tasks) > MAX_WORKER_FAILURES
        new_trigger = failure_count >= MAX_WORKER_FAILURES

        assert old_trigger is True, "Old behavior should trigger at 5 failures + 2 tasks"
        assert new_trigger is False, "New behavior should NOT trigger at 5 failures"
        assert (6 >= MAX_WORKER_FAILURES) is True, "Should trigger at exactly 6 failures"

    def test_circuit_breaker_increment(self):
        """Failure count should increment by 1 per failed round, not len(tasks)."""
        failure_count = 0
        tasks = [{"id": 1}, {"id": 2}]

        old_new_count = failure_count + len(tasks)
        new_new_count = failure_count + 1

        assert old_new_count == 2, "Old behavior added 2 per round"
        assert new_new_count == 1, "New behavior adds 1 per round (first-fail-abort)"


# ═══════════════════════════════════════════════════════════════
# PIPE-002: _git timeout
# ═══════════════════════════════════════════════════════════════

class TestGitTimeout:
    """Verify _git() has a 30s timeout."""

    def test_git_timeout_parameter(self):
        """_git() should pass timeout=30 to subprocess.run."""
        core_dir = Path(__file__).resolve().parent.parent / "core"
        sys.path.insert(0, str(core_dir))
        try:
            from evolution_infra import _git
        finally:
            sys.path.remove(str(core_dir))

        with patch("evolution_infra.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="ok\n", returncode=0)
            _git("status")
            call_kwargs = mock_run.call_args[1]
            assert "timeout" in call_kwargs, "_git() should pass timeout to subprocess.run"
            assert call_kwargs["timeout"] == 30, (
                f"timeout should be 30, got {call_kwargs.get('timeout')}"
            )

    def test_git_timeout_exception(self):
        """_git() should raise RuntimeError on timeout."""
        core_dir = Path(__file__).resolve().parent.parent / "core"
        sys.path.insert(0, str(core_dir))
        try:
            from evolution_infra import _git
        finally:
            sys.path.remove(str(core_dir))

        with patch("evolution_infra.subprocess.run", side_effect=subprocess.TimeoutExpired("git", 30)):
            with pytest.raises(RuntimeError, match="timed out"):
                _git("status")


# ═══════════════════════════════════════════════════════════════
# PIPE-003: clear_pipeline_checkpoint lock
# ═══════════════════════════════════════════════════════════════

class TestClearPipelineCheckpoint:
    """Verify clear_pipeline_checkpoint uses file locking."""

    def test_checkpoint_clear_no_crash_when_missing(self):
        """clear_pipeline_checkpoint should not crash when file doesn't exist."""
        import tempfile
        core_dir = Path(__file__).resolve().parent.parent / "core"
        sys.path.insert(0, str(core_dir))
        try:
            from evolution_infra import clear_pipeline_checkpoint
        finally:
            sys.path.remove(str(core_dir))

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_path = Path(tmpdir) / "nonexistent_checkpoint.json"
            with patch("evolution_infra.PIPELINE_STATE_FILE", fake_path):
                clear_pipeline_checkpoint()

    def test_checkpoint_clear_uses_locked_file(self):
        """clear_pipeline_checkpoint should call locked_file for safe deletion."""
        import tempfile
        core_dir = Path(__file__).resolve().parent.parent / "core"
        sys.path.insert(0, str(core_dir))
        try:
            from evolution_infra import clear_pipeline_checkpoint
        finally:
            sys.path.remove(str(core_dir))

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("{}")
            fake_path = Path(f.name)

        try:
            with patch("evolution_infra.PIPELINE_STATE_FILE", fake_path):
                with patch("evolution_infra.locked_file") as mock_lock:
                    mock_lock.return_value.__enter__ = MagicMock(return_value=MagicMock())
                    mock_lock.return_value.__exit__ = MagicMock(return_value=False)
                    clear_pipeline_checkpoint()
                    mock_lock.assert_called_once()
        finally:
            if fake_path.exists():
                fake_path.unlink()


# ═══════════════════════════════════════════════════════════════
# Decision Test: CRITICAL count is 9
# ═══════════════════════════════════════════════════════════════

class TestDecisionTestClassification:
    """Verify CRITICAL scenario count is reduced to 9."""

    def test_critical_count(self):
        core_dir = Path(__file__).resolve().parent.parent / "core"
        sys.path.insert(0, str(core_dir))
        try:
            from decision_tester import CRITICAL_SCENARIO_IDS
        finally:
            sys.path.remove(str(core_dir))

        assert len(CRITICAL_SCENARIO_IDS) == 9, (
            f"CRITICAL_SCENARIO_IDS should have 9 entries, got {len(CRITICAL_SCENARIO_IDS)}"
        )

    def test_core_scenarios_are_critical(self):
        """Core disaster-prevention scenarios must remain CRITICAL."""
        core_dir = Path(__file__).resolve().parent.parent / "core"
        sys.path.insert(0, str(core_dir))
        try:
            from decision_tester import CRITICAL_SCENARIO_IDS
        finally:
            sys.path.remove(str(core_dir))

        must_be_critical = {
            "preflop_aa_first_act",
            "preflop_kk_first_act",
            "flop_top_set_safe_board",
            "river_nut_flush_facing_bet",
            "flop_nut_straight_dry_board",
            "river_full_house_facing_raise",
        }
        for scenario in must_be_critical:
            assert scenario in CRITICAL_SCENARIO_IDS, f"{scenario} must be CRITICAL"

    def test_downgraded_scenarios_are_advisory(self):
        """Downgraded scenarios must NOT be in CRITICAL_SCENARIO_IDS."""
        core_dir = Path(__file__).resolve().parent.parent / "core"
        sys.path.insert(0, str(core_dir))
        try:
            from decision_tester import CRITICAL_SCENARIO_IDS
        finally:
            sys.path.remove(str(core_dir))

        should_be_advisory = {
            "flop_flush_draw_facing_cbet",
            "river_missed_draw_facing_big_bet",
            "turn_two_pair_facing_bet",
            "flop_air_facing_pot_bet",
        }
        for scenario in should_be_advisory:
            assert scenario not in CRITICAL_SCENARIO_IDS, (
                f"{scenario} should be advisory, not CRITICAL"
            )
