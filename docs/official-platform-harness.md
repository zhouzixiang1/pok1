# Official Windows Platform Harness

The official compliance oracle is:

```text
sever/国赛平台/德州扑克对弈平台限时一分钟2021版/德州扑克对弈平台限时一分钟2021版.exe
```

Linux runs it through Wine and Xvfb. The harness configures the UI, launches two
independent native TCP bots, proxies both TCP streams, and retains protocol,
process, screenshot, and THP evidence.

## Authority Boundary

The EXE is authoritative for official-protocol and runtime compliance. It does
not contribute to Glicko, H2H, source selection, chip-EV, or strategy strength.
Those remain complete 70-hand local raw native TCP responsibilities in the
current evaluation cycle.

Native bots must send raw stream tokens with no `\n` or `\r\n` delimiter:
`raise <amount>`, `fold`, `call`, `check`, or `allin`. They must never send
`bet`, must treat raise amounts as street raise-to totals, and must split sticky
packets such as `earnChips -100preflop|...` and `raise 200call`.

The official re-raise floor is inclusive 2x: after `raise 200`, exact
`raise 400` is legal. This was confirmed by a controlled two-seat EXE wire
oracle; see [official-raise-boundary-oracle-2026-07-11.md](official-raise-boundary-oracle-2026-07-11.md).
Templates may choose `2x + 1` for conservative headroom, but deterministic
replay must not label exact 2x illegal.

## Observed Server Message Semantics

The archived 2026-07-10 full-suite capture contains eight official rounds
(560 hand starts), and a controlled 2026-07-11 formal sandbox round reproduced
the same terminal behavior. Together they prove the following wire behavior:

- hands 1 through 69 send each seat its own signed `earnChips <amount>` and the
  paired values are zero-sum. At the natural end of hand 70, this 2021 EXE
  writes the complete 70-record THP and match footer but does not send the
  final `earnChips` pair. The older capture therefore has exactly 552 pairs
  (69 × 8), not eight incomplete matches. See
  [official-terminal-settlement-oracle-2026-07-11.md](official-terminal-settlement-oracle-2026-07-11.md);
- `oppo_hands|...` is sent only at showdown. The 68 messages represented 34
  showdowns, and every exposed hand matched the peer's actual hole cards;
- the EXE relayed all 696 raises, 526 folds, and 13 all-ins in the capture, but
  only 211 of 550 calls and 309 of 443 checks. The missing 339 calls and 134
  checks were terminal street-closing actions followed directly by a street or
  settlement boundary;
- there is no separate final winner, cumulative chip total, or complete action
  history TCP token. The official THP footer does contain the cumulative match
  result, and every `STATE` record contains named per-hand earnings.

The current strict typed-policy runtime therefore records relayed opponent
actions normally and infers only a terminal call/check that is proven by the
next street, showdown, or settlement boundary. It feeds those actions,
showdown cards, and `earnChips` values into the bot's in-match state and
opponent tracker. The harness also retains them for protocol/state diagnostics
and completeness checks. Official `earnChips`, THP profit, or win/loss values
remain excluded from Glicko, H2H, selection, precommit strength, and all other
strategy scoring.

## Prerequisites

Required host tools are `wine`, `Xvfb`, and `xdotool`; ImageMagick `import` is
optional for screenshots. The default Wine prefix is:

```text
/home/zzx/.cache/pok_wine_national_platform
```

It should contain the fake Chinese font mapping installed by
`winetricks -q fakechinese`.

Formal certification also requires a local Ed25519 private key, the tracked
repository trust root, and the signed verdict-ledger genesis. Doctor inspects
all three and never initializes authority state:

```bash
python3 scripts/official_certify.py doctor
```

For a new operator host, initialize genesis explicitly and re-check readiness:

```bash
python3 scripts/official_certify.py init-ledger
python3 scripts/official_certify.py doctor
```

The init command is idempotent for a valid ledger and refuses to replace an
invalid existing history.

Formal bot processes receive a sealed read-only artifact and one trusted,
host-preconnected TCP descriptor to their wire-proxy seat. Bubblewrap unshares
the network namespace; it never uses `--share-net`. The descriptor is marked
non-inheritable in the bootstrap and the parent copy is closed immediately
after launch. This prevents a candidate from bypassing the wire recorder,
connecting directly to the EXE, scanning the other seat, or reaching unrelated
host/network services.

## Commands

Use the low-level harness for diagnosis without issuing a certificate:

```bash
python3 scripts/official_platform_acceptance.py --check-env

python3 scripts/official_platform_acceptance.py \
  --candidate bots/national_v<N> \
  --self-play-rounds 1 \
  --opponent-rounds 0 \
  --target-hands 5
```

Use the policy-governed durable manager for evolution and formal acceptance:

```bash
python3 scripts/official_certify.py smoke bots/national_v<N> --wait-if-busy
python3 scripts/official_certify.py full bots/national_v<N> --wait-if-busy
python3 scripts/official_certify.py jobs-status
python3 scripts/official_certify.py reconcile-jobs --limit 4
```

These commands inspect and reconcile durable job directories. The retired
JSONL queue is not a production path or a compatibility API.

A command-line opponent is only a preference. Formal runs revalidate the exact
tagged native opponent and its artifact hash. Standalone sample scripts may be
used by the low-level diagnostic harness, not to bypass formal opponent policy.

## Evidence

Each round retains:

- `receipt.json`;
- `botA.log` and `botB.log` with RECV, DISPATCH, DECIDE, SEND, state, and timing;
- bot stdout/stderr;
- `platform.wine.log` and `xvfb.log`;
- screenshots;
- `wire_events.jsonl` and `replay_summary.json`;
- copied official THP text and parsed hand counts.

The suite writes `summary.json`, `official_evidence.json`, and a separate
`llm_official_analysis.json`. Deterministic evidence is the only gate authority.
The LLM sidecar is evidence-ID-grounded explanation and repair guidance only;
it cannot evaluate strength or change pass/fail.

Runtime artifacts live below `web/core/results/` and remain gitignored. Formal
raw evidence is packed into a content-addressed store selected by
`POK_OFFICIAL_EVIDENCE_STORE`; only a compact signed attestation is committed.
Full-mode suites are archived for pass, candidate failure, and inconclusive
outcomes, not only for certificates. If THP/log/wire evidence cannot be packed
and verified, the result is inconclusive rather than a bot failure. The archive
itself counts as raw-evidence availability even if the standalone normalized
JSON has later been removed.

## Pipeline Integration

`pokctl.sh` enables:

```bash
export POK_OFFICIAL_REQUIRED=1
export POK_OFFICIAL_SMOKE_GATE=1
export POK_OFFICIAL_PRECOMMIT_GATE=1
export POK_OFFICIAL_PRECOMMIT_SELF_ROUNDS=1
export POK_OFFICIAL_PRECOMMIT_OPPONENT_ROUNDS=1
export POK_OFFICIAL_PRECOMMIT_TARGET_HANDS=10
export POK_OFFICIAL_JOB_RECONCILER=1
```

Quality and precommit start short durable compliance jobs while local native
gates continue to own strength. The final `commit_bot` stage always requires the
immutable `official-full-v5` profile: five self-play rounds plus three eligible
opponent rounds, every round complete at 70 hands. The checkpoint moves to
`official_certifying` and polls the same identity-bound job; commit/tag cannot
occur before the signed certificate validates.

The sole first-strict exception is still operator-started by the acknowledged
v143 `bootstrap-first-strict` CLI. While its exact request-bound checkpoint is
parked at `official_bootstrap_required`, Web clients may only read that one
durable job from `GET /api/certification/jobs` or
`GET /api/certification/jobs/{job_id}`. Its
projection declares `formal_authority=operator_bootstrap_full_v5_job`,
`read_only=true`, and `cancel_allowed=false`. HTTP enqueue remains 410 and
bootstrap cancellation remains 404; the retired
`/api/certification/queue` route is absent, and old-epoch, v155, unbound,
drifted, or ambiguous jobs remain invisible.

`official-failed` means deterministic candidate-side evidence. It may enter bot
repair. `official-inconclusive` means platform, Wine, signer, opponent, or
evidence ambiguity. It blocks publication as infrastructure and must never be
rewritten as a bot defect.

Only one EXE process group runs at a time. `official-job-v3` provides atomic
state, live-log progress, heartbeat leases, safe process ownership, interrupted
cancel recovery, active-owner-first reconciliation, and full-suite terminal
retry. A process restart reuses every completed round, including failed rounds,
so recovery cannot cherry-pick successful evidence.

## Completion Rules

A 70-hand round still requires proof of exactly 70 completed hands. Policy
`official-full-v5` accepts either 70 paired TCP settlements, or the official
EXE's empirically verified terminal form: exact wire starts 1..70, exact paired
wire settlements 1..69 with no pending action, plus a new stable THP whose
strict states are exactly 0..69. In the terminal form, every named wire earning
for hands 1..69 must equal THP states 0..68, state 69 must have named zero-sum
earnings, and all 70 earnings must equal the THP footer result. The receipt
binds both raw wire and THP hashes and is later signed and archived.

This is not a `target - 1` allowance: 69 settlements without the exact
cross-bound THP proof still fails. Short smoke/compliance runs continue to
require all requested paired TCP settlements.

Every outbound action is checked against the exact wire grammar. Leading or
trailing whitespace, extra spaces, tabs, `bet`, unknown actions, illegal
check/call/allin/raise, unsolicited sends, timeouts, parser leftovers, process
crashes, missing THP, and incomplete rounds are retained as structured evidence.
