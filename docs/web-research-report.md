# `web/` 目录综合研究报告

## 1. 系统架构总览

### 1.1 技术栈

`web/` 目录是一个全栈 Web 应用，为扑克 AI 自进化框架提供实时监控和控制界面。

| 层 | 技术 | 版本 |
|---|---|---|
| 后端框架 | FastAPI + uvicorn | Python 3.x |
| 前端框架 | React + TypeScript | React 19, Vite 6 |
| CSS 框架 | Tailwind CSS | 4.x |
| 图表库 | ApexCharts | 4.1 |
| LLM 集成 | claude_agent_sdk | Claude Sonnet |
| 评分算法 | Glicko-2 | 自实现 |
| 构建工具 | Vite | 6.1 |
| 包管理 | npm | lockfile 存在 |

### 1.2 模块结构

系统分为四大模块，各司其职：

```
web/
├── main.py              入口：编排器 + 守护进程 + 前端 :8000
├── server/              FastAPI 后端（路由、缓存、状态管理）
├── core/                进化引擎 + 守护进程 + LLM 流水线 + 数据文件
├── frontend/            React 前端（SPA）
├── tests/               后端测试套件
└── logs/                运行时日志
```

### 1.3 进程模型

系统运行时涉及三个进程：

1. **主进程**（FastAPI + uvicorn）：服务 HTTP/SSE 请求，运行 `orchestrator_loop` 异步任务
2. **守护进程子进程**（`elo_daemon.py`）：`ProcessPoolExecutor` 驱动的镜像对战评分引擎
3. **Bot 子进程**（由守护进程/引擎按需启动）：每场对局独立的 Python 进程

主进程内部包含：
- uvicorn 事件循环（服务 HTTP）
- `orchestrator_loop` 异步任务（LLM 驱动的进化循环）
- `daemon_monitor_thread` 后台线程（监控守护进程存活）
- SSE 推送线程（`EventBroadcaster`）

### 1.4 模块间依赖关系

```
main.py
  ├── server/app.py (FastAPI 应用)
  │     ├── server/routes/* (9 个路由模块)
  │     ├── server/cache.py (共享文件缓存)
  │     ├── server/state.py (全局状态单例)
  │     └── server/static/ (构建后的前端资源)
  ├── core/orchestrator.py (进化主循环)
  │     ├── core/orchestrator_context.py
  │     ├── core/orchestrator_session.py
  │     ├── core/generation_scheduler.py
  │     └── core/evolution_infra.py (基础设施)
  │           ├── core/daemon_management.py
  │           ├── core/llm_query.py
  │           ├── core/rate_limiter.py
  │           └── core/glicko2.py
  ├── core/web_ui.py (SSE 广播)
  └── core/shutdown_manager.py
```

### 1.5 代码规模统计

| 类别 | 文件数 | 代码行数（约） |
|---|---|---|
| 后端 Python (`server/`) | 15 | 1,439 |
| 核心逻辑 (`core/`, 不含参考 bot) | 39 | 9,715 |
| 核心引擎 (`core/engine/`) | 3 | 1,301 |
| 提示词模板 (`core/prompts/`) | 14 | 883 |
| 参考 Bot (`core/reference_bots/`) | 60 | ~30,200 |
| 前端 TS/TSX (`frontend/src/`) | 40 | ~5,300 |
| 测试 (`tests/`) | 22 | ~3,600 |
| **总计（不含参考 bot）** | **~133** | **~22,238** |

---

## 2. 后端核心逻辑分析

### 2.1 演化系统

#### 2.1.1 三阶段生成周期

每代进化遵循严格的三阶段周期，由 `generation_scheduler.py`（358 行）管理：

**阶段 1 — `prepare_generation()`**（可丢弃重跑）

位于 `/home/zzx/project/pok/web/core/generation_scheduler.py`，核心流程：

1. 版本确定：`find_current_v()`（全局最高，级联查找 git tag > `.completed` > 目录名）+ `find_latest_active_v()`
2. 池清理：活跃 bot > `MAX_ACTIVE_BOTS`（30）时淘汰最弱 bot
3. 等待评估：`wait_for_daemon_eval()` 等待最新 bot 积累足够对局
4. 清理不完整目录：删除无 git tag 且无活跃 checkpoint 的 bot 目录
5. 并行分析：`asyncio.gather(_run_combined_analysis(), _analyze_recent_matches(), return_exceptions=True)`
6. 策略决策：`_decide_strategy()` 确定性代码逻辑

策略决策树（优先级从高到低）：
- 停滞 + 高/中置信度 → `crossover`
- 明确分支推荐 → `master`，从推荐的 `branch_from` 进化
- 多样性注入 → `crossover`
- LLM 推荐源 bot → `master`，从推荐版本进化
- 兜底 → `master`，从当前活跃版本进化

输出 `GenerationContext` 数据类，包含 `current_v`, `next_v`, `strategy`, `source_v`, `crossover_parents`, 分析文本等字段。

**阶段 2 — `_run_one_cycle()`**（状态保留，可恢复）

位于 `/home/zzx/project/pok/web/core/orchestrator.py`（639 行），核心流程：

1. 构建上下文（`_build_context()`）
2. 加载 pipeline checkpoint + 保存的 session_id
3. 创建 `ClaudeAgentOptions`（model=sonnet, MCP server=evolution）
4. 流式处理 LLM 输出：`TextBlock` → 实时显示、`ToolUseBlock` → 记录工具调用、`ThinkingBlock` → 显示思考过程
5. 错误处理层次：超时（3600s）→ 529 限流指数退避 → 429 配额解析重置时间 → 401/403 认证退避

**阶段 3 — `post_generation_cleanup()`**（幂等）

位于 `/home/zzx/project/pok/web/core/generation_scheduler.py`：
1. 活跃 bot > 30 时批量淘汰
2. 经验池整合（每 3 代或 `RECENT_LESSONS` >= 4 时触发）

三阶段可中断性设计：

| 阶段 | 可中断性 | 原因 |
|---|---|---|
| Phase 1 (prepare) | 可丢弃 | 纯分析，不修改状态 |
| Phase 2 (run cycle) | 状态保留 | session + checkpoint 持久化 |
| Phase 3 (cleanup) | 幂等 | 可重复执行无副作用 |

#### 2.1.2 Session 持久化与启动恢复

位于 `/home/zzx/project/pok/web/core/orchestrator_session.py`（148 行）。

**Session 文件管理**：`results/orchestrator_session.json`，原子写入（tmp + fsync + rename）。清除时机：仅自然完成时。

