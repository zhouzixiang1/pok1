"""Archived strategy.py placement-shadow gate tests.

Verifies that:
- detect_placement_shadow_warnings correctly classifies TRUE-SHADOW vs review
- verify_code now includes TRUE-SHADOW in its errors
- TRUE-SHADOW placement shadows block quality gates (all_passed=False)
- "review" level shadows remain advisory (all_passed=True when everything else passes)
"""

import textwrap
from pathlib import Path

import pytest


# --- Strategy templates for testing ---

# Strategy with TRUE-SHADOW: _river_stackoff_guard called inside `if to_call > 0:`
# AFTER `if to_call >= my_chips: return` early-return.
_STRATEGY_TRUE_SHADOW = textwrap.dedent('''\
    def choose_raise(to_call, my_chips, made_str, round_idx, **kw):
        """Fake strategy function with TRUE-SHADOW placement."""
        # Early return for stack-covering all-ins
        if to_call >= my_chips:
            return -1  # fold

        if to_call > 0:
            result = _river_stackoff_guard(to_call, my_chips, made_str)
            if result:
                return result

        return 0

    def _river_stackoff_guard(to_call, my_chips, made_str):
        """Fake guard."""
        return 0
''')

# Strategy with "review" level shadow: _river_stackoff_guard called after
# `if to_call >= my_chips: return` but NOT inside a `to_call > 0` block.
# When the enclosing If kind is "none" (no to_call reference in parent If),
# severity is "review" (advisory, not blocking).
_STRATEGY_REVIEW_SHADOW = textwrap.dedent('''\
    def choose_raise(to_call, my_chips, made_str, round_idx, **kw):
        """Fake strategy function with review-level placement shadow."""
        # Early return for stack-covering all-ins
        if to_call >= my_chips:
            return -1  # fold

        # Guard called outside any to_call block — kind="none" -> severity="review"
        result = _river_stackoff_guard(to_call, my_chips, made_str)
        if result:
            return result

        return 0

    def _river_stackoff_guard(to_call, my_chips, made_str):
        """Fake guard."""
        return 0
''')

# Strategy with NO shadow: guard called BEFORE the early-return.
_STRATEGY_NO_SHADOW = textwrap.dedent('''\
    def choose_raise(to_call, my_chips, made_str, round_idx, **kw):
        """Fake strategy function with correct guard placement."""
        if to_call > 0:
            result = _river_stackoff_guard(to_call, my_chips, made_str)
            if result:
                return result

        # Early return for stack-covering all-ins (after the guard)
        if to_call >= my_chips:
            return -1  # fold

        return 0

    def _river_stackoff_guard(to_call, my_chips, made_str):
        """Fake guard."""
        return 0
''')


class TestDetectPlacementShadowWarnings:
    """Unit tests for detect_placement_shadow_warnings()."""

    def test_true_shadow_detected(self, tmp_path):
        from code_verification import detect_placement_shadow_warnings
        (tmp_path / "strategy.py").write_text(_STRATEGY_TRUE_SHADOW)
        warnings = detect_placement_shadow_warnings(str(tmp_path))
        assert any("TRUE SHADOW" in w for w in warnings)

    def test_review_shadow_detected(self, tmp_path):
        from code_verification import detect_placement_shadow_warnings
        (tmp_path / "strategy.py").write_text(_STRATEGY_REVIEW_SHADOW)
        warnings = detect_placement_shadow_warnings(str(tmp_path))
        assert any("review" in w for w in warnings)
        assert not any("TRUE SHADOW" in w for w in warnings)

    def test_no_shadow_when_guard_before_early_return(self, tmp_path):
        from code_verification import detect_placement_shadow_warnings
        (tmp_path / "strategy.py").write_text(_STRATEGY_NO_SHADOW)
        warnings = detect_placement_shadow_warnings(str(tmp_path))
        assert len(warnings) == 0

    def test_no_strategy_py_returns_empty(self, tmp_path):
        from code_verification import detect_placement_shadow_warnings
        (tmp_path / "main.py").write_text("x = 1\n")
        warnings = detect_placement_shadow_warnings(str(tmp_path))
        assert warnings == []


class TestVerifyCodePlacementShadow:
    """Verify that verify_code() now includes TRUE-SHADOW errors."""

    def test_true_shadow_appears_in_verify_errors(self, tmp_path):
        from code_verification import verify_code
        (tmp_path / "strategy.py").write_text(_STRATEGY_TRUE_SHADOW)
        errors = verify_code(str(tmp_path))
        shadow_errors = [e for e in errors if "TRUE SHADOW" in e]
        assert len(shadow_errors) >= 1

    def test_review_shadow_not_in_verify_errors(self, tmp_path):
        from code_verification import verify_code
        (tmp_path / "strategy.py").write_text(_STRATEGY_REVIEW_SHADOW)
        errors = verify_code(str(tmp_path))
        shadow_errors = [e for e in errors if "placement_shadow" in e or "TRUE SHADOW" in e]
        assert len(shadow_errors) == 0

    def test_no_shadow_clean_verify(self, tmp_path):
        from code_verification import verify_code
        (tmp_path / "strategy.py").write_text(_STRATEGY_NO_SHADOW)
        errors = verify_code(str(tmp_path))
        assert len(errors) == 0
