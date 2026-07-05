"""Runtime git/worktree guard for mutating pipeline MCP tools."""

from __future__ import annotations

import functools
import inspect
import json
import os
import re
import subprocess
from typing import Any, Callable

from claude_agent_sdk import tool as sdk_tool

from bot_namespace import ACTIVE_BOT_PREFIX, bot_relpath
from evolution_infra import EVOLUTION_BRANCH, PROJECT_ROOT, read_pipeline_checkpoint
from evaluation_contract import evaluate_head_drift
from repo_state import get_last_snapshot, git_worktree_snapshot, is_generated_bot_dir_entry
from evolution_scope import (
    classify_status_entries,
)

_BOT_DIR_RE = re.compile(rf"^\?\? bots/{re.escape(ACTIVE_BOT_PREFIX)}(?P<version>\d+)/$")
_HEAD_CHANGE_ALLOWED_TOOLS = {"run_archivist"}
_HEAD_DRIFT_REPAIR_STAGES = {
    "quality_failed",
    "precommit_failed",
    "repair_planned",
    "rework_running",
}
_HEAD_DRIFT_TOOL_BY_STAGE = {
    "selected": {"prepare_next_gen", "run_crossover"},
    "prepared": {"run_direction_audit"},
    "direction_audited": {"run_literature_probe", "run_master"},
    "master_planned": {"execute_workers"},
    "workers_done": {"run_quality_gates"},
    "quality_failed": {"execute_workers"},
    "precommit_failed": {"execute_workers"},
    "repair_planned": {"execute_workers"},
    "rework_running": {"execute_workers"},
    "quality_passed": {"run_review", "execute_workers"},
    "reviewed": {"run_critic"},
    "critic_checked": {"run_precommit_eval"},
    "verified": {"commit_bot"},
}


def _json_tool_result(data: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(data, indent=2, ensure_ascii=False)}]}


def _guard_enabled() -> bool:
    if os.environ.get("POK_DISABLE_TOOL_RUNTIME_GUARD") == "1":
        return False
    if os.environ.get("PYTEST_CURRENT_TEST") and os.environ.get("POK_FORCE_TOOL_RUNTIME_GUARD") != "1":
        return False
    return True


def _candidate_version(tool_name: str, args: dict[str, Any]) -> int | None:
    for key in ("next_v", "version", "target_v"):
        value = args.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    checkpoint = read_pipeline_checkpoint()
    if checkpoint and isinstance(checkpoint.get("next_v"), int):
        return int(checkpoint["next_v"])
    if tool_name in {"cleanup_incomplete", "abandon_generation"}:
        try:
            from evolution_infra import compute_next_generation_v
            return int(compute_next_generation_v())
        except Exception:
            return None
    return None


def _source_version(args: dict[str, Any]) -> int | None:
    value = args.get("source_v")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    checkpoint = read_pipeline_checkpoint()
    if checkpoint and isinstance(checkpoint.get("source_v"), int):
        return int(checkpoint["source_v"])
    return None


def _entry_allowed(line: str, candidate_v: int | None) -> bool:
    stripped = line.strip()
    match = _BOT_DIR_RE.match(stripped)
    if match:
        return candidate_v is not None and int(match.group("version")) == int(candidate_v)
    return False


def _snapshot() -> dict[str, Any]:
    """Capture enough status lines for in-place shared worktrees."""
    try:
        return git_worktree_snapshot(max_lines=10000)
    except TypeError:
        # Tests commonly monkeypatch git_worktree_snapshot with a zero-arg lambda.
        return git_worktree_snapshot()


def _scope(snapshot: dict[str, Any], candidate_v: int | None) -> dict[str, Any]:
    return classify_status_entries(snapshot.get("entries") or [], candidate_v)


def _unexpected_entries(snapshot: dict[str, Any], candidate_v: int | None) -> list[str]:
    return list(_scope(snapshot, candidate_v).get("blocking_entries") or [])


def _ignored_entries(snapshot: dict[str, Any], candidate_v: int | None) -> list[str]:
    return list(_scope(snapshot, candidate_v).get("ignored_entries") or [])


