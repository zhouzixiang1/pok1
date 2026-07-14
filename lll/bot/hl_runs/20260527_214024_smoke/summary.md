# HL Run Summary

- Verdict: `RED`
- Candidate: `1.py`
- Seed: `123`
- Mirror groups per baseline: `1`

## Matchups

| Baseline | W-L-D | Avg mirror chips | Avg logged chips | Log |
| --- | ---: | ---: | ---: | --- |
| `bots\bot001\main.py` | 0-1-0 | -662.0 | -331.0 | `matchups\current_vs_bot001.json` |

## Signals

- `branch:lock_win_fold`: 33
- `branch:preflop_spot_action`: 29
- `trace_anti_lock_pressure`: 16
- `branch:check_or_pot_control`: 13
- `branch:call_defense`: 10
- `branch:fold_defense`: 9
- `branch:value_or_pressure_raise`: 9
- `branch:anti_lock_preflop_attack`: 8
- `candidate_high_loss_hand`: 3
- `branch:probe_raise`: 3
- `trace_probe_aggressive_bluff_line`: 3
- `trace_probe`: 2

## Interesting Hands

- `trace_probe` game=0 step=119 action=772
- `candidate_high_loss_hand` game=1 step=24 action={'round': 3, 'player_id': 0, 'action': 0, 'action_type': 'check'}
- `trace_probe_aggressive_bluff_line` game=1 step=69 action=169
- `trace_probe_aggressive_bluff_line` game=1 step=77 action=100
- `candidate_high_loss_hand` game=1 step=92 action={'player_id': 0, 'action': -1, 'action_type': 'fold'}
- `trace_probe_aggressive_bluff_line` game=1 step=129 action=163
- `trace_probe` game=1 step=135 action=0
- `candidate_high_loss_hand` game=1 step=140 action={'round': 3, 'player_id': 0, 'action': 0, 'action_type': 'call'}
- `trace_anti_lock_pressure` game=1 step=141 action=638
- `trace_anti_lock_pressure` game=1 step=147 action=307
- `trace_anti_lock_pressure` game=1 step=153 action=638
- `trace_anti_lock_pressure` game=1 step=159 action=273
