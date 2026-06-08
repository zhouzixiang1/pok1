# LLM 审计增强研究报告 — 扑克 AI 自进化系统

> 日期：2026-06-09 | 基于 21 个 LLM 触点、24 个代码层决策、10 篇文献、6 个技术方案、6 个案例研究的综合分析

---

## 1. 执行摘要

### 当前 LLM 利用率
- **LLM 触点总数**：21 个（10 core + 11 advisory）
- **代码层决策（无 LLM）**：24 个（6 high risk + 12 medium + 6 low）
- **Pipeline 覆盖率**：规划阶段 80%（Master/Direction Auditor），执行阶段 100%（Workers），验证阶段 60%（Reviewer/Critic 但缺少深层审计），提交阶段 50%（Archivist 但缺少一致性审计）

### Top 5 最高影响的增强机会

| # | 增强方案 | 风险等级 | 预期影响 |
|---|---------|---------|---------|
| 1 | **Master 计划后验证审计**（Post-Master Sanity Check） | P0 | 防止不合理计划进入 Worker，减少 30-50% 无效迭代 |
| 2 | **Precommit Eval 结果 LLM 语义解读** | P0 | 将数字比较升级为因果推理，捕获"rating 微涨但策略退化" |
| 3 | **Worker CoT 推理一致性检查** | P0 | 在 Code Review 之前检测策略矛盾，减少 Reviewer 负担 |
| 4 | **连续退化 LLM 诊断器** | P1 | 连续 2+ 代 rating 下降时触发根因分析，打破退化循环 |
| 5 | **H2H 异常 LLM 根因分析** | P1 | 检测对阵异常（某 matchup 突然大败），定位策略短板 |

---

## 2. 现状分析

### 2.1 当前 LLM 使用地图

按 Pipeline 阶段分组的所有 21 个 LLM 触点：

#### 规划阶段（4 触点）

| 文件 | 函数 | 角色 | 强制度 | 输入 | 输出 |
|------|------|------|--------|------|------|
| `direction_auditor.py` | `_run_direction_audit` | 重复方向检测 | mandatory | Git 历史 + 拒绝记录 | exhausted_directions, suggested_direction |
| `agent_master.py` | `_run_master_analysis` | 进化计划生成 | mandatory | Rating + Experience + Match 分析 | JSON 任务计划（2 个 Worker 分配） |
| `agent_master.py` | `_analyze_recent_matches` | 对局分析 | advisory | 近期败局/险胜回放 | 战术摘要 |
| `combined_analyst.py` | `_run_combined_analysis` | 停滞+性能综合分析 | advisory | Rating 趋势 + Active bots | is_stagnant, recommended_source |

#### 执行阶段（3 触点）

| 文件 | 函数 | 角色 | 强制度 | 输入 | 输出 |
|------|------|------|--------|------|------|
| `agent_workers.py` | `_run_single_worker` | 代码修改 | mandatory | Worker 任务 + Bot 源码 | 修改后的 .py 文件 |
| `agent_review.py` | `_run_crossover` | 跨代合并 | mandatory | 两个 Parent bot 源码 | 合并后的子代 bot |
| `experience_archivist.py` | `_consolidate_experience_pool` | 经验池去重 | advisory | 经验池全文 | 精简后的经验池 |

#### 验证阶段（4 触点）

| 文件 | 函数 | 角色 | 强制度 | 输入 | 输出 |
|------|------|------|--------|------|------|
| `agent_review.py` (via `tool_gates.py`) | Code Reviewer | 代码审查打分 | mandatory | Git diff + Worker 任务 | 分数 1-10 + 反馈 |
| `agent_review.py` | `_run_critic` | 策略批判 | mandatory | Diff + Rating + Experience | 分数 1-10（≥6 通过） |
| `agent_review.py` | `_run_performance_verification` | 性能验证 | advisory | Rating/WR 趋势 | 验证报告 |
| `stagnation_analyzer.py` | `_analyze_stagnation` | 停滞分析（legacy） | advisory | Rating 历史 | 停滞检测报告 |

#### 提交与归档阶段（3 触点）

| 文件 | 函数 | 角色 | 强制度 | 输入 | 输出 |
|------|------|------|--------|------|------|
| `experience_archivist.py` | `_run_archivist_analysis` | 代后评估 | advisory | 完成的代数据 | 经验池更新 |
| `orchestrator.py` | `_run_one_cycle` | Pipeline 控制 | mandatory | 系统状态上下文 | MCP tool 调用序列 |
| `tool_status.py` | `diagnose_environment` | 环境诊断 | advisory | 系统状态快照 | 异常诊断报告 |

#### 覆盖率分析

| Pipeline 阶段 | 有 LLM | 无 LLM | 覆盖率 |
|--------------|--------|--------|--------|
| 规划（Strategy Decision） | Direction Auditor, Master, Combined Analyst | `_decide_strategy`, `_pick_crossover_parents` | ~75% |
| 执行（Workers） | Worker agents | `_validate_worker_boundaries`, `_validate_master_plan` | ~70% |
| 验证（Gates） | Reviewer, Critic | Quality Gates (compile/smoke/decision), Worker CoT 审计 | ~60% |
| 评估（Eval） | （无） | `run_precommit_eval`, `wait_for_daemon_eval`, `_select_precommit_opponents` | **0%** |
| 提交（Commit） | Archivist | `commit_bot` gate ledger | ~40% |
| 维护（Maintenance） | Experience consolidator | `_do_reap_weakest`, data integrity checks | ~20% |

