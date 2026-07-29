import json
from pathlib import Path

import pytest

from bot_namespace import (
    NATIONAL_RUNTIME_MANIFEST,
    POLICY_EPOCH_RECEIPT,
    ROLE_OFFICIAL_OPPONENT,
    ROLE_PARENT_SOURCE,
    ROLE_RATING_POOL,
    artifact_contract_digest,
    bot_name,
    bot_tag,
    bot_tag_glob,
    build_policy_epoch_receipt,
    build_runtime_manifest,
    high_water_tag,
    high_water_tag_glob,
    parse_bot_version,
    resolve_national_bot_spec,
    strict_generation_identity,
)
from conftest import STRICT_TARGET_V, STRICT_SOURCE_V, strict_bot_name, strict_bot_tag


def _write_policy_bot(root: Path, version: int, *, parents=()) -> Path:
    bot = root / "bots" / bot_name(version)
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
    assert parse_bot_version(strict_bot_name()) == STRICT_TARGET_V
    assert parse_bot_version("claude_v143") is None
    assert parse_bot_version("v143") is None
    assert parse_bot_version(bot_name(0)) is None


def test_strict_generation_identity_maps_immutable_versions_to_ui_ordinals():
    later = STRICT_TARGET_V + 4
    assert strict_generation_identity(STRICT_TARGET_V, generation_ordinal=1) == {
        "generation_ordinal": 1,
        "canonical_version": STRICT_TARGET_V,
        "canonical_bot_name": strict_bot_name(),
        "canonical_tag": strict_bot_tag(),
    }
    assert strict_generation_identity(later, generation_ordinal=2) == {
        "generation_ordinal": 2,
        "canonical_version": later,
        "canonical_bot_name": bot_name(later),
        "canonical_tag": bot_tag(later),
    }


@pytest.mark.parametrize("value", [True, False, "143", 143.0, None])
def test_strict_generation_identity_rejects_non_integer_versions(value):
    with pytest.raises(TypeError):
        strict_generation_identity(value, generation_ordinal=1)


@pytest.mark.parametrize("value", [STRICT_SOURCE_V, -1])
def test_strict_generation_identity_rejects_pre_epoch_versions(value):
    with pytest.raises(ValueError):
        strict_generation_identity(value, generation_ordinal=1)


@pytest.mark.parametrize("value", [True, False, "2", 2.0, None])
def test_strict_generation_identity_rejects_non_integer_ordinals(value):
    with pytest.raises(TypeError):
        strict_generation_identity(147, generation_ordinal=value)


@pytest.mark.parametrize("value", [0, -1])
def test_strict_generation_identity_rejects_non_positive_ordinals(value):
    with pytest.raises(ValueError):
        strict_generation_identity(147, generation_ordinal=value)


def test_first_strict_candidate_has_fresh_noninherited_lineage(tmp_path):
    bot = _write_policy_bot(tmp_path, STRICT_TARGET_V)

    spec = resolve_national_bot_spec(bot, repo_root=tmp_path)

    assert spec.eligible is True
    assert spec.epoch_receipt["lineage"] == {
        "mode": "fresh_bootstrap",
        "parent_versions": [],
        "version_authority_high_water": STRICT_SOURCE_V,
        "source_artifact_inherited": False,
    }
    assert spec.epoch_receipt["artifact_contract_digest"] == artifact_contract_digest(
        spec.runtime_manifest
    )


def test_later_strict_candidate_requires_strict_parent_lineage(tmp_path):
    bot = _write_policy_bot(tmp_path, STRICT_TARGET_V + 1, parents=(STRICT_TARGET_V,))

    assert resolve_national_bot_spec(bot, repo_root=tmp_path).eligible is True

    receipt_path = bot / POLICY_EPOCH_RECEIPT
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["lineage"]["parent_versions"] = [STRICT_SOURCE_V]
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    spec = resolve_national_bot_spec(bot, repo_root=tmp_path)
    assert spec.eligible is False
    assert "strict_lineage_parent_versions_invalid" in spec.issues


def test_core_policy_tamper_is_rejected_by_runtime_manifest(tmp_path):
    bot = _write_policy_bot(tmp_path, STRICT_TARGET_V)
    (bot / "policy.py").write_text("# tampered\n", encoding="utf-8")

    spec = resolve_national_bot_spec(bot, repo_root=tmp_path)

    assert spec.eligible is False
    assert "runtime_core_file_digest_mismatch:policy.py" in spec.issues