**`_startup_recovery()` 四种恢复场景**：

| checkpoint | session | 场景 | 处理方式 |
|---|---|---|---|
| 无 | 无 | Case A | 全新开始 |
| 无 | 有 | Case D | 清除 session，全新开始 |
| 有 | 无 | Case B | 新 LLM session，从 checkpoint 阶段恢复 |
| 有 | 有 | Case C | 完整恢复：LLM 会话 + pipeline 状态 |

关键判断逻辑：`archived` 或 `prepared` 阶段视为无价值直接清除；`direction_audited` 到 `verified` 阶段即使超过 30 分钟也保留。

#### 2.1.3 LLM 上下文构建与 PreCompact 钩子

位于 `/home/zzx/project/pok/web/core/orchestrator_context.py`（303 行）。

**`_build_context()` 两条路径**：

1. **gen_ctx 提供（正常流程）**：精简上下文 — 基本信息、工具参考列表（Orchestrator 无 Bash/Read 工具）、分析数据、阶段提示
2. **无 gen_ctx（恢复/降级）**：完整状态快照 — 评分可靠性、H2H 胜率、不完整 bot 警告、Worker 失败记录

**`_inject_master_plan_hint()`**：解决 Orchestrator LLM 无 Bash/Read 工具的问题，直接内联每个 Worker 任务的摘要到上下文中。

**`_make_precompact_hook()`**：LLM 上下文压缩前注入关键状态（当前版本、checkpoint 阶段、Master plan 任务列表），使用 `HookMatcher(matcher="*")` 匹配所有 PreCompact 事件。

#### 2.1.4 Pipeline 检查点机制

位于 `/home/zzx/project/pok/web/core/evolution_infra.py`（743 行）。

**原子写入**：`write_pipeline_checkpoint()` 使用 tmp + fsync + rename + `fcntl.LOCK_EX`。

检查点内容包含：
- `next_v`, `source_v`, `stage` — 版本和阶段
- `master_plan` — Master 生成的任务计划
- `gate_results` — 各质量门结果
- `reviewer_feedback` — 审查反馈
- `generation_attempt` — 代内重试计数
- `direction_audit` — 方向审计结果

**阶段顺序**（`STAGE_ORDER`）：
```
prepared → direction_audited → master_planned → workers_done →
quality_passed → reviewed → critic_checked → verified → archived
```

### 2.2 LLM 集成

#### 2.2.1 查询执行原语

位于 `/home/zzx/project/pok/web/core/llm_query.py`（314 行）。

**`run_claude_query(prompt, context_files, ui, role_name, ...)`**：全局 LLM 调用原语。通过 `claude_agent_sdk.query()` 发起流式调用，返回 `(output_text, cost_usd, usage)` 三元组。

提示构造模式："基础 prompt + 上下文文件注入"拼接模式。上下文文件以 `--- path ---\ncontent` 格式追加。总长度超过 `MAX_PROMPT_CHARS`（700K 字符）时按比例压缩各上下文文件（每个至少 500 字符），基础 prompt 不压缩。

**`parse_json_output(output)`**：三级 JSON 提取策略 — 从最后的 `json` 代码块逆向解析 → 花括号平衡匹配 → 整体 raw JSON 尝试。核心设计决策：从**最后一个** `json` 代码块开始（因为 LLM 可能在计划前引用模板中的示例）。

**错误处理与重试**：
- 调用前预检：`rate_limiter.is_blocked()`，429 配额耗尽则阻塞等待
- 529 过载：指数退避（30s/60s/120s）最多 3 次
- 429 配额：解析重置时间戳，阻塞等待后重试 1 次
- 超长响应豁免：`_is_rate_limited` 和 `_is_quota_exceeded` 对超过 2000 字符的输出返回 False

#### 2.2.2 Agent 角色体系

| Agent | 工具权限 | 职责 | 重试次数 |
|---|---|---|---|
| Orchestrator | MCP tools only | 驱动流水线，决定进化流程 | N/A（自身管理） |
| Master Architect | Bash, Read | 分析状态，规划 Worker 任务 | 3 |
| Workers | Bash, Read, Edit | 修改 bot 源代码 | 4 |
| Code Reviewer | Bash, Read | 审查代码变更质量 | 3 |
| Critic | Bash, Read | 策略评估，1-10 分评分 | 无内部重试 |
| Direction Auditor | None | 检测进化方向重复性 | 无内部重试 |
| Combined Analyst | None | 停滞检测 + 性能验证 | 3 |

**Worker 执行引擎**（`agent_workers.py`，212 行）：

Worker 顺序执行（非并行），避免文件竞争。每个 Worker 有四层递进验证：
1. LLM 输出完成
2. 零变更检测（对比源/目标 target_files 内容）
3. `py_compile` 编译检查
4. 冒烟测试（单场 mirror battle）

重试时追加更详细的错误信息，第 3 次起建议 "FUNDAMENTALLY DIFFERENT approach"。失败记忆注入：最近 5 条 Worker 失败记录附加到 prompt 末尾。

**Master Architect**（`agent_master.py`，173 行）：

读取 `master_prompt.md` 模板，注入停滞/比赛/性能验证等上下文。比赛分析部分有预算控制：match_analysis 截取最后 10000 字符，performance_verification 截取 4000 字符。

**Critic / Reviewer**（`agent_review.py`，288 行）：

Critic 评估策略价值，返回 `{score, approved, strategic_assessment, feedback, local_optima_warning}`。采用 "安全降级" 模式 — 任何失败都不阻断流水线，返回 `{score:0, approved:False}`。Reviewer 通过 `reviewer_prompt.md` 模板审查代码变更质量。

**Combined Analyst**（`combined_analyst.py`，330 行）：

合并的停滞+性能分析（单次 LLM 调用替代原来的两次）。包含统计预检短路：数据明确时（delta<5=停滞, delta>20=改进）跳过 LLM 调用。RD>150 时统计不可靠，退回 LLM 判断。

#### 2.2.3 Pydantic 输出校验

位于 `/home/zzx/project/pok/web/core/output_schema.py`（139 行）。

7 个 Pydantic 模型覆盖所有 Agent 的结构化输出：
- `MasterPlan`：1-3 个 `WorkerTask`，每个含 worker_id(1-3)、role、target_files、worker_prompt(min_length=20)
- `CriticResult`：score(1-10)、approved、evidence 嵌套结构（h2h_weaknesses/experience_pool_refs/diff_refs）
- `DirectionAuditResult`：repetition_detected、exhausted_directions、confidence
- 等等

