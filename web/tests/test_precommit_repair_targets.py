"""Strict policy-epoch precommit repair routing tests."""

import hashlib
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
    assert "minimal mechanical adaptation" in prompt
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
    assert "minimal mechanical adaptation" in prompt


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
    assert "minimal mechanical adaptation" in clause

    # A missing matching checkpoint still renders the lock (fallback
    # wording), never an empty directive clause.
    fallback = tool_eval._precommit_repair_symbol_domain_clause(999, 998)
    assert "Symbol-domain lock" in fallback
    assert "plan binding unavailable" in fallback


def test_authorized_parent_restore_section_for_crossover(tmp_path):
    """2026-10-26: crossover review-repair must make the reviewer-demanded
    parent restores executable (v611/v612/v614 non-convergent loops)."""
    from tool_planning_quality_contracts import (
        _authorized_parent_restore_section,
    )

    parent = tmp_path / "policy.py"
    parent.write_text(
        "def _strategy_adjustments(ctx):\n"
        "    return ctx['x'] + 1\n"
        "\n"
        "def _unrelated(ctx):\n"
        "    return 2\n",
        encoding="utf-8",
    )
    ckpt = {"parent2_v": 598, "source_v": 608, "next_v": 616}
    feedback = (
        "Rejected: `_strategy_adjustments` carries inherited drift; restore "
        "its national_cloud_v608 form. Also policy.py:_unrelated was removed."
    )
    section = _authorized_parent_restore_section(
        ckpt, feedback, parent_policy_path=parent
    )
    assert "Authorized parent-restore set" in section
    assert "policy.py:_strategy_adjustments" in section
    assert "def _strategy_adjustments(ctx):" in section
    assert "def _unrelated(ctx):" in section
    assert "sha256" in section
    assert "BYTE-IDENTICALLY" in section

    single = {"parent2_v": None, "source_v": 608}
    assert _authorized_parent_restore_section(single, feedback, parent_policy_path=parent) == ""

    quiet = _authorized_parent_restore_section(
        ckpt, "Rejected: evidence depth insufficient.", parent_policy_path=parent
    )
    assert quiet == ""


_RESTORE_PARENT_SOURCE = (
    "def iter_decisions(ctx):\n"
    "    return _strategy_adjustments(ctx)\n"
    "\n"
    "def _strategy_adjustments(ctx):\n"
    "    return ctx['x'] + 1\n"
    "\n"
    "def _unrelated(ctx):\n"
    "    return 2\n"
    "\n"
    "def _note_only(ctx):\n"
    "    return 3\n"
)


def _restore_ckpt(change_symbol="policy.py:_strategy_adjustments"):
    """Crossover review-repair checkpoint bound to a frozen plan."""

    return {
        "parent2_v": 27,
        "source_v": 608,
        "next_v": 621,
        "master_plan": {
            "selected_proposal_id": "proposal-1",
            "proposal_ensemble": {
                "proposal_source_symbol_digests": {"proposal-1": {}},
            },
            "proposal_binding": {
                "change_symbol": change_symbol,
                "reachable_chain": [
                    "policy.py:iter_decisions",
                    change_symbol,
                ],
            },
        },
    }


