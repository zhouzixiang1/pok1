# 架构 + 提示词优化审计（2026-06-23）

> 本文基于 7 维度 fan-out 审计（架构 4 + 提示词 2 + 日志 1）→ 逐条对抗验证 → 完整性批判，共 41 agent / 2.7M tokens。
> **与 `evolution-plan-refresh-jun21.md` 的关系**：那份是面向策略根因的 Phase A-D 计划。本文聚焦**架构层 + 提示词层**（非 bot 策略），并核查"Phase A 哪些已真落地、哪些写了没接入、哪些是新缺口"。

---

## 0. 系统现状速览（核查后）

### 已经落地的（不要重做）

`866f6dc "Phase A 止血验证基础设施 + Exa集成 (A1-A7)"` 已把 evolution-plan Phase A **全部落地为框架代码**：

| ID | 内容 | 状态 |
|---|---|---|
| A1 | `battle.py:78-150` _PersistentBot stderr drain 线程 + `elo_daemon` 写 `bot_telemetry/*.jsonl` | ✅ committed |
| A2 | SPR/placement invariant 注入 `master_prompt.md:213` + `experience_pool.md` | ✅ committed |
| A3 | `code_verification.py:129` `detect_placement_shadow_warnings()` AST | ⚠️ committed 但**advisory 非 blocking**（见缺口 #3） |
| A4 | `replay_spotlight.py` SHA-256 锚 + `tool_planning.py:198` `_verify_cited_replays` | ✅ committed **BLOCKING**（仅 master plan 路径，见缺口 #8） |
| A5 | `run_literature_probe` MCP 工具（DeepEvolve plan/search/reflect/write + Exa） | ⚠️ 工具实现了**但没注册到 orchestrator MCP**（见缺口 #1，CRITICAL） |
| A6 | `research_governance.py` Ratchet 治理（ĉ/translation_gate/retire/blacklist/cooldown） | ⚠️ 完整但**运行时零流量**（见缺口 #1） |
| A7 | `exploitability_prober.py:110-182` champion/peer adaptive probe | ✅ committed（对抗验证确认 v146+ 已产出 0.767-0.989 真实信号，非装饰） |

另：`0877786` 修复了硬熔断器（`_CostCapTripped` raise 不再空跑）、precommit stall 自适应、`run_master` schema 5→6 对齐、`_target_rel` 防编辑已 reap bot；`74ed537` 落地了 **M6 telemetry_fidelity BLOCKING gate**（`code_verification.py:347`，已接入 `tool_gates.py`）。

**结论**：策略根因（INERTNESS 写侧 / 0%-fold / fabricated replay 防护）的框架基建已基本就位。**真正剩余的缺口集中在"写了没接入"的半成品 + advisory 信号无法形成闭环 + 评估/演进的速度与校准**。

---

## 1. 架构层优化（按 ROI 排序）

### 🔴 #1 [CRITICAL] A6 research_governance + A5 literature_probe 零流量——用户重点需求"Exa 进进化"形同未上

- **现状**：`research_governance.py` 的 Ratchet 治理（ĉ score / translation_gate / retire N_min=30 / active-cap=5 / blacklist / cooldown / kill-switch）代码完整；`run_literature_probe` 工具也已实现。但对抗验证发现**真根因比 review 说的更糟**：`run_literature_probe` **没有被注册到 orchestrator 的 MCP 工具列表**，LLM 看不到这个工具名 → 永远不会调用。stagnation 触发全靠 `generation_scheduler.py:236` 的自然语言 "You MUST call run_literature_probe"，对 LLM 无约束力。145+ 代来该工具**触发次数 = 0**。
- **改法（一行修复，最高 ROI）**：`web/core/tools.py` 在 `mcp_tools` 列表（约 L79，`run_direction_audit` 之后）加入 `run_literature_probe`。立即让工具对 orchestrator 可见。次要：清理 `web/research_governance_state.json` 里 `cooldown_until_gen=102` 的测试残留（可能误阻断）。
- **文献**：Ratchet (arxiv 2605.19576) 无治理检索 = SkillsBench +0.0pp；DeepEvolve (2510.06056) plan/search/reflect/write。
- **收益**：解锁用户重点需求（Exa 进进化）+ Ratchet 治理终于有流量。工作量 **low**，收益 **high**。