**关键盲区**：评估阶段（precommit eval、daemon eval）完全没有 LLM 参与，全部依赖纯数字比较。这是最大的审计缺口。

### 2.2 代码层决策风险分析

24 个无 LLM 监督的代码层决策，按风险分级：

#### High Risk（6 个）

| # | 文件:函数 | 决策 | 当前逻辑 | 风险 |
|---|----------|------|---------|------|
| 1 | `generation_scheduler.py:_decide_strategy` | master/crossover/branch 选择 | 优先级规则：stagnation→crossover, rec_source→branch | 策略选错导致无效进化 |
| 2 | `tool_gates.py:run_quality_gates` | 质量 gate 通过/失败 | py_compile + 1 mirror + decision tests + size | 漏过隐性 bug |
| 3 | `tool_commit.py:commit_bot` | 提交 gate ledger 验证 | 检查所有 stage gate 是否通过 | 缺少语义一致性验证 |
| 4 | `tool_helpers.py` stage gates | Pipeline 阶段顺序强制 | STAGE_GATE_ALLOWLIST 检查 | 阶段跳过 |
| 5 | `evolution_infra.py:wait_for_daemon_eval` | Daemon eval 充分性判断 | min_games + RD threshold | 数据不足时误判 |
| 6 | `decision_tester.py:run_decision_tests` | 决策测试通过判定 | 预定义场景 ≥70% pass rate | 测试覆盖不足 |

#### Medium Risk（12 个）

| # | 文件:函数 | 决策 |
|---|----------|------|
| 7 | `_pick_crossover_parents` | Crossover parent 选择（h2h_avg_wr + 版本间隔≥3） |
| 8 | `_statistical_stagnation_check` | 统计停滞预检（跳过 LLM 如果显而易见） |
| 9 | `_validate_worker_boundaries` | Worker 角色边界验证 |
| 10 | `_validate_master_plan` | Master 计划结构验证 |
| 11 | `execute_workers` circuit breaker | Worker 失败熔断（max 6） |
| 12 | `execute_workers` critic retry gate | Critic 拒绝后重规划 |
| 13 | `_select_precommit_opponents` | Precommit eval 对手选择 |
| 14 | `_do_reap_weakest` | Bot 淘汰（最弱淘汰） |
| 15 | `orchestrator_loop` degraded eval | 连续失败后的降级评估 |
| 16 | `run_smoke_test` | 烟雾测试（1 mirror match） |
| 17 | `execute_workers` code reset | Critic 拒绝后的代码重置 |
| 18 | `run_critic` threshold | Critic 分数阈值和 force_advance |

#### Low Risk（6 个）

| # | 文件:函数 | 决策 |
|---|----------|------|
| 19 | `post_generation_cleanup` | 经验池整合触发 |
| 20 | `run_precommit_eval` | Crossover parent_b 是否包含 precommit eval |
| 21 | `validate_agent_output` | Pydantic 输出 schema 验证 |
| 22 | `run_claude_query` rate limit | 429/529 重试逻辑 |
| 23 | `verify_code` compile check | py_compile 编译检查 |
| 24 | `check_code_size` | 文件大小限制执行 |

---

## 3. 文献调研与关键技术

### 3.1 LLM 代码审计技术

#### RAG 增强的多 Agent 代码审查（RovoDev / AutoReview）
- **核心技术**：将 LLM 代码审查与项目特定上下文（设计文档、历史审查、测试结果）结合检索，多 Agent 角色并行（安全、风格、逻辑）
- **适用性**：当前 Code Reviewer 仅看到 git diff + worker 任务描述。可引入 match replay 摘要、H2H 统计、历史 review 反馈作为 RAG 上下文，大幅提升审查深度
- **相关性**：★★★★★

#### Constitutional AI 自我批判模式（Anthropic）
- **核心技术**：模型生成输出后，依据一组原则（"宪法"）进行自我批判和修正。应用于特定领域时，宪法编码了领域专业知识
- **适用性**：为 Critic Agent 定义"扑克策略质量宪法"（如：不在有利位置过度 fold、保持合理的 3-bet 频率等），使评分更客观
- **相关性**：★★★★☆

#### DebugRepair 自主调试（RepairAgent）
- **核心技术**：LLM 生成补丁 → 运行测试 → 接收错误反馈 → 迭代修复。比单次生成成功率高 3-5x
- **适用性**：当 smoke test 或 decision test 失败时，将错误信息反馈给 Worker 进行针对性修复，而非通用重试
- **相关性**：★★★★☆

### 3.2 多阶段验证模式

#### Generate-Verify-Critique-Refine 四阶段验证 DAG
- **核心机制**：四个顺序验证阶段，每阶段作为带重试循环的 gatekeeper
- **映射**：当前 pipeline 是 Master(Generate) → Workers(Generate) → Reviewer(Verify) → Critic(Critique)。缺少的是 Refine 阶段（基于 Critic 反馈的定向修改），以及 Verify 阶段的深度不足