def test_archive_path_and_pre_policy_version_never_resolve(tmp_path):
    archived = tmp_path / "archive" / "bots" / bot_name(STRICT_SOURCE_V)
    archived.mkdir(parents=True)

    spec = resolve_national_bot_spec(archived, repo_root=tmp_path)

    assert spec.eligible is False
    assert "bot_path_not_in_active_namespace" in spec.issues
    # STRICT_SOURCE_V (ARCHIVED_VERSION_HIGH_WATER) sits below the strict floor.
    # Where it parses as a version (main: 142 < 143) it is rejected as
    # pre_policy_epoch_bot_archived; on a cloud namespace whose floor is 1 the
    # "v0" label is itself unparseable and surfaces as invalid_national_bot_label.
    # Either way the archived bot can never resolve to an active role.
    if STRICT_SOURCE_V > 0:
        assert "pre_policy_epoch_bot_archived" in spec.issues
    else:
        assert "invalid_national_bot_label" in spec.issues


def test_active_namespace_symlink_cannot_redirect_discovery_into_archive(tmp_path):
    archive_bots = tmp_path / "archive" / "bots"
    archive_bots.mkdir(parents=True)
    (tmp_path / "bots").symlink_to(archive_bots, target_is_directory=True)

    spec = resolve_national_bot_spec(strict_bot_name(), repo_root=tmp_path)

    assert spec.eligible is False
    assert "bot_path_not_in_active_namespace" in spec.issues


def test_published_role_requires_completion_identity_and_full_certificate(tmp_path):
    bot = _write_policy_bot(tmp_path, STRICT_TARGET_V)
    publication = lambda _path: {
        "published": True,
        "version": STRICT_TARGET_V,
        "tag": strict_bot_tag(),
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
        require_certificate=True,
    )

    assert spec.eligible is True
    assert spec.certificate_digest == "a" * 64
    assert spec.publication_tier == "certified"


def test_published_role_fails_closed_without_certificate(tmp_path):
    """Rating pool and official opponents ALWAYS require a certificate.

    With two-tier publication, parent_source accepts staging (no cert), but
    rating_pool and official_opponent must remain certified-only regardless
    of the ALLOW_STAGING_AS_PARENT flag.
    """
    bot = _write_policy_bot(tmp_path, STRICT_TARGET_V)
    publication = lambda _path: {
        "published": True,
        "version": STRICT_TARGET_V,
        "tag": strict_bot_tag(),
    }
    no_cert = lambda _path: {
        "eligible": False,
        "certificate_digest": "",
    }

    # Rating pool: must fail closed without certificate.
    rating_spec = resolve_national_bot_spec(
        bot,
        ROLE_RATING_POOL,
        repo_root=tmp_path,
        publication_resolver=publication,
        certificate_resolver=no_cert,
    )
    assert rating_spec.eligible is False
    assert "signed_full_official_certificate_required" in rating_spec.issues

    # Official opponent: must fail closed without certificate.
    opponent_spec = resolve_national_bot_spec(
        bot,
        ROLE_OFFICIAL_OPPONENT,
        repo_root=tmp_path,
        publication_resolver=publication,
        certificate_resolver=no_cert,
    )
    assert opponent_spec.eligible is False
    assert "signed_full_official_certificate_required" in opponent_spec.issues


def test_staging_parent_source_accepted_without_certificate(tmp_path):
    """Two-tier: parent_source accepts a staging bot (no cert) when the flag is on."""
    bot = _write_policy_bot(tmp_path, STRICT_TARGET_V)

    spec = resolve_national_bot_spec(
        bot,
        ROLE_PARENT_SOURCE,
        repo_root=tmp_path,
        publication_resolver=lambda _path: {
            "published": True,
            "version": STRICT_TARGET_V,
            "tag": strict_bot_tag(),
        },
        certificate_resolver=lambda _path: {
            "eligible": False,
            "certificate_digest": "",
        },
    )

    assert spec.eligible is True
    assert spec.publication_tier == "staging"
    assert spec.certificate_digest == ""


