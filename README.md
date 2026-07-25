# Poker Bot Evolution — National TCP Policy

This repository evolves heads-up No-Limit Texas Hold'em bots for the national
competition platform. The sole active protocol is the raw national TCP
protocol. The active epoch is `national_tcp_policy_v1`.

## Active architecture

- `sever/` implements the national rules engine, validator, TCP server, THP
  recorder, and diagnostic dashboard.
- `web/` implements the evolution control plane, native TCP evaluation,
  evidence snapshots, quality gates, official certification, and dashboard.
- `bots/national_v<N>/` contains active strict-policy candidates after they are
  prepared. A candidate owns only `policy.py`; the
  system-owned runtime owns TCP, protocol state, deadlines, action
  sanitization, and the only socket send path.
- `scripts/official_certify.py` runs content-bound official Windows EXE
  certification.
- `archive/` contains retired engines, adapters, bots, experiments, tests, and
  evidence. It is historical storage, never an active import or evidence root.

The former subprocess JSON engine, TCP adapter, RL experiments, mixed-ABI
national bots, and their analyses have been retired. Active code must not add
`archive/` to `PYTHONPATH`, import it dynamically, copy a retired bot into the
active pool, or inject retired ratings/experience into prompts.

Version numbering preserves only the annotated completion-tag high-water.
`national-cloud-bot-v0` is the retired numeric high-water, so the first strict
target is `national_cloud_v1`; an untagged old-wrapper directory such as
`national_cloud_v13` is stale debris and cannot advance the version, join the pool,
or provide source/evidence bytes.

## Candidate boundary

`policy.py` receives a versioned authoritative `decision_context` and returns a
typed intent: `pass`, `fold`, `allin`, or `raise` with integer `raise_to`.
Candidate code never parses the socket stream, reconstructs requests/responses,
returns an integer action, or decides whether `pass` becomes wire `call` or
`check`.

The system runtime:

- sends and splits raw TCP tokens with no `\n`/`\r\n`, without treating recv
  boundaries as message boundaries;
- is exercised locally against the same omitted street-closing call/check and
  hand-70 settlement wire boundaries observed from the official EXE;
- completes only street-closing actions proven by a new-street or settlement
  boundary;
- records terminal fold/call and showdown information in the connection-lived
  opponent tracker;
- maintains authoritative pot, contributions, stacks, SPR, pot odds, and legal
  raise-to bounds;
- computes an always-legal fallback, targets a 250 ms policy baseline, permits
  bounded refinement until 54 seconds, and returns by a 55 second hard
  deadline before the official 60 second timeout;
- owns the official-safe action delay and emits exactly one legal wire action.

See [National TCP Policy Epoch](docs/national-tcp-policy-epoch.md) and
[Runtime Architecture Policy](docs/national-runtime-architecture-policy.md).

## Quick start

```bash
# Web/evolution app
python web/main.py
python web/main.py --view-only

# Tests
python -m pytest sever/tests -q
cd web && python -m pytest tests -q

# Local national TCP platform
cd sever && python main.py

# One native diagnostic session (never strength/certification authority)
python scripts/national_arena.py run --mode managed \
  --top-bot national_v<N> --bottom-bot national_v<M> --hands 70 --wait

# Official protocol acceptance oracle
python scripts/official_platform_acceptance.py \
  --candidate bots/national_v<N> --opponent bots/national_v<M> \
  --self-play-rounds 1 --opponent-rounds 1 --target-hands 70

# Required signed full certification before commit/tag
python scripts/official_certify.py full bots/national_v<N> --wait-if-busy

# One-time first strict publication, only while the active pool is empty
python scripts/official_certify.py bootstrap-first-strict bots/national_cloud_v1 \
  --control-id first_strict_control_v1 \
  --acknowledge-one-time-first-strict-control --wait-if-busy

# Post-sync operator check: real Agent SDK, 3 Read + 2 exact Bash calls.
# It prints one JSON receipt and never exposes write/network/MCP tools.
python scripts/claude_sdk_operator_probe.py --timeout-seconds 300 --pretty
```

Local strength evidence is one complete 70-hand native TCP match. The sign of
final net chips determines win/loss/draw; magnitude is only a secondary
tie-breaker. Arena and official EXE chip results have zero rating weight.

## Official protocol anchors

The exact official-EXE findings in these files are evaluation-critical and
SHA-256 pinned:

- `docs/official-raise-boundary-oracle-2026-07-11.md`
- `docs/official-terminal-settlement-oracle-2026-07-11.md`

Exact `raise 400` after `raise 200` is legal. The EXE may omit a
street-closing peer call/check and may omit the final hand-70 `earnChips` pair;
the runtime and formal certificate follow the oracle contracts rather than
inventing messages.

## Two checkouts

Use `/home/zzx/project/pok` for infrastructure work and
`/home/zzx/project/pok/.evolution_pok` only for the running evolution service.
Synchronize them through `origin/main`; never copy files between them. See
[Dual-checkout sync policy](docs/evolution-dual-checkout-sync-policy.md).
