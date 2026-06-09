# 演化流水线瓶颈修复与竞赛级 Bot 强化集成计划

> 基于 pipeline-bottleneck-analysis.md 全面核实结果，结合国赛平台竞赛规则和最新扑克 AI 研究。
> 目标：不计 token 成本，持续运行，产出国赛级别最强 Texas Hold'em bot。

---

## 核实结果摘要

### 瓶颈分析报告 10 项声明核实

| # | 声明 | 核实结果 | 当前状态 |
|---|------|---------|---------|
| 1 | Worker 串行执行，信号量是死代码 | ✅ 已确认 | `agent_workers.py:166-235` 串行 for 循环，`MAX_PARALLEL_WORKERS`/`_WORKER_SEMAPHORE` 从未被调用 |
| 2 | 熔断器按 len(tasks) 递增（非实际失败数） | ✅ 已确认 | `tool_planning.py:363-371` + `:489`，行号偏移但缺陷不变 |
| 3 | 12/14 场景为 CRITICAL | ⚠️ 部分准确 | 实际 12/15（非 14），比例仍然过高 |
| 4 | 编排器缺乏看门狗 | ✅ 已确认 | 无任何 watchdog/liveness/heartbeat 机制 |
| 5 | 预提交评估串行执行 | ✅ 已确认 | `tool_eval.py:102-165` 串行 for 循环 |
| 6 | 方向审计 48% 误报率 | ⚠️ 无法从代码验证 | 需运行时日志统计，但 LLM 驱动的检测机制未变 |
| 7 | 零修改检测机制存在 | ✅ 已确认 | `agent_workers.py:122-142` + `tool_planning.py:429-436` 双层检测 |
| 8 | STAGE_GATE_ALLOWLIST 硬依赖 | ✅ 已确认 | 定义在 `evolution_infra.py:76-86`，通过 `tool_helpers.py` 的 `_xxx_gate_ok` 执行 |
| 9 | 角色边界违规是最常见拒因 | ⚠️ 无法从代码验证 | 但代码中已有多层边界执行（master plan + worker prompt + post-worker + reviewer） |
| 10 | 冒烟测试 60-120s | ✅ 已确认 | `smoke_tester.py:23` 1 次 mirror_battle（2 局 × 70 手） |

**结论：10 项声明中 7 项完全确认，3 项部分确认（数据差异或需运行时验证），0 项修复。所有瓶颈仍然存在。**

### 竞赛规则合规性差距

| 严重级别 | 规则 | 差距 | 影响 |
|---------|------|------|------|
| **Critical** | Wheel straight (A-2-3-4-5) 评估 | bot 的 `card_utils.py evaluate_5()` 不识别 wheel | ~2-3% 的牌局严重错判 |
| **High** | Re-raise 必须 strictly > 2x | `state.py` 的 `min_raise_action` 公式允许正好 2x | 每次 3bet/4bet/加注-再加注可能被判非法 → 自动弃牌 |
| Low | TOTAL_HANDS 常量不匹配 | 缺少 hand/max_hand 时默认 50（实际 70） | 仅在输入缺失时触发，正常竞赛不会触发 |
| ✅ 无差距 | 13 条验证规则中 10 条 | 全部正确实现 | — |

### Bot 策略差距

| 优先级 | 领域 | 当前状态 | 改进方向 |
|--------|------|---------|---------|
| **Critical** | Wheel straight 检测 | `evaluate_5()` 不识别 A-2-3-4-5 | 添加 `_is_wheel()` 检测（同 `engine/judge.py`） |
| **High** | 翻前范围构建 | 简单线性评分，open 阈值 ~0.46 | 需要位置特定范围表、对手倾向自适应、正确的防守频率 |
| **High** | 对手利用 | 贝叶斯平滑先验过宽（4-8），需 20+ 观察才可信 | 70 手对局中前 15-20 手几乎无利用能力 |
| **High** | 最小加注边界 bug | `min_raise_action` 允许 2x 而非 >2x | 修复为 `2 * last_raise_to + 1`（整数）或浮点比较 |
| Medium | 位置意识 | 调整幅度仅 0.015-0.02 | HU 中 SB/BB 的双重性质未正确建模 |
| Medium | 下注尺度优化 | 过多交互修饰符可能矛盾 | 几何尺度、超池下注、pot-commitment 感知 |
| Medium | 河牌诈唬/价值平衡 | 纯 blocker 逻辑 | 需要极化范围构建、GTO 频率参考 |
| Medium | 蒙特卡洛 equity | 500-900 次模拟，~3-5% 标准误差 | 方差缩减技术（对偶变量、分层采样） |
| Low | 短筹码策略 | 不需要 | 每手重置 20000 筹码，无短筹码场景 |

