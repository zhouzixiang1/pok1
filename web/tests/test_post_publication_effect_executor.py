from __future__ import annotations

from pathlib import Path

import pytest


def _bind_runtime_dirs(monkeypatch, tmp_path):
    import tool_commit

    results = tmp_path / "results"
    archive = results / "archive"
    results.mkdir()
    archive.mkdir()
    monkeypatch.setattr(tool_commit, "RESULTS_DIR", results)
    monkeypatch.setattr(tool_commit, "ARCHIVE_DIR", archive)
    return tool_commit, results, archive


def _frozen_reap_snapshot(names, *, max_active_bots):
    from bot_artifact import canonical_digest

    active = sorted(names)
    rows = []
    for offset, name in enumerate(active):
        rating = 1200.0 + offset * 100.0
        rows.append({
            "bot": name,
            "rating_r_hex": rating.hex(),
            "rating_rd_hex": (100.0).hex(),
            "games": 700,
            "leaderboard_score_hex": (offset / 10.0).hex(),
            "h2h_avg_wr_hex": (offset / 10.0).hex(),
        })
    snapshot = {
        "schema_version": 1,
        "kind": "strict-active-pool-selection-snapshot",
        "selection_policy": "conservative_glicko_v1",
        "max_active_bots": max_active_bots,
        "active_bots": active,
        "active_pool_digest": canonical_digest(active),
        "priority_bot": None,
        "bot_inputs": rows,
        "bot_inputs_digest": canonical_digest(rows),
    }
    snapshot["snapshot_digest"] = canonical_digest(snapshot)
    return snapshot


def test_strict_log_plan_never_opens_legacy_and_preserves_siblings(
    tmp_path, monkeypatch
):
    tool_commit, results, _archive = _bind_runtime_dirs(monkeypatch, tmp_path)
    poison = results / "v1" / "logs"
    poison.mkdir(parents=True)
    (poison / "legacy-poison.txt").write_text("must-not-open", encoding="utf-8")
    logs = results / "v143" / "logs"
    logs.mkdir(parents=True)
    (logs / "worker.txt").write_text("strict log\n", encoding="utf-8")
    sibling_result = results / "v143" / "result.json"
    sibling_replay = results / "v143" / "replay.json"
    sibling_result.write_text("result", encoding="utf-8")
    sibling_replay.write_text("replay", encoding="utf-8")

    real_manifest = tool_commit._safe_log_tree_manifest
    opened_versions = []

    def audited_manifest(path, *, version):
        opened_versions.append(version)
        assert version >= 143
        assert "v1/" not in str(path)
        return real_manifest(path, version=version)

    monkeypatch.setattr(tool_commit, "_safe_log_tree_manifest", audited_manifest)
    plan = tool_commit._build_strict_log_cleanup_plan(149)
    assert plan["keep_generations"] == 5
    assert plan["cutoff_version"] == 144
    receipts = tool_commit._execute_strict_log_cleanup(
        plan, expected_handoff_version=149
    )

    assert opened_versions and set(opened_versions) == {143}
    assert receipts[0]["effect_mode"] == "nondestructive-immutable-archive"
    assert receipts[0]["live_log_tree_preserved"] is True
    assert receipts[0]["quarantine_log_tree_touched"] is False
    assert receipts[0]["generation_siblings_preserved"] is True
    assert (logs / "worker.txt").read_text(encoding="utf-8") == "strict log\n"
    assert sibling_result.read_text(encoding="utf-8") == "result"
    assert sibling_replay.read_text(encoding="utf-8") == "replay"
    assert (poison / "legacy-poison.txt").read_text(encoding="utf-8") == "must-not-open"


def test_v143_log_plan_performs_zero_legacy_path_probes(tmp_path, monkeypatch):
    tool_commit, _results, _archive = _bind_runtime_dirs(monkeypatch, tmp_path)

    def forbidden(_path):
        raise AssertionError("v143 handoff must not probe any pre-epoch log path")

    monkeypatch.setattr(tool_commit.os.path, "lexists", forbidden)
    plan = tool_commit._build_strict_log_cleanup_plan(143)
    assert plan["cutoff_version"] == 138
    assert plan["archives"] == []


