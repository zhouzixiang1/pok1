# Welcome to Pok AI

This checkout develops the `national_tcp_policy_v1` heads-up poker evolution
system. Read `AGENTS.md` first; it is the repository-wide working contract.

## Start here

- Use `/home/zzx/project/pok` (or an ignored temporary worktree below it) for
  infrastructure, prompt, test, documentation, and frontend changes.
- Treat `/home/zzx/project/pok/.evolution_pok` as the autonomous runtime
  checkout. Synchronize the two through `origin/main` only; never copy files
  between them.
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

`national-cloud-bot-v0` is the retired numeric high-water only. The first strict
target is `national_cloud_v1`. An untagged old-wrapper directory such as
`national_cloud_v13` is stale runtime debris, not a completed bot and not version
authority.

Strategy strength comes only from complete 70-hand local native TCP matches in
the current evaluation cycle. The Web Arena is `diagnostic_only`. The official
Windows EXE is the compliance authority, and publication requires the signed
`official-full-v5` profile: five 70-hand self-play rounds plus three 70-hand
rounds against the policy-selected opponent. Arena and EXE chip outcomes never
enter Glicko, H2H, or source selection.

## First verification

```bash
python -m pytest sever/tests -q
cd web && python -m pytest tests -q
cd frontend && npm run lint && npm run build
```

Before official work, return to the repository root and run:

```bash
python scripts/official_certify.py doctor
```

The operator controls mutation through the shared `POK_CONTROL_TOKEN` /
`X-Control-Token` contract when same-origin loopback access is unavailable.
There is no generic HTTP tool executor and no Arena-specific authentication
token.

## Current references

- `docs/national-tcp-policy-epoch.md`
- `docs/national-runtime-architecture-policy.md`
- `docs/llm-stages.md`
- `docs/evolution-dual-checkout-sync-policy.md`
- `docs/national-web-arena.md`
- `docs/official-certification-policy.md`
- `docs/official-raise-boundary-oracle-2026-07-11.md`
- `docs/official-terminal-settlement-oracle-2026-07-11.md`

Do not use archived setup guides, editor memories, old match output, or retired
Botzone/adapter code as current instructions.
