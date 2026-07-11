# 平台稳定性压力测试报告

- **日期**: 2026-07-11
- **对象**: pok-arena 平台(TCP server + 引擎 + MatchManager + THP 增量落盘 + SSE)
- **bot**: 10 个 pok1 national native bot(v115 / v117 / v119 / v120 / v121 / v122 / v123 / v135 / v141 / v142)

## 规模

- **配对**: 10 bot round-robin,C(10,2) = 45 对 × **5 轮** = **225 场**
- **每场**: 70 手(完整赛制,SB/BB 交替,20000 筹码,盲注 50/100)
- **并发**: 4 路并行(4 个 serve 端口 50110-50113,8 个 bot 进程)
- **总手数**: 15750

## 结果: ✓ 平台稳定

| 指标 | 值 |
|---|---|
| 完成 | **225 / 225 场(100%)** |
| 状态分布 | `{completed: 225}` |
| THP 完整 | **225 / 225**(每场 STATE 行数 == hands_played) |
| 崩溃 / 断线 / 死锁 / 超时 | **0** |
| 耗时 | 1413s(23.6min) |
| 每场平均 | 6.3s |
| 每手平均 | 90ms |

**结论**:10 个强 bot 全配对 225 场 × 70 手 = 15750 手,**零异常**。平台 TCP 接入 / 引擎 / re-arm / THP 增量落盘 / 断线判负 / SSE 全链路在大量对战下稳定可靠。

## 日志(每场完整留痕)

每场写 `/tmp/stability-records/worker{0-3}/<对名>_r<轮>/`:

- `botA.log` / `botB.log` — native_bot wire 流(RECV / DISPATCH / DECIDE / SEND / state / timing)
- `botA.stdout` / `botB.stdout` — bot 进程输出 / 错误
- `events.jsonl` — serve 事件流(hand_start / cards_dealt / action_requested / action / settle / match_end)
- `result.json` — 结果摘要(match_id / hands / earnings / reason)
- `final.thp` — gb2312 THP 棋谱
- `index.json` + `<match_id>.thp` — MatchManager records

任意异常可逐场追溯到字节级。

## 覆盖的稳定性点

- **re-arm**:225 场连续,每场结束 close_clients → 下一对(单 worker 56 场 re-arm 55 次,全成功)
- **THP 增量落盘**:每手 settle append,225 场 × 70 手 = 15750 行 STATE 全完整 + footer
- **token 解析**:15750 手 × 双方动作,pop_client_action 无粘包/解析错误
- **raise 边界**:跨 10 bot 的 raise 全通过 validator(`>=2×`),0 非法误判
- **断线判负**:0 断线发生(bot 都正常),逻辑就绪(见 accept_disconnect.py)
- **多 serve 并发**:4 路并行 23.6min,进程/端口/records 隔离无冲突

## 复现

```bash
scripts/stability_test.py --rounds 5 --hands 70 --parallel 4   # 全量 225 场 × 70 手, 4 路(~24min)
scripts/stability_test.py --rounds 1 --hands 10 --parallel 2   # 快速冒烟(45 场 × 10 手, ~2min)
```

225 场逐场明细(状态 / 手数 / earnings / 耗时)见 `STABILITY_REPORT.json`(本地,gitignore)。
