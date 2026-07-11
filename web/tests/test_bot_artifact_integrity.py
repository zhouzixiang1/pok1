from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import bot_artifact
from bot_artifact import (
    ArtifactIntegrityError,
    artifact_manifest,
    hash_path,
    validate_completion_tag,
)


def _write_bot(root: Path) -> None:
    root.mkdir()
    (root / "national_bot.py").write_text("print('ready')\n", encoding="utf-8")
    strategy = root / "strategy"
    strategy.mkdir()
    (strategy / "policy.py").write_text("ACTION = 'check'\n", encoding="utf-8")


def test_symlink_national_entry_is_rejected(tmp_path):
    bot = tmp_path / "national_v1"
    bot.mkdir()
    target = tmp_path / "external.py"
    target.write_text("print('external')\n", encoding="utf-8")
    (bot / "national_bot.py").symlink_to(target)

    with pytest.raises(ArtifactIntegrityError, match="symbolic links are forbidden"):
        hash_path(bot)


def test_nested_file_and_directory_symlinks_are_rejected(tmp_path):
    bot = tmp_path / "national_v2"
    _write_bot(bot)
    external_file = tmp_path / "external-policy.py"
    external_file.write_text("ACTION = 'raise 200'\n", encoding="utf-8")
    (bot / "strategy" / "linked.py").symlink_to(external_file)

    with pytest.raises(ArtifactIntegrityError, match="strategy/linked.py"):
        artifact_manifest(bot)

    (bot / "strategy" / "linked.py").unlink()
    external_directory = tmp_path / "external-strategy"
    external_directory.mkdir()
    (external_directory / "policy.py").write_text("ACTION = 'fold'\n", encoding="utf-8")
    (bot / "linked_strategy").symlink_to(external_directory, target_is_directory=True)

    with pytest.raises(ArtifactIntegrityError, match="linked_strategy"):
        hash_path(bot)


def test_hidden_payload_is_hashed(tmp_path):
    bot = tmp_path / "national_v3"
    _write_bot(bot)
    payload = bot / ".payload.py"
    payload.write_text("ACTION = 'check'\n", encoding="utf-8")
    before = hash_path(bot)

    payload.write_text("ACTION = 'allin'\n", encoding="utf-8")

    assert hash_path(bot) != before


def test_completion_marker_and_python_caches_are_excluded(tmp_path):
    bot = tmp_path / "national_v4"
    _write_bot(bot)
    before = hash_path(bot)

    (bot / ".completed").write_text("runtime marker\n", encoding="utf-8")
    cache = bot / "__pycache__"
    cache.mkdir()
    (cache / "national_bot.cpython-313.pyc").write_bytes(b"cache-one")
    (bot / "legacy.pyo").write_bytes(b"cache-two")

    assert hash_path(bot) == before

    (bot / ".completed").write_text("changed marker\n", encoding="utf-8")
    (cache / "national_bot.cpython-313.pyc").write_bytes(b"changed cache")
    (bot / "legacy.pyo").write_bytes(b"changed optimized cache")

    assert hash_path(bot) == before


def test_manifest_and_hash_are_stable_for_ordinary_content(tmp_path):
    bot = tmp_path / "national_v5"
    _write_bot(bot)
    (bot / "empty").mkdir()

    first_manifest = artifact_manifest(bot)
    first_hash = hash_path(bot)

    assert artifact_manifest(bot) == first_manifest
    assert hash_path(bot) == first_hash
    assert first_manifest["entries"] == [
        {"path": ".", "type": "directory"},
        {"path": "empty", "type": "directory"},
        {
            "path": "national_bot.py",
            "type": "file",
            "size": 15,
            "sha256": "e13cdff7de4a58aa927d0812d424a163af37d0619bebb12c0e68f042cd02a511",
        },
        {"path": "strategy", "type": "directory"},
        {
            "path": "strategy/policy.py",
            "type": "file",
            "size": 17,
            "sha256": "95768bc5058e1402f3598a1474b90d6c84def3d920728b46d8efb1d3858023c0",
        },
    ]


def _completion_tag_git(expected, *, duplicate_certificate=False):
    def fake_git(*args):
        if args[0] == "for-each-ref":
            certificate_line = "official-certificate: " + expected["official-certificate"] + "\n"
            return SimpleNamespace(returncode=0, stdout=(
                certificate_line
                + (certificate_line if duplicate_certificate else "")
                + "official-candidate-hash: " + expected["official-candidate-hash"] + "\n"
                + "official-policy: " + expected["official-policy"] + "\n"
            ))
        if args[0] == "ls-tree":
            return SimpleNamespace(
                returncode=0,
                stdout="official_certificates/national_v143.json\n",
            )
        return SimpleNamespace(returncode=0, stdout="")

    return fake_git


def _published_completion_identity(expected):
    return {
        "tag": "national-bot-v143",
        "tag_type": "tag",
        "tag_object": "c" * 40,
        "commit_oid": "d" * 40,
        "artifact_hash": expected["official-candidate-hash"],
        "migrated_since_completion": False,
        "issues": [],
    }


def test_completion_tag_validation_rejects_duplicate_metadata(monkeypatch, tmp_path):
    bot = tmp_path / "national_v143"
    bot.mkdir()
    expected = {
        "official-certificate": "a" * 64,
        "official-candidate-hash": "b" * 64,
        "official-policy": "official-full-v4",
    }
    monkeypatch.setattr(
        bot_artifact,
        "published_bot_identity",
        lambda _path: _published_completion_identity(expected),
    )
    monkeypatch.setattr(
        bot_artifact,
        "_git",
        _completion_tag_git(expected, duplicate_certificate=True),
    )

    result = validate_completion_tag(
        bot,
        expected_metadata=expected,
        certificate_path="official_certificates/national_v143.json",
    )

    assert result["valid"] is False
    assert "completion_tag_metadata_mismatch:official-certificate" in result["issues"]


def test_completion_tag_validation_accepts_exact_annotated_tag(monkeypatch, tmp_path):
    bot = tmp_path / "national_v143"
    bot.mkdir()
    expected = {
        "official-certificate": "a" * 64,
        "official-candidate-hash": "b" * 64,
        "official-policy": "official-full-v4",
    }
    monkeypatch.setattr(
        bot_artifact,
        "published_bot_identity",
        lambda _path: _published_completion_identity(expected),
    )
    monkeypatch.setattr(bot_artifact, "_git", _completion_tag_git(expected))

    result = validate_completion_tag(
        bot,
        expected_metadata=expected,
        certificate_path="official_certificates/national_v143.json",
    )

    assert result["valid"] is True
