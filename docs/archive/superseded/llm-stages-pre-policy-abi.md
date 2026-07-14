# LLM 多阶段运行时数据流

本文档以 **时间线** 视角，描述 `python web/main.py` 启动后系统内逐一发生的事件，聚焦每个 LLM 调用的数据流：谁发起、输入什么、输出什么、输出去向。

---

## 一、启动序列（三阶段架构）

系统采用**代码层调度 + LLM 单代执行**的三阶段架构。代码层（`generation_scheduler.py`）负责 Phase 1 和 Phase 3，LLM 仅在 Phase 2 驱动 pipeline。

```
python web/main.py
        │
        ▼
  CLI 参数解析 (--port [PORT env], --host, --no-daemon, --view-only, --dev, --no-build)
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
                │     └── pipeline: prepare_next_gen(or run_crossover) → direction_audit → optional literature_probe → master → workers → quality → review → critic → precommit_eval → commit → archivist
                │     → 中断时 session + checkpoint 保留
                │
                ├── Phase 3: post_generation_cleanup(shutdown_mgr, ui, ctx) — 代码层
                │     ├── 仅已 tag 的版本继续执行；未提交/已放弃版本统一跳过
                │     ├── reap_if_needed()
                │     ├── consolidate_experience() (每3代 或 RECENT_LESSONS≥4)
                │     ├── exploitability probe / QD eval（仅已 commit + bot-vN tag 的版本）
                │     └── behavior fingerprint 等后清理记录
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
- **做什么**: 调用 `find_current_v()`（从 git tags 读取最新 bot 版本）+ `get_active_bots()`（扫描 `bots/` 目录）。若活跃 bot 数 > `MAX_ACTIVE_BOTS`(30)，**先自动淘汰**最弱 bot（`_do_reap_weakest`，最多 10 轮），然后重新绑定实际活跃池和等待目标。ratings 不在等待前缓存。
- **输出**: `current_v`、等待目标和刷新后的 `active_bots`（仅 `current_v` 存入 GenerationContext）

> **旧对比**: 原 Step 1 `get_status()` 由 Orchestrator LLM 触发，返回 13 个字段的 JSON 快照。现在这些数据直接在代码层获取，不再经过 LLM。

---

#### Phase 1.2：等待评估（代码直接调用）

- **触发者**: `prepare_generation()` 代码层
- **有无 LLM**: 无
- **做什么**: 异步轮询 `bot_stats.json` 和 ratings，等待守护进程为当前 bot 积累足够对局。当前 `national_native` profile 的双退出条件是 **≥ 24 个完整 70-hand 样本**，或 **rd < 110 且 ≥ 12 个样本**；超时 600s。
- **输出**: 初步 `eval_ok`。放行后代码仍会从稳定文件重新读取并复验，不把该布尔值当成可直接交给 LLM 的证据。

**等待后证据契约**:

1. 重新确认活跃池和来源版本在等待期间没有变化。
2. daemon 在独占 cycle lock 内依次写 H2H、bot stats、ratings、daemon stats、derived selection rows，并把 match/rating JSONL 的精确字节截止点复制进不可变 cycle 目录；最后才原子推进 `evaluation_cycle_manifest.json`。中途崩溃不会推进 pointer。
3. generation 校验 content-addressed cycle、五份 JSON payload 和两份 append-log 副本，再原子固化到 `vN/evidence_snapshot/`；缺文件、hash 不符、active pool 不符或 identity 变化都 fail closed。
4. 用冻结 bundle 中的 games/RD 再次验证 24-sample 或 RD 提前退出条件；任一不一致都返回 `None`，由下一轮重新准备。
5. H2H、ratings、bot stats、rating-history tail 和包含筹码次级指标的 selection rows 被编译为不可变 `SelectionView`。Combined、leader/source/oscillation/crossover parent 选择及其审计日志共用这一 identity，不再回读 live strength 文件。

> **旧对比**: 原 Step 3 `wait_for_eval()` 由 Orchestrator LLM 触发。现在代码层自动等待，无需 LLM 决策。

---

#### Phase 1.3：清理残留（代码直接调用）

- **触发者**: `prepare_generation()` 代码层
- **有无 LLM**: 无
- **做什么**: `_cleanup_incomplete()` — 删除无 `.completed` 且无 git tag 的残留 bot 目录。**Checkpoint 感知**: 若目录版本与活跃 `pipeline_state.json` 的 `next_v` 匹配且 stage 不为 None/archived，则**跳过删除**（保护中断恢复状态）。
- **输出**: 无（副作用：清理文件系统）

> **旧对比**: 原 Step 2 `housekeeping()` 由 Orchestrator LLM 按需调用。现在代码层自动执行，每代循环开头清理一次。

---

#### Phase 1.4：合并分析（停滞+性能）📎 `_run_combined_analysis(..., frozen_h2h, frozen_bot_stats)`

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
1. 使用 daemon cycle 中冻结的最近 10 个 rating-history 周期，提取 top H2H 胜率
2. 使用 `web/core/results/vN/evidence_snapshot/head_to_head.json` — 本代冻结的 H2H 胜率和对手覆盖率
3. 使用同一 generation bundle 的 frozen bot stats、ratings 和 selection rows 获取总体胜率、RD、场次及排序
4. 计算 Top 5 活跃 bot 列表（含 RD 警告）
5. 从 git tags 提取最近 8 代进化趋势（vN: h2h_avg_wr + coverage）
6. 从 git history 提取 lineage（vN ← parent: vM）
7. 读取 `worker_failures.jsonl` 最近 5 条失败记录
8. 加载上代 Critic 洞察（`archive/vN.json` → critic_data.strategic_assessment）
9. 拼装 prompt：`combined_analyst.md` 模板 + 全部数据

**输入数据来源**:
- `web/core/results/vN/evidence_snapshot/selection_snapshot.json` — 同周期 selection rows + rating-history tail
- `web/core/results/vN/evidence_snapshot/head_to_head.json` — 本代冻结 H2H
- `web/core/results/vN/evidence_snapshot/glicko_ratings.json` + `bot_stats.json` — 本代分析和选源共用
- `web/core/results/archive/vN.json` — 上代 Critic 洞察
- `web/core/results/worker_failures.jsonl` — Worker 失败记录

**对手覆盖率检查**: canonical `h2h_coverage/h2h_opponents/h2h_opponents_total` 统一映射到 analyst schema；若覆盖率 < 80%，直接返回 safe_default（跳过 LLM），不能用缺省字段把 `2/10` 误判成 `0/0=100%`。

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

**LLM 输出**: 纯文本（结构化分析）。`_analyze_recent_matches()` 返回原始 LLM 输出字符串（`agent_master.py: return output or ""`），不解析为 JSON。

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
elif combined.recommendation=="branch" && branch_from 是 active + .completed + bot-vN tag-backed:
    → strategy="master", source_v=branch_from
elif combined.diversity_needed && 有可用 crossover parents:
    → strategy="crossover", source_v=parent_a, parents=(parent_a, parent_b)  # 强制多样性注入
elif combined.recommended_source 是 active + .completed + bot-vN tag-backed:
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
| `next_v` | int | `compute_next_generation_v(current_v, max_committed_v, abandoned_floor)` |
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

### 步骤 7：准备下一代 `prepare_next_gen(source_v, next_v)`

- **触发者**: Orchestrator LLM
- **有无 LLM**: 无
- **前置**: Phase 1 已决定 `strategy="master"` 且给出 `source_v/next_v`
- **做什么**:
  1. 拒绝 `next_v ≤ source_v`
  2. 拒绝源 bot 不存在或未完成（无 `.completed` 或缺少 `bot-v{source_v}` tag）
  3. 拒绝 pipeline stage 已超过 `prepared`
  4. 拒绝覆盖已完成的 bot（有 `.completed`）
  5. `shutil.copytree()` 将 `bots/claude_v{source_v}/` 复制为 `bots/claude_v{next_v}/`
  6. 删除 `.completed` 标记文件
  7. 写入 pipeline checkpoint：`stage="prepared"`、`worker_failure_count=0`
- **输出**: `{prepared: true, next_v, source_v}`

---

### 步骤 8：方向审计 `run_direction_audit(next_v, source_v, ...)`

- **触发者**: Orchestrator LLM
- **有无 LLM**: 是（Direction Auditor）
- **前置**: checkpoint 已到 `prepared`
- **做什么**:
  1. 读取近代 commit/message、失败记录、经验池 exhaustion 标记和当前 Phase 1 分析
  2. 判断是否重复已经证伪或耗尽的方向
  3. 给 Master 输出 `mandatory_constraints`、`suggested_direction`、`exhausted_directions`
  4. 写入 checkpoint：`stage="direction_audited"` + `direction_audit`
- **输出**: 方向审计 JSON；这是 Master prompt 的约束输入，不是最终提交门。

---

### 步骤 8.5：受治理文献探针 `run_literature_probe(...)`

- **触发条件**: canonical Master context 判定停滞，或 Direction Auditor 判定方向重复。
- **硬路由**: 此时 `direction_audited` 只能进入 literature probe；不能直接跳到 Master。
- **上下文身份**: 每个完成、governed skip、timeout 或 provider failure 回执都必须同时绑定
  Master-context digest、完整 Direction-Audit digest 和 requirement-context digest。旧回执或调用方
  改写后的 weakness/stagnation 文本不能满足当前世代。
- **无副作用边界**: 探针只产生待验证 hypothesis，不修改 bot；Master/Worker 与本地 native TCP
  gates 仍负责实现和盈利性判定。

---

### 步骤 9：主架构师规划 📎 `run_master(source_v, next_v, stagnation_info, match_analysis, performance_verification)`

| 项目 | 内容 |
|---|---|
| **触发者** | Orchestrator LLM 调用 MCP 工具 `run_master` |
| **调用链** | `tool_planning.py:run_master()` → `agent_master.py:_run_master_analysis()` → 3 proposal scouts → 2 blind critics → final `run_claude_query()` |
| **LLM 角色** | MASTER |
| **模型** | Sonnet |
| **工具** | Read only；所有 results 路径只允许本代 exact `evidence_snapshot/` |
| **Prompt 模板** | `prompts/master_prompt.md` |
| **重试** | 最多 3 次 (`MAX_MASTER_RETRIES`)，每次需返回含 `tasks` 的 JSON |

**输入构建** (函数 `_run_master_analysis` 内):
1. 读取 `prompts/master_prompt.md` 模板
2. 替换占位符：`{stagnation_info}`、`{match_analysis}`（裁剪至 10K 字符）、`{performance_verification}`（裁剪至 4K 字符）、`{source_v}`
3. prompt 仅提供代际冻结且带 digest 的 `evidence_snapshot/head_to_head.json`、selection 等精确路径。replay/lesson/experience 是 prepare 阶段裁剪后直接注入的 bounded excerpts，不再要求 Master 打开 live results。
4. `run_claude_query()` 的 `context_files` 参数为 **空列表 `[]`**；Read guard 对 operator/runtime 双 checkout、复制的 results 树和路径穿越都 fail closed，只有 exact snapshot 目录放行。
5. 系统编译并校验 prepared artifact、能力债务、运行时合同和
   `strategy_reference_pack.py` 的 typed card。LLM 可以消耗更多 token 做分析，
   但不能用自由文本改写这些权限或凭据。
6. 最终 Master 前并行采样 3 个独立结构化机制提案（mechanism、counterfactual、compute/memory 视角），先做确定性 schema/源覆盖/重复过滤，再由 2 个 blind critic 分别按可证伪性和可达性排序。代码层用 Borda 顺序编译为 advisory packet；critic 不能修改 source、evidence、scope 或 gate。最终 Master 仍只选/合成一个可归因机制，并通过原有 Pydantic/plan compiler/validator。

> ⚠️ **注意**: 早期版本文档错误描述为 `context_files` 传入文件路径列表或允许 Bash。当前 Master 的 `context_files=[]` 且只有 Read；live evidence 不在精确 snapshot 下就会被 hook 拒绝。

**LLM 能做的事**: 用 Read 检查 source/target 代码、reference bots 和 exact frozen snapshot，并结合已注入的 rating/replay/经验摘要提出可证伪机制。

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
  "analysis": "..."
}
```