### 🔴 #2 [CRITICAL·新发现] LLM-as-judge 校准闭环物理失效——Critic 的判别力被锁死

- **现状**（completeness-critic 新发现，7 维度 agent 都漏了）：`agent_review.py:59-89` 的 critic calibration 是系统**唯一的 LLM-as-judge 自修正机制**。它的 ground-truth 信号 `rating_delta` 在 commit 时计算（`tool_commit.py:194-206` 用 `conservative_rating()=r-2*rd`），但此刻新 bot 的 rd 仍是初始值 **350**（daemon 还没评估它），`v_cons = r - 700` 恒为极端负值 → 实测 `critic_calibration.jsonl` 90 条里 **88 条 rating_delta=0**（仅 2 条非零）。两条校准分支（`avg_score>7 & avg_delta<0` / `avg_score<4 & avg_delta>0`）在 98% 代里**永不触发**。Critic 的系统性偏差永远无法被自身修正。
- **改法**：把 `rating_delta` 计算从 commit 时**延迟到 daemon 评估收敛（rd<60）后异步回填**（reconcile）。或最小侵入：`tool_commit.py:201` 改用 source_v 的 rd 上下文 + 等 `≥MIN_GAMES_FOR_EVAL` 后 append delta。
- **文献**：Generative Verifiers / Process Reward Models（step-level verification）；self-rewarding LMs 的 judge 自迭代；当前手写 ad-hoc calibration 未引用任何 judge-calibration 方法。
- **收益**：零成本止血 + 解锁 Critic 真实判别力。这是"critic advisory 化后唯一能恢复策略 gate 的非破坏性路径"——不需要恢复 hard gate，只需让 advisory 信号变可信。工作量 **low**，收益 **critical**。

### 🟠 #3 [HIGH] A3 placement_shadow AST 是 advisory——INERTNESS 第二根因活体复发

- **现状**：`detect_placement_shadow_warnings()`（`code_verification.py:129`）已接入 `tool_gates.py:141-154` 的 `run_quality_gates`，但只 `log_system_event(...)` + 存进 result 的 `placement_shadow_warnings` 列表，**不进 `all_passed`、不 return 失败**。对照同文件 M6 telemetry_fidelity 是真 BLOCKING（`all_passed` 含 `telemetry_fidelity_ok`），证明 advisory/blocking 区分真实存在。
- **活体铁证**：`system_events.jsonl` 有 **10 个 placement_shadow 事件覆盖 v156-v165**，每个 gen 标记含 6 个 TRUE-SHADOW `_river_stackoff_guard`（gt0 块）——**这 10 代全部 TAGGED 且 COMMITTED**，detect 了照提交。
- **改法**：`tool_gates.py:287` `all_passed` 加 `and not any('TRUE SHADOW' in w for w in placement_shadow_warnings)`——**仅对 TRUE SHADOW（kind=='gt0'，确定性根因）hard-block**，review/other 良性 case 保持 advisory 避免误伤卡死。复用 M6 已验证的 BLOCKING 范式（写 `worker_failures.jsonl` 给下个 worker 看 + return gate_failed）。
- **文献**：CoCoEvo Stop-n / Wesker 等效变异体检测（"改了行为后测试能否发现差异"= placement shadow 就是测试无法覆盖变异的极端 case）。
- **收益**：堵住 INERTNESS 第二根因。工作量 **low**，收益 **high**。

### 🟠 #4 [HIGH·新发现] 演进速度是未被识别的架构杠杆——79min/cycle 让所有修复验证周期拉到天级

