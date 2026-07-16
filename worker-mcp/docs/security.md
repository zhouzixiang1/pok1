# Security

## Enforced controls

Prompts are advisory. Enforcement also uses MCP schemas, repository allowlists,
mandatory forbidden paths, detached worktrees, owner markers, normalized path
checks, symlink containment, tool allow/deny lists, `can_use_tool`, hooks, a
parsed command allowlist, sanitized child environment, SDK sandboxing, process
timeouts, SQLite idempotency, leases, and bounded concurrency.

The command grammar permits only diagnostic Git commands, explicit-path
`python -m pytest`, explicit-path `python -m compileall`, and
`npm --prefix PATH run test|build|lint`. It rejects shell operators,
substitutions, wrappers, network tools, arbitrary Python, destructive Git, and
commands outside the grammar.

Mandatory forbidden scopes for this repository include `archive`,
`docs/archive`, `.evolution_pok`, existing agent worktrees, `.git`, and `.env`.
Recursive Grep/Glob calls are rejected when their root could cross into a
forbidden descendant.

## Credentials and logs

Production should forward only `WORKER_MCP_ANTHROPIC_AUTH_TOKEN` to the STDIO
server. The SDK child receives the token value under the protocol variable, but
the task envelope, SQLite request, MCP result, and normal logs do not. Recursive
redaction covers authorization, token, key, secret, password, and private-key
fields plus common bearer/token strings.

Do not place tokens in `config.yaml`, prompts, task context, allowed commands,
or Codex static `env` maps. Use Codex `env_vars` forwarding.

## Remaining trust

The service runs under the operator account, not a dedicated OS user. The
Claude Code Linux sandbox and worktree process boundary reduce impact, but a
kernel/CLI/sandbox escape remains outside this code's control. For higher
assurance, run the MCP service under a dedicated unprivileged account or
container with only the repository and state roots mounted.

Claude Agent SDK routed through CC Switch to a non-Claude backend is a
compatibility path, not an Anthropic-supported model contract. The adapter is
isolated in `compatibility.py`; component versions are pinned and must pass the
contract checklist before upgrades.