def test_version_authority_uses_tags_and_ignores_stale_directory(monkeypatch, tmp_path):
    import evolution_infra

    bots = tmp_path / "bots"
    # A stale directory at a higher version must NOT count as version authority.
    (bots / bot_name(STRICT_TARGET_V + 12)).mkdir(parents=True)
    monkeypatch.setattr(evolution_infra, "BOTS_DIR", bots)

    # STRICT_SOURCE_V is the archived high-water: below the strict floor where it
    # parses (main: 142 < 143) and an unparseable "v0" label on a floor-1 cloud
    # namespace. In both cases the namespace resolves to the archived floor and
    # the version does not enter the strict tagged-bot pool.
    archived_v = STRICT_SOURCE_V
    completion_tag = bot_tag(archived_v)
    high_water = high_water_tag(archived_v)

    def git(*args, **_kwargs):
        if args[:2] == (
            "for-each-ref",
            "--format=%(objecttype)%09%(*objecttype)%09%(refname:short)",
        ):
            return f"tag\tcommit\t{completion_tag}\ntag\tcommit\t{high_water}\n"
        if args[:3] == ("tag", "-l", bot_tag_glob()):
            return f"{completion_tag}\n"
        if args[:3] == ("tag", "-l", high_water_tag_glob()):
            return f"{high_water}\n"
        if args == ("rev-parse", f"refs/tags/{completion_tag}^{{commit}}"):
            return "a" * 40
        if args == ("rev-parse", f"refs/tags/{high_water}^{{commit}}"):
            return "a" * 40
        return ""

    monkeypatch.setattr(evolution_infra, "_git", git)

    assert evolution_infra.find_current_v() == STRICT_SOURCE_V
    assert evolution_infra._tagged_bot_versions() == set()


def test_version_authority_ignores_lightweight_completion_and_high_water_tags(
    monkeypatch,
):
    import evolution_infra

    archived_v = STRICT_SOURCE_V
    completion_tag = bot_tag(archived_v)
    high_water = high_water_tag(archived_v)

    monkeypatch.setattr(
        evolution_infra,
        "_git",
        lambda *args, **_kwargs: (
            f"tag\tcommit\t{completion_tag}\n"
            f"tag\tcommit\t{high_water}\n"
            f"commit\t\t{bot_tag(archived_v + 999)}\n"
            f"commit\t\t{high_water_tag(archived_v + 1000)}\n"
        ) if args and args[0] == "for-each-ref" else (
            "a" * 40 if args and args[0] == "rev-parse" else ""
        ),
    )

    assert evolution_infra.find_current_v() == STRICT_SOURCE_V


def test_version_authority_falls_back_to_archived_floor_without_annotated_tag(
    monkeypatch,
):
    """An empty namespace resolves to the archived floor instead of raising.

    A fresh deployment namespace with no paired annotated completion/high-water
    tags is the legitimate bootstrap state: it sits at ARCHIVED_VERSION_HIGH_WATER
    so version allocation and epoch state stay well-defined before the first
    strict publication. The strict fail-closed contract lives at the
    ``resolve_version_namespace_authority`` resolver boundary (see below).
    """
    import evolution_infra

    archived_v = STRICT_SOURCE_V
    monkeypatch.setattr(
        evolution_infra,
        "_git",
        lambda *args, **_kwargs: (
            f"commit\t\t{high_water_tag(archived_v)}\n"
        ) if args and args[0] == "for-each-ref" else "",
    )

    assert evolution_infra.find_current_v() == STRICT_SOURCE_V


def test_version_authority_ignores_unpaired_annotated_tag(monkeypatch):
    import evolution_infra

    paired = STRICT_TARGET_V
    completion_tag = bot_tag(paired)
    high_water = high_water_tag(paired)
    unpaired_completion = bot_tag(paired + 999)

    def git(*args, **_kwargs):
        if args and args[0] == "for-each-ref":
            return (
                f"tag\tcommit\t{completion_tag}\n"
                f"tag\tcommit\t{high_water}\n"
                f"tag\tcommit\t{unpaired_completion}\n"
            )
        if args and args[0] == "rev-parse":
            return "a" * 40
        return ""

    monkeypatch.setattr(evolution_infra, "_git", git)

    assert evolution_infra.find_current_v() == paired


def test_version_authority_rejects_pair_at_different_commits(monkeypatch):
    import evolution_infra

    paired = STRICT_TARGET_V
    completion_tag = bot_tag(paired)
    high_water = high_water_tag(paired)

    def git(*args, **_kwargs):
        if args and args[0] == "for-each-ref":
            return (
                f"tag\tcommit\t{completion_tag}\n"
                f"tag\tcommit\t{high_water}\n"
            )
        if args == ("rev-parse", f"refs/tags/{completion_tag}^{{commit}}"):
            return "a" * 40
        if args == ("rev-parse", f"refs/tags/{high_water}^{{commit}}"):
            return "b" * 40
        return ""

    monkeypatch.setattr(evolution_infra, "_git", git)

    # find_current_v/version_namespace_authority tolerate an unresolved namespace
    # as the bootstrap floor, so the strict commit-pairing contract is asserted
    # at the resolver boundary where it is actually enforced.
    with pytest.raises(RuntimeError, match=f"commit mismatch for v{paired}"):
        evolution_infra.resolve_version_namespace_authority(git)
