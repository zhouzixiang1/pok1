# LLM 多阶段运行时数据流

本文档以 **时间线** 视角，描述 `python web/main.py` 启动后系统内逐一发生的事件，聚焦每个 LLM 调用的数据流：谁发起、输入什么、输出什么、输出去向。

---

## 一、启动序列（三阶段架构）

系统采用**代码层调度 + LLM 单代执行**的三阶段架构。代码层（`generation_scheduler.py`）负责 Phase 1 和 Phase 3，LLM 仅在 Phase 2 驱动 pipeline。

```
python web/main.py
        │
        ▼
  CLI 参数解析 (--port [PORT env], --host, --no-daemon, --dev, --no-build)
  前端构建 (npm run build → web/server/static/)
  app_state 配置 (daemon_enabled, daemon_workers, daemon_pairs)
        │
        ▼
  app.py 模块级: EventBroadcaster(buffer_size=500) + WebUI(broadcaster)  ← SSE 广播器在 uvicorn 之前创建
        │
        ▼
  uvicorn.run("server.app:app")
        │
        ▼
  FastAPI lifespan 启动:
    ├── app_state.bootstrap(find_current_v())
    ├── ShutdownManager 创建 + 信号处理安装 (loop.add_signal_handler)
    ├── asyncio.create_task(orchestrator_loop(shutdown_mgr=...))
    └── orchestrator_loop() 内部:
          ├── inject_ui(web_ui)
          ├── start_daemon() + daemon_monitor_thread 启动
          ├── _startup_recovery() — 评估中断状态（4 种情况）
          └── while True:
                ├── Phase 1: prepare_generation(shutdown_mgr, ui) — 代码层
                │     ├── reap_if_needed() — 池 > 30 时自动淘汰
                │     ├── wait_for_daemon_eval()
                │     ├── _cleanup_incomplete()
                │     ├── asyncio.gather(
                │     │     _run_combined_analysis() 📎 LLM,   ← 合并停滞+性能
                │     │     _analyze_recent_matches() 📎 LLM  ← 对战分析
                │     │   )
                │     └── _decide_strategy() — 纯代码决策
                │     → 返回 GenerationContext | None
                │
                ├── Phase 2: _run_one_cycle(gen_ctx=ctx) — LLM session
                │     ├── _build_context(gen_ctx=ctx) 注入预计算分析
                │     ├── Orchestrator LLM 自主调用 MCP 工具
                │     └── pipeline: master → workers → quality → review → critic → commit
                │     → 中断时 session + checkpoint 保留
                │
                ├── Phase 3: post_generation_cleanup(shutdown_mgr, ui, ctx) — 代码层
                │     ├── reap_if_needed()
                │     └── consolidate_experience() (每 3 代)
                │     → 幂等，可安全中断
                │
                ├── shutdown_mgr.is_shutting_down? → break
                └── asyncio.sleep(5)
```

### 三阶段中断语义

| 阶段 | 中断行为 | 恢复方式 |
|------|---------|---------|
| Phase 1 (prepare) | 丢弃部分结果 | 下次循环重新执行，获得最新数据 |
| Phase 2 (LLM session) | session + checkpoint 保留 | 新 LLM session 从 checkpoint 断点继续 |
| Phase 3 (cleanup) | 幂等操作 | 重新执行，无副作用 |

### `_run_one_cycle()` 内部

当 `gen_ctx`（`GenerationContext`，来自 `generation_scheduler.py`）提供时，`_build_context()` 注入预计算分析数据：

1. 注入 GenerationContext 字段：strategy (master/crossover)、source_v、stagnation_info、match_analysis、performance_verification
2. Pipeline checkpoint 信息（用于断点恢复）
3. **不再**注入原始状态数据（ratings、bot stats 等）— 这些已在 Phase 1 预处理

当 `gen_ctx` 为 None（dry_run 或遗留路径）时，回退到旧行为：自行读取 ratings、bot stats、H2H 数据。

**完整流程**:
1. `_build_context(gen_ctx=ctx)` 构建上下文字符串
2. 将上下文注入 `orchestrator.md` 模板的 `{context}` 占位符
3. 检查 `orchestrator_session.json`：若存在（上次中断），用 `resume=session_id` 恢复会话
4. 以 `model="sonnet"` 启动 `claude_query()` 流式对话
5. Orchestrator LLM 开始自主调用 MCP 工具（Phase 2 pipeline）

**此后的一切 LLM 调用，都由 Orchestrator LLM 通过选择调用 MCP 工具来触发。**

---

## 二、一代进化的时间线

一代进化分为三个阶段。**Phase 1 和 Phase 3 由代码层调度**（`generation_scheduler.py`），**Phase 2 由 LLM 驱动**。LLM 调用以 **📎** 标记，标注完整数据流。

---

### Phase 1：准备阶段（代码层调度）

Phase 1 由 `prepare_generation()` 函数编排，每步完成后检查 `shutdown_mgr.is_shutting_down`，可安全中断。以下步骤**不再由 Orchestrator LLM 触发**，而是代码层自动执行。

---

#### Phase 1.1：状态查询（代码直接调用）

- **触发者**: `prepare_generation()` 代码层
- **有无 LLM**: 无
- **做什么**: 调用 `find_current_v()`（从 git tags 读取最新 bot 版本）+ `load_ratings()`（读取 `glicko_ratings.json`）+ `get_active_bots()`（扫描 `bots/` 目录）。若活跃 bot 数 > `MAX_ACTIVE_BOTS`(30)，**先自动淘汰**最弱 bot（`_do_reap_weakest`，最多 10 轮）。
- **输出**: `current_v`、`active_bots`、`ratings` 字典（中间变量，仅 `current_v` 存入 GenerationContext）

> **旧对比**: 原 Step 1 `get_status()` 由 Orchestrator LLM 触发，返回 13 个字段的 JSON 快照。现在这些数据直接在代码层获取，不再经过 LLM。

---

#### Phase 1.2：等待评估（代码直接调用）

- **触发者**: `prepare_generation()` 代码层
- **有无 LLM**: 无
- **做什么**: 异步轮询 `bot_stats.json`，等待守护进程为当前 bot 积累足够对局（默认 ≥ 100 局，超时 600s）
- **输出**: `eval_ok` 布尔值。不足 → 返回 `None`（本轮跳过，10 秒后重试）

> **旧对比**: 原 Step 3 `wait_for_eval()` 由 Orchestrator LLM 触发。现在代码层自动等待，无需 LLM 决策。

---

#### Phase 1.3：清理残留（代码直接调用）

- **触发者**: `prepare_generation()` 代码层
- **有无 LLM**: 无
- **做什么**: `_cleanup_incomplete()` — 删除无 `.completed` 且无 git tag 的残留 bot 目录。**Checkpoint 感知**: 若目录版本与活跃 `pipeline_state.json` 的 `next_v` 匹配且 stage 不为 None/archived，则**跳过删除**（保护中断恢复状态）。
- **输出**: 无（副作用：清理文件系统）

> **旧对比**: 原 Step 2 `housekeeping()` 由 Orchestrator LLM 按需调用。现在代码层自动执行，每代循环开头清理一次。

---

#### Phase 1.4：合并分析（停滞+性能）📎 `_run_combined_analysis(source_v, active_bots, ratings, ui)`

| 项目 | 内容 |
|---|---|
| **触发者** | `prepare_generation()` 代码层直接调用 |
| **调用链** | `generation_scheduler.py` → `combined_analyst.py:_run_combined_analysis()` → `run_claude_query()` |
| **LLM 角色** | COMBINED ANALYST |
| **模型** | Sonnet |
| **工具** | 无（纯 JSON 输出） |

**前置优化** — 统计预检查（纯代码，可能跳过 LLM）:
1. `_statistical_stagnation_check()` 对最近 6 个 rating 周期做滑动窗口比较
2. 若趋势明显（delta < 5 = 停滞，delta > 20 = 改善），**直接返回**，跳过 LLM 调用
3. RD > 150 时统计检查不可靠，回退到 LLM

**输入构建** (函数 `_run_combined_analysis` 内):
1. 读取 `rating_history.jsonl` 最近 10 个周期，提取 top H2H 胜率
2. 读取 `head_to_head.json` → `load_h2h_avg_winrates_with_coverage()` — H2H 胜率 + 对手覆盖率
3. 读取 `bot_stats.json` 获取总体胜率和场次
4. 计算 Top 5 活跃 bot 列表（含 RD 警告）
5. 从 git tags 提取最近 8 代进化趋势（vN: h2h_avg_wr + coverage）
6. 从 git history 提取 lineage（vN ← parent: vM）
7. 读取 `worker_failures.jsonl` 最近 5 条失败记录
8. 加载上代 Critic 洞察（`archive/vN.json` → critic_data.strategic_assessment）
9. 拼装 prompt：`combined_analyst.md` 模板 + 全部数据

**输入数据来源**:
- `web/core/results/rating_history.jsonl` — Rating 历史快照
- `web/core/results/head_to_head.json` → `load_h2h_avg_winrates()` — H2H 胜率
- `web/core/results/glicko_ratings.json` — 当前 ratings
- `web/core/results/bot_stats.json` — 总体统计
- `web/core/results/archive/vN.json` — 上代 Critic 洞察
- `web/core/results/worker_failures.jsonl` — Worker 失败记录

**对手覆盖率检查**: 若覆盖率 < 80%，直接返回 safe_default（跳过 LLM），等待更多 daemon 评估。

**LLM 输出**: JSON（经 `output_schema.py` 验证）
```json
{
  "is_stagnant": true/false,
  "confidence": "high/medium/low",
  "trend": "improving|stagnant|declining",
  "diversity_needed": true/false,
  "diversity_reason": "...",
  "recommendation": "continue|branch|crossover",
  "branch_from": "claude_vN" 或 null,
  "verified_improvements": ["..."],
  "persistent_weaknesses": ["..."],
  "reason": "简短解释",
  "suggestion": "...",
  "recommended_source": "claude_vN",
  "source_rationale": "解释为何选择此 bot 作为进化源"
}
```

