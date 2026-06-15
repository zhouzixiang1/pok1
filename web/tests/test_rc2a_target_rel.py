"""Tests for _target_rel status-annotation stripping (rc2a).

The LLM sometimes appends status markers to target_files entries, e.g.
"bet_size_profile.py (NEW)" or "strategy.py [CREATE]". _target_rel must strip
those trailing annotations so downstream consumers see the bare filename.
Legitimate filenames that merely contain bracketed content (e.g. "report(2).py")
must survive unchanged.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from evolution_infra import _target_rel  # noqa: E402


# ── MUST-HOLD examples ──

def test_strips_paren_new():
    assert _target_rel("bet_size_profile.py (NEW)", 97) == "bet_size_profile.py"


def test_strips_bracket_create():
    assert _target_rel("strategy.py [CREATE]", 50) == "strategy.py"


def test_strips_paren_delete_case_insensitive():
    assert _target_rel("helpers.py (delete)", 30) == "helpers.py"


def test_keeps_report_parens_with_digits():
    assert _target_rel("report(2).py", 30) == "report(2).py"


def test_keeps_utils_parens_with_non_keyword():
    assert _target_rel("utils(new).py", 30) == "utils(new).py"


def test_bare_filename_unchanged():
    assert _target_rel("foo.py", 30) == "foo.py"


def test_full_bot_path_relativized():
    assert _target_rel("bots/claude_v97/main.py", 97) == "main.py"


def test_empty_string():
    assert _target_rel("", 30) == ""


# ── Parametrized: all four keywords × both brackets × both cases ──

@pytest.mark.parametrize("keyword", ["NEW", "CREATE", "DELETE", "MODIFIED"])
@pytest.mark.parametrize("bracket", ["()", "[]"])
@pytest.mark.parametrize("case", ["upper", "lower"])
def test_annotation_stripping_parametrized(keyword, bracket, case):
    kw = keyword.upper() if case == "upper" else keyword.lower()
    open_b, close_b = bracket
    annotated = f"strategy.py {open_b}{kw}{close_b}"
    assert _target_rel(annotated, 50) == "strategy.py"


# ── Edge cases ──

def test_annotation_with_leading_whitespace():
    assert _target_rel("  strategy.py (NEW)  ", 30) == "strategy.py"


def test_annotation_then_marker_path():
    # Annotation stripped before marker relativisation.
    assert _target_rel("bots/claude_v97/main.py (MODIFIED)", 97) == "main.py"


def test_double_keyword_not_stripped_as_non_bare():
    # "(NEWNEW)" is not a bare keyword match -> not stripped.
    assert _target_rel("foo.py (NEWNEW)", 30) == "foo.py (NEWNEW)"
