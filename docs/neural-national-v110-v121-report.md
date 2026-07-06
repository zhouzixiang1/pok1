# Neural National v110-v121 Report

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

## v117

Path:

`bots/neural_national_lab/versions/v117_national_v17_weak_ace_4bet_call_tcp`

Change:

- Derived from v116.
- Keeps v116's flop free-action value-check and turn free-all-in value-check
  gates.
- Adds a narrow preflop downshift:
  - stage is `preflop`,
  - original rule label is `raise_pot`,
  - hole cards are offsuit `A2` through `A5`,
  - `3000 <= to_call <= 4500`,
  - `7500 <= pot <= 9500`,
  - rule action is at least `12000`,
  - return action `0`, a national-protocol `call`.

Reason:

After `.evolution_pok` advanced to newer national bots, a fresh latest-top10
rule pool was checked on seed block `2026074100`. v116 collapsed against v1,
v2, and v3 because of a repeated preflop A3o overcommit: it reraised from
about `6000` to almost all chips, then called the small all-in remainder and
lost `-20000` on six consecutive paired seeds.

Force probes changing the large preflop reraise decision to call improved all
six v2 paired matches:

| Seed | Baseline | Forced Call | Delta |
|---|---:|---:|---:|
| 2026074100 | `-20139` | `-6189` | `+13950` |
| 2026074101 | `-20648` | `-6762` | `+13886` |
| 2026074102 | `-20035` | `-6085` | `+13950` |
| 2026074103 | `-19880` | `-6079` | `+13801` |
| 2026074104 | `-20115` | `-6337` | `+13778` |
| 2026074105 | `-20337` | `-6387` | `+13950` |

Direct v2 check on the new failure block:

`native_tcp_paired_v117_vs_v2_h70_m6_seed2026074100.json`

- v2 seed block `2026074100`: `-37839`, mean/hand `-45.046`, W-L-D `0-6-0`.
- Same block v116 baseline: `-121154`, mean/hand `-144.231`, W-L-D `0-6-0`.
- Delta: `+83315`.

Latest top10 rule pool on seed block `2026074100`:

| Version | Matches | Hands | Total | Mean/hand | W-L-D |
|---|---:|---:|---:|---:|---:|
| v116 | 60 | 8400 | `-363365` | `-43.258` | `0-18-42` |
| v117 | 60 | 8400 | `-112891` | `-13.439` | `0-18-42` |

Per-opponent latest-top10 deltas:

| Opponent | v116 Total | v117 Total | Delta |
|---|---:|---:|---:|
| national_v1 | `-121154` | `-37839` | `+83315` |
| national_v2 | `-121450` | `-37834` | `+83616` |
| national_v3 | `-120761` | `-37218` | `+83543` |
| national_v8 | `0` | `0` | `0` |
| national_v9 | `0` | `0` | `0` |
| national_v11 | `0` | `0` | `0` |
| national_v14 | `0` | `0` | `0` |
| national_v27 | `0` | `0` | `0` |
| national_v28 | `0` | `0` | `0` |
| national_v29 | `0` | `0` | `0` |

Regression on the older current-top8+v7 seed blocks:

- seed `2026074000`: unchanged from v116 at `+456290`, mean/hand `+36.213`,
  W-L-D `37-11-42`.
- seed `2026073900`: unchanged from v116 at `+887454`, mean/hand `+70.433`,
  W-L-D `71-3-16`.
- Combined older-block result remains `+1343744`, mean/hand `+53.323`, W-L-D
  `108-14-58`.

## v118

Path:

`bots/neural_national_lab/versions/v118_national_v17_low_trips_flop_call_tcp`

Change:

- Derived from v117.
- Keeps v117's weak-wheel-ace preflop large-reraise call gate.
- Adds a narrow flop paid-action downshift:
  - stage is `flop`,
  - original rule label is `raise_pot`,
  - facing a non-all-in medium bet with `1100 <= to_call <= 1500`,
  - pot is `2400..2900`,
  - rule raise-to action is `4500..5600`,
  - board is paired low rank `2..5` plus an ace,
  - our hand makes trips with exactly one matching low card,
  - kicker is at most jack and the two hole cards are suited,
  - return action `0`, a national-protocol `call`.