- **现状**（completeness-critic 新发现）：每个 cycle 中位耗时 **78.9 分钟**（max 99.6 分钟）。构成：31 次 precommit scheduler_stall（累计 ~223 分钟 wall-time，**~9min/cycle 浪费在 daemon "0 new results" 轮询**）+ 10 次 `run_master` 同一 cycle 调用 2 次 + 24 次 Bash 重复调用（`system_events` `pipeline.redundant_tool_call`）。当前所有 timeout 治理（cycle_timeout / cost_cap / watchdog）**只防"彻底卡死"，不防"缓慢低效"**。`precommit_eval` scheduler 每 5s 轮询、stall 128 轮（~640s）才 break-fallback，期间 LLM agent 完全 idle 却占着 session。
- **改法**：(a) `precommit_eval` 从 poll-driven（5s/round）改 **event-driven**（daemon 产 match 后回调）；(b) 给 orchestrator `run_master` 加**代码级 idempotency guard**（检测 result 已含 `plan` key 则拒绝 re-call，消除 10 次 2x 重复——`orchestrator.md:43` 已明文禁止但 LLM 间歇违反，证明 prompt-only 约束对控制流 agent 不可靠）；(c) 补 stage 级 timing（master/workers/quality/review/critic/precommit 各阶段耗时），支撑 ROI 排序。
- **收益**：直接砍 15-20min/cycle，进化速率翻倍——这是验证所有其他修复的前提。工作量 **medium**，收益 **high**。

### 🟠 #5 [HIGH] Critic advisory + 跨代方向 pivot 缺失——19 代 exhausted 信号被换名字绕过

- **现状**：`tool_planning.py:367` 注释写 "This is a HARD constraint"，但 L376-378 实际是 `warnings.append(...)` + "advisory only"（`7dc4c3f` 有意降级，为修 hard-gate 误杀合法 novel plan 的反向 bug）。`system_events.jsonl` 实测：**19 代 direction_audit `repetition_detected=True`**（v144-152,155-158,161-164,166）+ 30 次 `worker_exhausted_warning`，**全部照常提交**。`_EXHAUSTED_DIRECTION_TOKENS` 只剩 3 个词（其余因误伤被注释删），hard path 几乎永不触发。
- **改法**（不要简单回退到字面 hard gate，会复发误杀）：三重条件才升 `plan_errors`——`confidence=high AND 连续≥2 次 precommit fail AND exhausted 命中语义轴`（`tool_planning.py:376-378`）。让 verify 直接消费 direction_audit 返回的 `exhausted_directions` **语义列表**（已是"fold-side/commitment-gate axis"等语义轴）而非从经验池 regex 抽 token。配合 #7 的 binding novelty gate。
- **收益**：打破 v138-v165 同轴 28 代死循环。工作量 **medium**，收益 **high**。

### 🟠 #6 [HIGH] Vendi Score + behavior_archive 不进选父——mode collapse 无数值化度量

- **现状**：`web/core/behavior_diversity.py` **不存在**（grep vendi/VS_SCORE/AutoQD/RFF/cwPCA 全 0）。`map_elites.py` 的 18 niches 全 `single-eval`、0 个 k=3 存活过 rebuild（write race），且 `behavior_archive.json` **不进 `_pick_crossover_parents` / `_decide_strategy` / Master prompt**（grep 0 命中）→ QD fitness 完全旁路方向/父选择。`_pick_crossover_parents`（`generation_scheduler.py:602-650`）按 `h2h_avg_wr + 版本差≥3` = **同源近亲杂交**（mode collapse 直接机制）。
- **改法**：(a) 新建 `behavior_diversity.py`：`compute_decision_fingerprint()`（从 decision_tester fixture 抽 7 维 key → RFF → np.float32[D=256]）+ `vendi_score()`（纯 numpy `VS=exp(-Σλlogλ)`，σ 用两两距离中位数动态算）；(b) `_pick_crossover_parents` 从不同 niche / 不同 Glicko `r-2*rd` 区间选父；(c) commit_bot 前算"加入后 pool VS"，`ΔVS<+0.05 AND rating 提升<30 → REJECT`（binding novelty gate，evolution-plan C3）。
- **文献**：AutoQD（ICLR 2026，纯 numpy VS）；QDSP（FM-as-judge novelty gate binding）；Pugh et al. / Cully "archive 必须进选父否则 QD 退化"形式证明。注意 Gaier 2024 caveat：BC 噪声在 POMDP 下非真 occupancy。
- **收益**：mode collapse 从 LLM 自由文本判断升级为数值化趋势监控。工作量 **medium**，收益 **high**。

