"""Strict policy-epoch precommit repair routing tests."""

from pathlib import Path

import pytest

from bot_namespace import STRICT_ARTIFACT_FILES, bot_name, strict_artifact_layout_errors
from conftest import STRICT_TARGET_V
import tool_planning


def _write_strict_bot(root: Path) -> Path:
    root.mkdir(parents=True)
    payloads = {
        "national_bot.py": "# system runtime\n",
        "precompute.py": "FACT = 1\n",
        "policy.py": "def get_baseline_decision(context): return {'kind': 'pass'}\n",
        "national_runtime_manifest.json": "{}\n",
        "policy_epoch_receipt.json": "{}\n",
    }
    assert frozenset(payloads) == STRICT_ARTIFACT_FILES
    for relative, payload in payloads.items():
        (root / relative).write_text(payload, encoding="utf-8")
    assert strict_artifact_layout_errors(root) == []
    return root


def _seed_bot_dirs(tmp_path, monkeypatch, changed_files):
    bots = tmp_path / "bots"
    _write_strict_bot(bots / bot_name(STRICT_TARGET_V))
    _write_strict_bot(bots / bot_name(STRICT_TARGET_V + 1))
    monkeypatch.setattr(
        tool_planning,
        "get_bot_dir",
        lambda version: bots / bot_name(version),
    )
    monkeypatch.setattr(
        tool_planning,
        "_py_files_changed_between",
        lambda _source, _candidate: list(changed_files),
    )


def _checkpoint(directive="National TCP precommit FAILED: 0W-1L vs parent"):
    return {
        "stage": "precommit_failed",
        "source_v": STRICT_TARGET_V,
        "next_v": STRICT_TARGET_V + 1,
        "gate_results": {"precommit_eval": {"directive": directive}},
    }


def test_strategy_regression_routes_only_to_policy(tmp_path, monkeypatch):
    _seed_bot_dirs(
        tmp_path,
        monkeypatch,
        ["national_bot.py", "precompute.py", "policy.py"],
    )
    assert tool_planning._precommit_repair_target_files(_checkpoint(), "") == [
        "policy.py"
    ]


def test_system_only_diff_falls_back_to_policy(tmp_path, monkeypatch):
    _seed_bot_dirs(
        tmp_path,
        monkeypatch,
        ["national_bot.py", "precompute.py"],
    )
    assert tool_planning._precommit_repair_target_files(_checkpoint(), "") == [
        "policy.py"
    ]


def test_protocol_failure_never_grants_system_runtime_write_authority(
    tmp_path, monkeypatch
):
    _seed_bot_dirs(tmp_path, monkeypatch, ["policy.py"])
    checkpoint = _checkpoint(
        "official smoke reported illegal wire action serialization in national_bot.py"
    )
    assert tool_planning._precommit_repair_target_files(checkpoint, "") == []
    with pytest.raises(ValueError, match="system/extra artifact"):
        tool_planning._precommit_repair_task(
            "national_bot.py",
            checkpoint,
            checkpoint["gate_results"]["precommit_eval"]["directive"],
        )


def test_policy_repair_task_preserves_system_owned_runtime():
    checkpoint = _checkpoint()
    feedback = checkpoint["gate_results"]["precommit_eval"]["directive"]
    task = tool_planning._precommit_repair_task("policy.py", checkpoint, feedback)

    assert task["role"] == "Strategic Regression Repair Architect"
    assert task["target_files"] == ["policy.py"]
    assert task["repair_contract"]["file"] == "policy.py"
    assert "`policy.py` is the sole writable file" in task["worker_prompt"]
    assert "national_bot.py and precompute.py remain byte-identical" in task[
        "worker_prompt"
    ]
    assert "decision_context.hand.position" in task["worker_prompt"]


def test_compatibility_flag_cannot_expand_policy_write_scope():
    assert tool_planning._precommit_filter_repair_targets(
        ["national_bot.py", "precompute.py", "policy.py", "helper.py"],
        allow_protocol_files=True,
    ) == ["policy.py"]


# ─────────────────────────────────────────────────────────────────────────
# P0-A (2026-10-20): repair-worker symbol-domain lock.  Production
# evidence: v599's repair worker modified three ``_bluff_allowed``
# parameters unrelated to its plan symbol (4 review rejections); v600's
# deleted that function's river-air branch (7 rejections + circuit
# breaker).  Every repair surface — the orchestrator-facing precommit
# directive, the precommit repair worker prompt, the quality-contract
# repair prompt, and the review-rework fallback prompt — must carry the
# hard lock interpolated from the checkpoint's frozen proposal binding.
# ─────────────────────────────────────────────────────────────────────────


