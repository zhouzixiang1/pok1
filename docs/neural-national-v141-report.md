# Neural National v141 Report

Date: 2026-07-09

This report covers the v141 neural national TCP candidate. All accepted strength
results used native national TCP evaluation. No `sever/bot_adapter.py` path was
used.

## Candidate

Current neural candidate:

`bots/neural_national_lab/versions/v141_national_v123_profile_veto_tcp`

Parent:

`bots/neural_national_lab/versions/v140_national_v123_overlay_no_large_commit_veto_tcp`

v141 starts from v140 and adds four narrow profile-gated overrides, all derived
from v140 trace and hard-negative seed review:

- KJo offsuit late preflop jam-call veto against rare all-in profiles.
- Medium-pair limp/reraise call conversion for 88-TT profile spots.
- Weak unpaired high-card flop small-lead all-in veto.
- Weak unpaired high-card flop free-action large-raise check conversion.

The new overrides only convert to `fold` or `call/check` action values (`-1` or
`0`). They do not introduce any new positive raise amount, so the national
raise-to-total wire contract is not widened by this version.

## Protocol Validation

Static validation:

- `PYTHONDONTWRITEBYTECODE=1 python -m py_compile bots/neural_national_lab/versions/v141_national_v123_profile_veto_tcp/*.py`
- `PYTHONDONTWRITEBYTECODE=1 python -m json.tool bots/neural_national_lab/versions/v141_national_v123_profile_veto_tcp/neural_config.json`
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=web/core python -c 'from national_native import check_native_contract; print(check_native_contract("bots/neural_national_lab/versions/v141_national_v123_profile_veto_tcp"))'`

Result:

- py_compile passed.
- JSON config parse passed.
- Native contract errors: `[]`.

Official Windows EXE acceptance:

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/official_platform_acceptance.py \
  --candidate bots/neural_national_lab/versions/v141_national_v123_profile_veto_tcp \
  --opponent bots/national_v123 \
  --self-play-rounds 1 \
  --opponent-rounds 1 \
  --target-hands 70 \
  --round-timeout 480 \
  --no-progress-timeout 120 \
  --results-dir bots/neural_national_lab/data/official_platform_v141_smoke
```

Result:

- Suite: `bots/neural_national_lab/data/official_platform_v141_smoke/acceptance_20260709_215440`
- Rounds requested/run: 2/2.
- Passed rounds: 2.
- Failed rounds: 0.
- Official platform: true.
- Self-play THP records: 70 hands.
- Opponent THP records: 70 hands.
- Logged issues: none.

The v130 neural bot is not a strict native TCP comparison baseline under the
current contract because it has no `national_bot.py` direct TCP entrypoint. v131
is the native port used for direct neural-line H2H.

## Strength Results

All rows below are paired native TCP results.

| Block | Opponents | Matches | Hands | Total | Mean/hand | W-L-D | Candidate illegal/timeout/adapter |
|---|---|---:|---:|---:|---:|---:|---:|
| v141 current m2 | national_v119-v123 | 10 | 1400 | `+99490` | `+71.064` | 5-0-5 | 0/0/0 |
| v141 current m3 | national_v119-v123 | 15 | 2100 | `+139262` | `+66.315` | 8-1-6 | 0/0/0 |
| v141 current combined | national_v119-v123 | 25 | 3500 | `+238752` | `+68.215` | 13-1-11 | 0/0/0 |
| v140 current combined | national_v119-v123 | 25 | 3500 | `+125895` | `+35.970` | 10-2-13 | 0/0/0 |
| national_v123 baseline current combined | national_v119-v123 | 25 | 3500 | `-80701` | `-23.057` | 4-4-17 | 0/0/0 |
| v141 old top8+v7 | v2,v3,v5,v7,v8,v9,v14,v15,v16 | 18 | 2520 | `-21232` | `-8.425` | 2-16-0 | 0/0/0 |
| v140 old top8+v7 | v2,v3,v5,v7,v8,v9,v14,v15,v16 | 18 | 2520 | `-30545` | `-12.121` | 3-15-0 | 0/0/0 |
| national_v123 baseline old top8+v7 | v2,v3,v5,v7,v8,v9,v14,v15,v16 | 18 | 2520 | `-50445` | `-20.018` | 3-15-0 | 0/0/0 |
| v141 vs v140 | v140 neural | 3 | 420 | `0` | `0.000` | 0-0-3 | 0/0/0 |
| v141 vs v131 | v131 native port | 3 | 420 | `+50557` | `+120.374` | 3-0-0 | 0/0/0 |

