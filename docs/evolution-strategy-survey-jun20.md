# 进化系统策略丰富化调研与规划（2026-06-20）

> 本文由 4-agent 并行调研综合而成：1 个系统现状深度诊断（逐文件核查）+ 3 路 exa 文献检索（LLM 代码进化 / 扑克 AI 策略 / 智能体评估与多样性，覆盖 2024–2026）。目标是**在已完成的 Phase0–4 基础设施之上，找出下一轮进化的高 ROI 改造点**，并附完整文献资料库供 Master/Worker prompt 注入。

---

## 0. 执行摘要（TL;DR）

系统已建完 Phase0–4（AIVAT / Confidence Sequences / FAMOU / MAP-Elites / PSRO MVP / 异步评估 / per-opponent stats），主流方向都有 MVP。**当前瓶颈不是"缺机制"，而是三个系统性问题**：

1. **inertness 死循环**（最高频踩坑）：新 detector/fold-gate 写了但不在 live 控制流触发，且**无法验证是否 fire**——v130 正处此境（2 个 detector LIVE 可达但无 fixture/遥测）。v103/v105/v127/v128 反复重蹈。
2. **mode collapse（策略趋同）**：crossover 频繁、line-label/threshold/sizing 轴被 critic 反复标 exhausted、crossover 父选择倾向结构相近的高分 bot。
3. **评估质量隐患**：precommit 反复波动（8–40 局噪声主导），可能源于 AIVAT heuristic value function 未在评估数据前固定（p-hacking 漏洞）。

**外加一个重要事实修正（已核实 `glicko_ratings.json`）**：v129 实际是当代（v120+）rating 最强的血脉（**r=1526 rd=92，conservative r−2rd=1342**——全场仅次于老牌 v111(1366)，当代最高；v127=1465/v128=1443/v118=1329 均更低），但 memory/experience_pool 仍按"回退"处理 → **lineage 锚定过时**，Master 上下文可能误判当代最强血脉。

**最高 ROI 路径**（详见 §4）：
- **Phase A（立即，低成本低风险，止血）**：inertness 检测三件套 + AIVAT heuristic 审计 + v129 lineage 修正 + 证据完整性 gate。
- **Phase B（短期，扑克策略，直接出 rating）**：Safe Exploitation 置信度门（强制前置）→ 5 条可编码 exploit 规则 → bet-sizing 离散桶表。
- **Phase C（中期，进化架构，治 mode collapse）**：AlphaEvolve elite archive + Voyager skill library + novelty gate。
- **Phase D（中长期，多样性与评估升级）**：AutoQD 行为描述符 + CSRO code-nemesis + crossover 多样化 + PSRO 升级包。

---

## 1. 系统现状诊断（基于逐文件核查，file:line）

### 1.1 三阶段生成 cycle + per-gen pipeline

| 阶段 | 工具 | 入口 |
|---|---|---|
| Phase 1 prepare | `prepare_generation()` | `generation_scheduler.py:67`（disposable，决定 master vs crossover） |
| Phase 2 run | `_run_one_cycle()` | `orchestrator.py:107`（session 持久，崩溃恢复） |
| Phase 3 cleanup | `post_generation_cleanup()` | `generation_scheduler.py:678`（idempotent，reap + 经验池合并 + exploitability probe + QD async eval） |

per-gen 工具顺序（gate ledger 强制 `STAGE_ORDER`，`evolution_infra.py:93`）：`prepare_next_gen`/`run_crossover` → `run_direction_audit`(`tool_planning.py:39`) → `run_master`(`:314`) → `execute_workers`(`:1123`，**顺序非并行**) → `run_quality_gates`(`tool_gates.py:100`) → `run_review`(`:452`) → `run_critic`(`:649`，**advisory 不阻塞**) → `run_precommit_eval`(`tool_eval.py:252`，**唯一 hard regression gate**) → `commit_bot`(`tool_commit.py:54`) → `run_archivist`(`:309`)。

### 1.2 当前进化状态

- 活跃池 38 个 `claude_v*`，graveyard 95 个。最新 tag `bot-v129@3fc3b8e`，`find_current_v()=129`。
- **v130 正在跑**：crossover v118×v128（导入 `check_raise_pressure()` + `barrel_pressure_profile()`），`pipeline_state.json` 显示 `stage=critic_checked, precommit_attempt=1`。
- Glicko（实测 `glicko_ratings.json`）：raw rating top — v95(1557) > v102(1545) > v108(1532) > v114(1527) > **v129(1526, rd=92)**；conservative(r−2rd) — v111(1366) > **v129(1342)** > v95(1317) > v114(1307)。**v129 是当代（v120+）最强**（v127=1465/v128=1443/v118=1329 均更低）且 rd=92 高置信。
- 🔴 **v129 的 turn_second_barrel_planner 进攻 pivot 实际表现良好（当代 rating 最高 + 高置信）**，但经验池 `RECENT_LESSONS` 仍标 REGRESSED → **lineage 锚定需修正**。注意 v130=crossover v118×v128 选的 source v118 r=1329 是低分血脉（crossover 取 trait 设计，但 source 锚定也值得复核）。

