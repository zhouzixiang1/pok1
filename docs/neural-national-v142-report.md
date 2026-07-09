# Neural National v142 Report

Date: 2026-07-09

This report covers the v142 neural national TCP candidate. All strength results
below used native national TCP evaluation. No `sever/bot_adapter.py` path was
used for the candidate.

## Candidate

Current neural candidate:

`bots/neural_national_lab/versions/v142_national_v123_oldpool_probe_tcp`

Parent:

`bots/neural_national_lab/versions/v141_national_v123_profile_veto_tcp`

v142 keeps the v123 rule base and the v141 neural overlay, but disables one
late-flop profile veto:

- `flop_late_weak_highcard_free_raise_check_enabled`

The trace finding was that the v141 guard converted an existing legal rule
raise-to-total into `check` in late weak-highcard free-action flops against old
pool opponents. v142 does not introduce any new positive action. It only lets
the rule base's already-sanitized national raise-to-total action stand in this
spot.

## Protocol Validation

Static validation:

- `PYTHONDONTWRITEBYTECODE=1 python -m py_compile bots/neural_national_lab/versions/v142_national_v123_oldpool_probe_tcp/*.py`
- `PYTHONDONTWRITEBYTECODE=1 python -m json.tool bots/neural_national_lab/versions/v142_national_v123_oldpool_probe_tcp/neural_config.json`
- `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=web/core python - <<'PY' ... check_native_contract(...)`

Result:

- py_compile passed.
- JSON config parse passed.
- Native contract errors for v142: `[]`.
- Native contract errors for v141: `[]`.
- Native contract errors for v131: `[]`.
- v130 remains non-strict-native under the current contract because its
  `national_bot.py` still uses newline/readline-style TCP and lacks the raw
  splitter/throttle contract.

Official Windows EXE acceptance:

```bash
PYTHONDONTWRITEBYTECODE=1 python scripts/official_platform_acceptance.py \
  --candidate bots/neural_national_lab/versions/v142_national_v123_oldpool_probe_tcp \
  --opponent bots/national_v123 \
  --self-play-rounds 1 \
  --opponent-rounds 1 \
  --target-hands 70 \
  --round-timeout 480 \
  --no-progress-timeout 120 \
  --results-dir bots/neural_national_lab/data/official_platform_v142_smoke
```

Result:

- Suite: `bots/neural_national_lab/data/official_platform_v142_smoke/acceptance_20260709_224043`
- Rounds requested/run: 2/2.
- Passed rounds: 2.
- Failed rounds: 0.
- Official platform: true.
- Self-play THP records: 70 hands.
- Opponent THP records: 70 hands.
- Logged issues: none.

## Trace And Counterfactual Evidence

Native TCP trace/force files:

- `bots/neural_national_lab/data/native_tcp_trace_v142_vs_v3_seed2026074500_4501.json`
- `bots/neural_national_lab/data/analysis_trace_v142_vs_v3_seed2026074500_4501.json`
- `bots/neural_national_lab/data/native_tcp_trace_v142_vs_v2_seed2026074501.json`
- `bots/neural_national_lab/data/force_v142_v3_seed4500_h65_d2_raise4958.json`
- `bots/neural_national_lab/data/force_v142_v3_seed4500_h65_d3_call.json`
- `bots/neural_national_lab/data/force_v142_v3_seed4500_h65_d3_fold.json`

Key finding:

- v3 seed `2026074500`, hand 65: the old guard changed rule `raise 4958` to
  `check`; forcing the rule raise produced `+12124 / 140`, while the check/call
  line stayed negative.
- v2 seed `2026074501` showed the same late weak-highcard free-action pattern;
  v142 produced `+12998 / 140` on that targeted row.

Force probe summary:

| Probe | Opponent/seed | Total | W-L-D | Candidate illegal/timeout/adapter |
|---|---|---:|---:|---:|
| Force hand65 decision2 `raise 4958` | national_v3 seed4500 | `+12124` | 1-0-0 | 0/0/0 |
| Force hand65 decision3 `fold` | national_v3 seed4500 | `+17082` | 1-0-0 | 0/0/0 |
| Force hand65 decision3 `call` | national_v3 seed4500 | `-993` | 0-1-0 | 0/0/0 |

## Strength Results

