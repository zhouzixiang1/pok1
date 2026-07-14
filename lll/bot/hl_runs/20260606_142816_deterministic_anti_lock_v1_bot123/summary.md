# HL 运行摘要

- 结论：`GREEN`
- 候选 bot：`bots\experiments\anti_lock_v1.py`
- 随机种子：`2026360601`
- 每个 baseline 的镜像组数：`5`
- 每个 baseline 的实际比赛场数：`10`
- 每场手牌数：`70`

## 对战

| Baseline | 候选座位 | 胜-负-平 | 平均镜像筹码差 | 平均日志筹码差 | 日志 |
| --- | ---: | ---: | ---: | ---: | --- |
| `bots\bot1.py` | P0 | 5-0-0 | 5030.6 | 2515.3 | `matchups\current_mirror_bot1.json` |
| `bots\bot2.py` | P0 | 2-3-0 | -3271.0 | -1635.5 | `matchups\current_mirror_bot2.json` |
| `bots\bot3.py` | P0 | 4-1-0 | 2831.4 | 1415.7 | `matchups\current_mirror_bot3.json` |

## 日志信号

- `branch:lock_win_fold`: 732
- `branch:preflop_spot_action`: 691
- `branch:check_or_pot_control`: 444
- `branch:call_defense`: 361
- `branch:fold_defense`: 340
- `branch:value_or_pressure_raise`: 233
- `trace_anti_lock_pressure`: 222
- `branch:probe_raise`: 206
- `trace_probe_aggressive_bluff_line`: 206
- `branch:anti_lock_pressure`: 52
- `branch:anti_lock_preflop_attack`: 52
- `trace_probe`: 29

## 值得复盘的手牌

- `trace_probe_aggressive_bluff_line` game=0 step=11 action=169
- `trace_probe_aggressive_bluff_line` game=0 step=119 action=163
- `trace_probe_aggressive_bluff_line` game=0 step=125 action=260
- `trace_anti_lock_pressure` game=0 step=149 action=0
- `trace_anti_lock_pressure` game=0 step=153 action=0
- `candidate_high_loss_hand` game=0 step=154 action={'round': 3, 'player_id': 0, 'action': 0, 'action_type': 'check'}
- `trace_anti_lock_pressure` game=0 step=155 action=258
- `trace_anti_lock_pressure` game=0 step=161 action=299
- `trace_anti_lock_pressure` game=0 step=167 action=262
- `trace_anti_lock_pressure` game=0 step=173 action=299
- `trace_anti_lock_pressure` game=0 step=179 action=0
- `trace_anti_lock_pressure` game=0 step=185 action=0
