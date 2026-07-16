#!/usr/bin/env python3
"""Remove exactly one clean, terminal, owner-marked Worker worktree."""

from __future__ import annotations

import argparse
from pathlib import Path

from worker_mcp.config import load_config
from worker_mcp.persistence import Persistence
from worker_mcp.schemas import TaskStatus
from worker_mcp.state_machine import is_terminal
from worker_mcp.worktree import WorktreeManager


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Safely clean one exact pok-worker-mcp-owned worktree"
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    args = parser.parse_args()
    config = load_config(args.config)
    store = Persistence(config.state_dir / "tasks.sqlite3")
    row = store.get_task(args.task_id)
    status = TaskStatus(row["status"])
    if not is_terminal(status):
        raise SystemExit(f"refusing cleanup for non-terminal task: {status.value}")
    if not row["worktree_path"]:
        raise SystemExit("task has no owned worktree")
    manager = WorktreeManager(config)
    manager.cleanup_owned_clean(
        task_id=args.task_id,
        repository=row["repository"],
        base_commit=row["base_commit"],
        worktree_path=row["worktree_path"],
    )
    print(f"removed clean owner-marked worktree for task {args.task_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