@pytest.mark.parametrize("target_version", [148, 149])
def test_forged_log_plan_cannot_remove_recent_or_current_generation(
    target_version, tmp_path, monkeypatch
):
    tool_commit, results, archive = _bind_runtime_dirs(monkeypatch, tmp_path)
    logs = results / f"v{target_version}" / "logs"
    logs.mkdir(parents=True)
    payload = logs / "protected.txt"
    payload.write_text("must remain live", encoding="utf-8")
    tree = tool_commit._safe_log_tree_manifest(
        logs, version=target_version
    )
    suffix = tree["tree_digest"][:20]
    forged_item = {
        **tree,
        "archive_relative_path": (
            f"v{target_version}_logs_{suffix}.tar.gz"
        ),
        "manifest_relative_path": (
            f"v{target_version}_logs_{suffix}.manifest.json"
        ),
        "quarantine_relative_path": (
            f"v{target_version}/.logs-archived-{tree['tree_digest']}"
        ),
    }
    plan = tool_commit._build_strict_log_cleanup_plan(149)
    plan["archives"].append(forged_item)

    with pytest.raises(RuntimeError, match="subject_invalid"):
        tool_commit._execute_strict_log_cleanup(
            plan, expected_handoff_version=149
        )

    assert payload.read_text(encoding="utf-8") == "must remain live"
    assert list(archive.iterdir()) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("handoff_version", 150),
        ("keep_generations", 4),
        ("cutoff_version", 145),
    ],
)
def test_log_cleanup_executor_rejects_forged_retention_identity(
    field, value, tmp_path, monkeypatch
):
    tool_commit, results, archive = _bind_runtime_dirs(monkeypatch, tmp_path)
    logs = results / "v143" / "logs"
    logs.mkdir(parents=True)
    payload = logs / "protected.txt"
    payload.write_text("must remain live", encoding="utf-8")
    plan = tool_commit._build_strict_log_cleanup_plan(149)
    plan[field] = value

    with pytest.raises(RuntimeError, match="plan_invalid"):
        tool_commit._execute_strict_log_cleanup(
            plan, expected_handoff_version=149
        )

    assert payload.read_text(encoding="utf-8") == "must remain live"
    assert list(archive.iterdir()) == []


def test_existing_archive_never_authorizes_deleting_new_logs(tmp_path, monkeypatch):
    tool_commit, results, _archive = _bind_runtime_dirs(monkeypatch, tmp_path)
    logs = results / "v143" / "logs"
    logs.mkdir(parents=True)
    (logs / "old.txt").write_text("old", encoding="utf-8")
    plan = tool_commit._build_strict_log_cleanup_plan(149)
    tool_commit._execute_strict_log_cleanup(
        plan, expected_handoff_version=149
    )
    (logs / "new.txt").write_text("new and not archived", encoding="utf-8")

    with pytest.raises(RuntimeError, match="preimage_changed"):
        tool_commit._execute_strict_log_cleanup(
            plan, expected_handoff_version=149
        )

    assert (logs / "new.txt").read_text(encoding="utf-8") == "new and not archived"


def test_log_plan_cannot_omit_a_live_cutoff_subject(tmp_path, monkeypatch):
    tool_commit, results, archive = _bind_runtime_dirs(monkeypatch, tmp_path)
    logs = results / "v143" / "logs"
    logs.mkdir(parents=True)
    payload = logs / "protected.txt"
    payload.write_text("must be archived", encoding="utf-8")
    plan = tool_commit._build_strict_log_cleanup_plan(149)
    plan["archives"] = []

    with pytest.raises(RuntimeError, match="plan_incomplete"):
        tool_commit._execute_strict_log_cleanup(
            plan, expected_handoff_version=149
        )

    assert payload.read_text(encoding="utf-8") == "must be archived"
    assert list(archive.iterdir()) == []


def test_log_archive_crash_before_manifest_resumes_without_loss(tmp_path, monkeypatch):
    import evolution_infra

    tool_commit, results, archive = _bind_runtime_dirs(monkeypatch, tmp_path)
    logs = results / "v143" / "logs"
    logs.mkdir(parents=True)
    (logs / "worker.txt").write_text("payload", encoding="utf-8")
    plan = tool_commit._build_strict_log_cleanup_plan(149)
    real_write = evolution_infra._atomic_publish_state_text

    def crash(path, text):
        if path.name.endswith(".manifest.json"):
            raise RuntimeError("injected-manifest-crash")
        return real_write(path, text)

    monkeypatch.setattr(evolution_infra, "_atomic_publish_state_text", crash)
    with pytest.raises(RuntimeError, match="injected-manifest-crash"):
        tool_commit._execute_strict_log_cleanup(
            plan, expected_handoff_version=149
        )
    assert logs.exists()
    assert list(archive.glob("v143_logs_*.tar.gz"))

    monkeypatch.setattr(evolution_infra, "_atomic_publish_state_text", real_write)
    receipt = tool_commit._execute_strict_log_cleanup(
        plan, expected_handoff_version=149
    )
    assert receipt[0]["live_log_tree_preserved"] is True
    assert (logs / "worker.txt").read_text(encoding="utf-8") == "payload"