### 1.3 经验池累积教训（忠实提炼 `experience_pool.md`）

**[POSSIBLY EXHAUSTED] 轴（劝退）**：
1. Defensive fold-gate 累加（应改 call-site RELOCATION 而非加新并行 detector）
2. `facing_barrel_continuation` 重复 re-import
3. `probe_mode` sizing-knob 常量微调
4. `broadway_suited` bucket
5. bluff/line-reading 阈值微调（`BLUFF_OPP_THRESHOLD`/`VALUE_PRESS_THRESHOLD`/`bluff_heavy_call_widen`）
6. `choose_raise()` sizing 常量（饱和 ≥6 代；例外：加新 opponent-signal gating 算新轴）
7. Crossover-as-default fallback（same-fn/byte-identical re-import）

**正向推荐方向**：
- **OFFENSIVE 新原语不受枯竭约束**：river raise-bluff、line-polarisation barrels、value-heavy donk-bluff、turn second-barrel（v129 已验证）。
- **canonical 6 birth requirements**（新 detector 才算新轴）：①NEW detector ②NEW opponent-line signal（**不是** `opp_current_round_check/bet_count`，已被消费）③≥3 wired reachable sites ④≥3 cited `match_replay/` JSON（不可伪造，2001 个可用）⑤≥30g confidence gate ⑥**persistent fixture self-test**（FOLD_GATE_FIRE stderr 是 debug-only，daemon 永不捕获，**不算证据**）。
- Calibration > new functions（target 空 HAND_CLASS_SCORE band 优于加 gate）。

### 1.4 三大未解问题当前状态（代码核实）

| 问题 | v130 状态 | 风险 |
|---|---|---|
| **inertness** | v130 fold gate 在 `strategy.py:632-662` LIVE 可达（未被上游 shadow，与 v103/v105 不同），**但无 fixture/无遥测**，runtime 是否真 fire 无法验证 | 🔴 当前最关键未验证风险 |
| **0%-fold leak** | `check_raise_pressure`+`barrel_pressure_profile`+river multi-barrel fold 结构性 LIVE，MUTATION 覆盖 mid-one-pair band，**但 fixture 验证未做** | 需 ≥100g 归因 |
| **check_raise_freq** | **仍非独立 NEW detector**（v130 复用已被消费的信号，经验池明确点名不可混淆） | 连续 9+ 代未交付 |

### 1.5 策略贫乏根源

1. fold-gate 轴被连续 3 代消费（v127/v128/v130），direction_auditor 已 HIGH-confidence 标 `repetition_detected`。
2. Master 修复后 `_decide_strategy` 的停滞/振荡检测仍倾向触发 crossover，`_pick_crossover_parents`（h2h_avg_wr + 版本差≥3）易选结构相近高分 bot → 趋同。
3. offensive 轴未充分开采（6 birth requirements 门槛高，尤其 ④可追溯 replay ⑤fixture self-test）。
4. inertness 负反馈：detector 不 fire → critic 低分但不阻塞 → 下一代换 detector 重蹈。
5. opponent-signal 单一化（都复用 `opp_current_round_check/bet_count`）。

---

## 2. 文献资料库（2024–2026，按主题）

> 供 Master/Worker prompt 注入与后续选型。URL 均可追溯。

### 2.1 LLM 驱动自我进化 / 代码进化（主干）

