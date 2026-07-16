# Architecture

## Boundary

Codex is the only commander and final reviewer. The MCP server is a task
control plane, not a planner. Claude Agent SDK is the execution plane. CC
Switch is a transparent loopback routing layer. No normal MCP request or result
contains a model, provider, channel, or upstream credential.

This subproject never imports `web/core`. The poker evolution Worker journal is
checkpoint- and candidate-specific; reusing it for general repository work
would couple unrelated recovery, identity, and publication contracts.

## State and execution

Every submit first persists `accepted`, then records `queued`. An atomic SQLite
claim advances one task to `preparing`. Worktree creation is idempotent and
bound to repository, base commit, task ID, configured root, and a marker in the
worktree-specific Git metadata directory. Execution advances through `running`
and `verifying`; only independently measured Git/tool evidence can advance to
`succeeded`.

Terminal exceptions are `failed`, `cancelled`, `timed_out`, and
`needs_review`. A dirty interrupted/cancelled/timed-out worktree always becomes
`needs_review`. Read-only process/gateway failures may retry once. Write tasks
never restart automatically.

SQLite uses WAL, `synchronous=FULL`, foreign keys, and `BEGIN IMMEDIATE` for
idempotency and claims. State transitions include timestamp, previous/next
state, phase, and reason.

## Isolation

The SDK runs inside a separately spawned Python child with a minimal
environment. It starts Claude Code with:

- explicit tools and allowed tools;
- `can_use_tool` and `PreToolUse` enforcement;
- path normalization and symlink-aware containment;
- a small parsed Bash grammar;
- empty MCP, setting sources, plugins, skills, and agent definitions;
- Web/Agent/Skill denial and no fallback model;
- Linux command sandbox with unsandboxed commands disabled;
- JSON Schema structured output.

The parent terminates the entire child process group on cancel or timeout.

## Concurrency

Separate global and per-repository semaphores apply to read and write tasks.
Writes also acquire stable path locks and a subprocess semaphore. The default
per-repository write concurrency is one, so overlapping edits serialize.