def test_log_archive_never_touches_existing_quarantine_or_live_source(
    tmp_path, monkeypatch
):
    tool_commit, results, archive = _bind_runtime_dirs(monkeypatch, tmp_path)
    logs = results / "v143" / "logs"
    logs.mkdir(parents=True)
    (logs / "worker.txt").write_text("payload", encoding="utf-8")
    plan = tool_commit._build_strict_log_cleanup_plan(149)
    quarantine = results / plan["archives"][0]["quarantine_relative_path"]
    quarantine.mkdir()
    (quarantine / "historical.txt").write_text(
        "must remain", encoding="utf-8"
    )
    real_unlink = Path.unlink

    def forbid_rename(*_args, **_kwargs):
        raise AssertionError("strict log archival must never rename live bytes")

    def forbid_rmdir(*_args, **_kwargs):
        raise AssertionError("strict log archival must never rmdir live bytes")

    def audited_unlink(path, *args, **kwargs):
        # Create-only archive publication may remove its own temporary link.
        if path.parent != archive:
            raise AssertionError(
                f"strict log archival attempted non-archive unlink: {path}"
            )
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(tool_commit.os, "rename", forbid_rename)
    monkeypatch.setattr(tool_commit.os, "rmdir", forbid_rmdir)
    monkeypatch.setattr(Path, "unlink", audited_unlink)
    receipt = tool_commit._execute_strict_log_cleanup(
        plan, expected_handoff_version=149
    )
    assert receipt[0]["quarantine_log_tree_touched"] is False
    assert (logs / "worker.txt").read_text(encoding="utf-8") == "payload"
    assert (quarantine / "historical.txt").read_text(
        encoding="utf-8"
    ) == "must remain"


def test_log_archive_recovers_link_success_before_temp_unlink(tmp_path, monkeypatch):
    tool_commit, results, archive = _bind_runtime_dirs(monkeypatch, tmp_path)
    logs = results / "v143" / "logs"
    logs.mkdir(parents=True)
    (logs / "worker.txt").write_text("payload", encoding="utf-8")
    plan = tool_commit._build_strict_log_cleanup_plan(149)
    real_unlink = Path.unlink
    injected = False

    def crash_unlink(path, *args, **kwargs):
        nonlocal injected
        if (
            not injected
            and path.parent == archive
            and path.name.startswith(".v143_logs_")
            and path.name.endswith(".tmp")
        ):
            injected = True
            raise RuntimeError("injected-link-unlink-crash")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", crash_unlink)
    with pytest.raises(RuntimeError, match="injected-link-unlink-crash"):
        tool_commit._execute_strict_log_cleanup(
            plan, expected_handoff_version=149
        )
    target = next(archive.glob("v143_logs_*.tar.gz"))
    assert target.stat().st_nlink == 2
    monkeypatch.setattr(Path, "unlink", real_unlink)

    receipt = tool_commit._execute_strict_log_cleanup(
        plan, expected_handoff_version=149
    )
    assert target.stat().st_nlink == 1
    assert receipt[0]["live_log_tree_preserved"] is True
    assert (logs / "worker.txt").read_text(encoding="utf-8") == "payload"


def test_log_archive_ignores_partial_quarantine_and_is_idempotent(
    tmp_path, monkeypatch
):
    tool_commit, results, _archive = _bind_runtime_dirs(monkeypatch, tmp_path)
    logs = results / "v143" / "logs"
    logs.mkdir(parents=True)
    (logs / "one.txt").write_text("one", encoding="utf-8")
    (logs / "two.txt").write_text("two", encoding="utf-8")
    plan = tool_commit._build_strict_log_cleanup_plan(149)
    quarantine = results / plan["archives"][0]["quarantine_relative_path"]
    quarantine.mkdir()
    (quarantine / "partial.txt").write_text("partial", encoding="utf-8")

    first = tool_commit._execute_strict_log_cleanup(
        plan, expected_handoff_version=149
    )
    second = tool_commit._execute_strict_log_cleanup(
        plan, expected_handoff_version=149
    )
    assert first == second
    assert (logs / "one.txt").read_text(encoding="utf-8") == "one"
    assert (logs / "two.txt").read_text(encoding="utf-8") == "two"
    assert (quarantine / "partial.txt").read_text(encoding="utf-8") == "partial"


