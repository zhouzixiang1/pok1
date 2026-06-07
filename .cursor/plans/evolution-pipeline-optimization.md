# LLM 进化系统 Pipeline 优化计划

## 背景

基于 338 次 LLM 调用（$187.73 总成本）、12 条 Worker 失败记录、~20 次超时代次的深入分析，本计划聚焦**提高每代进化的成功率**，而非为失败做兜底。

P0 项已全部完成（Direction Auditor 增强、intra_gen_attempts 持久化、Worker 串行执行）。本文档覆盖 P1 阶段。

---

## P1-1：合并停滞分析 + 性能验证为统一分析师

### 问题
- `_analyze_stagnation()`（stagnation_analyzer.py）和 `_run_performance_verification()`（agent_review.py）共享 60-70% 输入数据：都读 `rating_history.jsonl`、`head_to_head.json`、`glicko_ratings.json`
- 输出重叠：`is_stagnant` ↔ `trend: "stagnant"`，`recommendation: "branch"` ↔ `diversity_needed: true`
- 两次 LLM 调用（$0.08 + $0.06）可以合并为一次

### 方案
1. 新建 `web/core/prompts/combined_analyst.md` — 统一 prompt，输出合并 JSON：
   ```json
   {
     "is_stagnant": true/false,
     "confidence": "high/medium/low",
     "trend": "improving|stagnant|declining",
     "diversity_needed": true/false,
     "recommendation": "continue|branch|crossover",
     "branch_from": "claude_vN" 或 null,
     "verified_improvements": ["..."],
     "persistent_weaknesses": ["..."],
     "reason": "..."
   }
   ```
2. 新建 `web/core/combined_analyst.py`，包含 `_run_combined_analysis(current_v, active_bots, ratings, ui, prev_critic_info)` 函数
3. 修改 `web/core/generation_scheduler.py`：
   - 将 `asyncio.gather(_analyze_stagnation, _analyze_recent_matches, _run_performance_verification)` 改为 `asyncio.gather(_run_combined_analysis, _analyze_recent_matches)`
   - 更新 `_decide_strategy()` 消费合并后的 JSON 字段
4. 保留 `stagnation_analyzer.py` 和 `agent_review.py:_run_performance_verification` 作为 fallback

### 涉及文件
- `web/core/combined_analyst.py`（新建）
- `web/core/prompts/combined_analyst.md`（新建）
- `web/core/generation_scheduler.py`（修改 `prepare_generation` 和 `_decide_strategy`）

### 验证
- `pytest tests/ -v` 全部通过
- `GenerationContext` 的 `stagnation_info` 和 `performance_verification` 字段正常填充

---

## P1-2：统计检验前置减少 LLM 调用

### 问题
每代都调用 LLM 判断 rating 是否停滞（$0.08/次），但核心问题（"rating 近期是否平坦？"）可以用统计检验回答。LLM 的价值在于语义理解（"为什么停滞"），而非数值趋势判断。

### 方案
1. 在 `web/core/combined_analyst.py` 中新增 `_statistical_stagnation_check()` 函数：
   - 读取 `rating_history.jsonl` 最近 10 个周期
   - 计算滑动窗口均值（最近 3 个 vs 之前 3 个周期）
   - delta < 5 rating 分 → `is_stagnant=true, confidence="high"`（跳过 LLM）
   - delta > 20 rating 分 → `is_stagnant=false, confidence="high"`（跳过 LLM）
   - delta 在 [5, 20] → 调用 LLM 做深度分析
2. 在 `_run_combined_analysis()` 中先调用统计检验，结果明确时直接返回，模糊时才调 LLM

### 涉及文件
- `web/core/combined_analyst.py`（新增函数，依赖 P1-1）

### 验证
- 构造合成 rating_history 数据测试
- 确认明确的改善/停滞场景跳过 LLM 调用

---

## P1-3：强化 Worker Prompt 质量

### 问题
从 12 条失败记录分析：
- **33% 零改动** — Worker 不使用 Edit 工具修改文件
- **17% 角色越界** — Architect 改常量（应由 Tuner 完成）
- **8% 反向执行** — Tuner 把常量调反方向

根因是 Worker prompt 的指令不够精确。

### 方案
在 `web/core/prompts/worker_prompt.md` 中增加：

```
<mandatory_actions>
1. 你必须使用 Edit 工具修改文件。仅读取文件不算完成任务。
2. 每次编辑后，用 Read 工具验证修改已生效。
3. 完成前，对比目标文件与父版本确认变更存在。
</mandatory_actions>

<for_hyperparameter_tuner>
每个修改必须列出：
- 文件: {filename}, 行 {N}: {常量名} = {旧值} → {新值}
- 理由: {为什么是这个具体值}
不列出格式的修改视为无效。
</for_hyperparameter_tuner>

<for_algorithmic_logic_architect>
禁止修改任何数值常量（阈值、比率、边界值、系数）。
如果需要常量取不同值，新增参数或从现有逻辑推导——不得直接编辑已有的数字字面量。
</for_algorithmic_logic_architect>
```

### 涉及文件
- `web/core/prompts/worker_prompt.md`

### 验证
- 审查 prompt 内容确保边界约束清晰
- `pytest tests/ -v`

---

## P1-4：Worker 零改动前置拦截

### 问题
当前零改动在 Quality Gates 阶段才被发现（`code_changed: false`），浪费了后续 Reviewer（$0.53）和 Critic（$0.48）的 LLM 调用。

