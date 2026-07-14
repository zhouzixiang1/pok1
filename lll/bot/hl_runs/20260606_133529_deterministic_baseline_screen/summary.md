# HL 运行摘要

- 结论：`GREEN`
- 候选 bot：`1.py`
- 随机种子：`2026360601`
- 每个 baseline 的镜像组数：`5`
- 每个 baseline 的实际比赛场数：`10`
- 每场手牌数：`70`

## 对战

| Baseline | 候选座位 | 胜-负-平 | 平均镜像筹码差 | 平均日志筹码差 | 日志 |
| --- | ---: | ---: | ---: | ---: | --- |
| `bots\bot4.py` | P0 | 3-2-0 | -860.2 | -430.1 | `matchups\current_mirror_bot4.json` |
| `bots\bot5.py` | P0 | 2-3-0 | 3381.8 | 1690.9 | `matchups\current_mirror_bot5.json` |

## 日志信号

- `branch:lock_win_fold`: 460
- `branch:preflop_spot_action`: 458
- `branch:check_or_pot_control`: 272
- `branch:fold_defense`: 230
- `branch:call_defense`: 210
- `trace_anti_lock_pressure`: 183
- `branch:value_or_pressure_raise`: 156
- `branch:probe_raise`: 101
- `trace_probe_aggressive_bluff_line`: 101
- `branch:anti_lock_preflop_attack`: 50
- `branch:anti_lock_pressure`: 32
- `candidate_high_loss_hand`: 23

## 值得复盘的手牌

- `trace_probe_aggressive_bluff_line` game=0 step=11 action=169
- `trace_probe_aggressive_bluff_line` game=0 step=119 action=163
- `trace_probe_aggressive_bluff_line` game=0 step=125 action=260
- `trace_anti_lock_pressure` game=0 step=149 action=-2
- `candidate_high_loss_hand` game=0 step=152 action={'round': 2, 'player_id': 1, 'action': -2, 'action_type': 'allin'}
- `trace_anti_lock_pressure` game=0 step=153 action=286
- `trace_anti_lock_pressure` game=0 step=159 action=327
- `trace_anti_lock_pressure` game=0 step=165 action=290
- `trace_anti_lock_pressure` game=0 step=171 action=327
- `trace_anti_lock_pressure` game=0 step=177 action=638
- `trace_anti_lock_pressure` game=0 step=183 action=638
- `trace_anti_lock_pressure` game=0 step=189 action=327
