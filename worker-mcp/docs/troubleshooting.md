# Troubleshooting

## Server does not appear in Codex

Run the server command manually, then `scripts/smoke_test.py`. Check the
absolute Python, config, and cwd paths. Codex requires a restart after MCP
configuration changes. With `required=true`, startup intentionally fails closed
when the server cannot initialize.

## Health is unhealthy

- `database` or `state_directory`: verify ownership and mode of `state_dir`.
- `git`: verify Git is on the service PATH.
- `claude_agent_sdk` or `claude_code`: installed versions must match pins.
- `cc_switch_endpoint`: verify the existing local process is listening on the
  configured loopback endpoint and `/health` returns valid JSON.
- `worker_credential`: export the dedicated environment variable before Codex
  starts, then restart Codex so `env_vars` can forward it.
- canary failures: run shallow diagnosis first; deep diagnosis invokes the
  logical backend and can fail independently of basic health.

Do not probe CC Switch with `cc-switch --version` on Linux; version 3.17.0 is a
GUI executable and a headless second instance can crash. Pin the installed
version in config and use its supported UI plus `/health` for diagnostics.

## Task is stuck or interrupted

`get_status` reports coarse phases, not invented percentages. Use `cancel` once.
The child process group is terminated; a dirty worktree is preserved as
`needs_review`. Restarting the MCP server recovers durable rows using the rules
in `operations.md`.

## Cleanup refuses

This is expected when a diff exists or ownership evidence is incomplete. Never
delete or prune broadly. Inspect the task result and exact worktree. Make it
clean only after Codex has preserved or rejected the patch, then rerun cleanup
with the same task ID.

## Gateway 5xx, timeout, or stream failure

CC Switch owns request-level same-route retry, failover, circuit breaking, and
recovery probes. Worker MCP owns SDK process/IPC state and permits one safe
read-only task retry. Write tasks do not restart. After all routes fail, the
task returns an explicit failure; it does not create a second worktree.
