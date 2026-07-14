from __future__ import annotations

import os

import pytest

from bots.research_native_lab.common_contracts.manifest import (
    build_tree_manifest,
    verify_tree_manifest,
)


def _bindings(entrypoint: str = "main.py") -> dict[str, str]:
    return {
        "entrypoint": entrypoint,
        "config_digest": "01" * 32,
        "dependency_digest": "02" * 32,
        "resource_profile_digest": "03" * 32,
        "oracle_fixture_digest": "04" * 32,
        "action_set_digest": "05" * 32,
        "build_command": "python -m py_compile main.py",
        "run_command": "python main.py",
    }


def _artifact(tmp_path):
    root = tmp_path / "artifact"
    root.mkdir()
    (root / "main.py").write_text("print('ok')\n", encoding="utf-8")
    return root


def test_complete_tree_rejects_a_symlink_as_the_sealed_root(tmp_path) -> None:
    root = _artifact(tmp_path)
    link = tmp_path / "artifact-link"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match="real directory, not a symlink"):
        build_tree_manifest(link, _bindings())


def test_directory_set_empty_directories_and_modes_are_content_bound(tmp_path) -> None:
    root = _artifact(tmp_path)
    empty = root / "empty"
    nested = empty / "nested"
    nested.mkdir(parents=True)
    root.chmod(0o750)
    empty.chmod(0o711)
    nested.chmod(0o700)

    manifest = build_tree_manifest(root, _bindings())
    assert manifest["schema_version"] == 2
    assert manifest["directories"] == {
        ".": {"mode": 0o750},
        "empty": {"mode": 0o711},
        "empty/nested": {"mode": 0o700},
    }
    verify_tree_manifest(root, manifest)

    nested.chmod(0o755)
    with pytest.raises(ValueError, match=r"directory tree mismatch: .*changed"):
        verify_tree_manifest(root, manifest)
    nested.chmod(0o700)

    nested.rmdir()
    with pytest.raises(ValueError, match=r"directory tree mismatch: .*missing"):
        verify_tree_manifest(root, manifest)
    nested.mkdir()
    nested.chmod(0o700)

    (root / "new-empty").mkdir()
    with pytest.raises(ValueError, match=r"directory tree mismatch: .*extra"):
        verify_tree_manifest(root, manifest)


def test_nested_git_and_symlink_directories_remain_forbidden(tmp_path) -> None:
    root = _artifact(tmp_path)
    (root / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    with pytest.raises(ValueError, match="nested git metadata"):
        build_tree_manifest(root, _bindings())
    (root / ".git").unlink()

    real = root / "real-directory"
    real.mkdir()
    (root / "linked-directory").symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink directory"):
        build_tree_manifest(root, _bindings())


def test_directory_extended_attributes_remain_forbidden(tmp_path) -> None:
    root = _artifact(tmp_path)
    directory = root / "empty"
    directory.mkdir()
    try:
        os.setxattr(directory, "user.pok_manifest_test", b"1", follow_symlinks=False)
    except (AttributeError, OSError):
        pytest.skip("filesystem does not support user xattrs")
    with pytest.raises(ValueError, match="extended attributes are forbidden: empty"):
        build_tree_manifest(root, _bindings())
