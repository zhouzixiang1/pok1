# 演化流水线瓶颈修复与竞赛级 Bot 强化集成计划

> 基于 pipeline-bottleneck-analysis.md 全面核实 + 5-agent 综合审计结果（2026-06-09）。
> 目标：不计 token 成本，持续运行，产出国赛级别最强 Texas Hold'em bot。
> 审计规模：5 阶段、5 agent、161 次工具调用、~33min 运行时。

---

## 系统健康评分

| 子系统 | 评分 | 状态 |
|--------|------|------|
| 🟢 引擎合规 | **95/100** | 优秀 — 双引擎 raise 语义、13 条规则、card 转换全部验证通过 |
| 🟢 评级系统 | **88/100** | 健康 — 143,225 场比赛，评分范围 1475-1717，全部 confident 层级 |
| 🟡 流水线 | **75/100** | 可用但需加固 — 10 个瓶颈中 4 个已修、6 个未修 |
| 🔴 Bot 代码 | **62/100** | 需修复 — 1 critical + 1 high bug 直接影响比赛胜负 |
| **总体** | **78/100** | **修复 P0 bug 后可安全进行持续演化** |

---

## 一、核实结果摘要

### 1.1 瓶颈分析报告 10 项声明核实（第一轮）

| # | 声明 | 核实 | 当前状态 |
|---|------|------|---------|
| 1 | Worker 串行执行，信号量是死代码 | ✅ 已确认 | `agent_workers.py:166-235` 串行 for 循环，`MAX_PARALLEL_WORKERS`/`_WORKER_SEMAPHORE` 从未被调用 |
| 2 | 熔断器按 len(tasks) 递增 | ✅ 已确认 | `tool_planning.py:363-371` + `:489`，行号偏移但缺陷不变 |
| 3 | 12/14 场景为 CRITICAL | ⚠️ 实际 12/15 | 比例仍然过高，关键-建议分离机制已存在但几乎无用 |
| 4 | 编排器缺乏看门狗 | ✅ 已确认 | 无任何 watchdog/liveness/heartbeat 机制 |
| 5 | 预提交评估串行执行 | ✅ 已确认 | `tool_eval.py:102-165` 串行 for 循环 |
| 6 | 方向审计 48% 误报率 | ⚠️ 无法从代码验证 | 需运行时日志统计，但 LLM 驱动的检测机制未变 |
| 7 | 零修改检测机制存在 | ✅ 已确认 | `agent_workers.py:122-142` + `tool_planning.py:429-436` 双层检测 |
| 8 | STAGE_GATE_ALLOWLIST 硬依赖 | ✅ 已确认 | 定义在 `evolution_infra.py:76-86`，通过 `tool_helpers.py` 的 `_xxx_gate_ok` 执行 |
| 9 | 角色边界违规是最常见拒因 | ⚠️ 无法从代码验证 | 但代码中已有多层边界执行（master plan + worker prompt + post-worker + reviewer） |
| 10 | 冒烟测试 60-120s | ✅ 已确认 | `smoke_tester.py:23` 1 次 mirror_battle（2 局 × 70 手） |

### 1.2 瓶颈修复进度（第二轮审计更新）

| # | 瓶颈 | 状态 | 证据 |
|---|------|------|------|
| 1 | 编排器看门狗 | ❌ **未修复** | 搜索 `watchdog`/`liveness`/`heartbeat` 零结果 |
| 2 | 决策测试场景重分类 | ❌ **未修复** | 12/15 仍为 CRITICAL |
| 3 | Worker 并行执行 | ❌ **未修复** | `_WORKER_SEMAPHORE` 定义但从未在 `agent_workers.py` 中使用 |
| 4 | 预提交评估并行化 | ❌ **未修复** | `tool_eval.py:102` 仍为串行 `for` 循环 |
| 5 | Worker 零修改率优化 | ✅ **已修复** | 多层缓解：per-worker 检测 + pre-gate + 失败记忆注入 |
| 6 | 熔断器计数修复 | ❌ **未修复** | `tool_planning.py:375,499` 仍用 `len(tasks)` |
| 7 | Worker 角色边界加强 | ✅ **已修复** | master plan 验证 + 文件重叠检测 + snapshot 边界检查 + 选择性重置 |
| 8 | 方向审计简化 | ✅ **已修复** | 结果缓存 + 约束直接注入 + `resolved` 标志阻止重试 |
| 9 | 守护进程崩溃修复 | ✅ **已修复** | `MAX_POOL_RECOVERIES=3` + `BrokenProcessPool` 恢复 + 自动重启 |
| 10 | 冒烟测试超时优化 | ❌ **未修复** | 仍在每次质量门运行完整 mirror_battle |

