# Tasks

## 核心改进（打通断裂数据流）

- [x] Task 1: Critic 输出注入 Stagnation 分析
  - [x] 1.1: 在 `stagnation_analyzer.py` 的 `_analyze_stagnation()` 中添加 `prev_critic_info` 参数
  - [x] 1.2: 在 `generation_scheduler.py` 的 `prepare_generation()` 中，从 archive/ 或最近 checkpoint 提取 Critic gate 结果
  - [x] 1.3: 将 `local_optima_warning` 和 `strategic_assessment` 注入 Stagnation prompt 模板的新占位符 `{critic_insights}`
  - [x] 1.4: 更新 `prompts/stagnation_analyzer.md` 增加 `{critic_insights}` 占位符和使用指引

- [x] Task 2: Critic evidence 存入 experience_pool
  - [x] 2.1: 在 `tool_gates.py` 的 `run_critic()` 中提取 Critic 输出的 `evidence` 字段
  - [x] 2.2: 将 evidence 格式化为文本，追加到 `experience_pool.md` 的 `RECENT_LESSONS` 区段
  - [x] 2.3: 通过 `_append_experience_updates()` 或独立函数写入

- [x] Task 3: Reviewer 输出注入 Archivist
  - [x] 3.1: 在 `tool_commit.py` 的 `run_archivist()` 中，从 checkpoint 读取 `gate_results.review` 的 `change_summary` 和 `risk_areas`
  - [x] 3.2: 将这两个字段注入 Archivist prompt（通过 snapshot JSON 或独立字段）
  - [x] 3.3: 更新 `prompts/archivist.md` 增加 review 上下文的使用指引

- [x] Task 4: exhausted_directions 传入 Consolidator
  - [x] 4.1: 在 `generation_scheduler.py` 的 `post_generation_cleanup()` 中，从当前 checkpoint 读取 `direction_audit.exhausted_directions`
  - [x] 4.2: 将列表传入 `_consolidate_experience_pool()` 替换硬编码空字符串
  - [x] 4.3: 更新 `experience_archivist.py` 第 42 行使用实际值

- [x] Task 5: prev_critic 正确持久化
  - [x] 5.1: 在 `tool_helpers.py` 的 `_record_gate()` 中，当 gate_name == "critic" 时保存 `prev_critic` 字段到 gate_results（已确认代码中已正确实现）
  - [x] 5.2: 验证 `tool_gates.py` 的 `run_critic()` 中读取 `prev_critic` 的路径是否正确（已确认正确）

## 辅助改进（消除数据浪费）

- [x] Task 6: Master analysis 从 checkpoint 直接读取
  - [x] 6.1: 在 `direction_auditor.py` 的 `_run_direction_audit()` 中，添加从 checkpoint JSON 读取 `master_plan.analysis` 的逻辑
  - [x] 6.2: 保留日志正则提取作为 fallback

- [x] Task 7: Stagnation confidence 影响策略决策
  - [x] 7.1: 修改 `generation_scheduler.py` 的 `_decide_strategy()`，增加 `confidence == "low"` 时不触发 crossover 的逻辑

- [x] Task 8: Worker 失败类型结构化记录
  - [x] 8.1: 修改 `agent_workers.py` 的 `_record_worker_failure()` 添加 `failure_type` 字段
  - [x] 8.2: 在零变更检测、编译错误、冒烟错误、超时等路径中传入正确的 `failure_type`

## 验证

- [x] Task 9: 语法检查 + 集成验证
  - [x] 9.1: 所有修改文件通过 `py_compile` 检查
  - [x] 9.2: 验证 experience_pool.md 写入格式正确
  - [x] 9.3: 验证 pipeline_state.json 结构兼容

# Task Dependencies
- Task 2 依赖 Task 5（Critic evidence 提取需要 gate 记录正确）
- Task 4 独立
- Task 6 独立
- Task 7 独立
- Task 8 独立
- Task 1 和 Task 3 可并行
- Task 9 依赖所有其他 Task