def test_log_archive_parent_symlink_swap_cannot_mutate_redirected_tree(
    tmp_path, monkeypatch
):
    """A post-validation parent swap has no destructive operation to redirect."""

    tool_commit, results, _archive = _bind_runtime_dirs(monkeypatch, tmp_path)
    logs = results / "v143" / "logs"
    logs.mkdir(parents=True)
    (logs / "worker.txt").write_text("frozen", encoding="utf-8")
    outside_version = tmp_path / "outside-v143"
    outside_logs = outside_version / "logs"
    outside_logs.mkdir(parents=True)
    outside_payload = outside_logs / "outside.txt"
    outside_payload.write_text("must never move", encoding="utf-8")
    plan = tool_commit._build_strict_log_cleanup_plan(149)
    real_manifest = tool_commit._safe_log_tree_manifest
    calls = 0
    preserved_parent = results / "v143-preserved"

    def swap_after_validation(path, *, version):
        nonlocal calls
        manifest = real_manifest(path, version=version)
        calls += 1
        # The plan was captured before this audit hook.  Complete-plan reproof
        # is call 1, execute preimage is call 2, and the old implementation
        # renamed immediately after the final live validation at call 3.
        if calls == 3:
            path.parent.rename(preserved_parent)
            path.parent.symlink_to(outside_version, target_is_directory=True)
        return manifest

    monkeypatch.setattr(
        tool_commit, "_safe_log_tree_manifest", swap_after_validation
    )
    receipts = tool_commit._execute_strict_log_cleanup(
        plan, expected_handoff_version=149
    )

    assert receipts[0]["quarantine_log_tree_touched"] is False
    assert outside_payload.read_text(encoding="utf-8") == "must never move"
    assert (preserved_parent / "logs" / "worker.txt").read_text(
        encoding="utf-8"
    ) == "frozen"
    with pytest.raises(RuntimeError, match="log_tree_parent_unsafe"):
        tool_commit._revalidate_strict_log_archives(
            plan, receipts, expected_handoff_version=149
        )


def test_log_tree_rejects_symlink_and_hardlink(tmp_path, monkeypatch):
    tool_commit, results, _archive = _bind_runtime_dirs(monkeypatch, tmp_path)
    logs = results / "v143" / "logs"
    logs.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_text("outside", encoding="utf-8")
    (logs / "link").symlink_to(outside)
    with pytest.raises(RuntimeError, match="member_unsafe"):
        tool_commit._build_strict_log_cleanup_plan(149)
    (logs / "link").unlink()
    original = logs / "one"
    original.write_text("same inode", encoding="utf-8")
    (logs / "two").hardlink_to(original)
    with pytest.raises(RuntimeError, match="member_unsafe"):
        tool_commit._build_strict_log_cleanup_plan(149)