**校验** (函数 `_validate_master_plan`):
- tasks 数量 ≤ 3
- 每个 task 的 target_files ≤ 3
- 每个 task 的 worker_prompt 建议 ≤ 6000 字符，硬上限 12000 字符
- **Tuner 目标文件限制**: Hyperparameter Tuner 的 `target_files` 必须仅含 `constants.py`，指向其他文件会触发**硬错误**（阻断 plan，非警告）
- **Architect-Tuner 文件重叠检测**: 若 Architect 和 Tuner 共享任何 target_file，触发硬错误（因为 Tuner 边界检查会看到 Architect 的结构性改动，导致误判为越界）
- Hyperparameter Tuner prompt 会被 `_TUNER_STRUCTURAL_PATTERNS` 检查，含结构化指令（如 "add parameter"、"new function"）时发出边界警告（非阻断，reviewer/critic 执行实际约束）

**输出去向**: 返回给 Orchestrator LLM → Orchestrator 用 `plan["tasks"]` 调用 `execute_workers()`。

> **💡 真实示例 (v9)**: Master 分析了 v8 的 combined analysis（v6 52.7% → v7 47.7% → v8 47%，三代下降）和 match analysis（0% postflop fold rate, calling station），制定了 2-worker 计划：
> ```json
> {
>   "analysis": "v8 H2H avg 46.3% (7 opponents), 3-generation decline from v6 peak (52.7%). Match analysis reveals calling station: 0% postflop fold on every street, avg raise only 0.4x pot. Crossover recommended for structural diversity.",
>   "targeted_failure": "Zero postflop fold rate across all streets — never folds after seeing flop. Tight-passive preflop (48-55% fold, only 9-19% raise). Underbetting raises at 0.4x pot.",
>   "tasks": [
>     {"worker_id": 1, "role": "Algorithmic Logic Architect", "target_files": ["strategy.py"]},
>     {"worker_id": 2, "role": "Hyperparameter Tuner", "target_files": ["constants.py"]}
>   ]
> }
> ```
> 注：此 plan 因 Worker role boundary violations（Tuner 改了 strategy.py 中的数字常量）被 Reviewer 拒绝。系统最终改用 Crossover v6×v2 路径。

