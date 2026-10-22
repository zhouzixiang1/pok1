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
from conftest import strict_bot_name, strict_bot_tag


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
                stdout=f"official_certificates/{strict_bot_name()}.json\n",
            )
        if args[:2] == ("rev-parse", "--verify"):
            # The publication-branch tip the tag-commit-on-main ancestry leg
            # resolves before grading (commit "d"*40 is its ancestor via the
            # merge-base answer below).
            return SimpleNamespace(returncode=0, stdout="e" * 40 + "\n")
        return SimpleNamespace(returncode=0, stdout="")

    return fake_git


def _published_completion_identity(expected):
    return {
        "tag": strict_bot_tag(),
        "tag_type": "tag",
        "tag_object": "c" * 40,
        "commit_oid": "d" * 40,
        "artifact_hash": expected["official-candidate-hash"],
        "tag_artifact_hash": expected["official-candidate-hash"],
        "migrated_since_completion": False,
        "issues": [],
    }


def test_completion_tag_validation_rejects_duplicate_metadata(monkeypatch, tmp_path):
    bot = tmp_path / strict_bot_name()
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
        certificate_path=f"official_certificates/{strict_bot_name()}.json",
    )

    assert result["valid"] is False
    assert "completion_tag_metadata_mismatch:official-certificate" in result["issues"]


def test_completion_tag_validation_accepts_exact_annotated_tag(monkeypatch, tmp_path):
    bot = tmp_path / strict_bot_name()
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
        certificate_path=f"official_certificates/{strict_bot_name()}.json",
    )

    assert result["valid"] is True


# --- Frozen-vs-live comparisons grade through the publication_reproof engine
# (2026-10-22 migration): the completion-tag metadata alias ring through the
# engine catalog site ``bot_artifact.completion_tag_metadata``; the module's
# other producer-local judgments (scalar candidate-hash fallback, non-alias
# metadata rows, tag-commit-on-main ancestry, completion-tree equality)
# through the module-local declarations registered import-time.


def _spy_bot_artifact_engine(monkeypatch):
    import publication_reproof

    calls: list = []

    def spy(site_id, **kwargs):
        calls.append((site_id, kwargs.get("context") or {}))
        return publication_reproof.run_reproof(site_id, **kwargs)

    monkeypatch.setattr(bot_artifact, "run_reproof", spy, raising=False)
    return calls


def test_completion_tag_comparisons_route_through_reproof_engine(
    monkeypatch, tmp_path
):
    calls = _spy_bot_artifact_engine(monkeypatch)
    bot = tmp_path / strict_bot_name()
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
        certificate_path=f"official_certificates/{strict_bot_name()}.json",
    )
    assert result["valid"] is True
    names = {call[0] for call in calls}
    # (781-787) scalar official/staging candidate-hash fallback
    assert "bot_artifact.completion_candidate_hash" in names
    # (822-836) the alias ring for the candidate-hash spellings ...
    assert "bot_artifact.completion_tag_metadata" in names
    # ... and the exact single-row grade for non-alias metadata keys
    assert "bot_artifact.completion_tag_metadata_exact" in names
    # ... and the tag-commit-on-main ancestry leg (the branch tip resolved
    # through the _git seam, then graded oid-vs-oid by the same site the
    # published-identity path uses).
    assert "bot_artifact.tag_commit_on_main" in names
    # A non-ancestor branch tip still fails closed with the historical token.
    calls.clear()

    def non_ancestor_git(*args):
        result_git = _completion_tag_git(expected)(*args)
        if args[:2] == ("rev-parse", "--verify"):
            return SimpleNamespace(returncode=0, stdout="f" * 40 + "\n")
        if args[0] == "merge-base":
            return SimpleNamespace(returncode=1, stdout="")
        return result_git

    monkeypatch.setattr(bot_artifact, "_git", non_ancestor_git)
    result = validate_completion_tag(
        bot,
        expected_metadata=expected,
        certificate_path=f"official_certificates/{strict_bot_name()}.json",
    )
    assert "completion_tag_commit_not_on_main" in result["issues"]
    assert "bot_artifact.tag_commit_on_main" in {call[0] for call in calls}

    # The duplicate-row rejection still routes through the engine.
    calls.clear()
    monkeypatch.setattr(
        bot_artifact,
        "_git",
        _completion_tag_git(expected, duplicate_certificate=True),
    )
    result = validate_completion_tag(
        bot,
        expected_metadata=expected,
        certificate_path=f"official_certificates/{strict_bot_name()}.json",
    )
    assert result["valid"] is False
    assert (
        "completion_tag_metadata_mismatch:official-certificate"
        in result["issues"]
    )
    assert {
        call[0] for call in calls
    } & {"bot_artifact.completion_tag_metadata_exact"} == {
        "bot_artifact.completion_tag_metadata_exact"
    }


