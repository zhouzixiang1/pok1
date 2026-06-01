# LLM 多阶段运行时数据流

本文档以 **时间线** 视角，描述 `python web/main.py` 启动后系统内逐一发生的事件，聚焦每个 LLM 调用的数据流：谁发起、输入什么、输出什么、输出去向。

---

## 一、启动序列（无 LLM）

```
python web/main.py
        │
        ▼
  解析 CLI 参数 (--port [PORT env], --host, --no-daemon, --dev, --no-build)
  构建前端 (npm run build → web/server/static/)
  app_state.update_config(daemon_enabled, daemon_workers, daemon_pairs)  ← CLI 参数写入配置
        │
        ▼
  app.py 模块级: EventBroadcaster(buffer_size=500) + WebUI(broadcaster)  ← SSE 广播器在 uvicorn 之前创建
        │
        ▼
  uvicorn.run("server.app:app", host, port)
        │
        ▼
  FastAPI lifespan 启动:
    ├── app_state.bootstrap(find_current_v())    ← 从 git tags 读取最新 bot 版本
    ├── asyncio.create_task(orchestrator_loop()) ← 编排器作为后台协程启动
    └── orchestrator_loop() 内部:
          ├── inject_ui(web_ui)                  ← MCP 工具共享同一个 UI 实例
          ├── start_daemon(workers=配置值, pairs=配置值)  ← 默认 14/5，可通过 API 动态调整
          ├── daemon_monitor_thread 启动          ← 监控守护进程存活，自动重启
          └── while True:
                ├── gen_count += 1
                ├── _run_one_cycle()             ← 一个 Orchestrator LLM 会话
                ├── 连续 5 次零花费 → 指数退避 + 清除 session
                └── asyncio.sleep(5)             ← 代际间隔
```

### `_run_one_cycle()` 内部

1. `_build_context()` 构建上下文字符串：
   - 当前 bot 版本、rating、H2H 平均胜率、可靠度（games ≥ 100）
   - 未完成 bot 目录检测（上一轮中断）
   - 最近 5 个 git tags
   - 最近 3 条 worker 失败记录
   - Pipeline checkpoint 阶段提示
   - 模式标记（连续进化 / 单代 / dry-run）
   - 环境异常检测（如检测到不完整 bot、tags 缺失等异常，建议调用 `diagnose_environment`）
2. 将上下文注入 `orchestrator.md` 模板的 `{context}` 占位符
3. 检查 `orchestrator_session.json`：若存在（上次中断），用 `resume=session_id` 恢复会话
4. 以 `model="sonnet"` 启动 `claude_query()` 流式对话
5. Orchestrator LLM 开始自主调用 MCP 工具

**此后的一切 LLM 调用，都由 Orchestrator LLM 通过选择调用 MCP 工具来触发。**

---

## 二、一代进化的时间线

下面按 FSM 阶段顺序记录每个步骤。LLM 调用以 **📎** 标记，标注完整数据流。

---

### 步骤 1：状态查询 `get_status()`

- **触发者**: Orchestrator LLM
- **有无 LLM**: 无
- **做什么**: 读取 `glicko_ratings.json`、`bot_stats.json`、`elo_daemon_stats.json`、`head_to_head.json`（via `load_h2h_avg_winrates()`）、git tags，组装系统状态快照
- **输出返回给**: Orchestrator LLM（决定下一步）
- **输出内容**: `current_v`, `next_v`, `active_bots_count`, `top_ratings`, `daemon_total_games`, `incomplete_next_v`, `rating_reliable`, `current_bot_rd`, `current_bot_games`, `current_bot_win_rate`, `current_bot_h2h_avg_wr`, `recent_worker_failures`

> Orchestrator 据此判断：是否需要 `seed_initial_bots`、是否有未完成的 bot、rating 是否可靠、是否可以进入进化流程。

---

### 步骤 2：家政维护（无 LLM）

Orchestrator 按需调用：
- `reap_weakest()` — 若活跃 bot > 30，按 H2H 平均胜率淘汰最弱，移入 `bots/graveyard/`，清理相关数据
- `cleanup_incomplete()` — 删除无 `.completed` 且无 git tag 的残留目录
- `trim_experience()` — 裁剪 `experience_pool.md` 保留最近条目

