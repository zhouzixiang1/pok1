# Architecture

## Boundary

Codex is the only commander and final reviewer. This is a Codex desktop/CLI
control-plane helper; the MCP server is a task control plane, not a planner.
Claude Agent SDK is the execution plane. CC
Switch is a transparent loopback routing layer. No normal MCP request or result
contains a model, provider, channel, or upstream credential.

This subproject never imports `web/core`, and the poker runtime must never
import or launch this subproject. The poker evolution Worker journal is
checkpoint- and candidate-specific; reusing it for general repository work
would couple unrelated recovery, identity, and publication contracts. Source
delivery and manual Codex MCP enablement are separate operations.

## State and execution

Every submit first persists `accepted`, then records `queued`. The exact
execution-relevant envelope, including `context`, is fingerprinted for strict
idempotency. An atomic SQLite claim advances one task to `preparing`. Worktree creation is idempotent and
bound to repository, base commit, task ID, configured root, and a marker in the
worktree-specific Git metadata directory. Execution advances through `running`
and `verifying`; only independently measured Git/tool evidence can advance to
`succeeded`.

Terminal exceptions are `failed`, `cancelled`, `timed_out`, and
`needs_review`. A dirty interrupted/cancelled/timed-out worktree always becomes
`needs_review`. Read-only process/gateway failures may retry once. Write tasks
never restart automatically.

SQLite uses WAL, `synchronous=FULL`, foreign keys, and `BEGIN IMMEDIATE` for
idempotency, claims, and the cancel-versus-claim decision. One fail-closed flock
owns a state directory for the service lifetime, so a diagnostic or second
second daemon cannot recover a live lease. Multiple Codex clients connect to one
authenticated loopback Streamable HTTP process, so service recovery and bounded
concurrency remain process-global. State transitions include timestamp,
previous/next state, phase, and reason.

## Isolation

The SDK runs inside a separately spawned Python child with an isolated HOME,
minimal environment, bounded stdout/stderr, isolated Python import mode, and a
parent-death process-group fence. It starts Claude Code with:

- explicit tools and allowed tools;
- `can_use_tool` and `PreToolUse` enforcement;
- path normalization and symlink-aware containment;
- no Bash, recursive Glob/Grep, or repository-code execution (the parsed
  command grammar remains a dormant defense and is not exposed as a tool);
- empty MCP, setting sources, plugins, skills, and agent definitions;
- Web/Agent/Skill denial and no fallback model;
- Linux command sandbox with unsandboxed commands disabled;
- JSON Schema structured output.

The parent terminates the entire child process group on cancel or timeout.
Linux execution also requires `bwrap` and `socat`; missing dependencies fail
before the credential-bearing child starts.

Git status includes tracked, untracked, and ignored residue. Changed-file,
per-file, diff, child stdout, and child stderr budgets are hard-bounded. Any
incomplete worktree evidence is preserved for review and cannot become a
successful result.

## Concurrency

Separate global and per-repository semaphores apply to read and write tasks.
Every write also acquires one stable repository-wide flock and a subprocess
semaphore. Per-repository write concurrency is fixed at one, so ancestor and
descendant scopes such as `web` and `web/core` cannot overlap.

## Evidence semantics

Successful Read hooks and Git/worktree measurements are control-plane
evidence. Model summaries, findings, acceptance text, checks, risks, and
artifact descriptions are reported claims, not final quality evidence. A
read-only task without a successful Read fails; a write task with ignored or
over-budget residue becomes `needs_review`. Codex still reruns all final checks.
