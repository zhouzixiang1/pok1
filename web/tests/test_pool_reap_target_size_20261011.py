"""Pool-ratchet reap target-size regressions (2026-10-11).

Anchors: the reap trio plans against the mode-resolved target capacity
(N=12 -> one victim; legacy -> MAX_ACTIVE_BOTS=30 -> zero victims,
byte-equivalent plan shape), the ratchet protection profile's guaranteed
fall-through victim versus the legacy all_protected None, live-env target
changes failing closed during validation, and the tombstone reason deriving
from the frozen protection profile through both the direct and the
handoff-plan execution paths.
"""

import json

import pytest

from bot_namespace import bot_name


def _record():
    # Production handoff records always carry identity.version (the bot this
    # handoff published); the reap-plan validator's priority invariant binds
    # the snapshot's priority_bot against it (2026-10-12 hardening).
    return {
        "identity": {
            "publication_id": "a" * 64,
            "commit_oid": "b" * 40,
            "remote_publication": {"remote_main_oid": "c" * 40},
            "version": 13,
        }
    }


def _setup_pool(
    monkeypatch,
    tmp_path,
    versions,
    *,
    games_by_version=None,
    rating_by_version=None,
    priority_version=None,
):
    import tool_bot_management as tbm
    import tool_commit

    root = tmp_path
    bots_dir = root / "bots"
    names = [bot_name(v) for v in versions]
    for name in names:
        (bots_dir / name).mkdir(parents=True)
        (bots_dir / name / ".completed").touch()

    def active_from_disk():
        return [
            name
            for name in names
            if (bots_dir / name / ".completed").exists()
        ]

    results_dir = root / "web" / "core" / "results"
    results_dir.mkdir(parents=True)
    games_by_version = games_by_version or {}
    (results_dir / "bot_stats.json").write_text(
        json.dumps(
            {bot_name(v): {"games": games_by_version.get(v, 700)} for v in versions}
        ),
        encoding="utf-8",
    )
    priority_name = (
        bot_name(priority_version) if priority_version is not None else None
    )
    (results_dir / "priority_eval.json").write_text(
        json.dumps({"bot": priority_name} if priority_name else {}),
        encoding="utf-8",
    )
    monkeypatch.setattr(tbm, "PROJECT_ROOT", root)
    monkeypatch.setattr(tbm, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(tbm, "get_active_bots", active_from_disk)
    # The pool_reap trio reads tool_commit.get_active_bots (its own import).
    monkeypatch.setattr(tool_commit, "get_active_bots", active_from_disk)
    monkeypatch.setattr(
        tbm,
        "load_ratings",
        lambda: {
            bot_name(v): tbm.Glicko2Player(
                r=(rating_by_version or {}).get(v, (1200.0, 100.0))[0],
                rd=(rating_by_version or {}).get(v, (1200.0, 100.0))[1],
            )
            for v in versions
        },
    )
    monkeypatch.setattr(tbm, "load_h2h_avg_winrates", lambda: {})
    monkeypatch.setattr(tbm, "load_strength_scores", lambda: {})
    return names, bots_dir, results_dir


def test_ratchet_target_12_plans_exactly_one_victim(monkeypatch, tmp_path):
    import tool_commit

    monkeypatch.setenv("POK_PUBLISH_MODE", "pool_ratchet")
    monkeypatch.setenv("POK_POOL_RATCHET_TARGET_SIZE", "12")
    versions = list(range(1, 14))  # 13 bots, cap 12 -> 1 reap
    ratings = {v: (1200.0 + (v - 1) * 50.0, 100.0) for v in versions}
    _setup_pool(
        monkeypatch,
        tmp_path,
        versions,
        rating_by_version=ratings,
        priority_version=13,
    )

    plan = tool_commit._build_pool_reap_plan(_record())

    assert plan["max_active_bots"] == 12
    assert plan["selection_policy"] == "conservative_glicko_pool_ratchet_v1"
    assert plan["required_reaps"] == 1
    # Current bot (v13) and the priority bot (also v13) are the ONLY
    # protected members; the weakest conservative score is v1.
    assert [row["candidate"] for row in plan["targets"]] == [bot_name(1)]
    snapshot = plan["selection_snapshot"]
    assert snapshot["selection_policy"] == "conservative_glicko_pool_ratchet_v1"
    assert snapshot["protection_profile"] == "pool_ratchet"
    assert snapshot["max_active_bots"] == 12

    initial, targets, validated_snapshot = tool_commit._validate_pool_reap_plan(
        plan, _record()
    )
    assert targets == [bot_name(1)]
    assert validated_snapshot is snapshot


def test_legacy_mode_plan_is_byte_equivalent_to_historical_shape(
    monkeypatch, tmp_path
):
    import tool_bot_management as tbm
    import tool_commit

    monkeypatch.delenv("POK_PUBLISH_MODE", raising=False)
    monkeypatch.delenv("POK_POOL_RATCHET_TARGET_SIZE", raising=False)
    # The priority invariant requires the snapshot's priority_bot to equal
    # the handoff's published bot (v13 here) even in legacy mode.
    _setup_pool(monkeypatch, tmp_path, list(range(1, 14)), priority_version=13)

    plan = tool_commit._build_pool_reap_plan(_record())

    # Legacy anchor: same policy label, same cap, zero reaps, and the
    # snapshot keeps the exact historical key set (no protection_profile).
    assert plan["selection_policy"] == tbm.REAP_SELECTION_POLICY
    assert plan["selection_policy"] == "conservative_glicko_v1"
    assert plan["max_active_bots"] == tool_commit.MAX_ACTIVE_BOTS == 30
    assert plan["required_reaps"] == 0
    assert plan["targets"] == []
    assert set(plan["selection_snapshot"]) == tbm._REAP_SNAPSHOT_KEYS
    assert "protection_profile" not in plan["selection_snapshot"]
    tool_commit._validate_pool_reap_plan(plan, _record())


def test_ratchet_zero_game_fall_through_always_yields_a_victim(
    monkeypatch, tmp_path
):
    import tool_commit

    monkeypatch.setenv("POK_PUBLISH_MODE", "pool_ratchet")
    monkeypatch.setenv("POK_POOL_RATCHET_TARGET_SIZE", "12")
    versions = list(range(1, 14))
    ratings = {v: (1200.0 + (v - 1) * 50.0, 100.0) for v in versions}
    # Every non-protected member is untested (zero games): soft overflow
    # still prefers evaluated candidates, but the empty evaluated set must
    # fall through to the zero-game candidates instead of "all_protected".
    _setup_pool(
        monkeypatch,
        tmp_path,
        versions,
        games_by_version={v: 0 for v in versions},
        rating_by_version=ratings,
        priority_version=13,
    )

    plan = tool_commit._build_pool_reap_plan(_record())
    assert plan["required_reaps"] == 1
    assert [row["candidate"] for row in plan["targets"]] == [bot_name(1)]
    tool_commit._validate_pool_reap_plan(plan, _record())


def test_legacy_soft_overflow_keeps_all_protected_none(monkeypatch, tmp_path):
    """Legacy contrast: <600-game soft protection may leave no candidate."""
    import pool_ratchet
    import tool_commit

    monkeypatch.delenv("POK_PUBLISH_MODE", raising=False)
    monkeypatch.setattr(pool_ratchet, "pool_target_size", lambda: 12)
    versions = list(range(1, 16))  # 15 bots, cap 12, len <= cap+3 -> soft
    _setup_pool(
        monkeypatch,
        tmp_path,
        versions,
        games_by_version={v: 590 for v in versions},
        priority_version=15,
    )

    with pytest.raises(
        RuntimeError, match="pool_reap_cannot_plan_required_target"
    ):
        tool_commit._build_pool_reap_plan(_record())


def test_validate_fails_closed_when_live_target_size_changes(
    monkeypatch, tmp_path
):
    import tool_commit

    monkeypatch.setenv("POK_PUBLISH_MODE", "pool_ratchet")
    monkeypatch.setenv("POK_POOL_RATCHET_TARGET_SIZE", "12")
    _setup_pool(
        monkeypatch,
        tmp_path,
        list(range(1, 14)),
        priority_version=13,
    )
    plan = tool_commit._build_pool_reap_plan(_record())

    # Operator raises N while the handoff is in flight: the frozen plan's cap
    # no longer equals the live mode-resolved target -> fail closed.
    monkeypatch.setenv("POK_POOL_RATCHET_TARGET_SIZE", "13")
    with pytest.raises(RuntimeError, match="pool_reap_plan_contract_invalid"):
        tool_commit._validate_pool_reap_plan(plan, _record())


@pytest.mark.asyncio
async def test_reap_reason_derives_from_frozen_protection_profile(
    monkeypatch, tmp_path
):
    import tool_bot_management as tbm

    captured = []
    monkeypatch.setattr(
        tbm,
        "record_reaped_bot",
        lambda name, *, reason, data: captured.append((name, reason)),
    )

    # Ratchet snapshot -> pool_ratchet_replace_weakest.
    monkeypatch.setenv("POK_PUBLISH_MODE", "pool_ratchet")
    monkeypatch.setenv("POK_POOL_RATCHET_TARGET_SIZE", "12")
    _setup_pool(
        monkeypatch,
        tmp_path,
        list(range(1, 14)),
        priority_version=13,
    )
    ratchet_snapshot = tbm._capture_reap_selection_snapshot()
    result = await tbm._do_reap_weakest(
        quiet=True,
        expected_culled=bot_name(1),
        selection_snapshot=ratchet_snapshot,
    )
    assert result["reaped"] is True
    assert captured == [(bot_name(1), "pool_ratchet_replace_weakest")]

    # Legacy snapshot -> historical max_active_bots reason.
    monkeypatch.delenv("POK_PUBLISH_MODE", raising=False)
    captured.clear()
    for version in range(1, 14):
        bot_dir = tmp_path / "bots" / bot_name(version)
        bot_dir.mkdir(parents=True, exist_ok=True)
        (bot_dir / ".completed").touch()
    legacy_snapshot = tbm._capture_reap_selection_snapshot(
        [bot_name(v) for v in range(1, 14)],
        max_active_bots=12,
        selection_policy="conservative_glicko_v1",
        protection_profile="legacy",
    )
    result = await tbm._do_reap_weakest(
        quiet=True,
        expected_culled=bot_name(1),
        selection_snapshot=legacy_snapshot,
    )
    assert result["reaped"] is True
    assert captured == [(bot_name(1), "max_active_bots")]


@pytest.mark.asyncio
async def test_execute_pool_reap_plan_carries_derived_reason(
    monkeypatch, tmp_path
):
    import tool_bot_management as tbm
    import tool_commit

    monkeypatch.setenv("POK_PUBLISH_MODE", "pool_ratchet")
    monkeypatch.setenv("POK_POOL_RATCHET_TARGET_SIZE", "12")
    names, _bots_dir, _results = _setup_pool(
        monkeypatch,
        tmp_path,
        list(range(1, 14)),
        priority_version=13,
    )

    captured = []
    monkeypatch.setattr(
        tbm,
        "record_reaped_bot",
        lambda name, *, reason, data: captured.append((name, reason)) or {
            "bot": name,
        },
    )
    monkeypatch.setattr(
        tool_commit,
        "_converge_and_verify_reaped_target",
        lambda target, _record: {"bot": target, "tombstone": "proven"},
    )

    plan = tool_commit._build_pool_reap_plan(_record())
    output = await tool_commit._execute_pool_reap_plan(plan, _record())

    # The handoff path passes only expected_culled/selection_snapshot; the
    # derived reason still lands on the durable tombstone.
    assert output["removed_bots"] == [bot_name(1)]
    assert captured == [(bot_name(1), "pool_ratchet_replace_weakest")]
    assert not (tmp_path / "bots" / bot_name(1) / ".completed").exists()