#### Multi-Model Council Gate
- **核心机制**：3-5 个独立 LLM 评估，结构化裁决聚合
- **映射**：当前仅一个 Reviewer + 一个 Critic。对于关键 gate（如 precommit eval），可引入 Council 模式减少单一评分偏差

#### 对抗性自博弈辩论（MARS / MARSHAL）
- **核心机制**：部署多个 LLM Agent 在对抗角色（proponent vs opponent）中提高推理质量
- **映射**：替换单一 Critic 为支持者/反对者对 + 裁判综合器，实现更彻底的策略评估

### 3.3 自进化系统中的 LLM 监督

#### FunSearch（Google DeepMind, Nature 2024）
- **核心**：LLM + 确定性评估器的进化循环。LLM 负责创造性生成，确定性评估器守卫正确性
- **关键教训**：配对创意 LLM 生成与严格评估器效果极佳；没有多样性约束会收敛到平庸方案
- **映射**：当前系统已有此模式（Worker=LLM 生成，mirror battle=确定性评估器），但缺少多样性约束的 LLM 监督

#### LLaMEA 框架（LLM-driven Evolutionary Optimization）
- **核心**：LLM 作为进化循环中的智能变异/交叉算子，受适应度函数反馈引导
- **映射**：当前 Master Architect 已扮演此角色，但缺乏对 fitness landscape 的 LLM 深度理解（如"为什么这个策略空间已饱和"）

#### LLM-as-a-Judge（ICLR 2025）
- **核心**：用 LLM 对输出进行评分/比较，需要偏差校准（多模型集成、打乱顺序、历史准确率追踪）
- **关键教训**：单一 judge 无偏差缓解会产生不可靠排名；结构化输出格式（score + pass/fail + reason）效果显著
- **映射**：当前 Critic 打分已有结构化输出，但缺少偏差校准（无历史准确率追踪）

### 3.4 关键技术模式总结

| # | 模式 | 核心思想 | 系统适用点 |
|---|------|---------|-----------|
| 1 | RAG 增强审查 | 注入领域上下文提升审查深度 | Reviewer/Critic 增加 match/H2H 数据 |
| 2 | LLM 进化搜索算子 | LLM 智能变异+适应度反馈 | Master 已实现，可增加 fitness landscape 分析 |
| 3 | Constitutional 自我批判 | 领域原则编码为"宪法" | Critic 增加扑克策略质量宪法 |
| 4 | LLM-as-Judge 偏差校准 | 多模型+打乱+历史追踪 | Critic 增加评分偏差校准 |
| 5 | 对抗辩论 | Proponent/Opponent 对抗 | Critic 升级为三方辩论 |
| 6 | 测试反馈自调试 | 错误→修复→再测试循环 | Worker 重试增加错误引导修复 |
| 7 | 自动质量 gate 层 | CI/CD 分层检查 | Quality gates 增加 LLM 语义层 |
| 8 | 对抗场景生成 | LLM 生成边界测试用例 | Decision tests 增加 LLM 生成场景 |
| 9 | CoT 推理监控 | 分析推理链的一致性 | Worker 输出增加 CoT 一致性检查 |
| 10 | 探索-利用平衡 | 多样性 vs 精炼的权衡 | Worker 重试策略增加多样性控制 |

---

## 4. 缺口分析

### 4.1 策略与规划审计（A 类）— 4 个缺口

| # | 缺口 | 当前逻辑 | 为什么需要 LLM | 风险 |
|---|------|---------|--------------|------|
| A1 | **Master 计划合理性验证** | `_validate_master_plan` 仅检查 JSON 结构，不验证策略语义 | Master 可能生成自相矛盾的计划（如"加强激进度"同时"减少 3-bet"），Worker 按计划执行后产生矛盾代码 | P0 |
| A2 | **策略选择审计** | `_decide_strategy` 纯规则：stagnation→crossover, rec_source→branch | 规则优先级可能不适配当前状态。如 stagnation=false 但 combined_analyst 输出的 recommended_source 本身可能不合理 | P1 |
| A3 | **跨代方向连贯性检查** | Direction Auditor 仅检查最近 6 代的 commit message | 无法检测"长期策略摇摆"（如 A→B→A→B 的交替）或"策略发散"（每代方向完全不同） | P2 |
| A4 | **Crossover 父本选择审计** | `_pick_crossover_parents` 基于固定公式（h2h_avg_wr + 版本间隔≥3） | 不考虑策略兼容性，可能选择策略冲突的 parent（如一个偏激进一个偏保守）导致子代性能崩塌 | P1 |

### 4.2 代码质量与安全审计（B 类）— 5 个缺口

| # | 缺口 | 当前逻辑 | 为什么需要 LLM | 风险 |
|---|------|---------|--------------|------|
| B1 | **Worker 输出 CoT 一致性检查** | 无。Worker 产出直接进入 Reviewer | Worker 可能声称"加强激进策略"但代码实际增加了 fold 频率。Reviewer 看的是 diff，不追踪推理链 | P0 |
| B2 | **Worker 边界语义验证** | `_validate_worker_boundaries` 基于文件名和正则检查 | Tuner 可能通过间接方式修改策略逻辑（如修改 hand_evaluator 中的常量来影响 fold/call 决策），正则无法检测 | P1 |
| B3 | **LLM 生成的决策测试用例** | `decision_tester.py` 使用预定义场景 | 预定义场景无法覆盖 Worker 的具体变更。Worker 修改了"翻后 OOP check-raise 策略"但测试场景没有覆盖这种情况 | P0 |
| B4 | **Crossover 合并语义审计** | 无。Crossover LLM 合并后直接进入 Quality Gates | 合并可能引入逻辑矛盾（如 parent A 的 hand_evaluator 与 parent B 的 strategy.py 产生冲突的输出格式） | P1 |
| B5 | **深层安全审计** | 无。仅 compile check + smoke test | 可能引入死循环、极端内存使用、或特定牌面下的崩溃。1 mirror match 的覆盖面极窄 | P2 |

