"""Tests for _classify_target_change zero-changes classification (rc2b)."""

from core.agent_workers import _classify_target_change


def test_new_file():
    # Worker created a brand new file (success).
    assert _classify_target_change(False, True, "", "code") == "new_file"


def test_invalid_target():
    # Path resolves nowhere on disk (neither src nor dst exists) — failure.
    assert _classify_target_change(False, False, "", "") == "invalid_target"


def test_deleted():
    # File existed in source, now gone — failure.
    assert _classify_target_change(True, False, "x", "") == "deleted"


def test_unchanged():
    # Identical contents — failure (zero-change worker).
    assert _classify_target_change(True, True, "same", "same") == "unchanged"


def test_modified():
    # Both exist, contents differ — success.
    assert _classify_target_change(True, True, "a", "b") == "modified"


def test_new_file_requires_nonempty_dst():
    # Edge: (src missing, dst exists but empty) is NOT new_file — it is
    # invalid_target, since dst_text is falsy.
    assert _classify_target_change(False, True, "", "") == "invalid_target"
