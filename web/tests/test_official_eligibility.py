from types import SimpleNamespace
import hashlib

import official_eligibility


def test_active_role_policy_is_strict_and_grant_free():
    policy = official_eligibility.load_official_role_policy()

    assert policy["epoch"] == "national_tcp_policy_v1"
    assert policy["transitional_grants"] == "forbidden"
    assert set(policy["roles"]) == {
        "parent_source",
        "rating_pool",
        "official_opponent",
    }
    assert all(
        "signed_official-full-v5" in contract["required"]
        for contract in policy["roles"].values()
    )
    assert policy["historical_signed_ledger_root"] == {
        "status": "retired",
        "active_role_authority": False,
        "executable": False,
    }
    assert policy["first_strict_control"] == {
        "control_id": "first_strict_control_v1",
        "authority": "system_first_strict_control",
        "formal_bootstrap_scope": "first_policy_bot_empty_pool_only",
        "normal_official_opponent": False,
        "one_time": True,
        "strength_weight": 0,
        "rating_weight": 0,
    }


def test_retired_authorization_is_preserved_only_in_archive():
    root = official_eligibility.ROOT
    assert not (root / "web/core/official_grandfathering.json").exists()
    archived = (
        root
        / "archive/evolution_epochs/national_native_v1/authorization"
        / "official_grandfathering.json"
    )
    assert archived.is_file()
    assert hashlib.sha256(archived.read_bytes()).hexdigest() == (
        "473849bda17fe3856466907a9e28171dbd8c98afce99b422f5fdd77fc40df75e"
    )


def test_pre_policy_version_is_rejected_without_registry_or_archive_read(monkeypatch):
    monkeypatch.setattr(
        official_eligibility,
        "_registry_state",
        lambda: (_ for _ in ()).throw(AssertionError("registry must not be read")),
    )

    result = official_eligibility.epoch_lifecycle_eligibility(142)

    assert result["eligible"] is False
    assert result["reason"] == "pre_policy_epoch_archived"
    assert result["first_strict_version"] == 143


def test_strict_epoch_lifecycle_uses_durable_reap_registry(monkeypatch):
    monkeypatch.setattr(
        official_eligibility,
        "_registry_state",
        lambda: SimpleNamespace(
            available=True,
            reaped_versions=frozenset({144}),
            source="durable_tags",
            diagnostics=(),
        ),
    )

    active = official_eligibility.epoch_lifecycle_eligibility(143)
    reaped = official_eligibility.epoch_lifecycle_eligibility(144)

    assert active["eligible"] is True
    assert active["reason"] == "national_tcp_policy_epoch_active"
    assert reaped["eligible"] is False
    assert reaped["reason"] == "national_bot_reaped"


def test_target_version_has_v143_floor(monkeypatch):
    monkeypatch.setattr(official_eligibility, "_registry_state", lambda: object())
    monkeypatch.setattr(
        official_eligibility,
        "effective_target_version",
        lambda requested, **_kwargs: requested,
    )

    assert official_eligibility.current_target_version(1) == 143


def test_strict_role_eligibility_delegates_to_single_resolver(monkeypatch, tmp_path):
    seen = {}

    class FakeSpec:
        eligible = True

        def as_dict(self):
            return {"eligible": True, "issues": []}

    def resolve(candidate, role, *, repo_root):
        seen.update(candidate=candidate, role=role, repo_root=repo_root)
        return FakeSpec()

    monkeypatch.setattr(official_eligibility, "resolve_national_bot_spec", resolve)
    bot = tmp_path / "national_v143"

    result = official_eligibility.strict_role_eligibility(bot, "parent_source")

    assert result["eligible"] is True
    assert result["reason"] == "strict_policy_bot_signed_full_certified"
    assert seen["candidate"] == bot
    assert seen["role"] == "parent_source"