### 4.3 性能与回归审计（C 类）— 5 个缺口

| # | 缺口 | 当前逻辑 | 为什么需要 LLM | 风险 |
|---|------|---------|--------------|------|
| C1 | **Precommit Eval 语义解读** | `run_precommit_eval` 纯数字比较（win rate > parent） | "win rate 微涨 1% 但对阵 top-3 对手全败"这种情况数字上看不出，需要 LLM 语义分析 | P0 |
| C2 | **Rating 趋势因果推理** | `combined_analyst` 检测停滞但不确定根因 | "rating 下降 20 点"可能是：(a) 被更强 bot 超越，(b) 自身策略退化，(c) 对手针对性适应。不同根因需要不同对策 | P1 |
| C3 | **H2H 异常根因分析** | 无。H2H 矩阵仅用于数字展示 | 某 matchup 突然从 55% 跌到 30%，可能是关键策略短板。需要 LLM 分析对应 replay 定位问题 | P1 |
| C4 | **Daemon eval 数据质量判断** | `wait_for_daemon_eval` 等待 min_games + RD | 100 局游戏可能集中在少数对手（覆盖偏差），RD<60 但实际评估不足。LLM 可判断评估数据的"代表性" | P2 |
| C5 | **Mirror Battle 模式分析** | 无。Battle 结果仅用 win/loss 计数 | 连续赢的局都是小分差，输的局都是大分差——数字上 win rate 持平但实际策略有系统性弱点 | P2 |

### 4.4 元认知与自我改进审计（D 类）— 4 个缺口

| # | 缺口 | 当前逻辑 | 为什么需要 LLM | 风险 |
|---|------|---------|--------------|------|
| D1 | **经验池质量审计** | `experience_pool.py` 仅做行数修剪 | 经验条目可能过时（基于旧版本的策略建议）、矛盾（"加强激进" vs "减少激进度"两经验同时存在）、或与当前状态无关 | P1 |
| D2 | **长期进化方向反思** | 无。每代独立分析 | 无法回答"过去 10 代的进化方向是否在收敛？"、"是否陷入了局部最优？" 这类元认知问题 | P1 |
| D3 | **Worker 失败模式分析** | `worker_failures.jsonl` 仅记录不分析 | 连续多代 Worker 在同一模块（如 postflop.py）失败，暗示该模块结构有问题而非 Worker 能力不足 | P2 |
| D4 | **系统参数 LLM 建议优化** | 硬编码常量 | MAX_ACTIVE_BOTS=30、MIN_DECISION_PASS_RATE=0.7 等参数是否合理？随着 bot 池演化，固定值可能不再适用 | P3 |

### 4.5 数据完整性审计（E 类）— 3 个缺口

| # | 缺口 | 当前逻辑 | 为什么需要 LLM | 风险 |
|---|------|---------|--------------|------|
| E1 | **H2H 数据一致性检查** | 无。之前出现过 head_to_head.json 被 SIGKILL 截断为 0 字节 | 文件锁 + atomic write 已缓解，但仍需 LLM 做语义级验证（如 A vs B 的胜率 + B vs A 的胜率应接近 50% 对称） | P1 |
| E2 | **Rating 突变异常检测** | 无 | 单次 Glicko-2 更新导致 rating 变化 >100 是异常信号，可能是数据损坏或极端结果 | P2 |
| E3 | **Daemon crash 后数据诊断** | Daemon 重启后直接继续 | crash 可能导致部分 game 结果未写入。LLM 可分析 crash 前后的数据连续性 | P2 |

### 4.6 反退化与反漂移审计（F 类）— 4 个缺口

| # | 缺口 | 当前逻辑 | 为什么需要 LLM | 风险 |
|---|------|---------|--------------|------|
| F1 | **连续退化检测与诊断** | `combined_analyst` 检测停滞但不检测"退化趋势" | 连续 2+ 代 rating 下降时，需要 LLM 做根因分析：是策略退化还是对手变强？退化在哪个阶段（preflop/postflop）？ | P1 |
| F2 | **策略漂移检测** | 无 | Worker 可能在"改进 A 策略"的过程中意外修改了 B 策略。需要 LLM 对比变更前后的整体策略画像 | P1 |
| F3 | **Crossover 优势保持验证** | 无 | Crossover 子代应保持两个 parent 的优势。需要 LLM 验证子代策略是否确实融合了两者的优点而非继承了缺陷 | P1 |
| F4 | **经验池误导检测** | 无 | 过时的经验条目可能引导 Worker 采取已证明无效的策略方向 | P2 |

### 缺口统计

