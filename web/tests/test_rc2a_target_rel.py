"""Current strict-policy target normalization tests."""

import pytest

from evolution_infra import _target_rel


@pytest.mark.parametrize(
    "value",
    [
        "policy.py (NEW)",
        "policy.py [CREATE]",
        "policy.py (modified)",
        "  policy.py [delete]  ",
    ],
)
def test_status_annotations_normalize_to_policy(value):
    assert _target_rel(value, 143) == "policy.py"


def test_canonical_active_bot_path_normalizes_to_policy():
    assert _target_rel("bots/national_v143/policy.py", 143) == "policy.py"


def test_nested_canonical_active_bot_prefixes_are_removed():
    assert _target_rel(
        "bots/national_v143/bots/national_v144/policy.py [MODIFIED]",
        144,
    ) == "policy.py"


def test_non_annotation_suffix_is_not_silently_rewritten():
    assert _target_rel("policy.py (NEWNEW)", 143) == "policy.py (NEWNEW)"


def test_empty_target_stays_empty():
    assert _target_rel("", 143) == ""
