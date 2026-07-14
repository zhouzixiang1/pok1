# HL 运行摘要

- 结论：`RED`
- 候选 bot：`bots\experiments\anti_lock_v3.py`
- 随机种子：`2027360601`
- 每个 baseline 的镜像组数：`5`
- 每个 baseline 的实际比赛场数：`10`
- 每场手牌数：`70`

## 对战

| Baseline | 候选座位 | 胜-负-平 | 平均镜像筹码差 | 平均日志筹码差 | 日志 |
| --- | ---: | ---: | ---: | ---: | --- |
| `bots\bot1.py` | P0 | 4-1-0 | -1553.8 | -776.9 | `matchups\current_mirror_bot1.json` |
| `bots\bot2.py` | P0 | 2-3-0 | -3547.6 | -1773.8 | `matchups\current_mirror_bot2.json` |
| `bots\bot3.py` | P0 | 2-3-0 | -2981.6 | -1490.8 | `matchups\current_mirror_bot3.json` |
| `bots\bot4.py` | P0 | 3-2-0 | -2122.2 | -1061.1 | `matchups\current_mirror_bot4.json` |
| `bots\bot5.py` | P0 | 3-2-0 | 7036.6 | 3518.3 | `matchups\current_mirror_bot5.json` |

## 日志信号

- `branch:lock_win_fold`: 1293
- `branch:preflop_spot_action`: 1099
- `branch:check_or_pot_control`: 669
- `branch:call_defense`: 590
- `branch:fold_defense`: 555
- `branch:value_or_pressure_raise`: 372
- `branch:probe_raise`: 357
- `trace_probe_aggressive_bluff_line`: 357
- `trace_anti_lock_pressure`: 356
- `branch:anti_lock_preflop_attack`: 81
- `branch:anti_lock_pressure`: 69
- `trace_probe`: 60

## 值得复盘的手牌

- `trace_probe_aggressive_bluff_line` game=0 step=85 action=100
- `candidate_high_loss_hand` game=0 step=144 action={'player_id': 0, 'action': -1, 'action_type': 'fold'}
- `trace_probe_aggressive_bluff_line` game=0 step=169 action=166
- `trace_probe_aggressive_bluff_line` game=0 step=183 action=100
- `trace_probe_aggressive_bluff_line` game=0 step=205 action=184
- `trace_probe_aggressive_bluff_line` game=0 step=241 action=163
- `trace_probe` game=0 step=247 action=1201
- `trace_probe_aggressive_bluff_line` game=0 step=261 action=100
- `trace_probe` game=0 step=271 action=580
- `trace_probe_aggressive_bluff_line` game=0 step=313 action=135
- `trace_probe` game=0 step=319 action=703
- `trace_probe_aggressive_bluff_line` game=0 step=343 action=100