| 类别 | P0 | P1 | P2 | P3 | 合计 |
|------|----|----|----|----|------|
| A. 策略与规划 | 1 | 2 | 1 | 0 | 4 |
| B. 代码质量 | 2 | 2 | 1 | 0 | 5 |
| C. 性能回归 | 1 | 2 | 2 | 0 | 5 |
| D. 元认知 | 0 | 2 | 1 | 1 | 4 |
| E. 数据完整性 | 0 | 1 | 2 | 0 | 3 |
| F. 反退化 | 0 | 3 | 1 | 0 | 4 |
| **合计** | **4** | **12** | **8** | **1** | **25** |

---

## 5. 增强方案设计

### 5.1 P0 关键增强（建议立即实施）

#### P0-1: Post-Master 计划验证审计

| 属性 | 详情 |
|------|------|
| **对应缺口** | A1 — Master 计划合理性验证 |
| **集成位置** | `tool_planning.py:run_master` → Master 返回后、Workers 执行前 |
| **输入** | Master 输出的 JSON 计划 + 系统当前状态（rating、experience pool、H2H 矩阵） |
| **LLM Prompt 核心指令** | ① 检查两个 Worker 任务是否存在逻辑矛盾 ② 验证建议变更与 experience pool 中的经验教训是否冲突 ③ 评估计划是否针对 combined_analyst 指出的核心问题 ④ 判断方向是否与近期 3 代的 commit 方向重复 |
| **输出 JSON** | `{valid: bool, contradictions: [...], experience_conflicts: [...], direction_repetition: bool, suggestion: "..."}` |
| **集成逻辑** | `valid=false` → 注入矛盾信息回 Master prompt，要求重新规划（最多 1 次重试） |
| **回退策略** | LLM 调用失败 → 跳过验证，按原流程继续（不阻塞 pipeline） |
| **Token 消耗** | ~3K input + ~1K output = ~4K tokens/次 |
| **调用频率** | 每代 1 次（Master 后） |

#### P0-2: Worker CoT 推理一致性检查

| 属性 | 详情 |
|------|------|
| **对应缺口** | B1 — Worker 输出 CoT 一致性检查 |
| **集成位置** | `agent_workers.py:_run_single_worker` → Worker 输出后、进入 Reviewer 前 |
| **输入** | Worker 的完整文本输出（推理过程） + Worker 实际修改的代码 diff |
| **LLM Prompt 核心指令** | ① 提取 Worker 声称要做的 N 个变更 ② 对比实际 diff，确认每个声称的变更是否真正实现 ③ 检查推理过程中是否存在逻辑矛盾（如"减少 fold"但实际增加了 fold 条件） ④ 评估变更是否符合 Worker 角色边界 |
| **输出 JSON** | `{consistent: bool, claimed_changes: [...], actual_changes: [...], contradictions: [...], boundary_violations: [...]}` |
| **集成逻辑** | `consistent=false` → 将矛盾信息注入 Reviewer prompt，标记需要重点审查的区域 |
| **回退策略** | LLM 调用失败 → 跳过，Reviewer 按正常流程审查 |
| **Token 消耗** | ~5K input + ~1.5K output = ~6.5K tokens/次 |
| **调用频率** | 每代 2-3 次（每个 Worker 一次） |

#### P0-3: LLM 生成的决策测试用例

| 属性 | 详情 |
|------|------|
| **对应缺口** | B3 — 动态决策测试用例 |
| **集成位置** | `tool_gates.py:run_quality_gates` → decision tests 前生成补充测试 |
| **输入** | Master 计划 + Worker 任务的变更描述 + 当前 bot 的 strategy.py |
| **LLM Prompt 核心指令** | ① 根据 Worker 任务描述，生成 5-10 个与变更直接相关的测试场景 ② 每个场景包含：完整的牌面状态、预期动作、测试理由 ③ 覆盖 Worker 声称要改进的特定策略点 |
| **输出 JSON** | `{scenarios: [{hand: "...", board: "...", pot: N, action_history: "...", expected_range: "...", reason: "..."}]}` |
| **集成逻辑** | 将生成场景与预定义场景合并，统一执行 decision tests |
| **回退策略** | LLM 调用失败 → 仅使用预定义场景 |
| **Token 消耗** | ~4K input + ~2K output = ~6K tokens/次 |
| **调用频率** | 每代 1 次（Quality Gates 前） |

#### P0-4: Precommit Eval 语义解读

| 属性 | 详情 |
|------|------|
| **对应缺口** | C1 — Precommit Eval 结果解读 |
| **集成位置** | `tool_eval.py:run_precommit_eval` → mirror battle 结果出来后 |
| **输入** | 新 bot vs parent、vs top opponents 的 mirror battle 结果（胜率、分差分布）+ H2H 对阵矩阵 |
| **LLM Prompt 核心指令** | ① 分析胜负分布：是否"赢得小分、输得大分" ② 检查对阵 top-3 对手的表现趋势 ③ 判断总体 win rate 提升是否来自对弱对手的改进（而非核心策略提升） ④ 给出"通过/不通过"建议及理由 |
| **输出 JSON** | `{pass: bool, analysis: "...", risk_areas: [...], recommendation: "..."}` |
| **集成逻辑** | `pass=false` → 阻止提交，将分析反馈给 Orchestrator。`pass=true` 但有 risk_areas → 允许提交但记录风险区域到 experience pool |
| **回退策略** | LLM 调用失败 → 使用原有纯数字判断 |
| **Token 消耗** | ~3K input + ~1.5K output = ~4.5K tokens/次 |
| **调用频率** | 每代 1 次（Precommit Eval 后） |