def _checkpoint_repo_baseline(candidate_v: int | None) -> dict[str, Any] | None:
    if candidate_v is None:
        return None
    checkpoint = read_pipeline_checkpoint()
    if not checkpoint:
        return None
    try:
        if int(checkpoint.get("next_v") or -1) != int(candidate_v):
            return None
    except Exception:
        return None
    baseline = checkpoint.get("repo_baseline")
    return dict(baseline) if isinstance(baseline, dict) else None


def _head_change_allowed_for_checkpoint_resume(
    *,
    tool_name: str,
    candidate_v: int | None,
    baseline_head: str,
    current_head: str,
    snapshot: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """Allow safe checkpoint continuation after infrastructure HEAD changes.

    A checkpoint may legitimately survive a codebase update: a saved master
    plan must continue with the initial workers, and a failed checkpoint must
    continue with ``execute_workers`` plus the recorded gate failures. Blocking
    those paths leaves the service unable to start. Post-quality checkpoints
    can also continue through reviewer/critic/precommit/commit after the
    candidate has been revalidated on the current HEAD. We only allow the exact
    next tool for the checkpoint stage on the canonical branch, for the active
    checkpoint version, and when the worktree has no unexpected entries beyond
    that candidate bot directory.
    """
    if not baseline_head or not current_head or baseline_head == current_head:
        return False, {}
    checkpoint = read_pipeline_checkpoint()
    if not checkpoint:
        return False, {}
    try:
        if candidate_v is None or int(checkpoint.get("next_v") or -1) != int(candidate_v):
            return False, {}
    except Exception:
        return False, {}
    stage = str(checkpoint.get("stage") or "")
    allowed_tools = _HEAD_DRIFT_TOOL_BY_STAGE.get(stage, set())
    if tool_name not in allowed_tools:
        return False, {}
    current_branch = _branch_name(str(snapshot.get("branch") or ""))
    branch_alias_allowed = _branch_alias_allowed_for_tool(tool_name, snapshot)
    if current_branch != EVOLUTION_BRANCH and not branch_alias_allowed:
        return False, {}
    unexpected = _unexpected_entries(snapshot, candidate_v)
    if unexpected:
        return False, {"unexpected_entries": unexpected[:40]}
    if stage == "selected":
        resume_kind = "selected"
    elif stage in {"prepared", "direction_audited"}:
        resume_kind = "pre_master"
    elif stage == "master_planned":
        resume_kind = "initial_workers"
    elif stage == "workers_done":
        resume_kind = "gate"
    elif stage in _HEAD_DRIFT_REPAIR_STAGES:
        resume_kind = "repair"
    else:
        resume_kind = "post_quality"
    return True, {
        "stage": stage,
        "candidate_v": candidate_v,
        "baseline_head": baseline_head,
        "current_head": current_head,
        "branch": snapshot.get("branch"),
        "branch_alias_allowed": branch_alias_allowed,
        "resume_kind": resume_kind,
    }


def _run_git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )


def _log_guard_event(event_type: str, severity: str, message: str, data: dict[str, Any]) -> None:
    try:
        from system_log import log_system_event
        log_system_event(event_type, severity, message, data)
    except Exception:
        pass


def _branch_name(branch_status: str) -> str:
    return (branch_status or "").split("...", 1)[0].split()[0]


def _runtime_expected_head() -> str:
    return os.environ.get("POK_RUNTIME_EXPECTED_HEAD", "").strip()


def _branch_alias_allowed_for_tool(tool_name: str, snapshot: dict[str, Any]) -> bool:
    """Allow non-commit tools when only the branch name changed.

    The orchestrator stores its startup HEAD in ``POK_RUNTIME_EXPECTED_HEAD``.
    A branch switch to another name at that same commit does not change the
    files seen by planners, workers, or evaluators. ``commit_bot`` remains
    canonical-branch-only so a final accepted bot is not committed elsewhere.
    """
    if tool_name == "commit_bot":
        return False
    current_branch = _branch_name(str(snapshot.get("branch") or ""))
    if not current_branch or current_branch == EVOLUTION_BRANCH:
        return False
    expected_head = _runtime_expected_head()
    current_head = str(snapshot.get("head") or "").strip()
    return bool(expected_head and current_head and current_head == expected_head)