### 🟡 #7 [MEDIUM] Worker 失败无 DeepEvolve Debug 子代理

- **现状**：`agent_workers.py:330` 的 4 次重试反馈机制其实**比 review 说的好**——compile_error/invalid_target/zero_changes/timeout 各自注入**针对性** CRITICAL FIX 块（含完整 error 内容），不是泛化文本。真正的 gap：这是**同一 worker agent 的文本 append**（仍靠 worker 自己理解 error→改），而非 DeepEvolve 式**独立 debug sub-agent**（专门读 error→定位行→输出 patch→注入为下次 attempt 的结构化 reviewer_feedback）。实测 worker 真正失败频次很低（category==worker 仅 5/71）。
- **改法**：worker failure 时 capture py_compile/smoke error → 独立 LLM debug agent（budget 5）→ 失败 task score=0 进 `worker_failures`。DeepEvolve Table3：成功率 0.13→0.99。
- **收益**：4 代以上卡住的 worker 任务解锁。工作量 **medium**，收益 **medium-high**。

### 🟡 #8 [MEDIUM] evidence_gate 覆盖面太窄——worker/critic 阶段新捏造引用仍不被复核

- **现状**：`_verify_cited_replays`（`tool_planning.py:198-258`）只在 `_validate_master_plan`（L385）跑——**只校验 Master plan JSON 的 GxHx#anchor 引用**。同一 generation 内 Worker 在 worker_prompt 里、Critic 在 `evidence` 字段里新捏造的 G{N}H{M} 引用**不被同一 manifest 复核**（evidence-attribution 链断点）。`worker_prompt.md` / `critic_prompt.md` grep 'replay|spotlight|cite|manifest' = **0**。
- **改法**：(a) 把 `_verify_cited_replays` 重构成接收 text-list 的纯函数 `_check_citations(text_list)`；(b) Critic 路径在 `tool_gates.py:877` evidence 提取后跑同一校验，命中 fabricated 引用→降 critic score（cap 6）+ log；(c) `worker_prompt.md` / `critic_prompt.md` `<context>` 块各加一句"If you cite a replay hand, reference it by the anchored GxHx#anchor ID in the injected replay_spotlight block"。
- **文献**：NabaOS HMAC-signed tool receipts（覆盖全部 agent 输出，非仅 plan 阶段）。
- **收益**：堵 fabricated replay 在 worker/critic 阶段的复发。工作量 **low**，收益 **medium**。

### 🟡 #9 [MEDIUM·新发现] 结构漂移治理缺失——fix_injection.py 是定时炸弹 + regression_guardian 写完即丢

- **现状**（completeness-critic 新发现）：`fix_injection.py` 维护硬编码 search-and-replace 补丁注册表（BOT-001a wheel straight / BOT-002a re-raise min / BOT-004 TOTAL_HANDS=70），用**逐字字符串匹配** + guard 幂等。LLM worker 一旦重构 `card_utils.py evaluate_5()`（改名/换行/重排分支），search 字符串不再匹配 → fix **静默 skipped**（logged 无阻断）→ wheel straight bug 某代悄悄回归。`BOT-002b` 已标 `active=False`，说明这套机制已开始腐烂。另：`_run_regression_guardian`（`tool_gates.py:844-862`，score<4 触发）产出的 `guardian_diagnosis` 只写进 result 返回给 orchestrator，**grep 全仓 0 下游消费者**——既不进 experience_pool、不进 worker 失败记忆、不进下一代 Master 的 cross-gen constraint、也不阻断（最纯粹的"写了即丢"，且触发条件 score<4 正是 Critic 最该传递信号的场景）。
- **改法**：fix_injection 从逐字补丁迁移到 **AST 语义匹配**；`regression_guardian_diagnosis` 进 experience_pool 给下代 Master 看。
- **收益**：阻止结构漂移导致的 silent regression。工作量 **medium**，收益 **medium-high**。

### 🟡 #10 [MEDIUM] battle_experience 后台 LLM 循环占 45.8% LLM 花费

