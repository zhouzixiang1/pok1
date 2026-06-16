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
