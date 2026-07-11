from types import SimpleNamespace

import pytest


def test_record_reaped_bot_publishes_tombstone_before_ledger(monkeypatch, tmp_path):
    import evolution_infra
    import national_epoch_registry

    order = []
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(evolution_infra, "REAPED_BOTS_FILE", tmp_path / "reaped_bots.jsonl")
    monkeypatch.setattr(evolution_infra, "evolution_git_push_enabled", lambda: True)
    monkeypatch.setattr(evolution_infra, "evolution_git_push_required", lambda: True)
    monkeypatch.setattr(
        national_epoch_registry,
        "create_reaped_tombstone",
        lambda *_args, **_kwargs: order.append("tombstone")
        or SimpleNamespace(created_tags=("national-reaped-v142",)),
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

    result = evolution_infra.record_reaped_bot("national_v142", reason="test")

    assert order == [
        "tombstone",
        ("push", ("national-reaped-v142",)),
        "ledger",
    ]
    assert result["registry"]["pushed"] is True


def test_record_reaped_bot_push_failure_never_writes_ledger(monkeypatch, tmp_path):
    import evolution_infra
    import national_epoch_registry

    ledger_writes = []
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(evolution_infra, "REAPED_BOTS_FILE", tmp_path / "reaped_bots.jsonl")
    monkeypatch.setattr(evolution_infra, "evolution_git_push_enabled", lambda: True)
    monkeypatch.setattr(evolution_infra, "evolution_git_push_required", lambda: True)
    monkeypatch.setattr(
        national_epoch_registry,
        "create_reaped_tombstone",
        lambda *_args, **_kwargs: SimpleNamespace(created_tags=("national-reaped-v142",)),
    )
    monkeypatch.setattr(evolution_infra, "git_push_refs", lambda *_refs: False)
    monkeypatch.setattr(
        evolution_infra,
        "append_locked_jsonl",
        lambda *_args, **_kwargs: ledger_writes.append(True),
    )

    with pytest.raises(RuntimeError, match="failed to publish durable reaped tombstone"):
        evolution_infra.record_reaped_bot("national_v142", reason="test")

    assert ledger_writes == []


def test_git_commit_registry_preflight_blocks_before_any_git(monkeypatch, tmp_path):
    import bot_artifact
    import evolution_infra

    candidate = tmp_path / "national_v143"
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
            143,
            142,
            "test",
            official_certificate={
                "certificate_digest": "cert-digest",
                "candidate_hash": "candidate-hash",
                "policy_id": "official-full-v5",
            },
        )

    assert git_calls == []


@pytest.mark.asyncio
async def test_reap_failure_keeps_completed_sentinel(monkeypatch, tmp_path):
    import tool_bot_management as bot_management

    bots_dir = tmp_path / "bots"
    results_dir = tmp_path / "web" / "core" / "results"
    replay_dir = results_dir / "match_replay"
    replay_dir.mkdir(parents=True)
    for version in (1, 2):
        bot_dir = bots_dir / f"national_v{version}"
        bot_dir.mkdir(parents=True)
        (bot_dir / "main.py").write_text("# bot\n", encoding="utf-8")
        (bot_dir / ".completed").touch()
    (results_dir / "bot_stats.json").write_text(
        '{"national_v1":{"games":1000},"national_v2":{"games":1000}}\n',
        encoding="utf-8",
    )

    monkeypatch.setattr(bot_management, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(bot_management, "RESULTS_DIR", results_dir)
    monkeypatch.setattr(bot_management, "REPLAY_DIR", replay_dir)
    monkeypatch.setattr(bot_management, "MAX_ACTIVE_BOTS", 1)
    monkeypatch.setattr(bot_management, "get_active_bots", lambda: ["national_v1", "national_v2"])
    monkeypatch.setattr(bot_management, "find_latest_active_v", lambda: 2)
    monkeypatch.setattr(
        bot_management,
        "load_ratings",
        lambda: {
            "national_v1": bot_management.Glicko2Player(r=1200, rd=50),
            "national_v2": bot_management.Glicko2Player(r=1600, rd=50),
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

    assert (bots_dir / "national_v1" / ".completed").exists()