**输出去向**: 返回给 `prepare_generation()`，同时存入 `GenerationContext.stagnation_info` 和 `GenerationContext.performance_verification`（两者设为**相同值**，因为合并分析替代了原来的两个独立调用）。

> **合并历史**: 原 Phase 1 有 3 个独立 LLM 调用（`_analyze_stagnation` + `_analyze_recent_matches` + `_run_performance_verification`），现合并为 2 个并行调用（`_run_combined_analysis` + `_analyze_recent_matches`）。`combined_analyst.py` 文件头明确说明: "Replaces two separate LLM calls (_analyze_stagnation + _run_performance_verification) with a single call."

---

#### Phase 1.5：对战分析 📎 `_analyze_recent_matches(current_v, ui)`

| 项目 | 内容 |
|---|---|
| **触发者** | `prepare_generation()` 代码层直接调用（与 Phase 1.4 **并行**执行） |
| **调用链** | `generation_scheduler.py` → `agent_master.py:_analyze_recent_matches()` → `run_claude_query()` |
| **LLM 角色** | MATCH ANALYST |
| **模型** | Sonnet |
| **工具** | 无（纯 JSON 输出） |

> **注意**: Phase 1.4 和 Phase 1.5 通过 `asyncio.gather()` 并行执行，而非顺序执行。

**输入构建** (函数 `_analyze_recent_matches` 内):
1. 读取 `match_history.jsonl`，筛选当前 bot 的对局
2. 收集最近 8 场失败 + 4 场险胜（胜分差 ≤ 2）
3. 对每场对局加载 `match_replay/{id}` 完整录像
4. 调用 `summarize_replay_for_analysis()` 压缩为结构化摘要：胜率、筹码变化、行动分布、per-street 统计（fold/raise/call/allin 百分比、平均加注倍数）
5. 拼装 prompt："You are a Poker Hand Analyst..." + 摘要文本 + 分析指令

**输入数据来源**:
- `web/core/results/match_history.jsonl` — 对局历史索引
- `web/core/results/match_replay/{id}` — 完整对局录像 JSON

**LLM 输出**: JSON
```json
{
  "weaknesses": ["..."],
  "street_weaknesses": {"river": "...", "flop": "..."},
  "patterns": "...",
  "working": "...",
  "recommendation": "..."
}
```

**输出去向**: 返回给 `prepare_generation()`，存入 `GenerationContext.match_analysis`。

> **旧对比**: 原 Step 5 由 Orchestrator LLM 调用 MCP 工具触发。现在由代码层直接调用。

---

#### Phase 1.6：策略决策（纯代码，无 LLM）

- **触发者**: `prepare_generation()` 代码层
- **有无 LLM**: 无
- **做什么**: `_decide_strategy(combined, current_v, ratings)` — 基于合并分析结果，确定性选择策略

**决策逻辑**（`generation_scheduler.py:_decide_strategy()`）:
```
if combined.is_stagnant && confidence != "low" && 有可用 crossover parents:
    → strategy="crossover", source_v=parent_a, parents=(parent_a, parent_b)
elif combined.recommendation=="branch" && branch_from 有效:
    → strategy="master", source_v=branch_from
elif combined.diversity_needed && 有可用 crossover parents:
    → strategy="crossover", source_v=parent_a, parents=(parent_a, parent_b)  # 强制多样性注入
elif combined.recommended_source 有效 (bounds check: ≥1 且 bot 目录存在):
    → strategy="master", source_v=recommended_source  # LLM 推荐最佳进化源
else:
    → strategy="master", source_v=current_v  # 回退
```

**输出**: `(strategy, source_v, crossover_parents)` 三元组，存入 GenerationContext。

> **旧对比**: 原架构中策略决策由 Orchestrator LLM 在收到步骤 4-6 的输出后推理决定。现在是确定性代码逻辑，消除了 LLM 的决策不确定性。

---

#### Phase 1 输出：GenerationContext

`prepare_generation()` 返回 `GenerationContext` 对象（或 `None` 表示跳过本轮），包含：

| 字段 | 类型 | 来源 |
|------|------|------|
| `current_v` | int | `find_current_v()` |
| `next_v` | int | `current_v + 1` |
| `strategy` | str | `_decide_strategy()` |
| `source_v` | int | `_decide_strategy()` |
| `crossover_parents` | tuple | `_decide_strategy()` |
| `stagnation_info` | str | `_run_combined_analysis()` → JSON |
| `match_analysis` | str | `_analyze_recent_matches()` → JSON |
| `performance_verification` | str | `_run_combined_analysis()` → JSON（与 `stagnation_info` 相同） |
| `gen_count` | int | 循环计数器 |

> **注意**: `stagnation_info` 和 `performance_verification` 被设为**相同值**（`perf_text = stagnation_text`），因为合并分析替代了原来的两个独立调用。

---

### Phase 2：LLM 驱动的 Pipeline

> **Phase 2 开始**：从此处起，Orchestrator LLM 接管控制权。Phase 1 预计算的分析数据通过 `_build_context(gen_ctx=ctx)` 注入 LLM 上下文，Orchestrator 根据数据自主调用 MCP 工具驱动 pipeline。

**旧步骤 1-6（状态查询、家政维护、等待评估、停滞分析、对战分析、性能验证）已全部移入 Phase 1 代码层。Orchestrator LLM 不再调用 `get_status()`、`wait_for_eval()`、`analyze_stagnation()`、`run_match_analysis()`、`run_performance_verification()` 等 MCP 工具。**

以下步骤仍由 Orchestrator LLM 通过 MCP 工具触发：

---

### 步骤 7：主架构师规划 📎 `run_master(source_v, next_v, stagnation_info, match_analysis, performance_verification)`

| 项目 | 内容 |
|---|---|
| **触发者** | Orchestrator LLM 调用 MCP 工具 `run_master` |
| **调用链** | `tool_pipeline.py:run_master()` → `agent_master.py:_run_master_analysis()` → `run_claude_query()` |
| **LLM 角色** | MASTER |
| **模型** | Sonnet |
| **工具** | Bash, Read |
| **Prompt 模板** | `prompts/master_prompt.md` |
| **重试** | 最多 3 次 (`MAX_MASTER_RETRIES`)，每次需返回含 `tasks` 的 JSON |

**输入构建** (函数 `_run_master_analysis` 内):
1. 读取 `prompts/master_prompt.md` 模板
2. 替换占位符：`{stagnation_info}`、`{match_analysis}`（裁剪至 10K 字符）、`{performance_verification}`（裁剪至 4K 字符）、`{source_v}`
3. **附加上下文文件路径列表**（在 prompt 文本中提供路径，由 LLM 用 Bash/Read 自行读取）：`glicko_ratings.json`、`rating_history.jsonl`、`head_to_head.json`、`bot_stats.json`、`experience_pool.md`
4. `run_claude_query()` 的 `context_files` 参数为 **空列表 `[]`** — Master 通过工具自行读取文件，而非通过 context_files 注入

> ⚠️ **注意**: 早期版本文档错误描述为 `context_files` 传入文件路径列表。实际上 Master 的 `context_files=[]`，LLM 通过 Bash/Read 工具按需读取。

**LLM 能做的事**: 用 Bash/Read 读取上述文件，分析 rating 趋势、经验池、H2H 数据

**LLM 输出**: JSON（必须包含 `tasks` 数组）
```json
{
  "tasks": [
    {
      "worker_id": 1,
      "role": "Algorithmic Logic Architect",
      "target_files": ["strategy.py"],
      "worker_prompt": "..."
    },
    {
      "worker_id": 2,
      "role": "Hyperparameter Tuner",
      "target_files": ["constants.py"],
      "worker_prompt": "..."
    }
  ],
  "branch_from": "claude_vN" 或 null,
  "analysis": "..."
}
```

**校验** (函数 `_validate_master_plan`):
- tasks 数量 ≤ 3
- 每个 task 的 target_files ≤ 3
- 每个 task 的 worker_prompt ≤ 5000 字符（prompt 模板建议 ≤ 2000 字符）
- **Tuner 目标文件限制**: Hyperparameter Tuner 的 `target_files` 必须仅含 `constants.py`，指向其他文件会触发**硬错误**（阻断 plan，非警告）
- **Architect-Tuner 文件重叠检测**: 若 Architect 和 Tuner 共享任何 target_file，触发硬错误（因为 Tuner 边界检查会看到 Architect 的结构性改动，导致误判为越界）
- Hyperparameter Tuner prompt 会被 `_TUNER_STRUCTURAL_PATTERNS` 检查，含结构化指令（如 "add parameter"、"new function"）时发出边界警告（非阻断，reviewer/critic 执行实际约束）

**输出去向**: 返回给 Orchestrator LLM → Orchestrator 用 `plan["tasks"]` 调用 `execute_workers()`。

> **💡 真实示例 (v36)**: Master 分析了 v7 的 H2H 数据（55.62% avg，29 对手）和 match analysis（65% preflop fold + 0% postflop fold），制定了 2-worker 计划：
> ```json
> {
>   "analysis": "v7 H2H avg is 55.62% (29 opponents), stagnant 7+ periods. Match analysis reveals THREE catastrophic structural flaws: (1) 63-70% preflop fold rate; (2) 0% postflop fold rate; (3) Underbetting raises at 0.4x-0.7x pot.",
>   "targeted_failure": "Preflop over-folding (63-70%) + postflop call-station (0% fold rate). Opponent exploits by raising preflop (stealing 65% of hands uncontested), then betting postflop knowing the bot never folds.",
>   "expected_behavior_change": "(1) SB opens or limps ~85% of hands (was ~35%). (2) BB calls raises with most playable hands. (3) Postflop folds weak holdings ~15% of the time (was 0%). (4) Raise sizing increases from 0.4-0.7x pot to 0.6-1.0x pot.",
>   "branch_from": "claude_v7",
>   "tasks": [
>     {"worker_id": 1, "role": "Algorithmic Logic Architect", "target_files": ["strategy.py"], "difficulty": "hard"},
>     {"worker_id": 2, "role": "Hyperparameter Tuner", "target_files": ["strategy.py"], "difficulty": "medium"}
>   ]
> }
> ```
> 注：此 plan 因 JSON 格式问题被拒绝（共 3 次重试均失败），Orchestrator 最终改用 Crossover。