**总结：4/10 已修复，6/10 未修复。**

### 1.3 评级系统澄清

> ⚠️ 初步观察到的"所有评分为 0"是**误报**。实际数据：评分范围 1475-1717，RD 64-84，22 个 bot 全部处于 confident 层级（RD 50-100）。143,225 场比赛已累计。可能原因：前端显示 bug、瞬态、或 rating_history.jsonl 旋转后旧数据不可见。

---

## 二、综合审计发现（16 项）

### 2.1 全部发现一览

| 严重级别 | 数量 | ID 范围 |
|---------|------|---------|
| **Critical** | 1 | BOT-001 |
| **High** | 1 | BOT-002 |
| **Medium** | 5 | BOT-003, BOT-004, PIPE-001, PIPE-002, PIPE-003 |
| **Low** | 9 | RATE-001~004, PIPE-004~007, ENG-001 |

### 2.2 Critical

| ID | 问题 | 文件:行 | 影响 | 修复 |
|----|------|---------|------|------|
| **BOT-001** | Wheel straight (A-2-3-4-5) **从不被识别为顺子** | `card_utils.py:35-38` | ~2-3% 的牌局严重错判：A-2-3-4-5 被判为高牌 (class 0, metric 0.08) 而非顺子 (class 4, metric 0.93)。同时影响 `postflop.py` 的 draw 检测和 board texture 分析 | 第 38 行后添加 `if set(ranks) == {14,2,3,4,5}: is_straight = True; straight_high = 5`。同步修复 `postflop.py:160-168, 583-594` 的 wheel draw 检测 |

### 2.3 High

| ID | 问题 | 文件:行 | 影响 | 修复 |
|----|------|---------|------|------|
| **BOT-002** | Re-raise 最小值使用 ≥ 2x 而非严格 > 2x | `state.py:242` | 每次 3bet/4bet/加注-再加注可能被引擎判为非法 → 自动弃牌。engine/judge.py 确认正确拒绝 ==2x | `min_raise_action = max(0, 2 * last_raise_to - my_round_bet + 1)` |

### 2.4 Medium

| ID | 问题 | 文件:行 | 修复 |
|----|------|---------|------|
| **BOT-003** | `sanitize_action` 将 call(0) 转为 fold(-1) 而非 allin(-2) | `main.py:17-18` | `return 0 if action == 0 else (-2 if action == -2 else -1)` |
| **BOT-004** | `TOTAL_HANDS = 50` 但实际比赛为 70 手 | `constants.py:5` | 改为 `TOTAL_HANDS = 70` |
| **PIPE-001** | 熔断器按 `len(tasks)` 而非实际失败数累加 | `tool_planning.py:375,499` | 用实际失败任务数替代 `len(tasks)` |
| **PIPE-002** | `_git()` 无 timeout，可无限挂起 | `evolution_infra.py:511-513` | 添加 `timeout=30` 参数 |
| **PIPE-003** | `clear_pipeline_checkpoint()` 无锁，与写入存在竞态 | `evolution_infra.py:252-254` | 使用 `locked_file()` 或版本校验 |

### 2.5 Low

| ID | 问题 | 修复 |
|----|------|------|
| **RATE-001** | H2H 矩阵淘汰时裁剪但 bot_stats 未同步裁剪 | 淘汰时同步清理 bot_stats |
| **RATE-002** | Sigma 固定为 0.06 从不更新 | 每 1000 场调用 batch `update_rating_period` |
| **RATE-003** | `rating_history.jsonl` 无轮转/上限 | 添加定期轮转（~3200 行/天） |
| **RATE-004** | Daemon 崩溃恢复异常处理器递归调用 | 添加深度保护 |
| **PIPE-004** | `critic_calibration.jsonl` 写入无 fcntl 锁 | 替换为 `locked_file()` |
| **PIPE-005** | `tool_eval.py` 死代码：CORE_DIR 检查永远为 True | 删除不可达 else 分支 |
| **PIPE-006** | `tool_eval.py` n_games 上限从 5 增至 15 可能超时 | 评估是否需要或添加总体超时保护 |
| **PIPE-007** | 测试覆盖率缺口：`tool_eval.py` 零测试引用 | 补充单元测试 |
| **ENG-001** | `bot_adapter` 无客户端侧加注验证 | 添加最小加注验证，非法时 clamp |