| 文献 | 作者/机构 | 年份 | URL | 核心贡献 |
|---|---|---|---|---|
| **FunSearch** | Romera-Paredes et al. / DeepMind | 2024 Nature | par.nsf.gov/biblio/10499230 | LLM 提议变异 + evaluator 打分 + 程序化归档，函数空间搜索 |
| **Evolution of Heuristics (EoH)** | Liu et al. / CityU+MS | 2024 ICML Oral | proceedings.mlr.press/v235/liu24bs.html | 自然语言 thought + 代码双载体；E1/E2/E3 变异策略术语；超 FunSearch |
| **AlphaEvolve** | Novikov et al. / DeepMind | 2025 | arxiv.org/abs/2506.13131 | 分布式进化 pipeline，**elite program database + LLM mutation + 多 evaluator 自动拒绝编译/语义错误** |
| **EoH-S (Heuristic Set)** | Liu et al. / CityU | 2026 AAAI Oral | arxiv.org/abs/2508.03082 | 进化互补启发式集合（AHSD），complementary population management，性能 +60% |
| **Eureka** | Ma et al. / NVIDIA+UPenn | 2024 ICLR | openreview.net/forum?id=IEduRUO55F | reward 代码进化 + reward surrogate 批量评估 + 差分反馈 |
| **Voyager** | Wang et al. / NVIDIA+Caltech | 2024 TMLR | rpl.cs.utexas.edu/.../wang-tmlr24-voyager | **自动课程 + 可执行代码 skill library（写入/检索/复用）+ 迭代 prompting** |
| **Code-Space Response Oracles (CSRO)** | Hennes, Li, Schultz, Lanctot / DeepMind | 2026-03 | arxiv.org/abs/2603.10098 | 🔴 **PSRO 的 best-response oracle 用 LLM 代码生成替换 RL；在 Poker/Liar's Dice 验证** |
| **VAD-CFR / SHOR-PSRO** | Li, Schultz, Hennes, Lanctot / DeepMind | 2026-02 | arxiv.org/abs/2602.16928 | AlphaEvolve 进化 CFR/PSRO 算法变体；18 博弈含 Poker；**警示：distill 出的最小核心才是泛化驱动力** |
| **OMNI-EPIC** | Faldor, Zhang, Cully, Clune | 2024-25 ICLR | arxiv.org/abs/2405.15568 | FM 生成代码 + **interestingness model 过滤**"可学但无聊"变异 |
| **Self-Evolving Agents Survey** | — | 2026-01 | arxiv.org/abs/2507.21046 | what/when/how to evolve 选型框架 |

### 2.2 扑克 AI / 不完美信息博弈策略

| 文献 | 作者/机构 | 年份 | URL | 核心贡献 |
|---|---|---|---|---|
| **OX-Search: Safe and Robust Subgame Exploitation** | Ge, Xu, ... Gao / NJU | 2024 ICML | proceedings.mlr.press/v235/ge24b.html | 🔴 **Adaptation Safety：利用型策略可剥削度 ≤ 不利用时；对手模型不准也 robust** |
| **SpinGPT** | Maugin, Cazenave / Paris Dauphine | 2025 ACG | lamsade.dauphine.fr/~cazenave/papers/PokerACG2025.pdf | 首篇 LLM 微调玩扑克；**规则壳+简单 heuristic(all-in→2/3pot) 就把 SFT 模型翻盘**；zero-shot LLM 全崩 |
| **PokerBench** | Zhuang et al. / Berkeley | 2025 | arxiv.org/html/2501.08328v1 | 11000 preflop+postflop 场景基准，SOTA LLM 全差，微调后改善；可作 decision_tester 外部 ground-truth |
| **DecisionHoldem** | Zhou, Bai, Zhang et al. / CASIA | 2022/2024 | arxiv.org/html/2201.11580v2 | 开源 HUNL AI，safe depth-limited solving + diverse opponents，击败 Slumbot 730 mbb/h，Python 工程参考 |
| **TurboReBeL** | Li, Huang / THU | 2025/26 | openreview.net/forum?id=yMo7Z670f6 | ReBeL 250× 加速，PBS + depth-limited search |
| **Look-ahead Search on Policy Networks** | Kubaček, Burch, Lisy / CTU+Sony+Alberta | 2025 | arxiv.org/pdf/2312.15220 | 给 policy-gradient RL 加 test-time search，启示：手牌 equity 估计器可当简化 value function |
| **Range/Nut Advantage 编码指南** | PokerGTO Solver | 2026 | pokergtosolver.com/en/blog/board-texture-range-nut-advantage | Range advantage→下注频率；Nut advantage→尺寸/加压；dynamism→保护 vs 延迟 |
| **Exploitative 阈值 5 条** | RiverOdds | 2026 | riverodds.app/poker-exploitative-play/ | **可直接编码**：fold-to-cbet>75%→flop 诈唬；call station→TP 三街价值；never bluff river-caller；fold-to-checkraise≥0.6→draw shove；nit defense<30%→宽开 |
| **Bet Sizing 完整指南** | DeucesCracked | 2026 | deucescracked.com/blog/bet-sizing-no-limit-holdem-strategy-guide-2026 | 1/3 干牌保护、2/3 价值、pot+ 诈唬、overbet 极化 + SPR 分阶段 |
| **Donk Bet 精通** | Upswing Poker | 2025 | upswingpoker.com/donk-betting-lucid/ | Donk bet 触发=nut 优势转移（非随机领打） |
| **算法对比综述** | Sci. Reports | 2025 | nature.com/articles/s41598-025-86899-8 | CFR/FP/NFSP/PSRO 在 poker-like 游戏选型速查 |