---

### 步骤 8：准备下一代 `prepare_next_gen(source_v, next_v)`

- **触发者**: Orchestrator LLM
- **有无 LLM**: 无
- **做什么**:
  1. 拒绝 `next_v ≤ source_v`
  2. 拒绝源 bot 不存在或未完成（无 `.completed`）
  3. 拒绝 pipeline stage 已超过 `prepared`
  4. 拒绝覆盖已完成的 bot（有 `.completed`）
  5. `shutil.copytree()` 将 `bots/claude_v{source_v}/` 复制为 `bots/claude_v{next_v}/`
  6. 删除 `.completed` 标记文件
  7. 写入 pipeline checkpoint：`stage="prepared"`、`worker_failure_count=0`
- **输出**: `{prepared: true, next_v, source_v}`

---

### 步骤 9：Worker 编码 📎 `execute_workers(tasks, next_v, source_v, reviewer_feedback)`

| 项目 | 内容 |
|---|---|
| **触发者** | Orchestrator LLM 调用 MCP 工具 `execute_workers` |
| **调用链** | `tool_planning.py:execute_workers()` → `agent_workers.py:_execute_workers()` → `_run_single_worker()` × N → `run_claude_query()` |
| **LLM 角色** | WORKER {id} ({role}) |
| **模型** | Sonnet |
| **工具** | Bash, Read, Edit |
| **Prompt 模板** | `prompts/worker_prompt.md` |
| **执行方式** | **纯串行** — Workers 始终按顺序逐个执行（避免竞态条件） |
| **重试** | 每个 worker 最多 4 次 (`MAX_WORKER_RETRIES`) |
| **超时** | 1000 秒 (`WORKER_TIMEOUT`) |

**单个 Worker 的输入构建** (函数 `_run_single_worker` 内):
1. 读取 `prompts/worker_prompt.md` 模板
2. 替换占位符：`{role}`、`{worker_prompt}`（来自 Master 的任务描述）、`{version}`
3. 注入 reviewer_feedback（若有，前置 "CRITICAL REVISION NEEDED:" 标记）
4. 注入最近 3 条 worker 失败记忆（从 `worker_failures.jsonl` 读取）
5. 重试时注入前次错误（编译错误 / 冒烟错误 / 超时简化提示）
6. `context_files` 为空列表 `[]` — Worker 通过 Bash/Read/Edit 工具直接访问 bot 目录中的文件，而非通过 context_files 注入

**LLM 能做的事**: 用 Bash 运行测试、Read 读取代码、Edit 修改 bot 源文件

**LLM 输出**: 自由文本（代码修改通过 Edit 工具直接写入文件系统）

**每次尝试后的自动检查**（无 LLM）:
- `verify_code()` — `py_compile` 编译检查，失败则注入错误信息重试
- `run_smoke_test()` — 运行 1 局冒烟对战，失败则注入错误信息重试

**Worker 失败记忆**: 注入最近 **5 条** worker 失败记录（从 `worker_failures.jsonl` 读取，`_load_recent_failures(5)`）。

**⚠️ 重要机制补充**:

1. **Worker Circuit Breaker**: 每代最多允许 6 次 worker 失败（`MAX_WORKER_FAILURES = 6`）。计数器持久化在 pipeline checkpoint 中（`worker_failure_count` 字段），**跨 `execute_workers` 调用累计**。仅在失败时递增计数，成功的 worker 批次不消耗预算，防止无限重试的同时允许有价值的迭代改进。见 `tool_planning.py` 中 `failure_count` 检查。

3. **Worker Boundary Validation**: Worker 完成后自动检查：
   - 是否修改了未声明的 target_files 外的已有文件
   - 是否在 target_files 外创建了新文件
   - Hyperparameter Tuner 是否修改了非数字内容（通过 `_numbers_only_changed` 检测）
   - 见 `tool_helpers.py:_validate_worker_boundaries`

**输出**: `{success: bool, boundary_errors: [], logs, costs}`

**输出去向**:
- 返回给 Orchestrator LLM
- 代码变更已写入 `bots/claude_v{next_v}/` 文件系统
- 成功 → 写入 checkpoint `stage="workers_done"`

> **💡 真实示例 (v36)**: Worker 1（Logic Architect）收到 Master 的任务指令后，用 Edit 工具修改 `strategy.py`。prompt 开头指定角色边界和文件所有权：
> ```
> [WORKER 1 (Algorithmic Logic Architect) PROMPT]
> Role Boundary: ALLOWED: Adding new functions. FORBIDDEN: Changing well-tuned constants.
> Scope Contract: target_files = ["strategy.py"]. Do NOT modify other files.
>
> Task: Fix TWO catastrophic structural flaws:
>   Change 1: Widen SB preflop defense (open_threshold 0.49→0.38, limp_threshold 0.36→0.18)
>   Change 2: Add bb_vs_raise handler (currently returns None → falls through to tight logic)
>   Change 3: Add postflop fold logic (postflop_call_margin always returns POSITIVE values → never folds)
>   Change 4: Add explicit fold path for weak river holdings
> ```
> Worker 执行后自动编译检查+冒烟测试。v36 中 Worker 共被调用 4 轮（AKs fix、boundary retry、size refactor、restore features）。

---

### 步骤 10：质量门禁 `run_quality_gates(version)`

- **触发者**: Orchestrator LLM
- **有无 LLM**: 无
- **做什么** (函数 `run_quality_gates` 内):
  1. `verify_code()` — 编译检查
  2. `run_smoke_test()` — 冒烟对战
  3. `run_decision_test_details()` — 决策测试（≥70% 通过率 + 关键场景全部通过）
  4. `check_code_size()` — 文件行数检查（核心策略文件 ≤ 1500 行，辅助文件 ≤ 1200 行）
- **注意**: 无前置阶段检查 — 质量门禁无条件运行，即使没有 checkpoint 也会执行（此时 `checkpoint_recorded=false`）
- **输出**: `{compile_ok, smoke_ok, decision_pass_rate, decision_ok, critical_scenarios_passed, size_ok, all_passed}` + 详细字段：`compile_errors, smoke_errors, critical_passed, critical_total, critical_failures, decision_failures, scenario_results, total_lines, oversized_files, checkpoint_recorded`
- **输出去向**: 全部通过 → 写入 checkpoint `stage="quality_passed"` + gate `quality`

---

### 步骤 11：代码审查 📎 `run_review(version, source_v, plan)`

| 项目 | 内容 |
|---|---|
| **触发者** | Orchestrator LLM 调用 MCP 工具 `run_review` |
| **调用链** | `tool_pipeline.py:run_review()` → `run_claude_query()` |
| **LLM 角色** | LEAD CODE REVIEWER |
| **模型** | Sonnet |
| **工具** | Bash, Read |
| **Prompt 模板** | `prompts/reviewer_prompt.md` |
| **重试** | 无（单次 LLM 调用） |
| **前置条件** | checkpoint 中 quality gate 必须通过 |

**输入构建** (函数 `run_review` 内):
1. 读取 `prompts/reviewer_prompt.md` 模板
2. 替换占位符：`{master_plan}` = `json.dumps(plan)`、`{version}`、`{parent_version}`
3. 无附加上下文文件 — Reviewer 通过 Bash/Read 自行查看 diff 和代码

**LLM 能做的事**: 用 Bash 运行 `git diff`、Read 读取新旧代码

**LLM 输出**: JSON
```json
{
  "approved": true/false,
  "quality_score": 1-10,
  "change_summary": "...",
  "feedback": "...",
  "risk_areas": ["..."]
}
```

**输出去向**:
- 返回给 Orchestrator LLM
- 审批 → 写入 checkpoint `stage="reviewed"` + gate `review`
- 拒绝 → `stage=None`（保留前一阶段，不回退），Orchestrator 可用 feedback 作为 `reviewer_feedback` 重试 workers

> **💡 真实示例 (v36)**: Reviewer 运行 `git diff bot-v7 -- bots/claude_v36/`，审查 Crossover 产生的代码变更。首轮因死文件拒绝：
> ```json
> {
>   "approved": false,
>   "quality_score": 6,
>   "feedback": "main_backup.py (3268 lines) violates the 1000-line .py file size constraint. This is a dead leftover file — simply DELETE it.",
>   "risk_areas": ["main_backup.py must be deleted", "postflop.py at 960 lines is close to the ceiling"]
> }
> ```
> Orchestrator 删除 `main_backup.py` 后重试，第二轮通过：
> ```json
> {
>   "approved": true,
>   "quality_score": 7,
>   "change_summary": "Crossover v7×v30 with 3 regression fixes: (1) Restored classify_opponent_style() (4 archetypes). (2) Re-enabled river overbet bluffing. (3) Extended AKo+/JJ+ all-in guard.",
>   "risk_areas": ["postflop.py (960 lines) near the 1000-line ceiling", "get_action() is a 685-line monolithic function"]
> }
> ```

---

### 步骤 12：策略评审 📎 `run_critic(version, source_v, plan, reviewer_feedback, force_advance)`

| 项目 | 内容 |
|---|---|
| **触发者** | Orchestrator LLM 调用 MCP 工具 `run_critic` |
| **调用链** | `tool_pipeline.py:run_critic()` → `agent_review.py:_run_critic(next_v, source_v, master_plan_str, ui, prev_critic_result=None)` → `run_claude_query()` |
| **LLM 角色** | STRATEGY CRITIC |
| **模型** | Sonnet |
| **工具** | Bash, Read |
| **Prompt 模板** | `prompts/critic_prompt.md` |
| **前置条件** | checkpoint 中 quality + review gate 必须通过 |