def test_published_identity_comparisons_route_through_reproof_engine(
    monkeypatch, tmp_path
):
    calls = _spy_bot_artifact_engine(monkeypatch)
    bots = tmp_path / "bots"
    bots.mkdir()
    bot = bots / strict_bot_name()
    _write_bot(bot)
    monkeypatch.setattr(bot_artifact, "ROOT", tmp_path)
    tag = strict_bot_tag()
    name = strict_bot_name()
    commit_oid = "d" * 40
    main_oid = "e" * 40
    tree_oid = "a" * 40
    tag_object = "c" * 40

    def identity_git(*args):
        arg0 = args[0]
        if arg0 == "cat-file":
            return SimpleNamespace(returncode=0, stdout="tag")
        if arg0 == "rev-parse":
            target = args[1]
            if target == f"refs/tags/{tag}":
                return SimpleNamespace(returncode=0, stdout=tag_object)
            if target == "--verify":
                return SimpleNamespace(returncode=0, stdout=main_oid)
            return SimpleNamespace(returncode=0, stdout=tree_oid)
        if arg0 == "rev-list":
            return SimpleNamespace(returncode=0, stdout=commit_oid)
        if arg0 == "merge-base":
            return SimpleNamespace(returncode=0, stdout="")
        if arg0 == "diff":
            return SimpleNamespace(returncode=0, stdout="")
        if arg0 == "ls-files":
            return SimpleNamespace(returncode=0, stdout="")
        if arg0 == "for-each-ref":
            return SimpleNamespace(returncode=0, stdout="")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(bot_artifact, "_git", identity_git)
    monkeypatch.setattr(
        bot_artifact,
        "git_tree_artifact_manifest",
        lambda *_a, **_k: {"manifest": 1},
    )
    identity = bot_artifact.published_bot_identity(bot)
    assert identity["tag_commit_on_main"] is True
    assert identity["completion_tree_matches_main"] is True
    names = {call[0] for call in calls}
    # (641-652) publication commit ancestry on the configured main ref
    assert "bot_artifact.tag_commit_on_main" in names
    # (664-670) completion-tree equality against the main tree
    assert "bot_artifact.completion_tree_matches_main" in names
    assert name  # silence linters on the unused local


