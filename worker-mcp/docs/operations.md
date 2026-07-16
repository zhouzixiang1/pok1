# Operations

## Lifecycle

Install the package in its own virtual environment, copy and review the example
configuration, export the dedicated credential, run shallow and deep diagnosis,
then add the STDIO server to Codex manually. The poker web launcher and
evolution orchestrator must not start or supervise this service.

Tasks and transitions live in `<state_dir>/tasks.sqlite3`; JSONL audit records
live in `<state_dir>/logs/worker-mcp.jsonl`; task worktrees live under the
configured `worktree_root`. SQLite and log files may contain repository code
paths and task text, so the state directory should remain mode 0700 and should
not be published.

## Recovery

At startup:

- `accepted` tasks become `queued`;
- queued tasks are dispatched once;
- clean read-only interruptions may use their one retry;
- write interruptions never replay;
- any interrupted task with a diff becomes `needs_review`.

Codex should inspect `get_result.diff` and `worktree_path`. The service never
commits. After Codex has accepted or rejected a task and the worktree is clean,
remove exactly that task's worktree:

```bash
.venv/bin/python scripts/cleanup_worktrees.py \
  --config /absolute/path/config.yaml --task-id TASK_UUID
```

The cleanup command has no broad mode. It refuses non-terminal, dirty,
out-of-root, missing-marker, mismatched-task, and mismatched-repository targets.
It never scans branch prefixes or calls broad `git worktree prune`.

## Upgrade checklist

Before upgrading Claude Agent SDK, Claude Code, MCP SDK, or CC Switch:

1. Update one component and its pinned version only.
2. Run all unit, integration, and mock E2E tests.
3. Verify six-tool discovery and both input/output schemas.
4. Verify `setting_sources=[]`, strict empty MCP, no plugins/skills/agents/Web,
   `can_use_tool`, hooks, JSON Schema, and sandbox CLI serialization.
5. Run mock 503, timeout, bad JSON, bad structured output, cancel, restart, and
   dirty-write recovery tests.
6. Run the deep real canary: text, Read, multi-turn tool loop, Bash, Edit in a
   disposable repository, and structured output.
7. Confirm the primary checkout is unchanged and no duplicate worktree exists.
8. Confirm normal MCP results contain no routing identity or credential.
9. Roll back the version if any contract changes; do not patch model-specific
   behavior into task-service logic.
