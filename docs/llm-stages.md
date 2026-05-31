# LLM 多阶段使用说明

本文档整理了 `web/core/` 中所有 LLM 调用的阶段、角色、工具权限和调用方式。

---

## 总览

系统使用两种 LLM 调用模式：

| 模式 | 入口 | 说明 |
|---|---|---|
| **MCP Tool Server** | `orchestrator.py` | 编排器作为 Claude Agent 运行，通过 MCP 工具驱动整个流水线 |
| **Direct `run_claude_query()`** | `evolution_infra.py` | 子代理直接调用 SDK，用于 Master / Workers / Reviewer / Critic / Analysts |

所有子代理统一使用 `claude_agent_sdk`，模型默认为 **Sonnet**。编排器也使用 **Sonnet** (`orchestrator.py:239`)。

---

## 1. 编排器 (Orchestrator)

- **文件**: `orchestrator.py`
- **模型**: Sonnet
- **调用方式**: MCP Tool Server — `claude_query()` + `ClaudeAgentOptions`
- **可用工具**: 通过 MCP Server 暴露的 `evolution` 工具集（来自 `tool_pipeline.py` + `tool_status.py`）
- **特殊机制**:
  - Session 持久化 (`orchestrator_session.json`) 支持崩溃恢复
  - PreCompact Hook 在 LLM 上下文压缩时注入流水线状态
  - 连续 5 次零花费循环时自动退避重试
- **职责**: 自主决定调用哪些工具、以什么顺序驱动进化流水线

### 编排器可调用的 MCP 工具列表

| 工具名 | 所属模块 | 是否调用 LLM | 说明 |
|---|---|---|---|
| `prepare_next_gen` | `tool_pipeline.py` | 否 | 复制源 bot 目录，写入 `prepared` 检查点 |
| `run_master` | `tool_pipeline.py` | **是** | 调用 Master Architect 分析并生成任务计划 |
| `execute_workers` | `tool_pipeline.py` | **是** | 调用 Worker LLM 并行修改 bot 代码 |
| `run_quality_gates` | `tool_pipeline.py` | 否 | 自动化检查：编译、冒烟测试、决策测试、文件大小 |
| `run_review` | `tool_pipeline.py` | **是** | 调用 Code Reviewer LLM 评审 diff |
| `run_critic` | `tool_pipeline.py` | **是** | 调用 Strategy Critic LLM 评估策略质量 |
| `run_precommit_eval` | `tool_pipeline.py` | 否 | 提交前镜像对战回归测试 |
| `commit_bot` | `tool_pipeline.py` | 否 | Git commit + tag |
| `run_crossover` | `tool_pipeline.py` | **是** | 两精英 bot 交叉产生子代 |
| `run_inline_eval` | `tool_pipeline.py` | 否 | 无守护进程时在线对战评估 |
| `get_status` | `tool_status.py` | 否 | 查询系统当前状态 |
| `get_bot_info` | `tool_status.py` | 否 | 查询单个 bot 详情 |
| `get_match_history` | `tool_status.py` | 否 | 查询对战历史 |
| `run_match_analysis` | `tool_status.py` | **是** | 分析近期对战录像 |
| `get_h2h` | `tool_status.py` | 否 | 查询 H2H 对战数据 |
| `get_bot_stats` | `tool_status.py` | 否 | 查询 bot 统计 |
| `start_daemon` | `tool_status.py` | 否 | 启动 ELO 守护进程 |
| `stop_daemon` | `tool_status.py` | 否 | 停止守护进程 |
| `wait_for_eval` | `tool_status.py` | 否 | 等待守护进程完成评估 |
| `reap_weakest` | `tool_status.py` | 否 | 淘汰最弱 bot |
| `cleanup_incomplete` | `tool_status.py` | 否 | 清理未完成目录 |
| `trim_experience` | `tool_status.py` | 否 | 裁剪经验池 |
| `seed_initial_bots` | `tool_status.py` | 否 | 初始化种子 bot |
| `consolidate_experience` | `tool_status.py` | **是** | LLM 去重合并经验池 |
| `analyze_stagnation` | `tool_status.py` | **是** | LLM 分析停滞趋势 |
| `run_performance_verification` | `tool_status.py` | **是** | LLM 综合性能分析 |

---

## 2. Master Architect（主架构师）