---

### 步骤 3：等待评估 `wait_for_eval(version=source_v)`

- **触发者**: Orchestrator LLM
- **有无 LLM**: 无
- **做什么**: 异步轮询 `bot_stats.json`，等待守护进程为当前 bot 积累足够对局（默认 ≥ 100 局，超时 600s）
- **输出**: `version`, `eval_completed`, `current_rating`, `bot_stats`

> Orchestrator 据此判断 rating 是否可靠。`eval_completed: false` → 跳过停滞分析，直接进入 Master。

---

### 步骤 4：停滞分析 📎 `analyze_stagnation(source_v, active_bots)`

| 项目 | 内容 |
|---|---|
| **触发者** | Orchestrator LLM 调用 MCP 工具 `analyze_stagnation` |
| **调用链** | `tool_status.py:analyze_stagnation()` → `agent_master.py:_analyze_stagnation()` → `run_claude_query()` |
| **LLM 角色** | STAGNATION ANALYST |
| **模型** | Sonnet |
| **工具** | 无（纯 JSON 输出） |

**输入构建** (函数 `_analyze_stagnation` 内):
1. 读取 `rating_history.jsonl` 最近 10 个周期，提取每个周期的 top H2H 胜率或 top rating
2. 计算 Top 5 活跃 bot 的 H2H 平均胜率 + rating + rd
3. 拼装 prompt："You are a rating trend analyst..." + 趋势数据 + Top 5 bot 列表
4. 要求 JSON 输出

**输入数据来源**:
- `web/core/results/rating_history.jsonl` — Rating 历史快照
- `web/core/results/head_to_head.json` → `load_h2h_avg_winrates()` — H2H 胜率
- `web/core/results/glicko_ratings.json` — 当前 ratings

**LLM 输出**: JSON
```json
{
  "is_stagnant": true/false,
  "confidence": "high/medium/low",
  "recommendation": "continue|branch|crossover",
  "branch_from": "claude_vN" 或 null,
  "reason": "简短解释"
}
```

**输出去向**: 返回给 Orchestrator LLM → Orchestrator 据此决定 `stagnation_info` 字符串（传给 Master），或选择 crossover 替代正常流水线。

> **💡 真实示例 (v36)**: 当 v7 停滞 7+ 周期后，Performance Analyst 输出 `trend: "stagnant"`, `diversity_needed: true`。Orchestrator 据此跳过 Master 直接选择 Crossover 路径。
> ```json
> {
>   "trend": "stagnant",
>   "verified_improvements": ["System-level top H2H win rate held flat at ~53.49% for 8 consecutive periods"],
>   "persistent_weaknesses": [
>     "Top-performing bots (v7, v4, v8) are all from older generations",
>     "No bot has broken past ~53.5% average H2H win rate in 10+ evaluation periods"
>   ],
>   "diversity_needed": true,
>   "diversity_reason": "Incremental tuning has hit a local optimum. A fundamentally different strategic approach is required."
> }
> ```

---

### 步骤 5：对战分析 📎 `run_match_analysis(source_v)`

| 项目 | 内容 |
|---|---|
| **触发者** | Orchestrator LLM 调用 MCP 工具 `run_match_analysis` |
| **调用链** | `tool_status.py:run_match_analysis()` → `agent_master.py:_analyze_recent_matches()` → `run_claude_query()` |
| **LLM 角色** | MATCH ANALYST |
| **模型** | Sonnet |
| **工具** | 无（纯 JSON 输出） |

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

**输出去向**: 返回给 Orchestrator LLM → 作为 `match_analysis` 参数传给 `run_master()`。