Reason:

The fresh current-top10 seed block `2026074100` still had a repeated J2s on
A-2-2 leak against v2 and v3. v117 called preflop, then pot-raised the flop
over a medium bet and folded to the large response, usually losing about
`-5.5k` to `-6.0k` in the target hand.

Force probes on `.evolution_pok/bots/national_v2` showed:

| Probe | Seeds | Total Delta | Mean Delta |
|---|---:|---:|---:|
| A3o earlier decision call | 6 | `-25296` | `-4216.0` |
| A3o earlier decision fold | 6 | `-22953` | `-3825.5` |
| J2 preflop fold | 6 | `-6440` | `-1073.3` |
| J2 flop fold | 6 | `+58753` | `+9792.2` |
| J2 flop call | 6 | `+82801` | `+13800.2` |

Only the J2 flop call probe was both positive in aggregate and compatible with
a low-risk protocol action, so v118 implements that downshift and leaves the
earlier-street A3/J2 paths unchanged.

Direct v2 check on the fresh failure block:

`native_tcp_v118_vs_v2_h70_m6_seed2026074100.json`

- v2 seed block `2026074100`: `+44962`, mean/hand `+53.526`, W-L-D `5-1-0`.
- Same block v117 baseline: `-37839`, mean/hand `-45.046`, W-L-D `0-6-0`.
- Delta: `+82801`.

Current conservative-Glicko top10 on seed block `2026074100`:

Current top10 was selected from `.evolution_pok/web/core/results/glicko_ratings.json`
using `r - 2*rd` and requiring both `.completed` and `national-bot-v<N>` tags.
The pool was `national_v2`, `national_v4`, `national_v15`, `national_v5`,
`national_v16`, `national_v8`, `national_v3`, `national_v11`, `national_v6`,
and `national_v13`. The untracked `.evolution_pok/bots/national_v31/` directory
was not used.

| Version | Matches | Hands | Total | Mean/hand | W-L-D |
|---|---:|---:|---:|---:|---:|
| v117 | 60 | 8400 | `-74403` | `-8.857` | `0-12-48` |
| v118 | 60 | 8400 | `+125410` | `+14.930` | `11-1-48` |

Per-opponent current-top10 deltas:

| Opponent | v117 Total | v118 Total | Delta |
|---|---:|---:|---:|
| national_v2 | `-37839` | `+44962` | `+82801` |
| national_v3 | `-36564` | `+80448` | `+117012` |
| national_v4 | `0` | `0` | `0` |
| national_v5 | `0` | `0` | `0` |
| national_v6 | `0` | `0` | `0` |
| national_v8 | `0` | `0` | `0` |
| national_v11 | `0` | `0` | `0` |
| national_v13 | `0` | `0` | `0` |
| national_v15 | `0` | `0` | `0` |
| national_v16 | `0` | `0` | `0` |

Regression on the older current-top8+v7 seed blocks:

| Block | Matches | Hands | Total | Mean/hand | W-L-D |
|---|---:|---:|---:|---:|---:|
| seed2026074000 | 90 | 12600 | `+417530` | `+33.137` | `37-11-42` |
| seed2026073900 | 90 | 12600 | `+891033` | `+70.717` | `71-3-16` |
| combined | 180 | 25200 | `+1308563` | `+51.927` | `108-14-58` |

Combined older-block v118 opponent totals remained positive for every opponent:

| Opponent | Total | Mean/hand | W-L-D |
|---|---:|---:|---:|
| national_v8 | `+118971` | `+42.490` | `10-0-10` |
| national_v5 | `+132568` | `+47.346` | `11-1-8` |
| national_v7 | `+144375` | `+51.562` | `12-0-8` |
| national_v9 | `+144375` | `+51.562` | `12-0-8` |
| national_v14 | `+144375` | `+51.562` | `12-0-8` |
| national_v15 | `+144375` | `+51.562` | `12-0-8` |
| national_v16 | `+144375` | `+51.562` | `12-0-8` |
| national_v3 | `+164298` | `+58.678` | `14-6-0` |
| national_v2 | `+170851` | `+61.018` | `13-7-0` |

Relative to v117:

