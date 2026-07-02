"""Regression tests for the 2026-06-16 architecture-audit fixes.

Covers the fixes that the adversarial post-edit audit flagged as needing
hardening, plus the highest-value new gates:

- B-group: `_reset_target_files_to_source` must roll back to a worker's
  pre-run baseline (not source) so that sequential-overlap siblings' edits
  are preserved (Warning 1 from the audit), while still unlinking partial
  NEW files on retry.
- D-group: `normalize_worker_role` resolves mixed role strings to the
  stricter 'tuner' boundary.
- C-group: `_validate_master_plan` rejects Master-emitted source-override
  fields (branch_from) before Pydantic silently drops them.
- E-group: `MasterPlan` rejects duplicate worker_ids.
"""

import pytest

from core import agent_workers as aw
from core.tool_helpers import normalize_worker_role
from core.tool_planning import _validate_master_plan
from core.output_schema import MasterPlan, WorkerTask


# ── B-group: _reset_target_files_to_source baseline semantics ──

def _setup_dirs(tmp_path, source_v=105, next_v=106):
    source_dir = tmp_path / "bots" / f"claude_v{source_v}"
    next_dir = tmp_path / "bots" / f"claude_v{next_v}"
    source_dir.mkdir(parents=True, exist_ok=True)
    next_dir.mkdir(parents=True, exist_ok=True)
    return source_dir, next_dir


def test_reset_with_baseline_preserves_sibling_edits(tmp_path, monkeypatch):
    """Warning 1 regression: baseline-based reset must NOT revert a sequential
    sibling's edits back to source."""
    source_dir, next_dir = _setup_dirs(tmp_path)
    (source_dir / "strategy.py").write_text("SOURCE", encoding="utf-8")
    # next_dir carries an earlier sibling worker's edit
    (next_dir / "strategy.py").write_text("SOURCE + SIBLING EDIT", encoding="utf-8")

    monkeypatch.setattr(
        aw, "get_bot_dir",
        lambda v: tmp_path / "bots" / f"claude_v{v}",
    )

    task = {"target_files": ["strategy.py"]}
    baseline = {(0, "strategy.py"): "SOURCE + SIBLING EDIT"}

    aw._reset_target_files_to_source(
        task, 105, next_dir, 106, baseline_snapshots=baseline, task_idx=0,
    )

    assert (next_dir / "strategy.py").read_text(encoding="utf-8") == "SOURCE + SIBLING EDIT"


def test_reset_with_baseline_restores_pre_worker_content(tmp_path, monkeypatch):
    """A retry resets to the worker's own pre-run baseline (which may differ
    from both source and the current partial output)."""
    source_dir, next_dir = _setup_dirs(tmp_path)
    (source_dir / "strategy.py").write_text("SOURCE", encoding="utf-8")
    # The worker started from an already-sibling-modified state, then produced
    # a partial/failed edit on top.
    (next_dir / "strategy.py").write_text("PARTIAL FAILED EDIT", encoding="utf-8")

    monkeypatch.setattr(
        aw, "get_bot_dir",
        lambda v: tmp_path / "bots" / f"claude_v{v}",
    )

    task = {"target_files": ["strategy.py"]}
    baseline = {(0, "strategy.py"): "SOURCE + SIBLING EDIT"}  # pre-worker state

    aw._reset_target_files_to_source(
        task, 105, next_dir, 106, baseline_snapshots=baseline, task_idx=0,
    )

    # Rolled back to baseline (pre-worker), NOT source.
    assert (next_dir / "strategy.py").read_text(encoding="utf-8") == "SOURCE + SIBLING EDIT"


def test_reset_unlinks_new_file_with_empty_baseline(tmp_path, monkeypatch):
    """A NEW file (absent pre-worker per baseline) must be unlinked on retry,
    clearing the partial NEW-file pollution that B-group targets."""
    source_dir, next_dir = _setup_dirs(tmp_path)
    (source_dir / "strategy.py").write_text("SOURCE", encoding="utf-8")
    # foo.py is a NEW file not present in source; worker wrote partial content.
    (next_dir / "foo.py").write_text("PARTIAL", encoding="utf-8")

    monkeypatch.setattr(
        aw, "get_bot_dir",
        lambda v: tmp_path / "bots" / f"claude_v{v}",
    )

    task = {"target_files": ["foo.py"]}
    baseline = {(0, "foo.py"): ""}  # empty => did not exist pre-worker

    aw._reset_target_files_to_source(
        task, 105, next_dir, 106, baseline_snapshots=baseline, task_idx=0,
    )

    assert not (next_dir / "foo.py").exists()


def test_reset_without_baseline_falls_back_to_source(tmp_path, monkeypatch):
    """Without a baseline, source-based reset is used: existing file restored
    to source, source-absent NEW file unlinked."""
    source_dir, next_dir = _setup_dirs(tmp_path)
    (source_dir / "strategy.py").write_text("SOURCE", encoding="utf-8")
    (next_dir / "strategy.py").write_text("MODIFIED", encoding="utf-8")
    (next_dir / "foo.py").write_text("PARTIAL", encoding="utf-8")

    monkeypatch.setattr(
        aw, "get_bot_dir",
        lambda v: tmp_path / "bots" / f"claude_v{v}",
    )

    task = {"target_files": ["strategy.py", "foo.py"]}

    aw._reset_target_files_to_source(task, 105, next_dir, 106)

    assert (next_dir / "strategy.py").read_text(encoding="utf-8") == "SOURCE"
    assert not (next_dir / "foo.py").exists()


# ── B-group extension: undeclared-NEW-file unlink (Fix 1) ──

