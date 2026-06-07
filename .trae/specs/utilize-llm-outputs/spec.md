# LLM Agent 输出全链路利用改进 Spec

## Why
当前进化系统中多个 LLM Agent 产出的字段被记录后从未被下游消费（"死数据"），导致约 42%-52% 的 LLM 费用被浪费。Critic 产出的 `evidence`/`strategic_assessment`、Reviewer 产出的 `risk_areas`/`change_summary`、以及 Direction Auditor 的 `exhausted_directions` 都是高质量但被丢弃的信息。将这些数据注入正确的消费节点，可以显著提升进化迭代的策略质量和连贯性。

## What Changes

### 核心改进（高价值，打通断裂数据流）

1. **Critic `strategic_assessment` + `local_optima_warning` 注入 Stagnation Analyzer**
   - 将 Critic 的战略评估和局部最优警告从 checkpoint 中提取，作为 Stagnation Analyzer 的额外输入
   - 让停滞检测不仅依赖独立的 rating 趋势分析，还能利用 Critic 的实时策略洞察

2. **Critic `evidence` 字段提取并存入 experience_pool**
   - Critic LLM 产出的 H2H 弱点、experience pool 引用、diff 引用目前完全被丢弃
   - 提取后追加到 `experience_pool.md` 的 `RECENT_LESSONS` 区段

3. **Reviewer `change_summary` + `risk_areas` 注入 Archivist**
   - Archivist 当前仅使用 `experience_updates` 和 `strategic_advice`
   - 将 Reviewer 的变更摘要和风险区域注入 Archivist prompt，提升归档分析质量

4. **Direction Auditor `exhausted_directions` 传入 Consolidator**
   - 当前 `experience_archivist.py` 第 42 行硬编码 `exhausted_directions: ""`
   - 从 pipeline checkpoint 或 Direction Auditor 结果中获取实际值

5. **Critic `prev_critic` 正确持久化以支持重试上下文**
   - 当前 `_record_gate` 未保存 `prev_critic` 字段，导致 Critic 重试时丢失前次反馈
   - 修复 gate 记录逻辑

### 辅助改进（中价值，消除数据浪费）

6. **Master `analysis` 从 checkpoint 直接读取（而非日志正则）**
   - Direction Auditor 当前从 `master_io.txt` 日志文件用正则提取 analysis
   - 改为从 `pipeline_state.json` 的 `master_plan.analysis` 字段直接读取

7. **Stagnation `confidence` 和 `recommendation` 影响 `_decide_strategy`**
   - 当前 `_decide_strategy` 只检查 `is_stagnant` 布尔值
   - 增加 `confidence == "medium"` 时的保守策略和 `recommendation` 的精确匹配

8. **Worker 零变更失败的结构化记录**
   - 当前零变更的文件列表只存在 error 字符串中
   - 添加 `failure_type` 字段到 `worker_failures.jsonl` 记录

## Impact
- Affected specs: 进化管线的策略质量、费用效率、数据连贯性
- Affected code: `agent_review.py`, `tool_gates.py`, `tool_commit.py`, `stagnation_analyzer.py`, `direction_auditor.py`, `experience_archivist.py`, `generation_scheduler.py`, `tool_helpers.py`, `agent_workers.py`

## ADDED Requirements

### Requirement: Critic 输出注入 Stagnation 分析
系统 SHALL 在 Stagnation Analyzer 调用前，从最近一代的 pipeline checkpoint 中提取 Critic gate 的 `strategic_assessment` 和 `local_optima_warning` 字段，将其注入 Stagnation Analyzer 的 prompt 上下文。

#### Scenario: Critic 检测到局部最优
- **WHEN** 上一代 Critic 标记 `local_optima_warning: true`
- **THEN** Stagnation Analyzer 收到此信号并纳入停滞判断

#### Scenario: 无前代 Critic 结果
- **WHEN** 这是第一代或 checkpoint 中无 Critic gate
- **THEN** 注入空字符串，不影响 Stagnation Analyzer 独立判断

### Requirement: Critic evidence 存入 experience_pool
系统 SHALL 在 Critic gate 完成后，从 Critic 输出中提取 `evidence` 对象（包含 `h2h_weaknesses`、`experience_pool_refs`、`diff_refs`），将其格式化并追加到 `experience_pool.md` 的 `RECENT_LESSONS` 区段。

#### Scenario: Critic 产出含 evidence
- **WHEN** Critic 返回的 JSON 中包含非空 `evidence` 字段
- **THEN** evidence 被格式化为 `- **v{N} Critic 证据**: {evidence_summary}` 写入 experience_pool

#### Scenario: Critic 无 evidence
- **WHEN** evidence 为空或缺失
- **THEN** 跳过写入，不影响后续流程

### Requirement: Reviewer 输出注入 Archivist
系统 SHALL 在 Archivist 调用时，从 pipeline checkpoint 的 `gate_results.review` 中提取 `change_summary` 和 `risk_areas`，注入 Archivist 的 prompt 上下文。

#### Scenario: 有 Reviewer 输出
- **WHEN** checkpoint 中存在 review gate 且包含 `change_summary`
- **THEN** Archivist LLM 在 prompt 中看到变更摘要和风险区域

### Requirement: exhausted_directions 传入 Consolidator
系统 SHALL 在 Experience Consolidator 调用时，从 pipeline checkpoint 的 `direction_audit` 字段中读取 `exhausted_directions` 列表，替换当前硬编码的空字符串。

#### Scenario: 有已耗尽方向
- **WHEN** Direction Auditor 检测到 `exhausted_directions` 非空
- **THEN** Consolidator prompt 中显示这些方向并标记为 `[POSSIBLY EXHAUSTED]`

### Requirement: prev_critic 正确持久化
系统 SHALL 在 `_record_gate` 写入 critic gate 时，将前一次的 critic 结果保存到 `prev_critic` 字段，确保 Critic 重试时能读取前次反馈。

#### Scenario: Critic 重试
- **WHEN** Critic 未通过（score < 6）且进入重试
- **THEN** 下次 Critic 调用能通过 `prev_critic_result` 参数获取前次的 score 和 feedback

### Requirement: Master analysis 从 checkpoint 直接读取
系统 SHALL 在 Direction Auditor 构建 generation_history 时，优先从 `pipeline_state.json` 的 `master_plan.analysis` 读取，仅在 checkpoint 不可用时回退到日志正则提取。

#### Scenario: Checkpoint 可用
- **WHEN** 对应版本的 pipeline checkpoint 存在且包含 master_plan
- **THEN** 直接使用 `master_plan.analysis` 字段值

### Requirement: Stagnation confidence 影响策略决策
系统 SHALL 在 `_decide_strategy` 中使用 Stagnation Analyzer 的 `confidence` 字段：`confidence == "low"` 时不触发 crossover（即使 `is_stagnant=true`）。

#### Scenario: 低置信度停滞
- **WHEN** `is_stagnant=true` 但 `confidence="low"`
- **THEN** 不触发 crossover，继续 master 策略

### Requirement: Worker 失败类型结构化记录
系统 SHALL 在 `_record_worker_failure` 中添加 `failure_type` 字段，取值为 `zero_changes`、`compile_error`、`smoke_error`、`timeout`、`boundary_violation` 之一。

#### Scenario: Worker 零变更失败
- **WHEN** Worker 因 target_files 无变更而失败
- **THEN** worker_failures.jsonl 中记录 `"failure_type": "zero_changes"`
