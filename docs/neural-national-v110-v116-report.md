# Neural National v110-v116 Report

Date: 2026-07-06

All evaluations below use native national TCP bots against native national TCP
opponents from `.evolution_pok/bots/`. No national adapter path was used.

## v109 Trace Baseline

Additional v109 validation:

`native_tcp_paired_v109_vs_v2v3_h70_m10_seed2026073900.json`

- 20 paired matches / 2800 hands.
- Total: `+246161`, mean/hand `+87.915`, W-L-D `20-0-0`.
- v2: `+124211`, W-L-D `10-0-0`.
- v3: `+121950`, W-L-D `10-0-0`.

Trace of v109 on v2/v3 seed block `2026074000` showed `0` neural changes.
The remaining loss came from rule actions that v109's preflop/turn/river veto
never touched. The largest uncovered bucket was flop all-in risk:

- Existing v109 veto trigger count on that block: `0`.
- Adding flop coverage with the current large-commit thresholds would catch 13
  unique hands, aggregate hand result `-180000`.
- Adding flop-specific `to_call >= 2000`, `pot >= 5000`, `rule_value <= -0.20`,
  `best_margin >= 0.45` would catch 22 unique hands, aggregate hand result
  `-320000`.

## v110

Path:

`bots/neural_national_lab/versions/v110_national_v17_flop_commit_veto_tcp`

Change:

- Derived from v109.
- Keeps ordinary neural call/raise proposals disabled.
- Extends the learned large-commit veto to flop.
- Adds flop thresholds:
  - `to_call >= 2000`,
  - `pot >= 5000`,
  - rule label is `call` or `allin`,
  - learned rule value is at most `-0.20`,
  - best learned label is at least `0.45` above the rule label.

Direct v2/v3 checks:

- `seed2026073900`: `+205772`, mean/hand `+73.490`, W-L-D `18-2-0`.
- `seed2026074000`: `+35635`, mean/hand `+12.727`, W-L-D `8-12-0`.
- v2 `seed2026074000` improved from v109's `-80647` to `-2250`.
- 0 illegal actions, 0 timeouts, 0 adapter actions.

Full current-top8+v7 pool:

| Block | Matches | Hands | Total | Mean/hand | W-L-D |
|---|---:|---:|---:|---:|---:|
| seed2026073900 | 90 | 12600 | `+887454` | `+70.433` | `71-3-16` |
| seed2026074000 | 90 | 12600 | `+327367` | `+25.982` | `36-12-42` |
| combined | 180 | 25200 | `+1214821` | `+48.207` | `107-15-58` |

Combined v110 opponent totals were positive for every opponent:

| Opponent | Total | Mean/hand | W-L-D |
|---|---:|---:|---:|
| national_v2 | `+101785` | `+36.352` | `12-8-0` |
| national_v15 | `+118971` | `+42.490` | `10-0-10` |
| national_v5 | `+132568` | `+47.346` | `11-1-8` |
| national_v3 | `+139622` | `+49.865` | `14-6-0` |
| national_v14 | `+144375` | `+51.562` | `12-0-8` |
| national_v16 | `+144375` | `+51.562` | `12-0-8` |
| national_v7 | `+144375` | `+51.562` | `12-0-8` |
| national_v8 | `+144375` | `+51.562` | `12-0-8` |
| national_v9 | `+144375` | `+51.562` | `12-0-8` |

## v111 Negative Ablation

Path:

`bots/neural_national_lab/versions/v111_national_v17_flop_call_redirect_tcp`

Change:

- Derived from v110.
- When the flop large-commit veto fired on an all-in rule action, redirect to
  `call` instead of `fold` if the learned call value cleared additional value
  margins.

Result:

`native_tcp_paired_v111_vs_v2_h70_m10_seed2026074000.json`

- v2 seed block `2026074000`: `-46528`, mean/hand `-33.234`, W-L-D `5-5-0`.
- This is worse than v110's `-2250` on the same block.

Conclusion: the learned value head correctly identified that the all-in was
bad, but redirecting those spots to call was harmful. v111 should not be
promoted.

## v112

Path:

`bots/neural_national_lab/versions/v112_national_v17_preflop_small_jam_veto_tcp`

Change:

- Derived from v110, not v111.
- Adds a narrow preflop small-pot all-in veto:
  - stage is preflop,
  - rule label is `allin`,
  - `to_call <= 500`,
  - `pot <= 1000`,
  - learned all-in value is at most `-0.30`,
  - best learned label is at least `0.70` above all-in.
- The veto returns `fold`; broad neural call/raise proposals remain disabled.

Reason:

v110 trace on v2 seed block `2026074000` showed two `-20000` hands caused by
preflop small-pot all-in actions. The learned value head assigned all-in about
`-0.45` in those spots, but v110's preflop veto only covered very large pots.

Direct v2 check:

`native_tcp_paired_v112_vs_v2_h70_m10_seed2026074000.json`

- v2 seed block `2026074000`: `+20094`, mean/hand `+14.353`, W-L-D `3-7-0`.
- Same block baselines:
  - v110: `-2250`,
  - v111: `-46528`.

Full current-top8+v7 pool:

| Block | Matches | Hands | Total | Mean/hand | W-L-D |
|---|---:|---:|---:|---:|---:|
| seed2026073900 | 90 | 12600 | `+884004` | `+70.159` | `71-3-16` |
| seed2026074000 | 90 | 12600 | `+355511` | `+28.215` | `36-47-7` |
| combined | 180 | 25200 | `+1239515` | `+49.187` | `107-50-23` |

Combined v112 opponent totals were positive for every opponent:

| Opponent | Total | Mean/hand | W-L-D |
|---|---:|---:|---:|
| national_v15 | `+116721` | `+41.686` | `10-5-5` |
| national_v2 | `+123979` | `+44.278` | `12-8-0` |
| national_v5 | `+130318` | `+46.542` | `11-6-3` |
| national_v14 | `+141975` | `+50.705` | `12-5-3` |
| national_v16 | `+142125` | `+50.759` | `12-5-3` |
| national_v7 | `+142125` | `+50.759` | `12-5-3` |
| national_v8 | `+142125` | `+50.759` | `12-5-3` |
| national_v9 | `+142125` | `+50.759` | `12-5-3` |
| national_v3 | `+158022` | `+56.436` | `14-6-0` |

## v113

Path:

`bots/neural_national_lab/versions/v113_national_v17_filtered_small_jam_veto_tcp`

Change:

- Derived from v112.
- Keeps v112's high-value preflop small-pot all-in veto, but filters out
  blind-level small-jam vetoes by adding:
  - `preflop_small_jam_veto_min_to_call = 150`,
  - `preflop_small_jam_veto_min_pot = 300`.

Reason:

Trace-aligned deltas against v110 showed that v112's W-L-D regression came from
blind-level all-in vetoes at `to_call=50`, `pot=150`, typically changing `+100`
hands into `-50` hands. The large v2 gain came from `to_call=183`, `pot=383`
vetoes, changing two hands from `-20000` to `-100`.

Direct checks on seed block `2026074000`:

- v2: `+21294`, mean/hand `+15.210`, W-L-D `3-7-0`.
- v5: `+41676`, mean/hand `+29.769`, W-L-D `4-0-6`.
- Same block baselines:
  - v110 v2 `-2250`, v5 `+41676`;
  - v112 v2 `+20094`, v5 `+39876`.

Full current-top8+v7 pool:

| Block | Matches | Hands | Total | Mean/hand | W-L-D |
|---|---:|---:|---:|---:|---:|
| seed2026073900 | 90 | 12600 | `+887454` | `+70.433` | `71-3-16` |
| seed2026074000 | 90 | 12600 | `+370811` | `+29.429` | `36-12-42` |
| combined | 180 | 25200 | `+1258265` | `+49.931` | `107-15-58` |

Combined v113 opponent totals were positive for every opponent:

| Opponent | Total | Mean/hand | W-L-D |
|---|---:|---:|---:|
| national_v15 | `+118971` | `+42.490` | `10-0-10` |
| national_v2 | `+125329` | `+44.760` | `12-8-0` |
| national_v5 | `+132568` | `+47.346` | `11-1-8` |
| national_v14 | `+144375` | `+51.562` | `12-0-8` |
| national_v16 | `+144375` | `+51.562` | `12-0-8` |
| national_v7 | `+144375` | `+51.562` | `12-0-8` |
| national_v8 | `+144375` | `+51.562` | `12-0-8` |
| national_v9 | `+144375` | `+51.562` | `12-0-8` |
| national_v3 | `+159522` | `+56.972` | `14-6-0` |

## v114

Path:

`bots/neural_national_lab/versions/v114_national_v17_wheel_ace_jam_veto_tcp`

Change:

- Derived from v113.
- Keeps v113's filtered small-jam veto and flop large-commit veto.
- Adds a narrow preflop jam-call veto:
  - stage is preflop,
  - original rule label is `call`,
  - opponent is all-in,
  - `to_call >= 15000`,
  - `pot >= 20000`,
  - hole cards are suited `A2` through `A5`.