def test_completion_tag_alias_ring_stays_unidirectional(monkeypatch, tmp_path):
    # 2026-10-23 review pin: the #24 spelling alias widened ONLY the
    # official-expected → staging-observed direction.  The engine alias
    # site's live resolution (official→staging→legacy) would also accept
    # staging-expected + official-observed and bare-expected + official/
    # staging-observed — a reverse widening.  Those directions stay exact
    # single-key grades and still fail closed with the historical token.
    calls = _spy_bot_artifact_engine(monkeypatch)
    bot = tmp_path / strict_bot_name()
    bot.mkdir()
    tag_hash = "b" * 64

    def identity():
        return {
            "tag": strict_bot_tag(),
            "tag_type": "tag",
            "tag_object": "c" * 40,
            "commit_oid": "d" * 40,
            "tag_artifact_hash": tag_hash,
            "migrated_since_completion": False,
            "issues": [],
        }

    def observed_git(observed_key):
        def fake_git(*args):
            if args[0] == "for-each-ref":
                return SimpleNamespace(
                    returncode=0,
                    stdout=f"{observed_key}: {tag_hash}\n",
                )
            if args[:2] == ("rev-parse", "--verify"):
                return SimpleNamespace(returncode=0, stdout="e" * 40 + "\n")
            if args[0] == "ls-tree":
                return SimpleNamespace(returncode=0, stdout="")
            return SimpleNamespace(returncode=0, stdout="")

        return fake_git

    monkeypatch.setattr(
        bot_artifact, "published_bot_identity", lambda _path: identity()
    )

    # staging-expected + official-observed (SAME value): still a mismatch —
    # the alias only ever widened official-expected.
    monkeypatch.setattr(
        bot_artifact, "_git", observed_git("official-candidate-hash")
    )
    result = validate_completion_tag(
        bot,
        expected_metadata={"staging-candidate-hash": tag_hash},
        certificate_path="",
    )
    assert (
        "completion_tag_metadata_mismatch:staging-candidate-hash"
        in result["issues"]
    )
    # The engine alias site IS consulted (one call for the whole candidate-
    # hash expectation set) and its ONE-WAY rule produces the mismatch
    # itself — no adapter-side direction gate remains.
    assert "bot_artifact.completion_tag_metadata" in {
        call[0] for call in calls
    }

    # bare-expected + official-observed: same exact-key verdict.
    calls.clear()
    result = validate_completion_tag(
        bot,
        expected_metadata={"candidate-hash": tag_hash},
        certificate_path="",
    )
    assert "completion_tag_metadata_mismatch:candidate-hash" in result["issues"]

    # The widened direction itself stays accepted: official-expected +
    # staging-observed routes through the engine alias site.
    calls.clear()
    monkeypatch.setattr(
        bot_artifact, "_git", observed_git("staging-candidate-hash")
    )
    result = validate_completion_tag(
        bot,
        expected_metadata={"official-candidate-hash": tag_hash},
        certificate_path="",
    )
    assert "completion_tag_metadata_mismatch:official-candidate-hash" not in (
        result["issues"]
    )
    assert "bot_artifact.completion_tag_metadata" in {
        call[0] for call in calls
    }


def test_completion_tag_empty_candidate_hash_expectation_fails(
    monkeypatch, tmp_path
):
    # 2026-10-23 review pin: a present-but-EMPTY candidate-hash expectation
    # plus a missing observation was a hard ``not expected_hash`` failure
    # before the migration; the engine alias site would normalize the pair
    # to None/None and pass.  The frozen-side or-chain resolution stays in
    # the adapter as the availability gate, so the failure verdict is kept.
    calls = _spy_bot_artifact_engine(monkeypatch)
    bot = tmp_path / strict_bot_name()
    bot.mkdir()
    monkeypatch.setattr(
        bot_artifact,
        "published_bot_identity",
        lambda _path: {
            "tag": strict_bot_tag(),
            "tag_type": "tag",
            "tag_object": "c" * 40,
            "commit_oid": "d" * 40,
            "tag_artifact_hash": "",
            "migrated_since_completion": False,
            "issues": [],
        },
    )

    def empty_git(*args):
        if args[:2] == ("rev-parse", "--verify"):
            return SimpleNamespace(returncode=0, stdout="e" * 40 + "\n")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(bot_artifact, "_git", empty_git)
    result = validate_completion_tag(
        bot,
        expected_metadata={"official-candidate-hash": ""},
        certificate_path="",
    )
    assert "completion_tag_candidate_hash_mismatch" in result["issues"]
    # The engine graded no scalar pair for the unresolvable expectation.
    assert "bot_artifact.completion_candidate_hash" not in {
        call[0] for call in calls
    }