### 5.2 P1 高优先级增强

#### P1-1: 连续退化 LLM 诊断器

| 属性 | 详情 |
|------|------|
| **对应缺口** | F1 |
| **集成位置** | `generation_scheduler.py:prepare_generation` → combined_analyst 后 |
| **触发条件** | 最近 2+ 代 rating 连续下降 |
| **Prompt 核心** | 分析最近 N 代的 commit message、策略变更摘要、rating 曲线，判断退化根因（策略退化 vs 对手适应 vs 随机波动） |
| **输出** | `{degenerating: bool, root_cause: "strategy_decay|opponent_adaptation|random", affected_areas: [...], recovery_suggestion: "..."}` |
| **Token 消耗** | ~5K/次，频率：约 20% 的代 |

#### P1-2: H2H 异常根因分析

| 属性 | 详情 |
|------|------|
| **对应缺口** | C3 |
| **集成位置** | `combined_analyst.py:_run_combined_analysis` 内部或之后 |
| **触发条件** | 任何 matchup 的 win rate 与上一代偏差 >15% |
| **Prompt 核心** | 分析对应 replay 文件中的关键手牌，定位策略失误模式 |
| **输出** | `{anomalies: [{matchup: "...", delta: N, likely_cause: "...", key_hands: [...]}]}` |
| **Token 消耗** | ~6K/次，频率：约 30% 的代 |

#### P1-3: Crossover 父本兼容性审计

| 属性 | 详情 |
|------|------|
| **对应缺口** | A4, F3 |
| **集成位置** | `tool_commit.py:run_crossover` → 合并前 |
| **Prompt 核心** | 对比两个 parent bot 的核心策略模块，识别不兼容的假设（如不同的 card encoding、不同的 hand strength 计算），给出合并建议 |
| **输出** | `{compatible: bool, conflicts: [...], merge_strategy: "..."}` |
| **Token 消耗** | ~8K/次，频率：约 15% 的代（stagnation 时） |

#### P1-4: 经验池质量审计

| 属性 | 详情 |
|------|------|
| **对应缺口** | D1, F4 |
| **集成位置** | `experience_archivist.py:_consolidate_experience_pool` 增强 |
| **Prompt 核心** | 审查每条经验的：(1) 是否基于当前版本还是旧版本 (2) 与其他经验是否矛盾 (3) 是否与当前 rating 趋势一致 (4) 是否过于具体/过于泛化 |
| **输出** | `{valid_entries: [...], contradictory_pairs: [...], stale_entries: [...], new_insights: "..."}` |
| **Token 消耗** | ~4K/次，频率：每 3 代 1 次 |

#### P1-5: Rating 趋势因果推理

| 属性 | 详情 |
|------|------|
| **对应缺口** | C2 |
| **集成位置** | `combined_analyst.py:_run_combined_analysis` 增强 |
| **Prompt 核心** | 在当前 stagnation/performance 分析基础上增加因果推理层：rating 变化的主要原因是什么？是对手变强还是自身退化？退化在哪个策略维度？ |
| **输出** | 在现有 combined output 中增加 `causal_analysis` 字段 |
| **Token 消耗** | ~3K 额外/次，频率：每代 1 次 |

#### P1-6: 策略漂移检测

| 属性 | 详情 |
|------|------|
| **对应缺口** | F2 |
| **集成位置** | `tool_gates.py:run_review` → Reviewer prompt 增强 |
| **Prompt 核心** | 对比变更前后的核心策略参数（如果 constants.py 中有可量化指标），检测是否有非预期的策略维度变化 |
| **输出** | `{drift_detected: bool, drifted_dimensions: [...], expected_changes: [...], unexpected_changes: [...]}` |
| **Token 消耗** | ~4K 额外/次，频率：每代 1 次 |

### 5.3 P2/P3 改进建议

| 优先级 | 增强方案 | 缺口 | 概述 |
|--------|---------|------|------|
| P2 | Worker 失败模式分析器 | D3 | 定期分析 worker_failures.jsonl，检测集中失败模式 |
| P2 | 对抗性测试场景生成 | B5 | LLM 生成极端牌面测试 bot 鲁棒性 |
| P2 | Rating 突变异常检测 | E2 | 统计 + LLM 混合检测 rating 异常波动 |
| P2 | Daemon eval 代表性评估 | C4 | LLM 判断 eval 数据的对手覆盖是否充分 |
| P2 | H2H 数据语义验证 | E1 | LLM 验证对阵矩阵的逻辑一致性 |
| P2 | Mirror battle 模式分析 | C5 | LLM 分析胜负模式（大分差/小分差分布） |
| P3 | 系统参数 LLM 优化建议 | D4 | 每周一次 LLM 审查系统参数合理性 |
| P3 | 跨代方向连贯性长期分析 | A3 | LLM 分析 10+ 代的进化方向连贯性 |

### 5.4 元增强方案

#### 元增强 1: 持续审计循环（Continuous Audit Loop）