All rows are paired native TCP results.

| Block | Opponents | Matches | Hands | Total | Mean/hand | W-L-D | Candidate illegal/timeout/adapter |
|---|---|---:|---:|---:|---:|---:|---:|
| v142 current m2 | national_v119-v123 | 10 | 1400 | `-80663` | `-57.616` | 2-3-5 | 0/0/0 |
| v142 current m3 | national_v119-v123 | 15 | 2100 | `+139262` | `+66.315` | 8-1-6 | 0/0/0 |
| v142 current combined | national_v119-v123 | 25 | 3500 | `+58599` | `+16.743` | 10-4-11 | 0/0/0 |
| v142 old top8+v7 | v2,v3,v5,v7,v8,v9,v14,v15,v16 | 18 | 2520 | `+5535` | `+2.196` | 4-14-0 | 0/0/0 |
| v142 vs v141 | v141 neural | 3 | 420 | `0` | `0.000` | 0-0-3 | 0/0/0 |
| v142 vs v131 | v131 native port | 3 | 420 | `+50557` | `+120.374` | 3-0-0 | 0/0/0 |

Targeted checks:

| Check | Opponent | Matches | Hands | Total | Mean/hand | W-L-D | Candidate illegal/timeout/adapter |
|---|---|---:|---:|---:|---:|---:|---:|
| v142 targeted v2 | national_v2 seed4501 | 1 | 140 | `+12998` | `+92.843` | 1-0-0 | 0/0/0 |
| v142 targeted v3 | national_v3 seed4500,4501 | 2 | 280 | `+11294` | `+40.336` | 1-1-0 | 0/0/0 |

Current-pool per-opponent totals:

| Opponent | Matches | Hands | Total | Mean/hand | W-L-D |
|---|---:|---:|---:|---:|---:|
| national_v119 | 5 | 700 | `-37852` | `-54.074` | 2-3-0 |
| national_v120 | 5 | 700 | `+45083` | `+64.404` | 3-0-2 |
| national_v121 | 5 | 700 | `-12951` | `-18.501` | 1-1-3 |
| national_v122 | 5 | 700 | `+44419` | `+63.456` | 3-0-2 |
| national_v123 | 5 | 700 | `+19900` | `+28.429` | 1-0-4 |

Old top8+v7 per-opponent totals:

| Opponent | Matches | Hands | Total | Mean/hand | W-L-D | Samples |
|---|---:|---:|---:|---:|---:|---|
| national_v14 | 2 | 280 | `-1463` | `-5.225` | 0-2-0 | `[-1313, -150]` |
| national_v15 | 2 | 280 | `-3439` | `-12.282` | 0-2-0 | `[-3289, -150]` |
| national_v16 | 2 | 280 | `-2015` | `-7.196` | 1-1-0 | `[-3293, 1278]` |
| national_v2 | 2 | 280 | `+13328` | `+47.600` | 2-0-0 | `[330, 12998]` |
| national_v3 | 2 | 280 | `+11294` | `+40.336` | 1-1-0 | `[12124, -830]` |
| national_v5 | 2 | 280 | `-1853` | `-6.618` | 0-2-0 | `[-1703, -150]` |
| national_v7 | 2 | 280 | `-3439` | `-12.282` | 0-2-0 | `[-3289, -150]` |
| national_v8 | 2 | 280 | `-3439` | `-12.282` | 0-2-0 | `[-3289, -150]` |
| national_v9 | 2 | 280 | `-3439` | `-12.282` | 0-2-0 | `[-3289, -150]` |

## Comparison

Against the v141 report baselines:

- v142 current completed classic pool is positive at `+58599 / 3500`, but below
  the historical v141 `+238752 / 3500`.
- v142 old top8+v7 improves from historical v141 `-21232 / 2520` to
  `+5535 / 2520`.
- v142 ties v141 directly on the H2H seed block used here: `0 / 420`.
- v142 remains much stronger than the strict-native v131 baseline:
  `+50557 / 420`.
- v142 beats the national_v123 historical baselines on both evaluated classic
  pools: national_v123 was `-80701 / 3500` on current-pool seed blocks and
  `-50445 / 2520` on old top8+v7.

