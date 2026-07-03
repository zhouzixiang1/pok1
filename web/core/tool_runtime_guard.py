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

from evolution_infra import EVOLUTION_BRANCH, PROJECT_ROOT, read_pipeline_checkpoint
from repo_state import get_last_snapshot, git_worktree_snapshot, is_generated_bot_dir_entry

_BOT_DIR_RE = re.compile(r"^\?\? bots/claude_v(?P<version>\d+)/$")
_HEAD_CHANGE_ALLOWED_TOOLS = {"run_archivist"}
_HEAD_DRIFT_REPAIR_STAGES = {
    "quality_failed",
    "precommit_failed",
    "repair_planned",
    "rework_running",
}
_HEAD_DRIFT_TOOL_BY_STAGE = {
    "quality_failed": {"execute_workers"},
    "precommit_failed": {"execute_workers"},
    "repair_planned": {"execute_workers"},
    "rework_running": {"execute_workers"},
    "quality_passed": {"run_review"},
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


def _entry_allowed(line: str, candidate_v: int | None) -> bool:
    stripped = line.strip()
    match = _BOT_DIR_RE.match(stripped)
    if match:
        return candidate_v is not None and int(match.group("version")) == int(candidate_v)
    return False


def _unexpected_entries(snapshot: dict[str, Any], candidate_v: int | None) -> list[str]:
    return [
        line for line in snapshot.get("entries", []) or []
        if not _entry_allowed(line, candidate_v)
    ]


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

    A failed checkpoint may legitimately survive a codebase update: the next
    correct tool is ``execute_workers`` with the recorded gate failures. Blocking
    that path leaves the service unable to start. Post-quality checkpoints can
    also continue through reviewer/critic/precommit/commit after the candidate
    has been revalidated on the current HEAD. We only allow the exact next tool
    for the checkpoint stage on the canonical branch, for the active checkpoint
    version, and when the worktree has no unexpected entries beyond that
    candidate bot directory.
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
    if current_branch != EVOLUTION_BRANCH:
        return False, {}
    unexpected = _unexpected_entries(snapshot, candidate_v)
    if unexpected:
        return False, {"unexpected_entries": unexpected[:40]}
    resume_kind = "repair" if stage in _HEAD_DRIFT_REPAIR_STAGES else "post_quality"
    return True, {
        "stage": stage,
        "candidate_v": candidate_v,
        "baseline_head": baseline_head,
        "current_head": current_head,
        "branch": snapshot.get("branch"),
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


def ensure_runtime_git_guard(tool_name: str, args: dict[str, Any] | None = None) -> tuple[bool, dict[str, Any]]:
    """Ensure mutating pipeline tools run on the canonical branch and clean codebase."""
    args = args or {}
    if not _guard_enabled():
        return True, {"guard": "disabled"}

    candidate_v = _candidate_version(tool_name, args)
    before = git_worktree_snapshot()
    current_branch = _branch_name(str(before.get("branch") or ""))

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

    if current_branch and current_branch != EVOLUTION_BRANCH:
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

    snapshot = git_worktree_snapshot()
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
            "directive": "A git commit changed the runtime code during this generation. Abandon and restart from a fresh baseline.",
        }
        _log_guard_event("repo.runtime_guard_blocked", "error", "Runtime git guard blocked HEAD drift", payload)
        return False, payload

    unexpected = _unexpected_entries(snapshot, candidate_v)
    if unexpected:
        payload = {
            "blocked": True,
            "reason": "unexpected_worktree_entries",
            "tool": tool_name,
            "candidate_v": candidate_v,
            "branch": snapshot.get("branch"),
            "head": snapshot.get("head"),
            "unexpected_entries": unexpected[:40],
            "generated_bot_dirs": [
                line for line in snapshot.get("entries", []) or []
                if is_generated_bot_dir_entry(line)
            ][:40],
            "directive": "Unexpected repository changes appeared during evolution. Stop, inspect, then abandon or clean before retrying.",
        }
        _log_guard_event("repo.runtime_guard_blocked", "error", "Runtime git guard blocked unexpected worktree entries", payload)
        return False, payload

    return True, {
        "guard": "ok",
        "tool": tool_name,
        "candidate_v": candidate_v,
        "branch": snapshot.get("branch"),
        "head": snapshot.get("head"),
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