---

### 步骤 10：Worker 编码 📎 `execute_workers(tasks, next_v, source_v, reviewer_feedback)`

| 项目 | 内容 |
|---|---|
| **触发者** | Orchestrator LLM 调用 MCP 工具 `execute_workers` |
| **调用链** | `tool_planning.py:execute_workers()` → `agent_workers.py:_execute_workers()` → `_run_single_worker()` × N → `run_claude_query()` |
| **LLM 角色** | WORKER {id} ({role}) |
| **模型** | Sonnet |
| **工具** | Bash, Read, Edit |
| **Prompt 模板** | `prompts/worker_prompt.md` |
| **执行方式** | 默认并行（最多 3 个 worker）；target_files 重叠、目标不明或 API 压力较高时降级串行 |
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
   - 对完整 artifact（含二进制表格/模型/配置）做 byte snapshot，是否修改了未声明的 `target_files/files_allowed`
   - `must_change_files` 只表示必须完成，不能反向扩充写权限
   - Hyperparameter Tuner 是否修改了非数字内容（通过 `_numbers_only_changed` 检测）
   - 见 `tool_helpers.py:_validate_worker_boundaries`
4. **系统拥有返工权限**: quality/precommit/official 的反馈和 repair task 从
   checkpoint/gate receipt 重建；调用方非空 echo 必须与 canonical 值完全一致，
   不能伪造 blocker 或夹带无签名 task。
5. **批次原子性**: 多 Worker 中任一执行、边界或 transient-context 清理失败，
   整个批次恢复到执行前的完整 artifact bytes；不会把第一个 Worker 的半成品
   留给 prepared/repair baseline 的下一轮。
6. **容量边界**: snapshot 在读取 payload 前先检查 1024 files、2048 entries、
   depth 64、16 MiB/file、64 MiB total；稀疏巨文件不能借快照导致 OOM。

**输出**: `{success: bool, boundary_errors: [], logs, costs}`

**输出去向**:
- 返回给 Orchestrator LLM
- 完整 artifact 变更已写入 `bots/national_v{next_v}/` 文件系统
- 成功 → 写入 checkpoint `stage="workers_done"`

> **💡 真实示例 (v9)**: v9 尝试 Worker 路径但失败。Worker 1（Logic Architect）收到 Master 的任务指令后，用 Edit 工具修改 `strategy.py`。但 boundary validation 发现 Architect 修改了 10+ 个数字常量（属于 Tuner 范围），触发边界违规：
> ```
> [BOUNDARY VIOLATION] Worker 1 (Algorithmic Logic Architect):
>   Modified 12 numeric literals in strategy.py — this crosses the Tuner boundary.
>   Affected files: strategy.py (declared target, but numeric edits forbidden for Architect)
> ```
> Worker 2（Tuner）则因 postflop.py 无任何改动被标记 zero-change。Reviewer 以 score=4 拒绝。系统随后改用 Crossover v6×v2 路径。

---

### 步骤 11：质量门禁 `run_quality_gates(version)`

- **触发者**: Orchestrator LLM
- **有无 LLM**: 无
- **做什么** (函数 `run_quality_gates` 内):
  1. `verify_code()` — 编译检查
  2. `run_smoke_test()` — 冒烟对战
  3. `run_decision_test_details()` — 决策测试（≥70% 通过率 + 关键场景全部通过）
  4. `check_code_size()` — 文件行数检查（`strategy.py`/`postflop.py` 基础上限 2000 行，helper 基础上限 1500 行，硬上限 2500 行，并受 source bot +15% growth budget 约束）
  5. `code_changed` — 与 frozen prepared artifact 的 regular-file manifest diff（binary-only Worker 合法；source Python diff 仅是 telemetry）
  6. prepared contract、完整 declared scope、publication shape、protected contract、national protocol / acceptance、fix verification、telemetry fidelity、reachability 等硬门
- **注意**: 正式世代必须有有效的 prepared artifact contract；缺失、漂移、仅空目录变化或 residual `.task_context` 都失败关闭。
- **输出**: `{compile_ok, smoke_ok, decision_pass_rate, decision_ok, critical_scenarios_passed, size_ok, code_changed, all_passed}` + 详细字段：`compile_errors, smoke_errors, critical_passed, critical_total, critical_failures, decision_failures, scenario_results, total_lines, oversized_files, checkpoint_recorded`
- **输出去向**: 全部通过 → 写入 checkpoint `stage="quality_passed"` + gate `quality`

---

### 步骤 12：代码审查 📎 `run_review(version, source_v, plan)`

| 项目 | 内容 |
|---|---|
| **触发者** | Orchestrator LLM 调用 MCP 工具 `run_review` |
| **调用链** | `tool_gates.py:run_review()` → `run_claude_query()` |
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

> **💡 真实示例 (v10)**: Reviewer 审查 Crossover v4×v8 产生的代码变更。首次通过：
> ```json
> {
>   "approved": true,
>   "quality_score": 8,
>   "change_summary": "Conservative crossover: v4 base (simplest, strongest at 54.92% WR) + selective v8 imports (PREFLOP_STRENGTH_TABLE, CBet tracking, should_fold_postflop, CBet-based call margin adjustment). Skipped from v8: drift detection, safe exploitation lambda, 3bet/4bet logic, lowered EQR, wider open threshold.",
>   "risk_areas": ["opponent.py lines 43-45 contain dead code (unreachable)", "v10 reuses v9's exact should_fold_postflop which scored poorly"]
> }
> ```
> 对比 v9 的 Reviewer 拒绝案例（score=4，Worker 1 修改了 12 个数字常量属于 Tuner 越界），v10 的 Crossover 路径绕过了 Worker boundary 问题。

---

### 步骤 13：策略评审 📎 `run_critic(version, source_v, plan, reviewer_feedback, force_advance)`

