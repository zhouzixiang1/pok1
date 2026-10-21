# Welcome to Pok AI

This checkout develops the `national_tcp_policy_v1` heads-up poker evolution
system for the Tencent Cloud runtime. Read `AGENTS.md` first; it is the
repository-wide working contract.

## Start here

- `/home/ubuntu/pok1` is the operator/infrastructure checkout. Develop code,
  tests, prompts, documentation, and frontend changes here or in an ignored
  temporary worktree.
- Treat `/home/ubuntu/pok1/.evolution_pok` as the autonomous runtime checkout
  (`pok-evolution.service`). Candidate directories, checkpoints, ratings, and
  live result files belong there.
- Synchronize the two checkouts only through `origin/tencent-cloud-runtime`.
  Never copy files between them.
- The sole active bot protocol is delimiter-free raw national TCP. The platform
  does not promise that one `recv` call contains one message, and clients never
  append `\n` or `\r\n` to actions.
- Active candidates use the exact five-file typed-policy ABI. A Worker may edit
  only `policy.py`; the system owns the TCP runtime, precompute, manifests,
  authoritative state, legality, deadlines, and socket send.
- Everything below `archive/` and `docs/archive/` is historical,
  `legacy-untrusted`, and has zero execution, prompt, rating, parent, opponent,
  gate, or certification authority.

## Version and evidence authority

This cloud line uses the `national_cloud_v` namespace and restarts version
numbering at 1 (`ARCHIVED_VERSION_HIGH_WATER = 0`,
`FIRST_STRICT_POLICY_VERSION = 1`). The active high-water is parsed from
annotated `national-cloud-bot-v*` / `national-cloud-high-water-v*` tags, never
from an untagged directory.

Strategy strength comes only from complete 70-hand local native TCP matches in
the current evaluation cycle. Native TCP quality plus the precommit gates are
the sole correctness and publication authority. The official EXE certification
system has been removed: there is no `official_certify.py`, no signed full
certificate, and no certified tag namespace. The Web Arena and
`official_platform_acceptance.py` are diagnostic only, with zero strength
weight.

## First verification

```bash
export PYTHON=/home/ubuntu/pok1/.venv/bin/python
"$PYTHON" -m pytest sever/tests -q
(cd web && "$PYTHON" -m pytest tests -q)
(cd web/frontend && PYTHON="$PYTHON" npm test && npm run lint && npm run build)
```

## Runtime operations

```bash
sudo systemctl status pok-evolution
sudo systemctl restart pok-evolution
journalctl -u pok-evolution -f
curl -s http://127.0.0.1:8000/api/control/status
```

The operator controls mutations through the shared `POK_CONTROL_TOKEN` /
`X-Control-Token` contract when same-origin loopback access is unavailable.
There is no generic HTTP tool executor. Disk hygiene, publication rotation, and
the events auto-compression gate are documented in
`deploy/tencent-cloud/README.md` and
`docs/events-auto-compression-2026-10-21.md`.

## Current references

- `AGENTS.md`
- `docs/national-tcp-policy-epoch.md`
- `docs/national-runtime-architecture-policy.md`
- `docs/llm-stages.md`
- `docs/evolution-dual-checkout-sync-policy.md`
- `docs/national-web-arena.md`
- `docs/events-auto-compression-2026-10-21.md`
- `docs/official-raise-boundary-oracle-2026-07-11.md`
- `docs/official-terminal-settlement-oracle-2026-07-11.md`
- `docs/official-allin-runout-wire-oracle-2026-07-19.md`

Do not use archived setup guides, editor memories, old match output, or retired
Botzone/adapter code as current instructions.
