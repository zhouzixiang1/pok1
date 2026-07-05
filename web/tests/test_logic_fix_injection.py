"""Integration tests for fix_injection module.

Tests the centralized fix registry and application engine that ensures
critical fixes are applied to every new bot generation.
"""

import sys
import shutil
from pathlib import Path

import pytest

CORE_DIR = Path(__file__).resolve().parent.parent / "core"


@pytest.fixture
def fix_injection():
    sys.path.insert(0, str(CORE_DIR))
    try:
        import fix_injection as _fi
        yield _fi
    finally:
        sys.path.remove(str(CORE_DIR))


@pytest.fixture
def bare_bot_dir(tmp_path):
    """Create a temp bot dir with unfixed code matching the search patterns."""
    bot_dir = tmp_path / "claude_v99"
    bot_dir.mkdir()

    # card_utils.py — missing wheel straight fix
    (bot_dir / "card_utils.py").write_text(
        '''
def evaluate_5(cards):
    ranks = sorted((c // 4 + 2 for c in cards), reverse=True)
    suits = [c % 4 for c in cards]
    unique_ranks = sorted(set(ranks), reverse=True)

    is_straight = False
    straight_high = 0
    if len(unique_ranks) == 5:
        if unique_ranks[0] - unique_ranks[4] == 4:
            is_straight = True
            straight_high = unique_ranks[0]

    if is_flush and is_straight:
        return (8, straight_high)
    if is_straight:
        return (4, straight_high)
    return (0,)
'''
    )

    # constants.py — wrong TOTAL_HANDS
    (bot_dir / "constants.py").write_text(
        "TOTAL_HANDS = 50\n"
    )

    # state.py — missing +1 in min_raise
    (bot_dir / "state.py").write_text(
        '''
def get_state():
    last_raise_to = 100
    my_round_bet = 50
    min_raise_action = max(0, 2 * last_raise_to - my_round_bet)
    return {"min_raise_action": min_raise_action}
'''
    )

    return bot_dir


class TestApplyKnownFixes:
    """Test apply_known_fixes on a bare bot directory."""

    def test_apply_all_fixes(self, fix_injection, bare_bot_dir):
        applied, skipped = fix_injection.apply_known_fixes(bare_bot_dir)
        assert "BOT-001a" in applied, "Wheel straight fix should be applied"
        assert "BOT-002a" in applied, "Re-raise +1 fix should be applied"
        assert "BOT-004" in applied, "TOTAL_HANDS fix should be applied"
        assert "BOT-002b" not in applied, "BOT-002b is inactive, should not appear"
        # BOT-002b is inactive (dead template) — it won't appear in skipped either

        # Verify file contents
        card_utils = (bare_bot_dir / "card_utils.py").read_text()
        assert "{14, 2, 3, 4, 5}" in card_utils, "Wheel check not found in card_utils.py"

        constants = (bare_bot_dir / "constants.py").read_text()
        assert "TOTAL_HANDS = 70" in constants, "TOTAL_HANDS not fixed"
        assert "TOTAL_HANDS = 50" not in constants, "Old TOTAL_HANDS still present"

        state_py = (bare_bot_dir / "state.py").read_text()
        assert "2 * last_raise_to + 1 - my_round_bet" in state_py, "min_raise not fixed"

    def test_idempotent_reapplication(self, fix_injection, bare_bot_dir):
        # First application
        applied1, skipped1 = fix_injection.apply_known_fixes(bare_bot_dir)
        assert len(applied1) > 0

        # Second application — should be idempotent
        applied2, skipped2 = fix_injection.apply_known_fixes(bare_bot_dir)
        assert len(applied2) == 0, f"Second run should apply nothing, got {applied2}"
        assert len(skipped2) > 0, f"Second run should skip all, got {skipped2}"

    def test_skipped_when_search_not_found(self, fix_injection, tmp_path):
        bot_dir = tmp_path / "claude_v99"
        bot_dir.mkdir()
        # Write files where search strings don't match at all
        (bot_dir / "card_utils.py").write_text("# completely different code\n")
        (bot_dir / "constants.py").write_text("FOO = 42\n")
        (bot_dir / "state.py").write_text("# no min_raise here\n")

        applied, skipped = fix_injection.apply_known_fixes(bot_dir)
        assert len(applied) == 0, f"Nothing should be applied when search not found, got {applied}"
        # Only active fixes are processed; BOT-002b is inactive so won't be in skipped
        active_count = sum(1 for f in fix_injection.MANDATORY_FIXES if f.active)
        assert len(skipped) == active_count, (
            f"All {active_count} active fixes should be skipped"
        )