- **现状**：`battle_experience.py` 后台线程（`_experience_loop:235`，POLL_INTERVAL=20s，TARGET_BATCH=6）每个 unanalyzed match batch 调一次 `_run_llm_update`（:466）。实测 **179 调用 = $27.57 = 45.8% 总花费 / 68.6% 调用次数 / $0.154/call**。它是 self-consumed 循环：daemon 每跑一批就触发一次 LLM 重写整个 `battle_experience.md`。
- **改法**（修正 review 的两处误判）：(A) review 说"MAX_ANALYSES_PER_HOUR=240 即每小时 240 次/年省 90%"是把 rate-limit 上限误读为实际频率——实测平均 ~15 次/h、$2.49/h，降幅空间约 6x 而非 90%；(B) review 说"与 experience_pool.md 重叠→删前者"是误判：`battle_experience.md`=empirical cross-pair 统计（allin%/chip spread/worst-case tiers，0 处 META），`experience_pool.md`=strategy META——**不重叠**。真正改法：加内容哈希去重（同 batch 指纹不重跑 LLM）+ 按 source_v 增量合并而非全量重写 + N 代无 WR-lift 的观察降级 stale。
- **文献**：Ratchet 消融——retrieval-only +0.077 但无治理=+0.0pp；no injection +0.002（连续 LLM 重写同类经验无边际价值）。
- **收益**：砍 ~40% LLM 成本。工作量 **low**，收益 **high**。

---

## 2. 提示词层优化（按 ROI 排序）

### 🟡 #11 [MEDIUM] measurement_plan 是死字段——Master 预测从未回流，credit assignment 是 prompt 谎言

- **现状**：`master_prompt.md:170-176` 要求每任务给 `target_opponent + expected WR delta + 确认统计量`，并承诺 "After commit, predicted vs actual delta is logged to experience_pool RECENT_LESSONS. This is CREDIT ASSIGNMENT telemetry"——但**代码层从未解析这两个字段、从未把预测与实际比对**。`output_schema.py:18` `expected_behavior_change: str=""` + L20 `measurement_plan: str=""` 都是自由 str（schema 类型谎言）。`spot_analyzer.verify_behavior` 里 `expected_changes` 是**纯死赋值**。
- **改法**：(a) 立即止血：删 `master_prompt.md:174-175` 的承诺句，改为诚实表述"measurement_plan 是 Master 自评估记录，当前不回流"；同步删 `spot_analyzer.py:336` 死赋值；(b) 建闭环：与 #2（校准时序）合并——rating_delta 异步回填后，把"Master 预测 vs daemon 实际"写进 experience_pool RECENT_LESSONS（这是 #2 的同一个病根：commit 时序早于评估收敛）。
- **文献**：DeepEvolve reflection+eval 回流闭环；AgentAssay 回归测试统计框架。
- **收益**：消除 prompt 谎言 + 未来真建 credit assignment。工作量 **low**（止血）/ **medium**（闭环）。

### 🟡 #12 [MEDIUM] 主经验池无 Ratchet outcome-driven retirement——drift 已实证

- **现状**：`research_governance.py` 的 Ratchet 模型（ĉ/retire/blacklist）**只管控 web_candidates 池**（`WEB_CANDIDATES_FILE`），`experience_pool.py`/`battle_experience.py`/`experience_archivist.py` 三个文件 grep `research_governance` = **ZERO import**。`experience_pool.py:14` `MAX_EXPERIENCE_LINES=120` + `trim_experience_pool()` 纯尾部截断，无 attribution/retire。**漂移实证**：`experience_pool.md` 仍在说"battle.py stderr 不可读是最高 ROI unblock"（L4/L11/L32），但 A1 本会话已修——old entry stale after env change，正是 Ratchet 三阶段 library drift（accumulation → retrieval degradation → silent injection harm）。另 `battle_experience.md:14` 同一观察被 **66 代重复合并无 retire**。
- **改法**：不要新建 `experience_attribution.py`（plan 说的），而是**直接 import 现成 `research_governance` 的 score_candidate/record_outcome/retire 原语**到 `experience_archivist._consolidate_experience_pool`：给每条 lesson 加 `source_gen + attributed_hurt/help`，retire 逻辑从"EXHAUSTED 标签 tag-identity 检查"升级为"按 ĉ 排序 + RETIRE_N_MIN=30/TAU=-0.10 退役"。`battle_experience_update.md` + `experience_consolidator.md` 加"重复≥N 代且无 WR-lift 的观察降级 stale"。
- **⚠️ 注意**：harsh retirement 是 Ratchet 消融里 **-0.019 主动伤害项**（plan L106），`N_min` 必须 ≥30 不可激进。
- **收益**：经验池不再 drift。工作量 **medium**，收益 **medium**。