### 最新扑克 AI 研究方法

| 方法 | 适用性 | 关键参考 |
|------|--------|---------|
| DMC 自博弈 + 对手建模 | **直接适用** — 本项目 `rl/` 已有框架 | DouZero (ICML 2021), DanLM, SDMC (国赛 GuanDan 亚军) |
| CFR + GPU 加速 (Supremus) | **补充方案** — 生成参考策略 | Supremus/DCFR+ |
| LLM 扑克 Agent | **已采用** — 当前演化流水线核心 | PokerBench (AAAI 2025), PokerSkill |
| 安全对手利用 | **核心策略** — 竞赛对抗关键 | Counter Strategies (2025), 在线适应 |
| 端到端 RL (AlphaHoldem) | **可作为轻量 RL 基线** | AlphaHoldem (AAAI 2022) |
| GTO Solver 引导下注尺度 | **用于生成翻前范围和翻后基线** | PioSolver/GTO+ |

---

## 集成计划：六大阶段

### 阶段 1：紧急 Bug 修复 + 流水线快速修复（P0，~4h）

**目标**：修复所有已知 bug，消除最严重的流水线浪费源。

| 任务 | 文件 | 工作量 | 描述 |
|------|------|--------|------|
| **1.1** Wheel straight 检测 bug | `bots/claude_v{N}/card_utils.py` | S | 在 `evaluate_5()` 中添加 `_is_wheel()` 检测（与 `engine/judge.py` 一致），将 `[14,5,4,3,2]` 正确识别为顺子 |
| **1.2** Re-raise 严格 > 2x bug | `bots/claude_v{N}/state.py` | S | 修改 `min_raise_action` 公式：`min_total = last_raise_to * 2 + 1`（确保严格大于 2x） |
| **1.3** 熔断器计数修复 | `web/core/tool_planning.py` | S (1行) | `failure_count += len(tasks)` → `failure_count += actual_failed_count` |
| **1.4** 决策测试场景重新分类 | `web/core/decision_tester.py` | S | 将 12/15 降至 8/15，降级 `flop_flush_draw_facing_cbet`、`river_missed_draw_facing_big_bet`、`turn_two_pair_facing_bet`、`river_top_pair_facing_overbet` |
| **1.5** Worker 提示词加强 | `web/core/prompts/worker_prompt.md` | S | 添加"必须使用 Edit 工具修改代码"硬性指令，添加禁止操作白名单 |
| **1.6** 删除死代码 | `web/core/evolution_infra.py` | S | 移除未使用的 `MAX_PARALLEL_WORKERS`、`_WORKER_SEMAPHORE`、`_get_worker_semaphore()`（或标记为未来并行化预留） |

**完成标准**：
- `evaluate_5()` 正确识别 A-2-3-4-5 顺子
- 所有 re-raise 最小值严格 > 2x previous raise
- 熔断器按实际失败数计数
- CRITICAL 场景 ≤ 8/15
- 所有现有测试通过

---

### 阶段 2：流水线并行化与看门狗（P0，~8h）

**目标**：将流水线有效工作时间从 25% 提升至 60%+，消除多小时间歇停顿。

