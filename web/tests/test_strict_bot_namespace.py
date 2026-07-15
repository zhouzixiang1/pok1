import json
from pathlib import Path

import pytest

from bot_namespace import (
    NATIONAL_RUNTIME_MANIFEST,
    POLICY_EPOCH_RECEIPT,
    ROLE_PARENT_SOURCE,
    artifact_contract_digest,
    build_policy_epoch_receipt,
    build_runtime_manifest,
    parse_bot_version,
    resolve_national_bot_spec,
)


def _write_policy_bot(root: Path, version: int, *, parents=()) -> Path:
    bot = root / "bots" / f"national_v{version}"
    bot.mkdir(parents=True)
    (bot / "national_bot.py").write_text("# raw TCP system entry\n", encoding="utf-8")
    (bot / "policy.py").write_text(
        "def get_baseline_decision(context):\n"
        "    return {'kind': 'pass'}\n\n"
        "def iter_decisions(context, baseline, deadline):\n"
        "    return ()\n",
        encoding="utf-8",
    )
    (bot / "precompute.py").write_text("TABLE = ()\n", encoding="utf-8")
    manifest = build_runtime_manifest(bot)
    (bot / NATIONAL_RUNTIME_MANIFEST).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    receipt = build_policy_epoch_receipt(bot, version, parent_versions=parents)
    (bot / POLICY_EPOCH_RECEIPT).write_text(
        json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
    )
    return bot


def test_parser_accepts_only_canonical_active_namespace():
    assert parse_bot_version("national_v143") == 143
    assert parse_bot_version("claude_v143") is None
    assert parse_bot_version("v143") is None
    assert parse_bot_version("national_v0") is None


def test_first_strict_candidate_has_fresh_noninherited_lineage(tmp_path):
    bot = _write_policy_bot(tmp_path, 143)

    spec = resolve_national_bot_spec(bot, repo_root=tmp_path)

    assert spec.eligible is True
    assert spec.epoch_receipt["lineage"] == {
        "mode": "fresh_bootstrap",
        "parent_versions": [],
        "version_authority_high_water": 142,
        "source_artifact_inherited": False,
    }
    assert spec.epoch_receipt["artifact_contract_digest"] == artifact_contract_digest(
        spec.runtime_manifest
    )


def test_later_strict_candidate_requires_strict_parent_lineage(tmp_path):
    bot = _write_policy_bot(tmp_path, 144, parents=(143,))

    assert resolve_national_bot_spec(bot, repo_root=tmp_path).eligible is True

    receipt_path = bot / POLICY_EPOCH_RECEIPT
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["lineage"]["parent_versions"] = [142]
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    spec = resolve_national_bot_spec(bot, repo_root=tmp_path)
    assert spec.eligible is False
    assert "strict_lineage_parent_versions_invalid" in spec.issues


def test_core_policy_tamper_is_rejected_by_runtime_manifest(tmp_path):
    bot = _write_policy_bot(tmp_path, 143)
    (bot / "policy.py").write_text("# tampered\n", encoding="utf-8")

    spec = resolve_national_bot_spec(bot, repo_root=tmp_path)

    assert spec.eligible is False
    assert "runtime_core_file_digest_mismatch:policy.py" in spec.issues


def test_archive_path_and_pre_policy_version_never_resolve(tmp_path):
    archived = tmp_path / "archive" / "bots" / "national_v142"
    archived.mkdir(parents=True)

    spec = resolve_national_bot_spec(archived, repo_root=tmp_path)

    assert spec.eligible is False
    assert "bot_path_not_in_active_namespace" in spec.issues
    assert "pre_policy_epoch_bot_archived" in spec.issues


def test_active_namespace_symlink_cannot_redirect_discovery_into_archive(tmp_path):
    archive_bots = tmp_path / "archive" / "bots"
    archive_bots.mkdir(parents=True)
    (tmp_path / "bots").symlink_to(archive_bots, target_is_directory=True)

    spec = resolve_national_bot_spec("national_v143", repo_root=tmp_path)

    assert spec.eligible is False
    assert "bot_path_not_in_active_namespace" in spec.issues


def test_published_role_requires_completion_identity_and_full_certificate(tmp_path):
    bot = _write_policy_bot(tmp_path, 143)
    publication = lambda _path: {
        "published": True,
        "version": 143,
        "tag": "national-bot-v143",
    }
    certificate = lambda _path: {
        "eligible": True,
        "certificate_digest": "a" * 64,
    }

    spec = resolve_national_bot_spec(
        bot,
        ROLE_PARENT_SOURCE,
        repo_root=tmp_path,
        publication_resolver=publication,
        certificate_resolver=certificate,
    )

    assert spec.eligible is True
    assert spec.certificate_digest == "a" * 64


