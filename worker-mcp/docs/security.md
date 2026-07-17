# Security

This security model covers a manually enabled Codex desktop/CLI helper. It has
no authority in poker evolution and must never enter its runtime, checkpoints,
prompts, evidence, or process supervision.

## Enforced controls

Prompts are advisory. Enforcement also uses MCP schemas, repository allowlists,
non-removable forbidden paths, detached worktrees, owner markers, normalized
path checks, symlink containment, tool allow/deny lists, `can_use_tool`, hooks,
a sanitized child environment, SDK sandboxing, process timeouts, SQLite
idempotency, a state-directory owner lock, leases, and bounded concurrency.

The Agent SDK cannot currently give Bash a different environment from the
credential-bearing CLI. Bash is therefore absent from `tools` and
`allowed_tools` and explicitly disallowed; pytest, npm scripts, and other
repository code never run in the Agent child. A small command grammar remains
tested as dormant defense-in-depth, but no MCP task can reach it. Codex runs
tests after reviewing the returned worktree.

Recursive Glob/Grep tools are also disabled because their result paths cannot
be post-filtered before sensitive descendants are read. Codex supplies bounded
file paths; the Worker uses explicit `Read`, plus `Edit`/`Write` only for a
write-capable task.

Linux execution requires both `bwrap` and `socat`. Health marks a missing
dependency unhealthy, and the executor fails before spawning the Agent child
rather than accepting Claude Code's unsandboxed fallback.

Every real execution also rechecks the exact installed Agent SDK and Claude
Code versions against configuration; health is not a bypassable version gate.
The child interpreter must resolve to the same executable as the MCP server, so
an alternate venv cannot bypass the verified SDK/package boundary.
The production configuration accepts only `backend: claude_sdk`; the
deterministic Mock executor is injectable solely by the unshipped test
entrypoint and cannot produce a configured production success.
The CC Switch endpoint exposes no version evidence, so its configured version
remains explicitly unverified and is operationally covered by the loopback
health contract plus the explicit deep canary, not misreported as enforced.

Mandatory forbidden scopes for this repository include `archive`,
`docs/archive`, `.evolution_pok`, `.codex_worktrees`, `.claude`, `.git`, and
`.env`; local configuration can add but cannot remove them. State/worktree
roots must be outside allowed repositories and can never be placed inside the
autonomous runtime checkout.
Recursive Grep/Glob calls are rejected when their root could cross into a
forbidden descendant.

## Credentials and logs

Production should inject only `WORKER_MCP_ANTHROPIC_AUTH_TOKEN` as the model
credential and a separate random `WORKER_MCP_ACCESS_TOKEN` as the local HTTP
bearer credential. The server binds exactly `127.0.0.1`, validates the exact
Host and any supplied Origin, and compares bearer tokens in constant time.
Missing, short, malformed, or incorrect credentials fail closed. The SDK child
receives only the model credential under the protocol variable inside a
dedicated HOME and isolated import environment; it does not inherit the
configured variable name or ambient Claude credentials. The task envelope,
SQLite request, MCP result, and normal logs do not contain the credential.
Result, diff, failure, command, and audit exits use explicit-token plus
shape-based redaction as defense-in-depth.

`auth_token_env` must use the dedicated `WORKER_MCP_*` namespace. Ambient names
such as `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`, cloud credentials, PATH, or
HOME are rejected rather than reclassified as Worker credentials.

Do not place tokens in `config.yaml`, prompts, task context, allowed commands,
Codex TOML, service units, SQLite, scripts, or logs. Use an OS credential
launcher for both the daemon and Codex app-server, and configure Codex with
`bearer_token_env_var`. The HTTP access token grants only loopback MCP access.
Configuration rejects a shared environment name, startup rejects equal
credential values, and the daemon validates both the access token and listening
socket before constructing the task service or recovering durable tasks. The
access token is also a task-envelope rejection and redaction secret, so it
cannot be persisted by a new submission or forwarded to the model. Recovery
revalidates every incomplete durable envelope against the current scope and
secret set before enqueue; a legacy violation is terminally quarantined for
operator review without execution. Quarantine does not erase a historical
request row, so an access token already present in legacy state must still be
rotated and that state handled as credential-bearing material.

## Remaining trust

The service runs under the operator account, not a dedicated OS user. The
Claude Code Linux sandbox and worktree process boundary reduce impact, but a
kernel/CLI/sandbox escape remains outside this code's control. For higher
assurance, run the MCP service under a dedicated unprivileged account or
container with only the repository and state roots mounted.

Claude Agent SDK routed through CC Switch to a non-Claude backend is a
compatibility path, not an Anthropic-supported model contract. The adapter is
isolated in `compatibility.py`; enforced pins and the explicit unverified
CC Switch boundary must pass the contract checklist before upgrades.
