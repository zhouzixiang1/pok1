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
| `bots\bot1.py` | P0 | 5-0-0 | 5000.0 | 2500.0 | `matchups\current_mirror_bot1.json` |
| `bots\bot2.py` | P0 | 3-2-0 | -361.8 | -180.9 | `matchups\current_mirror_bot2.json` |
| `bots\bot3.py` | P0 | 4-1-0 | 2213.0 | 1106.5 | `matchups\current_mirror_bot3.json` |

## 日志信号

- `branch:lock_win_fold`: 757
- `branch:preflop_spot_action`: 689
- `branch:check_or_pot_control`: 458
- `branch:call_defense`: 369
- `branch:fold_defense`: 345
- `branch:value_or_pressure_raise`: 235
- `branch:probe_raise`: 211
- `trace_probe_aggressive_bluff_line`: 211
- `trace_anti_lock_pressure`: 178
- `branch:anti_lock_preflop_attack`: 41
- `branch:anti_lock_pressure`: 31
- `trace_probe`: 31

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
