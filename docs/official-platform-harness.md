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
Those remain local national-native TCP responsibilities.

Native bots must send raw stream tokens without assuming newline boundaries:
`raise <amount>`, `fold`, `call`, `check`, or `allin`. They must never send
`bet`, must treat raise amounts as street raise-to totals, and must split sticky
packets such as `earnChips -100preflop|...` and `raise 200call`.

## Prerequisites

Required host tools are `wine`, `Xvfb`, and `xdotool`; ImageMagick `import` is
optional for screenshots. The default Wine prefix is:

```text
/home/zzx/.cache/pok_wine_national_platform
```

It should contain the fake Chinese font mapping installed by
`winetricks -q fakechinese`.

Formal certification also requires a local Ed25519 private key and the tracked
repository trust root. Check both platform and signer before running:

```bash
python3 scripts/official_certify.py doctor
```

## Commands

Use the low-level harness for diagnosis without issuing a certificate:

```bash
python3 scripts/official_platform_acceptance.py --check-env

python3 scripts/official_platform_acceptance.py \
  --candidate bots/national_v142 \
  --self-play-rounds 1 \
  --opponent-rounds 0 \
  --target-hands 5
```

Use the policy-governed durable manager for evolution and formal acceptance:

```bash
python3 scripts/official_certify.py smoke bots/national_v<N> --wait-if-busy
python3 scripts/official_certify.py full bots/national_v<N> --wait-if-busy
python3 scripts/official_certify.py queue-status
python3 scripts/official_certify.py process-queue --limit 4
```

The CLI keeps `queue-status` and `process-queue` names for compatibility, but
they operate on durable job directories. The retired JSONL queue is not a
production path.

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
immutable `official-full-v4` profile: five self-play rounds plus three eligible
opponent rounds, every round complete at 70 hands. The checkpoint moves to
`official_certifying` and polls the same identity-bound job; commit/tag cannot
occur before the signed certificate validates.

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

The EXE has occasionally produced 70 preflop starts but only 69 visible final
`earnChips` dispatches. A round may therefore satisfy live completion with at
least 70 starts and 69 settlements on both bot logs. Formal 70-hand acceptance
still requires an official THP file containing at least 70 `STATE` records.

Every outbound action is checked against the exact wire grammar. Leading or
trailing whitespace, extra spaces, tabs, `bet`, unknown actions, illegal
check/call/allin/raise, unsolicited sends, timeouts, parser leftovers, process
crashes, missing THP, and incomplete rounds are retained as structured evidence.
