# HL Run Summary

- Verdict: `RED`
- Candidate: `1.py`
- Seed: `20260527`
- Mirror groups per baseline: `10`

## Matchups

| Baseline | Slot | W-L-D | Avg mirror chips | Avg logged chips | Log |
| --- | ---: | ---: | ---: | ---: | --- |
| `bots\bot001\main.py` | P0 | 3-5-2 | -1919.5 | -959.8 | `matchups\current_vs_bot001.json` |

## Signals

- `branch:preflop_spot_action`: 362
- `branch:lock_win_fold`: 294
- `trace_anti_lock_pressure`: 175
- `branch:fold_defense`: 169
- `branch:check_or_pot_control`: 163
- `branch:call_defense`: 143
- `branch:value_or_pressure_raise`: 100
- `branch:probe_raise`: 42
- `trace_probe_aggressive_bluff_line`: 42
- `branch:anti_lock_pressure`: 42
- `branch:anti_lock_preflop_attack`: 36
- `candidate_high_loss_hand`: 13

## Interesting Hands

- `trace_probe_aggressive_bluff_line` game=0 step=11 action=180
- `trace_probe_aggressive_bluff_line` game=0 step=59 action=150
- `trace_probe` game=0 step=65 action=0
- `trace_probe_aggressive_bluff_line` game=0 step=75 action=100
- `trace_probe_aggressive_bluff_line` game=0 step=109 action=330
- `candidate_high_loss_hand` game=0 step=112 action={'round': 3, 'player_id': 1, 'action': 0, 'action_type': 'call'}
- `trace_anti_lock_pressure` game=0 step=149 action=-2
- `trace_probe_aggressive_bluff_line` game=1 step=49 action=138
- `trace_probe_aggressive_bluff_line` game=1 step=91 action=100
- `trace_probe_aggressive_bluff_line` game=1 step=101 action=173
- `trace_probe_aggressive_bluff_line` game=1 step=137 action=143
- `trace_anti_lock_pressure` game=1 step=165 action=-2
