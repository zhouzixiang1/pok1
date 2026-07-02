"""Git/worktree observability helpers for the evolution pipeline."""

from __future__ import annotations

import subprocess
from pathlib import Path

_LAST_SNAPSHOT: dict | None = None


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
    protected = [
        line for line in entries
        if "web/core/results/" in line
        or "web/logs/" in line
        or "bots/claude_v" in line
    ]
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "branch": branch,
        "dirty_count": len(dirty),
        "untracked_count": len(untracked),
        "entry_count": len(entries),
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
    new_protected = [
        line for line in new_entries
        if "web/core/" in line or "sever/" in line or "engine/" in line or "bots/claude_v" in line
    ]
    prev_branch = previous.get("branch", "")
    curr_branch = current.get("branch", "")
    return {
        "has_previous": True,
        "previous_branch": prev_branch,
        "current_branch": curr_branch,
        "branch_changed": bool(prev_branch and curr_branch and prev_branch != curr_branch),
        "previous_dirty_count": previous.get("dirty_count", 0),
        "current_dirty_count": current.get("dirty_count", 0),
        "previous_untracked_count": previous.get("untracked_count", 0),
        "current_untracked_count": current.get("untracked_count", 0),
        "new_entries": new_entries[:40],
        "cleared_entries": cleared_entries[:40],
        "new_dirty_entries": new_dirty[:40],
        "new_protected_entries": new_protected[:40],
    }
