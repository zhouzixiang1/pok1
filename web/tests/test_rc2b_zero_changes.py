"""Tests for _classify_target_change zero-changes classification (rc2b)."""

from agent_workers import (
    _classify_target_change,
    _classify_target_change_for_worker,
    _compose_worker_task_prompt,
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


def test_cot_repair_discrepancies_require_confirmed_lead_to_block():
    # Root fix 2026-10-08: an LLM CoT verdict is a LEAD, never direct
    # enforcement. A repair task with plain claim-vs-diff discrepancies no
    # longer hard-blocks (the old repair-marker one-vote veto); only a
    # deterministically confirmed lead blocks.
    import worker_role_policy as wrp

    def _facts(added_lines=(), changed=True):
        return {
            "policy.py": {
                "before_present": True,
                "after_present": True,
                "changed": changed,
                "numbers_only": not changed,
                "added_lines": list(added_lines),
            }
        }

    for task_kind in ("quality_repair", "precommit_repair"):
        task = {"task_kind": task_kind, "role": "Algorithmic Logic Architect",
                "target_files": ["policy.py"]}
        verdict = wrp.review_worker_cot_lead(
            task,
            {
                "cot_consistent": False,
                "discrepancies": ["Claimed a rewrite but the diff is smaller."],
            },
            _facts(changed=True),
        )
        assert verdict.block is False
        assert verdict.advisory_notes

    feature_task = {"task_kind": "feature_work", "role": "Expert Coder 1",
                    "target_files": ["policy.py"]}
    assert wrp.review_worker_cot_lead(
        feature_task,
        {"cot_consistent": False,
         "discrepancies": ["Summary omitted one low-level arithmetic rationale."]},
        _facts(changed=True),
    ).block is False


def test_cot_undisclosed_runtime_side_effects_block_only_with_diff_evidence():
    # Side-effect leads now need deterministic diff confirmation: an added
    # line carrying a side-effect token blocks for ANY task kind; the same
    # claim without diff evidence is downgraded to advisory.
    import worker_role_policy as wrp

    def _facts(added_lines):
        return {
            "policy.py": {
                "before_present": True,
                "after_present": True,
                "changed": True,
                "numbers_only": False,
                "added_lines": list(added_lines),
            }
        }

    feature_task = {"task_kind": "feature_work", "worker_prompt": "add a new idea",
                    "role": "Expert Coder 1", "target_files": ["policy.py"]}
    side_effect_claim = {
        "discrepancies": [
            "Worker added _sys.stderr.write telemetry inside estimate_preflop_strength "
            "but did not disclose this runtime side-effect."
        ],
    }
    debug_claim = {
        "focus_areas": ["Undisclosed debug logging path added to hot decision code."],
    }
    guilty_diff = _facts(["    _sys.stderr.write('telemetry')"])
    clean_diff = _facts(["    strength = base * 1.05"])

    assert wrp.review_worker_cot_lead(feature_task, side_effect_claim, guilty_diff).block is True
    assert wrp.review_worker_cot_lead(
        feature_task, debug_claim, _facts(["    logging.info('debug')"])
    ).block is True
    downgraded = wrp.review_worker_cot_lead(feature_task, side_effect_claim, clean_diff)
    assert downgraded.block is False
    assert downgraded.downgrade_reasons


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