---

## 三、引擎合规性审计

### 3.1 engine/judge.py — 全部通过

| 检查项 | 结果 |
|--------|------|
| Re-raise strictly > 2x | ✅ `line 364-368`: `raise_to <= self.last_raise_to * 2` 正确拒绝 ==2x |
| First raise 允许 == 2x baseline | ✅ `line 367-368`: preflop 200, postflop 100 合法 |
| Allin 规则 | ✅ 连续 allin、raise 后 allin、全筹码必须 allin |
| 非法 call/check | ✅ 翻后首行动 call、翻前 BB call after SB call 自动弃牌 |
| Raise-to-total 语义 | ✅ `bet > 0` = raise-to-total，`last_raise_to` 正确跟踪 |

### 3.2 sever/engine/validator.py — 13 条规则全部通过

| 规则 | 行号 | 状态 |
|------|------|------|
| 1. bet 永远非法 | 33-34 | ✅ |
| 2. 翻后首行动 call 非法 | 53-55 | ✅ |
| 3. 翻前 BB call after SB call 非法 | 57-59 | ✅ |
| 4. 翻后非首行动 check 非法 | 75-81 | ✅ |
| 5. 翻前 check 仅 BB 首行动 | 63-71 | ✅ |
| 6. 翻前 SB 首 raise ≥ 200 | 106-109 | ✅ |
| 7. 翻前 BB raise after SB raise > 2x | 118-121 | ✅ |
| 8. 连续 raise > 2x | 128-132 | ✅ |
| 9. 翻后首 raise ≥ 100 | 123-126 | ✅ |
| 10. Raise 超过筹码非法 | 98-100 | ✅ |
| 11. Raise = 全筹码必须 allin | 95-97 | ✅ |
| 12. Allin 后仅 call/fold | 101-103 | ✅ |
| 13. 连续 allin 非法 | 85-88 | ✅ |

### 3.3 bot_adapter.py — 功能正确但缺少安全网

| 检查项 | 结果 |
|--------|------|
| Card 转换 | ✅ 52 张牌双向验证，bijective |
| Action 映射 | ✅ raise-to-total 无需转换 |
| 客户端验证 | ❌ **缺失** — 完全依赖服务器验证，bot 错误 → auto-fold 而非纠正 |

---

## 四、Bot 策略质量评估

**综合评级：B+**

| 维度 | 评价 | 改进优先级 |
|------|------|-----------|
| 翻前范围 | SB open ~75%（略紧于最优 85%+），BB defend ~86% 合理。3bet 以 AA/AK/AQs+ 为主，bluff 3bet ~25% 频率 | Medium |
| 翻后逻辑 | 分层决策系统：锦标赛压力 → 对手模型 → 手牌评估 → 听牌分析 → 牌面纹理。Fold 阈值按街递增，设计良好 | Low |
| 对手建模 | 贝叶斯平滑 + 每街画像，~15 手收敛。Aligned signal boost 检测可靠模式 | Medium |
| 下注尺度 | 0.55-0.85 底池比，合理但缺少几何/超池/pot-commitment | Medium |
| 诈唬频率 | 保守型 5-18%（对手依赖），可能对弱对手诈唬不足 | Medium |
| 手牌评估 | 除 wheel straight bug 外逻辑清洁。Value tier 分类结构良好 | **Critical** (bug) |
| 代码质量 | 3,263 行，strategy.py 1282 行（在 1500 限制内）。5 处死代码 | Low |

### 死代码
- `postflop.py:479` — `straight_draw_value()` 定义但从未调用
- `strategy.py:26` — `_per_street_diverges()` 定义但从未调用
- `strategy.py:2` — `draw_potential` 导入但未使用
- `opponent.py:1` — `N_PLAYERS` 导入但未使用

---

## 五、集成计划：六大阶段

### 阶段 1：紧急 Bug 修复 + 流水线快速修复（P0，~2h）

**目标**：修复所有已知 critical/high bug，消除最严重的流水线浪费源。