### 方案
1. 在 `web/core/tool_planning.py` 的 `execute_workers()` 中，`_execute_workers()` 返回 `success=True` 后：
   - 新增 `_check_code_actually_changed(next_v, source_v)` 函数
   - 比较源和目标目录所有 .py 文件
   - 如果完全相同，直接返回 `{success: false, reason: "zero_code_changes"}`
   - **不推进** checkpoint 到 `workers_done`
2. 此检查已在 `run_quality_gates` 中存在（第 60-69 行），提前到 Worker 完成后可节省后续 LLM 调用

### 涉及文件
- `web/core/tool_planning.py`（在 `execute_workers` 中添加前置检查）

### 验证
- 用相同的 bot 目录测试 → 应快速失败
- `pytest tests/ -v`

---

## P1-5：明确 Reviewer 和 Critic 的职责边界

### 问题
Reviewer 和 Critic 存在职责重叠：
- Reviewer 检查代码质量、大小、边界，但有时也评策略价值
- Critic 检查策略质量，但有时也提代码问题（死代码、文件大小）
- 两者都打 1-10 分，可能产生矛盾的评价

### 方案
保持两个角色不变，但收窄各自关注范围：

**Reviewer — 代码质量门禁**（仅关注格式/边界/正确性）：
- 更新 `web/core/prompts/reviewer_prompt.md`：
  - 移除策略评估要求
  - 聚焦：角色边界合规、文件大小限制、代码正确性、无死代码
  - 评分改为通过/不通过（不再 1-10），通过 = 无边界违规、无死代码、文件大小合规

**Critic — 策略质量门禁**（仅关注策略/创新/影响）：
- 更新 `web/core/prompts/critic_prompt.md`：
  - 移除代码层面关注（文件大小、死代码、边界违规）
  - 聚焦：策略方向、预期行为变化、测量计划、局部最优风险
  - 保持 1-10 评分，≥6 通过

### 涉及文件
- `web/core/prompts/reviewer_prompt.md`（收窄到代码质量）
- `web/core/prompts/critic_prompt.md`（收窄到策略评估）

### 验证
- 审查两个 prompt 确认无重叠
- `pytest tests/ -v`

---

## P1-6：减少 ToolSearch 调用

### 问题
Orchestrator LLM 调用 ToolSearch ~50 次（每次消耗 prompt tokens），表明 LLM 对可用工具集不够熟悉。

### 方案
1. 在 `web/core/orchestrator_context.py` 的 `_build_context()` 中注入工具清单：
   ```
   可用工具（按精确名称调用）：
   - prepare_next_gen(source_v, next_v) — 复制源 bot
   - run_direction_audit(source_v, next_v) — 检测重复方向
   - run_master(source_v, next_v, ...) — 规划 worker 任务
   - execute_workers(tasks, next_v, source_v, ...) — 修改 bot 代码
   - run_quality_gates(version, source_v) — 编译+冒烟+决策测试
   - run_review(version, source_v, plan) — 代码质量审查
   - run_critic(version, source_v, plan, ...) — 策略评估
   - run_precommit_eval(version, source_v) — 回归镜像对战
   - commit_bot(version, source_v, ...) — git commit + tag
   - run_archivist(version, source_v) — 归档 + 清理
   - run_crossover(parent_a, parent_b, target_v) — 合并两个 bot
   ```
2. `orchestrator.md` 的 `<state_machine>` 表格已有这些信息，但需要在 `_build_context` 输出中也包含（恢复的 session 可能丢失了表格信息）

### 涉及文件
- `web/core/orchestrator_context.py`（添加工具清单注入）

### 验证
- 确认恢复的 session 上下文中有工具清单
- `pytest tests/ -v`

---

## 执行顺序

```
阶段 B-1（简单，独立）:
  P1-3: 强化 Worker Prompt        ~30 分钟
  P1-4: 零改动前置拦截             ~30 分钟
  P1-6: ToolSearch 优化            ~20 分钟

阶段 B-2（有依赖关系）:
  P1-1: 统一分析师                 ~1 小时
  P1-2: 统计停滞检验               ~30 分钟（依赖 P1-1）

阶段 B-3（prompt 调优）:
  P1-5: Reviewer/Critic 边界       ~30 分钟

总计预估: ~3.5 小时
```

## 文件清单

| 文件 | 涉及项 | 操作 |
|------|--------|------|
| `web/core/combined_analyst.py` | P1-1, P1-2 | 新建 |
| `web/core/prompts/combined_analyst.md` | P1-1 | 新建 |
| `web/core/generation_scheduler.py` | P1-1 | 修改 `prepare_generation` + `_decide_strategy` |
| `web/core/prompts/worker_prompt.md` | P1-3 | 增加强制操作指引 |
| `web/core/agent_workers.py` | P1-3 | 加强零改动检测 |
| `web/core/tool_planning.py` | P1-4 | 增加零改动前置检查 |
| `web/core/prompts/reviewer_prompt.md` | P1-5 | 收窄到代码质量 |
| `web/core/prompts/critic_prompt.md` | P1-5 | 收窄到策略评估 |
| `web/core/orchestrator_context.py` | P1-6 | 增加工具清单注入 |

## 测试策略

每完成一个 P1 项：
1. `cd web && python -m pytest tests/ -v` — 329 个测试全部通过
2. 检查修改模块无导入错误
3. 验证无新增 linter 警告

全部 P1 完成后：
1. 完整测试套件运行
2. 验证 `pipeline_state.json` schema 兼容性
3. 验证 `GenerationContext` dataclass 正常工作
