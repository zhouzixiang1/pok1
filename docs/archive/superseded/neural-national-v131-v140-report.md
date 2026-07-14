# Neural National v131-v140 Report

Date: 2026-07-09

This report covers the neural national TCP line from v131 through v140. All
strength evaluations here used native national TCP matches. No `sever/bot_adapter.py`
path was used for accepted results.

## Current Candidate

Best current candidate:

`bots/neural_national_lab/versions/v140_national_v123_overlay_no_large_commit_veto_tcp`

Parent:

`bots/neural_national_lab/versions/v139_national_v123_overlay_no_preflop_call_veto_tcp`

v140 keeps the v123 rule base with the neural overlay, but disables
`large_commit_veto_enabled`. The v139 trace against old strong bots showed that
the learned large-commit veto could convert a rule `allin` into `fold` and miss
a +20000 recovery hand. Disabling that veto preserved the v119-v123 seed4320
result and recovered most of the old-pool regression.

Native contract:

- `python -m py_compile bots/neural_national_lab/versions/v140_national_v123_overlay_no_large_commit_veto_tcp/*.py`
- `PYTHONPATH=web/core python -c "from national_native import check_native_contract; print(check_native_contract('bots/neural_national_lab/versions/v140_national_v123_overlay_no_large_commit_veto_tcp'))"`
- Result: 0 contract errors.

Official EXE smoke:

`python scripts/official_platform_acceptance.py --candidate bots/neural_national_lab/versions/v140_national_v123_overlay_no_large_commit_veto_tcp --opponent bots/national_v123 --self-play-rounds 1 --opponent-rounds 1 --target-hands 10 --round-timeout 240 --no-progress-timeout 90 --results-dir bots/neural_national_lab/data/official_platform_v140_smoke`

Result:

- Suite: `bots/neural_national_lab/data/official_platform_v140_smoke/acceptance_20260709_170802`
- Rounds requested/run: 2/2.
- Passed rounds: 2.
- Failed rounds: 0.
- Self-play and opponent smoke both reached 10 started hands.

## Version Summary

All rows below are paired native TCP results.

| Version/block | Opponents | Matches | Hands | Total | Mean/hand | W-L-D | Candidate illegal/timeout/adapter |
|---|---|---:|---:|---:|---:|---:|---:|
| v132 current m3 | v119-v123 | 15 | 2100 | `-148813` | `-70.863` | 0-15-0 | 0/0/0 |
| v133 current m3 | v119-v123 | 15 | 2100 | `-20691` | `-9.853` | 0-15-0 | 0/0/0 |
| v134 current m3 | v119-v123 | 15 | 2100 | `+11419` | `+5.438` | 2-13-0 | 0/0/0 |
| v135 current m3 | v119-v123 | 15 | 2100 | `-2304` | `-1.097` | 2-13-0 | 0/0/0 |
| v136 current m3 | v119-v123 | 15 | 2100 | `-191341` | `-91.115` | 2-13-0 | 0/0/0 |
| v137 current m3 | v119-v123 | 15 | 2100 | `-191341` | `-91.115` | 2-13-0 | 0/0/0 |
| v138 current m3 | v119-v123 | 15 | 2100 | `+136377` | `+64.941` | 9-3-3 | 0/0/0 |
| v139 current m3 | v119-v123 | 15 | 2100 | `+139262` | `+66.315` | 8-1-6 | 0/0/0 |
| v139 current m2 | v119-v123 | 10 | 1400 | `-6734` | `-4.810` | 2-2-6 | 0/0/0 |
| v140 current m3 | v119-v123 | 15 | 2100 | `+139262` | `+66.315` | 8-1-6 | 0/0/0 |
| v140 current m2 | v119-v123 | 10 | 1400 | `-13367` | `-9.548` | 2-1-7 | 0/0/0 |
| national_v123 baseline current m3 | v119-v123 | 15 | 2100 | `-38` | `-0.018` | 2-1-12 | 0/0/0 |
| national_v123 baseline current m2 | v119-v123 | 10 | 1400 | `-80663` | `-57.616` | 2-3-5 | 0/0/0 |
| v139 old top8+v7 m2 | v2,v3,v5,v7,v8,v9,v14,v15,v16 | 18 | 2520 | `-226796` | `-89.998` | 3-15-0 | 0/0/0 |
| v140 old top8+v7 m2 | v2,v3,v5,v7,v8,v9,v14,v15,v16 | 18 | 2520 | `-30545` | `-12.121` | 3-15-0 | 0/0/0 |
| national_v123 baseline old top8+v7 m2 | v2,v3,v5,v7,v8,v9,v14,v15,v16 | 18 | 2520 | `-50445` | `-20.018` | 3-15-0 | 0/0/0 |
| v140 vs v131 | v131 native port | 3 | 420 | `+50557` | `+120.374` | 3-0-0 | 0/0/0 |

Combined v140 current pool over the two seed blocks:

- v119-v123, 25 paired matches, 3500 hands.
- Total: `+125895`.
- Mean/hand: about `+35.970`.
- Candidate illegal/timeouts/adapter actions: `0/0/0`.
- Same seed-block national_v123 baseline total: `-80701`.

