# CLAUDE.md

Read `AGENTS.md` first. It is the authoritative repository map and working
contract.

The only active bot architecture is `national_tcp_policy_v1` over raw national
TCP. Candidate code lives only in `policy.py`, receives an authoritative typed
`decision_context`, and returns typed `fold/pass/allin/raise_to` intent; the
raise intent alone carries an integer `raise_to`. System-owned code alone parses TCP, completes
proven street boundaries, tracks terminal/showdown evidence, validates actions,
handles deadlines, and writes to the socket.

Everything below `archive/` is retired and has zero execution, evaluation, or
prompt-evidence authority. Never add it to an import path or use its ratings,
experience, replays, tests, bots, adapter, engine, RL output, or analyses to
drive an active generation.

Use the outer checkout (`/home/ubuntu/pok1` on the cloud runtime) for
development and `.evolution_pok` for the running service; synchronize through
Git only (on this branch, through `origin/tencent-cloud-runtime`). Before
edits, follow `docs/evolution-dual-checkout-sync-policy.md`.

Strict Master/Reviewer/Critic calls never share a flat role log. Each accepted
call binds exactly one generation-scoped `strict_invocations/<invocation_id>`
provider log; historic flat logs and their evidence trailers have zero recovery
or prompt authority. Generation abandonment fences both the Worker and strict
child journals, including an abandoned tombstone before first dispatch. The log
API/frontend use a validated opaque id and never infer authority from a path.
A terminal strict Master slot canonically abandons only at
`direction_audited`; it must not loop through `run_master` or bypass a later
gate.
Proposal Scouts do not receive the complete final-Master tutorial. Bootstrap
Scouts may read only the target; normal Scouts may read only source, target and
the exact frozen snapshot. They copy a system-verified current ABI-reachable
chain, and the validator rejects dead-helper chains. Bootstrap persists
field-level errors in the strict journal; normal evolution content-binds the
same errors into its sole repair prompt/provenance. Blocked documentation reads
are never prompt or evidence input.
Checkpoint disappearance is terminal only with one unique canonical result
from the current authorized owner tool, including `workflow_run_id`, and a
current-head reproof of both complete journals, the transaction, ledger and
finalize receipt. Its result is bound to exactly one pending route-mutating
ToolUse by explicit id/parent id or the bounded sole-pending SDK form; unknown,
reused, swapped or unsettled ids block. Otherwise recovery blocks. With no active checkpoint the
provider ends its stream; only the outer scheduler owns non-MCP
`prepare_generation`. Exact `selected`/`preparing` routes respectively own
first/crash-recovery `prepare_next_gen`; an unbound target preimage triggers
system canonical abandon. Timeout states are active leases: `timed_out`
abandons, while `infra_timed_out` retries native precommit only after exact
artifact/gate/baseline reproof and CAS. Post-publication handoff makes the
provider end its stream while outer deterministic recovery owns Archivist.
`--one-gen` follows one workflow through as many fresh streams and deterministic
stages as needed and never prepares its successor after abandon.

Primary references:

- `docs/national-tcp-policy-epoch.md`
- `docs/national-runtime-architecture-policy.md`
- `docs/official-certification-policy.md`
- `docs/official-raise-boundary-oracle-2026-07-11.md`
- `docs/official-terminal-settlement-oracle-2026-07-11.md`

Typical verification:

```bash
python -m pytest sever/tests -q
cd web && python -m pytest tests -q
python scripts/official_certify.py doctor
```

Do not treat the Arena or official EXE chip result as strength evidence. Local
Glicko/H2H strength uses complete 70-hand raw native TCP matches only.
For the first strict candidate (`national_cloud_v1` on this branch), a green
doctor and valid unused `first_strict_control_v1` at artifact hash
`b37cd019fe6b635a119950adb5f7ecf10ddceeafacfbed6b4c3a0955064516e2` prove the
official 5+3 dependency is present. Only the checkpoint stage
`official_bootstrap_required` unlocks the operator action.

## Documentation synchronization rule

Any functional change — a feature added, removed, or modified — must update the
relevant documentation **in the same change**: `AGENTS.md` and `CLAUDE.md` for
cross-cutting contracts, `docs/` for architecture/policy/oracle detail, and
`deploy/` for operational deployment. Version numbers, namespace identifiers,
paths, LLM/signer configuration, command examples, file lists, and
ABI/protocol/gate/lifecycle descriptions must all stay consistent with the code
they describe. Do not leave a green result, a working feature, or a deployed
change with stale documentation. When in doubt, update.