def _unrelated_head_drift_allowed(
    *,
    candidate_v: int | None,
    source_v: int | None = None,
    baseline_head: str,
    current_head: str,
) -> tuple[bool, dict[str, Any]]:
    checkpoint = read_pipeline_checkpoint()
    allowed, payload = evaluate_head_drift(
        PROJECT_ROOT,
        baseline_head,
        current_head,
        candidate_v=candidate_v,
        source_v=source_v,
        checkpoint=checkpoint if isinstance(checkpoint, dict) else None,
    )
    contract_paths = list(payload.get("head_contract_paths") or [])
    candidate_prefix = bot_relpath(candidate_v) + "/" if candidate_v is not None else ""
    candidate_entries = [
        f"?? {path}" for path in contract_paths
        if candidate_prefix and path.startswith(candidate_prefix)
    ]
    blocking_entries = [
        f"?? {path}" for path in contract_paths
        if not candidate_prefix or not path.startswith(candidate_prefix)
    ]
    payload["head_candidate_entries"] = candidate_entries[:40]
    payload["head_blocking_entries"] = blocking_entries[:40]
    return allowed, payload


def _branch_head_drift_unrelated_allowed(
    *,
    tool_name: str,
    candidate_v: int | None,
    source_v: int | None,
    snapshot: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    if tool_name == "commit_bot":
        return False, {}
    current_branch = _branch_name(str(snapshot.get("branch") or ""))
    if not current_branch or current_branch == EVOLUTION_BRANCH:
        return False, {}
    expected_head = _runtime_expected_head()
    current_head = str(snapshot.get("head") or "").strip()
    if not expected_head or not current_head or expected_head == current_head:
        return False, {}
    allowed, payload = _unrelated_head_drift_allowed(
        candidate_v=candidate_v,
        source_v=source_v,
        baseline_head=expected_head,
        current_head=current_head,
    )
    if not allowed:
        return False, payload
    return True, {
        "tool": tool_name,
        "candidate_v": candidate_v,
        "source_v": source_v,
        "branch": snapshot.get("branch"),
        "expected_branch": EVOLUTION_BRANCH,
        "runtime_expected_head": expected_head,
        "head": current_head,
        **payload,
    }


def ensure_runtime_git_guard(tool_name: str, args: dict[str, Any] | None = None) -> tuple[bool, dict[str, Any]]:
    """Ensure mutating pipeline tools run on the canonical branch and clean codebase."""
    args = args or {}
    if not _guard_enabled():
        return True, {"guard": "disabled"}

    candidate_v = _candidate_version(tool_name, args)
    source_v = _source_version(args)
    before = _snapshot()
    current_branch = _branch_name(str(before.get("branch") or ""))
    branch_alias_allowed = _branch_alias_allowed_for_tool(tool_name, before)
    branch_head_drift_unrelated_allowed, branch_head_drift_payload = _branch_head_drift_unrelated_allowed(
        tool_name=tool_name,
        candidate_v=candidate_v,
        source_v=source_v,
        snapshot=before,
    )

    if before.get("truncated"):
        payload = {
            "blocked": True,
            "reason": "worktree_snapshot_truncated",
            "tool": tool_name,
            "candidate_v": candidate_v,
            "branch": before.get("branch"),
            "head": before.get("head"),
            "entry_count": before.get("entry_count"),
            "entries": (before.get("entries") or [])[:40],
            "directive": "The worktree has too many dirty entries to audit safely. Stop and inspect the full git status before retrying.",
        }
        _log_guard_event(
            "repo.runtime_guard_blocked",
            "error",
            "Runtime git guard blocked truncated worktree snapshot",
            payload,
        )
        return False, payload

    if (
        current_branch
        and current_branch != EVOLUTION_BRANCH
        and not branch_alias_allowed
        and not branch_head_drift_unrelated_allowed
    ):
        unexpected = _unexpected_entries(before, candidate_v)
        payload = {
            "blocked": True,
            "reason": "branch_drift",
            "tool": tool_name,
            "candidate_v": candidate_v,
            "branch": before.get("branch"),
            "expected_branch": EVOLUTION_BRANCH,
            "head": before.get("head"),
            "unexpected_entries": unexpected[:40],
            "directive": (
                "Runtime evolution tools must run from the canonical evolution "
                "branch. Stop the service, inspect this branch/worktree, and "
                "switch branches explicitly before restarting; the guard will "
                "not auto-checkout."
            ),
        }
        _log_guard_event(
            "repo.runtime_guard_blocked",
            "error",
            f"Runtime git guard blocked branch drift before {tool_name}",
            payload,
        )
        return False, payload
    if branch_alias_allowed:
        _log_guard_event(
            "repo.runtime_guard_branch_alias_allowed",
            "warn",
            f"Runtime git guard allowed {tool_name} on branch alias with unchanged HEAD",
            {
                "tool": tool_name,
                "candidate_v": candidate_v,
                "branch": before.get("branch"),
                "expected_branch": EVOLUTION_BRANCH,
                "runtime_expected_head": _runtime_expected_head(),
                "head": before.get("head"),
            },
        )
    elif branch_head_drift_unrelated_allowed:
        _log_guard_event(
            "repo.runtime_guard_branch_head_drift_unrelated_allowed",
            "warn",
            f"Runtime git guard allowed {tool_name} on branch with unrelated HEAD drift",
            branch_head_drift_payload,
        )

    snapshot = _snapshot()
    if snapshot.get("truncated"):
        payload = {
            "blocked": True,
            "reason": "worktree_snapshot_truncated",
            "tool": tool_name,
            "candidate_v": candidate_v,
            "branch": snapshot.get("branch"),
            "head": snapshot.get("head"),
            "entry_count": snapshot.get("entry_count"),
            "entries": (snapshot.get("entries") or [])[:40],
            "directive": "The worktree has too many dirty entries to audit safely. Stop and inspect the full git status before retrying.",
        }
        _log_guard_event(
            "repo.runtime_guard_blocked",
            "error",
            "Runtime git guard blocked truncated worktree snapshot",
            payload,
        )
        return False, payload

    snapshot_branch = _branch_name(str(snapshot.get("branch") or ""))
    snapshot_branch_alias_allowed = _branch_alias_allowed_for_tool(tool_name, snapshot)
    snapshot_branch_head_drift_unrelated_allowed, snapshot_branch_head_drift_payload = (
        _branch_head_drift_unrelated_allowed(
            tool_name=tool_name,
            candidate_v=candidate_v,
            source_v=source_v,
            snapshot=snapshot,
        )
    )
    branch_alias_allowed = branch_alias_allowed or snapshot_branch_alias_allowed
    branch_head_drift_unrelated_allowed = (
        branch_head_drift_unrelated_allowed or snapshot_branch_head_drift_unrelated_allowed
    )
    if (
        snapshot_branch
        and snapshot_branch != EVOLUTION_BRANCH
        and not snapshot_branch_alias_allowed
        and not snapshot_branch_head_drift_unrelated_allowed
    ):
        unexpected = _unexpected_entries(snapshot, candidate_v)
        payload = {
            "blocked": True,
            "reason": "branch_drift",
            "tool": tool_name,
            "candidate_v": candidate_v,
            "branch": snapshot.get("branch"),
            "expected_branch": EVOLUTION_BRANCH,
            "head": snapshot.get("head"),
            "unexpected_entries": unexpected[:40],
            "directive": (
                "Runtime evolution tools must run from the canonical evolution "
                "branch, except for non-commit tools on a branch alias with the "
                "unchanged runtime HEAD."
            ),
        }
        _log_guard_event(
            "repo.runtime_guard_blocked",
            "error",
            f"Runtime git guard blocked branch drift before {tool_name}",
            payload,
        )
        return False, payload
    if snapshot_branch_head_drift_unrelated_allowed and not branch_alias_allowed:
        _log_guard_event(
            "repo.runtime_guard_branch_head_drift_unrelated_allowed",
            "warn",
            f"Runtime git guard allowed {tool_name} on branch with unrelated HEAD drift",
            snapshot_branch_head_drift_payload,
        )

    baseline = _checkpoint_repo_baseline(candidate_v) or get_last_snapshot() or {}
    baseline_head = baseline.get("head") or ""
    current_head = snapshot.get("head") or ""
    enforce_head_stability = tool_name != "prepare_generation" and candidate_v is not None
    if (
        enforce_head_stability
        and tool_name not in _HEAD_CHANGE_ALLOWED_TOOLS
        and baseline_head
        and current_head
        and baseline_head != current_head
    ):
        unrelated_allowed, unrelated_payload = _unrelated_head_drift_allowed(
            candidate_v=candidate_v,
            source_v=source_v,
            baseline_head=baseline_head,
            current_head=current_head,
        )
        if unrelated_allowed:
            _log_guard_event(
                "repo.runtime_guard_head_drift_unrelated_allowed",
                "warn",
                f"Runtime git guard allowed {tool_name} after unrelated HEAD change",
                {
                    "tool": tool_name,
                    "candidate_v": candidate_v,
                    "baseline_head": baseline_head,
                    "current_head": current_head,
                    **unrelated_payload,
                },
            )
            return True, {
                "guard": "ok",
                "head_drift_unrelated_allowed": True,
                "candidate_v": candidate_v,
                "baseline_head": baseline_head,
                "current_head": current_head,
                **unrelated_payload,
            }
        allowed, allowed_payload = _head_change_allowed_for_checkpoint_resume(
            tool_name=tool_name,
            candidate_v=candidate_v,
            baseline_head=baseline_head,
            current_head=current_head,
            snapshot=snapshot,
        )
        if allowed:
            _log_guard_event(
                "repo.runtime_guard_head_drift_repair_allowed",
                "warn",
                f"Runtime git guard allowed {tool_name} after infrastructure HEAD change",
                allowed_payload,
            )
            return True, {
                "guard": "ok",
                "head_drift_resume_allowed": True,
                "head_drift_repair_allowed": allowed_payload.get("resume_kind") == "repair",
                **allowed_payload,
            }
        payload = {
            "blocked": True,
            "reason": "head_changed_during_generation",
            "tool": tool_name,
            "candidate_v": candidate_v,
            "baseline_head": baseline_head,
            "current_head": current_head,
            "baseline_source": "checkpoint" if baseline.get("captured_stage") else "process_snapshot",
            "branch": snapshot.get("branch"),
            **unrelated_payload,
            "directive": "A git commit changed the runtime code during this generation. Abandon and restart from a fresh baseline.",
        }
        _log_guard_event("repo.runtime_guard_blocked", "error", "Runtime git guard blocked HEAD drift", payload)
        return False, payload

    unexpected = _unexpected_entries(snapshot, candidate_v)
    if unexpected:
        ignored = _ignored_entries(snapshot, candidate_v)
        payload = {
            "blocked": True,
            "reason": "unexpected_worktree_entries",
            "tool": tool_name,
            "candidate_v": candidate_v,
            "branch": snapshot.get("branch"),
            "head": snapshot.get("head"),
            "unexpected_entries": unexpected[:40],
            "ignored_entries": ignored[:40],
            "ignored_count": len(ignored),
            "generated_bot_dirs": [
                line for line in snapshot.get("entries", []) or []
                if is_generated_bot_dir_entry(line)
            ][:40],
            "directive": "Unexpected repository changes appeared during evolution. Stop, inspect, then abandon or clean before retrying.",
        }
        _log_guard_event("repo.runtime_guard_blocked", "error", "Runtime git guard blocked unexpected worktree entries", payload)
        return False, payload

    ignored = _ignored_entries(snapshot, candidate_v)
    return True, {
        "guard": "ok",
        "tool": tool_name,
        "candidate_v": candidate_v,
        "branch": snapshot.get("branch"),
        "head": snapshot.get("head"),
        "branch_alias_allowed": branch_alias_allowed,
        "branch_head_drift_unrelated_allowed": branch_head_drift_unrelated_allowed,
        "ignored_entries": ignored[:40],
        "ignored_count": len(ignored),
    }


def tool(name: str, description: str, input_schema: dict[str, Any]):
    """claude_agent_sdk.tool wrapper with runtime git/worktree validation."""
    def decorator(func: Callable[..., Any]):
        @functools.wraps(func)
        async def guarded(args: dict[str, Any]):
            ok, payload = ensure_runtime_git_guard(name, args)
            if not ok:
                return _json_tool_result({
                    "error": "runtime_git_guard_blocked",
                    **payload,
                })
            result = func(args)
            if inspect.isawaitable(result):
                return await result
            return result

        return sdk_tool(name, description, input_schema)(guarded)

    return decorator