| 任务 | 文件 | 工作量 | 描述 |
|------|------|--------|------|
| **2.1** 编排器活跃看门狗 | `web/core/orchestrator.py` | M (~100行) | 后台协程监控 `pipeline_state.json` 的 `last_stage_change` 时间戳。超过 5 分钟无进展 → 清除过期会话、基于检查点重启循环。添加 `pipeline_state["last_stage_change"]` 写入点 |
| **2.2** Worker 并行执行 | `web/core/agent_workers.py` | M | 当 Worker 的 `target_files` 集合不相交时，使用 `asyncio.gather` 并行执行。冲突检测：比较两个 Worker 的 `target_files` 集合。冲突时退化为串行。更新 `worker_snapshots` 逻辑处理并发快照 |
| **2.3** 预提交评估并行化 | `web/core/tool_eval.py` | M | 将 `tool_eval.py:102-165` 的串行 `for` 循环改为 `asyncio.gather` + `run_in_executor`，同时运行所有对手的镜像对战。每个 `mirror_battle` 子进程隔离，无状态冲突。预计节省 60-80% 预提交评估时间 |
| **2.4** 冒烟测试策略优化 | `web/core/code_verification.py`, `web/core/smoke_tester.py` | S | Worker 重试循环中仅运行编译检查，跳过冒烟测试。仅在最终质量门（进入 Review 前）运行完整冒烟测试。每次重试节省 60-120s |
| **2.5** 方向审计简化 | `web/core/tool_planning.py`, `web/core/direction_auditor.py` | S | 当方向审计检测到重复时，在返回结果中明确标注"此为最终结果，请勿重试"，阻止编排器 LLM 反复调用。审计结果直接注入 Master 约束 |
| **2.6** 守护进程崩溃诊断 | `web/core/elo_daemon.py` | M | 添加结构化错误日志（记录异常堆栈到 `daemon_crash.log`），添加内存监控（`resource` 模块），`BrokenProcessPool` 恢复中添加详细错误信息 |

**完成标准**：
- 编排器间歇停顿 > 5 分钟自动恢复
- Worker 非冲突时并行执行
- 预提交评估 4 个对手并行（~300s → ~75s）
- 冒烟测试仅在最终质量门运行
- 单代次理想时间 < 15 分钟（从 25m34s 基线）

---

### 阶段 3：策略引擎深度升级（P1，~16h）

**目标**：将 bot 策略从 LLM 启发式提升到接近 GTO 的竞赛级别。

| 任务 | 文件 | 工作量 | 描述 |
|------|------|--------|------|
| **3.1** 翻前范围表构建 | `bots/claude_v{N}/strategy.py`, `constants.py` | L | 基于 GTO solver 输出构建 HU 位置特定翻前范围表（SB open/3bet/call, BB defend/3bet/4bet）。替换当前简单线性评分为查表法。范围应包含 bluff 3bet 频率和防守频率 |
| **3.2** 对手建模加速收敛 | `bots/claude_v{N}/opponent_model.py` | M | 降低先验权重从 4-8 到 1-2，加速早期收敛。前 10 手使用更激进的先验（基于对手第一手行为快速分类）。实现 "fast start" 模式：初始分类为 LAG/TAG/passive/unknown，后续调整 |
| **3.3** 几何下注尺度 | `bots/claude_v{N}/sizing.py` (新) | M | 实现多街几何下注尺度（geometric sizing）：对于 N 街价值下注，每街下注比例 = (最终底池/初始底池)^(1/N)。添加超池下注（overbet）能力用于坚果牌场景。添加 pot-commitment 感知 |
| **3.4** 位置感知增强 | `bots/claude_v{N}/strategy.py` | M | SB: 翻前主动但翻后先行动（需更强的 check-raising 和 donk-betting）。BB: 翻后位置优势（需更宽的漂浮跟注和延迟加注）。添加 donk-bet 策略（BB 翻后首行动主动下注） |
| **3.5** 蒙特卡洛方差缩减 | `bots/claude_v{N}/equity.py` | S | 添加对偶变量采样（antithetic variates）和分层采样（stratified sampling）。将 500-900 次模拟的误差从 ~3-5% 降至 ~1-2% |
| **3.6** 河牌极化范围策略 | `bots/claude_v{N}/river_strategy.py` (新) | M | 基于 GTO 频率参考构建极化范围。价值下注范围：顶对+ 的 thinner value。诈唬范围：blocker 选择 + 频率控制。实现 OBFUSCATION 模式（混合不同尺度的价值/诈唬） |
| **3.7** 安全对手利用框架 | `bots/claude_v{N}/exploit.py` (新) | L | 基于 Counter Strategies (2025) 论文：从近似纳什均衡出发，在线检测对手偏离，安全地偏向利用。实现 best-response 计算（基于对手观测频率），限制偏离纳什均衡的幅度（安全边界） |

**完成标准**：
- 翻前范围覆盖 SB/BB 所有情况
- 对手模型在 10 手内产生有用信号
- 下注尺度包含几何、超池、pot-commitment
- 蒙特卡洛误差 < 2%
- 河牌策略包含极化范围和频率控制

---

### 阶段 4：RL 训练集成（P1，~24h）

**目标**：将 `rl/` 模块（DanLM-style DMC self-play）集成到演化系统，产生 LLM 无法发现的策略创新。