> **💡 真实示例 (v36)**: v7 vs v21 的赛后分析，精准定位了三大缺陷：
> ```json
> {
>   "weaknesses": [
>     "Massive preflop overfolding (65%) in heads-up — bleeding chips from blinds",
>     "Postflop calling station: 0% fold on every postflop street, causing -19k+ losses",
>     "River value extraction failure: 87% call / only 12% raise with 0.5x pot sizing"
>   ],
>   "street_weaknesses": {
>     "preflop": "65% fold rate is ruinous in heads-up. Only 14% raise — the bot is limping or folding almost every hand",
>     "river": "87% call / 12% raise / 0% fold is the worst profile possible"
>   },
>   "patterns": "Classic 'fit-or-fold preflop, calling station postflop' pattern. Asymmetric payoff: small wins, huge losses.",
>   "recommendation": "Priority fix — preflop range expansion: reduce fold rate from 65% to ≤30%"
> }
> ```

---

### 步骤 6：性能验证 📎 `run_performance_verification(source_v)`

| 项目 | 内容 |
|---|---|
| **触发者** | Orchestrator LLM 调用 MCP 工具 `run_performance_verification` |
| **调用链** | `tool_status.py:run_performance_verification()` → `agent_review.py:_run_performance_verification()` → `run_claude_query()` |
| **LLM 角色** | PERFORMANCE ANALYST |
| **模型** | Sonnet |
| **工具** | 无（纯 JSON 输出） |

**输入构建** (函数 `_run_performance_verification` 内):
1. 读取 `rating_history.jsonl` 最近 10 个周期的 top H2H 胜率或 top rating
2. 读取 `match_history.jsonl` 最近 100 条计算当前 bot 近期胜率
3. 读取 `head_to_head.json` 提取每对手胜负，标注 STRENGTH/WEAKNESS
4. 读取 `bot_stats.json` 获取总体胜率和场次
5. 计算 Top 5 活跃 bot 列表
6. 拼装 prompt："You are a Performance Verification Analyst..." + 全部数据

**输入数据来源**:
- `rating_history.jsonl`, `match_history.jsonl`, `head_to_head.json`, `bot_stats.json`

**LLM 输出**: JSON
```json
{
  "trend": "improving|stagnant|declining",
  "verified_improvements": ["..."],
  "persistent_weaknesses": ["..."],
  "diversity_needed": true/false,
  "diversity_reason": "...",
  "suggestion": "..."
}
```

**输出去向**: 返回给 Orchestrator LLM → 作为 `performance_verification` 参数传给 `run_master()`。若 `diversity_needed: true`，Orchestrator 会在 `stagnation_info` 中注明。

> **💡 真实示例 (v36)**: Performance Analyst 综合了 10 个评估周期的 rating history、近 100 条 match history、H2H 对阵矩阵和 bot_stats，输出完整的趋势分析。v36 的 `trend: "stagnant"` + `diversity_needed: true` 直接触发了 Crossover 路径（跳过 Master）。完整输出见附录3。

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
- 每个 task 的 worker_prompt ≤ 3000 字符
- Hyperparameter Tuner prompt 会被 `_TUNER_STRUCTURAL_PATTERNS` 检查，含结构化指令（如 "add parameter"、"new function"）时发出边界警告

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
  7. 写入 pipeline checkpoint：`stage="prepared"`、`worker_invocation_count=0`
- **输出**: `{prepared: true, next_v, source_v}`

---

### 步骤 9：Worker 并行编码 📎 `execute_workers(tasks, next_v, source_v, reviewer_feedback)`

| 项目 | 内容 |
|---|---|
| **触发者** | Orchestrator LLM 调用 MCP 工具 `execute_workers` |
| **调用链** | `tool_pipeline.py:execute_workers()` → `agent_workers.py:_execute_workers()` → `_run_single_worker()` × N → `run_claude_query()` |
| **LLM 角色** | WORKER {id} ({role}) |
| **模型** | Sonnet |
| **工具** | Bash, Read, Edit |
| **Prompt 模板** | `prompts/worker_prompt.md` |
| **并发** | 最多 3 个并行（`_get_worker_semaphore()` 创建 `Semaphore(MAX_PARALLEL_WORKERS)`） |
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

**并行→串行回退**: 若并行中任一 worker 失败，清除目标目录，从源 bot 重新复制，串行重试全部 tasks。

**⚠️ 重要机制补充**:

1. **Architect + Tuner 串行执行**: 当 tasks 中同时包含 role 含 "Architect" 和 role 含 "Tuner" 时，系统自动串行执行（Tuner 需要 Architect 的输出作为基础）。见 `agent_workers.py` 中 `has_architect and has_tuner` 检测逻辑。

2. **Worker Circuit Breaker**: 每代最多允许 6 次 worker 调用（`MAX_WORKER_INVOCATIONS = 6`，`execute_workers` 内局部变量），防止无限重试。见 `tool_pipeline.py` 中 `invocation_count` 检查。

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
  4. `check_code_size()` — 文件行数检查（每文件 ≤ 1000 行）
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
  1. 重新编译检查 + 冒烟测试
  2. 选择对手：父版本 bot + 当前 Top 3 + H2H 弱点对手（最多 2 个）
  3. 与每个对手运行 `mirror_battle(n_games=1)`
  4. 阻断条件：编译/冒烟失败、输给父版本、**总输≥3 且 总输≥赢+2**、对局超时、无对手可选（`no_opponents`）、对局异常（`match_exception`）
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
  2. 运行时守卫：编译检查、冒烟测试、决策测试（≥70%）、文件大小（≤1000行）、`review_approved` 检查
  3. `git add` + `git commit` + `git tag bot-v{N}`
  4. 验证 git tag 确实创建成功
  5. 写入 `.completed` 标记文件
  6. 归档调用：`archive_generation()` 生成快照、`archive_rotate_files()` 归档轮转、`archive_old_logs()` 日志压缩
  7. 清除 pipeline checkpoint
  8. 发送 `.reap_signal` 通知守护进程刷新 bot 列表
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
| **有无 LLM** | 条件性（仅连续 3 代评分下降 或 `EVOLUTION_ALWAYS_ARCHIVE_LLM=1` 时调用 LLM） |
| **LLM 角色** | CYCLE ARCHIVIST |
| **模型** | Sonnet |
| **工具** | Bash, Read（通过 `_run_archivist_analysis` 传入 `run_claude_query`） |

**确定性步骤**（始终执行，无 LLM）:
1. **一致性验证**：确认 `.completed` 文件存在、git tag 存在、ratings 包含新 bot
2. **自动 reap**：若活跃 bot > `MAX_ACTIVE_BOTS`(30)，自动调用 `reap_weakest`
3. **加载归档快照**：读取 `results/archive/v{N}.json`（由 `commit_bot` 内的 `archive_generation()` 创建）

**条件性 LLM 分析**（仅在评分下降时触发）:
- 检查最近 5 代的归档快照，判断是否连续 3 代评分下降
- 触发时调用 `_run_archivist_analysis(version, source_v, snapshot, ui)`
- LLM 输出追加到归档快照的 `archivist_notes` 字段

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

### 代际结束

`_run_one_cycle()` 检测到 `cycle_completed`:
- 清除 `orchestrator_session.json`
- 返回花费给 `orchestrator_loop()`
- `orchestrator_loop()` 记录花费，`sleep(5)` 后进入下一代

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

### 3.3 并行→串行回退

```
_execute_workers():
  并行运行所有 tasks (Semaphore(3))
      │
      ├── 全部成功 → 返回 True
      └── 任一失败 → 清除目标目录，从源 bot 重新复制
                      串行逐个执行 tasks → 返回结果
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

### 3.5 中断信号链（Web 模式）

`python web/main.py` 模式下 Ctrl+C 的完整传递路径：

```
用户按 Ctrl+C (SIGINT)
  │
  ▼ uvicorn 内置 SIGINT handler
  │
  ▼ FastAPI lifespan shutdown (app.py:52-63)
  │
  ├─ 1. _evolution_task.cancel()             ← 触发 CancelledError
  │     await asyncio.wait_for(task, 10s)    ← 等待优雅退出
  │
  │     orchestrator.py CancelledError 处理 (line 396-408):
  │       ├─ query_gen.aclose()              ← 关闭 LLM 流式生成器
  │       ├─ _clear_orchestrator_session()   ← ⚠️ session 被清除！
  │       └─ raise CancelledError            ← 继续传播
  │
  │     orchestrator_loop finally (line 540-547):
  │       ├─ _daemon_stop.set()             ← 停止监控线程
  │       └─ stop_daemon()                  ← 终止 daemon 子进程
  │
  ├─ 2. stop_daemon() (再次调用，幂等)
  │     ├─ os.killpg(pgid, SIGTERM)         ← 终止整个进程组
  │     ├─ daemon wait(5s)
  │     ├─ 超时 → os.killpg(pgid, SIGKILL)
  │     └─ 删除 .daemon_pid
  │
  └─ 3. atexit.register(stop_daemon)        ← 退出时的冗余安全网