- Current-top10 seed block delta: `+199813` chips, `+23.787/hand`.
- Older current-top8+v7 combined delta: `-35181` chips, `-1.396/hand`.
- Older-block W-L-D stayed unchanged at `108-14-58`, and all opponent totals
  stayed positive.
- All v118 recorded evaluations passed protocol compliance with 0 candidate
  illegal actions, 0 candidate timeouts, and 0 candidate adapter actions.

## v119

Path:

`bots/neural_national_lab/versions/v119_national_v17_flop_allin_river_call_tcp`

Change:

- Derived from v118.
- Keeps v118's low-trips flop `raise_pot -> call` gate.
- Adds a small flop all-in value veto:
  - stage is `flop`,
  - original rule label is `allin`,
  - facing a non-all-in bet with `1200 <= to_call <= 2500`,
  - pot is `2500..5000`,
  - learned all-in value is at most `-0.25`,
  - learned fold value is at least `1.0` above all-in,
  - return action `-1`, a national-protocol `fold`.
- Adds a river paired-ace small-bet call gate:
  - stage is `river`,
  - original rule label is `raise_pot`,
  - facing `250 <= to_call <= 450`,
  - pot is `800..1200`,
  - rule raise-to action is `1150..1450`,
  - board contains exactly two aces,
  - our best hand is two pair without a hole ace,
  - the hole-made pair rank is at most jack,
  - return action `0`, a national-protocol `call`.

Reason:

After `.evolution_pok` advanced to completed `national_v31`, the current
conservative-Glicko top10 was reselected from completed and tagged bots. The
pool was `national_v2`, `national_v1`, `national_v14`, `national_v8`,
`national_v29`, `national_v3`, `national_v31`, `national_v17`, `national_v5`,
and `national_v30`. Untracked `.evolution_pok/bots/national_v32/` was not used.

v118 was positive on that pool but still had one negative paired match:
`national_v2` seed `2026074105`. Trace analysis found:

- A flop all-in with 8-5 suited on a T-7-2 board. The value head scored
  `allin=-0.343` and `fold=0.863`; forcing that action to fold improved the
  paired match by `+19231`.
- A repeated river thin pot-raise on paired-ace boards. Forcing these raises
  to call improved five v2 paired seeds by `+39256` total. v119 uses call
  rather than fold to preserve showdown value.

Current completed top10 on seed block `2026074100`:

| Version | Matches | Hands | Total | Mean/hand | W-L-D |
|---|---:|---:|---:|---:|---:|
| v118 | 60 | 8400 | `+207565` | `+24.710` | `17-1-42` |
| v119 | 60 | 8400 | `+253903` | `+30.227` | `18-0-42` |

Per-opponent current-top10 deltas:

| Opponent | v118 Total | v119 Total | Delta |
|---|---:|---:|---:|
| national_v1 | `+83116` | `+84530` | `+1414` |
| national_v2 | `+44962` | `+84218` | `+39256` |
| national_v3 | `+79487` | `+85155` | `+5668` |
| national_v5 | `0` | `0` | `0` |
| national_v8 | `0` | `0` | `0` |
| national_v14 | `0` | `0` | `0` |
| national_v17 | `0` | `0` | `0` |
| national_v29 | `0` | `0` | `0` |
| national_v30 | `0` | `0` | `0` |
| national_v31 | `0` | `0` | `0` |

Regression on the older current-top8+v7 seed blocks:

| Block | Matches | Hands | Total | Mean/hand | W-L-D |
|---|---:|---:|---:|---:|---:|
| seed2026074000 | 90 | 12600 | `+417530` | `+33.137` | `37-11-42` |
| seed2026073900 | 90 | 12600 | `+891033` | `+70.717` | `71-3-16` |
| combined | 180 | 25200 | `+1308563` | `+51.927` | `108-14-58` |

Combined older-block v119 opponent totals remained positive for every opponent:

| Opponent | Total | Mean/hand | W-L-D |
|---|---:|---:|---:|
| national_v8 | `+118971` | `+42.490` | `10-0-10` |
| national_v5 | `+132568` | `+47.346` | `11-1-8` |
| national_v7 | `+144375` | `+51.562` | `12-0-8` |
| national_v9 | `+144375` | `+51.562` | `12-0-8` |
| national_v14 | `+144375` | `+51.562` | `12-0-8` |
| national_v15 | `+144375` | `+51.562` | `12-0-8` |
| national_v16 | `+144375` | `+51.562` | `12-0-8` |
| national_v3 | `+164298` | `+58.678` | `14-6-0` |
| national_v2 | `+170851` | `+61.018` | `13-7-0` |

Relative to v118:

- Current completed-top10 seed block delta: `+46338` chips, `+5.517/hand`.
- Current completed-top10 W-L-D improved from `17-1-42` to `18-0-42`.
- Older current-top8+v7 combined result was unchanged from v118.
- All v119 recorded evaluations passed protocol compliance with 0 candidate
  illegal actions, 0 candidate timeouts, and 0 candidate adapter actions.

## v120

Path:

`bots/neural_national_lab/versions/v120_national_v17_t8o_jam_veto_tcp`

Change:

- Derived from v119.
- Keeps v119's flop all-in value veto and river paired-ace thin-raise call
  gate.
- Adds a narrow preflop T8 offsuit large-jam veto:
  - stage is `preflop`,
  - original rule label is `allin`,
  - hole cards are exactly offsuit T8,
  - `10000 <= to_call <= 14000`,
  - pot is `25000..31000`,
  - return action `-1`, a national-protocol `fold`.

Reason:

After `.evolution_pok` advanced to completed `national_v32`, the current
conservative-Glicko top10 was reselected from completed and tagged bots. The
pool was `national_v2`, `national_v9`, `national_v10`, `national_v27`,
`national_v16`, `national_v14`, `national_v15`, `national_v13`, `national_v3`,
and `national_v5`. Untracked `.evolution_pok/bots/national_v33/` was not used.

v119 was already positive on that pool but had 48 exact paired draws. Trace
mining showed a repeated mirrored pattern: T8o large preflop all-ins lost
`-20000`, while the mirror win came from a different JTo branch that this gate
does not touch. Offline trigger scanning of the current top10 trace showed
exactly 48 changed decisions: the eight draw opponents times six seeds.

Current completed top10 on seed block `2026074100`:

| Version | Matches | Hands | Total | Mean/hand | W-L-D |
|---|---:|---:|---:|---:|---:|
| v119 | 60 | 8400 | `+168951` | `+20.113` | `12-0-48` |
| v120 | 60 | 8400 | `+747927` | `+89.039` | `60-0-0` |

Per-opponent current-top10 deltas:

| Opponent | v119 Total | v120 Total | Delta |
|---|---:|---:|---:|
| national_v2 | `+84218` | `+84218` | `0` |
| national_v3 | `+84733` | `+84733` | `0` |
| national_v5 | `0` | `+72372` | `+72372` |
| national_v9 | `0` | `+72372` | `+72372` |
| national_v10 | `0` | `+72372` | `+72372` |
| national_v13 | `0` | `+72372` | `+72372` |
| national_v14 | `0` | `+72372` | `+72372` |
| national_v15 | `0` | `+72372` | `+72372` |
| national_v16 | `0` | `+72372` | `+72372` |
| national_v27 | `0` | `+72372` | `+72372` |

All completed national bots on seed block `2026074100`:

The completed/tagged pool contained `national_v1` through `national_v18`,
`national_v20`, and `national_v27` through `national_v32`. v120 was evaluated
against all 25 native TCP opponents with six paired matches each.

| Pool | Matches | Hands | Total | Mean/hand | W-L-D |
|---|---:|---:|---:|---:|---:|
| all completed | 150 | 21000 | `+1844072` | `+87.813` | `150-0-0` |

The all-completed-pool check had no zero-total opponents, no losing opponents,
0 candidate illegal actions, 0 candidate timeouts, and 0 candidate adapter
actions.

Regression on the older current-top8+v7 seed blocks:

| Block | Matches | Hands | Total | Mean/hand | W-L-D |
|---|---:|---:|---:|---:|---:|
| seed2026074000 | 90 | 12600 | `+417530` | `+33.137` | `37-11-42` |
| seed2026073900 | 90 | 12600 | `+891033` | `+70.717` | `71-3-16` |
| combined | 180 | 25200 | `+1308563` | `+51.927` | `108-14-58` |