**输入构建** (函数 `_run_critic` 内):
1. 读取 `prompts/critic_prompt.md` 模板
2. 替换占位符：`{master_plan}`、`{version}`、`{parent_version}`
3. 无附加上下文文件 — Critic 通过 Bash/Read 自行查看 diff

**LLM 输出**: JSON
```json
{
  "score": 1-10,
  "approved": true/false,
  "strategic_assessment": "...",
  "feedback": "...",
  "local_optima_warning": true/false
}
```

**通过逻辑** (函数 `run_critic` 内): `score ≥ 6` 且 `approved == true`。当 LLM 输出省略 `approved` 字段时，自动推导 `approved = score >= 6`。

**⚠️ 重要**: `force_advance=true` 时，即使 score < 6，也会写入 `critic_checked` checkpoint（用于耗尽重试后推进，避免重启时无限重试）。`commit_bot` 允许 `force_advanced` 状态下的提交（其他 gate 仍须通过）。

**输出去向**:
- 返回给 Orchestrator LLM
- `action: "approve"` → 写入 checkpoint `stage="critic_checked"` + gate `critic`
- `action: "retry_workers"` → Orchestrator 注入 critic feedback 重试 workers（计入 `intra_gen_attempts`，最多 2 次）
- `action: "force_commit"` → `force_advance=true` 时强制推进 checkpoint（但不是提交许可）

> **💡 真实示例 (v36)**: Critic 独立评估 Crossover 的策略价值，引用 H2H 数据、experience pool 和 diff 作为证据：
> ```json
> {
>   "score": 7,
>   "approved": true,
>   "strategic_assessment": "Well-justified v7×v30 crossover addressing three documented v7 weaknesses: (1) Restores classify_opponent_style() for +2-3pts adaptation. (2) Adds AKo+ all-in guard. (3) Re-enables river overbet bluff with proper guards.",
>   "evidence": {
>     "h2h_weaknesses": ["v7 vs v12: 40.8% win rate (130 games)", "v7 vs v11: 41.8% (110 games)"],
>     "experience_pool_refs": ["classify_opponent_style returns +2-3 pts adaptation", "style_deltas MUST propagate to ALL thresholds"]
>   },
>   "feedback": "Minor concern: AKo all-in guard is unconditional. Against a tight opponent who only jams AA/KK, calling with AKo is -EV.",
>   "local_optima_warning": false,
>   "local_optima_reason": null
> }
> ```
> 首轮 Critic 给了 8.0 分，后续因 Worker 修复中误删了部分 features 降至 7.0。

---

### 步骤 13：提交前验证 `run_precommit_eval(version, source_v, n_games)`

- **触发者**: Orchestrator LLM
- **有无 LLM**: 无
- **前置**: checkpoint 中 quality + review + critic gate 全部通过
- **做什么** (函数 `run_precommit_eval` 内):
  1. 选择对手：父版本 bot + 当前 Top 2 H2H 胜率 + H2H 弱点对手（最多 1 个）+ crossover parent_b（若适用）= 最多 4 个
  2. 与每个对手运行 `mirror_battle(n_games)` — **n_games 硬性上限 5**（`min(max(1, n_games), 5)`），防止 Orchestrator LLM 传入过大值导致超时
  3. Per-opponent timeout 随 n_games 缩放: `max(300s, n_games × 120s)`
  4. 阻断条件：输给父版本、**总输≥3 且 总输≥赢+2**、对局超时、无对手可选（`no_opponents`）、对局异常（`match_exception`）
- **输出**: `{passed, blockers, matchups, total_wins/losses/draws}`
- **输出去向**: 通过 → checkpoint `stage="verified"` + gate `precommit_eval`

> **💡 真实示例 (v36)**: v36 经历了多次 precommit eval。前两次失败（0-1 vs parent, 1-2 vs parent），触发 Worker 修复。最终通过时选择 5 场对战：
> ```
> 对手选择: parent(v7) + top_h2h_wr(v4, v23, v8) + source_h2h_weakness(v30, v6)
> vs parent v7: 2-2-1 (tied, 不触发"输给父版本"阻断)
> vs v23: 3-2 (won)
> aggregate: 7-7-1, blockers=[] → PASSED ✅
> ```

---

### 步骤 14：提交 `commit_bot(version, source_v, strategy, review_approved=false)`

> ⚠️ `review_approved` 默认为 `false`，Orchestrator 必须**显式传递** `review_approved=true`（仅在 `run_review` 返回 `approved:true` 后）。

- **触发者**: Orchestrator LLM
- **有无 LLM**: 无
- **前置**: checkpoint 中所有 gates 必须存在且通过
- **做什么** (函数 `commit_bot` 内):
  1. 验证 gate ledger 完整性（quality + review + critic + precommit_eval）
  2. 验证 `review_approved=true`（quality gates 已在 checkpoint 中验证，**不重新运行** compile/smoke/decision/size）
  3. `git_commit_bot()` — `git add` + `git commit` + `git tag bot-v{N}`
  4. 验证 git tag 确实创建成功
  5. 写入 `.completed` 标记文件
  6. 归档调用：`archive_generation()` 生成快照、`archive_rotate_files()` 归档轮转、`archive_old_logs()` 日志压缩
  7. 清除 pipeline checkpoint（`clear_pipeline_checkpoint()`）
  8. `app_state.set_generation(v)` — 更新 Web UI 生成计数
  9. 发送 `.reap_signal` 通知守护进程刷新 bot 列表
  10. 写入 `priority_eval.json` — 标记新 bot 需要优先评估
- **输出**: `{committed: true, version, source_v, push_ok}`（若池 > 30 额外返回 `needs_reap: true, pool_size`）

> **💡 真实示例 (v36)**: commit_bot 在 v36 中被调用了两次——第一次因 AKs all-in 测试失败被阻断（`committed: false`），修复后第二次成功：
> ```json
> {"committed": true, "version": 36, "source_v": 7, "push_ok": false}
> ```
> Git commit message：
> ```
> evolve: v7 → v36
> parent: claude_v7
> strategy: Crossover v7×v30 (stagnation break). Restored classify_opponent_style()
> (4 archetypes), re-enabled river overbet bluffing, extended AKo+/JJ+ all-in guard.
> Critic score 7.0.
> ```

---

### 步骤 15：归档审计 `run_archivist(version, source_v)`

| 项目 | 内容 |
|---|---|
| **触发者** | Orchestrator LLM 调用 MCP 工具 `run_archivist` |
| **调用链** | `tool_pipeline.py:run_archivist()` → 确定性归档 + 条件性 `agent_master.py:_run_archivist_analysis()` → `run_claude_query()` |
| **有无 LLM** | 有（每次 commit 都调用 LLM，无条件触发） |
| **LLM 角色** | CYCLE ARCHIVIST |
| **模型** | Sonnet |
| **工具** | Bash, Read（通过 `_run_archivist_analysis` 传入 `run_claude_query`） |

**确定性步骤**（始终执行，无 LLM）:
1. **一致性验证**：确认 `.completed` 文件存在、git tag 存在、ratings 包含新 bot
2. **自动 reap**：若活跃 bot > `MAX_ACTIVE_BOTS`(30)，自动调用 `reap_weakest`
3. **加载归档快照**：读取 `results/archive/v{N}.json`（由 `commit_bot` 内的 `archive_generation()` 创建）

**LLM 分析**（**每次 commit 都调用**，无条件触发）:
- 调用 `_run_archivist_analysis(version, source_v, snapshot, ui)` — 分析归档快照，生成本代评估和经验更新
- LLM 输出追加到归档快照的 `archivist_notes` 字段
- 目的：持续积累经验池，非仅用于异常诊断

**输入数据来源**:
- `results/archive/v{N}.json` — 本代归档快照（rating, H2H, review/critic scores, diff stats）
- `results/archive/v{N-4..N-1}.json` — 用于趋势判断

**LLM 输出**: JSON
```json
{
  "generation_assessment": "improvement|neutral|regression",
  "archive_notes": "...",
  "experience_updates": ["..."],
  "strategic_advice": "..."
}
```

**输出去向**: 返回给 Orchestrator LLM。尝试写入 checkpoint `stage="archived"` 然后清除。注意：正常流程中 `commit_bot` 已清除 checkpoint，所以 `_matching_checkpoint` 返回 `None`，`"archived"` 阶段实际上**不会被写入**——该写入逻辑是预防性代码（仅在非正常路径下生效）。

> **💡 真实示例 (v36)**: Archivist 在 v36 中运行了 match_analyst（分析 v36 vs v21: 9W/11L，发现 preflop 65% fold + postflop 0% fold 仍未完全修复）和 performance_verification（trend: stagnant, diversity_needed: true）。v36 的 `archived` checkpoint 未写入（commit_bot 已清除）。

---

### 代际结束（Phase 2 → Phase 3）

Phase 2 `_run_one_cycle()` 检测到 `cycle_completed`:
- 清除 `orchestrator_session.json`
- 返回花费给 `orchestrator_loop()`

Phase 3 `post_generation_cleanup()`（仅在 cost ≥ 0 即成功或非 auth 错误时执行）:
- `reap_if_needed()` — 活跃 bot > 30 时自动淘汰最弱
- `consolidate_experience()` — 每 3 代整合经验池

`orchestrator_loop()` 检查 `shutdown_mgr.is_shutting_down`，若未关闭则 `sleep(5)` 后进入下一代。

---

## 三、重试与恢复流程

### 3.1 代内重试循环（Critic 驱动）

由 Orchestrator LLM 手动管理（非自动），规则来自 `prompts/orchestrator.md`：

```
intra_gen_attempts = 0

Critic 返回 score < 6:
  └── intra_gen_attempts < 2 ?
        ├── 是: intra_gen_attempts++
        │     注入 critic feedback 到 reviewer_feedback
        │     重新调用 execute_workers (从源 bot 重新复制)
        │     → run_quality_gates → run_review → run_critic (再次判断)
        └── 否: force_advance=true 记录到 checkpoint
              但不提交。返回 Master 重新规划 或 尝试 crossover
```