def test_final_log_reproof_rejects_archive_tamper(tmp_path, monkeypatch):
    tool_commit, results, archive = _bind_runtime_dirs(monkeypatch, tmp_path)
    logs = results / "v143" / "logs"
    logs.mkdir(parents=True)
    (logs / "worker.txt").write_text("payload", encoding="utf-8")
    plan = tool_commit._build_strict_log_cleanup_plan(149)
    receipts = tool_commit._execute_strict_log_cleanup(
        plan, expected_handoff_version=149
    )
    tool_commit._revalidate_strict_log_archives(
        plan, receipts, expected_handoff_version=149
    )
    target = next(archive.glob("v143_logs_*.tar.gz"))
    with target.open("ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(RuntimeError, match="final_manifest_mismatch"):
        tool_commit._revalidate_strict_log_archives(
            plan, receipts, expected_handoff_version=149
        )


def test_final_log_reproof_rejects_receipt_or_live_source_drift(
    tmp_path, monkeypatch
):
    tool_commit, results, _archive = _bind_runtime_dirs(monkeypatch, tmp_path)
    logs = results / "v143" / "logs"
    logs.mkdir(parents=True)
    payload = logs / "worker.txt"
    payload.write_text("payload", encoding="utf-8")
    plan = tool_commit._build_strict_log_cleanup_plan(149)
    receipts = tool_commit._execute_strict_log_cleanup(
        plan, expected_handoff_version=149
    )
    forged = [{**receipts[0], "live_log_tree_preserved": False}]
    with pytest.raises(RuntimeError, match="final_receipt_mismatch"):
        tool_commit._revalidate_strict_log_archives(
            plan, forged, expected_handoff_version=149
        )

    payload.write_text("changed after archive", encoding="utf-8")
    with pytest.raises(RuntimeError, match="preimage_changed"):
        tool_commit._revalidate_strict_log_archives(
            plan, receipts, expected_handoff_version=149
        )


def test_rotation_final_reproof_is_read_only_and_accepts_live_append(
    tmp_path, monkeypatch
):
    import evolution_infra

    results = tmp_path / "results"
    archive = results / "archive"
    results.mkdir()
    archive.mkdir()
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results)
    monkeypatch.setattr(evolution_infra, "ARCHIVE_DIR", archive)
    for attribute, name in (
        ("WORKER_FAILURES_FILE", "worker_failures.jsonl"),
        ("MATCH_HISTORY_FILE", "match_history.jsonl"),
        ("RATING_HISTORY_FILE", "rating_history.jsonl"),
        ("LLM_COSTS_FILE", "llm_costs.jsonl"),
    ):
        monkeypatch.setattr(evolution_infra, attribute, results / name)
    source = results / "events.jsonl"
    source.write_text(
        "".join(f'{{"event":{index}}}\n' for index in range(1002)),
        encoding="utf-8",
    )
    rotation_plan = evolution_infra.build_archive_rotation_plan(149, "a" * 64)
    receipts = evolution_infra.archive_rotate_files(149, rotation_plan)
    assert [row["source"] for row in receipts] == ["events.jsonl"]
    assert evolution_infra.validate_archive_rotation_receipts(
        149, receipts, rotation_plan=rotation_plan
    ) == receipts
    before = sorted(path.name for path in archive.iterdir())

    with source.open("a", encoding="utf-8") as handle:
        handle.write('{"later":true}\n' * 2000)
    assert evolution_infra.validate_archive_rotation_receipts(
        149, receipts, rotation_plan=rotation_plan
    ) == receipts
    assert sorted(path.name for path in archive.iterdir()) == before

    plan_path = archive / "events_v149.rotation.json"
    plan = __import__("json").loads(plan_path.read_text(encoding="utf-8"))
    plan["archive_sha256"] = "0" * 64
    from bot_artifact import canonical_digest

    unsigned = {key: value for key, value in plan.items() if key != "digest"}
    plan["digest"] = canonical_digest(unsigned)
    plan_path.write_text(__import__("json").dumps(plan), encoding="utf-8")
    with pytest.raises(RuntimeError, match="preimage|archive|receipt"):
        evolution_infra.validate_archive_rotation_receipts(
            149, receipts, rotation_plan=rotation_plan
        )


def test_pool_reap_plan_freezes_every_target(monkeypatch):
    import post_publication_handoff
    import tool_bot_management
    import tool_commit
    from bot_artifact import canonical_digest

    monkeypatch.setattr(tool_commit, "MAX_ACTIVE_BOTS", 2)
    monkeypatch.setattr(
        tool_commit,
        "get_active_bots",
        lambda: ["national_v143", "national_v144", "national_v145", "national_v146"],
    )

    snapshot = _frozen_reap_snapshot(
        ["national_v143", "national_v144", "national_v145", "national_v146"],
        max_active_bots=2,
    )
    monkeypatch.setattr(
        tool_bot_management,
        "_capture_reap_selection_snapshot",
        lambda *_args, **_kwargs: snapshot,
    )
    record = {
        "identity": {
            "publication_id": "a" * 64,
            "commit_oid": "b" * 40,
            "remote_publication": {"remote_main_oid": "c" * 40},
        }
    }
    plan = tool_commit._build_pool_reap_plan(record)
    assert [row["candidate"] for row in plan["targets"]] == [
        "national_v143",
        "national_v144",
    ]
    assert plan["required_reaps"] == 2
    assert plan["schema_version"] == 2
    assert plan["selection_snapshot_digest"] == snapshot["snapshot_digest"]
    assert plan["target_sequence_digest"]

    forged_skip = {
        "plan_digest": canonical_digest(plan),
        "removed_bots": [],
        "required_reaps": 0,
        "reap_proofs": [],
        "reap_proof_set_digest": canonical_digest([]),
    }
    errors = post_publication_handoff._step_output_contract_errors(
        "pool_reap",
        forged_skip,
        plan,
        canonical_digest(plan),
        record["identity"],
    )
    assert any("pool_reap:identity" in error for error in errors)


