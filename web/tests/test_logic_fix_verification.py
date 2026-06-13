"""Tests for fix_verification — structural/runtime mandatory-fix verification.

verify_fixes() is the AUTHORITATIVE fix-present judgment (fix_injection.py's
substring matching silently misses when workers refactor target functions).
Tests cover: real v72 all-ok, missing-wheel not-ok, TOTAL_HANDS=50 not-ok,
AST robustness when variables are renamed, and the exception path returning ok.
"""

import sys
from pathlib import Path

import pytest

CORE_DIR = Path(__file__).resolve().parent.parent / "core"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def fv():
    sys.path.insert(0, str(CORE_DIR))
    try:
        import fix_verification as _fv
        yield _fv
    finally:
        sys.path.remove(str(CORE_DIR))


@pytest.fixture
def make_bot(tmp_path):
    """Return a factory that writes a complete bot dir from a dict of files."""
    def _make(name="claude_v99", **files):
        bot_dir = tmp_path / name
        bot_dir.mkdir()
        for rel, text in files.items():
            if not rel.endswith(".py"):
                rel = rel + ".py"
            (bot_dir / rel).write_text(text)
        return bot_dir
    return _make


# ── Real bot: claude_v72 should pass every verifier ──

_V72 = PROJECT_ROOT / "bots" / "claude_v72"


def _v72_present():
    return (_V72 / "card_utils.py").exists() and (_V72 / "constants.py").exists()


class TestVerifyFixesV72:
    """The canonical fixed bot (v72) passes every verifier."""

    @pytest.mark.skipif(not _v72_present(), reason="claude_v72 not present")
    def test_v72_all_ok(self, fv):
        results = fv.verify_fixes(_V72)
        assert set(results) == {"BOT-001a", "BOT-002a", "BOT-004"}
        for fix_id, r in results.items():
            assert r["ok"] is True, f"{fix_id} should pass on v72: {r['reason']}"


class TestWheelStraight:
    # A bot WITH the wheel fix (mirrors v72 card_utils.evaluate_5).
    CARD_UTILS_FIXED = '''
def card_number(c): return c // 4 + 2
def card_suit(c): return c % 4
def evaluate_5(cards):
    ranks = sorted((card_number(c) for c in cards), reverse=True)
    suits = [card_suit(c) for c in cards]
    unique_ranks = sorted(set(ranks), reverse=True)
    is_flush = len(set(suits)) == 1
    is_straight = False
    straight_high = 0
    if len(unique_ranks) == 5:
        if unique_ranks[0] - unique_ranks[4] == 4:
            is_straight = True
            straight_high = unique_ranks[0]
        elif set(unique_ranks) == {14, 2, 3, 4, 5}:
            is_straight = True
            straight_high = 5
    if is_flush and is_straight:
        return (8, straight_high)
    if is_straight:
        return (4, straight_high)
    return (0, *ranks)
'''
    # A bot WITHOUT the wheel fix.
    CARD_UTILS_UNFIXED = '''
def card_number(c): return c // 4 + 2
def card_suit(c): return c % 4
def evaluate_5(cards):
    ranks = sorted((card_number(c) for c in cards), reverse=True)
    unique_ranks = sorted(set(ranks), reverse=True)
    is_straight = False
    straight_high = 0
    if len(unique_ranks) == 5:
        if unique_ranks[0] - unique_ranks[4] == 4:
            is_straight = True
            straight_high = unique_ranks[0]
    if is_straight:
        return (4, straight_high)
    return (0, *ranks)
'''

    def test_wheel_present_ok(self, fv, make_bot):
        bot_dir = make_bot(card_utils=self.CARD_UTILS_FIXED)
        r = fv.verify_fixes(bot_dir)["BOT-001a"]
        assert r["ok"] is True

    def test_wheel_missing_not_ok(self, fv, make_bot):
        bot_dir = make_bot(card_utils=self.CARD_UTILS_UNFIXED)
        r = fv.verify_fixes(bot_dir)["BOT-001a"]
        assert r["ok"] is False
        assert "straight" in r["reason"].lower() or "wheel" in r["reason"].lower()

    def test_wheel_ast_fallback_after_rename(self, fv, make_bot):
        """If evaluate_5 is renamed (probe import fails), the AST fallback still
        finds the {14, 2, 3, 4, 5} literal inside any function body and passes."""
        renamed = self.CARD_UTILS_FIXED.replace("def evaluate_5(", "def evaluate_hand(")
        bot_dir = make_bot(card_utils=renamed)
        r = fv.verify_fixes(bot_dir)["BOT-001a"]
        # evaluate_5 no longer exists -> probe fails -> AST fallback fires.
        assert r["ok"] is True
        assert "AST fallback" in r["reason"]