`validate_agent_output(agent_name, data)` 返回 `(validated_data, errors)`，验证失败返回原始数据+错误消息（不抛异常）。

### 2.3 MCP 工具系统

#### 2.3.1 工具注册

位于 `/home/zzx/project/pok/web/core/tools.py`（107 行）。

区分 MCP 工具集（约 15 个，供 Orchestrator LLM 调用）和全量工具集（额外包含管理类工具，供 HTTP API 端点手动调用）。

MCP 工具列表：
1. `run_master`, `execute_workers`, `run_quality_gates`, `run_review`, `run_critic`, `run_precommit_eval` — 流水线阶段
2. `prepare_next_gen`, `run_direction_audit` — 规划准备
3. `commit_bot`, `run_archivist`, `run_crossover` — 提交归档
4. `get_bot_info`, `get_match_history`, `get_h2h`, `get_bot_stats` — 数据查询

#### 2.3.2 阶段门控系统

**门控依赖链**：

```
prepare_next_gen → [prepared checkpoint]
    ↓
run_direction_audit → [direction_audited checkpoint]
    ↓
run_master → [master_planned checkpoint, validated plan]
    ↓
execute_workers → [requires master_plan in checkpoint]
    ↓                  (熔断器: MAX_WORKER_FAILURES=6)
run_quality_gates → [quality: compile + smoke + decision(70%) + size + code_changed]
    ↓
run_review → [requires quality gate passed]
    ↓
run_critic → [requires quality + review gates passed]
    ↓                  (retry_workers / force_advance)
run_precommit_eval → [requires quality + review + critic gates passed]
    ↓
commit_bot → [Gate Ledger: all 4 gates passed + review_approved param]
    ↓
run_archivist → [post-commit consistency check]
```

#### 2.3.3 质量门控详情

位于 `/home/zzx/project/pok/web/core/tool_gates.py`（466 行）。

**`run_quality_gates` 五项检查**：

| 检查项 | 函数 | 通过条件 |
|---|---|---|
| 编译检查 | `verify_code()` | 无编译错误 |
| 冒烟测试 | `run_smoke_test()` | 1 场镜像对战不崩溃 |
| 决策测试 | `run_decision_test_details()` | 通过率 >= 70% 且无 critical 失败 |
| 文件大小 | `check_code_size()` | 核心文件 <= 1500 行，辅助 <= 1200 行 |
| 代码变更 | `_py_files_changed_between()` | 至少一个 .py 文件不同 |

**防僵尸循环**：`code_changed` 检查防止 Worker 报告成功但实际零变更时质量门控通过父版本代码。

#### 2.3.4 Worker 边界验证

位于 `/home/zzx/project/pok/web/core/tool_helpers.py`（551 行）。

三级边界检查：
1. **target_file_violation**：变更的 .py 文件不在任何 Worker 的 `target_files` 中
2. **new_file_violation**：新建的 .py 文件不在 `target_files` 中
3. **hyperparameter_boundary_violation**：Tuner 的文件除了数值字面量外有结构性变更（用 `_NUMERIC_LITERAL_RE` 正则将数字替换为 `<NUM>` 后比较文本）

`worker_snapshots` 机制：记录每个 Worker 执行前的文件快照，当多个 Worker 修改同一文件时可精确判定每个 Worker 的变更范围。

#### 2.3.5 提交门控账本

位于 `/home/zzx/project/pok/web/core/tool_commit.py`（411 行）。

`commit_bot` 的 Gate Ledger 验证是最严格的检查点：

| 门控 | 必须满足的条件 |
|---|---|
| quality | `all_passed === true` 且 `critical_scenarios_passed === true` |
| review | `approved === true` |
| critic | `approved === true` 且 `score >= 6`，或 `force_advanced === true` |
| precommit_eval | `passed === true` |

缺少任何门控或任何门控失败都阻塞提交。额外守卫：`review_approved` 参数必须为 `true`。

提交后操作：验证 git tag → 创建 `.completed` 哨兵 → 归档 → 清除 checkpoint → 更新 `AppState` → 写 `.reap_signal` 通知守护进程 → 写 `priority_eval.json` 优先评估。

#### 2.3.6 决策测试系统

位于 `/home/zzx/project/pok/web/core/decision_tester.py`（202 行）。

12 个 Critical 场景检测灾难性错误：
- 翻前：AA/KK/QQ/JJ/AKs 首次行动/面对加注
- 翻后：顶暗三条、坚果同花、坚果顺子、两对、葫芦、同花听牌
- 特殊：错过听牌面对大注

测试机制：构造标准 JSON 输入 payload，启动 Bot 子进程（10s 超时），解析输出 action，与 `forbidden_actions`/`expected_actions` 对比。通过条件：总通过率 >= 70% 且零 critical 失败。

### 2.4 守护进程与评分

#### 2.4.1 Glicko-2 评分算法

位于 `/home/zzx/project/pok/web/core/glicko2.py`（222 行）。

`Glicko2Player` 使用 `__slots__` 优化内存，维护 `r`（rating, 默认 1500）、`rd`（rating deviation, 默认 350）、`sigma`（volatility, 默认 0.06）。

核心函数：
- **`update_rating_period(player, results)`**：标准 Glicko-2 批量更新，包含 Illinois 算法迭代求解新 sigma（最多 1000 次迭代）
- **`update_single_game(player, opponent, score)`**：简化逐局更新，跳过 Illinois sigma 搜索
- **`decay_rd(player, elapsed_periods=1)`**：不活跃 bot 的 RD 衰减
- **`conservative_rating()`**：返回 `r - 2*rd`（95% 置信下界，用于排序）

#### 2.4.2 镜像对战守护进程

位于 `/home/zzx/project/pok/web/core/elo_daemon.py`（738 行）。

**整体架构**：独立子进程，`ProcessPoolExecutor` 并发执行镜像对战。主循环采用"完成-补充"模式。

**`pick_matches()` 智能调度**：
- 60% 权重给"欠评估"配对（对局数低于 50）
- 40% 权重给"多样性"配对（评分差距大的配对）
- 低覆盖率 bot 获得 `new_pair_bonus`
- 优先评估 bot（`priority_eval.json`）获得 +2.0 强力提升
- 按 per-bot 上限过滤，防止某个 bot 占满所有 slot

**`process_result()` 逐局 Glicko-2 更新**：使用当时的实时对手评分调用 `update_single_game()`，不是批量更新。

**`save_cycle()` 周期性持久化**（每 20 局或 60 秒）：
- 对未参与对局的 bot 执行 `decay_rd()`
- 原子写入 ratings（tmp + fsync + replace）
- 追加 `rating_history.jsonl` 快照
- 裁剪回放文件至 200 个

