from pathlib import Path
import json

import official_eligibility
import pytest


@pytest.fixture(autouse=True)
def _healthy_epoch_registry(monkeypatch):
    monkeypatch.setattr(
        official_eligibility,
        "epoch_lifecycle_eligibility",
        lambda version: {"eligible": True, "reason": "national_epoch_active", "version": version},
    )
    monkeypatch.setattr(
        official_eligibility,
        "current_target_version",
        lambda requested=None: int(requested or 143),
    )


def _policy(artifact_hash="abc"):
    return {
        "schema_version": 1,
        "policy_id": "test-transition",
        "new_candidate_cutoff": 143,
        "sunset_version": 160,
        "grants": [
            {
                "bot": "national_v142",
                "artifact_hash": artifact_hash,
                "roles": ["parent_source", "rating_pool", "official_opponent"],
                "role_sunset_versions": {"official_opponent": 145},
                "reason": "test",
            }
        ],
    }


def _identity(path, *, artifact_hash="abc", published=True):
    return {
        "label": Path(path).name,
        "version": 142,
        "path": str(path),
        "artifact_hash": artifact_hash,
        "published": published,
        "issues": [] if published else ["not published"],
    }


def test_grandfather_grant_is_bound_to_artifact_role_and_sunset(monkeypatch, tmp_path):
    bot = tmp_path / "national_v142"
    monkeypatch.setattr(
        official_eligibility,
        "published_bot_identity",
        lambda path: _identity(path),
    )

    allowed = official_eligibility.grandfather_eligibility(
        bot,
        "official_opponent",
        target_version=143,
        policy=_policy(),
    )
    expired = official_eligibility.grandfather_eligibility(
        bot,
        "official_opponent",
        target_version=146,
        policy=_policy(),
    )

    assert allowed["eligible"] is True
    assert allowed["reason"] == "content_bound_grandfather_grant"
    assert expired["eligible"] is False
    assert expired["reason"] == "grandfather_grant_expired"


def test_grandfather_grant_fails_closed_on_hash_or_publication(monkeypatch, tmp_path):
    bot = tmp_path / "national_v142"
    monkeypatch.setattr(
        official_eligibility,
        "published_bot_identity",
        lambda path: _identity(path, artifact_hash="changed"),
    )
    mismatch = official_eligibility.grandfather_eligibility(
        bot,
        "rating_pool",
        target_version=143,
        policy=_policy(),
    )
    monkeypatch.setattr(
        official_eligibility,
        "published_bot_identity",
        lambda path: _identity(path, published=False),
    )
    unpublished = official_eligibility.grandfather_eligibility(
        bot,
        "rating_pool",
        target_version=143,
        policy=_policy(),
    )

    assert mismatch["reason"] == "grandfather_artifact_hash_mismatch"
    assert unpublished["reason"] == "not_published_artifact"


def test_official_opponent_grant_sunsets_by_certified_readiness_not_version(monkeypatch, tmp_path):
    bot = tmp_path / "national_v142"
    monkeypatch.setattr(official_eligibility, "published_bot_identity", lambda path: _identity(path))
    policy = _policy()
    policy.update({
        "schema_version": 2,
        "readiness_rules": {
            "official_opponent": {"minimum_certified_alternatives": 2},
        },
    })

    still_needed = official_eligibility.grandfather_eligibility(
        bot,
        "official_opponent",
        target_version=999,
        policy=policy,
        readiness={"certified_alternatives": 1},
    )
    migrated = official_eligibility.grandfather_eligibility(
        bot,
        "official_opponent",
        target_version=143,
        policy=policy,
        readiness={"certified_alternatives": 2},
    )

    assert still_needed["eligible"] is True
    assert still_needed["minimum_certified_alternatives"] == 2
    assert migrated["eligible"] is False
    assert migrated["reason"] == "grandfather_readiness_satisfied"


def test_new_candidate_cannot_receive_legacy_grant(monkeypatch, tmp_path):
    bot = tmp_path / "national_v143"
    monkeypatch.setattr(
        official_eligibility,
        "published_bot_identity",
        lambda path: {
            **_identity(path),
            "label": "national_v143",
            "version": 143,
        },
    )

    result = official_eligibility.grandfather_eligibility(
        bot,
        "parent_source",
        target_version=143,
        policy=_policy(),
    )

    assert result["eligible"] is False
    assert result["reason"] == "new_candidate_cannot_be_grandfathered"


def test_grandfather_grant_fails_closed_when_epoch_registry_is_unavailable(monkeypatch, tmp_path):
    bot = tmp_path / "national_v142"
    monkeypatch.setattr(official_eligibility, "published_bot_identity", lambda path: _identity(path))
    monkeypatch.setattr(
        official_eligibility,
        "epoch_lifecycle_eligibility",
        lambda _version: {"eligible": False, "reason": "national_epoch_registry_unavailable"},
    )

    result = official_eligibility.grandfather_eligibility(
        bot,
        "rating_pool",
        target_version=143,
        policy=_policy(),
    )

    assert result["eligible"] is False
    assert result["reason"] == "national_epoch_registry_unavailable"


def test_monotonic_epoch_target_prevents_expired_grant_revival(monkeypatch, tmp_path):
    bot = tmp_path / "national_v142"
    monkeypatch.setattr(official_eligibility, "published_bot_identity", lambda path: _identity(path))
    monkeypatch.setattr(
        official_eligibility,
        "current_target_version",
        lambda requested=None: max(int(requested or 1), 161),
    )

    result = official_eligibility.grandfather_eligibility(
        bot,
        "rating_pool",
        target_version=143,
        policy=_policy(),
    )

    assert result["eligible"] is False
    assert result["reason"] == "grandfather_grant_expired"
    assert result["target_version"] == 161


def test_schema3_grant_is_bound_to_tag_object_and_completion_tree(monkeypatch, tmp_path):
    bot = tmp_path / "national_v142"
    identity = {
        **_identity(bot, artifact_hash="a" * 64),
        "tag_object": "b" * 40,
        "completion_tree_oid": "c" * 40,
    }
    monkeypatch.setattr(official_eligibility, "published_bot_identity", lambda _path: identity)
    policy = {
        "schema_version": 3,
        "policy_id": "bound-transition",
        "new_candidate_cutoff": 143,
        "grants": [{
            "bot": "national_v142",
            "artifact_hash": "a" * 64,
            "tag_object": "b" * 40,
            "completion_tree_oid": "c" * 40,
            "roles": ["official_opponent"],
        }],
    }

    allowed = official_eligibility.grandfather_eligibility(
        bot, "official_opponent", policy=policy, target_version=143
    )
    policy["grants"][0]["tag_object"] = "d" * 40
    rejected = official_eligibility.grandfather_eligibility(
        bot, "official_opponent", policy=policy, target_version=143
    )

    assert allowed["eligible"] is True
    assert rejected["reason"] == "grandfather_tag_object_mismatch"


def test_policy_loader_rejects_duplicate_grants(tmp_path):
    policy = {
        "schema_version": 3,
        "policy_id": "duplicate-transition",
        "grants": [
            {
                "bot": "national_v142",
                "artifact_hash": "a" * 64,
                "tag_object": "b" * 40,
                "completion_tree_oid": "c" * 40,
                "roles": ["official_opponent"],
            },
            {
                "bot": "national_v142",
                "artifact_hash": "d" * 64,
                "tag_object": "e" * 40,
                "completion_tree_oid": "f" * 40,
                "roles": ["rating_pool"],
            },
        ],
    }
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate"):
        official_eligibility.load_grandfather_policy(path)