### 🟢 #13 [LOW] 死代码/不一致清理（一批，低风险高可读性）

- **#13a intra-gen retry 死代码**：`generation_attempt` 永远=0（全仓无 increment 点 + `run_master` 强制 reset + 1034 事件实证恒 0），`tool_planning.py:1442` 的 `require_new_plan` 护栏是不可达死代码。`orchestrator.md:97` 仍描述已废弃的 "retry workers after critic rejection" 路径，与 `:78` "advisory, ALWAYS proceed to precommit" 自相矛盾。改法：删护栏 + 清理 prompt 措辞（采纳 advisory 设计，不恢复 increment）。
- **#13b source-loop 死代码 typo**：`generation_scheduler.py:531` 过滤 `evt.type=="pipeline.prepare"`，但生产者是 `tool_gates.py:502` 的 `"pipeline.prepare_done"` → `_detect_source_loop`/`_detect_source_oscillation` 永久死代码。一行修复 typo，但注意即便修复也打不断当前线性爬山（实测 source_v 序列单调递增全 distinct，两个 detector 触发条件不命中）——需额外加第三个 detector。
- **#13c worker_prompt 长度不一致**：`master_prompt.md:83` "MUST be under 6000"，`tool_planning.py:278` 代码检查 12000。实质是 tiered 设计（6000=inline 软目标配 .task_context 溢出 / 12000=硬上限），但 "MUST" 措辞与 enforcement 错配。改法：`master_prompt.md:83` 改"target 6000 (soft); hard limit 12000"。
- **#13d DeepEvolve 伪代码未代码级校验**：`master_prompt.md:89` 要求 code skeleton 但 `_validate_master_plan` 不校验。影响小（Master 多数会附 skeleton），如要做在 `output_schema.py` WorkerTask 加可选 `pseudocode` 字段。建议**不做**——反复加 hard gate 会增加 Master 失败率，依赖 reviewer/critic soft gate 即可。
- **#13e orchestrator.md 与代码的 critic 语义矛盾**：`orchestrator.md:59` "run_critic returned approved: true" 与 `tool_gates.py:798` 无条件 `approved=True` 一致但误导；`@tool` 描述（tool_gates.py:709）"score ≥ 6 = approved" 是过时字符串（实际恒 True）。改法：清理过时字符串。

---

## 3. 对抗验证纠正的关键误判（这些已修，勿重做）

| 误判 | 真相 |
|---|---|
| "exploitability_probe 41 事件全 1.0，纯装饰" | **STALE**。`866f6dc` A7 已修：v146+ 实测 0.767-0.989 有方差，v146 产出真实 weakness。49 条里仅 7 条=1.0。`tool_planning.py:706-781` 已注入 master_prompt |
| "AIVAT MC seed 未固定 = p-hacking" | seed 子主张**被夸大**。dead-code 主张成立（488 行 `aivat_enabled=False` 生产默认），但 seed 治理需更细核查 |
| "worker 重试只靠 attempt_note 文本增量" | **不准确**。compile_error/invalid_target/zero_changes/timeout 各自注入针对性 CRITICAL FIX 块 |
| "precommit 无 binding gate，100% 旁路" | **含误读**。`tool_eval.py:90` `-2000` chip-loss hard-reject 已是 binding（commit gate 强制 fail）；真正缺口是 semantic 'caution' 分支（borderline 仅 log） |
| "MAP-Elites 19 niches" | 实测 **18 niches** |
| "battle_experience 与 experience_pool 重叠→删前者" | **误判**。前者=empirical 统计，后者=strategy META，不重叠 |