| 任务 | 文件 | 工作量 | 修复 ID | 描述 |
|------|------|--------|---------|------|
| **1.1** | `bots/claude_v{N}/card_utils.py` | S (3处) | BOT-001 | `evaluate_5()` 第 38 行后添加 wheel straight 检测。同步修复 `postflop.py:160-168, 583-594` 的 draw 检测和 board texture 分析 |
| **1.2** | `bots/claude_v{N}/state.py` | S (1行) | BOT-002 | `min_raise_action = max(0, 2 * last_raise_to - my_round_bet + 1)` |
| **1.3** | `bots/claude_v{N}/main.py` | S (1行) | BOT-003 | `sanitize_action`: call(0) 在 to_call >= chips 时应保持 0（all-in call）而非转为 fold |
| **1.4** | `bots/claude_v{N}/constants.py` | S (1行) | BOT-004 | `TOTAL_HANDS = 70` |
| **1.5** | `web/core/tool_planning.py` | S (2行) | PIPE-001 | 第 375、499 行 `len(tasks)` → 实际失败任务数 |
| **1.6** | `web/core/evolution_infra.py` | S (1行) | PIPE-002 | `_git()` 的 `subprocess.run()` 添加 `timeout=30` |
| **1.7** | `web/core/evolution_infra.py` | S | PIPE-003 | `clear_pipeline_checkpoint()` 使用 `locked_file()` 或版本校验 |
| **1.8** | `web/core/decision_tester.py` | S | 瓶颈#2 | 将 12/15 降至 8/15 CRITICAL。降级 4 个场景（听牌跟进、失败听牌弃牌、中等牌力处理、顶对 facing overbet） |
| **1.9** | `web/core/evolution_infra.py` | S | — | 删除或标记死代码：`_WORKER_SEMAPHORE`、`_get_worker_semaphore()`、无用 re-export |

**完成标准**：
- [ ] `evaluate_5()` 正确识别 A-2-3-4-5 顺子（wheel straight 测试通过）
- [ ] 所有 re-raise 最小值严格 > 2x previous raise
- [ ] `sanitize_action` 不再将 call 转为 fold
- [ ] `TOTAL_HANDS = 70`
- [ ] 熔断器按实际失败数计数
- [ ] `_git()` 有 30s 超时
- [ ] `clear_pipeline_checkpoint()` 有 fcntl 锁保护
- [ ] CRITICAL 场景 ≤ 8/15
- [ ] 所有现有测试通过（386 tests）

---

### 阶段 2：流水线并行化与看门狗（P1，~8h）

**目标**：将流水线有效工作时间从 25% 提升至 60%+，消除多小时间歇停顿。

| 任务 | 文件 | 工作量 | 瓶颈# | 描述 |
|------|------|--------|-------|------|
| **2.1** | `web/core/orchestrator.py` | M (~100行) | #1 | 后台协程监控 `pipeline_state["last_stage_change"]`。超过 5 分钟无进展 → 清除会话、基于检查点重启 |
| **2.2** | `web/core/agent_workers.py` | M | #3 | Worker `target_files` 不相交时 `asyncio.gather` 并行。冲突时退化为串行 |
| **2.3** | `web/core/tool_eval.py` | M | #4 | 对手循环改为 `asyncio.gather` + `run_in_executor`。预计 1200s → ~300s |
| **2.4** | `web/core/code_verification.py`, `smoke_tester.py` | S | #10 | Worker 重试循环仅编译检查，最终质量门才运行冒烟测试 |
| **2.5** | `web/core/tool_planning.py` | S | — | 清理死代码：`tool_eval.py:95,266` 永远为 True 的 CORE_DIR 检查 |

**完成标准**：
- [ ] 编排器间歇停顿 > 5 分钟自动恢复
- [ ] Worker 非冲突时并行执行
- [ ] 预提交评估 4 个对手并行（~1200s → ~300s）
- [ ] 冒烟测试仅在最终质量门运行
- [ ] 单代次理想时间 < 15 分钟

---

### 阶段 3：策略引擎深度升级（P1，~16h）

**目标**：将 bot 策略从 LLM 启发式提升到接近 GTO 的竞赛级别。