@pytest.mark.asyncio
async def test_multi_reap_crash_converges_without_reselecting_or_repeating(
    monkeypatch,
):
    import tool_bot_management
    import tool_commit
    active = [
        "national_v143", "national_v144", "national_v145", "national_v146",
    ]
    calls = []
    monkeypatch.setattr(tool_commit, "MAX_ACTIVE_BOTS", 2)
    monkeypatch.setattr(tool_commit, "get_active_bots", lambda: list(active))
    frozen = _frozen_reap_snapshot(active, max_active_bots=2)
    monkeypatch.setattr(
        tool_bot_management,
        "_capture_reap_selection_snapshot",
        lambda *_args, **_kwargs: frozen,
    )

    async def reap(*, quiet, expected_culled, selection_snapshot):
        assert quiet is True
        assert selection_snapshot is frozen
        calls.append(expected_culled)
        active.remove(expected_culled)
        return {"reaped": True, "culled": expected_culled}

    monkeypatch.setattr(tool_bot_management, "_do_reap_weakest", reap)
    proof_calls = []
    injected = False

    def prove(name, _record):
        nonlocal injected
        proof_calls.append(name)
        if name == "national_v144" and not injected:
            injected = True
            raise RuntimeError("injected-proof-crash")
        return {"bot": name, "tombstone": "proven"}

    monkeypatch.setattr(tool_commit, "_converge_and_verify_reaped_target", prove)
    record = {
        "identity": {
            "publication_id": "a" * 64,
            "commit_oid": "b" * 40,
            "remote_publication": {"remote_main_oid": "c" * 40},
        }
    }
    plan = tool_commit._build_pool_reap_plan(record)
    with pytest.raises(RuntimeError, match="injected-proof-crash"):
        await tool_commit._execute_pool_reap_plan(plan, record)
    assert active == ["national_v145", "national_v146"]

    output = await tool_commit._execute_pool_reap_plan(plan, record)
    assert calls == ["national_v143", "national_v144"]
    assert output["removed_bots"] == ["national_v143", "national_v144"]
    assert proof_calls == [
        "national_v143", "national_v144", "national_v143", "national_v144",
    ]


@pytest.mark.asyncio
async def test_forged_pool_reap_target_is_rejected_before_any_effect(monkeypatch):
    import tool_bot_management
    import tool_commit
    from bot_artifact import canonical_digest

    active = [
        "national_v143", "national_v144", "national_v145", "national_v146",
    ]
    monkeypatch.setattr(tool_commit, "MAX_ACTIVE_BOTS", 2)
    monkeypatch.setattr(tool_commit, "get_active_bots", lambda: list(active))
    frozen = _frozen_reap_snapshot(active, max_active_bots=2)
    monkeypatch.setattr(
        tool_bot_management,
        "_capture_reap_selection_snapshot",
        lambda *_args, **_kwargs: frozen,
    )
    record = {
        "identity": {
            "publication_id": "a" * 64,
            "commit_oid": "b" * 40,
            "remote_publication": {"remote_main_oid": "c" * 40},
        }
    }
    plan = tool_commit._build_pool_reap_plan(record)
    plan["targets"][0]["candidate"] = "national_v146"
    # Recompute the public digest too: target validity comes from deterministic
    # policy replay, not from trusting a self-asserted checksum.
    plan["target_sequence_digest"] = canonical_digest(plan["targets"])
    effect_calls = []

    async def forbidden_reap(**_kwargs):
        effect_calls.append("reap")
        raise AssertionError("forged target reached reap effect")

    monkeypatch.setattr(tool_bot_management, "_do_reap_weakest", forbidden_reap)
    monkeypatch.setattr(
        tool_commit,
        "_converge_and_verify_reaped_target",
        lambda *_args: effect_calls.append("prove"),
    )
    with pytest.raises(RuntimeError, match="target_sequence_invalid"):
        await tool_commit._execute_pool_reap_plan(plan, record)
    assert effect_calls == []
    assert active == [
        "national_v143", "national_v144", "national_v145", "national_v146",
    ]


