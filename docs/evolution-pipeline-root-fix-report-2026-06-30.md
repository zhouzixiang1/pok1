# 进化管线根因修复报告（2026-06-30）

## 背景

本轮修复针对 v229-v231 暴露出的演进管线问题：crossover 子代运行期导入崩溃未被早期质量门挡住、crossover 完成后编排器又回到 Master、Master audit 拒绝后仍可能继续执行、失败代际在 commit 前污染经验池，以及日志上下文不足导致定位困难。

## 根因

1. `py_compile` 只验证语法，不执行 `from module import symbol` 绑定。v230 的 `strategy.py` 引入了 `opponent.py` 不存在的 `_allin_polarized_equity_fold`，语法检查通过，运行期才崩。
2. `smoke_tester.py` 只看 mirror battle 是否结束；bot 子进程崩溃/EOF 在 battle 层会退化成 fold，导致崩溃 bot 也能 smoke pass。
3. `run_crossover` 成功后 checkpoint 只有 `workers_done + parent2_v`，没有 `master_plan`。上下文注入器看到“无 master_plan”会提示 LLM 调 `run_master`，和 `workers_done -> run_quality_gates` 冲突。
4. `run_master` 对 `overall_pass=false` 的 master audit 结果不够硬：`retry_recommended=false` 或重试耗尽时仍可能接受计划。
5. `quality_failed` 没有独立 checkpoint stage，失败后恢复提示不明确。
6. cross-gen pivot 和 critic evidence 存在 commit 前写 `experience_pool.md` 的路径，失败代际可能污染 tracked 经验文档。
7. event_bus 在 `_last_known` 与 payload `next_v` 不一致时优先使用旧 run_id，出现过 v231 事件挂到 `230#0` 的错配。

## 已执行修复

### 运行期 import 合约

- 新增 `run_import_contract_test()`，在干净子进程中导入 `main/strategy/postflop/opponent/state`。
- `run_quality_gates` 在 compile 后、smoke 前执行 import contract，并返回 `import_ok/import_errors/failed_gates`。
- `_run_crossover` 在 smoke 前执行同一 import contract，失败会 retry crossover 并记录 `pipeline.crossover_import_contract_failed`。
- `smoke_tester.py` 增加入口 import probe；`run_smoke_test()` 对 traceback、ImportError、BrokenPipeError 等失败输出不再误判为成功。

### 状态机和编排

- 新增 `quality_failed` stage，并给上下文提示明确下一步：带失败反馈重跑 `execute_workers` 或 abandon，不能回 Master。
- `run_master` 识别 crossover checkpoint（含无 plan 和合成 crossover plan 两种），返回 `CROSSOVER_ALREADY_DONE`，阻止回 Master/Workers。
- `run_crossover` 成功后写合成 crossover plan 和 audit_context，明确下一步是 `run_quality_gates`。
- Master plan audit 引入权威 `next_v`，不再用 `source_v + 1` 猜目标版本；版本身份不一致会直接返回 blocking audit。
- Master audit rejected 不再被当 advisory 接受；重试耗尽后返回 `MASTER_AUDIT_REJECTED` 并 abandon 当前代。
- `write_pipeline_checkpoint()` 默认阻断真正非法 stage 回退和 active checkpoint identity mismatch；保留必要 rework 白名单。

### 经验池保护

- cross-gen pivot 不再写 `experience_pool.md`，只记录运行时事件 `pipeline.cross_gen_pivot_runtime_only`。
- `_append_experience_updates()` 默认要求 `bot-vN` tag 存在；未提交版本写经验池会被 `pipeline.experience_write_blocked_uncommitted` 阻断。
- `post_generation_cleanup()` 在经验池 consolidation 前检查 tag，未提交版本跳过。

### 日志和重启

- event_bus 会用 payload 中的 `next_v/version/target_v` 修正 run_id，避免跨代错配。
- 增加 session、checkpoint、gate dropped、daemon lifecycle、control clear-session 等结构化事件。
- `pokctl.sh` 停 daemon 改为 SIGTERM grace，再 SIGKILL；`.server.pid` 写入 pid/pgid/port/cmd/started_at。
- 新增 `scripts/pok_restart_observe.sh`：带锁、快照、可选备份清 checkpoint、写 daemon config、调用 `pokctl.sh restart`、HTTP health check、按 terminal events 观察若干代。

## 验证

已通过：

- `python -m py_compile` 覆盖本轮修改的 Python 文件。
- `bash -n pokctl.sh scripts/pok_restart_observe.sh`。
- `web/tests/test_logic_pipeline_root_fixes.py`：6 passed。
- 状态机/管线 focused tests：65 passed。
- audit/regression/context focused tests：34 passed。
- `scripts/pok_restart_observe.sh --clear-checkpoint backup-and-clear --observe-generations 0 --dry-run` 成功输出预期动作。

全量 `cd web && python -m pytest tests -q` 当前结果：1169 passed，1 skipped，1 deselected，6 failed。

剩余 6 个失败未归入本轮改动：

- `TestMatchAnalystSentinel.test_infra_error_returns_sentinel`：测试没有创建 replay 文件，`_analyze_recent_matches` 在 LLM 前返回空串。
- `TestDriftEntryUpdated.test_pool_no_longer_claims_stderr_unreadable`：当前 tracked `experience_pool.md` 缺少测试期望的 `RESOLVED (A1)` 文案。
- `get_bot_info` 相关 3 个失败：测试选择 active bot v202，但接口返回 400，需单独查当前 bot/tag/fixture 状态。
- `TestSchedulerPathUsedWhenCapable.test_partial_results_fallback`：scheduler partial fallback 期望 1 次 serial，实际 2 次。

## 重启策略

当前真实运行态存在 stale checkpoint：

- `web/core/results/pipeline_state.json`: `next_v=231, source_v=224, stage=direction_audited`
- `bots/claude_v231` 不在 active 目录，已在 `bots/graveyard/failed_v229_v231_20260630/claude_v231`
- `.server.pid` 残留指向不存在 PID

因此实际重启建议使用：

```bash
scripts/pok_restart_observe.sh \
  --port 8000 \
  --no-build \
  --clear-session stale \
  --clear-checkpoint backup-and-clear \
  --observe-generations 1 \
  --observe-timeout 1800
```

先备份并清理 stale checkpoint，再启动 Web 主入口。若需要连续观察 3 代，将 `--observe-generations 1` 改为 `3`，并把 timeout 放宽到 21600 秒。