**信号处理**：SIGTERM/SIGINT → 设置 `running=False`。孤儿检测：每 5 秒检查 `os.getppid()`。reap 信号：检测 `.reap_signal` 文件触发 bot 列表刷新。

#### 2.4.3 Daemon 生命周期管理

位于 `/home/zzx/project/pok/web/core/daemon_management.py`（233 行）。

`start_daemon(workers, pairs)`：
1. 检查内存中 `daemon_proc` + PID 文件孤儿
2. `subprocess.Popen(start_new_session=True)` 创建独立进程组
3. 原子写入 PID 文件
4. 启动 `_drain_stdout` 守护线程防管道死锁
5. 注册 `atexit.register(stop_daemon)`

`stop_daemon()`：先 SIGTERM 整个进程组 → 等 5 秒 → 超时 SIGKILL。

`daemon_monitor_thread(ui, stop_event)`：每 3 秒轮询，检测退出后指数退避重启（3*2^(n-1) 秒，上限 120s），最多 5 次连续重启。

---

## 3. API 层分析

### 3.1 应用初始化与生命周期

位于 `/home/zzx/project/pok/web/server/app.py`（130 行）。

**启动阶段**：
1. `configure_logging(broadcaster=broadcaster)` 配置结构化日志
2. `app_state.bootstrap(find_current_v())` 初始化全局状态
3. 读取守护进程配置，创建 `ShutdownManager`
4. `app_state.try_set_running(True)` 原子性 CAS 防重复启动
5. 创建 `asyncio.Task` 运行 `orchestrator_loop`

**关闭阶段**：
1. `app_state.stop_running()` 取回 Task 引用
2. `shutdown_mgr.request_shutdown()` 发送关闭信号
3. `asyncio.wait_for(task, 20s)` → 超时则 cancel + 等 5s
4. `_daemon_shutting_down = True` 防监控线程重启
5. 线程池中调用 `stop_daemon()`

**路由注册**：9 个路由模块挂载到不同前缀。

**SPA 支持**：检测 `server/static/` 目录，存在时挂载 `/assets` 为 StaticFiles，注册兜底路由返回 `index.html`。

### 3.2 SSE 数据流端点

#### 3.2.1 `/api/data/stream` — 周期性轮询 SSE

位于 `/home/zzx/project/pok/web/server/routes/data_stream.py`（197 行）。

以 1 秒为 tick 单位的无限循环，三级推送频率：

| 频率 | 推送事件 | 数据内容 |
|---|---|---|
| 3 秒 | ratings, daemon, bots, stats, rate_limit | 评分排名、守护进程状态、bot 列表、比赛统计 |
| 10 秒 | matches, generations | 最近 100 场比赛、归档版本 |
| 15 秒 | matrix, h2h, bot_stats, history | 对战矩阵、H2H、统计、评分历史（降采样至 200 点） |
| 30 秒 | ping | 空心跳 |

数据读取通过 `cached_read(key, path)` 统一缓存。守护进程状态通过文件 mtime 判断：<60s=active, <600s=recent, >=600s=idle。

#### 3.2.2 `/api/evolution/stream` — 事件驱动 SSE

位于 `/home/zzx/project/pok/web/server/routes/evolution.py`（40 行）。

基于 `EventBroadcaster` 的发布-订阅模式：
1. `broadcaster.add_client()` 注册新客户端，获得 `client_id` 和 `asyncio.Queue`
2. 循环 `queue.get(timeout=30s)` 获取事件
3. 超时未收到发送 `ping` 心跳
4. 断开时 `broadcaster.remove_client(cid)` 清理

**与 data_stream 的本质区别**：data_stream 是轮询式（定时读文件推送），evolution_stream 是事件驱动式（由 `WebUI` 写入时实时推送）。EventBroadcaster 内部维护环形缓冲区（500 条），新客户端连接时回放历史。

### 3.3 控制 API

位于 `/home/zzx/project/pok/web/server/routes/control.py`（269 行）。

| 端点 | 方法 | 功能 |
|---|---|---|
| `/api/control/start` | POST | 启动进化循环（原子 CAS 防重复） |
| `/api/control/stop` | POST | 两阶段停止（SIGTERM → cancel → 等 10+5s） |
| `/api/control/config` | GET/PUT | Daemon 配置（动态启停守护进程） |
| `/api/control/status` | GET | 应用状态快照 |
| `/api/control/tool/{name}` | POST | 手动调用 MCP 工具（懒加载工具映射） |
| `/api/control/tools` | GET | 列出可用 MCP 工具 |
| `/api/control/orchestrator/session` | GET/DELETE | Session 查询/清除 |
| `/api/control/decisions` | GET | 工具调用决策日志 |
| `/api/control/reset` | POST | 重置进化到基线（v1-v6）+ 自动重启 |

**关键设计**：
- `start_evolution()` 使用 `app_state.try_set_running(True)` 原子检查+设置，防止重复启动
- `stop_evolution()` 实现两阶段取消：第一次 cancel + 等 10 秒，超时再 cancel + 等 5 秒
- `call_tool()` 的后置状态同步：若工具为 `start_daemon`/`stop_daemon`，自动更新 `app_state` 的 `daemon_enabled`

### 3.4 数据查询 API

#### 3.4.1 评级与历史（ratings.py）

位于 `/home/zzx/project/pok/web/server/routes/ratings.py`（200 行），10 个端点。

`GET /api/ratings`：调用 `build_ranked_ratings()` — 遍历所有 bot，计算保守评分（r - 2*rd）、置信度、H2H 平均胜率，按 `h2h_avg_wr` 降序排序。

`GET /api/history`：支持 `bots` 过滤和 `resolution` 降采样（full/medium/low）。

`GET /api/experience`（GET/PUT/POST）：经验池 CRUD。PUT 用独占锁写入，POST 追加时先读后写。

`GET /api/daemon/status`：基于 `glicko_ratings.json` 的 mtime 判断守护进程活跃状态。

`GET /api/h2h`：支持按 `bot_name` 过滤的 H2H 对战数据。

#### 3.4.2 Bot 管理（bots.py）

位于 `/home/zzx/project/pok/web/server/routes/bots.py`（114 行），3 个端点。

`GET /api/bots`：扫描 `bots/` 目录（`claude_v*` 前缀 + `.completed` 哨兵），调用 `build_bot_summary()` 计算代码行数、评级信息。支持 `include_graveyard` 参数。