### 3.2 Worker 自修复

每个 worker 的每次尝试后自动执行（在 `_run_single_worker` 内）：

```
尝试 N (1-4):
  run_claude_query(prompt + 失败记忆 + reviewer_feedback)
      │
      ├── 超时 (>1000s) → 简化 prompt，重试
      ├── 编译失败     → 注入编译错误，重试
      ├── 冒烟失败     → 注入运行时错误，重试
      └── 成功         → 返回 True
      
全部 4 次失败 → 记录到 worker_failures.jsonl → 返回 False
```

### 3.3 Worker 串行执行

Workers **始终按顺序逐个执行**（`agent_workers.py` 中 `_execute_workers` 使用简单的 for 循环），不使用并行。这避免了多 Worker 同时修改同一文件导致的竞态条件。

```
_execute_workers():
  for task in tasks:  ← 顺序逐个执行
      │
      ├── _run_single_worker(task)
      │     ├── 尝试 1-4: run_claude_query + verify_code + run_smoke_test
      │     ├── 成功 → 返回 True
      │     └── 全部失败 → 记录到 worker_failures.jsonl → 返回 False
      │
      └── 任一 worker 失败 → 标记 success=False
```

### 3.4 进程层级

```
python web/main.py                          ← 主进程 (uvicorn)
  ├── asyncio.Task: orchestrator_loop        ← 事件循环中的协程
  │     ├── daemon_monitor_thread            ← daemon 线程，3s 轮询，自动重启
  │     └── python elo_daemon.py             ← 子进程，独立进程组 (start_new_session=True)
  │           └── ProcessPoolExecutor        ← n_workers 个 worker 子进程
  │                 └── run_single_match()
  └── FastAPI + SSE streams                  ← Web 服务
```

CLI 模式 (`python web/core/orchestrator.py`) 不启动 daemon，直接在 `asyncio.run()` 中调用 `_run_one_cycle()`。

### 3.5 中断信号链

`ShutdownManager`（`shutdown_manager.py`）统一处理 Web 和 CLI 两种模式的中断信号。使用 `loop.add_signal_handler()`（非 `signal.signal()`）在 asyncio 事件循环内正确处理 SIGINT/SIGTERM。

#### Web 模式 Ctrl+C

```
用户按 Ctrl+C (SIGINT)
  │
  ▼ ShutdownManager._on_signal() → _event.set()
  │
  ▼ orchestrator_loop 主循环检查 is_shutting_down:
  │
  ├─ Phase 1: prepare_generation() 中的 LLM 调用被取消（Disposable，无状态，丢弃即可）
  │
  ├─ Phase 2: _run_one_cycle() 中的 LLM 流被 aclose()
  │     ├─ CancelledError handler:
  │     │     ├─ query_gen.aclose()                     ← 关闭 LLM 流式生成器
  │     │     ├─ 不调用 _clear_orchestrator_session()   ← Session 保留！
  │     │     └─ raise CancelledError                   ← 继续传播
  │     │
  │     └─ Exception handler:
  │           ├─ query_gen.aclose()
  │           ├─ 不调用 _clear_orchestrator_session()   ← Session 保留！
  │           └─ 写入日志 "[ERROR]"
  │
  ├─ Phase 3: post_generation_cleanup() 中断（幂等，可重跑）
  │
  └─ finally:
        ├─ _daemon_stop.set()                          ← 仅停止监控线程
        └─ 不停止 daemon                                ← daemon 独立存活
```

#### CLI 模式 Ctrl+C

```
用户按 Ctrl+C (SIGINT)
  │
  ▼ ShutdownManager._on_signal() → _event.set()
  │
  ▼ 三阶段中断行为与 Web 模式相同
  │
  ▼ KeyboardInterrupt fallback 兜底:
  │     ├─ query_gen.aclose()
  │     ├─ 不调用 _clear_orchestrator_session()        ← Session 保留！
  │     └─ 写入日志 "[INTERRUPTED]"
  │
  └─ finally: stop_daemon()（CLI 模式无 daemon，空操作）
```

**关键修复**: `CancelledError` 和 `Exception` handler（中断信号）均**不再**调用 `_clear_orchestrator_session()`，Session 保留用于恢复。但以下情况**也会**清除 Session：
1. **自然完成**: `commit_bot` 成功后（`cycle_completed=True`）
2. **显式放弃**: API 调用 `abandon` 或 `_startup_recovery` 检测到 stale session
3. **超时**: `TimeoutError` 触发 `_clear_orchestrator_session()`（标记 pipeline 为 `timed_out`）
4. **529 限流**: API rate-limit 时清除 session 并指数退避重试
5. **认证错误**: 401/403 错误时清除 session（防止无效 session 循环）
6. **Orchestrator crash**: 未捕获 `Exception` 时清除 session（竞态条件保护）

### 3.7 重启恢复流程

统一的 `_startup_recovery()` 在 `orchestrator_loop` 启动时执行，根据 checkpoint 和 session 文件的组合状态决定恢复策略。

#### 四种恢复场景

```
python web/main.py / python web/core/orchestrator.py
  │
  └─ _startup_recovery(ui):
        │
        ├─ Case A: checkpoint 不存在 + session 不存在
        │     └─ 返回 {"action": "fresh_start"}
        │
        ├─ Case B: checkpoint 存在 + session 不存在
        │     └─ 返回 {"action": "resume", "session_id": None}
        │           → 新 LLM session，从 checkpoint stage 继续
        │
        ├─ Case C: checkpoint 存在 + session 存在
        │     └─ 返回 {"action": "resume", "session_id": session_id}
        │           → 恢复 LLM 对话 + pipeline stage
        │
        └─ Case D: checkpoint 不存在 + session 存在
              └─ 清除 session，返回 {"action": "fresh_start"}
                    → stale session，丢弃
```

#### 特殊处理

以下 checkpoint 状态被视为无效，清除后返回 fresh_start：
- `stage="archived"` — 已完成并归档，无需恢复
- `stage="prepared"` 且无 `master_plan` — 仅复制了源文件，无实质工作

#### 恢复后的执行路径

```
orchestrator_loop():
  recovery = _startup_recovery(ui)
  │
  ├─ recovery.action == "resume":
  │     ├─ 构建 GenerationContext（从 checkpoint 读取 source_v, next_v）
  │     ├─ 跳过 Phase 1（prepare_generation），直接进入 Phase 2
  │     ├─ 消费 recovery（设为 None），仅恢复一次
  │     └─ _run_one_cycle() 中 LLM 对话已恢复（session_id 存在时 resume=）
  │
  └─ recovery.action == "fresh_start":
        ├─ Phase 1: prepare_generation()（新建 GenerationContext）
        └─ Phase 2: _run_one_cycle()
```

#### Checkpoint 阶段提示映射

`_build_context()` (orchestrator_context.py) 根据 checkpoint 的 stage 注入下一步建议：

| stage | 注入提示 |
|-------|---------|
| `prepared` | Call `run_direction_audit` first |
| `direction_audited` | Direction audited → call `run_master` |
| `master_planned` | Master done → call `execute_workers` |
| `workers_done` | Workers done → call `run_quality_gates` |
| `quality_passed` | Quality passed → call `run_review` |
| `reviewed` | Review passed → call `run_critic` |
| `critic_checked` | Critic done → call `run_precommit_eval` |
| `verified` | Precommit eval passed → call `commit_bot` |
| `archived` | Committed & archived → start next generation |

若 checkpoint 中有 `master_plan`，额外注入: "Master plan is saved in session history — do NOT call run_master again."

#### 部分完成的阶段

- **阶段内崩溃**（如 workers 执行到一半、quality gates 运行到一半）: stage 不变（只有成功才推进），重启后重新执行该阶段
- **阶段间崩溃**（workers 完成但 quality gates 未调用）: checkpoint 显示 `stage="workers_done"`，Orchestrator 被告知调用 `run_quality_gates`
- **Gate 失败后崩溃**（quality 不通过、review 被拒）: stage 停在上一成功阶段，gate 记录 `passed=false`，重启后可重试或放弃
- **无阶段内部分恢复**: 单个 gate 执行中途崩溃后无法恢复进度，必须重新运行（如 decision tests 只完成一半 → 重来）

### 3.8 Daemon 守护进程恢复

#### 孤儿进程检测

`start_daemon()` (evolution_infra.py:394-406) 每次启动时检查 `.daemon_pid`：

```
start_daemon():
  ├─ daemon_proc 已在运行? → 返回
  ├─ .daemon_pid 文件存在?
  │     ├─ 读取 old_pid
  │     ├─ os.killpg(os.getpgid(old_pid), SIGTERM)  ← 杀死孤儿进程组
  │     ├─ sleep(1)
  │     └─ 删除 .daemon_pid
  ├─ subprocess.Popen(start_new_session=True)        ← 独立进程组
  ├─ 写 .daemon_pid (新 PID)
  └─ atexit.register(stop_daemon)                    ← 退出安全网
```

独立进程组（`start_new_session=True`）确保 `killpg` 能干净地终止 daemon 及其所有 `ProcessPoolExecutor` worker 子进程。

#### Daemon 生命周期

```
orchestrator_loop finally:
  ├─ _daemon_stop.set()           ← 仅停止监控线程轮询
  └─ 不调用 stop_daemon()         ← daemon 独立存活，跨 orchestrator 重启

app.py lifespan shutdown:
  └─ stop_daemon()                ← 仅在完整进程退出时终止 daemon

Web UI 显式 stop:
  └─ stop_daemon()                ← 用户通过 API 显式停止
```

**关键变化**: `orchestrator_loop` 的 finally 块**不再**停止 daemon 子进程。Daemon 是独立的评估引擎，仅在以下情况终止：
1. 完整进程退出（`app.py` lifespan shutdown）
2. Web UI 显式调用 stop
3. `start_daemon()` 检测到孤儿进程时替换

#### 监控线程自动重启

`daemon_monitor_thread()` (evolution_infra.py:461-486):

