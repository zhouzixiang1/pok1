# HL 运行摘要

- 结论：`GREEN`
- 候选 bot：`1.py`
- 随机种子：`2026052704`
- 每个 baseline 的镜像组数：`2`

## 对战

| Baseline | 候选座位 | 胜-负-平 | 平均镜像筹码差 | 平均日志筹码差 | 日志 |
| --- | ---: | ---: | ---: | ---: | --- |
| `bots\bot002\main.py` | P0 | 2-0-0 | 1123.0 | 561.5 | `matchups\current_forward_bot002.json` |
| `bots\bot003\main.py` | P0 | 2-0-0 | 17351.5 | 8675.8 | `matchups\current_forward_bot003.json` |
| `bots\bot004\main.py` | P0 | 2-0-0 | 10904.5 | 5452.2 | `matchups\current_forward_bot004.json` |
| `bots\bot005\main.py` | P0 | 2-0-0 | 16954.5 | 8477.2 | `matchups\current_forward_bot005.json` |
| `bots\bot006\main.py` | P0 | 2-0-0 | 1364.0 | 682.0 | `matchups\current_forward_bot006.json` |
| `bots\bot002\main.py` | P1 | 2-0-0 | 1233.0 | 616.5 | `matchups\current_reverse_bot002.json` |
| `bots\bot003\main.py` | P1 | 2-0-0 | 10247.5 | 5123.8 | `matchups\current_reverse_bot003.json` |
| `bots\bot004\main.py` | P1 | 2-0-0 | 7439.5 | 3719.8 | `matchups\current_reverse_bot004.json` |
| `bots\bot005\main.py` | P1 | 2-0-0 | 9741.0 | 4870.5 | `matchups\current_reverse_bot005.json` |
| `bots\bot006\main.py` | P1 | 2-0-0 | 908.0 | 454.0 | `matchups\current_reverse_bot006.json` |

## 日志信号

- `branch:lock_win_fold`: 1308
- `branch:preflop_spot_action`: 445
- `branch:check_or_pot_control`: 366
- `branch:call_defense`: 292
- `branch:probe_raise`: 188
- `trace_probe_aggressive_bluff_line`: 188
- `branch:value_or_pressure_raise`: 169
- `branch:fold_defense`: 169
- `trace_anti_lock_pressure`: 44
- `branch:anti_lock_pressure`: 38
- `trace_probe`: 34
- `candidate_high_loss_hand`: 26

## 值得复盘的手牌

- `trace_probe_aggressive_bluff_line` game=0 step=11 action=141
- `trace_probe_aggressive_bluff_line` game=0 step=37 action=213
- `trace_probe_aggressive_bluff_line` game=0 step=57 action=178
- `trace_probe_aggressive_bluff_line` game=0 step=73 action=136
- `trace_probe_aggressive_bluff_line` game=0 step=91 action=136
- `trace_probe_aggressive_bluff_line` game=0 step=105 action=396
- `trace_probe_aggressive_bluff_line` game=1 step=43 action=159
- `trace_probe_aggressive_bluff_line` game=1 step=73 action=199
- `trace_probe` game=1 step=89 action=747
- `trace_probe_aggressive_bluff_line` game=1 step=99 action=100
- `trace_probe_aggressive_bluff_line` game=1 step=117 action=135
- `trace_probe_aggressive_bluff_line` game=1 step=129 action=140
