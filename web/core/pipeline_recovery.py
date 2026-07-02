"""Shared recovery diagnostics for active evolution checkpoints."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from evolution_infra import EVOLUTION_BRANCH, PROJECT_ROOT
from repo_state import git_worktree_snapshot

INACTIVE_STAGES = {None, "archived", "abandoned", "timed_out"}
TARGET_DIR_STAGES = {
    "preparing",
    "prepared",
    "crossover_running",
    "direction_audited",
    "master_planned",
    "workers_done",
    "quality_failed",
    "quality_passed",
    "reviewed",
    "critic_checked",
    "precommit_failed",
    "verified",
    "infra_timed_out",
}


def branch_name(branch_status: str | None) -> str:
    """Return the branch name from ``git status --short --branch`` output."""
    return (branch_status or "").split("...", 1)[0].split()[0]


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _enforce_evolution_branch() -> bool:
    if os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get("POK_FORCE_PIPELINE_RECOVERY_GUARD") != "1":
        return False
    return True


def checkpoint_recovery_diagnostics(
    checkpoint: dict[str, Any] | None,
    *,
    snapshot: dict[str, Any] | None = None,
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    """Describe whether a checkpoint can be safely resumed on this worktree.

    This is intentionally read-only. It does not clear checkpoints or move bot
    directories because losing generation artifacts should be an explicit
    recovery action, not a side effect of a health probe.
    """
    if not checkpoint:
        return {"active": False, "recoverable": True, "issues": []}

    root = Path(project_root or PROJECT_ROOT)
    stage = checkpoint.get("stage")
    next_v = _as_int(checkpoint.get("next_v"))
    active = next_v is not None and stage not in INACTIVE_STAGES
    issues: list[str] = []
    warnings: list[str] = []

    diag: dict[str, Any] = {
        "active": active,
        "recoverable": True,
        "issues": issues,
        "warnings": warnings,
        "stage": stage,
        "next_v": next_v,
    }
    if not active:
        return diag

    snapshot = snapshot if snapshot is not None else git_worktree_snapshot(root)
    baseline = checkpoint.get("repo_baseline")
    baseline = baseline if isinstance(baseline, dict) else {}
    current_branch = branch_name(str(snapshot.get("branch") or ""))
    baseline_branch = branch_name(str(baseline.get("branch") or ""))
    current_head = str(snapshot.get("head") or "")
    baseline_head = str(baseline.get("head") or "")

    repo_diag = {
        "current_branch": current_branch,
        "expected_branch": EVOLUTION_BRANCH,
        "current_head": current_head,
        "baseline_branch": baseline_branch,
        "baseline_head": baseline_head,
        "snapshot_ok": snapshot.get("ok"),
        "snapshot_error": snapshot.get("error"),
    }
    diag["repo"] = repo_diag
    if snapshot.get("truncated"):
        issues.append("worktree_snapshot_truncated")
    if _enforce_evolution_branch() and current_branch and current_branch != EVOLUTION_BRANCH:
        issues.append("repo_not_on_evolution_branch")
    if baseline_branch and current_branch and baseline_branch != current_branch:
        issues.append("repo_baseline_branch_mismatch")
    if baseline_head and current_head and baseline_head != current_head:
        issues.append("repo_baseline_head_mismatch")

    if stage in TARGET_DIR_STAGES and next_v is not None:
        target_dir = root / "bots" / f"claude_v{next_v}"
        diag["target"] = {
            "path": str(target_dir),
            "exists": target_dir.exists(),
            "completed": (target_dir / ".completed").exists(),
        }
        if not target_dir.exists():
            warnings.append("target_bot_dir_missing")

    diag["recoverable"] = not issues
    return diag


def checkpoint_recovery_blockers(checkpoint: dict[str, Any] | None) -> list[str]:
    """Return blocking issue names for a checkpoint on the current worktree."""
    return list(checkpoint_recovery_diagnostics(checkpoint).get("issues") or [])