```
3 秒轮询:
  ├─ daemon_proc.poll() is not None? (已退出)
  │     ├─ restart_count > 5 → 停止自动重启，日志报错
  │     ├─ backoff = min(3 * 2^(restart_count-1), 120) 秒
  │     │     → 3, 6, 12, 24, 48 (最多 5 次重启，到 48s 后停止)
  │     ├─ _daemon_stop.wait(backoff)  ← 等待期间可被停止信号中断
  │     └─ start_daemon() 重启
  └─ daemon 正常运行 → restart_count 归零
```

监控线程由 `_daemon_stop` Event 控制生命周期。`orchestrator_loop` finally 中 `_daemon_stop.set()` 使监控线程退出轮询循环，但不影响 daemon 子进程本身。

#### 配置持久化

`app_config.json` (state.py 写入) 保存 daemon 配置，跨重启生效：

```json
{
  "daemon_enabled": true,
  "daemon_workers": 14,
  "daemon_pairs": 5
}
```

由 `main.py` 在 `uvicorn.run()` 前通过 `app_state.update_config()` 写入，lifespan 启动时通过 `app_state.get_config()` 读取。

#### `.reap_signal` 通知

bot 池变更时（`reap_weakest` 工具），写入 `.reap_signal` 文件（含时间戳）。daemon 每 0.5s 检查一次：
- 文件存在且 < 300 秒 → 刷新 bot 列表，清理已淘汰 bot 的 ratings/stats，过滤 match 队列
- 处理后删除文件

### 3.9 恢复文件清单

| 文件 | 写入时机 | 清除时机 | 作用 |
|------|---------|---------|------|
| `orchestrator_session.json` | 每次 SDK `ResultMessage` 返回 `session_id` 时 | 自然完成 / 显式放弃 / 超时 / 529 限流 / 认证错误 / Orchestrator crash | LLM 对话 session ID，用于 `resume` 参数 |
| `pipeline_state.json` | 每个 pipeline 阶段完成时（`_record_gate`） | `commit_bot` 成功后（`clear_pipeline_checkpoint`） | 阶段断点 + gate 结果 + master plan |
| `pipeline_state.json` | **原子写入**：`tmp` + `os.replace()`（POSIX 原子操作） | — | 崩溃安全，不会出现半写状态 |
| `.daemon_pid` | `start_daemon()` spawn 后 | `stop_daemon()` 清理 | daemon 子进程 PID，用于孤儿检测 |
| `app_config.json` | `update_config()` 调用时 | 不清除（永久保留） | daemon 配置（enabled/workers/pairs） |
| `.reap_signal` | `reap_weakest` / `eliminate_bot` 调用时 | daemon 读取后删除 | bot 池变更通知 |
| `worker_failures.jsonl` | worker 全部重试失败时 | 不清除（累积记录） | 注入未来 worker prompt 作为失败记忆 |

### 3.10 PreCompact Hook

当 Orchestrator LLM 的上下文即将被压缩时：

```python
# PreCompact hook 注入 (orchestrator.py 中 _make_precompact_hook 函数):
"=== EVOLUTION STATE — PRESERVE DURING COMPACTION ==="
f"Current completed bot: claude_v{current_v}"
f"ACTIVE GENERATION: v{next_v} (from v{source_v}), stage={stage}. Next tool: {next_step}."
"DO NOT restart this generation — continue from this stage."
# 额外：若 checkpoint 中有 master_plan，注入 worker task 列表
# 阶段映射：archived -> run_archivist
```

确保上下文压缩后 Orchestrator 不丢失进化进度。支持所有阶段包括 `archived`（对应 `run_archivist`）。

---

## 四、辅助 LLM 调用（按需触发）

### 4.1 经验池整合 📎 `consolidate_experience()`

| 项目 | 内容 |
|---|---|
| **触发者** | Orchestrator LLM 调用 `consolidate_experience`（通常每 3 代） |
| **调用链** | `tool_status.py:consolidate_experience()` → `agent_master.py:_consolidate_experience_pool()` → `run_claude_query()` |
| **LLM 角色** | EXPERIENCE CONSOLIDATOR |
| **模型** | Sonnet |
| **工具** | 无 |

**输入**: 读取当前 `experience_pool.md` 全文，嵌入 prompt

**LLM 输出**: 纯 Markdown 文本（去重合并后），要求使用固定分类头：
`## OPPONENT_MODELING` / `## POSTFLOP_STRATEGY` / `## BLUFF_CALIBRATION` / `## PARAMETER_TUNING` / `## GENERAL` / `## RECENT_LESSONS`

**输出去向**: 代码直接 `write()` 回 `experience_pool.md`（不依赖 LLM 的 Edit 工具）。连续 3+ 代重复同类型条目会被标记 `[POSSIBLY EXHAUSTED]`。

---

### 4.2 交叉代理 📎 `run_crossover(parent_a, parent_b, target_v)`

| 项目 | 内容 |
|---|---|
| **触发者** | Orchestrator LLM 调用 `run_crossover`（停滞严重时替代正常流水线） |
| **调用链** | `tool_pipeline.py:run_crossover()` → `agent_review.py:_run_crossover()` → `run_claude_query()` |
| **LLM 角色** | CROSSOVER v{A}×v{B}→v{target} |
| **模型** | Sonnet |
| **工具** | Bash, Read, Edit |
| **Prompt 模板** | `prompts/crossover_prompt.md` |
| **重试** | 最多 3 次 (`MAX_CROSSOVER_RETRIES`) |

**输入构建**:
1. 从 `parent_a` 复制目录作为起点
2. 读取 `prompts/crossover_prompt.md` 模板
3. 替换占位符：`{parent_a_version}`、`{parent_b_version}`、`{version}`

**LLM 能做的事**: 读取两个父 bot 的代码，Edit 合并到目标 bot

**每次尝试后的自动检查**: 编译检查 + 冒烟测试

**输出去向**: 代码写入 `bots/claude_v{target}/`，成功后由 Orchestrator 决定是否提交

> **💡 真实示例 (v36)**: Master 3 次重试失败后，Orchestrator 选择 Crossover v7×v30。Crossover Agent 分析两个父 bot 后制定了合并策略：
> - **从 v30 导入**: `classify_opponent_style()`（4 种对手画像）、`big_pot_safety_guard()`、river exact equity、更保守的 air EQR（0.68/0.56 vs 0.72/0.62）、deterministic blocker bluff
> - **保留 v7**: simulation accuracy、anti-lock pressure、match pressure、anti-bot4 exploitation
> - **Mutation**: SB open threshold 0.49 → 0.47（加宽 SB 开牌范围）
>
> Crossover Agent 用 Read 读取两个父 bot 的全部源码，用 Edit 将合并后的代码写入 `bots/claude_v36/`。执行后自动编译检查+冒烟测试通过。

---

### 4.3 其他辅助工具

以下工具在数据流全景图中未展开，但同样可用：

- **`run_inline_eval(version, n_games)`** — 当守护进程未运行时，手动运行镜像对战并更新 Glicko-2 评分
- **`get_h2h(bot_name, opponent?)`** — 获取指定 bot 的 Head-to-Head 数据，标注 STRENGTH/WEAKNESS
- **`get_bot_stats(bot_name)`** — 获取指定 bot 的累计胜负统计
- **`get_bot_info(version)`** — 获取指定 bot 的详细信息（rating、parent、files、code size）
- **`get_match_history(version, n)`** — 获取指定 bot 的最近对局记录

---

## 五、数据流全景图

三阶段架构：Phase 1（代码层预计算）→ Phase 2（LLM 驱动 pipeline）→ Phase 3（代码层清理）。Phase 1 可丢弃重算，Phase 2 通过 session + checkpoint 持久化保护，Phase 3 幂等可安全重跑。

```
                    ┌─────────────────────────────────────────────┐
                    │         orchestrator_loop() 启动            │
                    │    (后台 asyncio Task, 由 app.py 创建)       │
                    │    + ShutdownManager 信号处理安装             │
                    │    + _startup_recovery() 中断状态评估         │
                    └──────────────────┬──────────────────────────┘
                                       │
                    ┌──────────────────▼──────────────────────────┐
                    │   Phase 1: prepare_generation() — 代码层     │
                    │   (可丢弃，中断后重算)                        │
                    │                                            │
                    │   reap_if_needed() → 池 > 30 时淘汰          │
                    │   wait_for_daemon_eval() → 等待足够对局      │
                    │   _cleanup_incomplete() → 清理孤儿目录       │
                    │   asyncio.gather(                           │
                    │     _run_combined_analysis() 📎 COMBINED LLM,│
                    │     _analyze_recent_matches() 📎 MATCH LLM  │
                    │   )                                         │
                    │   _decide_strategy() → 纯代码策略决策        │
                    │                                            │
                    │   输出: GenerationContext (strategy, source_v,│
                    │          stagnation_info, match_analysis)   │
                    └──────────────────┬──────────────────────────┘
                                       │
                    ┌──────────────────▼──────────────────────────┐
                    │   Phase 2: _run_one_cycle(gen_ctx) — LLM    │
                    │   (状态保留：session + checkpoint 文件)       │
                    │                                            │
                    │   _build_context(gen_ctx) → 注入预计算分析   │
                    │   Orchestrator LLM 自主调用 MCP 工具         │
                    │   Pipeline: master → workers → quality →    │
                    │            review → critic → commit         │
                    │   中断 → session + checkpoint 保留到磁盘     │
                    │   下次启动 → _startup_recovery() 恢复        │
                    └──────────────────┬──────────────────────────┘
                                       │
                    ┌──────────────────▼──────────────────────────┐
                    │   Phase 3: post_generation_cleanup() — 代码层│
                    │   (幂等，可安全中断并重跑)                    │
                    │                                            │
                    │   reap_if_needed() → 淘汰最弱 bot           │
                    │   consolidate_experience() → 每 3 代去重    │
                    └──────────────────┬──────────────────────────┘
                                       │
                    ┌──────────────────▼──────────────────────────┐
                    │   shutdown_mgr.is_shutting_down?            │
                    │     ├── 是 → 优雅退出                        │
                    │     └── 否 → sleep(5) → 回到 Phase 1        │
                    └─────────────────────────────────────────────┘
```

