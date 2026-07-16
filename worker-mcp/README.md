# Pok Worker MCP

`pok-worker-mcp` is an external Codex execution service. Codex keeps planning,
risk decisions, diff review, final tests, commits, merges, and publication. The
service exposes exactly six STDIO MCP tools and runs each bounded task through
Claude Agent SDK in a detached, owner-marked Git worktree.

It is deliberately separate from `web/core`: it is not imported by the poker
evolution runtime, does not share its SQLite journal, does not read
`.evolution_pok`, and is inert until explicitly installed in Codex.

## Architecture

```text
Codex Commander
  -> STDIO MCP: submit/get_status/get_result/cancel/list/healthcheck
  -> SQLite-WAL queue + state history + idempotency + locks
  -> detached Git worktree + path/command policy
  -> sanitized child process + Claude Agent SDK
  -> local CC Switch logical endpoint
```

CC Switch routing identity is intentionally absent from MCP inputs and normal
results. The compatibility adapter only knows a loopback logical endpoint.

## Install

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
```

The service does not load user/project Claude settings, MCP servers, plugins,
skills, hooks, fallback models, Agent teams, or Web tools. Existing Claude
settings are therefore not a credential source; the dedicated environment
variable is required by default.

## Start and diagnose

```bash
.venv/bin/python -m worker_mcp.server --config /absolute/path/config.yaml
.venv/bin/python scripts/diagnose.py --config /absolute/path/config.yaml
.venv/bin/python scripts/diagnose.py --config /absolute/path/config.yaml --deep
```

`--deep` performs a real text + Read tool + structured-output canary. It may
invoke the configured logical backend. A shallow healthcheck never sends a
model request.

## Codex MCP configuration

Add this manually to project `.codex/config.toml` (trusted repository) or the
user-level `~/.codex/config.toml`:

```toml
[mcp_servers.pok_worker]
command = "/home/zzx/project/pok/worker-mcp/.venv/bin/python"
args = ["-m", "worker_mcp.server", "--config", "/home/zzx/project/pok/worker-mcp/config.yaml"]
cwd = "/home/zzx/project/pok/worker-mcp"
env_vars = ["WORKER_MCP_ANTHROPIC_AUTH_TOKEN"]
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
calls themselves stay short.

## Example submit

```json
{
  "goal": "Find every call site of WorkerWorkflow and report contract risks",
  "context": "Read-only architecture inventory; Codex will verify citations",
  "repo": "/home/zzx/project/pok",
  "base_commit": "FULL_COMMIT_SHA",
  "allowed_paths": ["web/core", "web/tests"],
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

Then call `get_status`, followed by `get_result`. Codex must inspect the actual
diff and run final tests before accepting any write result.

## Tests

```bash
PYTHONPATH=src python -m pytest -q
python -m compileall -q src tests scripts
```

Mock tests cover gateway 503/bad responses without changing CC Switch. The real
smoke script is:

```bash
.venv/bin/python scripts/smoke_test.py --config config.yaml --deep \
  --repo /home/zzx/project/pok --allowed-path web/core
```

See `docs/` for security, operations, recovery, and upgrade procedures.