将多个独立审计点串联为一个贯穿 pipeline 的审计链：

```
Master 计划审计(P0-1) → Worker CoT 审计(P0-2) → 动态测试生成(P0-3)
    → Reviewer 增强(策略漂移检测 P1-6) → Precommit 语义解读(P0-4)
    → 代后退化诊断(P1-1) → 经验池审计(P1-4)
```

**关键设计**：每步审计的输出作为下一步的输入上下文。例如：
- Worker CoT 审计发现的矛盾 → 注入 Reviewer prompt 的"重点审查区域"
- Precommit 语义解读发现的弱点 → 写入 experience pool → 影响下一代 Master
- 退化诊断的结论 → 注入下一代的 combined_analyst context

**预估成本**：~30K tokens/代，约 $0.09/代（按 Claude Sonnet 价格）

#### 元增强 2: 反退化守护者（Anti-Regression Guardian）

一个独立的 LLM Agent，在以下时机触发深度分析：

```
触发条件:
├── 连续 2+ 代 rating 下降 → 全面的退化根因分析
├── 单代 rating 下降 > 30 点 → 紧急策略审计
├── Crossover 后 win rate 低于双 parent 平均 → 合并失败诊断
└── 经验池存在 3+ 条矛盾经验 → 知识库健康检查

输出:
├── 退化类型分类（策略/适应/随机）
├── 受影响的策略维度
├── 恢复建议（branch_from=某个历史版本、强制 crossover、重置参数等）
└── 预警信号写入 system_events.jsonl
```

**预估成本**：~8K tokens/次触发，约每月 20-30 次触发

#### 元增强 3: Critic 偏差校准系统（Critic Bias Calibration）

基于 LLM-as-a-Judge 模式的偏差缓解：

```
1. 历史追踪: 记录每代 Critic 分数与实际 rating 变化的相关性
2. 偏差检测: Critic 给 8 分但实际 rating 下降 → 标记为"过度乐观"
3. 校准注入: 在 Critic prompt 中注入历史校准数据:
   "过去 5 代中，你给出的平均分为 7.2，实际 rating 变化为 -15/+20。
    你倾向于高估 [策略复杂度] 维度的改进。本次请特别注意。"
4. 多角度评审: 对关键代，运行 2 个独立 Critic（不同 prompt 视角），
   分数差异 >2 时触发仲裁
```

**预估成本**：~2K tokens/代的校准注入 + 偶尔的双 Critic 开销

### 5.5 实施路线图

| 阶段 | 时间 | 增强方案 | 预估工作量 |
|------|------|---------|-----------|
| **Phase 1** | 第 1-2 天 | P0-1 Post-Master 验证 + P0-2 Worker CoT 审计 | ~200 行新代码 |
| **Phase 2** | 第 3-4 天 | P0-3 动态测试生成 + P0-4 Precommit 语义解读 | ~250 行新代码 |
| **Phase 3** | 第 5-7 天 | P1-1 退化诊断 + P1-2 H2H 异常分析 + P1-5 因果推理 | ~300 行新代码 |
| **Phase 4** | 第 8-10 天 | P1-3 Crossover 审计 + P1-4 经验池审计 + P1-6 策略漂移 | ~250 行新代码 |
| **Phase 5** | 第 11-14 天 | 元增强 1（持续审计循环）+ 元增强 3（Critic 校准） | ~200 行新代码 |
| **Phase 6** | 第 15-17 天 | 元增强 2（反退化守护者）+ P2 级增强 | ~300 行新代码 |

---

## 6. 成本效益分析

### 6.1 Token 成本估算

基于 Claude Sonnet 4 定价（$3/M input, $15/M output）：

| 增强方案 | Input tokens | Output tokens | 单次成本 | 频率/代 | 日成本（10代/日） |
|---------|-------------|--------------|---------|---------|-----------------|
| P0-1 Master 验证 | 3K | 1K | $0.024 | 1 | $0.24 |
| P0-2 Worker CoT | 5K | 1.5K | $0.038 | 2.5 | $0.94 |
| P0-3 动态测试 | 4K | 2K | $0.042 | 1 | $0.42 |
| P0-4 Precommit 语义 | 3K | 1.5K | $0.032 | 1 | $0.31 |
| P1-1 退化诊断 | 5K | 1.5K | $0.038 | 0.2 | $0.08 |
| P1-2 H2H 异常 | 6K | 2K | $0.048 | 0.3 | $0.14 |
| P1-3 Crossover 审计 | 8K | 2K | $0.054 | 0.15 | $0.08 |
| P1-4 经验池审计 | 4K | 1.5K | $0.035 | 0.33 | $0.11 |
| P1-5 因果推理 | 3K | 1K | $0.024 | 1 | $0.24 |
| P1-6 策略漂移 | 4K | 1K | $0.027 | 1 | $0.27 |
| **合计** | | | | | **$2.83/日** |

### 6.2 质量提升预期