**Phase 2 内部 MCP 工具调用序列（由 Orchestrator LLM 自主编排）：**

```
    ┌──────────────────▼──────────────────────────┐
    │   Orchestrator LLM 会话                      │
    │   输入: _build_context() → combined分析/     │
    │         match分析 + checkpoint 断点           │
    │   工具: 15 MCP tools (见 tools.py mcp_tools) │
    └──────────────────┬──────────────────────────┘
                       │
              ┌────────▼────────────┐
              │run_direction_audit  │
              │(无 LLM，纯代码)     │
              │检查最近进化方向重复  │
              └────────┬────────────┘
                       │
              ┌────────▼────────────┐
              │run_master           │
              │📎 MASTER            │
              │工具: Bash, Read      │
              │输入: 预计算分析      │
              │输出: tasks[]         │
              └────┬────────────────┘
                   │ plan["tasks"]
              ┌────▼────────────────┐
              │prepare_next_gen      │
              │(无 LLM)              │
              │复制 bots/claude_v{N}/│
              │写入 checkpoint       │
              └────────┬────────────┘
                       │
              ┌────────▼────────────┐
              │execute_workers      │
              │📎 WORKERS (串行)    │
              │工具: Bash, Read, Edit│
              │自检: compile+smoke   │
              │熔断: failures≤6    │
              └────────┬────────────┘
                       │
              ┌────────▼────────────┐
              │run_quality_gates    │
              │(无 LLM)             │
              │compile+smoke+decision│
              │+size (≤1500/1200行)  │
              └────────┬────────────┘
                       │
              ┌────────▼────────────┐
              │run_review           │
              │📎 CODE REVIEWER    │
              │工具: Bash, Read      │
              │输出: approved+score  │
              └────────┬────────────┘
                       │
              ┌────────▼────────────┐
              │run_critic           │
              │📎 STRATEGY CRITIC  │
              │工具: Bash, Read      │
              │阈值: ≥6 通过         │
              └────────┬────────────┘
                       │
              ┌────────▼────────────┐
              │run_precommit_eval   │
              │(无 LLM)             │
              │镜像对战: vs父+Top    │
              │n_games上限: 5       │
              └────────┬────────────┘
                       │
              ┌────────▼────────────┐
              │commit_bot (无 LLM)  │
              │git commit + tag     │
              │清除 checkpoint       │
              └─────────────────────┘
```

**后台守护进程（独立于 Orchestrator 生命周期）：**

```
    ┌────────────────────────────────────────────────────┐
    │  elo_daemon.py (独立子进程，orchestrator 停止不影响) │
    │    ├── ProcessPoolExecutor 并行对战               │
    │    ├── 每 game 实时更新 Glicko-2 rating           │
    │    ├── 写入: ratings, h2h, bot_stats, history,    │
    │    │         replay (≤200), daemon_stats          │
    │    └── 响应 .reap_signal 刷新 bot 列表            │
    └────────────────────────────────────────────────────┘
```

---

## 六、全局约束

- **所有 LLM 调用统一使用 Sonnet 模型**，通过 `claude_agent_sdk` 的 `query()` 函数
- **API 限流 (529)**: `run_claude_query()` 内自动指数退避重试（30s → 60s → 120s）
- **Prompt 预算**: `MAX_PROMPT_CHARS = 700_000`，超限时按文件均分压缩上下文
- **MCP 工具**: 15 个工具注册在 `tools.py` 的 `mcp_tools` 列表中，通过 `create_sdk_mcp_server(name='evolution', tools=mcp_tools)` 暴露给 Orchestrator LLM。工具来自 `tool_planning.py`（direction_audit, master, workers）、`tool_gates.py`（quality_gates, review, critic, prepare_next_gen）、`tool_eval.py`（precommit_eval, inline_eval）、`tool_commit.py`（commit, archivist）、`tool_status.py`（查询、daemon 控制、bot 管理）。**注意**: `get_status` 等工具仅在 `all_tools`（HTTP 端点 `/api/control/tool/`）中可用，不在 MCP 中。
- **子代理 MCP 屏蔽**: `_BLOCKED_MCP_TOOLS` 屏蔽以下外部工具（防止子代理访问网络）：
  - `mcp__web-reader__webReader`
  - `mcp__web-search-prime__web_search_prime`
  - `mcp__zread__get_repo_structure`
  - `mcp__zread__read_file`
  - `mcp__zread__search_doc`
- **角色边界**: Worker 受 prompt + reviewer 双重约束 — Logic Architect 不改常数，Tuner 不加函数
- **Gate Ledger**: Pipeline checkpoint 强制阶段顺序 — 每个阶段写入 gate 记录，后续阶段验证前置 gates 完整
- **阶段常量**: `STAGE_ORDER = [prepared, direction_audited, master_planned, workers_done, quality_passed, reviewed, critic_checked, verified, archived]`
- **ShutdownManager**: `loop.add_signal_handler()` 注册 SIGINT/SIGTERM，设置 `is_shutting_down` 标志，Phase 1/3 检查后优雅退出，Phase 2 等待当前 LLM 调用完成
- **Pipeline checkpoint 原子写入**: `pipeline_state.json` 使用 tmp + `os.replace()` 原子替换，避免中断导致文件损坏
- **Session 持久化策略**: `orchestrator_session.json` 在自然完成、超时、529 限流、认证错误、Orchestrator crash 时清除；`CancelledError` / 用户中断信号时保留 session 到磁盘，下次启动 `_startup_recovery()` 恢复会话
- **Daemon 独立生命周期**: `elo_daemon.py` 作为独立子进程运行，orchestrator 停止不影响 daemon 持续评估；daemon 仅通过 `.reap_signal` 文件与 orchestrator 通信
- **归档阶段**: Phase 3 中 `reap_if_needed()` + `consolidate_experience()` 在 commit 后执行，幂等可安全重跑

## 附录：真实循环示例（v28 → v29）

> ⚠️ **历史示例**: 以下示例基于**旧架构**（Phase 2 中 Orchestrator 可直接调用 `get_status` 等 MCP 工具）。当前架构中，这些操作已移入 Phase 1 代码层，`get_status` 不再是 MCP 工具。工具调用序列仅供理解恢复流程参考。

以下展示一个真实的完整进化循环，基于实际日志文件。

### 背景

- **源 bot**: claude_v28 (r=1581, 160 games, 60% WR)
- **目标 bot**: claude_v29
- **结果**: 成功提交 (`git tag bot-v29`, commit `e3101ad`)

### 实际工具调用序列

```
get_status() → run_review(v29, v28) → run_critic(v29, v28) → commit_bot(v29, v28)
```

> 注：此循环从 `quality_passed` checkpoint 恢复（之前因认证失败中断），因此跳过了 Master 和 Worker 阶段。

### 各阶段真实输出

**1. get_status()**
```json
{
  "current_v": 28,
  "active_bots_count": 28,
  "rating_reliable": true,
  "incomplete_next_v": 29,
  "current_bot_h2h_avg_wr": 0.60
}
```

**2. run_review(v29, v28)** — 5 次审查循环

- Review 1-4: 均被拒绝（发现 bug：缺少 `TOTAL_HANDS` 导入、river 3-branch 抢占 nut overbet、preflop tier 防御折叠 AA/KK 等）
- Review 5: **通过** (score: 7/10)

```json
{
  "approved": true,
  "quality_score": 7,
  "change_summary": "Added 5-tier preflop hand classification system, river 3-branch decision framework...",
  "risk_areas": [
    "River 3-branch may bypass nuanced edge-case handling",
    "Raise sizing increased 10-15% across all streets",
    "betting.py doubled in size (305→605 lines)"
  ]
}
```

**3. run_critic(v29, v28)** — 通过 (score: 7/10)

```json
{
  "score": 7,
  "approved": true,
  "strategic_assessment": "River 3-branch fixes confirmed 0% raise/fold leak... Preflop 5-tier system provides principled decisions...",
  "local_optima_warning": false,
  "local_optima_reason": "v28 added board texture, v27 added fold gate, v26 added SPR awareness. v29 adds preflop tier + river 3-branch — fundamentally different."
}
```

**4. commit_bot(v29, v28)**

```json
{
  "committed": true,
  "tag": "bot-v29",
  "sha": "e3101ad"
}
```

Git commit message:
```
evolve: v28 → v29

parent: claude_v28
strategy: Gen 29 from v28: Added 5-tier preflop hand classification system, 
river 3-branch decision framework (strong/medium/weak) to fix 0% raise/fold leak, 
widened BB defense with tier-based logic, increased raise sizing ratios ~10-15% 
across all streets, tightened thresholds for tighter-aggressive profile. 
Review score 7, Critic score 7.
```

### 关键文件变更

| 文件 | 变更 |
|---|---|
| `state.py` | +79 行：新增 `classify_preflop_tier()` |
| `betting.py` | +300 行：river 3-branch 系统、preflop tier 集成 |
| `strategy.py` | -10 行：导入新函数、BB 防御、river 分发 |

### 相关日志文件

- `web/logs/orchestrator_20260530_155557.txt` — Orchestrator 循环
- `web/core/results/v29/logs/master_io.txt` — Master 规划
- `web/core/results/v29/logs/worker_1_io.txt` — Worker 1（Logic Architect）
- `web/core/results/v29/logs/worker_2_io.txt` — Worker 2（Hyperparameter Tuner）
- `web/core/results/v29/logs/reviewer_io.txt` — 5 次审查记录
- `web/core/results/v29/logs/critic_io.txt` — Critic 评估
- `web/core/results/v29/logs/match_analyst_io.txt` — 赛后分析
- `web/core/results/v29/logs/performance_verification_io.txt` — 性能验证
- `web/core/results/v29/logs/stagnation_analysis.txt` — 停滞分析

---

## 附录：非典型路径示例（v3 → v35）

