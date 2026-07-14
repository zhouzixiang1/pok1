# HL Run Summary

- Verdict: `GREEN`
- Candidate: `1.py`
- Seed: `2026052701`
- Mirror groups per baseline: `3`

## Matchups

| Baseline | Slot | W-L-D | Avg mirror chips | Avg logged chips | Log |
| --- | ---: | ---: | ---: | ---: | --- |
| `bots\bot001\main.py` | P0 | 3-0-0 | 515.7 | 257.8 | `matchups\current_forward_bot001.json` |
| `bots\bot001\main.py` | P1 | 1-1-1 | -8.0 | -4.0 | `matchups\current_reverse_bot001.json` |

## Signals

- `branch:preflop_spot_action`: 241
- `branch:lock_win_fold`: 153
- `branch:check_or_pot_control`: 133
- `branch:fold_defense`: 122
- `branch:call_defense`: 104
- `trace_anti_lock_pressure`: 100
- `branch:value_or_pressure_raise`: 71
- `branch:probe_raise`: 47
- `trace_probe_aggressive_bluff_line`: 47
- `branch:anti_lock_preflop_attack`: 26
- `branch:anti_lock_pressure`: 25
- `candidate_high_loss_hand`: 10

## Interesting Hands

- `trace_probe_aggressive_bluff_line` game=0 step=79 action=100
- `trace_probe_aggressive_bluff_line` game=0 step=105 action=166
- `trace_anti_lock_pressure` game=0 step=121 action=-2
- `candidate_high_loss_hand` game=0 step=122 action={'round': 1, 'player_id': 0, 'action': -2, 'action_type': 'allin'}
- `trace_anti_lock_pressure` game=0 step=125 action=288
- `trace_anti_lock_pressure` game=0 step=131 action=638
- `trace_anti_lock_pressure` game=0 step=137 action=327
- `trace_anti_lock_pressure` game=0 step=143 action=570
- `trace_anti_lock_pressure` game=0 step=149 action=286
- `trace_anti_lock_pressure` game=0 step=155 action=259
- `trace_anti_lock_pressure` game=0 step=161 action=286
- `trace_anti_lock_pressure` game=0 step=167 action=307
