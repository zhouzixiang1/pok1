"""Git/worktree observability helpers for the evolution pipeline."""

from __future__ import annotations

import subprocess
from pathlib import Path


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
    payload = git_worktree_snapshot()
    payload.update(extra)
    try:
        from system_log import log_system_event
        log_system_event(event_type, severity, message, payload)
    except Exception:
        pass
    return payload