The current m2 block is noisy and contains large paired all-in swings against
national_v119. It should not be treated as solved by v142. The important new
evidence is that the old-pool v2/v3 late-flop leak is fixed without causing a
direct H2H regression against v141.

## Assessment

v142 is a useful neural-line candidate and a valid protocol-compliant native TCP
bot:

- It passes strict native contract checks and official EXE acceptance.
- It keeps current completed classic pool aggregate positive.
- It turns old top8+v7 aggregate positive for the first time in this neural
  sequence.
- It directly ties v141 and beats v131 on native TCP H2H.

It is not yet a clear replacement for the strongest classic-rule mainline or
for v141 as a general-purpose neural default, because its current-pool aggregate
is lower than the v141 report baseline and the old top8+v7 W-L-D is still
loss-heavy despite positive chip EV. The next version should preserve v142's
v2/v3 improvement while recovering the v141 current-pool margin, likely by
making the late-flop free-raise check veto conditional instead of globally
disabled.

## Prompt For The Next Agent Run

```text
Continue in /home/zzx/project/pok on the neural national poker bot line. Do not
modify the classic national_v<N> rule mainline except for reading it as
reference. Create a new independent version directory under
bots/neural_national_lab/versions/; do not overwrite v142.

Current candidate to build from:
bots/neural_national_lab/versions/v142_national_v123_oldpool_probe_tcp

Parent lineage:
- v141: bots/neural_national_lab/versions/v141_national_v123_profile_veto_tcp
- v131: strict raw-stream native TCP baseline for older neural H2H

v142 change:
- Disabled flop_late_weak_highcard_free_raise_check_enabled.
- This fixed old-pool v2/v3 late weak-highcard free-action spots by allowing the
  existing legal rule raise-to-total to stand.
- It did not add any new positive action proposal.

v142 evidence:
- check_native_contract: [].
- Official EXE acceptance:
  bots/neural_national_lab/data/official_platform_v142_smoke/acceptance_20260709_224043
  passed 2/2 rounds with target-hands 70.
- Current completed classic pool national_v119-v123:
  seed4400 m2: -80663 / 1400, W-L-D 2-3-5.
  seed4320 m3: +139262 / 2100, W-L-D 8-1-6.
  combined: +58599 / 3500, W-L-D 10-4-11, 0 illegal/timeouts/adapter.
- Old top8+v7 pool:
  +5535 / 2520, W-L-D 4-14-0, 0 illegal/timeouts/adapter.
- Targeted old-pool checks:
  national_v2 seed4501: +12998 / 140.
  national_v3 seed4500,4501: +11294 / 280.
- H2H:
  v142 vs v141: 0 / 420, 0-0-3.
  v142 vs v131: +50557 / 420, 3-0-0.

Strict protocol requirements:
- Native national TCP only; never use sever/bot_adapter.py for formal strength
  or acceptance.
- Entry point must be national_bot.py and pass check_native_contract.
- Handle sticky TCP packets via raw sock.recv and splitter.
- Send only fold, call, check, allin, or exactly "raise <amount>".
- raise <amount> is national raise-to-total, not delta.
- Keep official-safe action delay by default.
- Official EXE acceptance must pass before claiming a new completed neural
  candidate.

Next objective:
Build v143 or later that preserves v142's old-pool v2/v3 gains but recovers
more of the v141 current-pool margin. Do not simply re-enable or disable the
late-flop guard globally. Use trace/counterfactual data to make it conditional
on opponent profile, pot/SPR, board texture, or remaining-hands features.

Minimum validation:
- py_compile and json.tool.
- check_native_contract returns [].
- Native TCP paired evaluation:
  * national_v119-v123 seed4400 m2 and seed4320 m3,
  * old top8+v7 seed4500 m2,
  * H2H vs v142 and v141,
  * H2H vs v131 if comparing to old neural line.
- Record total chips, mean/hand, W-L-D, candidate illegal actions, timeouts,
  and adapter actions per opponent.
- Official EXE acceptance self-play + vs national_v123, target-hands 70.

Success bar:
- 0 candidate illegal actions.
- 0 candidate timeouts.
- 0 candidate adapter actions.
- Official EXE acceptance passes.
- Current-pool combined should stay positive and ideally move back toward the
  v141 report baseline.
- Old top8+v7 should remain positive or improve beyond v142's +5535 / 2520.
```