def test_authorized_parent_restore_section_excludes_planned_change_symbol(tmp_path):
    """2026-10-26 mutex: a restore authorization for the plan's own
    change_symbol is unsatisfiable (the changed-symbol gate requires it to
    differ from the baseline). v620 injected exactly that."""
    from tool_planning_quality_contracts import (
        _authorized_parent_restore_section,
        _restore_conflicts_with_planned_change_symbol,
    )

    parent = tmp_path / "policy.py"
    parent.write_text(_RESTORE_PARENT_SOURCE, encoding="utf-8")
    ckpt = _restore_ckpt()
    feedback = (
        "Rejected: the planned contract permits changes only to "
        "`policy.py:_strategy_adjustments`, but the candidate also wires "
        "`_unrelated` from `policy.py:iter_decisions`. Restore the parent "
        "forms."
    )

    # The mutex is detected deterministically from the frozen binding.
    assert _restore_conflicts_with_planned_change_symbol(
        ckpt, {"_unrelated", "_strategy_adjustments"}
    ) == "_strategy_adjustments"
    # Non-conflicting mention sets and missing bindings never fabricate one.
    assert (
        _restore_conflicts_with_planned_change_symbol(ckpt, {"_unrelated"}) == ""
    )
    assert (
        _restore_conflicts_with_planned_change_symbol(
            {"parent2_v": 27, "source_v": 608}, {"_strategy_adjustments"}
        )
        == ""
    )
    assert (
        _restore_conflicts_with_planned_change_symbol(
            _restore_ckpt("_strategy_adjustments"), {"_strategy_adjustments"}
        )
        == "_strategy_adjustments"
    )

    section = _authorized_parent_restore_section(
        ckpt, feedback, parent_policy_path=parent
    )
    # The planned change_symbol is never injected as a parent form ...
    assert "### `policy.py:_strategy_adjustments`" not in section
    assert "def _strategy_adjustments(ctx):" not in section
    # ... and the carve-out adjudicates the conflict explicitly.
    assert "Planned change_symbol carve-out (hard)" in section
    assert "`policy.py:_strategy_adjustments`" in section
    assert "NOT a restore authorization" in section
    # Every other reviewer-named symbol — including reachable-chain call
    # sites that are NOT the change_symbol — stays authorized.
    assert "### `policy.py:_unrelated`" in section
    assert "### `policy.py:iter_decisions`" in section
    assert "def _unrelated(ctx):" in section
    assert "def iter_decisions(ctx):" in section

    # Non-mutex feedback (no change_symbol mention) is byte-for-byte the
    # pre-existing behavior: authorized, and no carve-out invented.
    clean = _authorized_parent_restore_section(
        ckpt,
        "Rejected: `_unrelated` drift; restore its national_cloud_v608 form.",
        parent_policy_path=parent,
    )
    assert "### `policy.py:_unrelated`" in clean
    assert "Planned change_symbol carve-out" not in clean
    assert "### `policy.py:_strategy_adjustments`" not in clean


def test_authorized_parent_restore_section_ignores_reviewer_note_side_notes(tmp_path):
    """Symbols named only in reviewer NOTE sections are not restore demands."""

    from tool_planning_quality_contracts import (
        _authorized_parent_restore_section,
    )

    parent = tmp_path / "policy.py"
    parent.write_text(_RESTORE_PARENT_SOURCE, encoding="utf-8")
    feedback = (
        "Rejected: `_unrelated` inherits the parent drift; restore its "
        "national_cloud_v608 form.\n\n"
        "NOTE: This is an in-place repair. `_note_only` and `iter_decisions` "
        "are style observations only.\n"
    )
    section = _authorized_parent_restore_section(
        _restore_ckpt(), feedback, parent_policy_path=parent
    )
    assert "### `policy.py:_unrelated`" in section
    assert "### `policy.py:_note_only`" not in section
    assert "### `policy.py:iter_decisions`" not in section


def test_authorized_parent_restore_section_injects_full_body_above_4000_chars(
    tmp_path,
):
    """A clamped body + a digest of the complete function is a contract the
    worker can never satisfy (v620: 5461-char `_decision_from_equity` cut at
    4000, 4 rounds judged "parent form not restored")."""

    from tool_planning_quality_contracts import (
        _authorized_parent_restore_section,
    )

    lines = ["def _big(ctx):"]
    for index in range(200):
        lines.append(f"    value_{index} = ctx.get('k{index}', {index})")
    lines.append("    return value_199 + 424242")
    body = "\n".join(lines) + "\n"
    assert len(body) > 4000

    parent = tmp_path / "policy.py"
    parent.write_text(body, encoding="utf-8")
    section = _authorized_parent_restore_section(
        _restore_ckpt("policy.py:_strategy_adjustments"),
        "Rejected: `_big` must be restored to its parent form.",
        parent_policy_path=parent,
    )
    assert "### `policy.py:_big`" in section
    assert "(truncated" not in section
    assert body in section
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    assert f"sha256[:16]={digest}" in section
    assert "return value_199 + 424242" in section


def test_authorized_parent_restore_section_renders_carve_out_without_parent_funcs(
    tmp_path,
):
    """When the reviewer names ONLY the change_symbol, the section still
    adjudicates it (and injects nothing else)."""

    from tool_planning_quality_contracts import (
        _authorized_parent_restore_section,
    )

    parent = tmp_path / "policy.py"
    parent.write_text(_RESTORE_PARENT_SOURCE, encoding="utf-8")
    section = _authorized_parent_restore_section(
        _restore_ckpt(),
        "Rejected: restore `_strategy_adjustments` to its parent form.",
        parent_policy_path=parent,
    )
    assert "### `policy.py:_strategy_adjustments`" not in section
    assert "Planned change_symbol carve-out (hard)" in section
    assert "### `policy.py:_unrelated`" not in section