| 任务 | 文件 | 工作量 | 描述 |
|------|------|--------|------|
| **3.1** | `bots/claude_v{N}/strategy.py`, `constants.py` | L | 翻前范围表构建：基于 GTO solver 输出的 HU 位置特定范围（SB open/3bet/call, BB defend/3bet/4bet）。替换简单线性评分为查表法 |
| **3.2** | `bots/claude_v{N}/opponent.py` | M | 对手建模加速：降低先验权重 4-8→1-2，前 10 手 fast start 分类（LAG/TAG/passive/unknown） |
| **3.3** | `bots/claude_v{N}/strategy.py` (新 sizing 模块) | M | 几何下注尺度 + 超池下注 + pot-commitment 感知 |
| **3.4** | `bots/claude_v{N}/strategy.py` | M | 位置感知增强：SB donk-bet/check-raise，BB 漂浮跟注/延迟加注 |
| **3.5** | `bots/claude_v{N}/simulation.py` | S | 蒙特卡洛方差缩减：对偶变量 + 分层采样，误差 3-5%→1-2% |
| **3.6** | `bots/claude_v{N}/strategy.py` (river 部分) | M | 河牌极化范围策略：GTO 频率参考、blocker 选择、OBFUSCATION 模式 |
| **3.7** | `bots/claude_v{N}/exploit.py` (新) | L | 安全对手利用框架：近似纳什均衡 + 在线检测偏离 + 安全边界限制 |
| **3.8** | `sever/bot_adapter.py` | S | 添加客户端侧最小加注验证，非法时 clamp 到合法最小值（ENG-001） |

**完成标准**：
- [ ] 翻前范围覆盖 SB/BB 所有情况
- [ ] 对手模型在 10 手内产生有用信号
- [ ] 下注尺度包含几何、超池、pot-commitment
- [ ] 蒙特卡洛误差 < 2%
- [ ] 河牌策略包含极化范围和频率控制
- [ ] bot_adapter 有客户端安全网

---

### 阶段 4：RL 训练集成（P2，~24h）

**目标**：将 `rl/` 模块（DanLM-style DMC self-play）集成到演化系统。

| 任务 | 文件 | 工作量 | 描述 |
|------|------|--------|------|
| **4.1** | `web/core/tool_pipeline.py`, `rl/scripts/train.py` | L | 注册 `run_rl_training()` MCP tool，编排器可选触发 RL 训练 |
| **4.2** | `rl/core/config.py` | M | 优化训练超参数：cycle N/k/S 调整、reward shaping |
| **4.3** | `web/core/elo_daemon.py` | M | RL bot 作为固定对手参与镜像对战，提供对抗多样性 |
| **4.4** | `bots/claude_v{N}/` (新模块) | XL | 策略知识蒸馏：RL Q值网络 → 启发式规则 → LLM bot 策略常量 |
| **4.5** | `rl/models/transformer.py` | L | 训练 Transformer Q-Network，目标 ≥10M 手自博弈 |
| **4.6** | `rl/training/trainer.py` | M | 训练 checkpoint 保存/恢复，每 100K 手评估 |

**完成标准**：
- [ ] 编排器可以触发 RL 训练
- [ ] RL bot 参与评分
- [ ] RL bot 对抗最佳 LLM bot 胜率 ≥ 55%

---

### 阶段 5：大规模对抗测试与强化（P2，~持续运行）

**目标**：通过大规模自对弈发现弱点，迭代修复。

| 任务 | 文件 | 工作量 | 描述 |
|------|------|--------|------|
| **5.1** | `web/core/combined_analyst.py` | L | 弱点模式分析：哪些牌局类型输最多？输出结构化弱点报告 |
| **5.2** | `web/core/elo_daemon.py` | XL | 多对手种群：LLM bot + 参考bot(bot1-6) + RL bot + 外部bot |
| **5.3** | `web/core/tool_commit.py` | M | 3-parent crossover + RL 评估 parent 选择 |
| **5.4** | `web/core/decision_tester.py` | M | 场景扩展至 30-50 个：wheel、re-raise 边界、donk-bet、overbet |
| **5.5** | `web/core/replay_analysis.py` | M | 关键决策点自动标注 + 对手类型分析报告 |
| **5.6** | `web/frontend/src/pages/` | M | RL 训练监控 + 弱点可视化 + 策略演进时间线 |
| **5.7** | `web/core/elo_daemon.py` | S | rating_history.jsonl 轮转（RATE-003） |

