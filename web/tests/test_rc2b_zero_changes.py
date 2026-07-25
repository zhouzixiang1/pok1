"""Tests for _classify_target_change zero-changes classification (rc2b)."""

from agent_workers import (
    _classify_target_change,
    _classify_target_change_for_worker,
    _compose_worker_task_prompt,
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
    bot = tmp_path / "national_v144"
    bot.mkdir()
    (bot / "policy.py").write_text("prepared candidate policy\n", encoding="utf-8")
    task = {"target_files": ["policy.py"]}
    snapshots = {(0, "policy.py"): "prepared candidate policy\n"}

    assert (
        _classify_target_change_for_worker(
            task, 0, "policy.py", bot, 144, source_v=None, baseline_snapshots=snapshots
        )
        == "unchanged"
    )

    (bot / "policy.py").write_text("worker changed the candidate policy\n", encoding="utf-8")
    assert (
        _classify_target_change_for_worker(
            task, 0, "policy.py", bot, 144, source_v=None, baseline_snapshots=snapshots
        )
        == "modified"
    )


def test_must_change_files_retains_strict_policy_contract():
    task = {
        "target_files": ["policy.py"],
        "must_change_files": ["policy.py"],
    }
    assert _must_change_rels_for_task(task, 268) == ["policy.py"]


def test_cot_inconsistency_blocks_repair_tasks_only():
    assert _cot_inconsistency_blocks_task({"task_kind": "quality_repair"})
    assert _cot_inconsistency_blocks_task({"task_kind": "precommit_repair"})
    assert not _cot_inconsistency_blocks_task({"task_kind": "feature_work", "worker_prompt": "add a new idea"})


def test_cot_inconsistency_blocks_undisclosed_runtime_side_effects_for_any_task():
    feature_task = {"task_kind": "feature_work", "worker_prompt": "add a new idea"}
    assert _cot_inconsistency_blocks_task(feature_task, {
        "discrepancies": [
            "Worker added _sys.stderr.write telemetry inside estimate_preflop_strength "
            "but did not disclose this runtime side-effect."
        ],
    })
    assert _cot_inconsistency_blocks_task(feature_task, {
        "focus_areas": ["Undisclosed debug logging path added to hot decision code."],
    })
    assert not _cot_inconsistency_blocks_task(feature_task, {
        "discrepancies": ["Summary omitted one low-level arithmetic rationale."],
    })


def test_file_scoped_quality_repair_omits_global_feedback():
    task = {
        "task_kind": "quality_repair",
        "repair_blocker": "quality_gate",
        "target_files": ["policy.py"],
        "must_change_files": ["policy.py"],
        "worker_prompt": "Repair only policy.py typed-action-intent contract.",
        "repair_contract": {
            "blocker": "quality_gate",
            "file": "policy.py",
        },
    }
    feedback = "Quality gates failed: typed_action_intent(national_bot.py:223); protected_contract"

    prompt = _compose_worker_task_prompt(task, feedback)

    assert "Repair only policy.py typed-action-intent contract." in prompt
    assert "Scope Isolation" in prompt
    assert "national_bot.py:223" not in prompt
    assert "protected_contract" not in prompt
    assert "CRITICAL REVISION NEEDED" not in prompt


def test_non_file_scoped_repair_keeps_reviewer_feedback():
    task = {
        "task_kind": "precommit_repair",
        "target_files": ["policy.py"],
        "worker_prompt": "Fix regression.",
    }
    feedback = "Native precommit failed vs national_v143"

    prompt = _compose_worker_task_prompt(task, feedback)

    assert "CRITICAL REVISION NEEDED" in prompt
    assert feedback in prompt
    assert "ORIGINAL:\nFix regression." in prompt


def test_file_scoped_precommit_repair_omits_duplicate_global_feedback():
    task = {
        "task_kind": "precommit_repair",
        "repair_blocker": "precommit_regression",
        "target_files": ["policy.py"],
        "must_change_files": ["policy.py"],
        "worker_prompt": "Exact precommit feedback: already embedded here.",
        "repair_contract": {
            "blocker": "precommit_regression",
            "file": "policy.py",
        },
    }
    feedback = "National-native precommit failed vs national_v145"

    prompt = _compose_worker_task_prompt(task, feedback)

    assert "Exact precommit feedback: already embedded here." in prompt
    assert "Scope Isolation" in prompt
    assert feedback not in prompt
    assert "CRITICAL REVISION NEEDED" not in prompt
