# Neural National v110-v112 Report

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

## Current Assessment

- v110 is the cleaner stability artifact: lower total EV than v112, but much
  better match W-L-D (`107-15-58` over two full-pool blocks).
- v112 is the highest measured EV artifact: `+1239515` over 25200 full-pool
  hands and positive against every opponent, but it trades many prior draws for
  small losses (`107-50-23`).
- v112 improves the hardest v2/v3 aggregate without using the adapter:
  - v108 v2/v3 combined over two full-pool blocks: v2 `-64781`, v3 `-21241`.
  - v110: v2 `+101785`, v3 `+139622`.
  - v112: v2 `+123979`, v3 `+158022`.
- All v110/v111/v112 recorded evaluations passed protocol compliance with 0
  candidate illegal actions, 0 candidate timeouts, and 0 candidate adapter
  actions.

The route now has a clear native-TCP neural performance gain over v108/v109.
It is still not complete domination: v112's W-L-D regression and v2's remaining
`12-8-0` match record mean the next generation should reduce small-loss
frequency while preserving the higher v2/v3 EV.