| 项目 | 内容 |
|---|---|
| **触发者** | Orchestrator LLM 调用 MCP 工具 `run_critic` |
| **调用链** | `tool_gates.py:run_critic()` → `agent_review.py:_run_critic(next_v, source_v, master_plan_str, ui, prev_critic_result=None)` → `run_claude_query()` |
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

**通过逻辑** (函数 `run_critic` 内): Critic 是 advisory。它记录风险和建议，
不能认证、否决或改写强度结论；本地 native TCP precommit 才是最终策略硬门。
任何需要执行的返工都必须先变成系统拥有、内容绑定的 repair contract，不能把
Critic 自由文本直接当成 Worker 写权限。

**⚠️ 重要**: `force_advance` 仅作为历史兼容/telemetry 字段保留，不改变
Critic 或 precommit 结果。只要 Critic 调用和 schema 有效，该 advisory 角色即完成；
调用/解析失败按 LLM infrastructure retry 处理，不制造策略否决。

**输出去向**:
- 返回给 Orchestrator LLM
- schema-valid advisory → 写入 checkpoint `stage="critic_checked"` + gate `critic`，同时保留 raw score/feedback
- `run_precommit_eval` → 用本地 native TCP 统计证据决定通过或进入系统合成的返工路线

> **💡 真实示例 (v10)**: Critic 独立评估 Crossover v4×v8 的策略价值，score=6（勉强通过）：
> ```json
> {
>   "score": 6,
>   "approved": true,
>   "strategic_assessment": "Conservative crossover from v4 base with selective v8 features. CBet exploitation mutation is the main differentiator vs v9.",
>   "evidence": {
>     "h2h_weaknesses": ["v9 scored 44.6% WR with similar features (CBet tracking, should_fold_postflop)"],
>     "diff_refs": ["v10 reuses v9's exact should_fold_postflop function", "Only differentiation is v4 base and weaker CBet exploitation bonus"]
>   },
>   "feedback": "v10's features are too similar to v9 which performed poorly. Local optima risk: repeating failed strategies with a different base.",
>   "local_optima_warning": true,
>   "local_optima_reason": "v9 scored 44.6% WR with CBet tracking + should_fold_postflop. v10 uses same features on v4 base — may produce similar results."
> }
> ```
> Critic 勉强通过但发出了 local_optima_warning。v9 的 Critic 也给了 score=6，反馈 Worker 2 完全失败（postflop.py 无改动）。

---

### 步骤 14：提交前验证 `run_precommit_eval(version, source_v, n_games)`

- **触发者**: Orchestrator LLM
- **有无 LLM**: 无
- **前置**: checkpoint 中 quality + review + critic gate 全部通过
- **做什么** (函数 `run_precommit_eval` 内):
  1. 选择对手：父版本 bot + 当前 Top 2 H2H 胜率 + H2H 弱点对手（最多 1 个）+ crossover parent_b（若适用）= 最多 4 个
  2. 与每个对手运行 `mirror_battle(n_games)` — 默认 8，参数 clamp 到 4-16；调度器/超时/fallback 逻辑决定是否走 daemon scheduler 或本地并行路径
  3. Per-opponent timeout 随 n_games 缩放: `max(300s, n_games × 120s)`
  4. 阻断条件：输给父版本、**总输≥3 且 总输≥赢+2**、对局超时、无对手可选（`no_opponents`）、对局异常（`match_exception`）
- **输出**: `{passed, blockers, matchups, total_wins/losses/draws}`
- **输出去向**: 通过 → checkpoint `stage="verified"` + gate `precommit_eval`

> **💡 真实示例 (v9)**: v9 的 precommit eval 选择对手 parent(v6) + crossover parent_b(v2)，运行 mirror battle：
> ```
> 对手选择: parent(v6) + crossover_parent_b(v2)
> vs v6: 10-10 (tied, 不触发"输给父版本"阻断)
> vs v2: 10-10 + 3-1 (won)
> aggregate: 23-11, blockers=[] → PASSED ✅
> ```
>
> **v10 的 precommit eval 超时（历史反例）**: v10 请求 n_games=80（远超当前 4-16 的 clamp 范围；当时流程也缺少足够防护），但 CYCLE_TIMEOUT=3600s 先到，导致整个 cycle 超时。v10 最终被手动提交（无 bot-v10 tag）。

---

### 步骤 15：提交 `commit_bot(version, source_v, strategy, review_approved=false)`

> ⚠️ `review_approved` 默认为 `false`，Orchestrator 必须**显式传递** `review_approved=true`（仅在 `run_review` 返回 `approved:true` 后）。

- **触发者**: Orchestrator LLM
- **有无 LLM**: 无
- **前置**: checkpoint 中所有 gates 必须存在且通过
- **做什么** (函数 `commit_bot` 内):
  1. 验证 gate ledger 完整性（quality + review + critic + precommit_eval）
  2. 验证 `review_approved=true`（quality gates 已在 checkpoint 中验证，**不重新运行**完整质量门）
  3. `git_commit_bot()` — `git add` + `git commit` + `git tag bot-v{N}`
  4. 验证 git tag 确实创建成功
  5. 写入 `.completed` 标记文件
  6. 归档调用：`archive_generation()` 生成快照、`archive_rotate_files()` 归档轮转、`archive_old_logs()` 日志压缩
  7. 清除 pipeline checkpoint（`clear_pipeline_checkpoint()`）
  8. `app_state.set_generation(v)` — 更新 Web UI 生成计数
  9. 发送 `.reap_signal` 通知守护进程刷新 bot 列表
  10. 写入 `priority_eval.json` — 标记新 bot 需要优先评估
- **输出**: `{committed: true, version, source_v, push_ok}`（若池 > 30 额外返回 `needs_reap: true, pool_size`）

> **💡 真实示例 (v9)**: v9 通过完整 pipeline 后成功提交，但缺少 git tag：
> ```json
> {"committed": true, "version": 9, "source_v": 6, "push_ok": false}
> ```
> 注：v9 提交后未创建 `bot-v9` tag（只有 commit `32e8f1f`），导致 `find_current_v()` 返回 8 而非 9。这是一个已知的 tag 一致性问题。
> Git commit message：
> ```
> feat: crossover bot v9 -- v6xv2 hybrid with tighter folding + 3bet logic + CBet exploitation
> ```
>
> **v10 的手动提交（历史反例，不是当前流程）**: v10 因 precommit eval 超时（n_games=80，3600s CYCLE_TIMEOUT 先到）未能通过 commit_bot 工具。手动 `git commit` + `git push` 后同样缺少 `bot-v10` tag。当前规范禁止把这种手动提交当作完成态；bot 版本只有在 `commit_bot` 通过 gate、提交源码、创建 annotated `bot-v{N}` tag 并写入 `.completed` 后才算完成。

---

### 步骤 16：归档审计 `run_archivist(version, source_v)`