### 2.3 智能体评估 / 多样性 / 对手建模 / 自博弈

| 文献 | 作者/机构 | 年份 | URL | 核心贡献 |
|---|---|---|---|---|
| **AIVAT 不确定性传播** | Kim, Sandholm / CMU | 2026 | arxiv.org/abs/2605.14261 | 🔴 **AIVAT 官方后续：heuristic 必须在观察评估数据前固定（否则 p-hacking）；传播不确定性省 43% 样本** |
| **A-PSRO** | Hu et al. | 2025 ICML | proceedings.mlr.press/v267/hu25n.html | Advantage 度量内禀连接 Nash，每次更新向均衡收敛 |
| **AdaDO/RMDO** | — | 2024 | arxiv.org/abs/2411.00954 | Double Oracle 最优扩展频率，样本复杂度指数→多项式 |
| **FAMOU (CoEvo)** | liu / — | 2026 | github.com/1xiangliu1/FAMOU-CoEvo | 🔴 与本系统几乎同构：LLM 策略代码进化 + 动态对手池(add/retire/reweight) + deep_eval(top-k champion) |
| **Covolve** | Sygkounas, Hazra et al. | 2026 ICLR 投稿 | openreview.net/forum?id=ser00zCWC2 | LLM 同时生成 agent+environment，**MSNE meta-policy 防过拟合最新环境** |
| **Absolute Zero (AZR)** | — | 2025 | arxiv.org/abs/2505.03335 | 单模型自提任务+自解，code executor 统一 verify，零数据自进化 |
| **CURE (coder+tester 共进化)** | Wang et al. / Princeton+ByteDance | 2025 | arxiv.org/abs/2506.03136 | coder 和 tester 共进化，无 ground-truth |
| **am-ELO** | Liu et al. | 2025 ICML | proceedings.mlr.press/v267/liu25ak.html | MLE 替代迭代更新 + annotator 能力度量，稳化 Arena 评分 |
| **AgentAssay** | Qualixar | 2026 | github.com/qualixar/agentassay | 行为指纹 + **自适应预算(5-10 次校准→最小试验数) + 变异测试**，5-20x 成本降 |
| **AutoQD** | Hedayatian, Nikolaidis | 2026 ICLR | openreview.net/forum?id=FNnJIf4ymV | 🔴 **自动学行为描述符（occupancy measure MMD 嵌入），消除手工 BC** |
| **OMIS (in-context 对手搜索)** | Jing et al. | 2024 NeurIPS | openreview.net/forum?id=bGhsbfyg3b | Transformer in-context 学 actor/imitator/critic，决策时 search（需 Transformer，与纯规则 bot 不兼容） |
| **AMP3 (扑克对手风格)** | Shi et al. / THU | 2025 | link.springer.com/article/10.1007/s00521-025-11262-x | 扑克专用风格库+深度预测+Actor-Critic 自适应 |
| **smooth-FP in-context** | Liu, Feng / Google | 2026 | arxiv.org/abs/2602.19309 | smooth Fictitious Play 嵌入 LLM 推理，零参数更新在线适应 |
| **How Many Agents to Avoid Collapse** | Hypogenic AI | 2026 | github.com/Hypogenic-AI/agents-avoid-collapse-2361-claude | 架构多样性 > 数量；微调生态 N=16 仍 mode collapse |

---

## 3. 可行性分析：三路文献的交叉信号

三个独立调研方向不约而同收敛到同一组结论——这增强了可信度：

### 3.1 inertness 是头号系统性问题（三方共识）

| 来源 | 指向 |
|---|---|
| 系统诊断 | fixture self-test + 生产级遥测缺失（v130 两个 detector 无法验证 fire） |
| LLM 进化文献 | Voyager skill library 强制"live-site 接入证明" + executor 真跑验证行为变化；AlphaEvolve evaluator 自动拒绝无效变异 |
| 扑克文献 | 5 条 exploit 规则给**精确 firing 条件**（哪 3 个特征、什么阈值、哪个决策点），而非含糊 detector |
| 评估文献 | **变异测试**（对 detector 逻辑做小改动，决策分布不变 → INERT）可在 review/critic 之前自动抓 |