`GET /api/bots/{version}/code/{filename}`：安全检查（filename 必须以 `.py` 结尾且不含 `/` 或 `\`），返回 `PlainTextResponse`。

#### 3.4.3 比赛与回放（matches.py）

位于 `/home/zzx/project/pok/web/server/routes/matches.py`（77 行），5 个端点。

`GET /api/matches/matrix`：调用 `build_match_matrix()` 构建 NxN 胜率矩阵。

`GET /api/matches/commentary/{match_id}`：惰性生成 + 缓存模式 — 先检查 `commentary/` 目录缓存，不存在则读取回放数据调用 `commentary.generate_match_commentary()` 生成并缓存。

#### 3.4.4 日志（logs.py）

位于 `/home/zzx/project/pok/web/server/routes/logs.py`（149 行），6 个端点。

`GET /api/logs/system-events`：支持 type/severity/since 过滤 + limit/offset 分页。

`GET /api/logs/worker-failures`：支持 gen 精确匹配 + role 大小写不敏感子串匹配 + 分页。

安全措施：路径穿越防护（`resolve()` + `is_relative_to()`），文件名校验。

#### 3.4.5 提示词（prompts.py）

位于 `/home/zzx/project/pok/web/server/routes/prompts.py`（137 行），4 个端点。

13 个白名单提示词文件。`POST /api/prompts/{name}/reset` 调用 `git checkout HEAD -- {path}` 恢复到 git 最后提交版本。

### 3.5 缓存策略

位于 `/home/zzx/project/pok/web/server/cache.py`（39 行）。

全局单例 `_CACHE` 字典 + 2 秒 TTL。所有路由模块共享同一缓存字典，通过字符串 key 命名空间隔离（data_stream 用 `ds_` 前缀，其他无前缀）。

`read_locked(path)`：使用 `fcntl.flock(f, fcntl.LOCK_SH)` 共享锁读取，捕获 JSON 解码错误返回 None。

### 3.6 状态管理

位于 `/home/zzx/project/pok/web/server/state.py`（156 行）。

`AppState` 单例，所有属性通过 `threading.RLock` 保护。持久化配置到 `app_config.json`（daemon_enabled/workers/pairs）。

核心方法：
- `try_set_running(bool)` — 原子性 CAS 操作
- `stop_running()` — 原子设 running=False 并取出 Task
- `bootstrap(current_v)` — 初始化版本号和代计数器

---

## 4. 前端架构分析

### 4.1 路由与页面

#### 4.1.1 应用结构

位于 `/home/zzx/project/pok/web/frontend/src/`。

组件嵌套层级：
```
StrictMode → ThemeProvider → AppWrapper(HelmetProvider) → App.tsx →
DataProvider(SSE) → BrowserRouter → ScrollToTop → Routes →
AppLayout(SidebarProvider) → <Outlet/>
```

10 条路由全部嵌套在 `AppLayout` 布局下，共享侧边栏 + 顶部栏。

#### 4.1.2 页面详解

**Overview（总览页，414 行）**

系统主仪表盘。展示 Top 5 Bot 卡片（含评分、H2H 胜率、Sparkline 迷你图）、完整排行榜、流水线状态、429 限速告警横幅。

数据源：8 个 DataProvider hooks + 3 个轮询 REST。紧凑指标条水平排列 4 个核心指标。DaemonToggle 内联开关调用 `controlApi.setConfig()`。Sparkline 用纯 SVG 三点折线图。置信度 4 级颜色编码。

**EvolutionMonitor（进化监控页，596 行）**

实时 LLM 进化过程可视化终端。核心是仿 macOS 终端的对话流窗口，右侧面板提供流水线状态、成本分解、排行榜。

数据源：独立 SSE 连接 `/api/evolution/stream`（10 种事件类型）+ 4 个轮询 REST。

关键交互：
- 角色药丸过滤器：11 种角色独立颜色方案，点击过滤消息
- 消息类型渲染：tool_call → ToolCard（可展开）、thinking → ThinkingBlock（可展开）、claude → 绿色前缀文本
- 消息合并：同类型同角色的连续消息合并
- 500 条消息上限，超出自动裁剪
- 流式光标：工作中靛蓝色闪烁光标

**MatchReplay（对局回放页，266 行）**

扑克对局回放查看器。左侧对局列表，右侧 Canvas 可视化牌桌 + 评论 + 回放控制。

回放状态机：手牌索引 + 步骤索引 + 播放状态。速度选择：0.5x(1500ms)/1x(800ms)/2x(400ms)/4x(200ms)。自动播放到末尾切换下一手。

**RatingTrends（评分趋势页，185 行）**

ApexCharts 折线图展示评分/H2H 胜率趋势。支持 Glicko 评分/H2H 胜率两种视图切换，Glicko 模式下可选 rangeArea 置信带（r +/- 2*rd）。17 色循环数组。

**MatchMatrix（对局矩阵页，197 行）**

ApexCharts 热力图展示 Bot 间 H2H 胜率或对局数。7 级颜色编码（无数据灰, <35%红, 35-45%浅红, 45-55%深灰均势, 55-65%浅蓝, 65-75%蓝, >75%深蓝）。自定义 Tooltip 显示详细胜负平。

**Logs（日志页，462 行）**

5 标签页日志查看器。LLM 对话标签包含完整的日志解析器 `parseConversation(raw)`：将原始文本解析为 7 种 `ConvPart` 类型（prompt/claude/thinking/tool/cycle_end/separator），状态机式行扫描约 100 行。

**ControlPanel（控制面板页，527 行）**

进化系统中央控制台。26 个 MCP 工具的手风琴式分组调用界面。动态表单生成器 `ToolForm`：根据参数类型（int→number, str→text, bool→checkbox, list/dict→textarea）生成控件。双重确认的危险操作（进化重置）。

**BotManager（Bot 管理页，466 行）**

管理所有 Bot 版本。活跃/归档 Bot 卡片列表，每个卡片可展开查看代码、H2H 对战记录。全局操作：淘汰最弱、准备下一代、杂交。BotCard 约 200 行，含懒加载详情、代码查看、H2H 进度条分析。

**ExperiencePool（经验池页，212 行）**

查看/编辑策略经验池（Markdown）。编辑/追加/裁剪/LLM 整合操作。`isEditingRef` 模式避免自动刷新覆盖正在编辑的内容。

**PromptEditor（提示词编辑器页，227 行）**

类 IDE 布局的 LLM 提示词编辑器。左侧 13 个文件列表，右侧 textarea 编辑器。脏检测 + 未保存警告 + 确认保护。

### 4.2 组件体系

#### 4.2.1 共享基础组件（`components/shared/`）

8 个组件，通过 `index.ts` 统一 barrel export：

| 组件 | 功能 | 关键特性 |
|---|---|---|
| Card | 通用容器 | solid/glass/danger 三种变体 |
| CardHeader | 卡片头部 | 标题+副标题+操作区 |
| MetricCard | 指标展示 | 自带 shimmer 加载态，趋势箭头 |
| Badge | 标签/徽章 | 5 种颜色变体 + pulse 动画 |
| StatusDot | 状态指示 | active/idle/error 三色 + 脉冲环 |
| Skeleton | 加载骨架 | Line/Text/Circle/Card 四种形态 |
| SegmentedControl | 分段选择 | iOS 风格，受控组件 |
| EmptyState | 空状态占位 | message + 可选 action slot |

所有组件使用 `cn()`（clsx + tailwind-merge）合并类名，支持 `dark:` 暗色模式。

#### 4.2.2 进化监控组件（`components/evolution/`）

| 组件 | 功能 |
|---|---|
| PipelineStatus + PipelineStepper | 水平步进条 + 可折叠详情面板 |
| ToolCard + ThinkingBlock | 工具调用卡片 + 思考过程展示 |
| CostBreakdown | LLM 成本明细面板 |
| WorkerProgress | Worker 执行进度 |
| icons | 6 个 SVG 图标组件 |

`ToolCard` 的 `formatToolSummary()` 为不同工具生成可读摘要（Bash 显示命令前 120 字符，Read/Edit/Write 显示文件路径）。

#### 4.2.3 日志组件（`components/logs/`）

- `SystemLogTab`：自包含的系统日志浏览面板（API 调用+筛选+分页+展开详情）
- `WorkerFailuresTab`：自包含的 Worker 失败记录面板（动态提取筛选选项）

两个组件结构高度相似（筛选器+卡片列表+展开详情+分页），有抽象为通用列表组件的潜力。

#### 4.2.4 可视化组件

`PokerTable.tsx`（282 行）：800x500 Canvas 画布，绘制完整扑克桌面。包括径向渐变桌面、手牌（正面显示点数+花色，背面菱形图案）、公共牌、底池、行动标签（弃牌灰/过牌蓝/加注黄/全押红）、结算结果。牌编号遵循 engine/judge.py 协议。

### 4.3 数据流

#### 4.3.1 双 SSE 通道

**主数据通道 — DataProvider**

`context/DataProvider.tsx`（103 行）建立 `EventSource("/api/data/stream")`，监听 11 种 SSE 事件类型，通过 `setStore` 合并到单一 `DataStore` 状态对象。暴露 12 个专用 hook。

断线自动 5 秒重连。

**进化监控通道 — useEvolutionSSE**

`api/evolution.ts`（146 行）提供独立 SSE 连接 `/api/evolution/stream`。注意：`useEvolutionSSE` 命名以 `use` 开头但不是 React hook（不调用任何 hook），不符合 React 命名约定。

监听 10 种事件，通过回调 handler 推给页面组件。断线自动 5 秒重连。

#### 4.3.2 API 客户端

三个独立模块：

1. `api/client.ts`（162 行）：主 API 客户端，`fetchJSON<T>(url, signal?)` 使用 `AbortSignal.any` 合并内部超时和外部 signal。7 大领域约 30 个方法。
2. `api/control.ts`（69 行）：控制 API，独立实现了 `fetchJSON`/`extractError`（与 client.ts 代码重复）。
3. `api/evolution.ts`（146 行）：SSE hook + REST 状态获取。

#### 4.3.3 Context 体系

| Context | 职责 | 消费方式 |
|---|---|---|
| ThemeProvider | light/dark 主题切换，localStorage 持久化 | `useTheme()` |
| DataProvider | SSE 实时数据订阅，全局只读数据源 | 12 个专用 hook |
| SidebarProvider | 侧边栏展开/折叠、移动端抽屉 | `useSidebar()` |

嵌套顺序：ThemeProvider（最外层）→ AppWrapper → DataProvider → Router，SidebarProvider 在 AppLayout 内部。

---

## 5. 数据流与同步机制

### 5.1 文件读写模型

#### 5.1.1 写入模式

系统使用三种文件写入模式：

**1. 原子替换（最安全）**

用于：`glicko_ratings.json`, `orchestrator_session.json`, `rate_limit_state.json`, `.daemon_pid`

流程：写入 `.tmp` → `os.fsync()` → `os.replace()`（POSIX 保证原子）

读者永远不会看到半写状态。

**2. fcntl 排他锁写入**

用于：`head_to_head.json`, `bot_stats.json`, `elo_daemon_stats.json`, `match_history.jsonl`, `worker_failures.jsonl`, `system_events.jsonl`, `llm_costs.jsonl`, `pipeline_state.json`

通过 `locked_file()` 上下文管理器：写模式以 `r+` 打开（避免截断后再加锁），获取 `LOCK_EX` 后 truncate+write。

**3. 无锁直接写入**

用于：`app_config.json`, prompt 文件

仅在单进程内操作，依赖 `AppState.RLock` 或文件系统原子性。

#### 5.1.2 读取模式

**1. TTL 缓存读取**（最常用）

`cache.py` 的 `cached_read(key, path)`：全局字典 + 2 秒 TTL + `LOCK_SH` 共享锁。所有路由模块共享同一缓存实例。

**2. 直接加锁读取**

`locked_file(path, "r")` 使用 `LOCK_SH`，不经过缓存。用于需要最新数据的场景（如经验池编辑、pipeline checkpoint）。

**3. 无锁读取**

直接 `path.read_text()`，用于不与其他进程竞争的文件。

#### 5.1.3 数据文件读写关系总表

| 文件 | 写入方 | 写入频率 | 写入模式 | 读取方 |
|---|---|---|---|---|
| `glicko_ratings.json` | elo_daemon | 每 20 局/60s | 原子替换 | data_stream, helpers, web_ui, tool_helpers |
| `head_to_head.json` | elo_daemon | 同上 | LOCK_EX | data_stream, helpers, tool_helpers |
| `bot_stats.json` | elo_daemon | 同上 | LOCK_EX | data_stream, helpers |
| `match_history.jsonl` | elo_daemon | 每局 | LOCK_EX 追加 | data_stream |
| `pipeline_state.json` | tool_pipeline | 每阶段 | LOCK_EX 读-合并-写 | pipeline 路由, orchestrator_session |
| `orchestrator_session.json` | orchestrator_session | 每次工具调用 | 原子替换 | orchestrator, control 路由 |
| `llm_costs.jsonl` | web_ui | 每次 LLM 调用 | LOCK_EX 追加 | web_ui（启动加载历史） |

### 5.2 SSE 推送机制

#### 5.2.1 EventBroadcaster 架构

位于 `/home/zzx/project/pok/web/core/web_ui.py`（292 行）。

扇出广播器 + 环形缓冲区（deque, maxlen=500）。每个客户端有独立的 `asyncio.Queue(maxsize=2000)`。

**跨线程安全**：核心设计难点。`broadcast()` 检测调用线程是否是 Queue 所属事件循环线程：
- 同事件循环：直接 `put_nowait()`
- 跨线程：`loop.call_soon_threadsafe()` 路由
- 无事件循环（测试/CLI）：直接 `put_nowait()`

**新客户端连接**：ring_buffer 中最近 500 条事件重放到新 Queue。

**SSEHandler 限流**：`logging_config.py` 中的日志→SSE 桥接器，每秒最多 10 条事件，防止日志风暴。

#### 5.2.2 SSE 事件类型总表

| 事件名 | 触发源 | Payload 结构 |
|---|---|---|
| `history` | `WebUI.log_history()` | `{msg, status, ts}` |
| `status` | `WebUI.set_status()` | `{msg, is_working, ts}` |
| `io` | `WebUI.log_io()` | `{msg, stream_type, role, ts}` |
| `clear_io` | `WebUI.clear_io()` | `{ts}` |
| `eval_table` | `WebUI.update_eval_table()` | `{rows: [...], ts}` |
| `daemon` | `WebUI.update_daemon_status()` | `{total_matches, total_games, n_bots, ts}` |
| `header` | `WebUI.set_header()` | `{msg, ts}` |
| `cost` | `WebUI.update_cost()` | `{role, cost_usd, input_tokens, output_tokens, gen_total, grand_total, ts}` |
| `metrics` | `WebUI.update_metrics()` | `{current_v, next_v, success_rate, ...}` |
| `tool_call` | `WebUI.emit_tool_call()` | `{tool_name, args, role, ts}` |
| `system_event` | `system_log.log_system_event()` | `{ts, type, severity, message, data?}` |
| `log_event` | `logging_config.SSEHandler` | `{level, logger, msg}` (限流 10/s) |

### 5.3 并发控制

#### 5.3.1 已实现的保护机制

**1. fcntl 文件锁**

- Daemon 写 + 后端读：daemon 用 LOCK_EX 写，后端用 LOCK_SH 读
- 多路由并发读：共享锁互不阻塞
- 写-写互斥：`locked_file("w")` 确保同一时刻只有一个写者

**2. 原子文件替换**

POSIX `os.replace()` 保证读者永远不会看到半写状态。

**3. TTL 缓存**

2 秒 TTL 大幅减少 I/O，代价是最多 2 秒数据延迟。

**4. 内存锁**

| 锁 | 保护对象 | 类型 |
|---|---|---|
| `AppState._lock` | running/task/generation_count | RLock（可重入） |
| `EventBroadcaster._lock` | ring_buffer + client 列表 | Lock |
| `daemon_management._daemon_lock` | daemon_proc 全局变量 | Lock |

**5. asyncio Queue 容量限制**

每个 SSE 客户端 Queue maxsize=2000，满时 `put_nowait` 静默丢弃。

**6. Worker 并发信号量**

`_WORKER_SEMAPHORE`（asyncio.Semaphore, max 3）限制并发 LLM Worker 调用。

#### 5.3.2 完整数据流图

```
┌─────────────────────────────────────────────────────────┐
│                  elo_daemon.py (子进程)                   │
│  ProcessPoolExecutor → mirror_battle → process_result    │
│  周期刷盘: save_cycle() (每20局/60s)                      │
└────────────────────┬────────────────────────────────────┘
                     │ 原子写 / LOCK_EX
                     ▼
