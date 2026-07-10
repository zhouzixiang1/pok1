from pathlib import Path

import official_eligibility


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
