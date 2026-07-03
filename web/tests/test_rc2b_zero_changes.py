"""Tests for _classify_target_change zero-changes classification (rc2b)."""

from core.agent_workers import (
    _classify_target_change,
    _classify_target_change_for_worker,
    _cot_inconsistency_blocks_task,
    _must_change_rels_for_task,
)


def test_new_file():
    # Worker created a brand new file (success).
    assert _classify_target_change(False, True, "", "code") == "new_file"


def test_invalid_target():
    # Path resolves nowhere on disk (neither src nor dst exists) — failure.
    assert _classify_target_change(False, False, "", "") == "invalid_target"


def test_deleted():
    # File existed in source, now gone — failure.
    assert _classify_target_change(True, False, "x", "") == "deleted"


def test_unchanged():
    # Identical contents — failure (zero-change worker).
    assert _classify_target_change(True, True, "same", "same") == "unchanged"


def test_modified():
    # Both exist, contents differ — success.
    assert _classify_target_change(True, True, "a", "b") == "modified"


def test_new_file_requires_nonempty_dst():
    # Edge: (src missing, dst exists but empty) is NOT new_file — it is
    # invalid_target, since dst_text is falsy.
    assert _classify_target_change(False, True, "", "") == "invalid_target"


def test_worker_change_check_uses_pre_run_snapshot_for_crossover_candidate(tmp_path):
    bot = tmp_path / "claude_v11"
    bot.mkdir()
    (bot / "strategy.py").write_text("crossover already differs from source\n", encoding="utf-8")
    task = {"target_files": ["strategy.py"]}
    snapshots = {(0, "strategy.py"): "crossover already differs from source\n"}

    assert (
        _classify_target_change_for_worker(
            task, 0, "strategy.py", bot, 11, source_v=None, baseline_snapshots=snapshots
        )
        == "unchanged"
    )

    (bot / "strategy.py").write_text("repair changed the candidate\n", encoding="utf-8")
    assert (
        _classify_target_change_for_worker(
            task, 0, "strategy.py", bot, 11, source_v=None, baseline_snapshots=snapshots
        )
        == "modified"
    )


def test_must_change_files_narrows_target_files_contract():
    task = {
        "target_files": ["strategy.py", "opponent.py"],
        "must_change_files": ["opponent.py"],
    }
    assert _must_change_rels_for_task(task, 268) == ["opponent.py"]


def test_cot_inconsistency_blocks_repair_tasks_only():
    assert _cot_inconsistency_blocks_task({"task_kind": "quality_repair"})
    assert _cot_inconsistency_blocks_task({"task_kind": "precommit_repair"})
    assert not _cot_inconsistency_blocks_task({"task_kind": "feature_work", "worker_prompt": "add a new idea"})