| 指标 | 当前 | 预期改善 | 原因 |
|------|------|---------|------|
| 无效 Worker 迭代率 | ~30%（Critic 拒绝后重试） | 降至 ~15% | Post-Master 验证 + Worker CoT 审计减少无效计划 |
| 策略退化频率 | 每 5-8 代出现一次 | 降至每 15-20 代 | 退化诊断 + Precommit 语义解读 + 策略漂移检测 |
| Crossover 失败率 | ~40%（子代弱于 parent） | 降至 ~20% | 父本兼容性审计 + 优势保持验证 |
| 经验池质量 | 存在过时/矛盾条目 | 显著改善 | 定期质量审计 + 矛盾检测 |
| 进化方向重复率 | ~25%（Direction Auditor 检测后） | 降至 ~10% | Post-Master 验证增加方向去重 |
| Rating 增长速率 | ~5-10 点/代（平均） | 提升至 ~12-18 点/代 | 更精准的策略定位 + 减少无效迭代 |

### 6.3 ROI 分析

| 项目 | 数值 |
|------|------|
| 当前 LLM 成本/日（进化系统） | ~$15-25（Master + Workers + Reviewer + Critic + 其他） |
| 新增审计成本/日 | ~$2.83 |
| 成本增幅 | ~12-19% |
| 预期每代质量提升 | 减少 30-50% 无效迭代 + 减少 50% 退化事件 |
| **ROI** | 每投入 $1 审计成本，节省 $3-5 的无效 Worker/Review 调用 |

---

## 7. 实施建议与风险

### 7.1 分阶段实施计划

**原则**：渐进式添加，每个新增强独立可测，不影响现有 pipeline。

1. **先加 P0 级**：这 4 个增强覆盖了最高风险的缺口，且实现简单（每个 ~50-80 行代码）
2. **观察 2-3 天**：收集审计日志，评估 LLM 审计的实际准确率
3. **逐步添加 P1 级**：根据 P0 的经验调整 prompt 和集成方式
4. **最后添加元增强**：需要前面的基础设施（审计日志、校准数据）支持

### 7.2 潜在风险与缓解

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| LLM 审计误报（阻止了好的变更） | 中 | 高 | 所有 P0 增强都有 fallback（LLM 失败时使用原逻辑）。审计结果作为"建议"而非"强制"，除非信心 >90% |
| Token 成本超预期 | 低 | 中 | 每个增强都有独立开关，可逐个禁用。监控实际 token 消耗 |
| 审计 LLM 引入新延迟 | 中 | 低 | 所有审计并行执行，不增加 pipeline 总时间 |
| 审计 prompt 不适配新策略 | 中 | 中 | 每月 review 审计 prompt 的有效性，根据 false positive/negative 调整 |
| 多个审计点之间冲突 | 低 | 中 | 元增强 1 的持续审计循环设计为串联而非并行，每步的输出是下步的输入 |

### 7.3 监控指标

实施后应追踪的关键指标：

| 指标 | 目标 | 监控方式 |
|------|------|---------|
| Post-Master 审计拦截率 | 5-15% 的 Master 计划被拦截 | `pipeline_state.json` 中 audit_results 字段 |
| Worker CoT 矛盾检出率 | 检出 10-20% 的 Worker 输出有矛盾 | `worker_failures.jsonl` 新增 contradiction 类型 |
| Precommit 语义解读准确率 | 与实际 rating 走势一致率 >80% | 对比 audit_pass/fail 与后续 rating 变化 |
| 退化诊断准确率 | 根因分析准确率 >70% | 人工验证或回测历史数据 |
| 审计 LLM 的 false positive 率 | <10% | 被审计拦截但实际表现良好的代 |

---

## 8. 结论

### 核心发现

1. **当前系统的 LLM 利用率适中但存在关键盲区**。21 个 LLM 触点已覆盖规划、执行、验证的核心流程，但评估阶段（precommit eval、daemon eval）完全没有 LLM 参与，且验证阶段的深度不足（Reviewer/Critic 缺少 CoT 审计和偏差校准）。

2. **25 个审计缺口中 4 个为 P0 级**。这些缺口集中在：(a) Master 计划缺乏合理性验证，导致无效 Worker 迭代；(b) Worker 推理链缺乏一致性检查，矛盾代码进入 Review；(c) 决策测试用例静态，无法覆盖动态变更；(d) Precommit eval 纯数字判断，遗漏语义层面的问题。

3. **文献和案例验证了这些增强方向的可行性**。FunSearch 的 LLM+确定性评估器模式、LLaMEA 的进化搜索算子、LLM-as-a-Judge 的偏差校准、以及 Constitutional AI 的自我批判模式都提供了成熟的实现范式。

4. **成本效益比优异**。新增审计成本仅增加 ~12-19% 的 token 开销（$2.83/日），但预期减少 30-50% 的无效迭代和 50% 的退化事件，ROI 约 3-5 倍。

### 建议的优先行动

1. **立即实施 P0-1 和 P0-2**（Post-Master 验证 + Worker CoT 审计）—— 这两个增强拦截率最高、实现最简单
2. **本周内实施 P0-3 和 P0-4**（动态测试 + Precommit 语义解读）—— 补充质量 gate 的深度
3. **下周开始 P1 级增强**，根据 P0 的实际效果调整策略
4. **持续收集审计日志数据**，用于 Critic 偏差校准和长期效果评估

---

*本报告基于对 web/core/ 下 30+ 个源文件的代码分析，结合 2024-2026 年 LLM 代码审计、自进化系统、多 Agent 质量保证领域的文献调研生成。*