→ **结论**：建 inertness 检测基础设施（fixture + 遥测 + 变异测试）是根治 6-gen 反复踩坑的根本，且 v130 现成 2 个 detector 是首个验证目标。

### 3.2 评估质量 > 新机制（评估调研核心论点）

- **AIVAT heuristic 固定审计**（Kim & Sandholm 2026 贡献①）：若 heuristic 是用评估数据拟合的 → **现存 precommit 假阳性通过风险**，可能是反复波动的隐藏根因。修复几乎免费。
- **自适应预算**（AgentAssay）：precommit 固定 ~16 局，但记忆显示 8 局够/40 局才稳，应按方差动态分配。
- **变异测试兼做 inertness detector**：一举两得。

### 3.3 mode collapse 是 LLM 进化的头号杀手（LLM 进化文献 + 多样性文献共识）

- AlphaEvolve/EoH 用**种群进化 + E1 强制不同思路**对抗；当前系统是"贪心爬山"。
- OMNI-EPIC **interestingness/novelty gate**：binding 拒绝重复变异（当前 Direction Auditor 是 advisory）。
- AutoQD 自动行为描述符让 write-only MAP-Elites 真正可用。
- **Covolve MSNE meta-policy**：commit 判据从"vs 父代"升级为"vs 全对手池混合"，防过拟合最新父代。

### 3.4 扑克策略可翻译性（扑克文献）

- SpinGPT 教训**验证了本系统架构合理性**：规则壳 + 启发式 >> 纯模型（一个 all-in heuristic 就翻盘）。
- OX-Search **Adaptation Safety** 是所有 exploit 调整的强制前置（防 FAMOU nemesis 反利用）。
- 5 条 exploit 规则阈值已量化，可直接转 Worker 任务。
- CFR/ReBeL 本体无法翻译成规则，只有**产出物（蓝图频率表）+ 结构性洞察（range/nut advantage、SPR、极化 sizing）**可编码；离线 CFR 蒸馏是中长期路径（可与 `rl/` 模块协同）。

---

## 4. 行动计划（分阶段，按 ROI）

### Phase A — 止血与验证基础设施（立即，低成本，根治反复踩坑）

| # | 行动 | 解决的瓶颈 | 难度 | 收益 | 落地位置 |
|---|---|---|---|---|---|
| **A1** | **inertness 检测三件套**：①每个新 detector 附带持久化 pytest fixture（断言 `severity>0` 在目标 leak spot_info）②把 FOLD_GATE_FIRE stderr 改为写 `results/fold_gate_fire.jsonl`（daemon 可读，每手记 gate 命中）③变异测试：对 detector 逻辑做小改动，若决策分布不变 → INERT，集成进 `run_quality_gates` | inertness（当前最大风险，6-gen 反复加 inert detector） | 中 | **高** | `web/core/code_verification.py` + 新 fixture 目录 + `tool_gates.py:100` |
| **A2** | **AIVAT heuristic 固定审计**：核查 Phase1 AIVAT 的 heuristic value function 来源——若是评估数据拟合的 → p-hacking 漏洞，立即锁定（在评估数据前固定）；加不确定性传播（省 43% 样本） | precommit 反复波动可能根因 | 低-中 | **高** | Phase1 AIVAT 模块（`web/core/` 下，见 phase1-aivat 记忆） |
| **A3** | **v129 lineage 修正**：✅ 已核实 v129 r=1526 rd=92（conservative r−2rd=1342，当代 v120+ 最高，全场仅次于 v111）——更新 memory/上下文锚定；把 turn_second_barrel_planner 进攻机制正式化（避免 shadow value-bet path `strategy.py:1247-1259`） | lineage 锚定过时（误判回退） | 低 | **高** | memory + Master 上下文 + `agent_master.py` |
| **A4** | **证据完整性 gate**：pre-Master 自动校验脚本——引用的 `match_replay/` 文件与 H2H 数字必须 grep-provable 存在，否则拒绝 plan | 伪造证据污染决策（v127/v129/v130 反复出现） | 低 | 中 | `tool_planning.py` master 工具前置 |

**Phase A 依赖**：无（全部独立）。**预估**：A1 是主要工作量（1–2 天），其余几小时。**与 Phase0-4 关系**：增强，不重复。

### Phase B — 扑克策略丰富化（短期，直接出 rating，翻译成 Worker 任务）

> ⚠️ **B1 是 B2/B3/B4 的强制前置**——没有置信度门的 exploit = 给 FAMOU nemesis 送分（OX-Search Adaptation Safety）。

