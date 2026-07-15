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

Use the outer checkout for development and `.evolution_pok` for the running
service; synchronize through Git only. Before edits, follow
`docs/evolution-dual-checkout-sync-policy.md`.

Strict Master/Reviewer/Critic calls never share a flat role log. Each accepted
call binds exactly one generation-scoped `strict_invocations/<invocation_id>`
provider log; historic flat logs and their evidence trailers have zero recovery
or prompt authority. Generation abandonment fences both the Worker and strict
child journals, including an abandoned tombstone before first dispatch. The log
API/frontend use a validated opaque id and never infer authority from a path.

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
