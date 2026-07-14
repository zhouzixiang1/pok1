from __future__ import annotations

import math

import pytest

from ..decisionholdem_like import secure_files


def test_strict_json_rejects_duplicate_and_overflowed_finite_syntax() -> None:
    with pytest.raises(ValueError, match="duplicate JSON key"):
        secure_files.strict_json_loads('{"value":1,"value":2}')
    with pytest.raises(ValueError, match="non-finite JSON number"):
        secure_files.strict_json_loads('{"value":1e999}')
    with pytest.raises(ValueError):
        secure_files.canonical_bytes({"value": math.nan})


def test_atomic_json_rejects_parent_rename_during_serialization(
    tmp_path,
    monkeypatch,
) -> None:
    parent = tmp_path / "workspace"
    parent.mkdir()
    moved = tmp_path / "moved-workspace"
    target = parent / "result.json"
    original = secure_files.pretty_json_bytes

    def rename_parent(payload: object) -> bytes:
        parent.rename(moved)
        parent.mkdir()
        return original(payload)

    monkeypatch.setattr(secure_files, "pretty_json_bytes", rename_parent)
    with pytest.raises(RuntimeError, match="ancestry changed"):
        secure_files.atomic_json_write(target, {"ok": True})
    assert not target.exists()
    assert not list(moved.glob(".*.tmp"))


def test_atomic_json_rejects_existing_target_swap_before_publish(
    tmp_path,
    monkeypatch,
) -> None:
    target = tmp_path / "result.json"
    target.write_bytes(b'{"old":true}\n')
    original = secure_files.pretty_json_bytes

    def replace_target(payload: object) -> bytes:
        target.write_bytes(b'{"attacker":true}\n')
        return original(payload)

    monkeypatch.setattr(secure_files, "pretty_json_bytes", replace_target)
    with pytest.raises(RuntimeError, match="target changed"):
        secure_files.atomic_json_write(target, {"new": True})
    assert target.read_bytes() == b'{"attacker":true}\n'
    assert not list(tmp_path.glob(".*.tmp"))


def test_complete_tree_second_scan_catches_earlier_file_mutation(
    tmp_path,
    monkeypatch,
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    first = root / "a.txt"
    first.write_bytes(b"before")
    (root / "b.txt").write_bytes(b"stable")
    original = secure_files.read_stable_regular_at
    mutated = False

    def mutate_after_first_stable_read(directory_fd: int, name: str) -> bytes:
        nonlocal mutated
        data = original(directory_fd, name)
        if name == "a.txt" and not mutated:
            first.write_bytes(b"after")
            mutated = True
        return data

    monkeypatch.setattr(
        secure_files,
        "read_stable_regular_at",
        mutate_after_first_stable_read,
    )
    with pytest.raises(RuntimeError, match="between complete snapshot scans"):
        secure_files.secure_file_map(root)


def test_stable_path_rejects_a_symlinked_ancestor(tmp_path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "input.json").write_bytes(b"{}")
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink aliases"):
        secure_files.stable_read_path(alias / "input.json")