| # | 行动 | 解决的瓶颈 | 难度 | 收益 | 落地位置 |
|---|---|---|---|---|---|
| **B1** | **Safe Exploitation 置信度门**：每个 per-opponent exploit detector（barrel_freq/check_raise/fold-to-cbet）加 `samples>=N_min 且 feature_std<σ_max` 门；样本不足走 GTO-ish baseline（推广 `EVAL_RD_THRESHOLD=60` 思想） | 过度利用被反利用（v103/v104 regression）+ 防 nemesis | 低 | **中高** | `opponent.py` 每个 exploit detector |
| **B2** | **5 条可编码 exploit 规则**（精确 firing 条件）：①`opp_fold_to_cbet≥0.75 且样本≥20` → flop 必诈唬 ②call_station → TP 三街价值关诈唬 ③`never_fold_river`(call>0.85) → river 零诈唬 ④`fold_to_checkraise≥0.6 + draw` → shove ⑤`blind_defense<0.30` → 宽开加注。**重点①**：记忆显示 opp turn ftb ~99% 但 v111 turn raise 仅 33.5% = 巨大未兑现利用点 | 进攻轴枯竭 + inertness（给精确 firing 条件） | 低 | **高** | `strategy.py`/`postflop.py` 各决策点 |
| **B3** | **bet-sizing 离散桶表**：`choose_raise()` 重构为「牌面 dynamism × 手牌档位 × range advantage」分派表（先粗分 3×4×2=24 桶），终结 LLM 反复调常数的循环 | sizing 轴耗尽（v104/118/124/126 共同死结） | 中 | **高** | `strategy.py` choose_raise（注意 dispatch-order，桶选择须在所有 cap 之前） |
| **B4** | **range/nut advantage + SPR 矩阵化**：新增 `range_advantage_signal`(preflop 角色+牌面偏好)+`nut_advantage_signal`；`_spr_commitment_gate` 从单阈值改「SPR 档×手牌档」矩阵 | 防 cooler 失血（演进 v103 stackoff guard / v110） | 中 | 中 | `strategy.py`/`strategy_helpers.py` |

**Phase B 依赖**：B1 → B2/B3/B4。**与 Phase0-4 关系**：B1 用了 Phase3 对手画像；B2 命中经验池明确推荐方向。**注意**：B2 每条都需满足 6 birth requirements（尤其 ④≥3 cited replay ⑤fixture self-test，Phase A1 已铺路）。

### Phase C — 进化架构升级（中期，结构改造，治 mode collapse 根因）

| # | 行动 | 解决的瓶颈 | 难度 | 收益 | 落地位置 |
|---|---|---|---|---|---|
| **C1** | **AlphaEvolve elite archive + EoH 变异策略**：维护 N 个 elite 的 commit hash + 行为 descriptor + fitness（不只最高分）；Worker prompt 注入随机选中 elite 的 diff 摘要 + 明确变异策略（E1 完全不同思路/E2 细微调参/E3 交叉）；评估后按 niche+fitness 双标准进 archive，从贪心爬山升级为种群进化 | 趋同 + 维度枯竭 + JSON 崩（evaluator 拒绝无效变异） | 中 | **高** | `agent_master.py`/`agent_workers.py`/`generation_scheduler.py` + 新 `results/elite_archive.json` |
| **C2** | **Voyager skill library**：`skills/` 目录存独立函数 + 自然语言描述；Worker 改 bot 时先检索复用；新 skill 入库门禁=**必须接 3 个 live site（Phase A1 验证）+ 通过 novelty check** | 维度枯竭（Worker 不知已有素材）+ inertness（强制 live-site 接入证明） | 中 | 中高 | 新 `skills/` + Worker prompt + 复用 experience_pool 写盘机制 |
| **C3** | **novelty gate（binding）**：Direction Auditor 升级——Worker 产出后 LLM 判"diff 相对 archive 是否带来新行为"，novelty 低直接拒、不进评估；MAP-Elites 从 advisory 升 binding（fill 中的 niche 不再接受同类） | 趋同（硬性拒绝重复）+ 省评估预算 | 低-中 | 中 | `direction_auditor.py` + Phase3 MAP-Elites |
| **C4** | **distill 最小核心 + 周期 ablation**：Critic 加审查"提升是否来自可一句话表述的最小机制"，复杂耦合堆砌降分；每周跑 ablation（关掉某模块看 rating 变化）识别真驱动力 | 复杂堆砌假性提升不泛化（critic 反复质疑的 exhausted/FABRICATED/confounded） | 低 | 中高 | `critic_prompt.md` + 新 ablation 脚本 |

