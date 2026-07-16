# Pok Worker MCP

`pok-worker-mcp` is an external Codex control-plane helper for desktop/CLI
tasks. Codex keeps planning,
risk decisions, diff review, final tests, commits, merges, and publication. The
service exposes exactly six MCP tools and runs each bounded task through
Claude Agent SDK in a detached, owner-marked Git worktree.

It is not a poker-evolution Worker. `web/core`, Orchestrator, WorkerWorkflow,
the web launcher, rating/evolution daemons, candidate generation, and
`.evolution_pok` must never import, start, supervise, call, or record it. It
does not share checkpoints or evidence and is inert until an operator performs
a separate manual install and Codex MCP registration. Merging source alone
does not enable it, restart evolution, or rotate evaluation identity.

## Architecture

```text
Codex sessions
  -> authenticated loopback Streamable HTTP MCP
  -> one operator-managed Worker daemon
  -> submit/get_status/get_result/cancel/list/healthcheck
  -> SQLite-WAL queue + state history + idempotency + locks
  -> detached Git worktree + path/tool policy
  -> sanitized, parent-fenced child process + Claude Agent SDK
  -> local CC Switch logical endpoint
```

CC Switch routing identity is intentionally absent from MCP inputs and normal
results. The compatibility adapter only knows a loopback logical endpoint.

## Install

On Linux, install the Claude Code sandbox prerequisites first. Worker execution
fails closed unless both executables are available; it never accepts an
unsandboxed fallback:

```bash
sudo apt-get install bubblewrap socat
```

```bash
cd /home/zzx/project/pok/worker-mcp
python -m venv .venv
.venv/bin/pip install -e '.[test]'
cp config.example.yaml config.yaml
```

Review `allowed_repositories`, forbidden paths, pinned component versions, and
runtime paths in `config.yaml`. Export a dedicated credential without placing it
in Git or Codex configuration:

```bash
export WORKER_MCP_ANTHROPIC_AUTH_TOKEN='...'
export WORKER_MCP_ACCESS_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
```

The service does not load user/project Claude settings, MCP servers, plugins,
skills, hooks, fallback models, Agent teams, Bash, or Web tools. Existing
Claude settings are therefore not a credential source; the dedicated
environment variable is required by default. Repository commands and tests run
only in the final Codex review, outside the credential-bearing Agent process.

## Start and diagnose

```bash
.venv/bin/python -m worker_mcp.server --config /absolute/path/config.yaml
.venv/bin/python scripts/diagnose.py --config /absolute/path/config.yaml
.venv/bin/python scripts/diagnose.py --config /absolute/path/config.yaml --deep
```

`--deep` performs a real text + successful Read + structured-output canary. It
may invoke the configured logical backend. A shallow healthcheck never sends a
model request. For concurrent Codex sessions, set `server.transport` to
`streamable-http` and keep one operator-managed process running. The state
directory has one live service owner; all Codex sessions share it over the
loopback endpoint. Stop that daemon before running `diagnose.py`, or call its
public shallow `healthcheck` tool instead. `--transport stdio` remains an
explicit one-shot compatibility and smoke-test override.

## Codex MCP configuration

Add this manually to project `.codex/config.toml` (trusted repository) or the
user-level `~/.codex/config.toml`:

```toml
[mcp_servers.pok_worker]
url = "http://127.0.0.1:8765/mcp"
bearer_token_env_var = "WORKER_MCP_ACCESS_TOKEN"
required = true
startup_timeout_sec = 30
tool_timeout_sec = 30
enabled_tools = ["submit", "get_status", "get_result", "cancel", "list", "healthcheck"]
default_tools_approval_mode = "prompt"

[mcp_servers.pok_worker.tools.get_status]
approval_mode = "auto"

[mcp_servers.pok_worker.tools.get_result]
approval_mode = "auto"

[mcp_servers.pok_worker.tools.list]
approval_mode = "auto"

[mcp_servers.pok_worker.tools.healthcheck]
approval_mode = "auto"
```

Restart Codex after adding the server. Long work happens after `submit`; MCP
calls themselves stay short. The same `WORKER_MCP_ACCESS_TOKEN` must be injected
into the daemon and the Codex app-server environment by the operator's OS
credential launcher. Do not put either the local access token or the model
credential in TOML, YAML, scripts, task data, SQLite, or logs.

## Example submit

```json
{
  "goal": "Review the named workflow modules and report contract risks",
  "context": "Read-only architecture inventory; Codex will verify citations",
  "repo": "/home/zzx/project/pok",
  "base_commit": "FULL_COMMIT_SHA",
  "allowed_paths": ["web/core/workflow_kernel.py", "web/core/worker_workflow.py"],
  "forbidden_paths": ["archive", ".evolution_pok"],
  "constraints": ["Do not modify files", "Cite exact symbols"],
  "acceptance_criteria": ["Return schema-valid findings and unresolved risks"],
  "execution": {
    "read_only": true,
    "use_worktree": true,
    "max_turns": 12,
    "timeout_sec": 900
  },
  "idempotency_key": "worker-workflow-audit-v1",
  "task_type": "analyze"
}
```

Then call `get_status`, followed by `get_result`. A read task needs successful
Read evidence; a write task needs a bounded, independently measured Git diff.
Model-reported findings and checks remain advisory. Codex must inspect the
actual worktree/diff and run final tests before accepting any write result.

## Tests

```bash
PYTHONPATH=src python -m pytest -q
python -m compileall -q src tests scripts
```

Mock tests cover gateway 503/bad responses without changing CC Switch. The real
smoke script launches its own service, forwards only the configured credential,
requires an overall healthy shallow check, and uses a bounded poll deadline:

```bash
.venv/bin/python scripts/smoke_test.py --config config.yaml \
  --repo /home/zzx/project/pok --allowed-path web/core
```

Run the explicit real-model canary separately with
`.venv/bin/python scripts/diagnose.py --config config.yaml --deep`.

See `docs/` for security, operations, recovery, and upgrade procedures.
