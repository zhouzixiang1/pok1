from __future__ import annotations

from ..tools.milestone_manifest import verify_manifest


def test_committed_milestone_manifest_is_complete_and_self_consistent() -> None:
    assert verify_manifest() == []
