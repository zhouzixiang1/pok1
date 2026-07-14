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
| `bots\bot4.py` | P0 | 5-0-0 | 10447.2 | 5223.6 | `matchups\current_mirror_bot4.json` |
| `bots\bot5.py` | P0 | 2-3-0 | 1801.2 | 900.6 | `matchups\current_mirror_bot5.json` |

## 日志信号

- `branch:lock_win_fold`: 463
- `branch:preflop_spot_action`: 459
- `branch:check_or_pot_control`: 270
- `branch:fold_defense`: 230
- `branch:call_defense`: 206
- `trace_anti_lock_pressure`: 190
- `branch:value_or_pressure_raise`: 155
- `branch:probe_raise`: 106
- `trace_probe_aggressive_bluff_line`: 106
- `branch:anti_lock_preflop_attack`: 48
- `branch:anti_lock_pressure`: 42
- `candidate_high_loss_hand`: 22

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