Daemon 内部收到 SIGTERM:
  ├─ signal handler: running = False
  ├─ drain in-flight futures (10s/each)     ← 等待进行中的对局完成
  ├─ executor.shutdown(wait=False)
  └─ save_cycle()                           ← 最终持久化 ratings/stats
```

**关键**: Web 模式下 `CancelledError` **清除** `orchestrator_session.json`（line 402），重启后无法恢复原 LLM 对话，但 `pipeline_state.json` **保留**，Orchestrator 通过 checkpoint 信息从断点继续。

### 3.6 中断信号链（CLI 模式）

`python web/core/orchestrator.py` 独立模式：

```
用户按 Ctrl+C (SIGINT)
  │
  ▼ KeyboardInterrupt in _run_one_cycle (line 384-394)
  │
  ├─ query_gen.aclose()                     ← 关闭 LLM 流式生成器
  ├─ 不调用 _clear_orchestrator_session()   ← ⚠️ session 保留！
  └─ 写入日志 "[INTERRUPTED]"
  │
  ▼ run_orchestrator_cli finally (line 582-587)
  │
  └─ stop_daemon()                          ← 无 daemon（CLI 模式未启动）
```

**关键**: CLI 模式下 `KeyboardInterrupt` **不清除** session 文件。重启后可通过 `resume=session_id` 恢复原始 LLM 对话，拥有完整上下文。

两种模式的差异原因：Web 模式由 `task.cancel()` 触发 `CancelledError`（程序性取消），CLI 模式由 `SIGINT` 触发 `KeyboardInterrupt`（用户中断）。

### 3.7 重启恢复流程

#### Web 模式重启

```
python web/main.py
  │
  ├─ app_state._load_config()               ← 读取 app_config.json 恢复 daemon 配置
  ├─ uvicorn.run() → lifespan startup
  │     ├─ app_state.bootstrap(find_current_v())
  │     └─ asyncio.create_task(orchestrator_loop)
  │           ├─ start_daemon()
  │           │     ├─ 检查 .daemon_pid → 若有孤儿 → killpg → 清理
  │           │     ├─ Popen(start_new_session=True) → 新 daemon
  │           │     └─ 写 .daemon_pid
  │           └─ daemon_monitor_thread 启动
  │
  └─ _run_one_cycle():
        ├─ _load_orchestrator_session() → None（已被清除）
        ├─ 新建 LLM 对话（无 resume）
        └─ _build_context() 检测 pipeline_state.json:
              ├─ 若 checkpoint 存在:
              │     注入 "PIPELINE CHECKPOINT: v{next_v} reached stage='{stage}'"
              │     注入下一步建议（见下方映射表）
              │     注入 "ENVIRONMENT ANOMALIES DETECTED" 警告
              └─ Orchestrator LLM 看到提示后从断点继续
```

#### CLI 模式重启

```
python web/core/orchestrator.py
  │
  └─ _run_one_cycle():
        ├─ _load_orchestrator_session() → session_id（保留）
        ├─ resume=session_id             ← SDK 恢复原 LLM 对话
        └─ _build_context() 同样注入 checkpoint 信息
              └─ LLM 拥有完整上下文 + checkpoint 提示，直接继续