- **文件**: `agent_master.py` → `_run_master_analysis()`
- **模型**: Sonnet（默认）
- **工具**: `Bash`, `Read`
- **Prompt**: `prompts/master_prompt.md`
- **重试**: 最多 3 次 (`MAX_MASTER_RETRIES`)
- **输入**: 停滞信息、对战分析、性能验证、rating 历史、H2H 数据、经验池
- **输出**: JSON 任务计划，包含 2 个 worker 分配（Algorithmic Logic Architect + Hyperparameter Tuner）
- **调用者**: `run_master` MCP 工具

---

## 3. Workers（编码执行者）

- **文件**: `agent_workers.py` → `_run_single_worker()` / `_execute_workers()`
- **模型**: Sonnet（默认）
- **工具**: `Bash`, `Read`, `Edit`
- **Prompt**: `prompts/worker_prompt.md`
- **并发**: 最多 3 个并行 (`MAX_PARALLEL_WORKERS`)，信号量控制
- **重试**: 每个 worker 最多 4 次 (`MAX_WORKER_RETRIES`)
- **超时**: 1000 秒 (`WORKER_TIMEOUT`)，超时后重试并简化任务
- **失败记忆**: 失败记录写入 `worker_failures.jsonl`，注入后续 worker prompt
- **质量自检**: 每次重试后自动执行编译检查和冒烟测试
- **调用者**: `execute_workers` MCP 工具
- **并行→串行回退**: 并行失败时回退到串行模式

---

## 4. Code Reviewer（代码审查员）

- **文件**: `tool_pipeline.py` → `run_review()` → 调用 `run_claude_query()`
- **模型**: Sonnet（默认）
- **工具**: `Bash`, `Read`
- **Prompt**: `prompts/reviewer_prompt.md`
- **输出**: JSON（`approved`, `quality_score`, `feedback`, `risk_areas`）
- **前置条件**: 必须通过 quality gates
- **调用者**: `run_review` MCP 工具

---

## 5. Strategy Critic（策略评审员）

- **文件**: `agent_review.py` → `_run_critic()`
- **模型**: Sonnet（默认）
- **工具**: `Bash`, `Read`
- **Prompt**: `prompts/critic_prompt.md`
- **输出**: JSON（`score` 1-10, `approved`, `strategic_assessment`, `feedback`, `local_optima_warning`）
- **通过阈值**: score ≥ 6
- **前置条件**: 必须通过 quality gates + reviewer 审批
- **调用者**: `run_critic` MCP 工具

---

## 6. Stagnation Analyst（停滞分析师）

- **文件**: `agent_master.py` → `_analyze_stagnation()`
- **模型**: Sonnet（默认）
- **工具**: 无（纯 JSON 输出）
- **输入**: Rating 历史趋势、Top 5 bot 的 H2H 胜率
- **输出**: JSON（`is_stagnant`, `confidence`, `recommendation`, `branch_from`, `reason`）
- **调用者**: `analyze_stagnation` MCP 工具

---

## 7. Match Analyst（对战分析师）

- **文件**: `agent_master.py` → `_analyze_recent_matches()`
- **模型**: Sonnet（默认）
- **工具**: 无（纯 JSON 输出）
- **输入**: 近期失败对局和险胜对局的 replay 摘要（最多 8+4 场）
- **输出**: JSON（`weaknesses`, `street_weaknesses`, `patterns`, `working`, `recommendation`）
- **调用者**: `run_match_analysis` MCP 工具

---

## 8. Performance Analyst（性能分析师）

- **文件**: `agent_review.py` → `_run_performance_verification()`
- **模型**: Sonnet（默认）
- **工具**: 无（纯 JSON 输出）
- **输入**: 10 个周期的 rating 历史、总体胜率、H2H 数据、Top 5 活跃 bot
- **输出**: JSON（`trend`, `verified_improvements`, `persistent_weaknesses`, `diversity_needed`, `suggestion`）
- **调用者**: `run_performance_verification` MCP 工具

---

## 9. Experience Consolidator（经验池整合器）

- **文件**: `agent_master.py` → `_consolidate_experience_pool()`
- **模型**: Sonnet（默认）
- **工具**: 无（纯文本输出，由代码写回文件）
- **触发**: 每 3 代运行一次
- **输出**: 合并去重后的 markdown 文本（固定分类头：OPPONENT_MODELING / POSTFLOP_STRATEGY 等）
- **调用者**: `consolidate_experience` MCP 工具

---

## 10. Crossover Agent（交叉代理）

- **文件**: `agent_review.py` → `_run_crossover()`
- **模型**: Sonnet（默认）
- **工具**: `Bash`, `Read`, `Edit`
- **Prompt**: `prompts/crossover_prompt.md`
- **重试**: 最多 3 次 (`MAX_CROSSOVER_RETRIES`)
- **功能**: 两个精英 bot 交叉产生子代，带编译和冒烟测试自检
- **调用者**: `run_crossover` MCP 工具

