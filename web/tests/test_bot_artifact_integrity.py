from __future__ import annotations

from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

import bot_artifact
from bot_artifact import (
    ArtifactIntegrityError,
    artifact_manifest,
    publication_shape_errors,
    validate_staged_artifact,
    hash_path,
    validate_completion_tag,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
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


def test_completion_marker_and_control_plane_caches_are_excluded(tmp_path):
    bot = tmp_path / "national_v4"
    _write_bot(bot)
    before = hash_path(bot)

    (bot / ".completed").write_text("runtime marker\n", encoding="utf-8")
    cache = bot / "__pycache__"
    cache.mkdir()
    (cache / "national_bot.cpython-313.pyc").write_bytes(b"cache-one")
    (bot / "legacy.pyo").write_bytes(b"cache-two")
    task_context = bot / ".task_context"
    task_context.mkdir()
    (task_context / "w1.md").write_text("system prompt context\n", encoding="utf-8")
    pytest_cache = bot / ".pytest_cache"
    pytest_cache.mkdir()
    (pytest_cache / "nodeids").write_text("[]\n", encoding="utf-8")

    assert hash_path(bot) == before

    (bot / ".completed").write_text("changed marker\n", encoding="utf-8")
    (cache / "national_bot.cpython-313.pyc").write_bytes(b"changed cache")
    (bot / "legacy.pyo").write_bytes(b"changed optimized cache")
    (task_context / "w1.md").write_text("changed context\n", encoding="utf-8")
    (pytest_cache / "nodeids").write_text("[\"changed\"]\n", encoding="utf-8")

    assert hash_path(bot) == before


def test_quality_fingerprint_uses_complete_artifact_hash(tmp_path):
    from tool_gates import _bot_code_fingerprint

    bot = tmp_path / "national_v41"
    bot.mkdir()
    (bot / "strategy.py").write_text("VALUE = 1\n", encoding="utf-8")
    (bot / "tables").mkdir()
    (bot / "tables" / "policy.bin").write_bytes(b"policy-v1")

    assert _bot_code_fingerprint(bot) == hash_path(bot)


def test_artifact_manifest_rejects_sparse_oversize_before_read(tmp_path, monkeypatch):
    bot = tmp_path / "national_v42"
    bot.mkdir()
    sparse = bot / "tables.bin"
    with sparse.open("wb") as stream:
        stream.truncate(bot_artifact.ARTIFACT_MAX_FILE_BYTES + 1)

    reads = []
    monkeypatch.setattr(
        bot_artifact,
        "_read_file_digest",
        lambda *_args, **_kwargs: reads.append(True) or "unreachable",
    )

    with pytest.raises(ArtifactIntegrityError, match="exceeds byte limit"):
        artifact_manifest(bot)
    assert reads == []


@pytest.mark.parametrize(
    ("limit_name", "limit", "files", "expected"),
    [
        ("ARTIFACT_MAX_FILE_COUNT", 2, {"a": b"1", "b": b"2", "c": b"3"}, "file count"),
        ("ARTIFACT_MAX_TOTAL_BYTES", 8, {"a": b"12345", "b": b"67890"}, "total bytes"),
    ],
)
def test_artifact_manifest_rejects_global_caps_before_payload_reads(
    tmp_path,
    monkeypatch,
    limit_name,
    limit,
    files,
    expected,
):
    bot = tmp_path / "national_v43"
    bot.mkdir()
    for name, payload in files.items():
        (bot / name).write_bytes(payload)
    monkeypatch.setattr(bot_artifact, limit_name, limit)
    reads = []
    monkeypatch.setattr(
        bot_artifact,
        "_read_file_digest",
        lambda *_args, **_kwargs: reads.append(True) or "unreachable",
    )

    with pytest.raises(ArtifactIntegrityError, match=expected):
        artifact_manifest(bot)
    assert reads == []


def test_artifact_manifest_rejects_depth_cap_before_payload_reads(tmp_path, monkeypatch):
    bot = tmp_path / "national_v44"
    deep = bot / "one" / "two" / "three"
    deep.mkdir(parents=True)
    (deep / "policy.bin").write_bytes(b"bounded")
    monkeypatch.setattr(bot_artifact, "ARTIFACT_MAX_DIRECTORY_DEPTH", 2)
    reads = []
    monkeypatch.setattr(
        bot_artifact,
        "_read_file_digest",
        lambda *_args, **_kwargs: reads.append(True) or "unreachable",
    )

    with pytest.raises(ArtifactIntegrityError, match="depth"):
        artifact_manifest(bot)
    assert reads == []


def test_staged_manifest_matches_blobs_and_rejects_ignored_or_empty_assets(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    (repo / ".gitignore").write_text("build/\n", encoding="utf-8")
    bot = repo / "bots" / "national_v1"
    (bot / "tables").mkdir(parents=True)
    (bot / "strategy.py").write_text("VALUE = 1\n", encoding="utf-8")
    (bot / "tables" / "policy.bin").write_bytes(b"packed-policy")
    _git(repo, "add", "--", "bots/national_v1")

    matched = validate_staged_artifact(bot, repo_root=repo)
    assert matched["valid"] is True
    assert matched["working_hash"] == matched["staged_hash"]

    (bot / "build").mkdir()
    (bot / "build" / "hidden.bin").write_bytes(b"ignored-policy")
    (bot / "empty_policy_dir").mkdir()
    _git(repo, "add", "--", "bots/national_v1")

    mismatched = validate_staged_artifact(bot, repo_root=repo)
    assert mismatched["valid"] is False
    errors = publication_shape_errors(bot, repo_root=repo)
    assert "git_ignored_artifact_file:build/hidden.bin" in errors
    assert "empty_directory_not_publishable:empty_policy_dir" in errors


def test_staged_manifest_rejects_nested_gitlink(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    bot = repo / "bots" / "national_v2"
    bot.mkdir(parents=True)
    (bot / "strategy.py").write_text("VALUE = 1\n", encoding="utf-8")
    nested = bot / "subrepo"
    nested.mkdir()
    _git(nested, "init")
    (nested / "policy.bin").write_bytes(b"nested-policy")
    _git(nested, "add", "policy.bin")
    _git(nested, "-c", "user.name=test", "-c", "user.email=test@example.com", "commit", "-m", "nested")
    _git(repo, "add", "--", "bots/national_v2")

    with pytest.raises(ArtifactIntegrityError, match="non-blob git mode"):
        validate_staged_artifact(bot, repo_root=repo)
    assert any(
        error.startswith("nested_git_metadata_forbidden:subrepo/.git")
        for error in publication_shape_errors(bot, repo_root=repo)
    )


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
        "tag_artifact_hash": expected["official-candidate-hash"],
        "migrated_since_completion": False,
        "issues": [],
    }


def test_completion_tag_validation_rejects_duplicate_metadata(monkeypatch, tmp_path):
    bot = tmp_path / "national_v143"
    bot.mkdir()
    expected = {
        "official-certificate": "a" * 64,
        "official-candidate-hash": "b" * 64,
        "official-policy": "official-full-v5",
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
        "official-policy": "official-full-v5",
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