def test_unlink_undeclared_new_files_removes_worker_created_files(tmp_path):
    """_unlink_undeclared_new_files removes .py files a worker created that were
    NOT present before it ran (the undeclared-target gap that
    _reset_target_files_to_source cannot see, since it only iterates declared
    target_files)."""
    next_dir = tmp_path / "bots" / "claude_v106"
    next_dir.mkdir(parents=True)
    # Pre-run state: strategy.py + a legitimate sibling NEW file (sibling_extra.py)
    (next_dir / "strategy.py").write_text("SOURCE", encoding="utf-8")
    (next_dir / "sibling_extra.py").write_text("# sibling", encoding="utf-8")
    pre_run = {p.name for p in next_dir.glob("*.py")}
    # Worker then created two undeclared NEW files (e.g. via Edit on a stray path)
    (next_dir / "worker_created.py").write_text("# partial", encoding="utf-8")
    (next_dir / "stray_module.py").write_text("# stale", encoding="utf-8")

    aw._unlink_undeclared_new_files(next_dir, pre_run)

    # Undeclared worker-created files removed.
    assert not (next_dir / "worker_created.py").exists()
    assert not (next_dir / "stray_module.py").exists()
    # Pre-run files (incl. legitimate sibling NEW file) preserved.
    assert (next_dir / "strategy.py").exists()
    assert (next_dir / "sibling_extra.py").exists()


def test_unlink_undeclared_new_files_noop_without_snapshot(tmp_path):
    """If pre_run_py_files is empty (snapshot never captured), the helper must
    NOT remove anything — avoids deleting legitimate files when the safety
    precondition is unmet."""
    next_dir = tmp_path / "bots" / "claude_v106"
    next_dir.mkdir(parents=True)
    (next_dir / "legit.py").write_text("# keep me", encoding="utf-8")

    aw._unlink_undeclared_new_files(next_dir, set())

    assert (next_dir / "legit.py").exists()


def test_unlink_undeclared_new_files_noop_on_missing_dir(tmp_path):
    """Non-existent next_dir must not raise."""
    aw._unlink_undeclared_new_files(tmp_path / "does_not_exist", {"a.py"})


# ── D-group: normalize_worker_role ──

def test_normalize_worker_role_basic_and_mixed():
    assert normalize_worker_role("Algorithmic Logic Architect") == "architect"
    assert normalize_worker_role("Hyperparameter Tuner") == "tuner"
    assert normalize_worker_role("Tuner") == "tuner"
    assert normalize_worker_role("HP Tuner") == "tuner"
    assert normalize_worker_role("hyperparameter") == "tuner"
    assert normalize_worker_role("Opponent Modeler") == "other"
    assert normalize_worker_role("") == "other"
    assert normalize_worker_role(None) == "other"
    # Mixed role must resolve to the stricter 'tuner' boundary (reorder fix).
    assert normalize_worker_role("Hyperparameter Tuner (Architect-assisted)") == "tuner"
    assert normalize_worker_role("Algorithmic Logic Architect + Tuner") == "tuner"


# ── C-group: _validate_master_plan source-override hard gate ──

def _valid_task(**over):
    base = {
        "worker_id": 1,
        "role": "Algorithmic Logic Architect",
        "target_files": ["strategy.py"],
        "skill_layer": "spr",
        "worker_prompt": "make a focused structural change to strategy.py" * 3,
    }
    base.update(over)
    return base


def test_validate_master_plan_rejects_branch_from():
    plan = {"branch_from": "claude_v99", "tasks": [_valid_task()]}
    errors, _ = _validate_master_plan(plan, next_v=106)
    assert any("branch_from" in e.lower() or "source-override" in e.lower() for e in errors), errors


def test_validate_master_plan_rejects_source_override():
    plan = {"source_override": "claude_v88", "tasks": [_valid_task()]}
    errors, _ = _validate_master_plan(plan, next_v=106)
    assert any("source-override" in e.lower() for e in errors), errors


def test_validate_master_plan_accepts_plan_without_source_override():
    plan = {"tasks": [_valid_task()]}
    errors, _ = _validate_master_plan(plan, next_v=106)
    assert not any("source-override" in e.lower() or "branch_from" in e.lower() for e in errors), errors


def test_validate_master_plan_rejects_tuner_non_constants_target():
    plan = {
        "tasks": [
            _valid_task(
                role="Hyperparameter Tuner",
                target_files=["strategy_helpers.py"],
                worker_prompt=(
                    "Tune an existing helper threshold based on match data; "
                    "this intentionally targets a non-constants module."
                ),
            )
        ]
    }
    errors, _ = _validate_master_plan(plan, next_v=106)
    assert any("non-constants" in e and "strategy_helpers.py" in e for e in errors), errors


def _valid_plan(tasks):
    return {
        "analysis": "stagnation detected; pivot to value extraction",
        "targeted_failure": "river overfold vs passive opponent",
        "tasks": tasks,
    }


# ── E-group: MasterPlan worker_id uniqueness ──

def test_masterplan_rejects_duplicate_worker_id():
    with pytest.raises(Exception):
        MasterPlan(**_valid_plan([
            WorkerTask(**_valid_task(worker_id=1, target_files=["strategy.py"])),
            WorkerTask(**_valid_task(worker_id=1, target_files=["postflop.py"])),
        ]))


def test_masterplan_accepts_distinct_worker_ids():
    # Must not raise.
    MasterPlan(**_valid_plan([
        WorkerTask(**_valid_task(worker_id=1, target_files=["strategy.py"])),
        WorkerTask(**_valid_task(worker_id=2, target_files=["postflop.py"])),
    ]))
