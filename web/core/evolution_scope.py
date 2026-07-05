"""Path ownership rules for running evolution inside a shared worktree."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from bot_namespace import ACTIVE_BOT_PREFIX, bot_relpath, parse_bot_version

CRITICAL_PREFIXES = (
    "engine/",
    "sever/",
    "web/core/",
    "web/tests/",
)
NON_CONTRACT_PREFIXES = (
    # Original national-platform documents and Windows reference assets. They
    # are important references, but changing them does not alter a running
    # local evaluation unless the Python server/engine code changes too.
    "sever/国赛平台/",
)
CRITICAL_EXACT = {
    "web/main.py",
}
RUNTIME_PREFIXES = (
    "web/core/results/",
    "web/logs/",
    "web/frontend/dist/",
    "web/server/static/",
    "results/",
    "ladder_results/",
    "bots/graveyard/",
)

_ACTIVE_BOT_RE = re.compile(rf"^bots/{re.escape(ACTIVE_BOT_PREFIX)}(?P<version>\d+)(?:/|$)")


def normalize_repo_path(path: str) -> str:
    """Normalize a git porcelain path to a slash-separated relative path."""
    path = (path or "").strip().strip('"').replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def status_entry_paths(entry: str) -> list[str]:
    """Extract one or two paths from a porcelain v1 status entry."""
    raw = (entry or "").rstrip()
    if not raw or raw.startswith("## "):
        return []
    payload = raw[3:] if len(raw) > 3 else raw
    if " -> " in payload:
        left, right = payload.split(" -> ", 1)
        paths = [left, right]
    else:
        paths = [payload]
    return [p for p in (normalize_repo_path(path) for path in paths) if p]


def is_runtime_path(path: str) -> bool:
    path = normalize_repo_path(path)
    return any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in RUNTIME_PREFIXES)


def is_non_contract_path(path: str) -> bool:
    path = normalize_repo_path(path)
    return any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in NON_CONTRACT_PREFIXES)


def active_bot_version(path: str) -> int | None:
    match = _ACTIVE_BOT_RE.match(normalize_repo_path(path))
    if not match:
        return None
    return parse_bot_version(f"{ACTIVE_BOT_PREFIX}{match.group('version')}")


def is_candidate_bot_path(path: str, candidate_v: int | None) -> bool:
    if candidate_v is None:
        return False
    return normalize_repo_path(path).startswith(bot_relpath(candidate_v) + "/")


def is_foreign_active_bot_path(path: str, candidate_v: int | None) -> bool:
    version = active_bot_version(path)
    return version is not None and (candidate_v is None or version != int(candidate_v))


def is_critical_evolution_path(path: str) -> bool:
    path = normalize_repo_path(path)
    if is_runtime_path(path) or is_non_contract_path(path):
        return False
    return path in CRITICAL_EXACT or any(path.startswith(prefix) for prefix in CRITICAL_PREFIXES)


def classify_path(path: str, candidate_v: int | None) -> str:
    """Classify a repo path for in-place evolution ownership checks."""
    path = normalize_repo_path(path)
    if not path:
        return "empty"
    if is_runtime_path(path):
        return "runtime"
    if is_non_contract_path(path):
        return "external"
    if is_candidate_bot_path(path, candidate_v):
        return "candidate"
    if is_foreign_active_bot_path(path, candidate_v):
        return "foreign_active_bot"
    if is_critical_evolution_path(path):
        return "critical"
    return "external"


def classify_status_entries(entries: list[str] | tuple[str, ...] | None, candidate_v: int | None) -> dict[str, Any]:
    """Classify porcelain status entries into blocking and ignored groups."""
    groups: dict[str, list[str]] = {
        "candidate_entries": [],
        "critical_entries": [],
        "foreign_bot_entries": [],
        "runtime_entries": [],
        "external_entries": [],
        "unknown_entries": [],
    }
    entry_classes: list[dict[str, Any]] = []
    for entry in entries or []:
        paths = status_entry_paths(str(entry))
        if not paths:
            groups["unknown_entries"].append(str(entry))
            continue
        classes = {classify_path(path, candidate_v) for path in paths}
        item = {"entry": str(entry), "paths": paths, "classes": sorted(classes)}
        entry_classes.append(item)
        if "critical" in classes:
            groups["critical_entries"].append(str(entry))
        elif "foreign_active_bot" in classes:
            groups["foreign_bot_entries"].append(str(entry))
        elif "candidate" in classes:
            groups["candidate_entries"].append(str(entry))
        elif "runtime" in classes:
            groups["runtime_entries"].append(str(entry))
        elif "external" in classes:
            groups["external_entries"].append(str(entry))
        else:
            groups["unknown_entries"].append(str(entry))

    blocking_entries = groups["critical_entries"] + groups["foreign_bot_entries"]
    ignored_entries = groups["runtime_entries"] + groups["external_entries"]
    return {
        **groups,
        "entry_classes": entry_classes,
        "blocking_entries": blocking_entries,
        "ignored_entries": ignored_entries,
        "blocking_count": len(blocking_entries),
        "ignored_count": len(ignored_entries),
    }


def classify_paths(paths: list[str] | tuple[str, ...] | set[str], candidate_v: int | None) -> dict[str, Any]:
    entries = [f"?? {normalize_repo_path(path)}" for path in sorted(paths)]
    return classify_status_entries(entries, candidate_v)


def changed_paths_between_heads(root: str | Path, old_head: str, new_head: str) -> list[str] | None:
    """Return changed paths between two git heads, or None if git cannot answer."""
    if not old_head or not new_head or old_head == new_head:
        return []
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only", f"{old_head}..{new_head}"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return [normalize_repo_path(line) for line in (proc.stdout or "").splitlines() if line.strip()]