v140 is therefore materially stronger than the v123 baseline on the current
completed classic pool in these seed blocks. It is also better than v123 on the
old top8+v7 block, but still negative in absolute terms there. The old-pool
weakness remains a real next target, especially v2 and the repeated small
losses to v7/v8/v9/v14/v15.

## Protocol Notes

v140 is a native national TCP bot:

- Direct TCP client entrypoint: `national_bot.py`.
- No `sever/bot_adapter.py` usage.
- No Botzone JSON stdout response.
- Raw `sock.recv` stream processing with `_split_messages`.
- Official-safe action delay via `POK_OFFICIAL_ACTION_DELAY`, default `0.30`.
- Action vocabulary restricted to `fold`, `call`, `check`, `allin`, and exact
  `raise <amount>`.
- Positive raise amount is treated as national raise-to-total.
- CLI supports `--host`, `--port`, `--name`, `--seat`, and `--log` for official
  EXE harness compatibility.

The earlier v130 neural bot cannot be used as a strict native TCP opponent
under the current contract because its `national_bot.py` still contains
`makefile/readline` legacy stream handling. The strict native comparison uses
v131, the raw-stream native port of that line.

## Prompt For The Next Agent Run

Use this prompt to continue the neural line:

```text
You are continuing work in /home/zzx/project/pok on the neural national poker
bot line. Do not modify the classic national_v<N> rule mainline except for
reading it as reference. Work only under bots/neural_national_lab/versions/ for
new neural versions, and create a new independent directory for every version.

Current best neural candidate:
bots/neural_national_lab/versions/v140_national_v123_overlay_no_large_commit_veto_tcp

Strict requirements:
- New bots must be native national TCP bots. Do not use sever/bot_adapter.py.
- The native entrypoint must pass web/core/national_native.check_native_contract.
- It must handle official TCP streams and sticky packets using raw sock.recv,
  not makefile/readline.
- It must send only fold/call/check/allin/raise <amount>.
- raise <amount> is raise-to-total, not delta.
- raise must use exactly one ASCII space and no trailing protocol decorations.
- Keep official-safe action delay by default. Local strength eval may use
  POK_NATIVE_LOCAL_ACTION_DELAY=0 and POK_OFFICIAL_ACTION_DELAY=0.
- Official EXE smoke must pass before claiming protocol completion.

Known evidence:
- v140 passed local native contract and official EXE smoke:
  bots/neural_national_lab/data/official_platform_v140_smoke/acceptance_20260709_170802
- v140 vs current completed classic pool v119-v123:
  seed4320 m3: +139262 over 2100 hands.
  seed4400 m2: -13367 over 1400 hands.
  combined: +125895 over 3500 hands, 0 illegal/timeouts/adapter.
- Same current-pool baseline national_v123:
  seed4320 m3: -38.
  seed4400 m2: -80663.
- v140 vs old top8+v7 pool:
  -30545 over 2520 hands, better than national_v123 baseline -50445 but still
  absolute negative.
- v140 vs v131 native port:
  +50557 over 420 hands, 3-0, 0 illegal/timeouts/adapter.

Do not re-enable the failed broad guards from v132-v137:
- preflop escalation guard,
- deep/near-jam guard as globally applied,
- weak suited ace sizing guard,
- guard-only v123 hook,
- large_commit_veto allin fold without profile gating.

Main next objective:
Build v141 or later that is stronger than v140 and stronger than classic rule
baselines across both:
1. current completed classic pool: national_v119-v123, and
2. old top8+v7 pool: national_v2,v3,v5,v7,v8,v9,v14,v15,v16.

Recommended technical direction:
- Start from v140, not v139.
- Use trace/counterfactual analysis on the remaining v2 and v119 losses.
- Prefer narrow, evidence-backed changes over broad action proposal.
- Investigate opponent-profile gated behavior rather than global toggles.
- A good candidate change should improve v2/v119 or the old top8+v7 negatives
  without giving back the v119-v123 aggregate gain.
- Compare against national_v123 baseline on identical seed blocks and against
  v140 on identical seed blocks.

Minimum validation before reporting a new best:
- py_compile for the new version.
- check_native_contract returns no errors.
- Local native TCP paired evaluation:
  * v119-v123, at least seed4320 m3 and seed4400 m2.
  * old top8+v7, at least seed4500 m2.
  * H2H versus v140, at least 3 paired matches.
  * H2H versus v131 native port if comparing to old neural line.
- Record total chips, mean/hand, W-L-D, candidate illegal actions, candidate
  timeouts, and candidate adapter actions per opponent.
- Official EXE smoke:
  scripts/official_platform_acceptance.py with candidate self-play and
  candidate vs national_v123, target-hands at least 10 for smoke.

Success bar:
- 0 candidate illegal actions.
- 0 candidate timeouts.
- 0 candidate adapter actions.
- Official EXE smoke passes.
- Aggregate current-pool result must exceed v140's +125895 over the same two
  seed blocks, or match it while materially improving old top8+v7.
- Old top8+v7 should move from v140's -30545 toward positive; do not accept a
  candidate that worsens this block unless current-pool gain is large and
  explicitly justified.
```

## Next Work

The next bottleneck is not protocol. It is robustness. v140 has passed strict
native and official-smoke requirements, but the old top8+v7 block is still
absolute negative. The next version should use traces on v2/v119 losses and
only add opponent-profile-gated changes that can be tested against identical
seed blocks.