class TestMinRaise:
    STATE_FIXED = '''
def get_state(last_raise_to, my_round_bet):
    min_raise_action = max(0, 2 * last_raise_to + 1 - my_round_bet)
    return min_raise_action
'''
    STATE_UNFIXED = '''
def get_state(last_raise_to, my_round_bet):
    min_raise_action = max(0, 2 * last_raise_to - my_round_bet)
    return min_raise_action
'''

    def test_min_raise_plus_one_ok(self, fv, make_bot):
        bot_dir = make_bot(state=self.STATE_FIXED)
        r = fv.verify_fixes(bot_dir)["BOT-002a"]
        assert r["ok"] is True

    def test_min_raise_missing_plus_one_not_ok(self, fv, make_bot):
        bot_dir = make_bot(state=self.STATE_UNFIXED)
        r = fv.verify_fixes(bot_dir)["BOT-002a"]
        assert r["ok"] is False
        assert "+ 1" in r["reason"] or "+1" in r["reason"]

    def test_min_raise_no_assignment_ok(self, fv, make_bot):
        """A state.py with no min_raise_action assignment does not block."""
        bot_dir = make_bot(state="X = 1\n")
        r = fv.verify_fixes(bot_dir)["BOT-002a"]
        assert r["ok"] is True


class TestTotalHands:
    def test_total_70_ok(self, fv, make_bot):
        bot_dir = make_bot(constants="TOTAL_HANDS = 70\n")
        r = fv.verify_fixes(bot_dir)["BOT-004"]
        assert r["ok"] is True

    def test_total_50_not_ok(self, fv, make_bot):
        bot_dir = make_bot(constants="TOTAL_HANDS = 50\n")
        r = fv.verify_fixes(bot_dir)["BOT-004"]
        assert r["ok"] is False
        assert "50" in r["reason"]

    def test_total_ast_fallback_after_syntax_issue(self, fv, make_bot):
        """If the subprocess import probe fails (e.g. constants has an import
        side-effect), AST fallback on TOTAL_HANDS = 70 still passes."""
        # constants.py with a bogus import that would crash the probe, but a
        # valid TOTAL_HANDS = 70 literal the AST fallback can still see.
        bot_dir = make_bot(constants="import nonexistent_module_xyz\nTOTAL_HANDS = 70\n")
        r = fv.verify_fixes(bot_dir)["BOT-004"]
        assert r["ok"] is True
        assert "AST fallback" in r["reason"]


class TestExceptionSafety:
    def test_missing_dir_returns_ok(self, fv, tmp_path):
        """A verifier that cannot find its file must return ok (never block)."""
        results = fv.verify_fixes(tmp_path / "does_not_exist")
        for fix_id, r in results.items():
            assert r["ok"] is True, f"{fix_id} must not block on missing files: {r}"

    def test_verifier_exception_returns_ok(self, fv, monkeypatch, make_bot):
        """If a verifier raises, verify_fixes swallows it and returns ok=True."""
        bot_dir = make_bot(constants="TOTAL_HANDS = 70\n")

        def boom(_bot_dir):
            raise RuntimeError("simulated verifier crash")

        monkeypatch.setitem(fv._VERIFIERS, "BOT-004", boom)
        r = fv.verify_fixes(bot_dir)["BOT-004"]
        assert r["ok"] is True
        assert "RuntimeError" in r["reason"]


class TestResultShape:
    def test_returns_all_three_fix_ids(self, fv, tmp_path):
        results = fv.verify_fixes(tmp_path)
        assert set(results) == {"BOT-001a", "BOT-002a", "BOT-004"}
        for r in results.values():
            assert set(r) == {"ok", "reason"}
            assert isinstance(r["ok"], bool)
            assert isinstance(r["reason"], str)