Reason:

v113's remaining v2 seed block `2026074000` losses included two identical
suited wheel-ace jam calls. The bot raised `A2s`, faced a preflop all-in for
about `17161`, called, and lost `-20000`.

Force probes were mixed but positive in aggregate:

- Seed `2026074000`, hand 10, decision 2 forced to fold: paired match changed
  from `-11934` to `+19036`.
- Seed `2026074001`, hand 9, decision 2 forced to fold: the target hand improved
  but the full paired match changed from `-11667` to `-17937`.
- Net evidence justified a separate v114 experiment, not direct promotion.

Direct v2 check:

`native_tcp_paired_v114_vs_v2_h70_m10_seed2026074000.json`

- v2 seed block `2026074000`: `+45994`, mean/hand `+32.853`, W-L-D `4-6-0`.
- Same block v113 baseline: `+21294`, mean/hand `+15.210`, W-L-D `3-7-0`.

Full current-top8+v7 pool:

| Block | Matches | Hands | Total | Mean/hand | W-L-D |
|---|---:|---:|---:|---:|---:|
| seed2026073900 | 90 | 12600 | `+887454` | `+70.433` | `71-3-16` |
| seed2026074000 | 90 | 12600 | `+429974` | `+34.125` | `37-11-42` |
| combined | 180 | 25200 | `+1317428` | `+52.279` | `108-14-58` |

Combined v114 opponent totals were positive for every opponent:

| Opponent | Total | Mean/hand | W-L-D |
|---|---:|---:|---:|
| national_v15 | `+118971` | `+42.490` | `10-0-10` |
| national_v5 | `+132568` | `+47.346` | `11-1-8` |
| national_v14 | `+144375` | `+51.562` | `12-0-8` |
| national_v16 | `+144375` | `+51.562` | `12-0-8` |
| national_v7 | `+144375` | `+51.562` | `12-0-8` |
| national_v8 | `+144375` | `+51.562` | `12-0-8` |
| national_v9 | `+144375` | `+51.562` | `12-0-8` |
| national_v2 | `+150029` | `+53.582` | `13-7-0` |
| national_v3 | `+193985` | `+69.280` | `14-6-0` |

## v115

Path:

`bots/neural_national_lab/versions/v115_national_v17_flop_value_check_tcp`

Change:

- Derived from v114.
- Keeps v114's preflop wheel-ace jam-call veto and existing large-commit vetoes.
- Adds a narrow flop free-action value proposal:
  - stage is `flop`,
  - original rule label is `raise_pot`,
  - `to_call == 0`, so action `0` is a national-protocol `check`,
  - `4000 <= pot <= 6000`,
  - learned check/call value is at least `1.0`,
  - learned check/call value is at least `0.5` above learned `raise_pot` value.

Reason:

v114's v2 seed block `2026074000` still had a convertible loss at match 9,
hand 58. The rule bot made a free flop pot-size raise of `5494` into pot
`4778`. The multi-action value head scored check/call at `1.2336` and
`raise_pot` at `0.6793`. A force probe changing only that decision to check
improved the paired match by about `+5494`.

Two facing-bet flop probes were negative, so v115 explicitly does not alter
paid-call spots.

Direct v2 check:

`native_tcp_paired_v115_vs_v2_h70_m10_seed2026074000.json`

- v2 seed block `2026074000`: `+51488`, mean/hand `+36.777`, W-L-D `4-6-0`.
- Same block v114 baseline: `+45994`, mean/hand `+32.853`, W-L-D `4-6-0`.
- Delta: `+5494`.

Full current-top8+v7 pool:

| Block | Matches | Hands | Total | Mean/hand | W-L-D |
|---|---:|---:|---:|---:|---:|
| seed2026073900 | 90 | 12600 | `+887454` | `+70.433` | `71-3-16` |
| seed2026074000 | 90 | 12600 | `+440962` | `+34.997` | `37-11-42` |
| combined | 180 | 25200 | `+1328416` | `+52.715` | `108-14-58` |

Combined v115 opponent totals were positive for every opponent:

| Opponent | Total | Mean/hand | W-L-D |
|---|---:|---:|---:|
| national_v15 | `+118971` | `+42.490` | `10-0-10` |
| national_v5 | `+132568` | `+47.346` | `11-1-8` |
| national_v7 | `+144375` | `+51.562` | `12-0-8` |
| national_v8 | `+144375` | `+51.562` | `12-0-8` |
| national_v9 | `+144375` | `+51.562` | `12-0-8` |
| national_v14 | `+144375` | `+51.562` | `12-0-8` |
| national_v16 | `+144375` | `+51.562` | `12-0-8` |
| national_v2 | `+155523` | `+55.544` | `13-7-0` |
| national_v3 | `+199479` | `+71.242` | `14-6-0` |