class TestFixRegistry:
    """Test the MANDATORY_FIXES registry."""

    def test_registry_has_expected_fixes(self, fix_injection):
        fix_ids = {f.fix_id for f in fix_injection.MANDATORY_FIXES}
        assert "BOT-001a" in fix_ids
        assert "BOT-002a" in fix_ids
        assert "BOT-002b" in fix_ids
        assert "BOT-004" in fix_ids
        assert "BOT-005" in fix_ids

    def test_all_fixes_are_active(self, fix_injection):
        for fix in fix_injection.MANDATORY_FIXES:
            if fix.fix_id == "BOT-002b":
                # BOT-002b is intentionally inactive (dead template: no bot uses judge_round_raise)
                assert not fix.active, "BOT-002b should be inactive"
                continue
            assert fix.active, f"Fix {fix.fix_id} should be active"

    def test_all_patches_have_guard(self, fix_injection):
        for fix in fix_injection.MANDATORY_FIXES:
            for patch in fix.patches:
                assert patch.guard is not None, (
                    f"Patch {fix.fix_id}/{patch.file_rel} should have a guard string"
                )


def test_bot005_updates_disciplined_margin_selftest_fixture(fix_injection, tmp_path):
    bot_dir = tmp_path / "claude_v99"
    bot_dir.mkdir()
    (bot_dir / "postflop.py").write_text(
        '''
def disciplined_opp_river_margin():
    """
    Standard-bucket (vpip>=0.58, pfr>=0.28) returns exactly 0.0 by construction —
    long-tail H2H is unaffected.
    """

if __name__ == '__main__':
    # ── disciplined_opp_river_margin self-test ──────────────────────────────
    # Fixture A — standard-bucket defaults (vpip/pfr at priors): delta MUST be 0
    std_om = {"vpip": 0.58, "pfr": 0.28, "confidence": 0.5}
''',
        encoding="utf-8",
    )

    applied, _skipped = fix_injection.apply_known_fixes(bot_dir)

    text = (bot_dir / "postflop.py").read_text(encoding="utf-8")
    assert "BOT-005" in applied
    assert "Standard-bucket (vpip>=0.62, pfr>=0.32)" in text
    assert 'std_om = {"vpip": 0.62, "pfr": 0.32, "confidence": 0.5}' in text


def test_embedded_selftest_gate_catches_and_bot005_repairs_v296(tmp_path):
    sys.path.insert(0, str(CORE_DIR))
    try:
        import evolution_infra  # noqa: F401 - initialize existing re-export import order
        import code_verification
        import fix_injection as _fix_injection
    finally:
        sys.path.remove(str(CORE_DIR))

    source = CORE_DIR.parents[1] / "bots" / "claude_v296"
    if not source.exists():
        pytest.skip("claude_v296 fixture bot is not present in this checkout")
    bot_dir = tmp_path / "claude_v296"
    shutil.copytree(source, bot_dir, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".completed"))

    before = code_verification.run_bot_embedded_self_tests(bot_dir)
    assert any("postflop.py" in err and "standard bucket must be 0" in err for err in before)

    applied, _skipped = _fix_injection.apply_known_fixes(bot_dir)
    after = code_verification.run_bot_embedded_self_tests(bot_dir)

    assert "BOT-005" in applied
    assert after == []


def test_embedded_selftest_gate_reports_synthetic_failure(tmp_path):
    sys.path.insert(0, str(CORE_DIR))
    try:
        import evolution_infra  # noqa: F401 - initialize existing re-export import order
        import code_verification
    finally:
        sys.path.remove(str(CORE_DIR))

    bot_dir = tmp_path / "claude_v1"
    bot_dir.mkdir()
    (bot_dir / "postflop.py").write_text(
        "if __name__ == '__main__':\n"
        "    # self-test\n"
        "    assert False, 'boom'\n",
        encoding="utf-8",
    )

    errors = code_verification.run_bot_embedded_self_tests(bot_dir)

    assert len(errors) == 1
    assert "postflop.py" in errors[0]
    assert "boom" in errors[0]
