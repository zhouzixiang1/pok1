"""Git/worktree observability helpers for the evolution pipeline."""

from __future__ import annotations

import subprocess
import re
from pathlib import Path

_LAST_SNAPSHOT: dict | None = None

_GENERATED_BOT_DIR_RE = re.compile(r"^\?\? bots/claude_v\d+/$")


def get_last_snapshot() -> dict | None:
    """Return the last process-local snapshot recorded by log_git_worktree_snapshot."""
    return dict(_LAST_SNAPSHOT) if _LAST_SNAPSHOT else None


def is_generated_bot_dir_entry(line: str) -> bool:
    return bool(_GENERATED_BOT_DIR_RE.match(line.strip()))


def git_worktree_snapshot(root: str | Path | None = None, *, max_lines: int = 40) -> dict:
    """Return a compact, read-only git status snapshot."""
    if root is None:
        from evolution_infra import PROJECT_ROOT
        root_path = PROJECT_ROOT
    else:
        root_path = Path(root)

    try:
        proc = subprocess.run(
            ["git", "status", "--short", "--branch"],
            cwd=str(root_path),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
    branch = lines[0].replace("## ", "", 1) if lines and lines[0].startswith("## ") else ""
    entries = lines[1:]
    dirty = [line for line in entries if not line.startswith("??")]
    untracked = [line for line in entries if line.startswith("??")]
    generated_bot_dirs = [line for line in entries if is_generated_bot_dir_entry(line)]
    protected = [
        line for line in entries
        if (
            "web/core/" in line
            or "web/tests/" in line
            or "sever/" in line
            or "engine/" in line
            or "web/logs/" in line
            or "web/core/results/" in line
            or ("bots/claude_v" in line and not is_generated_bot_dir_entry(line))
        )
    ]
    head = ""
    try:
        head_proc = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=str(root_path),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if head_proc.returncode == 0:
            head = (head_proc.stdout or "").strip()
    except Exception:
        head = ""
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "branch": branch,
        "head": head,
        "dirty_count": len(dirty),
        "untracked_count": len(untracked),
        "entry_count": len(entries),
        "generated_bot_dirs": generated_bot_dirs[:max_lines],
        "protected_entries": protected[:max_lines],
        "entries": entries[:max_lines],
        "truncated": len(entries) > max_lines,
        "stderr": (proc.stderr or "").strip()[:500],
    }


def log_git_worktree_snapshot(event_type: str, message: str, *, severity: str = "info", **extra) -> dict:
    """Emit a structured git/worktree snapshot event and return the payload."""
    global _LAST_SNAPSHOT
    payload = git_worktree_snapshot()
    payload.update(extra)
    try:
        from system_log import log_system_event
        log_system_event(event_type, severity, message, payload)
        if extra.get("emit_delta"):
            delta = git_worktree_delta(_LAST_SNAPSHOT, payload)
            if not delta.get("has_previous"):
                log_system_event(
                    "repo.worktree_baseline",
                    "info",
                    "Worktree baseline captured",
                    {
                        "branch": payload.get("branch", ""),
                        "head": payload.get("head", ""),
                        "dirty_count": payload.get("dirty_count", 0),
                        "untracked_count": payload.get("untracked_count", 0),
                        "entry_count": payload.get("entry_count", 0),
                    },
                )
            else:
                if delta.get("branch_changed"):
                    log_system_event(
                        "repo.branch_changed",
                        "warn",
                        f"Git branch changed: {delta.get('previous_branch')} -> {delta.get('current_branch')}",
                        delta,
                    )
                if delta.get("head_changed"):
                    log_system_event(
                        "repo.head_changed",
                        "warn",
                        f"Git HEAD changed: {delta.get('previous_head')} -> {delta.get('current_head')}",
                        delta,
                    )
                if delta.get("new_entries") or delta.get("cleared_entries"):
                    log_system_event(
                        "repo.worktree_changed",
                        "warn" if delta.get("new_protected_entries") or delta.get("new_dirty_entries") else "info",
                        "Git worktree entries changed",
                        delta,
                    )
    except Exception:
        pass
    _LAST_SNAPSHOT = payload
    return payload


def git_worktree_delta(previous: dict | None, current: dict) -> dict:
    """Compare two compact snapshots and return branch/entry deltas."""
    if not previous:
        return {"has_previous": False}
    prev_entries = set(previous.get("entries") or [])
    curr_entries = set(current.get("entries") or [])
    new_entries = sorted(curr_entries - prev_entries)
    cleared_entries = sorted(prev_entries - curr_entries)
    new_dirty = [line for line in new_entries if not line.startswith("??")]
    new_generated_bot_dirs = [line for line in new_entries if is_generated_bot_dir_entry(line)]
    new_protected = [
        line for line in new_entries
        if (
            "web/core/" in line
            or "web/tests/" in line
            or "sever/" in line
            or "engine/" in line
            or ("bots/claude_v" in line and not is_generated_bot_dir_entry(line))
        )
    ]
    prev_branch = previous.get("branch", "")
    curr_branch = current.get("branch", "")
    prev_head = previous.get("head", "")
    curr_head = current.get("head", "")
    return {
        "has_previous": True,
        "previous_branch": prev_branch,
        "current_branch": curr_branch,
        "branch_changed": bool(prev_branch and curr_branch and prev_branch != curr_branch),
        "previous_head": prev_head,
        "current_head": curr_head,
        "head_changed": bool(prev_head and curr_head and prev_head != curr_head),
        "previous_dirty_count": previous.get("dirty_count", 0),
        "current_dirty_count": current.get("dirty_count", 0),
        "previous_untracked_count": previous.get("untracked_count", 0),
        "current_untracked_count": current.get("untracked_count", 0),
        "new_entries": new_entries[:40],
        "cleared_entries": cleared_entries[:40],
        "new_dirty_entries": new_dirty[:40],
        "new_generated_bot_dirs": new_generated_bot_dirs[:40],
        "new_protected_entries": new_protected[:40],
    }
