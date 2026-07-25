"""Current strict-policy target normalization tests."""

import pytest

from conftest import STRICT_TARGET_V, strict_bot_name
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
    assert _target_rel(value, STRICT_TARGET_V) == "policy.py"


def test_canonical_active_bot_path_normalizes_to_policy():
    assert _target_rel(
        f"bots/{strict_bot_name()}/policy.py", STRICT_TARGET_V
    ) == "policy.py"


def test_nested_canonical_active_bot_prefixes_are_removed():
    child_v = STRICT_TARGET_V + 1
    assert _target_rel(
        f"bots/{strict_bot_name()}/bots/{strict_bot_name(child_v)}/policy.py [MODIFIED]",
        child_v,
    ) == "policy.py"


def test_non_annotation_suffix_is_not_silently_rewritten():
    assert _target_rel(
        "policy.py (NEWNEW)", STRICT_TARGET_V
    ) == "policy.py (NEWNEW)"


def test_empty_target_stays_empty():
    assert _target_rel("", STRICT_TARGET_V) == ""
