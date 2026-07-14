# HL 运行摘要

- 结论：`RED`
- 候选 bot：`1.py`
- 随机种子：`2026060601`
- 每个 baseline 的镜像组数：`5`
- 每个 baseline 的实际比赛场数：`10`
- 每场手牌数：`70`

## 对战

| Baseline | 候选座位 | 胜-负-平 | 平均镜像筹码差 | 平均日志筹码差 | 日志 |
| --- | ---: | ---: | ---: | ---: | --- |
| `bots\bot1.py` | P0 | 3-2-0 | -278.0 | -139.0 | `matchups\current_mirror_bot1.json` |
| `bots\bot2.py` | P0 | 3-2-0 | 2717.0 | 1358.5 | `matchups\current_mirror_bot2.json` |
| `bots\bot3.py` | P0 | 2-3-0 | 857.8 | 428.9 | `matchups\current_mirror_bot3.json` |
| `bots\bot4.py` | P0 | 3-2-0 | -4036.6 | -2018.3 | `matchups\current_mirror_bot4.json` |
| `bots\bot5.py` | P0 | 1-4-0 | -9931.4 | -4965.7 | `matchups\current_mirror_bot5.json` |

## 日志信号

- `branch:preflop_spot_action`: 1153
- `branch:lock_win_fold`: 1143
- `branch:check_or_pot_control`: 704
- `branch:fold_defense`: 646
- `branch:call_defense`: 580
- `branch:value_or_pressure_raise`: 395
- `trace_anti_lock_pressure`: 379
- `branch:probe_raise`: 303
- `trace_probe_aggressive_bluff_line`: 303
- `branch:anti_lock_preflop_attack`: 112
- `branch:anti_lock_pressure`: 67
- `trace_probe`: 58

## 值得复盘的手牌

- `trace_probe_aggressive_bluff_line` game=0 step=87 action=100
- `candidate_high_loss_hand` game=0 step=128 action={'player_id': 0, 'action': -1, 'action_type': 'fold'}
- `trace_probe_aggressive_bluff_line` game=0 step=181 action=180
- `trace_probe_aggressive_bluff_line` game=0 step=187 action=288
- `trace_probe_aggressive_bluff_line` game=0 step=207 action=145
- `trace_anti_lock_pressure` game=0 step=273 action=7024
- `trace_probe_aggressive_bluff_line` game=0 step=293 action=163
- `trace_probe_aggressive_bluff_line` game=1 step=81 action=100
- `trace_probe_aggressive_bluff_line` game=1 step=95 action=149
- `candidate_high_loss_hand` game=1 step=120 action={'round': 3, 'player_id': 1, 'action': 0, 'action_type': 'check'}
- `trace_anti_lock_pressure` game=1 step=131 action=2605
- `trace_probe_aggressive_bluff_line` game=1 step=157 action=190