def test_housekeeping_is_read_only_verification(monkeypatch):
    import tool_commit

    calls = []

    def git(*args, **_kwargs):
        calls.append(args)
        if args[:2] == ("rev-parse", "HEAD"):
            return "a" * 40
        if args[:2] == ("status", "--porcelain"):
            return ""
        return ""

    monkeypatch.setattr(tool_commit, "_git", git)
    monkeypatch.setattr(tool_commit, "_git_ensure_main_branch", lambda: None)
    result = tool_commit._verify_post_publication_worktree(
        expected_head="a" * 40,
        expected_dirty=set(),
    )
    assert result["tracked_housekeeping_commit"] is False
    assert not any(args and args[0] in {"add", "commit", "push"} for args in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "crash_step",
    [
        "stability_observation",
        "reap_signal",
        "priority_eval",
        "archive_rotation",
        "log_cleanup",
        "pool_reap",
        "cycle_annotation",
        "housekeeping",
    ],
)
async def test_executor_crash_after_effect_resumes_from_persisted_plan(
    crash_step, tmp_path, monkeypatch
):
    """Every required effect has a durable plan before its first mutation."""

    import cycle_archivist
    import evolution_infra
    import post_publication_handoff as handoff
    import stability_observation
    import tool_commit
    from bot_artifact import canonical_digest

    _tool, results, archive = _bind_runtime_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results)
    monkeypatch.setattr(evolution_infra, "ARCHIVE_DIR", archive)
    for attribute, name in (
        ("WORKER_FAILURES_FILE", "worker_failures.jsonl"),
        ("MATCH_HISTORY_FILE", "match_history.jsonl"),
        ("RATING_HISTORY_FILE", "rating_history.jsonl"),
        ("LLM_COSTS_FILE", "llm_costs.jsonl"),
    ):
        monkeypatch.setattr(evolution_infra, attribute, results / name)
    monkeypatch.setattr(tool_commit, "MAX_ACTIVE_BOTS", 8)
    monkeypatch.setattr(tool_commit, "get_active_bots", lambda: [])
    monkeypatch.setattr(tool_commit, "_git_ensure_main_branch", lambda: None)
    monkeypatch.setattr(
        tool_commit,
        "_git",
        lambda *args, **_kwargs: (
            "e" * 40
            if args[:2] == ("rev-parse", "HEAD")
            else ""
        ),
    )

    steps = {
        name: {"status": "pending"} for name in handoff.REQUIRED_STEPS
    }
    record = {
        "state": "pending",
        "identity_digest": "d" * 64,
        "identity": {
            "version": 143,
            "source_v": 142,
            "publication_id": "a" * 64,
            "commit_oid": "e" * 40,
            "publishing_checkpoint_digest": "f" * 64,
            "remote_publication": {
                "required": False,
                "explicit_test_mode": True,
                "remote_main_oid": None,
                "paired_refs": {},
            },
        },
        "steps": steps,
    }
    snapshot = {
        "evaluation_epoch": "national_tcp_policy_v1",
        "version": 143,
        "source_v": 142,
        "bot_name": "national_v143",
        "git_tag": "national-bot-v143",
        "publication_identity": {
            "version": 143,
            "source_v": 142,
            "publication_id": "a" * 64,
            "commit_oid": "e" * 40,
            "candidate_artifact_hash": "c" * 64,
        },
        "publishing_checkpoint_projection": {"stage": "publishing"},
        "strength_evidence_identity": {"mode": "test"},
        "post_publication_handoff": {
            "identity_digest": "d" * 64,
            "publication_id": "a" * 64,
            "state": "running",
        },
        "review_score": 9,
        "critic_score": 8,
        "precommit_passed": True,
    }
    events = []
    crashed = False
    claim_number = 0

    def claim(_version, _source):
        nonlocal claim_number
        claim_number += 1
        record["state"] = "running"
        return record, f"claim-{claim_number}"

    def plan(_version, _source, _claim, step, payload):
        assert record["steps"][step]["status"] == "pending"
        events.append(f"plan:{step}")
        record["steps"][step] = {
            "status": "planned",
            "plan": payload,
            "plan_digest": canonical_digest(payload),
        }
        return record

    def complete_step(_version, _source, _claim, step, output):
        nonlocal crashed
        events.append(f"complete:{step}")
        if step == crash_step and not crashed:
            crashed = True
            raise RuntimeError(f"injected-crash:{step}")
        previous = record["steps"][step]
        completed = {
            "status": "completed",
            "receipt": {
                "output": output,
                "receipt_digest": canonical_digest({"step": step, "output": output}),
            },
        }
        if previous.get("status") == "planned":
            completed["plan"] = previous["plan"]
            completed["plan_digest"] = previous["plan_digest"]
        record["steps"][step] = completed
        return record

    def release(_version, _source, _claim, *, error=None):
        record["state"] = "pending"
        record["last_error"] = error

    def complete_all(_version, _source, _claim):
        assert all(
            row["status"] == "completed" for row in record["steps"].values()
        )
        record["state"] = "completed"
        return record

    monkeypatch.setattr(handoff, "claim_post_publication_handoff", claim)
    monkeypatch.setattr(handoff, "plan_handoff_step", plan)
    monkeypatch.setattr(handoff, "complete_handoff_step", complete_step)
    monkeypatch.setattr(handoff, "release_post_publication_handoff_claim", release)
    monkeypatch.setattr(handoff, "complete_post_publication_handoff", complete_all)
    monkeypatch.setattr(handoff, "load_archive_snapshot", lambda _version: snapshot)

    def write_annotation(_version, _source, _claim, annotation):
        events.append("effect:cycle_annotation")
        snapshot["archivist_notes"] = annotation
        return {
            "annotation_digest": annotation["annotation_digest"],
            "archive_semantic_digest": "1" * 64,
        }

    monkeypatch.setattr(handoff, "write_archive_annotation", write_annotation)
    monkeypatch.setattr(handoff, "local_handoff_identity_errors", lambda _record: [])
    monkeypatch.setattr(
        stability_observation,
        "record_published_generation",
        lambda **_kwargs: events.append("effect:stability_observation") or {
            "continuity_id": "continuity",
            "count": 1,
            "target": 10,
            "complete": False,
        },
    )
    monkeypatch.setattr(
        tool_commit,
        "archive_rotate_files",
        lambda _version, _plan: events.append("effect:archive_rotation") or [],
    )
    monkeypatch.setattr(
        tool_commit,
        "_execute_strict_log_cleanup",
        lambda _plan, **_kwargs: events.append("effect:log_cleanup") or [],
    )
    monkeypatch.setattr(
        tool_commit,
        "_durable_archivist_state_write",
        lambda path, _payload: events.append(
            "effect:priority_eval" if path.name == "priority_eval.json" else "effect:state"
        ) or "2" * 64,
    )
    monkeypatch.setattr(
        tool_commit,
        "_verify_post_publication_worktree",
        lambda **_kwargs: events.append("effect:housekeeping") or {
            "head_oid": "e" * 40,
            "worktree_status_digest": "3" * 64,
            "tracked_housekeeping_commit": False,
        },
    )
    monkeypatch.setattr(
        cycle_archivist,
        "_offline_cycle_input_errors",
        lambda *_args, **_kwargs: [],
    )

    async def annotate(*_args, **_kwargs):
        payload = {
            "schema_version": 1,
            "kind": "national-tcp-policy-cycle-annotation",
            "subject": {
                "epoch": "national_tcp_policy_v1",
                "version": 143,
                "source_v": 142,
                "bot": "national_v143",
                "tag": "national-bot-v143",
                "artifact_hash": "c" * 64,
                "strength_evidence_identity": {"mode": "test"},
            },
            "status": "annotated",
            "issues": [],
            "analysis": {
                "generation_assessment": "neutral",
                "archive_notes": "Test annotation.",
            },
        }
        return {**payload, "annotation_digest": canonical_digest(payload)}

    monkeypatch.setattr(cycle_archivist, "run_cycle_archivist_analysis", annotate)

    real_publish = evolution_infra._atomic_publish_state_text

    def publish(path, text):
        events.append("effect:reap_signal")
        return real_publish(path, text)

    monkeypatch.setattr(evolution_infra, "_atomic_publish_state_text", publish)

    first = await tool_commit._run_durable_post_publication_archivist(143, 142)
    assert first["error"] == "POST_PUBLICATION_ARCHIVIST_PENDING"
    assert record["steps"][crash_step]["status"] == "planned"
    second = await tool_commit._run_durable_post_publication_archivist(143, 142)
    assert second["archivist_completed"] is True
    assert events.index(f"plan:{crash_step}") < events.index(
        f"effect:{crash_step}"
    ) if crash_step not in {"pool_reap"} else True