┌─────────────────────────────────────────────────────────┐
│               web/core/results/ (文件系统)                │
│  glicko_ratings.json  head_to_head.json  bot_stats.json  │
│  match_history.jsonl  pipeline_state.json  ...           │
└────────────────────┬────────────────────────────────────┘
                     │ LOCK_SH 读 + 2秒TTL缓存
                     ▼
┌─────────────────────────────────────────────────────────┐
│            FastAPI 后端 (cache.py + routes/)              │
│  /api/data/stream (周期SSE)     /api/evolution/stream    │
│  /api/control/* (REST)          /api/bots/* etc.         │
└──────┬─────────────────────────────────┬────────────────┘
       │ SSE (EventSource)               │ REST (fetch)
       ▼                                 ▼
┌─────────────────────────────────────────────────────────┐
│                    React 前端                             │
│  DataProvider ← /api/data/stream (11种事件)               │
│  EvolutionMonitor ← /api/evolution/stream (10种事件)     │
│  各页面 → REST API 按需调用                               │
└─────────────────────────────────────────────────────────┘
```

---

## 6. 关键设计模式

### 6.1 安全降级优先

几乎所有 Agent 失败都返回安全的默认值而非抛异常：
- Critic 失败 → `{score:0, approved:False}`
- Combined Analyst 失败 → 12 字段预填充的 `safe_default`
- Direction Auditor 失败 → `{repetition_detected: False}`
- Performance Verification 失败 → 空字符串

这确保流水线不会因任何单 Agent 失败而中断。

### 6.2 Gate Ledger 架构

`commit_bot` 不重新运行检查，而是验证 checkpoint 中已记录的门控结果。确保"一次通过、全程可信"，避免 LLM 绕过中间步骤直接提交。

### 6.3 选择性重置

Worker 边界违规时仅重置违规文件而非全部回滚，最大化保留合规 Worker 的劳动成果。

### 6.4 三层 Worker 边界防护

(a) Master 计划验证（硬性错误+警告）→ (b) Worker 执行后边界检测（diff 级别）→ (c) Reviewer/Critic 人工审查。

### 6.5 统计预检短路

`combined_analyst.py` 在数据明确时跳过 LLM 调用：
- |delta| < 5 → 停滞
- delta > 20 → 改进中
- RD > 150 → 统计不可靠，退回 LLM

### 6.6 H2H 加权收割

不按 Glicko 评分收割（受初始值影响大），而是按 H2H 平均胜率，并用 `r - 2*rd` 保守评分作为决胜指标。

### 6.7 环形缓冲区 + 客户端独立队列

`EventBroadcaster` 维护 500 条环形缓冲区用于新客户端重放，每个客户端有独立的 `asyncio.Queue` 用于实时推送。解决"后发的先到"问题和历史事件丢失问题。

### 6.8 PreCompact 钩子状态注入

在 LLM 上下文压缩前注入进化关键状态（当前版本、checkpoint 阶段、Master plan），防止上下文压缩导致信息丢失。

### 6.9 原子写入统一协议

所有关键文件写入使用统一的原子写入协议：tmp + fsync + rename。POSIX 保证 `os.replace()` 的原子性。

### 6.10 Session 保留策略

只有自然完成的 cycle 清除 session 文件。超时、取消、错误、529/429 均保留，支持崩溃恢复。

### 6.11 429 配额智能阻塞

`rate_limiter.py`（216 行）解析 429 响应中的重置时间，持久化到 `rate_limit_state.json`，阻塞所有后续 LLM 调用直到配额恢复。前端通过 SSE 接收限速状态显示告警横幅。

---

## 7. 潜在改进点

### 7.1 代码重复

**问题描述**：`api/client.ts` 和 `api/control.ts` 各自实现了几乎相同的 `fetchJSON` 和 `extractError` 函数。

**影响范围**：`/home/zzx/project/pok/web/frontend/src/api/client.ts`（162 行）和 `/home/zzx/project/pok/web/frontend/src/api/control.ts`（69 行）。

**建议**：抽取共享的 HTTP 工具函数到 `api/http.ts`，两个客户端模块引用。

### 7.2 命名不规范

**问题描述**：`useEvolutionSSE` 以 `use` 开头但不是 React hook（不调用任何 hook），违反 React 命名约定。

**影响范围**：`/home/zzx/project/pok/web/frontend/src/api/evolution.ts`。

**建议**：重命名为 `createEvolutionSSE` 或 `connectEvolutionSSE`。

### 7.3 SSE 事件解析错误静默吞掉

**问题描述**：`DataProvider` 中 SSE 事件处理函数的 `try { handler(JSON.parse(e.data)); } catch { /* ignore */ }` 静默吞掉所有解析错误，可能掩盖问题。

**影响范围**：`/home/zzx/project/pok/web/frontend/src/context/DataProvider.tsx`。

**建议**：至少在开发模式下 `console.warn` 输出解析错误。

### 7.4 缓存键分裂

**问题描述**：`data_stream.py` 用不同缓存键（`ds_ratings`, `ds_ratings_bots`, `ds_ratings_matrix`）读同一个 `glicko_ratings.json`。各键独立过期，同一 3 秒周期内不同事件可能携带不同版本的评级数据。

**影响范围**：`/home/zzx/project/pok/web/server/routes/data_stream.py`。

**建议**：对同一文件使用单一缓存键，或在每个 tick 开始时统一加载一次所有需要的数据。

### 7.5 app_config.json 无 fcntl 锁

**问题描述**：`state.py` 的 `_save_config()` 用普通 `write_text()` 写入，没有 fcntl 锁。同进程内 `RLock` 保护足够，但跨进程（CLI 和 web 同时运行）可能损坏。

**影响范围**：`/home/zzx/project/pok/web/server/state.py`。

**建议**：使用 `locked_file()` 或原子写入协议。

### 7.6 reset.py 代码缺陷

**问题描述**：第 174 行有一个孤立的文档字符串和代码块，缺少函数定义头 `def _delete_version_log_dirs(keep_versions):`。第 269 行调用了该函数，运行时会触发 `NameError`。

**影响范围**：`/home/zzx/project/pok/web/core/reset.py` 第 174-186 行。

**建议**：添加缺失的函数定义头。

### 7.7 日志组件结构重复

**问题描述**：`SystemLogTab` 和 `WorkerFailuresTab` 结构高度相似（筛选器+卡片列表+展开详情+分页），有抽象为通用列表组件的潜力。

**影响范围**：`/home/zzx/project/pok/web/frontend/src/components/logs/`。

**建议**：抽取通用的 `FilterableList<T>` 组件，两个标签页作为配置实例。

### 7.8 StatusDot 命名冲突

**问题描述**：`shared/StatusDot.tsx`（CSS div 实现）和 `evolution/icons.tsx` 中的 `StatusDot`（SVG 实现）同名但不同实现，可能造成导入混淆。

**影响范围**：两个文件均可通过相对路径导入。

**建议**：将 `evolution/icons.tsx` 中的 `StatusDot` 重命名为 `SvgStatusDot` 或直接内联使用。

### 7.9 前端多源轮询重叠

**问题描述**：Overview、ControlPanel、EvolutionMonitor 三个页面各自维护独立的 `setInterval` 轮询，且部分数据源重叠（如 pipeline checkpoint 被 3 个页面同时轮询）。

**影响范围**：多个页面组件中的 `useEffect` + `setInterval` 模式。

**建议**：将高频共享数据（pipeline checkpoint、控制状态）整合到 DataProvider SSE 中统一推送，减少重复请求。

### 7.10 LLM 输出解析鲁棒性

**问题描述**：`parse_json_output` 的花括号平衡匹配在 JSON 字符串值中嵌入代码块的边界情况下可能失败。虽然三级策略提供了回退，但第一级的"从最后一个 json 代码块开始"策略在 LLM 输出多个代码块时可能选错。

**影响范围**：`/home/zzx/project/pok/web/core/llm_query.py`。

**建议**：增加对解析失败的日志记录和统计，监控实际失败率。考虑在 Agent prompt 中更明确地要求 JSON 输出格式。

### 7.11 Daemon 内存状态延迟

**问题描述**：Daemon 在内存中持续更新 ratings/h2h/bot_stats，仅每 20 局或 60 秒刷盘。后端读到的是上一次刷盘的状态而非实时。若 Daemon 崩溃且 `finally` 块未执行，最多丢失 20 局数据。

**影响范围**：`/home/zzx/project/pok/web/core/elo_daemon.py` 的 `save_cycle()`。

**建议**：这是有意设计的性能优化，当前影响可接受。若需更高数据可靠性，可考虑写前日志（WAL）模式。

### 7.12 错误处理模式不一致

**问题描述**：后端路由的错误处理风格不一致：部分端点静默忽略错误（如 `stop_evolution` 的 daemon 停止失败），部分返回 500，部分返回 4xx。缺乏统一的错误处理中间件。

**影响范围**：`/home/zzx/project/pok/web/server/routes/` 全部路由模块。

**建议**：引入 FastAPI exception handler 统一错误响应格式，对静默忽略的场景添加日志记录。