Hard-negative trace seed checks:

| Check | Opponent | Matches | Hands | v141 total | Prior v140 on same row | Delta |
|---|---|---:|---:|---:|---:|---:|
| seed2026074401 | national_v119 | 1 | 140 | `+365` | `-37054` | `+37419` |
| seed2026074501 | national_v2 | 1 | 140 | `-652` | `-25452` | `+24800` |

Current-pool per-opponent combined totals:

| Opponent | Matches | Hands | Total | W-L-D |
|---|---:|---:|---:|---:|
| national_v119 | 5 | 700 | `+39282` | 4-1-0 |
| national_v120 | 5 | 700 | `+77519` | 3-0-2 |
| national_v121 | 5 | 700 | `+57619` | 2-0-3 |
| national_v122 | 5 | 700 | `+44432` | 3-0-2 |
| national_v123 | 5 | 700 | `+19900` | 1-0-4 |

Old top8+v7 per-opponent totals:

| Opponent | Matches | Hands | Total | W-L-D | Samples |
|---|---:|---:|---:|---:|---|
| national_v14 | 2 | 280 | `-1463` | 0-2-0 | `[-1313, -150]` |
| national_v15 | 2 | 280 | `-3439` | 0-2-0 | `[-3289, -150]` |
| national_v16 | 2 | 280 | `-2015` | 1-1-0 | `[-3293, 1278]` |
| national_v2 | 2 | 280 | `-322` | 1-1-0 | `[330, -652]` |
| national_v3 | 2 | 280 | `-1823` | 0-2-0 | `[-993, -830]` |
| national_v5 | 2 | 280 | `-1853` | 0-2-0 | `[-1703, -150]` |
| national_v7 | 2 | 280 | `-3439` | 0-2-0 | `[-3289, -150]` |
| national_v8 | 2 | 280 | `-3439` | 0-2-0 | `[-3289, -150]` |
| national_v9 | 2 | 280 | `-3439` | 0-2-0 | `[-3289, -150]` |

## Assessment

v141 is the best neural candidate so far for the current completed classic pool:

- It improves current-pool combined score from v140's `+125895` to `+238752`
  over the same 3500-hand seed blocks.
- It keeps v140's strong m3 block exactly intact.
- It flips the bad v119 m2 seed row from a large loss to a small win.
- It improves old top8+v7 from `-30545` to `-21232`.
- It passes strict native contract and official EXE acceptance at 70 hands per
  round.

It is not yet a fully solved classic-rule replacement because old top8+v7 is
still negative in absolute terms. v141 also appears to regress the old v3 row
relative to v140's old-pool trace history, so v142 should focus on v3 and the
repeated small fold-loss pattern against v7/v8/v9/v14/v15 without giving back
the current-pool gain.

## Prompt For The Next Agent Run

Use this prompt to continue the neural line:

```text
You are continuing work in /home/zzx/project/pok on the neural national poker
bot line. Work from the clean independent neural version directories, not by
overwriting an existing bot. Do not modify the classic national_v<N> rule
mainline except for reading it as reference.

Current best neural candidate:
bots/neural_national_lab/versions/v141_national_v123_profile_veto_tcp

Parent lineage:
- v140: bots/neural_national_lab/versions/v140_national_v123_overlay_no_large_commit_veto_tcp
- v131: first strict raw-stream native TCP port used as the old neural H2H baseline

Strict protocol requirements:
- New versions must be native national TCP bots.
- Do not use sever/bot_adapter.py.
- The entrypoint must be national_bot.py and must pass
  web/core/national_native.check_native_contract.
- Handle official TCP streams and sticky packets with raw sock.recv plus
  explicit message splitting. Do not use makefile/readline.
- Send only fold, call, check, allin, or exactly "raise <amount>".
- "raise <amount>" is national raise-to-total, not a delta.
- Keep official-safe action delay by default. Local strength evaluation may set
  POK_NATIVE_LOCAL_ACTION_DELAY=0 and POK_OFFICIAL_ACTION_DELAY=0.
- Official Windows EXE acceptance must pass before claiming protocol completion.

Known v141 evidence:
- Strict native contract: 0 errors.
- Official Windows EXE acceptance:
  bots/neural_national_lab/data/official_platform_v141_smoke/acceptance_20260709_215440
  with self-play 1 round, candidate vs national_v123 1 round, target-hands 70,
  passed 2/2 rounds.
- v141 current completed classic pool national_v119-v123:
  seed4320 m3: +139262 / 2100, W-L-D 8-1-6.
  seed4400 m2: +99490 / 1400, W-L-D 5-0-5.
  combined: +238752 / 3500, W-L-D 13-1-11, 0 illegal/timeouts/adapter.
- v140 on the same current-pool seed blocks:
  +125895 / 3500.
- national_v123 baseline on the same current-pool seed blocks:
  -80701 / 3500.
- v141 old top8+v7 pool:
  -21232 / 2520, W-L-D 2-16-0, 0 illegal/timeouts/adapter.
- v140 old top8+v7 pool:
  -30545 / 2520.
- national_v123 baseline old top8+v7:
  -50445 / 2520.
- v141 vs v140 direct H2H:
  0 / 420, 0-0-3, 0 illegal/timeouts/adapter.
- v141 vs v131 native port:
  +50557 / 420, 3-0-0, 0 illegal/timeouts/adapter.
- v130 is not a strict-native H2H baseline because it has no national_bot.py
  direct TCP entrypoint.

Do not reintroduce broad failed guard families:
- global preflop escalation guard,
- global deep/near-jam guard,
- weak suited ace sizing guard,
- guard-only v123 hook,
- large_commit_veto allin fold without profile gating.

Main v142 objective:
Preserve v141's current-pool combined result and make old top8+v7 positive, or
at least materially less negative with clear per-opponent evidence. The most
important old-pool targets are national_v3 and the repeated small losses to
national_v7/v8/v9/v14/v15. Continue using trace and counterfactual analysis
against real native TCP protocol bots, not neural-only self-play.

Recommended method:
- Copy v141 into a new independent v142 directory.
- Use trace_decisions on old top8+v7 losses, especially national_v3 and the
  repeated -3289/-150 patterns.
- Make only narrow opponent-profile-gated changes backed by trace evidence.
- Prefer action conversions to fold/check/call unless a positive raise-to-total
  is already legal and carefully bounded.
- If adding a positive action, prove it is national raise-to-total safe and
  preserve sanitize_action behavior.
- Compare against v141, v140, national_v123 baseline, and the relevant classic
  opponents on identical seed blocks.

Minimum validation before reporting a new best:
- py_compile for the new version.
- json.tool for neural_config.json.
- check_native_contract returns no errors.
- Local native TCP paired evaluation:
  * current pool national_v119-v123 seed4320 m3 and seed4400 m2,
  * old top8+v7 seed4500 m2,
  * H2H versus v141, at least 3 paired matches,
  * H2H versus v131 native port if comparing to the older neural line.
- Record total chips, mean/hand, W-L-D, candidate illegal actions, candidate
  timeouts, and candidate adapter actions per opponent.
- Official EXE acceptance with candidate self-play and candidate vs
  national_v123, target-hands at least 70 when claiming protocol pass.

Success bar:
- 0 candidate illegal actions.
- 0 candidate timeouts.
- 0 candidate adapter actions.
- Official EXE acceptance passes.
- Current-pool combined should not fall materially below v141's +238752 / 3500.
- Old top8+v7 should improve beyond v141's -21232 / 2520, preferably positive.
```
