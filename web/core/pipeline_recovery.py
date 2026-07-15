"""Shared recovery diagnostics for active evolution checkpoints."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from bot_namespace import bot_relpath
from evolution_infra import EVOLUTION_BRANCH, PROJECT_ROOT
from evaluation_contract import build_evaluation_contract, contract_bot_versions, evaluate_head_drift
from evolution_scope import classify_status_entries
from pipeline_state import head_drift_resume_policy, head_drift_resume_stages
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
    "repair_planned",
    "rework_running",
    "verified",
    "official_bootstrap_required",
    "official_certifying",
    "official_failed",
    "official_inconclusive",
    "publishing",
    "infra_timed_out",
}
HEAD_DRIFT_REPAIR_STAGES = {
    "quality_failed",
    "precommit_failed",
    "repair_planned",
    "rework_running",
    "official_failed",
}
HEAD_DRIFT_POST_QUALITY_STAGES = {
    "quality_passed",
    "reviewed",
    "critic_checked",
    "verified",
    "official_certifying",
    "publishing",
}
HEAD_DRIFT_GATE_STAGES = {
    "master_planned",
    "workers_done",
}
HEAD_DRIFT_SELECTED_STAGES = {"selected"}
HEAD_DRIFT_PRE_MASTER_STAGES = {"prepared", "direction_audited"}
HEAD_DRIFT_RESUME_STAGES = head_drift_resume_stages()


def _resume_policy(stage: str | None) -> dict[str, Any] | None:
    return head_drift_resume_policy(stage)


def _resume_warning(stage: str | None) -> str:
    policy = _resume_policy(stage) or {}
    suffix = str(policy.get("warning_suffix") or "checkpoint")
    return f"repo_baseline_head_mismatch_{suffix}_resume"


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


def _snapshot_for_recovery(root: Path) -> dict[str, Any]:
    try:
        return git_worktree_snapshot(root, max_lines=10000)
    except TypeError:
        return git_worktree_snapshot(root)


def _target_available_for_resume(root: Path, stage: str | None, next_v: int | None) -> bool:
    policy = _resume_policy(stage)
    if policy and not policy.get("requires_target", True):
        return True
    if next_v is None:
        return False
    return (root / bot_relpath(next_v)).exists()


def _current_branch_alias_resume_allowed(
    *,
    stage: str | None,
    current_branch: str,
    current_head: str,
    baseline_head: str,
    target_available: bool,
    blocking_entries: list[str],
) -> bool:
    """Allow recovery on a temporary branch name when files are unchanged."""
    policy = _resume_policy(stage)
    return bool(
        policy
        and policy.get("branch_alias_allowed", True)
        and current_branch
        and current_branch != EVOLUTION_BRANCH
        and current_head
        and baseline_head
        and current_head == baseline_head
        and target_available
        and not blocking_entries
    )


def _current_branch_unrelated_head_resume_allowed(
    *,
    root: Path,
    checkpoint: dict[str, Any],
    stage: str | None,
    next_v: int | None,
    current_branch: str,
    current_head: str,
    baseline_head: str,
    target_available: bool,
    blocking_entries: list[str],
) -> tuple[bool, dict[str, Any]]:
    """Allow checkpoint recovery on a temporary branch with unrelated HEAD drift."""
    policy = _resume_policy(stage)
    if not (
        policy
        and policy.get("branch_alias_allowed", True)
        and current_branch
        and current_branch != EVOLUTION_BRANCH
        and current_head
        and baseline_head
        and current_head != baseline_head
        and target_available
        and not blocking_entries
    ):
        return False, {}
    allowed, drift = evaluate_head_drift(
        root,
        baseline_head,
        current_head,
        candidate_v=next_v,
        checkpoint=checkpoint,
    )
    if not drift.get("head_drift_paths_available"):
        return False, {"current_branch_head_paths_available": False}
    contract_paths = list(drift.get("head_contract_paths") or [])
    candidate_prefix = bot_relpath(next_v) + "/" if next_v is not None else ""
    candidate_entries = [
        f"?? {path}" for path in contract_paths
        if candidate_prefix and path.startswith(candidate_prefix)
    ]
    blocking = [
        f"?? {path}" for path in contract_paths
        if not candidate_prefix or not path.startswith(candidate_prefix)
    ]
    payload = {
        "current_branch_head_paths_available": True,
        "current_branch_head_changed_paths": drift.get("head_changed_paths", [])[:80],
        "current_branch_head_blocking_entries": blocking[:40],
        "current_branch_head_candidate_entries": candidate_entries[:40],
        "current_branch_head_ignored_entries": drift.get("head_ignored_entries", [])[:40],
        "current_branch_evaluation_contract_unchanged": allowed,
    }
    if not allowed:
        return False, payload
    return True, payload


def _head_is_ancestor(root: Path, ancestor_head: str, descendant_head: str) -> bool:
    if not ancestor_head or not descendant_head:
        return False
    if ancestor_head == descendant_head:
        return True
    try:
        proc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor_head, descendant_head],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return False
    return proc.returncode == 0


def _baseline_branch_alias_resume_allowed(
    *,
    root: Path,
    stage: str | None,
    next_v: int | None,
    current_branch: str,
    baseline_branch: str,
    current_head: str,
    baseline_head: str,
    target_available: bool,
    blocking_entries: list[str],
    current_branch_alias_allowed: bool,
) -> tuple[bool, dict[str, Any]]:
    if not (baseline_branch and current_branch and baseline_branch != current_branch):
        return False, {}
    if current_branch_alias_allowed:
        return True, {"baseline_branch_alias_reason": "current_branch_alias_same_head"}
    if not (
        _resume_policy(stage)
        and current_branch == EVOLUTION_BRANCH
        and baseline_branch != EVOLUTION_BRANCH
        and target_available
        and not blocking_entries
    ):
        return False, {}
    if baseline_head and current_head and baseline_head == current_head:
        return True, {"baseline_branch_alias_reason": "same_head"}
    if _head_is_ancestor(root, baseline_head, current_head):
        return True, {"baseline_branch_alias_reason": "main_resume_ancestor_head_drift"}
    return False, {"baseline_branch_alias_reason": "non_ancestor_head_drift"}


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

    # Epoch identity is a prerequisite for interpreting every later field.
    # Short-circuit before repository/target diagnostics: applying current
    # path contracts to a retired checkpoint would make it look repairable and
    # could route an old ``direction_audited`` payload into run_master.
    from checkpoint_schema import (
        checkpoint_epoch_errors,
        checkpoint_epoch_reset_route,
        live_checkpoint_parent_authority_errors,
        live_policy_epoch_reset_receipt_errors,
    )

    epoch_issues = checkpoint_epoch_errors(checkpoint)
    if not epoch_issues:
        epoch_issues.extend(
            live_checkpoint_parent_authority_errors(
                checkpoint,
                repo_root=root,
            )
        )
    if not epoch_issues:
        epoch_issues.extend(
            live_policy_epoch_reset_receipt_errors(
                checkpoint,
                project_root=root,
            )
        )
    if epoch_issues:
        route = checkpoint_epoch_reset_route(checkpoint, epoch_issues)
        issues.extend(epoch_issues)
        diag.update(
            {
                "recoverable": False,
                "epoch": {
                    "valid": False,
                    "issues": list(epoch_issues),
                },
                "operator_action": route["operator_action"],
                "operator_command": route["operator_command"],
                "directive": route["directive"],
                "route": route,
            }
        )
        return diag
    diag["epoch"] = {
        "valid": True,
        "evaluation_epoch": checkpoint.get("evaluation_epoch"),
        "mode": (checkpoint.get("epoch_binding") or {}).get("mode"),
        "binding_digest": (checkpoint.get("epoch_binding") or {}).get(
            "binding_digest"
        ),
    }

    if stage == "official_bootstrap_required":
        issues.append("official_bootstrap_requires_operator_action")
        warnings.append("automatic_first_strict_control_consumption_forbidden")
    elif stage == "official_inconclusive":
        issues.append("official_inconclusive_requires_infra_intervention")
        warnings.append("official_full_gate_not_recoverable_by_bot_rework")

    snapshot = snapshot if snapshot is not None else _snapshot_for_recovery(root)
    baseline = checkpoint.get("repo_baseline")
    baseline = baseline if isinstance(baseline, dict) else {}
    current_branch = branch_name(str(snapshot.get("branch") or ""))
    baseline_branch = branch_name(str(baseline.get("branch") or ""))
    current_head = str(snapshot.get("head") or "")
    baseline_head = str(baseline.get("head") or "")
    target_available = _target_available_for_resume(root, stage, next_v)

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
    worktree_scope = classify_status_entries(
        snapshot.get("entries") or [],
        next_v,
        contract_bot_versions=contract_bot_versions(
            candidate_v=next_v,
            checkpoint=checkpoint,
        ),
        evaluation_contract=build_evaluation_contract(
            root,
            candidate_v=next_v,
            source_v=checkpoint.get("source_v"),
            checkpoint=checkpoint,
        ),
    )
    diag["worktree_scope"] = {
        "blocking_entries": (worktree_scope.get("blocking_entries") or [])[:40],
        "ignored_entries": (worktree_scope.get("ignored_entries") or [])[:40],
        "candidate_entries": (worktree_scope.get("candidate_entries") or [])[:40],
        "blocking_count": worktree_scope.get("blocking_count", 0),
        "ignored_count": worktree_scope.get("ignored_count", 0),
    }
    if snapshot.get("truncated"):
        issues.append("worktree_snapshot_truncated")
    if worktree_scope.get("blocking_entries"):
        issues.append("repo_blocking_worktree_entries")
    if worktree_scope.get("ignored_entries"):
        warnings.append("repo_unrelated_worktree_entries_ignored")
    blocking_entries = list(worktree_scope.get("blocking_entries") or [])
    current_branch_alias_allowed = _current_branch_alias_resume_allowed(
        stage=stage,
        current_branch=current_branch,
        current_head=current_head,
        baseline_head=baseline_head,
        target_available=target_available,
        blocking_entries=blocking_entries,
    )
    if current_branch_alias_allowed:
        warnings.append("repo_current_branch_alias_resume")
        repo_diag["current_branch_alias_allowed"] = True
    current_branch_unrelated_head_allowed, current_branch_unrelated_head_diag = (
        _current_branch_unrelated_head_resume_allowed(
            root=root,
            checkpoint=checkpoint,
            stage=stage,
            next_v=next_v,
            current_branch=current_branch,
            current_head=current_head,
            baseline_head=baseline_head,
            target_available=target_available,
            blocking_entries=blocking_entries,
        )
    )
    if current_branch_unrelated_head_allowed:
        warnings.append("repo_current_branch_unrelated_head_resume")
        repo_diag["current_branch_unrelated_head_allowed"] = True
    if current_branch_unrelated_head_diag:
        repo_diag.update(current_branch_unrelated_head_diag)
    baseline_branch_alias_allowed, baseline_branch_alias_diag = _baseline_branch_alias_resume_allowed(
        root=root,
        stage=stage,
        next_v=next_v,
        current_branch=current_branch,
        baseline_branch=baseline_branch,
        current_head=current_head,
        baseline_head=baseline_head,
        target_available=target_available,
        blocking_entries=blocking_entries,
        current_branch_alias_allowed=current_branch_alias_allowed,
    )
    if baseline_branch_alias_allowed:
        warnings.append("repo_baseline_branch_alias_resume")
        repo_diag["baseline_branch_alias_allowed"] = True
    if baseline_branch_alias_diag:
        repo_diag.update(baseline_branch_alias_diag)

    if (
        _enforce_evolution_branch()
        and current_branch
        and current_branch != EVOLUTION_BRANCH
        and not current_branch_alias_allowed
        and not current_branch_unrelated_head_allowed
    ):
        issues.append("repo_not_on_evolution_branch")
    if (
        baseline_branch
        and current_branch
        and baseline_branch != current_branch
        and not baseline_branch_alias_allowed
        and not current_branch_unrelated_head_allowed
    ):
        issues.append("repo_baseline_branch_mismatch")
    if baseline_head and current_head and baseline_head != current_head:
        contract_baseline_present = bool(
            isinstance(baseline.get("evaluation_contract"), dict)
            and baseline.get("evaluation_contract")
        )
        contract_unchanged = False
        contract_diag: dict[str, Any] = {}
        if contract_baseline_present:
            contract_unchanged, contract_diag = evaluate_head_drift(
                root,
                baseline_head,
                current_head,
                candidate_v=next_v,
                checkpoint=checkpoint,
            )
            repo_diag["baseline_evaluation_contract_unchanged"] = contract_unchanged
            repo_diag["baseline_head_contract_paths"] = contract_diag.get("head_contract_paths", [])[:40]
            repo_diag["baseline_head_external_paths"] = contract_diag.get("head_external_paths", [])[:40]
        target_dir = root / bot_relpath(next_v) if next_v is not None else None
        policy = _resume_policy(stage) or {}
        requires_contract_unchanged = bool(policy.get("requires_contract_unchanged", True))
        branch_compatible = (
            current_branch == EVOLUTION_BRANCH
            and (not baseline_branch or baseline_branch == current_branch or baseline_branch_alias_allowed)
        ) or current_branch_unrelated_head_allowed
        can_resume = (
            policy
            and branch_compatible
            and not worktree_scope.get("blocking_entries")
            and target_dir is not None
            and target_available
            and (
                not contract_baseline_present
                or contract_unchanged
                or current_branch_unrelated_head_allowed
                or not requires_contract_unchanged
            )
        )
        if can_resume:
            warnings.append(_resume_warning(stage))
            repo_diag["baseline_head_mismatch_allowed"] = True
            repo_diag["head_drift_resume_kind"] = policy.get("resume_kind", "checkpoint")
            repo_diag["head_drift_allowed_tools"] = list(policy.get("allowed_tools") or [])
            repo_diag["head_drift_requires_contract_unchanged"] = requires_contract_unchanged
        else:
            issues.append("repo_baseline_head_mismatch")

    if stage in TARGET_DIR_STAGES and next_v is not None:
        target_dir = root / bot_relpath(next_v)
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