以下展示一个包含多次重试和低分通过的循环。

### 背景

- **源 bot**: claude_v3（非最近 lineage，因停滞分析建议 branch）
- **目标 bot**: claude_v35
- **特点**: Worker 多次重试、Critic 低分通过、无 Reviewer 日志

### 关键偏差

1. **Worker 1 收到 5 次顺序 prompt**（非单次执行）：
   - Prompt 1: 加宽 preflop 防御
   - Prompt 2: 修复 AKs 折叠 bug + 压缩文件
   - Prompt 3: 修复 `sanitize_action` bug
   - Prompt 4-5: 验证/无操作

2. **Critic 评分 6/10（最低通过线）**，反馈：
   - "preflop threshold magnitudes are extreme"
   - "~900 lines of whitespace-only changes make review harder"
   - "constants.py is identical to worker prompt claim of 'reverted SIZING_TABLE'"

3. **无 Reviewer 日志** — `web/core/results/v35/logs/reviewer_io.txt` 不存在

4. **Precommit 结果**: 3-2-0（边际通过）

### 结果

- **提交**: 是 (`git tag bot-v35`, commit `06d5743`)
- **策略**: Fixed sanitize_action all-in bug, widened preflop defense, lowered air-hand EQR

> 此示例说明：即使 Critic 低分通过、Worker 多次重试，系统仍可能完成提交。这符合 `force_advance` 的设计意图 — 在重试耗尽后推进，避免无限循环。

---

## 附录3：完整生命周期示例（v7 → v36）

> ⚠️ **历史示例**: 以下示例基于**旧架构**（Phase 2 中 Orchestrator 直接调用 `get_status`、`wait_for_eval`、`run_match_analysis`、`run_performance_verification` 等 MCP 工具）。当前架构中，这些操作已移入 Phase 1 代码层（`prepare_generation()`）。步骤 1-6 在新架构中由代码层自动完成，不再需要 Orchestrator LLM 调用。

以下展示一个包含 **Master 失败、Crossover 替代、4 轮 Worker 修复** 的完整进化循环。数据来自 orchestrator 日志和各阶段 LLM 输出。

### 背景

- **源 bot**: claude_v7（H2H avg 55.56%, 停滞 7+ 个评估周期）
- **目标 bot**: claude_v36
- **结果**: 成功提交（`git tag bot-v36`, commit `6b23ed3`）
- **路径**: v35 刚提交 → reap v1 → 分析 v7（停滞）→ Master 失败 ×3 → Crossover v7×v30 → 4 轮 Worker 修复 → 最终通过
- **总耗时**: 约 3 小时（00:17 - 03:01）

### 完整工具调用序列

Orchestrator 在本代共调用约 **35 次** MCP 工具。按阶段分组：

**准备阶段（步骤 1-6）**

| # | 工具调用 | 结果 |
|---|---|---|
| 1 | `get_status()` | v35 刚提交，31 active bots（超 30 上限），无未完成代 |
| 2 | `reap_weakest()` | 移除 claude_v1（h2h_avg_wr: 9.9%），剩 30 bots |
| 3 | `wait_for_eval(v35)` | 0 games, rating unreliable → 跳过 v35 分析 |
| 4 | `get_bot_info()` + `get_h2h()` | 查看 v7 对战数据 |
| 5 | `run_match_analysis(v7)` | preflop 65% fold, postflop 0% fold, underbetting 0.4-0.7x pot |
| 6 | `run_performance_verification(v7)` | stagnant 7+ periods, `diversity_needed: true` |

**Master 失败 → Crossover 替代（步骤 7-10）**

| # | 工具调用 | 结果 |
|---|---|---|
| 7 | `run_master()` | **失败 ×3** — Master 产出 3 版计划（2-worker→2-worker→1-worker），均因 JSON 格式问题被拒绝 |
| 8 | `run_crossover(v7, v30, v36)` | 第一次失败 — v36 目录已存在（孤儿，无 git tag） |
| 9 | `cleanup_incomplete()` + Bash | 删除孤儿 v36 目录 |
| 10 | `run_crossover(v7, v30, v36)` | **成功** — v7（top H2H avg 55.56%）× v30（beats v7 at 55%） |

**第一轮 Pipeline（步骤 11-16）**

| # | 工具调用 | 结果 |
|---|---|---|
| 11 | `run_quality_gates()` | ALL PASSED ✅（12/12 critical scenarios, 100% decision） |
| 12 | `run_review()` | APPROVED ✅（score 7） |
| 13 | `run_critic()` | APPROVED ✅（score 8.0） |
| 14 | `run_precommit_eval()` | FAILED ❌（0-1 vs parent v7，1 场样本不足） |
| 15 | `run_precommit_eval()`（retry, 3 games） | PASSED ✅（2-0-1 vs parent, 4-4-1 aggregate） |
| 16 | `commit_bot()` | **BLOCKED** ❌ — `preflop_aks_facing_allin` test failed（folded AKs vs all-in） |

**Worker 修复循环 ×4（步骤 17-26）**

commit_bot 因 AKs 测试失败被阻断，触发 4 轮 Worker 修复：

| # | 工具调用 | 原因 | 结果 |
|---|---|---|---|
| 17 | `execute_workers()`（fix AKs） | AKs all-in guard 缺失 | **失败**：boundary violation（修改了声明外的文件） |
| 18 | `execute_workers()`（retry） | 扩展 target_files | 成功，但 file size 超限 |
| 19 | `run_quality_gates()` | — | decision pass, BUT strategy.py 1164 行 / postflop.py 1015 行 |
| 20 | `execute_workers()`（refactor） | 拆分大文件到 <1000 行 | 成功 |
| 21-23 | `quality_gates` → `review(7)` → `critic(7.0)` | — | ALL PASSED ✅ |
| 24-25 | `precommit_eval()` ×2 | — | FAILED ❌（1-2 vs parent, 5-7-3 aggregate） |
| 26 | `execute_workers()`（restore features） | Crossover 中被误删的 classify_opponent_style + river overbet bluff | 成功 |

**最终 Pipeline 通过（步骤 27-35）**

| # | 工具调用 | 结果 |
|---|---|---|
| 27 | `run_quality_gates()` | ALL PASSED ✅ |
| 28 | `run_review()` | **REJECTED** ❌（main_backup.py 3268 行违反 1000 行约束） |
| 29 | Bash: `rm main_backup.py` | Orchestrator 直接删除死文件 |
| 30 | `run_review()`（retry） | APPROVED ✅（score 7） |
| 31 | `run_critic()` | APPROVED ✅（score 7.0） |
| 32 | `run_precommit_eval()`（5 games） | PASSED ✅（2-2-1 vs parent, 7-7-1 aggregate） |
| 33 | `commit_bot()` | **SUCCESS** ✅（`bot-v36` tagged） |
| 34 | Bash: `git push` | 推送到远程 |
| 35 | `run_archivist()` | 轻微错误（无关），commit 安全 |

### 关键节点真实输出

**1. Crossover v7×v30 决策**

Master 失败后 Orchestrator 的推理：
```
Master failed after 3 retries. Since stagnation is confirmed and diversity is needed,
I'll take the crossover approach — combining v7 (top h2h_avg_wr: 0.5696)
with v30 (beats v7 at 55%).
```

Crossover 导入 v30 的核心特性：
- `classify_opponent_style()`（4 种对手画像：tight/loose/aggressive/passive，+2-3pt 适应性）
- River overbet bluff（blocker quality gating）
- 确定性诈唬替代随机诈唬

保留 v7 的核心：simulation accuracy、anti-lock pressure、match pressure
Mutation: SB open threshold 0.49 → 0.47

**2. commit_bot 被阻断**

```
Commit BLOCKED ❌ — preflop_aks_facing_allin test failed
(folded AKs vs all-in, a catastrophic blunder)
```

Crossover 的代码重构破坏了 preflop all-in guard（AKs 面对全下应该 call 却 fold 了）。这是一个灾难性错误，quality gates 中的 critical scenario 测试没捕获（可能是非确定性行为），但 commit_bot 内置的最终校验发现了。

**3. Reviewer 因死文件拒绝**

```json
{
  "approved": false,
  "quality_score": 6,
  "feedback": "main_backup.py (3268 lines) violates the 1000-line .py file size constraint.
  This is a dead leftover file not referenced by any operational code — simply DELETE it."
}
```

`main_backup.py` 是 v7/v30 的遗留备份文件（3268 行），不被任何代码引用，但 Reviewer 严格执行了"所有 .py 文件 ≤1000 行"的约束。Orchestrator 直接用 Bash 删除了它。

**4. 最终 Gate Ledger**

```
✅ quality_passed: true (12/12 critical, size_ok)
✅ critical_scenarios_passed: true
✅ review_approved: true (score 7)
✅ critic_score: 7.0 (approved)
✅ precommit_eval: passed (7-7-1, tied parent v7)
```

### 结果

| 项目 | 内容 |
|---|---|
| 提交 | `git tag bot-v36`, commit `6b23ed3` |
| 策略 | Crossover v7×v30（stagnation break） |
| Worker 迭代 | 4 轮（AKs fix → boundary retry → size refactor → restore features） |
| Pipeline 重跑 | 3 次完整 cycle（quality→review→critic→precommit） |
| Review score | 7（第 2 次通过，第 1 次因 main_backup.py 拒绝） |
| Critic score | 7.0（首轮 Critic 8.0，后续因删除 features 降至 7.0） |
| 文件变更 | 6 files, +502/-549 |

> 此示例展示了进化系统的三种恢复机制协同工作：(1) **Master 失败后 Crossover 替代** — 当 stagnation 确认且 diversity needed 时，Orchestrator 自动切换到遗传交叉路径；(2) **commit_bot 阻断后 Worker 修复循环** — AKs 测试失败触发 4 轮迭代修复，含 boundary violation 重试、文件大小重构、误删功能恢复；(3) **Reviewer 因死文件拒绝后清理重试**。共 35 次工具调用、3 次 Pipeline 完整重跑，耗时约 3 小时。