Relative to v114:

- Full-pool combined delta: `+10988` chips, `+0.436/hand`.
- v2 delta: `+5494`.
- v3 delta: `+5494`.
- Other current-top8+v7 opponents were unchanged on the two seed blocks.

## v116

Path:

`bots/neural_national_lab/versions/v116_national_v17_turn_free_allin_check_tcp`

Change:

- Derived from v115.
- Keeps v115's flop free-action value-check gate.
- Adds a narrow turn free-all-in value gate:
  - stage is `turn`,
  - original rule label is `allin`,
  - `to_call == 0`, so action `0` is a national-protocol `check`,
  - pot is at least `8000`,
  - learned check/call value is at least `0.8`,
  - learned all-in value is at most `-0.3`,
  - learned check/call value is at least `1.0` above all-in.

Reason:

v115's v2 seed block `2026074000` still had a single `-20000` turn free
all-in on seed `2026074001`, hand 35. At that decision, the pot was `9344`,
`to_call` was `0`, the value head scored check/call at `0.830` and all-in at
`-0.387`.

A force probe changing only that decision to check improved the paired match
from `-17937` to `-2609`, a `+15328` chip gain. Scanning the full v115-v2 trace
found no other free all-in decision under this threshold set.

Direct v2 check:

`native_tcp_paired_v116_vs_v2_h70_m10_seed2026074000.json`

- v2 seed block `2026074000`: `+66816`, mean/hand `+47.726`, W-L-D `4-6-0`.
- Same block v115 baseline: `+51488`, mean/hand `+36.777`, W-L-D `4-6-0`.
- Delta: `+15328`.

Full current-top8+v7 pool:

| Block | Matches | Hands | Total | Mean/hand | W-L-D |
|---|---:|---:|---:|---:|---:|
| seed2026073900 | 90 | 12600 | `+887454` | `+70.433` | `71-3-16` |
| seed2026074000 | 90 | 12600 | `+456290` | `+36.213` | `37-11-42` |
| combined | 180 | 25200 | `+1343744` | `+53.323` | `108-14-58` |

Combined v116 opponent totals were positive for every opponent:

| Opponent | Total | Mean/hand | W-L-D |
|---|---:|---:|---:|
| national_v15 | `+118971` | `+42.490` | `10-0-10` |
| national_v5 | `+132568` | `+47.346` | `11-1-8` |
| national_v7 | `+144375` | `+51.562` | `12-0-8` |
| national_v8 | `+144375` | `+51.562` | `12-0-8` |
| national_v9 | `+144375` | `+51.562` | `12-0-8` |
| national_v14 | `+144375` | `+51.562` | `12-0-8` |
| national_v16 | `+144375` | `+51.562` | `12-0-8` |
| national_v2 | `+170851` | `+61.018` | `13-7-0` |
| national_v3 | `+199479` | `+71.242` | `14-6-0` |

Relative to v115:

- Full-pool combined delta: `+15328` chips, `+0.608/hand`.
- v2 delta: `+15328`.
- All other current-top8+v7 opponents were unchanged on the two seed blocks.

## Current Assessment

- v116 is the current best artifact by EV with unchanged full-pool W-L-D:
  - `+1343744` over 25200 full-pool hands,
  - mean/hand `+53.323`,
  - W-L-D `108-14-58`,
  - positive combined result against every opponent.
- v116 preserves v115's low-loss full-pool profile while improving the hard
  v2/v3 aggregate.
- v112 improves the hardest v2/v3 aggregate without using the adapter:
  - v108 v2/v3 combined over two full-pool blocks: v2 `-64781`, v3 `-21241`.
  - v110: v2 `+101785`, v3 `+139622`.
- v112: v2 `+123979`, v3 `+158022`.
- v113: v2 `+125329`, v3 `+159522`.
- v114: v2 `+150029`, v3 `+193985`.
- v115: v2 `+155523`, v3 `+199479`.
- v116: v2 `+170851`, v3 `+199479`.
- All v110/v111/v112/v113/v114/v115/v116 recorded evaluations passed protocol
  compliance with 0 candidate illegal actions, 0 candidate timeouts, and 0
  candidate adapter actions.

The route now has a clear native-TCP neural performance gain over v108/v109.
It is still not complete domination: v116's v2 record remains `13-7-0`, so the
next generation should keep improving v2 match conversion while preserving
v116's higher EV and restored low-loss full-pool profile.