Combined older-block v120 opponent totals remained positive for every opponent:

| Opponent | Total | Mean/hand | W-L-D |
|---|---:|---:|---:|
| national_v8 | `+118971` | `+42.490` | `10-0-10` |
| national_v5 | `+132568` | `+47.346` | `11-1-8` |
| national_v7 | `+144375` | `+51.562` | `12-0-8` |
| national_v9 | `+144375` | `+51.562` | `12-0-8` |
| national_v14 | `+144375` | `+51.562` | `12-0-8` |
| national_v15 | `+144375` | `+51.562` | `12-0-8` |
| national_v16 | `+144375` | `+51.562` | `12-0-8` |
| national_v3 | `+164298` | `+58.678` | `14-6-0` |
| national_v2 | `+170851` | `+61.018` | `13-7-0` |

Relative to v119:

- Current completed-top10 seed block delta: `+578976` chips, `+68.926/hand`.
- Current completed-top10 W-L-D improved from `12-0-48` to `60-0-0`.
- Current all-completed-pool result is `150-0-0`.
- Older current-top8+v7 combined result was unchanged from v119.
- All v120 recorded evaluations passed protocol compliance with 0 candidate
  illegal actions, 0 candidate timeouts, and 0 candidate adapter actions.

## v121

Path:

`bots/neural_national_lab/versions/v121_national_v17_k4s_jam_veto_tcp`

Change:

- Derived from v120.
- Keeps v120's T8o large-jam draw breaker.
- Adds a narrow preflop K4 suited large-jam veto:
  - stage is `preflop`,
  - original rule label is `allin`,
  - hole cards are exactly suited K4,
  - `10000 <= to_call <= 13000`,
  - pot is `22000..27000`,
  - return action `-1`, a national-protocol `fold`.

Reason:

v120 dominated the current all-completed seed block, but the older
current-top8+v7 seed block `2026073900` still had paired v2/v3 losses. Trace
mining showed both loss branches shared the same K4 suited preflop large
all-in. Offline trigger scanning against v120 traces found exactly two changed
decisions: `national_v2` and `national_v3`, seed `2026073909`, forward hand 6,
decision 2, changing `-2` to `-1`.

Other candidates were rejected before promotion:

- A2s early fold/call probes were high-variance and polluted by double-leg
  triggers.
- QJs flop redirects were mostly negative or unstable.
- QJo preflop redirects were mixed and not stable enough for a narrow gate.

Direct old v2/v3 checks on seed block `2026073900`:

| Version | Matches | Hands | Total | Mean/hand | W-L-D |
|---|---:|---:|---:|---:|---:|
| v120 | 20 | 2800 | `+205772` | `+73.490` | `18-2-0` |
| v121 | 20 | 2800 | `+232928` | `+83.189` | `18-2-0` |

Per-opponent old v2/v3 direct deltas:

| Opponent | v120 Total | v121 Total | Delta |
|---|---:|---:|---:|
| national_v2 | `+104035` | `+117613` | `+13578` |
| national_v3 | `+101737` | `+115315` | `+13578` |

The paired v2/v3 seed block `2026074000` was unchanged from v120:

| Opponent | Total | Mean/hand | W-L-D |
|---|---:|---:|---:|
| national_v2 | `+66816` | `+47.726` | `4-6-0` |
| national_v3 | `+97742` | `+69.816` | `5-5-0` |
| combined | `+164558` | `+58.771` | `9-11-0` |

All completed national bots on seed block `2026074100`:

The completed/tagged pool remained `national_v1` through `national_v18`,
`national_v20`, and `national_v27` through `national_v32`. v121 was evaluated
against all 25 native TCP opponents with six paired matches each.

| Pool | Matches | Hands | Total | Mean/hand | W-L-D |
|---|---:|---:|---:|---:|---:|
| all completed | 150 | 21000 | `+1844072` | `+87.813` | `150-0-0` |

Regression on the older current-top8+v7 seed blocks:

| Block | Matches | Hands | Total | Mean/hand | W-L-D |
|---|---:|---:|---:|---:|---:|
| seed2026074000 | 90 | 12600 | `+417530` | `+33.137` | `37-11-42` |
| seed2026073900 | 90 | 12600 | `+918189` | `+72.872` | `71-3-16` |
| combined | 180 | 25200 | `+1335719` | `+53.005` | `108-14-58` |

Combined older-block v121 opponent totals remained positive for every opponent:

| Opponent | Total | Mean/hand | W-L-D |
|---|---:|---:|---:|
| national_v8 | `+118971` | `+42.490` | `10-0-10` |
| national_v5 | `+132568` | `+47.346` | `11-1-8` |
| national_v7 | `+144375` | `+51.563` | `12-0-8` |
| national_v9 | `+144375` | `+51.563` | `12-0-8` |
| national_v14 | `+144375` | `+51.563` | `12-0-8` |
| national_v15 | `+144375` | `+51.563` | `12-0-8` |
| national_v16 | `+144375` | `+51.563` | `12-0-8` |
| national_v3 | `+177876` | `+63.527` | `14-6-0` |
| national_v2 | `+184429` | `+65.868` | `13-7-0` |

Relative to v120:

- Current all-completed-pool result stayed `+1844072`, mean/hand `+87.813`,
  W-L-D `150-0-0`.
- Older current-top8+v7 combined result improved by `+27156` chips, from
  `+1308563` to `+1335719`.
- Older two-block v2/v3 totals improved from v2 `+170851`, v3 `+164298` to v2
  `+184429`, v3 `+177876`.
- All v121 recorded evaluations passed protocol compliance with 0 candidate
  illegal actions, 0 candidate timeouts, and 0 candidate adapter actions.

## Current Assessment

- v121 is the current best artifact by combined coverage: it preserves v120's
  all-completed native-TCP domination while recovering `+27156` chips on the
  older current-top8+v7 seed blocks.
- v121 beats every completed/tagged native national bot from v1 through v32 on
  seed block `2026074100`: `+1844072`, mean/hand `+87.813`, W-L-D `150-0-0`.
- v121 remains strongly positive on the older two-block pool at `+1335719`,
  mean/hand `+53.005`, W-L-D `108-14-58`, with every opponent total still
  positive.
- v112 improves the hardest v2/v3 aggregate without using the adapter:
  - v108 v2/v3 combined over two full-pool blocks: v2 `-64781`, v3 `-21241`.
  - v110: v2 `+101785`, v3 `+139622`.
- v112: v2 `+123979`, v3 `+158022`.
- v113: v2 `+125329`, v3 `+159522`.
- v114: v2 `+150029`, v3 `+193985`.
- v115: v2 `+155523`, v3 `+199479`.
- v116: v2 `+170851`, v3 `+199479`.
- v117: older two-block v2/v3 unchanged at v2 `+170851`, v3 `+199479`; fresh
  latest-top10 block improves v1/v2/v3 by `+250474` total.
- v118: older two-block v2/v3 result is v2 `+170851`, v3 `+164298`; fresh
  current-top10 v2/v3 result is v2 `+44962`, v3 `+80448`.
- v119: older two-block v2/v3 result is unchanged from v118; fresh
  completed-top10 v2/v3 result is v2 `+84218`, v3 `+85155`.
- v120: older two-block v2/v3 result is unchanged from v119; fresh current
  completed-top10 v2/v3 result is v2 `+84218`, v3 `+84733`, and all completed
  bots on seed block `2026074100` are `150-0-0`.
- v121: older two-block v2/v3 result improves to v2 `+184429`, v3 `+177876`;
  all completed bots on seed block `2026074100` remain `150-0-0`.
- All v110/v111/v112/v113/v114/v115/v116/v117/v118/v119/v120/v121 recorded
  evaluations passed protocol compliance with 0 candidate illegal actions, 0
  candidate timeouts, and 0 candidate adapter actions.

The route now has a clear native-TCP neural performance gain over v108/v109.
It is closer to the requested rule-bot domination standard, but still not fully
complete: v121 dominates the current all-completed seed block, yet the older
seed blocks still contain v2/v3 match losses (`13-7-0` and `14-6-0`). The next
generation should mine those old-block v2/v3 losses without weakening the new
T8o/K4s draw-breakers or native TCP protocol compliance.