---

## 4. 推荐落地顺序（按 ROI + 依赖）

```
第一批（止血，全部 low effort，解锁关键闭环）:
  #1  tools.py 注册 run_literature_probe（一行，解锁 Exa 进进化 + A6 治理有流量）
  #2  critic calibration rating_delta 异步回填（解锁 Critic 真实判别力）
  #3  placement_shadow TRUE-SHADOW 升 BLOCKING（复用 M6 范式，堵 INERTNESS 第二根因）
  #10 battle_experience 内容哈希去重（砍 40% LLM 成本）
  #11 删 measurement_plan prompt 谎言（止血）
  决策点：daemon≥30g 后 grep run_literature_probe 触发 + critic_calibration 非零 delta 占比

第二批（速度 + mode collapse 治理，medium effort）:
  #4  precommit poll→event-driven + run_master idempotency guard（79→~50min/cycle）
  #5  critic 跨代 local_optima→方向 pivot（三重条件，不回退字面 hard gate）
  #6  behavior_diversity.py Vendi Score + crossover 消费 behavior_archive（C1/C2/C3）
  #8  evidence_gate 扩展到 worker/critic 路径
  决策点：观察 pool VS 趋势 + cycle 耗时 + 同轴重复代数

第三批（韧性 + 治理，medium-high effort）:
  #7  DeepEvolve Debug sub-agent（worker 失败 0.13→0.99）
  #9  fix_injection AST 语义匹配 + regression_guardian 进 experience_pool
  #12 experience_pool 接入 Ratchet retire（复用 research_governance 原语）
  #13 死代码/不一致清理（一批）
```

**关键约束**：
1. **#1 是用户重点需求"Exa 进进化"的真正解锁点**——工具实现完整但没注册，145 代零触发。一行修复 ROI 最高。
2. **#2 和 #11 是同一病根**（commit 时序早于评估收敛）——rating_delta 异步回填同时修两个。
3. **#3 不要对所有 placement_shadow 升 blocking**——仅 TRUE SHADOW（gt0），否则误伤卡死 pipeline（M6 范式已验证 advisory/blocking 混用可行）。
4. **#5/#6 不要简单回退 hard gate**——`7dc4c3f` 已证明字面 hard gate 误杀合法 novel plan，用三重条件 + 语义轴 + Vendi Score binding。
5. **#12 的 retire `N_min` 必须 ≥30**——Ratchet 消融 `N_min=20` 是 -0.019 主动伤害。
6. fold-side bot 策略改动仍受 experience_pool FOLD-SIDE RULE 约束（binary return-1 EXHAUSTED v135-v154）——本文不动 bot 策略，只动框架/提示词。

---

## 附录：已覆盖项（22 条，识别但已落地或部分落地，勿重做）

commit gate critic 检查恒通（误导但已识别）/ precommit n≤16 统计功效不足（被 infra 兜底）/ 三层 timeout 自洽（正面核查通过）/ A1 telemetry 写侧闭环（读侧断裂见 #3 链路）/ AIVAT dead code / MAP-Elites 不进选父（见 #6）/ CS early-stop 仅 parent 侧 / direction_auditor advisory / _pick_crossover_parents 近亲杂交（见 #6）/ master_plan_audit advisory / placement_shadow advisory（升 blocking 见 #3）/ decision_tester fixture 覆盖缺口 / verify_code 与 AST gate 解耦 / orchestrator critic approved 矛盾 / master_prompt 'Known Mandatory Fixes' 膨胀（6663 字符，建议压缩见 #13）/ literature_probe MANDATORY 仅 prompt 声明（已识别为 M6 clause 5 follow-up，与 #1 同源）/ 主经验池无 retire（见 #12）/ master_prompt:222 自承条款不可执行 / 所有 advisory 信号 100% commit（见 #5）/ _river_bet_commit_guard 跨代未修（实为 _river_stackoff_guard 6 次 TRUE SHADOW，见 #3）/ exploitability_probe（已修见误判表）/ exhausted repetition pivot（见 #5）。