| 项目 | 内容 |
|---|---|
| **触发者** | Orchestrator LLM 调用 MCP 工具 `run_archivist` |
| **调用链** | `tool_commit.py:run_archivist()` → 确定性归档 + 条件性 `agent_master.py:_run_archivist_analysis()` → `run_claude_query()` |
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

> **💡 真实示例 (v7)**: v7 是目前唯一有完整归档快照（`archive/v7.json`）的 bot。归档数据：
> ```json
> {
>   "version": 7, "source_v": 5,
>   "timestamp": "2026-06-05T12:43:58",
>   "git_tag": "bot-v7", "git_commit": "0e0f491",
>   "review_score": 7, "critic_score": 7.0,
>   "precommit_eval": {"passed": true},
>   "pool_size": 7
> }
> ```
> v9 和 v10 因缺少 git tag，未生成归档快照文件。

---

### 代际结束（Phase 2 → Phase 3）

Phase 2 `_run_one_cycle()` 检测到 `cycle_completed`:
- 清除 `orchestrator_session.json`
- 返回花费给 `orchestrator_loop()`

Phase 3 `post_generation_cleanup()`（仅在 cost ≥ 0 即成功或非 auth 错误时进入）:
- 若 `bot-v{N}` tag 不存在，直接记录 `pipeline.post_cleanup_skipped_uncommitted` 并跳过所有后清理副作用
- `reap_if_needed()` — 已提交版本后，活跃 bot > 30 时自动淘汰最弱
- `consolidate_experience()` — 已提交版本后，每 3 代 **或** `RECENT_LESSONS >= 4` 条目时整合经验池（代码层直接调用，非 MCP 工具）

`orchestrator_loop()` 检查 `shutdown_mgr.is_shutting_down`，若未关闭则 `sleep(5)` 后进入下一代。

---

## 三、重试与恢复流程

### 3.1 代内重试循环（Checkpoint/工具指令驱动）

由 Orchestrator LLM 按 checkpoint 和工具返回字段执行，规则来自 `prompts/orchestrator.md`。不要让 LLM 自己维护私有计数器；权威字段是 `generation_attempt`、`worker_failure_count`、`precommit_attempt`、`action`、`directive`、`circuit_breaker`、`require_new_plan`。

```
Master 失败:
  └── 按 run_master 返回的 retry/abandon 指令处理

Quality gates 失败:
  └── 从 quality_failed checkpoint 按 blocker 反馈重试 workers；
      只有工具明确要求重新规划/放弃时才回 Master 或 abandon_generation

Review 拒绝:
  └── 注入 reviewer feedback 重试 workers

Critic 低分:
  └── 作为硬门拒绝；注入 critic reviewer_feedback 重试 workers，
      不允许 unchanged code 进入 run_precommit_eval

Precommit 失败:
  └── 按 blockers 和 tool directive 重试 workers、回 Master、或 abandon_generation
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

### 3.3 Worker 并行/串行执行

Workers 默认按文件边界并行执行，最大并发由 `MAX_PARALLEL_WORKERS = 3` 控制；当 target_files 重叠、目标不清晰或 API 压力较高时降级串行。这避免了多 Worker 同时修改同一文件导致的竞态条件。

```
_execute_workers():
  group tasks by non-overlapping target_files
  run up to MAX_PARALLEL_WORKERS concurrently
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
| `quality_failed` | Quality failed → retry `execute_workers` with exact gate feedback, or abandon |
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
  "daemon_workers": 12,
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
| **触发者** | 已提交/tagged 版本的 Phase 3 `post_generation_cleanup()` 代码层直接调用（每 3 代 **或** `RECENT_LESSONS >= 4` 条目时） |
| **调用链** | `generation_scheduler.py:post_generation_cleanup()` → `experience_archivist.py:_consolidate_experience_pool()` → `run_claude_query()` |
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
| **触发者** | Orchestrator LLM 调用 `run_crossover`（停滞严重时生成双亲重组基线，随后仍进入正常规划流水线） |
| **调用链** | `tool_commit.py:run_crossover()` → `agent_review.py:_run_crossover()` → `run_claude_query()` |
| **LLM 角色** | CROSSOVER v{A}×v{B}→v{target} |
| **模型** | Sonnet |
| **工具** | Bash, Read, Edit |
| **Prompt 模板** | `prompts/crossover_prompt.md` |
| **重试** | 最多 3 次 (`MAX_CROSSOVER_RETRIES`) |

**输入构建**:
1. 校验 scheduler checkpoint 中的 `(parent_a, parent_b, target_v)` 与调用参数完全一致，再从 `parent_a` 复制目录作为起点
2. 读取 `prompts/crossover_prompt.md` 模板
3. 替换占位符：`{parent_a_version}`、`{parent_b_version}`、`{version}`

**LLM 能做的事**: 读取两个父 bot 的代码，Edit 合并到目标 bot

**每次尝试后的自动检查**: 编译检查 + import/smoke + 国赛位置契约 +
代码尺寸 + 完整 artifact Parent-B 来源门 + preplan 架构 transition。Python
胶水必须绑定 Parent-B 符号，非 Python 表格/模型/配置必须是 Parent-B exact
bytes；纯阈值/启发式改动若不能追溯到 Parent B 会被拒绝；preplan 只阻断父本能力回退和系统 wrapper
能力缺失；`plan_required_floor_checks` 明确留给 Master/Workers。

**输出去向**: 代码写入 `bots/national_v{target}/`，成功后 checkpoint
进入 `prepared`。下一步固定为 `run_direction_audit` → 停滞时
`run_literature_probe` → `run_master` → `execute_workers`。Crossover 只允许
移植可追溯到 Parent B 的组件，不做独立 mutation；系统会冻结子代内容哈希、
逐文件来源、能力快照、行数和 H2H snapshot 身份。Master 必须以该子代为
规划基线，不能按 Parent A 覆盖重组结果，也不能直接进入质量门或提交。基础设施
恢复绑定双亲和子代完整 artifact hash；暂停期间漂移会废弃本世代。

> **💡 历史 Botzone 示例（仅背景，不是当前契约） (v9)**: Combined analysis 检测到 3 代下降（v6 52.7% → v7 47.7% → v8 47%），推荐 branch from v6。策略决策选择 crossover v6×v2。Crossover Agent 分析两个父 bot 后制定了合并策略：
> - **从 v2 导入**: `PREFLOP_STRENGTH_TABLE`（169 手牌分级表）、CBet tracking、3bet/4bet logic、lower postflop_call_margin、draw-aware EQR
> - **保留 v6**: min_raise_action、must_continue_vs_raise、should_fold_postflop（防御性折叠）
> - 当时还混入了 CBet threshold mutation；当前 pure crossover 来源门会拒绝它，必须留到后续 Master/Worker 阶段并接受完整质量评估。
>
> Crossover Agent 用 Read 读取两个父 bot 的全部源码，用 Edit 将合并后的代码写入 `bots/claude_v9/`。执行后自动编译检查+冒烟测试通过。