def test_review_rework_prompt_carve_out_keeps_planned_change_symbol(
    tmp_path, monkeypatch
):
    """The review-rework prompt inherits the carve-out: the planned change
    survives while the reviewer-named drift function is restored."""

    import tool_planning_quality_contracts as contracts
    import tool_planning_quality_rework as rw

    monkeypatch.setattr(
        contracts,
        "_extract_parent_functions",
        lambda _path, wanted: {
            name: f"def {name}(ctx):\n    return 1\n"
            for name in wanted
            if name in {"_strategy_adjustments", "_unrelated"}
        },
    )
    feedback = (
        "Rejected: the planned contract permits changes only to "
        "`policy.py:_strategy_adjustments`, but the candidate also changed "
        "`_unrelated`."
    )
    ckpt = {
        "next_v": 621,
        "source_v": 608,
        "parent2_v": 27,
        "stage": "repair_planned",
        "reviewer_feedback": feedback,
        "master_plan": _restore_ckpt()["master_plan"],
        "gate_results": {
            "review": {"passed": False, "approved": False, "feedback": feedback}
        },
    }
    tasks = rw._synthesize_rework_tasks_from_checkpoint(ckpt, feedback)
    assert tasks
    prompt = "\n".join(str(task.get("worker_prompt") or "") for task in tasks)
    assert "Authorized parent-restore set" in prompt
    assert "### `policy.py:_unrelated`" in prompt
    assert "### `policy.py:_strategy_adjustments`" not in prompt
    assert "Planned change_symbol carve-out (hard)" in prompt


def test_review_rework_prompt_omits_restore_section_for_approved_review(
    monkeypatch,
):
    """A passing review never renders restore authorization (the section is
    review-rejection-only, even if the prose mentions symbols)."""

    import tool_planning_quality_contracts as contracts
    import tool_planning_quality_rework as rw

    monkeypatch.setattr(
        contracts,
        "_extract_parent_functions",
        lambda _path, wanted: {
            name: f"def {name}(ctx):\n    return 1\n" for name in wanted
        },
    )
    feedback = "Approved. `_unrelated` and `_strategy_adjustments` were reviewed."
    ckpt = {
        "next_v": 621,
        "source_v": 608,
        "parent2_v": 27,
        "stage": "critic_checked",
        "reviewer_feedback": feedback,
        "master_plan": _restore_ckpt()["master_plan"],
        "gate_results": {
            "review": {"passed": True, "approved": True, "feedback": feedback}
        },
    }
    tasks = rw._synthesize_rework_tasks_from_checkpoint(ckpt, feedback)
    # A non-rework stage synthesizes nothing; and even when the same checkpoint
    # is force-classified as a repair stage the approved review is not one.
    assert tasks == []
    assert (
        rw._qc._is_review_rework_checkpoint(dict(ckpt, stage="repair_planned"))
        is False
    )


def test_review_rework_prompt_carries_parent_restore(tmp_path):
    """The review-rework fallback prompt interpolates the restore section."""
    parent = tmp_path / "policy.py"
    parent.write_text(
        "def _strategy_adjustments(ctx):\n    return ctx['x'] + 1\n",
        encoding="utf-8",
    )
    import tool_planning_quality_rework as rw

    real_section = rw._qc._authorized_parent_restore_section
    ckpt = {
        "next_v": 616,
        "source_v": 608,
        "parent2_v": 598,
        "stage": "repair_planned",
        "reviewer_feedback": "Rejected: `_strategy_adjustments` drift; restore v608 form.",
        "gate_results": {"review": {"passed": False, "approved": False, "feedback": "Rejected: `_strategy_adjustments` drift; restore v608 form."}},
    }
    tasks = rw._synthesize_rework_tasks_from_checkpoint(
        ckpt, ckpt["reviewer_feedback"]
    )
    assert tasks
    prompts = [str(t.get("worker_prompt") or "") for t in tasks]
    assert any("Authorized parent-restore set" in p for p in prompts), prompts[0][:300]
    assert callable(real_section)