---

## 单代进化流水线完整流程

```
┌─────────────────────────────────────────────────────────────────┐
│                     Orchestrator (Sonnet)                       │
│              MCP Tool Server — 自主决定调用顺序                   │
└─────────────────────┬───────────────────────────────────────────┘
                      │
                      ▼
    ┌─────────────────────────────────┐
    │   prepare_next_gen (无 LLM)      │  复制 bot 目录
    └────────────┬────────────────────┘
                 │
                 ▼
    ┌─────────────────────────────────┐
    │   run_master (LLM: Master)       │  分析 + 生成 2 个 worker 任务
    │   工具: Bash, Read               │
    └────────────┬────────────────────┘
                 │
                 ▼
    ┌─────────────────────────────────┐
    │   execute_workers (LLM: Workers) │  并行执行（≤3），修改代码
    │   工具: Bash, Read, Edit         │  最多 4 次重试/worker
    │   超时: 1000s                    │
    └────────────┬────────────────────┘
                 │
                 ▼
    ┌─────────────────────────────────┐
    │   run_quality_gates (无 LLM)     │  编译 / 冒烟 / 决策测试 / 大小
    └────────────┬────────────────────┘
                 │
                 ▼
    ┌─────────────────────────────────┐
    │   run_review (LLM: Reviewer)     │  代码审查，评分 1-10
    │   工具: Bash, Read               │
    └────────────┬────────────────────┘
                 │
                 ▼
    ┌─────────────────────────────────┐
    │   run_critic (LLM: Critic)       │  策略评审，≥6 通过
    │   工具: Bash, Read               │  失败可重试 workers（≤2 次）
    └────────────┬────────────────────┘
                 │
                 ▼
    ┌─────────────────────────────────┐
    │   run_precommit_eval (无 LLM)    │  镜像对战回归测试
    └────────────┬────────────────────┘
                 │
                 ▼
    ┌─────────────────────────────────┐
    │   commit_bot (无 LLM)            │  Git commit + tag
    └─────────────────────────────────┘
```

---

## 辅助分析阶段（按需调用）

```
┌──────────────────────┐
│ analyze_stagnation    │  停滞分析 → 无工具，JSON 输出
│ (LLM, 无工具)         │  判断是否真正停滞，建议 branch/crossover
└──────────────────────┘

┌──────────────────────┐
│ run_match_analysis    │  对战分析 → 无工具，JSON 输出
│ (LLM, 无工具)         │  从 replay 摘要提取弱点和建议
└──────────────────────┘

┌──────────────────────┐
│ run_performance_      │  性能分析 → 无工具，JSON 输出
│ verification (LLM)    │  综合 rating/wr/H2H 趋势
└──────────────────────┘

┌──────────────────────┐
│ consolidate_          │  经验池整合 → 无工具，文本输出
│ experience (LLM)      │  每 3 代去重合并
└──────────────────────┘
```

---

## 各阶段工具权限汇总

| 阶段 | 工具 | 输出格式 |
|---|---|---|
| Orchestrator | MCP tools | 工具调用序列 |
| Master | Bash, Read | JSON（任务计划） |
| Worker | Bash, Read, Edit | 代码修改 |
| Reviewer | Bash, Read | JSON（评分 + 反馈） |
| Critic | Bash, Read | JSON（评分 + 策略评估） |
| Crossover | Bash, Read, Edit | 代码修改 |
| Stagnation Analyst | 无 | JSON |
| Match Analyst | 无 | JSON |
| Performance Analyst | 无 | JSON |
| Experience Consolidator | 无 | Markdown 文本 |

---

## 关键约束

- **所有 LLM 调用默认使用 Sonnet 模型**
- **API 限流 (529)**: 自动指数退避重试（30s → 60s → 120s）
- **Prompt 大小限制**: 最大 700K 字符 (`MAX_PROMPT_CHARS`)，超限时自动压缩上下文文件
- **子代理统一屏蔽**: `_BLOCKED_MCP_TOOLS` 屏蔽外部 MCP 工具（web-reader、web-search、zread）
- **角色边界**: Worker 角色受 prompt + reviewer 约束 — Logic Architect 不能调常数，Hyperparameter Tuner 不能加函数
- **Gate Ledger**: Pipeline checkpoint 强制阶段顺序 — 后续阶段验证前置 gates 是否通过