def _plan_binding_checkpoint():
    checkpoint = _checkpoint()
    checkpoint["master_plan"] = {
        "proposal_binding": {
            "change_symbol": "policy.py:_refinement_prior_equity",
            "reachable_chain": [
                "policy.py:iter_decisions",
                "policy.py:_refinement_prior_equity",
            ],
        }
    }
    return checkpoint


def test_planned_change_symbol_domain_extracts_binding():
    checkpoint = _plan_binding_checkpoint()
    assert tool_planning._planned_change_symbol_domain(checkpoint) == (
        "policy.py:_refinement_prior_equity",
        "policy.py:iter_decisions, policy.py:_refinement_prior_equity",
    )
    # No plan / no binding degrades to empty, never to a fabricated symbol.
    assert tool_planning._planned_change_symbol_domain(_checkpoint()) == ("", "")


def test_precommit_repair_task_carries_symbol_domain_lock():
    checkpoint = _plan_binding_checkpoint()
    feedback = checkpoint["gate_results"]["precommit_eval"]["directive"]
    task = tool_planning._precommit_repair_task("policy.py", checkpoint, feedback)
    prompt = task["worker_prompt"]
    assert "Symbol-domain lock" in prompt
    assert "policy.py:_refinement_prior_equity" in prompt
    assert "iter_decisions" in prompt
    assert "Any edit to other symbols/functions will be rejected by review" in prompt
    assert "successive partial edits" in prompt


def test_precommit_repair_task_symbol_lock_survives_missing_plan():
    feedback = _checkpoint()["gate_results"]["precommit_eval"]["directive"]
    task = tool_planning._precommit_repair_task(
        "policy.py", _checkpoint(), feedback
    )
    # The lock still names the rule when the plan binding is unavailable.
    assert "Symbol-domain lock" in task["worker_prompt"]
    assert "plan binding unavailable" in task["worker_prompt"]


def test_quality_contract_repair_task_carries_symbol_domain_lock():
    checkpoint = _plan_binding_checkpoint()
    checkpoint["stage"] = "quality_failed"
    contract = {
        "blocker": "quality_gate",
        "file": "policy.py",
        "evidence": "reviewer rejected an unrelated three-parameter edit",
    }
    preservation = "This is a gate repair for bots/national_cloud_v{next_v}."
    task = tool_planning._quality_contract_task(
        contract, checkpoint, preservation, "quality_repair"
    )
    prompt = task["worker_prompt"]
    assert "Symbol-domain lock" in prompt
    assert "policy.py:_refinement_prior_equity" in prompt
    assert "Any edit to other symbols/functions will be rejected by review" in prompt


def test_review_rework_fallback_prompt_carries_symbol_domain_lock():
    checkpoint = _plan_binding_checkpoint()
    checkpoint["stage"] = "repair_planned"
    checkpoint["reviewer_feedback"] = (
        "Rejected: the candidate edited _bluff_allowed outside its planned "
        "change_symbol; restore the planned scope."
    )
    checkpoint["gate_results"]["review"] = {
        "passed": False,
        "approved": False,
        "feedback": checkpoint["reviewer_feedback"],
    }
    tasks = tool_planning._synthesize_rework_tasks_from_checkpoint(
        checkpoint, checkpoint["reviewer_feedback"]
    )
    assert tasks, "review rework must synthesize a repair task"
    prompts = [str(task.get("worker_prompt") or "") for task in tasks]
    assert any(
        "Symbol-domain lock" in prompt
        and "policy.py:_refinement_prior_equity" in prompt
        for prompt in prompts
    )


def test_tool_eval_precommit_directive_interpolates_planned_symbols(monkeypatch):
    import tool_eval

    checkpoint = _plan_binding_checkpoint()
    monkeypatch.setattr(
        tool_eval,
        "_matching_checkpoint",
        lambda version, source_v=None: (
            checkpoint
            if version == checkpoint["next_v"]
            and source_v == checkpoint["source_v"]
            else None
        ),
    )
    clause = tool_eval._precommit_repair_symbol_domain_clause(
        checkpoint["next_v"], checkpoint["source_v"]
    )
    assert "Symbol-domain lock" in clause
    assert "policy.py:_refinement_prior_equity" in clause
    assert "Any edit to other symbols/functions will be rejected by review" in clause

    # A missing matching checkpoint still renders the lock (fallback
    # wording), never an empty directive clause.
    fallback = tool_eval._precommit_repair_symbol_domain_clause(999, 998)
    assert "Symbol-domain lock" in fallback
    assert "plan binding unavailable" in fallback