**Phase C 依赖**：C2/C3 依赖 C1 的 archive；C4 独立。**预估**：C1 是主要工程（3–5 天），LLM 查询预算可能翻倍（维持 ~5-8 elite vs 1 祖先）。**与 Phase0-4 关系**：C1/C3 是 Phase3 MAP-Elites + Direction Auditor 的进化；C2 复用 experience_pool。

### Phase D — 多样性与评估升级（中长期）

| # | 行动 | 解决的瓶颈 | 难度 | 收益 | 落地位置 |
|---|---|---|---|---|---|
| **D1** | **AutoQD 自动行为描述符**：用决策指纹（信息集→动作分布）的 MMD 嵌入替代手工 25-niche，让 write-only archive 指导 Master 选"行为互补"父母 + 淘汰"行为重复"弱者 | mode collapse（手工维度未必覆盖真实行为多样性） | 中 | 中高 | Phase3 MAP-Elites 模块 |
| **D2** | **CSRO code-nemesis**：FAMOU 加"code nemesis generator"——分析 head_to_head 找被压制 spot，LLM 写专门 exploit bot 加入评估池（用 Phase3 exclude 隔离不污染主评分） | 趋同（外部压力逼分化）+ 暴露 dead path | 中高 | **高** | Phase3 FAMOU + Phase4 PSRO wrapper |
| **D3** | **crossover 父选择多样化 + reap 互补性**：`_pick_crossover_parents` 加 behavioral niche 距离（用 MAP-Elites/AutoQD archive）；reap 从"最低 h2h_avg"改"边际互补贡献"（EoH-S complementary management） | crossover 产出趋同 + reap 误杀专精 bot（v112/v120 orphaned） | 中 | 中 | `generation_scheduler.py:599` + `tool_bot_management.py` |
| **D4** | **PSRO 升级包**：A-PSRO Advantage（收敛导向评估核）+ AdaDO（对手池扩张时机）+ **Covolve MSNE meta-policy**（commit 判据升级为 vs 全对手池混合，最现实落地形态） | commit 假阳性（过拟合最新父代）+ self-play 局部最优 | 中高 | 中高 | Phase4 PSRO 模块 |

**Phase D 依赖**：D1/D2/D3 依赖 C1 archive；D4 受 **LLM 单步成本硬约束**（一代 ≈ 一次 oracle，无法快速 best-response）→ 只能"伪 PSRO"（archive 积累 + 离线算 mixture），**D4 以 MSNE meta-policy 形态落地最现实**。**与 Phase0-4 关系**：全部是现有 MVP 的下一代增强。

### 可选 / 锦上添花

- **E1 PokerBench 接 decision_tester**：11000 场景作 quality gate 外部 ground-truth，防 bot 在标准 spot 漂移（难度低，收益中）。
- **E2 am-ELO 稳化评分**：MLE 全局一致性 + 对手可靠性建模（部分与 Glicko-2 rd 重复，需 A/B 实测，优先级低）。
- **E3 离线 CFR 蓝图蒸馏**：DecisionHoldem 在 Leduc/FHP 跑 CFR → 导出"桶→频率"JSON，bot 运行时查表（中长期，可与 `rl/` 模块协同，给规则 bot 真正 GTO 基线）。

---

## 5. 风险与陷阱（落地必读）

1. **reward hacking / Goodhart**：扑克方差极大（即使有 AIVAT），LLM 进化会抓评估噪声而非真实优势（RHB 论文：72% hacking 带 CoT 合理化）。→ 任何"提升"须在独立对手集 + ≥100g H2H 复现。
2. **mode collapse（LLM 进化头号杀手）**：LLM 收敛到"看起来合理"的少数模板。→ novelty gate 必须 binding，archive 必须 forcing 未填 niche。
3. **distill 陷阱**：复杂耦合假性提升不泛化（VAD-CFR 警示）。→ C4 ablation + 评估对手集定期轮换。
4. **dead code / inert path（本系统独有）**：LLM 写了正确函数但没接入控制流。→ Phase A1 的 live-site 接入证明是入库门禁（系统化 v129/v127 已在用的 grep producer 验证）。
5. **过度利用被反利用**：所有 exploit 调整（B2/B3/B4）让 bot 更可剥削，FAMOU nemesis 专找漏洞。→ **B1 Safe Exploitation 门是 B2/B3/B4 强制前置**。
6. **nemesis 污染主评分**：CSRO/PSRO 共性风险。→ Advantage/MSNE 只在 advisory/对手池路径算，不进 Glicko-2 主评分（保持 Phase3 aggregate 双路径隔离 + exclude weak）。
7. **PSRO 在 LLM-代码场景的计算成本**：原生 PSRO 假设每步能 cheaply 训新策略；本系统一代 = 一次 LLM oracle（数十分钟 + $$）。→ 只能"伪 PSRO"，D4 以 MSNE meta-policy 落地。
8. **归因混淆**：代码进化每次多文件改动让"哪个改动有效"无法判断。→ 单变异原则（一个 Worker 任务只改一个机制，收紧现有 Logic/Hyperparameter 角色分离）。
9. **特征可得性**：bot 只能看到公开动作 + 自己手牌；range/nut advantage 的"对手 range"是低置信推断；fold_to_cbet 等是统计估计 → 必须 B2 门。手牌 equity 可用 Monte Carlo 精确算（neuron_poker ref 有 C++ 版 ~500x 快）。
10. **discrete bucket 表 over-fitting**：桶太粗失信息、太细回连续调参 → 先粗分跑通再细化。
11. **AutoQD 在部分可观测博弈的局限**：occupancy measure 需定义在 (信息集,动作) 上，可降级为"决策指纹分布"，但会丢对手手牌条件化 → 用变异测试（A1③）作特征有效性 sanity check。