def test_published_role_fails_closed_without_certificate(tmp_path):
    bot = _write_policy_bot(tmp_path, 143)

    spec = resolve_national_bot_spec(
        bot,
        ROLE_PARENT_SOURCE,
        repo_root=tmp_path,
        publication_resolver=lambda _path: {
            "published": True,
            "version": 143,
            "tag": "national-bot-v143",
        },
        certificate_resolver=lambda _path: {
            "eligible": False,
            "certificate_digest": "",
        },
    )

    assert spec.eligible is False
    assert "signed_full_official_certificate_required" in spec.issues
    assert "official_certificate_digest_invalid" in spec.issues


def test_version_authority_uses_tags_and_ignores_stale_directory(monkeypatch, tmp_path):
    import evolution_infra

    bots = tmp_path / "bots"
    (bots / "national_v155").mkdir(parents=True)
    monkeypatch.setattr(evolution_infra, "BOTS_DIR", bots)

    def git(*args, **_kwargs):
        if args[:2] == ("for-each-ref", "--format=%(objecttype)%09%(*objecttype)%09%(refname:short)"):
            return (
                "tag\tcommit\tnational-bot-v141\n"
                "tag\tcommit\tnational-bot-v142\n"
                "tag\tcommit\tnational-high-water-v142\n"
            )
        if args[:3] == ("tag", "-l", "national-bot-v*"):
            return "national-bot-v141\nnational-bot-v142\n"
        if args[:3] == ("tag", "-l", "national-high-water-v*"):
            return "national-high-water-v142\n"
        if args == ("rev-parse", "refs/tags/national-bot-v142^{commit}"):
            return "a" * 40
        if args == ("rev-parse", "refs/tags/national-high-water-v142^{commit}"):
            return "a" * 40
        return ""

    monkeypatch.setattr(evolution_infra, "_git", git)

    assert evolution_infra.find_current_v() == 142
    assert evolution_infra._tagged_bot_versions() == set()


def test_version_authority_ignores_lightweight_completion_and_high_water_tags(
    monkeypatch,
):
    import evolution_infra

    monkeypatch.setattr(
        evolution_infra,
        "_git",
        lambda *args, **_kwargs: (
            "tag\tcommit\tnational-bot-v142\n"
            "tag\tcommit\tnational-high-water-v142\n"
            "commit\t\tnational-bot-v999\n"
            "commit\t\tnational-high-water-v1000\n"
        ) if args and args[0] == "for-each-ref" else (
            "a" * 40 if args and args[0] == "rev-parse" else ""
        ),
    )

    assert evolution_infra.find_current_v() == 142


def test_version_authority_fails_closed_without_annotated_tag(monkeypatch):
    import evolution_infra

    monkeypatch.setattr(
        evolution_infra,
        "_git",
        lambda *args, **_kwargs: (
            "commit\t\tnational-high-water-v142\n"
        ) if args and args[0] == "for-each-ref" else "",
    )

    with pytest.raises(RuntimeError, match="annotated completion/high-water"):
        evolution_infra.find_current_v()


def test_version_authority_ignores_unpaired_annotated_tag(monkeypatch):
    import evolution_infra

    def git(*args, **_kwargs):
        if args and args[0] == "for-each-ref":
            return (
                "tag\tcommit\tnational-bot-v142\n"
                "tag\tcommit\tnational-high-water-v142\n"
                "tag\tcommit\tnational-bot-v999\n"
            )
        if args and args[0] == "rev-parse":
            return "a" * 40
        return ""

    monkeypatch.setattr(evolution_infra, "_git", git)

    assert evolution_infra.find_current_v() == 142


def test_version_authority_rejects_pair_at_different_commits(monkeypatch):
    import evolution_infra

    def git(*args, **_kwargs):
        if args and args[0] == "for-each-ref":
            return (
                "tag\tcommit\tnational-bot-v142\n"
                "tag\tcommit\tnational-high-water-v142\n"
            )
        if args == ("rev-parse", "refs/tags/national-bot-v142^{commit}"):
            return "a" * 40
        if args == ("rev-parse", "refs/tags/national-high-water-v142^{commit}"):
            return "b" * 40
        return ""

    monkeypatch.setattr(evolution_infra, "_git", git)

    with pytest.raises(RuntimeError, match="commit mismatch for v142"):
        evolution_infra.find_current_v()
