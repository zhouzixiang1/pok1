# Operations

## Lifecycle

Source may be merged while remaining inert. As a separate manual Codex-side
operation, install the package in its own virtual environment, copy and review
the example configuration, inject the dedicated credentials, run shallow and
deep diagnosis, then add the loopback Streamable HTTP endpoint to Codex
manually. The poker web
launcher, evolution orchestrator/WorkerWorkflow, daemons, candidate flow, and
autonomous checkout must not start, supervise, call, or record this service.
Its installation is never a reason to restart evolution or rotate evaluation
identity.

Tasks and transitions live in `<state_dir>/tasks.sqlite3`; JSONL audit records
live in `<state_dir>/logs/worker-mcp.jsonl`; task worktrees live under the
configured `worktree_root`. SQLite and log files may contain repository code
paths and task text, so the state directory should remain mode 0700 and should
not be published. Startup tightens the state/worktree/log directories to 0700
and the SQLite/audit files to 0600; an ownership or permission failure aborts.

Exactly one live daemon may own a state directory. Any number of authenticated
Codex clients share that daemon; they do not each start a Worker service. A
second daemon, `diagnose.py`, or smoke process using the same state fails closed
instead of recovering live rows. Use the MCP `healthcheck` tool while the
daemon is running; stop it before starting a standalone diagnostic or smoke
server.

## Multi-session service

Set the following configuration and start exactly one user-level service:

```yaml
server:
  transport: streamable-http
  host: 127.0.0.1
  port: 8765
  path: /mcp
  access_token_env: WORKER_MCP_ACCESS_TOKEN
```

The service manager should execute only:

```bash
python -m worker_mcp.server --config /absolute/path/config.yaml
```

Use `Restart=on-failure`, a private umask, and an OS credential launcher that
injects `WORKER_MCP_ACCESS_TOKEN` and the configured gateway credential. Do not
put credentials in the unit, repository, YAML, or command line. Bind failures,
missing/short access tokens, a second state owner, and non-loopback config all
fail before serving tools. Stop the service to roll back, restore the previous
Codex MCP stanza, and restart Codex; durable task rows remain in the same state
directory.

## Recovery

At startup:

- `accepted` tasks become `queued`;
- queued tasks are dispatched once;
- clean read-only interruptions may use their one retry;
- write interruptions never replay;
- any interrupted task with a diff becomes `needs_review`.

The service child installs a parent-death process-group guard, so a hard server
exit cannot leave the credential-bearing SDK/CLI tree running while a new
owner recovers the journal.

Codex should inspect `get_result.diff` and `worktree_path`. The service never
commits. After Codex has accepted or rejected a task and the worktree is clean,
remove exactly that task's worktree:

```bash
.venv/bin/python scripts/cleanup_worktrees.py \
  --config /absolute/path/config.yaml --task-id TASK_UUID
```

The cleanup command has no broad mode. It cross-checks the durable repository,
base commit, task ID, canonical `path_for(...)`, owner marker, current HEAD, and
configured root. It refuses non-terminal, tracked, untracked or ignored dirty,
over-budget, missing-marker, or mismatched targets. It never scans branch
prefixes or calls broad `git worktree prune`.

## Upgrade checklist

Before upgrading Claude Agent SDK, Claude Code, MCP SDK, or CC Switch:

1. Update one component and its pinned version only.
2. Run all unit, integration, and mock E2E tests.
3. Verify six-tool discovery and both input/output schemas.
4. Verify `setting_sources=[]`, strict empty MCP, no plugins/skills/agents/Web,
   `can_use_tool`, hooks, JSON Schema, and sandbox CLI serialization.
5. Run mock 503, timeout, bad JSON, bad structured output, cancel, restart, and
   dirty-write recovery tests.
6. Run the deep real canary: text, successful Read, multi-turn control loop,
   and structured output. Bash is intentionally unavailable.
7. Confirm the primary checkout is unchanged and no duplicate worktree exists.
8. Confirm normal MCP results contain no routing identity or credential.
9. Submit a separate disposable write task to exercise Edit/diff verification;
   run its tests from Codex after the Agent exits.
10. Roll back the version if any contract changes; do not patch model-specific
   behavior into task-service logic.

For supply-chain evidence, build twice from the same committed tree with the
same pinned build frontend and `SOURCE_DATE_EPOCH`, then compare SHA-256. A hash
from an ordinary timestamped wheel build proves only that one artifact built;
it is not a reproducible commit identity.