---

### 4.3 其他辅助工具

以下工具在数据流全景图中未展开，但同样可用：

- **`run_inline_eval(version, n_games)`** — HTTP/control `all_tools` 可用的手动镜像评估工具，不在 Orchestrator MCP 工具列表中
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
                    │   Pipeline: prepare/crossover →             │
                    │   direction_audit → optional literature →   │
                    │   master → workers → quality → review →     │
                    │   critic → precommit_eval → commit →        │
                    │   archivist                                 │
                    │   中断 → session + checkpoint 保留到磁盘     │
                    │   下次启动 → _startup_recovery() 恢复        │
                    └──────────────────┬──────────────────────────┘
                                       │
                    ┌──────────────────▼──────────────────────────┐
                    │   Phase 3: post_generation_cleanup() — 代码层│
                    │   (幂等，可安全中断并重跑)                    │
                    │                                            │
                    │   未打 bot-vN tag → 跳过全部副作用          │
                    │   reap_if_needed() → 淘汰最弱 bot           │
                    │   consolidate_experience() → 每3代或       │
                    │   RECENT_LESSONS≥4                          │
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
    │   工具: 17 MCP tools (见 tools.py mcp_tools) │
    └──────────────────┬──────────────────────────┘
                       │
              ┌────────▼────────────┐
              │prepare_next_gen      │
              │(无 LLM)              │
              │复制 bots/claude_v{N}/│
              │写入 checkpoint       │
              └────────┬────────────┘
                       │
              ┌────────▼────────────┐
              │run_direction_audit  │
              │📎 DIRECTION AUDITOR │
              │工具: 无（纯JSON输出）│
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
              │+size +code_changed    │
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
              │硬门；低分回 worker │
              └────────┬────────────┘
                       │
              ┌────────▼────────────┐
              │run_precommit_eval   │
              │(无 LLM)             │
              │镜像对战: vs父+Top    │
              │n_games默认8，范围4-16│
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
    │    │         replay (≤2000), daemon_stats         │
    │    └── 响应 .reap_signal 刷新 bot 列表            │
    └────────────────────────────────────────────────────┘