```

#### Checkpoint 阶段提示映射

`_build_context()` (orchestrator.py:194-219) 根据 checkpoint 的 stage 注入下一步建议：

| stage | 注入提示 |
|-------|---------|
| `prepared` | Workers not yet run → call `execute_workers` |
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

#### 监控线程自动重启

`daemon_monitor_thread()` (evolution_infra.py:461-486):

```
3 秒轮询:
  ├─ daemon_proc.poll() is not None? (已退出)
  │     ├─ restart_count > 5 → 停止自动重启，日志报错
  │     ├─ backoff = min(3 * 2^(n-1), 120) 秒
  │     │     → 3, 6, 12, 24, 48, 96, 120...
  │     ├─ stop_event.wait(backoff)  ← 等待期间可被停止
  │     └─ start_daemon() 重启
  └─ daemon 正常运行 → restart_count 归零
```

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
| `orchestrator_session.json` | 每次 SDK `ResultMessage` 返回 `session_id` 时 | 自然完成 / `CancelledError` / API error / 529 retry / auth error backoff | LLM 对话 session ID，用于 `resume` 参数 |
| `pipeline_state.json` | 每个 pipeline 阶段完成时 (`_record_gate`) | `commit_bot` 成功后（`clear_pipeline_checkpoint`） | 阶段断点 + gate 结果 + master plan |
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

```
                    ┌─────────────────────────────────────────────┐
                    │         orchestrator_loop() 启动            │
                    │    (后台 asyncio Task, 由 app.py 创建)       │
                    └──────────────────┬──────────────────────────┘
                                       │
                    ┌──────────────────▼──────────────────────────┐
                    │    _run_one_cycle() — Orchestrator LLM 会话  │
                    │    输入: _build_context() → ratings/tags/    │
                    │          checkpoint/failures 注入 prompt     │
                    │    工具: MCP tools + Bash + Read             │
                    │    输出: 工具调用序列                         │
                    └──────────────────┬──────────────────────────┘
                                       │
         ┌─────────────────────────────┼─────────────────────────────┐
         │                             │                              │
    ┌────▼─────┐              ┌────────▼────────┐           ┌────────▼────────┐
    │get_status │              │wait_for_eval     │           │housekeeping     │
    │(无 LLM)   │              │(无 LLM, 轮询)    │           │(无 LLM)         │
    │读取:      │              │读取:             │           │reap/cleanup/trim│
    │ratings,   │              │bot_stats.json    │           └─────────────────┘
    │bot_stats, │              │                  │
    │daemon_stats│             └────────┬─────────┘
    └───────────┘                       │
         │                     ┌────────▼─────────┐
         │                     │rating_reliable?   │
         │                     └──┬─────────────┬──┘
         │                  false │             │ true
         │                     ┌──▼───┐   ┌─────▼──────────┐
         │                     │跳过  │   │analyze_stagnation│
         │                     │停滞  │   │📎 STAGNATION     │
         │                     │分析  │   │  ANALYST         │
         │                     └──┬───┘   │输入: history×10  │
         │                        │       │      + h2h_top5   │
         │                        │       │输出: is_stagnant  │
         │                        │       │      + recommend  │
         │                        │       └──────┬───────────┘
         │                        │              │
         │              ┌─────────▼──────────────▼───────────────┐
         │              │                                        │
         │         ┌────▼──────────────┐  ┌──────────────────────▼───┐
         │         │run_match_analysis │  │run_performance_verification│
         │         │📎 MATCH ANALYST   │  │📎 PERFORMANCE ANALYST     │
         │         │输入: replay 摘要   │  │输入: history+wr+h2h+stats│
         │         │      (8败+4险胜)  │  │      ×10周期              │
         │         │输出: weaknesses   │  │输出: trend+weaknesses     │
         │         │      +street_wk   │  │      +diversity+suggestion│
         │         └────┬──────────────┘  └──────────┬───────────────┘
         │              │ match_analysis              │ performance_verification
         │              └─────────────┬──────────────┘
         │                            │
         │               ┌────────────▼────────────┐
         │               │run_master               │
         │               │📎 MASTER                │
         │               │工具: Bash, Read          │
         │               │输入: stagnation_info     │
         │               │      + match_analysis   │
         │               │      + perf_verification│
         │               │输出: JSON tasks[]        │
         │               │      (1-3 个 worker 任务) │
         │               └────────────┬─────────────┘
         │                            │ plan["tasks"]
         │               ┌────────────▼────────────┐
         │               │prepare_next_gen (无 LLM) │
         │               │复制 bots/claude_v{N}/    │
         │               │写入 checkpoint=prepared  │
         │               └────────────┬─────────────┘
         │                            │
         │               ┌────────────▼────────────┐
         │               │execute_workers          │
         │               │📎 WORKERS (并行≤3)      │
         │               │工具: Bash, Read, Edit   │
         │               │输入: worker_prompt.md   │
         │               │      + task instructions│
         │               │      + failure_memory   │
         │               │      + reviewer_feedback│
         │               │输出: 代码写入文件系统    │
         │               │自检: compile+smoke/重试  │
         │               │回退: parallel→serial    │
         │               │断路器: max 6 次调用      │
         │               └────────────┬─────────────┘
         │                            │
         │               ┌────────────▼────────────┐
         │               │run_quality_gates (无LLM)│
         │               │compile+smoke+decision   │
         │               │+size (≤1000行/文件)      │
         │               │pass_rate ≥ 70%           │
         │               └────────────┬─────────────┘
         │                            │
         │               ┌────────────▼────────────┐
         │               │run_review               │
         │               │📎 LEAD CODE REVIEWER    │
         │               │工具: Bash, Read          │
         │               │输入: reviewer_prompt.md │
         │               │      + master_plan JSON  │
         │               │输出: approved+score      │
         │               │      +feedback+risk_areas│
         │               └────────────┬─────────────┘
         │                            │
         │               ┌────────────▼────────────┐
         │               │run_critic               │
         │               │📎 STRATEGY CRITIC       │
         │               │工具: Bash, Read          │
         │               │输入: critic_prompt.md   │
         │               │      + master_plan       │
         │               │输出: score(1-10)+approved│
         │               │阈值: ≥6 通过             │
         │               │force_advance 可绕过      │
         │               └────────────┬─────────────┘
         │                            │
         │               ┌────────────▼────────────┐
         │               │run_precommit_eval(无LLM)│
         │               │镜像对战: vs父+Top+弱点    │
         │               │阻断: 输父版/崩溃/退化     │
         │               └────────────┬─────────────┘
         │                            │
         │               ┌────────────▼────────────┐
         │               │commit_bot (无 LLM)      │
         │               │运行时守卫 (编译/冒烟/决策)│
         │               │git commit + tag + 验证   │
         │               │归档快照+轮转+日志压缩    │
         │               │清除 checkpoint           │
         │               └────────────┬─────────────┘
         │                            │
         │               ┌────────────▼────────────┐
         │               │run_archivist            │
         │               │确定性: 一致性验证+reap   │
         │               │条件📎: ARCHIVIST LLM    │
         │               │  (仅连续3代评分下降时)   │
         │               │写入 archived checkpoint  │
         │               └────────────┬─────────────┘
         │                            │
         │               ┌────────────▼────────────┐
         │               │新一代完成, sleep(5)       │
         │               │回到 get_status()         │
         │               └─────────────────────────┘
         │
    ┌────▼──────────────────────────────────────────────┐
    │后台并行运行:                                        │
    │  elo_daemon.py (子进程)                            │
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
- **子代理 MCP 屏蔽**: `_BLOCKED_MCP_TOOLS` 屏蔽以下外部工具（防止子代理访问网络）：
  - `mcp__web-reader__webReader`
  - `mcp__web-search-prime__web_search_prime`
  - `mcp__zread__get_repo_structure`
  - `mcp__zread__read_file`
  - `mcp__zread__search_doc`
- **角色边界**: Worker 受 prompt + reviewer 双重约束 — Logic Architect 不改常数，Tuner 不加函数
- **Gate Ledger**: Pipeline checkpoint 强制阶段顺序 — 每个阶段写入 gate 记录，后续阶段验证前置 gates 完整
- **阶段常量**: `STAGE_ORDER = [prepared, workers_done, quality_passed, reviewed, critic_checked, verified, archived]`
- **归档阶段**: `run_archivist` 在 `commit_bot` 后执行，写入 `archived` checkpoint，确保 post-commit 一致性验证和自动 reap。仅连续 3 代评分下降时调用 LLM。

---

## 附录：真实循环示例（v28 → v29）

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