| 任务 | 文件 | 工作量 | 描述 |
|------|------|--------|------|
| **4.1** RL 训练流水线集成 | `web/core/tool_pipeline.py`, `rl/scripts/train.py` | L | 在演化流水线中添加 RL 训练阶段（可选）。`tool_pipeline.py` 中注册 `run_rl_training()` MCP tool。编排器可以选择触发 RL 训练来生成新策略参数，注入到 LLM 演化的 bot 中 |
| **4.2** RL 训练配置优化 | `rl/core/config.py` | M | 优化训练超参数：增加 cycle 数量、调整 N/k/S 比例。针对 HU NL Hold'em 特性调整 reward shaping（考虑底池大小、位置优势） |
| **4.3** RL Bot ↔ 演化 Bot 对抗 | `engine/battle.py`, `web/core/elo_daemon.py` | M | 在 Glicko-2 daemon 中添加 RL bot 对手。RL bot（`rl/scripts/rl_bot.py`）作为固定对手参与镜像对战，提供不同于 LLM bot 的对抗多样性 |
| **4.4** 策略知识蒸馏 | `bots/claude_v{N}/` (新模块) | XL | 将 RL 训练得到的 Q 值网络蒸馏为启发式规则：提取 RL bot 在常见场景中的最优动作，转化为 LLM bot 的策略常量。实现策略参数的自动注入 |
| **4.5** Transformer Q-Network 训练 | `rl/models/transformer.py` | L | 训练 Transformer Q-Network（DanLM-style），利用 tokenized game history。训练周期目标：至少 10M 手自博弈对局 |
| **4.6** 训练中断/恢复 | `rl/training/trainer.py` | M | 实现训练 checkpoint 保存/恢复，支持中断后继续训练。添加定期评估（每 100K 手对抗当前最佳 LLM bot） |

**完成标准**：
- 编排器可以触发 RL 训练作为演化策略之一
- RL bot 作为固定对手参与评分
- RL 训练可持续运行不中断
- RL bot 对抗当前最佳 LLM bot 胜率 ≥ 55%

---

### 阶段 5：大规模对抗测试与强化（P2，~持续运行）

**目标**：通过大规模自对弈发现弱点，迭代修复，持续提升。

| 任务 | 文件 | 工作量 | 描述 |
|------|------|--------|------|
| **5.1** 弱点发现流水线 | `web/core/combined_analyst.py` | L | 增强 combined_analyst：除停滞分析外，添加弱点模式分析（哪些牌局类型输最多？哪些位置被 exploit？）。输出结构化弱点报告，直接指导下一轮演化 |
| **5.2** 多对手种群演化 | `web/core/elo_daemon.py`, `web/core/generation_scheduler.py` | XL | 扩展对手种群：不仅 LLM bot 之间对战，还引入参考 bot（bot1-bot6）、RL bot、和外部 bot（如 Botzone 排名靠前的 bot）。种群多样性保证演化方向不陷入局部最优 |
| **5.3** 交叉变异策略优化 | `web/core/tool_commit.py`, `web/core/agent_review.py` | M | 增强交叉变异：支持 3-parent crossover（从 3 个不同 bot 各取优势策略）。添加基于 RL 评估的 parent 选择（不仅看 Glicko 评分，还看特定场景下的对抗表现） |
| **5.4** 场景测试扩展 | `web/core/decision_tester.py`, `web/core/test_scenarios.json` | M | 从 15 个场景扩展到 30-50 个。添加：wheel straight 相关场景、re-raise 边界场景、short-stack 场景、donk-bet 场景、overbet 场景。添加对手类型特定场景 |
| **5.5** 比赛回放分析增强 | `web/core/replay_analysis.py`, `web/core/commentary.py` | M | 增强回放分析：自动标注"关键决策点"（大底池、弃牌后对方亮牌、cooler）。生成对手类型分析报告。注入到下一轮 Master Architect 的上下文中 |
| **5.6** 持续监控仪表板 | `web/frontend/src/pages/` | M | 前端添加 RL 训练监控页面、弱点分析可视化、策略演进时间线。实时显示训练 loss、胜率曲线、弱点热力图 |

**完成标准**：
- 每轮演化都产生弱点分析报告
- 对手种群 ≥ 5 种类型
- 场景测试 ≥ 30 个
- 关键决策点自动标注率 ≥ 80%

---

### 阶段 6：竞赛准备与部署（P2，~8h）