```

---

## 六、全局约束

- **所有 LLM 调用统一使用 Sonnet 模型**，通过 `claude_agent_sdk` 的 `query()` 函数
- **API 限流 (529)**: `run_claude_query()` 内自动指数退避重试（30s → 60s → 120s）
- **Prompt 预算**: `MAX_PROMPT_CHARS = 700_000`，超限时按文件均分压缩上下文
- **MCP 工具**: 17 个工具注册在 `tools.py` 的 `mcp_tools` 列表中，通过 `create_sdk_mcp_server(name='evolution', tools=mcp_tools)` 暴露给 Orchestrator LLM。完整列表：`prepare_next_gen`、`run_direction_audit`、`run_literature_probe`、`run_master`、`execute_workers`、`run_quality_gates`、`run_review`、`run_critic`、`run_precommit_eval`、`run_crossover`、`commit_bot`、`run_archivist`、`abandon_generation`、`get_bot_info`、`get_match_history`、`get_h2h`、`get_bot_stats`。工具来自 `tool_planning.py`（direction_audit, literature_probe, master, workers）、`tool_gates.py`（quality_gates, prepare_next_gen, review, critic）、`tool_eval.py`（precommit_eval）、`tool_commit.py`（commit, archivist, crossover）、`tool_bot_management.py`（abandon_generation）、`tool_status.py`（查询工具）。**注意**: `get_status`、`run_inline_eval`、`consolidate_experience`、`reap_weakest` 等工具仅在 `all_tools`（HTTP 端点 `/api/control/tool/`）中可用，不在 MCP 中。`consolidate_experience` 由 Phase 3 代码层直接调用，不经 MCP。
- **子代理 MCP 屏蔽**: `_BLOCKED_MCP_TOOLS` 屏蔽以下外部工具（防止子代理访问网络）：
  - `mcp__web-reader__webReader`
  - `mcp__web-search-prime__web_search_prime`
  - `mcp__zread__get_repo_structure`
  - `mcp__zread__read_file`
  - `mcp__zread__search_doc`
- **角色边界**: Worker 受 prompt + reviewer 双重约束 — Logic Architect 不改常数，Tuner 不加函数
- **Gate Ledger**: Pipeline checkpoint 强制阶段顺序 — 每个阶段写入 gate 记录，后续阶段验证前置 gates 完整
- **阶段常量**: `STAGE_ORDER = [prepared, direction_audited, master_planned, workers_done, quality_failed, quality_passed, reviewed, critic_checked, verified, archived]`
- **ShutdownManager**: `loop.add_signal_handler()` 注册 SIGINT/SIGTERM，设置 `is_shutting_down` 标志，Phase 1/3 检查后优雅退出，Phase 2 等待当前 LLM 调用完成
- **Pipeline checkpoint 原子写入**: `pipeline_state.json` 使用 tmp + `os.replace()` 原子替换，避免中断导致文件损坏
- **Session 持久化策略**: `orchestrator_session.json` 在自然完成、超时、529 限流、认证错误、Orchestrator crash 时清除；`CancelledError` / 用户中断信号时保留 session 到磁盘，下次启动 `_startup_recovery()` 恢复会话
- **Daemon 独立生命周期**: `elo_daemon.py` 作为独立子进程运行，orchestrator 停止不影响 daemon 持续评估；daemon 仅通过 `.reap_signal` 文件与 orchestrator 通信
- **归档阶段**: Phase 3 只在 `bot-v{N}` tag 存在时执行后清理副作用；未提交/已放弃版本记录跳过。已提交版本中 `reap_if_needed()` + `consolidate_experience()` 在 commit 后执行，幂等可安全重跑

## 附录1：标准 Master 路径示例（v5 → v7）

以下展示一个**标准路径**的完整进化循环：Phase 1 代码层分析 → Phase 2 LLM pipeline 一次通过 → Phase 3 清理。基于实际日志和归档数据。

### 背景

- **源 bot**: claude_v5 (master 策略，非 crossover)
- **目标 bot**: claude_v7
- **结果**: 成功提交（`git tag bot-v7`, commit `f22ecbc`）
- **总耗时**: 约 26 分钟（2026-06-05 12:17 → 12:43）
- **路径**: Phase 1 分析 v5 → Master 规划 → Workers 修改 → Quality/Review/Critic 一次通过 → Commit

### Phase 1（代码层自动执行）

Phase 1 由 `prepare_generation()` 编排，无需 Orchestrator LLM 参与：

1. **状态查询**: `find_current_v()` → v6（最新 git tag），`get_active_bots()` → 7 bots
2. **等待评估**: v6 已有足够对局（≥100 局），eval_ok=True
3. **合并分析** (`_run_combined_analysis`): v6 表现正常，无停滞迹象。推荐继续从 v5 进化（v5 代码更简单，适合 Master 路径）
4. **对战分析** (`_analyze_recent_matches`): 发现 v5 近乎零的 3-bet 频率——BB 面对 raise 时缺乏 3-bet 处理逻辑
5. **策略决策**: `_decide_strategy()` → strategy="master", source_v=5（LLM 推荐进化源）

### Phase 2 工具调用序列

| # | 工具调用 | 结果 |
|---|---|---|
| 1 | `prepare_next_gen(v5, v7)` | 复制 v5 → v7，checkpoint stage="prepared" |
| 2 | `run_direction_audit` | 未检测到重复方向（方向多样：之前是 postflop fold、EQR 调整） |
| 3 | `run_master(v5, v7)` | 2-worker 计划：Worker1 处理 BB vs Raise preflop 3-bet，Worker2 调优 opponent modeling 参数 |
| 4 | `execute_workers` | Worker1 修改 strategy.py，Worker2 修改 constants.py。编译+冒烟测试均通过 |
| 5 | `run_quality_gates` | ALL PASSED ✅（含 code_changed 检查——确认文件确实被修改） |
| 6 | `run_review` | APPROVED ✅（score 7） |
| 7 | `run_critic` | APPROVED ✅（score 7.0） |
| 8 | `commit_bot` | SUCCESS ✅（`git tag bot-v7`, commit `f22ecbc`） |
| 9 | `run_archivist` | 归档快照 `archive/v7.json` 写入成功 |

### 各阶段真实输出

**Master 规划**
```json
{
  "analysis": "v5 match analysis reveals near-zero 3-bet frequency. BB facing raise lacks 3-bet handler, falls through to tight logic. Opponent modeling improvements needed for adaptive play.",
  "tasks": [
    {"worker_id": 1, "role": "Algorithmic Logic Architect", "target_files": ["strategy.py"]},
    {"worker_id": 2, "role": "Hyperparameter Tuner", "target_files": ["constants.py"]}
  ]
}
```

**Critic 评估**
```json
{
  "score": 7.0,
  "approved": true,
  "strategic_assessment": "BB vs Raise preflop 3-bet handler addresses documented weakness. Opponent modeling improvements add adaptation capability."
}
```

**归档快照** (`archive/v7.json`)
```json
{
  "version": 7, "source_v": 5,
  "timestamp": "2026-06-05T12:43:58",
  "git_tag": "bot-v7", "git_commit": "0e0f491",
  "review_score": 7, "critic_score": 7.0,
  "precommit_eval": {"passed": true},
  "pool_size": 7
}
```

### 相关日志文件

- `web/core/results/v7/logs/master_io.txt` — Master 规划
- `web/core/results/v7/logs/worker_1_io.txt` — Worker 1（Logic Architect）
- `web/core/results/v7/logs/worker_2_io.txt` — Worker 2（Tuner）
- `web/core/results/v7/logs/reviewer_io.txt` — Reviewer 评估
- `web/core/results/v7/logs/critic_io.txt` — Critic 评估
- `web/core/results/v7/logs/match_analyst_io.txt` — 对战分析
- `web/core/results/v7/logs/stagnation_analysis.txt` — 停滞分析
- `web/core/results/v7/logs/performance_verification_io.txt` — 性能验证

> 此示例展示了**标准 Master 路径**的完整流程：Phase 1 代码层自动完成分析→ Phase 2 通过 10 次 MCP 工具调用一次通过 → Phase 3 清理。总耗时仅 26 分钟，是理想情况下的进化速度。

---

## 附录2：Crossover 路径示例（v6 → v9）

以下展示一个 **Worker 失败 + Crossover 替代** 的进化循环。Combined analysis 检测到三代下降后，系统自动切换到 crossover 路径。基于实际日志。

### 背景

- **源 bot**: claude_v6 (v6 52.7% h2h_avg_wr，从 v6 峰值连续 3 代下降)
- **目标 bot**: claude_v9
- **结果**: 成功提交（commit `32e8f1f`，**但缺少 `bot-v9` tag**）
- **总耗时**: 约 30+ 小时（2026-06-06 至 06-07，含多次中断和重试）
- **路径**: v6 峰值 → v7/v8 连续下降 → Combined analysis 推荐 crossover → v6×v2 合并

### Phase 1（代码层自动执行）

1. **合并分析** (`_run_combined_analysis`): 明确的三代下降——v6(52.71%) → v7(46.63%) → v8(47.00%)。`is_stagnant=true, confidence=high, trend=declining`。`recommended_source=claude_v6`。
2. **对战分析** (`_analyze_recent_matches`): 核心发现——0% postflop fold rate on every street，从不 folding postflop。48-55% preflop fold 但仅 9-19% raise。Avg raise 只有 0.4x pot。
3. **策略决策**: `_decide_strategy()` → strategy="crossover", source_v=6, crossover_parents=(v6, v2)。v2 代码包含 v6 缺乏的结构化功能（PREFLOP_STRENGTH_TABLE, CBet tracking, 3bet/4bet logic）。

### Phase 2 工具调用序列（约 100 次，含多次重试）

**早期 Master 路径尝试（失败）**

| # | 工具调用 | 结果 |
|---|---|---|
| 1-5 | `prepare_next_gen` → `direction_audit` → `run_master` → `execute_workers` | Worker 1 boundary violation（Tuner 改了 strategy.py 而非 constants.py），Reviewer 拒绝（score=4） |
| 6-10 | 重试 `execute_workers` ×3 | 持续 role boundary violations。Circuit breaker 熔断（6/6 worker failures） |
| 11-15 | `run_master` ×3 重试 | Master validation 失败：Tuner 被分配到 strategy.py（硬错误）。API Error 400（model not found） |
| 16-20 | 多次 3600s cycle 超时 | Orchestrator 反复尝试不同策略均超时 |

**Crossover 路径（成功）**

| # | 工具调用 | 结果 |
|---|---|---|
| N | `run_crossover(v6, v2, v9)` | **成功** — v6 base + v2 features 合并 |
| N+1 | `run_quality_gates` | ALL PASSED ✅ |
| N+2 | `run_review` | APPROVED ✅（score 8） |
| N+3 | `run_critic` | APPROVED ✅（score 7） |
| N+4 | `run_precommit_eval` | PASSED ✅（v9 vs v6: 10-10, v9 vs v2: 10-10+3-1） |
| N+5 | `commit_bot` | SUCCESS（commit `32e8f1f`，但未创建 bot-v9 tag） |

### Crossover 合并策略

Crossover Agent 分析 v6 和 v2 后的合并决策：

- **从 v2 导入**: `PREFLOP_STRENGTH_TABLE`（169 手牌分级表）、CBet tracking（对手 cbet 响应统计）、3bet/4bet logic、draw-aware EQR、lower postflop_call_margin
- **保留 v6**: `min_raise_action`、`must_continue_vs_raise`、`should_fold_postflop`（防御性折叠框架）
- **Mutation**: CBet exploitation — 当对手 `fold_to_cbet > 55%` 时，降低 flop 下注门槛 0.03

### 关键错误和恢复

1. **Worker boundary violations 反复出现**: Hyperparameter Tuner 持续被 Master 分配到 strategy.py（应为 constants.py），导致 3 次 Master validation 失败
2. **Circuit breaker 熔断**: 累计 6/6 worker failures，阻止进一步 Worker 尝试
3. **429 rate limit**: 在 16:05 触发 5 小时用量上限
4. **3600s cycle 超时**: 多次因 Orchestrator 在 cycle 内耗尽时间
5. **Tag 缺失**: v9 提交后未创建 `bot-v9` tag，导致 `find_current_v()` 返回 8 而非 9

### 相关日志文件

- `web/core/results/v9/logs/combined_analysis.txt` — 合并分析（推荐 branch from v4 或 v6）
- `web/core/results/v9/logs/crossover_io.txt` — Crossover 代码合并
- `web/core/results/v9/logs/master_io.txt` — Master 规划（多次失败尝试）
- `web/core/results/v9/logs/worker_1_io.txt` — Worker 1 尝试记录
- `web/core/results/v9/logs/worker_2_io.txt` — Worker 2 尝试记录
- `web/core/results/v9/logs/reviewer_io.txt` — 2 次审查（首次 score=4 拒绝）
- `web/core/results/v9/logs/critic_io.txt` — Critic 评估
- `web/core/results/v9/logs/match_analyst_io.txt` — 对战分析

> 此示例展示了进化系统的**自适应恢复机制**：(1) Worker boundary violations 触发 circuit breaker 熔断后，系统通过 crossover 路径绕过 Worker 问题；(2) Combined analysis 的 `recommended_source` 指导策略决策选择最优进化源；(3) 但也暴露了 Master validation 的弱点——Tuner 持续被分配到错误文件。共约 100 次工具调用、30+ 小时（含中断恢复）。

---

## 附录3：保守 Crossover + 低分通过示例（v4 → v10）

以下展示一个 **Critic 勉强通过 + Precommit 超时 + 手动提交** 的非典型路径。

### 背景

- **源 bot**: claude_v4 (v4 拥有最高 h2h_avg_wr: 54.92%)
- **目标 bot**: claude_v10
- **结果**: 手动提交（commit `4f4203e`，**无 `bot-v10` tag**）
- **总耗时**: 约 1.5 小时（2026-06-07 19:47 → 20:29）
- **路径**: v9 失败后 → Combined analysis 推荐 branch from v4 → Crossover v4×v8 保守合并

### Phase 1（代码层自动执行）

1. **合并分析** (`_run_combined_analysis`): 三代下降从 v6 峰值。v4 是最强祖先（54.92% h2h_avg_wr，最简单代码库）。`recommended_source=claude_v4, recommendation=crossover`。
2. **对战分析**: 与 v9 相同的 calling station 模式——0% postflop fold，catastrophic -15K to -20K 单场亏损。
3. **策略决策**: `_decide_strategy()` → strategy="crossover", source_v=4, crossover_parents=(v4, v8)。v8 提供结构化功能（CBet tracking, 3bet/4bet），v4 提供稳定的 base。

### Phase 2 工具调用序列

| # | 工具调用 | 结果 |
|---|---|---|
| 1 | `run_crossover(v4, v8, v10)` | **成功** — 保守合并：v4 base + selective v8 imports |
| 2 | Diff 验证 | 确认代码变更合理 |
| 3 | `run_quality_gates` | ALL PASSED ✅ |
| 4 | `run_review` | APPROVED ✅（score 8，审查 crossover 质量） |
| 5 | `run_critic` | APPROVED ✅（score 6，**勉强通过**，local_optima_warning=true） |
| 6 | `run_precommit_eval` | **TIMEOUT** ❌（历史反例：n_games=80，3600s CYCLE_TIMEOUT 先到；当前参数会 clamp 到 4-16） |

### Crossover 合并策略

Crossover Agent 的保守合并决策：

- **从 v8 导入（选择性）**: `PREFLOP_STRENGTH_TABLE`、CBet tracking、`should_fold_postflop`、CBet-based call margin adjustment
- **跳过 v8 的**: drift detection、safe exploitation lambda、3bet/4bet logic、lowered EQR、wider open threshold
- **Mutation**: CBet exploitation — 当对手 `fold_to_cbet > 55%` 时，降低 flop raise threshold 0.03

### Critic 勉强通过（score=6）

```json
{
  "score": 6,
  "approved": true,
  "strategic_assessment": "Conservative crossover from v4 base with selective v8 features. CBet exploitation mutation is the main differentiator vs v9.",
  "feedback": "v10's features are too similar to v9 which performed poorly (44.6% WR). Local optima risk: repeating failed strategies with a different base.",
  "local_optima_warning": true,
  "local_optima_reason": "v9 scored 44.6% WR with CBet tracking + should_fold_postflop. v10 uses same features on v4 base — may produce similar results."
}
```

### 结果

| 项目 | 内容 |
|---|---|
| 提交 | 手动 commit `4f4203e`（**无 `bot-v10` tag**） |
| 策略 | Crossover v4×v8（保守合并 + CBet exploitation mutation） |
| Review score | 8（一次通过） |
| Critic score | 6（勉强通过，local_optima_warning=true） |
| Precommit | 超时（n_games=80 超出当时流程可承受范围；当前实现会 clamp 到 4..16，且由 precommit gate/超时保护处理） |
| Tag 状态 | 缺失——`find_current_v()` 返回 8 |

### 相关日志文件

- `web/core/results/v10/logs/crossover_io.txt` — 2 次 crossover 尝试
- `web/core/results/v10/logs/direction_audit_io.txt` — 方向审计
- `web/core/results/v10/logs/reviewer_io.txt` — 3 次审查（含早期 v6 base 拒绝）
- `web/core/results/v10/logs/critic_io.txt` — Critic 评估（score=6）

> 此示例展示了进化系统的**边界情况**：(1) **Critic 勉强通过** — score=6 触发 local_optima_warning 但仍允许推进；(2) **Precommit 超时** — Orchestrator LLM 请求 n_games=80（远超硬编码上限 5），导致 mirror battle 无法在 CYCLE_TIMEOUT 内完成；(3) **手动提交** — Pipeline 未能自动完成，需要人工介入；(4) **Tag 缺失** — 手动提交未走 commit_bot 流程，缺少 git tag 和归档快照。共 8 次工具调用，约 1.5 小时。