---

## 6. 与现有 Phase0–4 的衔接（避免重复）

| 现有 MVP | 本计划如何增强（而非重复） |
|---|---|
| Phase0 per-opponent stats/etag | B2 exploit 规则消费它；A1 变异测试用 per-opp 决策分布 |
| Phase1 AIVAT | A2 加不确定性传播 + heuristic 固定审计（直接增量） |
| Phase2 CS/SPRT/行为指纹 | A1③ 变异测试扩展指纹；自适应预算（AgentAssay） |
| Phase3 FAMOU/MAP-Elites/对手画像 | C1 archive + D1 AutoQD + D2 code-nemesis + C3 novelty gate（全下一代） |
| Phase4 异步/QD/PSRO | D4 PSRO 升级包（Advantage/MSNE）；C1 异步评估复用 |

**核心原则**：Phase0–4 是"评估与对手建模基础设施"，本计划 Phase A/B 是"用现有基础设施修止血 + 出策略"，Phase C/D 是"把进化从贪心爬山升级为种群进化"。不推倒重来。

---

## 7. 推荐实施顺序

```
Week 1-2 (Phase A，止血，全部独立可并行):
  A4 证据gate(几小时) → A3 lineage修正(几小时) → A2 AIVAT审计(半天)
  A1 inertness三件套(1-2天) ← 主工作量，v130两detector作首个验证目标
  ↓ 解锁：6-gen反复踩坑根治 + 评估质量提升

Week 3-4 (Phase B，扑克策略，B1先行):
  B1 Safe Exploitation门(必须先做) → B2 5条exploit规则(重点①ftb利用)
                          → B3 sizing桶表 → B4 range/SPR
  ↓ 解锁：offensive轴开采 + sizing死结终结 + 直接rating

Week 5+ (Phase C，进化架构，治本mode collapse):
  C1 elite archive(主工程) → C2 skill library + C3 novelty gate + C4 ablation
  ↓ 解锁：种群进化 + 维度枯竭根治

Week 8+ (Phase D，多样性升级，依赖C1):
  D1 AutoQD → D2 CSRO nemesis → D3 crossover多样化 + D4 PSRO升级(MSNE形态)
```

**决策点**：Phase A 完成后评估——若 AIVAT 审计（A2）发现 heuristic p-hacking 漏洞，应优先修评估再继续 B（避免在失真评估上进化）。Phase B 完成后评估——若 rating 仍停滞，说明问题是 mode collapse 而非策略贫乏，跳到 Phase C。

---

## 附录：关键 file:line 索引

- 流水线：`orchestrator.py:107,885`；`generation_scheduler.py:67,380,678`；`tool_planning.py:314,1123`；`tool_gates.py:100,452,649`；`tool_eval.py:252`；`tool_commit.py:54,309`
- stage gate：`evolution_infra.py:93,600`
- 策略决策：`bots/claude_v130/strategy.py:583-664,1078`；`strategy_helpers.py:326,356`
- 经验池：`web/core/experience_pool.md`
- Master 指令：`web/core/prompts/master_prompt.md`
- reap/crossover：`generation_scheduler.py:599`；`tool_bot_management.py`
- 对手画像：`opponent.py`/`line_reading.py`（Phase3）
- Phase1 AIVAT / Phase3 MAP-Elites / Phase4 PSRO：见 memory `phase1-aivat`/`phase3-famou-mapelites`/`phase4-async-qd-psro`
