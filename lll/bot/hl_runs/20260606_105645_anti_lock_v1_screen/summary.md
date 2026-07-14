# HL 运行摘要

- 结论：`RED`
- 候选 bot：`bots\experiments\anti_lock_v1.py`
- 随机种子：`2026360601`
- 每个 baseline 的镜像组数：`5`
- 每个 baseline 的实际比赛场数：`10`
- 每场手牌数：`70`

## 对战

| Baseline | 候选座位 | 胜-负-平 | 平均镜像筹码差 | 平均日志筹码差 | 日志 |
| --- | ---: | ---: | ---: | ---: | --- |
| `bots\bot4.py` | P0 | 3-2-0 | 3567.4 | 1783.7 | `matchups\current_mirror_bot4.json` |
| `bots\bot5.py` | P0 | 2-3-0 | -3038.6 | -1519.3 | `matchups\current_mirror_bot5.json` |

## 日志信号

- `branch:preflop_spot_action`: 470
- `branch:lock_win_fold`: 428
- `branch:check_or_pot_control`: 295
- `branch:fold_defense`: 255
- `branch:call_defense`: 220
- `trace_anti_lock_pressure`: 192
- `branch:value_or_pressure_raise`: 150
- `branch:probe_raise`: 104
- `trace_probe_aggressive_bluff_line`: 104
- `branch:anti_lock_preflop_attack`: 53
- `branch:anti_lock_pressure`: 43
- `candidate_high_loss_hand`: 21

## 值得复盘的手牌

- `trace_probe_aggressive_bluff_line` game=0 step=11 action=169
- `trace_anti_lock_pressure` game=0 step=135 action=0
- `trace_anti_lock_pressure` game=0 step=139 action=0
- `candidate_high_loss_hand` game=0 step=140 action={'round': 3, 'player_id': 0, 'action': 0, 'action_type': 'check'}
- `trace_anti_lock_pressure` game=0 step=141 action=258
- `trace_anti_lock_pressure` game=0 step=147 action=299
- `trace_anti_lock_pressure` game=0 step=153 action=262
- `trace_anti_lock_pressure` game=0 step=159 action=299
- `trace_anti_lock_pressure` game=0 step=165 action=0
- `trace_anti_lock_pressure` game=0 step=171 action=0
- `trace_anti_lock_pressure` game=0 step=177 action=299
- `trace_anti_lock_pressure` game=0 step=183 action=275
