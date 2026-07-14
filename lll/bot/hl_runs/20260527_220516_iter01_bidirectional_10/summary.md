# HL Run Summary

- Verdict: `YELLOW`
- Candidate: `1.py`
- Seed: `2026052702`
- Mirror groups per baseline: `10`
- Control run: candidate and baseline hashes match; use this as harness calibration, not merge evidence.

## Matchups

| Baseline | Slot | W-L-D | Avg mirror chips | Avg logged chips | Log |
| --- | ---: | ---: | ---: | ---: | --- |
| `bots\bot001\main.py` | P0 | 6-2-2 | 1651.6 | 825.8 | `matchups\current_forward_bot001.json` |
| `bots\bot001\main.py` | P1 | 2-8-0 | -571.5 | -285.8 | `matchups\current_reverse_bot001.json` |

## Signals

- `branch:preflop_spot_action`: 789
- `branch:lock_win_fold`: 513
- `branch:fold_defense`: 399
- `branch:check_or_pot_control`: 380
- `branch:call_defense`: 343
- `trace_anti_lock_pressure`: 303
- `branch:value_or_pressure_raise`: 243
- `branch:probe_raise`: 103
- `trace_probe_aggressive_bluff_line`: 103
- `branch:anti_lock_pressure`: 81
- `branch:anti_lock_preflop_attack`: 72
- `candidate_high_loss_hand`: 36

## Interesting Hands

- `candidate_high_loss_hand` game=0 step=14 action={'player_id': 0, 'action': -1, 'action_type': 'fold'}
- `trace_probe` game=0 step=45 action=412
- `trace_probe_aggressive_bluff_line` game=0 step=71 action=416
- `trace_probe_aggressive_bluff_line` game=0 step=79 action=100
- `trace_probe_aggressive_bluff_line` game=0 step=89 action=166
- `trace_probe_aggressive_bluff_line` game=0 step=95 action=258
- `trace_probe_aggressive_bluff_line` game=0 step=117 action=154
- `candidate_high_loss_hand` game=0 step=128 action={'round': 3, 'player_id': 0, 'action': 0, 'action_type': 'call'}
- `trace_anti_lock_pressure` game=0 step=129 action=322
- `trace_anti_lock_pressure` game=0 step=135 action=308
- `trace_anti_lock_pressure` game=0 step=141 action=638
- `trace_anti_lock_pressure` game=0 step=147 action=290