**目标**：最终调优，Botzone/国赛平台适配，实战验证。

| 任务 | 文件 | 工作量 | 描述 |
|------|------|--------|------|
| **6.1** bot_adapter.py 全面验证 | `sever/bot_adapter.py` | M | 验证所有竞赛规则在 adapter 层的正确性：re-raise 严格 > 2x、非法行为过滤、allin 边界。添加 adapter 层的安全网（即使 bot 逻辑出错，adapter 也能输出合法动作） |
| **6.2** 国赛平台 TCP 协议测试 | `sever/tests/` | M | 使用 `sever/` 的 TCP 服务器进行完整对抗测试。模拟国赛平台 70 手对局，验证所有边界情况 |
| **6.3** Botzone 上传与排名 | `scripts/botzone_upload_match.py` | S | 上传当前最佳 bot 到 Botzone，参与排名赛。收集实战数据 |
| **6.4** 最终 bot 打包 | `merge_bot.py`, `bots/claude_v{N}/` | S | 使用 `merge_bot.py` 合并最终 bot 为单文件。验证单文件 bot 在 subprocess 协议下的正确性 |
| **6.5** 竞赛参数调优 | `bots/claude_v{N}/constants.py` | S | 基于 Botzone 实战数据微调关键参数：翻前范围阈值、下注尺度比例、bluff 频率 |

**完成标准**：
- bot_adapter.py 所有 13 条规则 100% 合规
- 国赛平台完整 70 手对局无非法行为
- Botzone 排名 ≥ 前 50%（目标：前 10%）
- 单文件 bot 通过所有决策测试

---

## 风险与缓解

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| LLM Worker 反复零修改 | 高 | 浪费时间和 token | 已有双层检测 + 提示词加强；极端情况下自动降级为交叉变异 |
| 编排器会话崩溃 | 中 | 多小时停顿 | 阶段 2.1 添加看门狗自动恢复 |
| RL 训练不收敛 | 中 | 无 RL 策略改进 | 保留 MLP Q-Network 基线；使用 DanLM 预训练 checkpoint 作为起点 |
| GTO solver 计算资源不足 | 低 | 翻前范围质量受限 | 使用公开 GTO solver 数据集（GTO Wizard）；降低分辨率（169 × 169 → 组合索引） |
| 竞赛规则理解偏差 | 低 | 非法行为导致输牌 | adapter 层安全网 + 全面 TCP 测试 |
| 策略过于复杂导致推理超时 | 中 | 60s 超时弃牌 | 性能测试：确保每个决策 < 5s（目标 < 1s）。添加计时器和 fallback |

---

## 预期成果

| 指标 | 当前 | 目标 |
|------|------|------|
| 流水线有效工作时间占比 | 25% | 60%+ |
| 标准流水线成功率 | 16.7% (1/6) | 60%+ |
| 单代次理想时长 | 25m34s | < 15m |
| 单代次最差时长 | 47h23m | < 2h |
| Worker 零修改率 | ~30% | < 10% |
| 决策测试通过率 | 93% (1 场景失败阻塞) | 95%+ (降低关键场景门槛) |
| 预提交评估耗时 | 1200s (串行 4 对手) | < 300s (并行) |
| Wheel straight 检测 | ❌ 不识别 | ✅ 正确识别 |
| Re-raise 边界 | ❌ 允许 2x | ✅ 严格 > 2x |
| 对手模型收敛速度 | 20+ 手才可信 | 10 手内有用信号 |
| 翻前范围质量 | 简单线性评分 | GTO solver 参考范围表 |
| RL 训练集成 | ❌ 独立模块 | ✅ 集成到演化流水线 |

---

## 实施顺序

```
阶段 1（4h）：紧急 bug + 快速修复
    ↓
阶段 2（8h）：流水线并行化 + 看门狗
    ↓
阶段 3（16h）：策略引擎深度升级 ← 可与阶段 2 部分并行
    ↓
阶段 4（24h）：RL 训练集成 ← 阶段 3 完成后开始
    ↓
阶段 5（持续）：大规模对抗测试 ← 阶段 4 有初步结果后开始
    ↓
阶段 6（8h）：竞赛准备与部署 ← 贯穿全过程，最终确认
```

**总预估时间**：60h 编码 + 持续 RL 训练（可 24/7 运行）

**立即可开始**：阶段 1 的 6 个任务（全部 P0，无依赖关系，可并行执行）。
