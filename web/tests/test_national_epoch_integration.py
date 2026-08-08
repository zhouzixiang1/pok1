import json
from types import SimpleNamespace

import pytest

from bot_namespace import bot_name
from conftest import STRICT_SOURCE_V, STRICT_TARGET_V


def test_record_reaped_bot_publishes_tombstone_before_ledger(monkeypatch, tmp_path):
    import evolution_infra
    import national_epoch_registry

    reaped_v = STRICT_TARGET_V + 5
    reaped_tag = f"national-reaped-v{reaped_v}"
    order = []
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(evolution_infra, "REAPED_BOTS_FILE", tmp_path / "reaped_bots.jsonl")
    monkeypatch.setattr(evolution_infra, "evolution_git_push_enabled", lambda: True)
    monkeypatch.setattr(evolution_infra, "evolution_git_push_required", lambda: True)
    monkeypatch.setattr(
        national_epoch_registry,
        "create_reaped_tombstone",
        lambda *_args, **_kwargs: order.append("tombstone")
        or SimpleNamespace(created_tags=(reaped_tag,)),
    )
    monkeypatch.setattr(
        evolution_infra,
        "git_push_refs",
        lambda *refs: order.append(("push", refs)) or True,
    )
    monkeypatch.setattr(
        evolution_infra,
        "append_locked_jsonl",
        lambda *_args, **_kwargs: order.append("ledger"),
    )

    result = evolution_infra.record_reaped_bot(bot_name(reaped_v), reason="test")

    assert order == [
        "tombstone",
        ("push", (reaped_tag,)),
        "ledger",
    ]
    assert result["registry"]["pushed"] is True


def test_record_reaped_bot_push_failure_never_writes_ledger(monkeypatch, tmp_path):
    import evolution_infra
    import national_epoch_registry

    reaped_v = STRICT_TARGET_V + 5
    reaped_tag = f"national-reaped-v{reaped_v}"
    ledger_writes = []
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(evolution_infra, "REAPED_BOTS_FILE", tmp_path / "reaped_bots.jsonl")
    monkeypatch.setattr(evolution_infra, "evolution_git_push_enabled", lambda: True)
    monkeypatch.setattr(evolution_infra, "evolution_git_push_required", lambda: True)
    monkeypatch.setattr(
        national_epoch_registry,
        "create_reaped_tombstone",
        lambda *_args, **_kwargs: SimpleNamespace(created_tags=(reaped_tag,)),
    )
    monkeypatch.setattr(evolution_infra, "git_push_refs", lambda *_refs: False)
    monkeypatch.setattr(
        evolution_infra,
        "append_locked_jsonl",
        lambda *_args, **_kwargs: ledger_writes.append(True),
    )

    with pytest.raises(RuntimeError, match="failed to publish durable reaped tombstone"):
        evolution_infra.record_reaped_bot(bot_name(reaped_v), reason="test")

    assert ledger_writes == []


def test_git_commit_registry_preflight_blocks_before_any_git(monkeypatch, tmp_path):
    import bot_artifact
    import evolution_infra

    candidate = tmp_path / bot_name(STRICT_TARGET_V)
    candidate.mkdir()
    (candidate / "national_bot.py").write_text("import socket\n", encoding="utf-8")
    git_calls = []
    monkeypatch.setattr(evolution_infra, "get_bot_dir", lambda _version: candidate)
    monkeypatch.setattr(bot_artifact, "hash_path", lambda _path: "candidate-hash")
    monkeypatch.setattr(
        evolution_infra,
        "_require_national_epoch_registry_for_commit",
        lambda: (_ for _ in ()).throw(RuntimeError("registry migration marker missing")),
    )
    monkeypatch.setattr(evolution_infra, "_git", lambda *args, **_kwargs: git_calls.append(args) or "")

    with pytest.raises(RuntimeError, match="registry migration marker missing"):
        evolution_infra.git_commit_bot(
            STRICT_TARGET_V,
            STRICT_SOURCE_V,
            "test",
            official_certificate={
                "certificate_digest": "cert-digest",
                "candidate_hash": "candidate-hash",
                "policy_id": "official_full_policy_removed",
            },
        )

    assert git_calls == []


@pytest.mark.asyncio
async def test_reap_failure_keeps_completed_sentinel(monkeypatch, tmp_path):
    import tool_bot_management as bot_management

    bots_dir = tmp_path / "bots"
    results_dir = tmp_path / "web" / "core" / "results"
    results_dir.mkdir(parents=True)
    versions = (STRICT_TARGET_V, STRICT_TARGET_V + 1)
    for version in versions:
        bot_dir = bots_dir / bot_name(version)
        bot_dir.mkdir(parents=True)
        (bot_dir / "policy.py").write_text("# policy\n", encoding="utf-8")
        (bot_dir / ".completed").touch()
    (results_dir / "bot_stats.json").write_text(
        json.dumps({bot_name(v): {"games": 1000} for v in versions}) + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(bot_management, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(bot_management, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(bot_management, "MAX_ACTIVE_BOTS", 1)
    monkeypatch.setattr(
        bot_management,
        "get_active_bots",
        lambda: [bot_name(v) for v in versions],
    )
    monkeypatch.setattr(
        bot_management,
        "load_ratings",
        lambda: {
            bot_name(versions[0]): bot_management.Glicko2Player(r=1200, rd=50),
            bot_name(versions[1]): bot_management.Glicko2Player(r=1600, rd=50),
        },
    )
    monkeypatch.setattr(bot_management, "load_h2h_avg_winrates", lambda: {})
    monkeypatch.setattr(bot_management, "load_strength_scores", lambda: {})
    monkeypatch.setattr(
        bot_management,
        "record_reaped_bot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("publish failed")),
    )

    with pytest.raises(RuntimeError, match="publish failed"):
        await bot_management._do_reap_weakest(quiet=True)

    assert (bots_dir / bot_name(versions[0]) / ".completed").exists()