**完成标准**：
- [ ] 每轮演化产生弱点分析报告
- [ ] 对手种群 ≥ 5 种类型
- [ ] 场景测试 ≥ 30 个

---

### 阶段 6：竞赛准备与部署（P2，~8h）

**目标**：最终调优，Botzone/国赛平台适配，实战验证。

| 任务 | 文件 | 工作量 | 描述 |
|------|------|--------|------|
| **6.1** | `sever/bot_adapter.py` | M | 全面验证 + 安全网（即使 bot 逻辑出错也输出合法动作） |
| **6.2** | `sever/tests/` | M | TCP 服务器完整 70 手对局测试，验证所有边界情况 |
| **6.3** | `scripts/botzone_upload_match.py` | S | 上传 Botzone，参与排名赛 |
| **6.4** | `merge_bot.py` | S | 合并最终 bot 为单文件，验证 subprocess 协议 |
| **6.5** | `bots/claude_v{N}/constants.py` | S | 基于 Botzone 实战数据微调参数 |

**完成标准**：
- [ ] bot_adapter 13 条规则 100% 合规 + 客户端安全网
- [ ] 国赛平台完整 70 手对局无非法行为
- [ ] Botzone 排名 ≥ 前 50%（目标前 10%）

---

## 六、风险与缓解

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| LLM Worker 反复零修改 | 高 | 浪费时间和 token | 已有双层检测 + 失败记忆注入；极端情况自动降级为交叉变异 |
| 编排器会话崩溃 | 中 | 多小时停顿 | 阶段 2.1 添加看门狗自动恢复 |
| RL 训练不收敛 | 中 | 无 RL 策略改进 | MLP Q-Network 基线 + DanLM checkpoint 起点 |
| GTO solver 资源不足 | 低 | 翻前范围质量受限 | 公开 GTO solver 数据集 + 降低分辨率 |
| 策略过于复杂超时 | 中 | 60s 超时弃牌 | 性能测试确保 < 5s/决策，添加计时器 + fallback |
| bot_adapter 无安全网 | 中 | bot 错误导致 auto-fold | 阶段 3.8 添加客户端验证 |

---

## 七、预期成果

| 指标 | 当前 | 目标 |
|------|------|------|
| 系统健康评分 | 78/100 | 90+/100 |
| 流水线有效工作时间 | 25% | 60%+ |
| 标准流水线成功率 | 16.7% | 60%+ |
| 单代次理想时长 | 25m34s | < 15m |
| 单代次最差时长 | 47h23m | < 2h |
| Worker 零修改率 | ~30% | < 10%（已缓解） |
| 决策测试关键场景 | 12/15 | 8/15 |
| 预提交评估耗时 | 1200s 串行 | < 300s 并行 |
| Wheel straight 检测 | ❌ 不识别 | ✅ 正确识别 |
| Re-raise 边界 | ❌ 允许 2x | ✅ 严格 > 2x |
| bot_adapter 安全网 | ❌ 无 | ✅ 客户端验证 |
| 对手模型收敛速度 | 15 手 | 10 手内 |
| 翻前范围质量 | 简单线性评分 | GTO 范围表 |
| RL 训练集成 | ❌ 独立模块 | ✅ 集成到演化 |
| Botzone 排名 | 未测试 | ≥ 前 50% |

---

## 八、实施顺序

```
阶段 1（~2h）：紧急 bug 修复 + 流水线快速修复 ← 立即开始，全部 P0
    ↓
阶段 2（~8h）：流水线并行化 + 看门狗
    ↓
阶段 3（~16h）：策略引擎深度升级 ← 可与阶段 2 部分并行
    ↓
阶段 4（~24h）：RL 训练集成 ← 阶段 3 完成后开始
    ↓
阶段 5（持续）：大规模对抗测试 ← 阶段 4 有初步结果后开始
    ↓
阶段 6（~8h）：竞赛准备与部署 ← 贯穿全过程
```

**总预估时间**：~58h 编码 + 持续 RL 训练（24/7 运行）

**立即可开始**：阶段 1 的 9 个任务（全部 P0，大部分无依赖，可并行执行）。

---

*审计完成时间：2026-06-09 | 审计方法：5-agent Workflow（Ratings → Bot Code → Engine → Pipeline → Synthesize）*
*下次审计建议：阶段 1 完成后重新运行，验证所有 P0 修复效果。*
