# 进化系统重新理解 + Exa新鲜文献调研 + 可行性计划（2026-06-21 刷新版）

> 本文由 29-agent 多阶段工作流综合而成：4 个并行**代码核查 agent**（逐 file:line 验证当前状态）+ 8 路 **Exa 文献检索**（2024-2026，每路搜索→深读 top 1-2 源）+ 1 个 Opus 综合 agent。
> **与 Jun20 报告（`docs/evolution-strategy-survey-jun20.md`）的关系**：Jun20 是面向 v130 的 4-agent 调研；本文是面向 **v144/v145 最新状态**的刷新，核查发现 Jun20 之后**三个根因已被精确定位到具体代码行**，直接重排优先级。文献有大量新增（DeepEvolve、Ratchet、jingu、PokerSkill、QDSP、CoCoEvo、Wesker 等）。**不重复 Jun20，互补更新。**
> 另：peer 会话独立深读了 DeepEvolve（arxiv 2510.06056），其成果（六大模块/防污染三重闸门/MAP-Elites+Island 精确超参/Debug agent 增益数据/4 步落地路径）已并入本文 §C 与 §E（集成检索专章）。

---

## 0. 执行摘要（TL;DR）

v144 已确认 LIVE-reachable 且 preflop 进攻轴干净（v145 正在跑第三条 `bb_vs_raise_opp_sizing_delta` 轴）。但本次核查揭示 **Jun20 之后三个事实比当时判断更严重**，直接重排优先级：

1. **INERTNESS 验证缺口 = 头号系统性病灶，且根因已被精确定位**。daemon 的 `_PersistentBot.call()`（`battle.py:78-90`）**只读 stdout、从不动 stderr** → 6+ 代所有 `FOLD_GATE_FIRE`/`SB_OPEN_OPP_SIZE`/`BB_VS_RAISE_OPP_SIZE` 遥测在生产环境**永久丢失**。且 v138-v143 的 guard 全部放在 `to_call>=my_chips` early-return（`strategy.py:1007`）之后 = **结构性 placement shadow**。这不是"难以验证"，是"**系统设计上验证不了**"。

2. **0%-fold + -20k stack-off 泄漏 5 代未修的根因也已被精确定位**。allin-cover 块（`strategy.py:1007-1034`）**根本没有 fold gate**，`win_rate >= shove_odds + shove_buffer`（buffer 上限 +0.14）使 marginal 手永远 call，所有下游 guard 结构性不可达。修复是**闭式数学**（SPR commitment 矩阵 + pot-odds equity 门槛 + 把 gate 插在 early-return 之前）——不需 solver、不会被 dir-audit 判为"exhausted fold axis"。

3. **MAP-Elites / PSRO / AIVAT / exploitability-probe 全是"装饰性"**。`PSRO_ENABLED` 默认 OFF（145 代 0 事件）；AIVAT 488 行纯 dead code（`aivat_enabled=False` 生产默认）；probe 退化为 1.0/1.0 无信号；direction_auditor 是 advisory 非 binding（v136/v137/v144/v145 全 `repetition_detected=True` 仍提交）；crossover 父选择按 `h2h_avg_wr + 版本差≥3` = **同源近亲杂交**（mode collapse 直接机制）。

**最高 ROI 路径（详见 §4）：**
- **Phase A（立即止血）**：(A1) 一行 `battle.py` 改动让 daemon 捕获 stderr（解锁 6+ 代失信号，30 分钟 ROI 最高）→ (A2) `strategy.py:1007` 块内插 SPR fold gate 修 5 代泄漏 → (A4) 把 FABRICATED replay 做成确定性 evidence_gate 硬校验。
- **Phase B（策略）**：preflop 轴顺延到 bb_vs_limp iso-raise + SPR commitment 矩阵（闭式数学修 0%-fold）+ adaptation-safe 置信度门（防被反利用）。
- **Phase C（架构）**：把已存在但孤悬的 MAP-Elites 多样性信号接入选父 + advisory 升 binding + 6 birth requirements 从自由文本提升为代码硬校验 + Debug sub-agent。
- **Phase D（评估质量）**：hold-out 对手集（防 Goodhart）+ all-in adjusted winnings（降 precommit 噪声）+ experience_attribution（经验池治理）。
- **§E（用户重点新需求：Exa 检索进进化）**：Master 之前可选 `literature_probe` 阶段（DeepEvolve plan/search/reflect/write）+ Ratchet 式 governance（outcome-driven retirement + active-cap + translation/verification gate）防检索噪声污染。**核心原则：检索治理 > 检索本身（Ratchet："bottleneck is the librarian, not the author"）。**

---

## 1. 系统现状诊断（基于逐 file:line 核查，全部可 grep 验证）

### 1.1 当前进化状态

- **v144 已 COMMITTED + PUSHED**（`bot-v144@259c618`）。v144 新 preflop 进攻 `sb_open_opp_sizing_delta()`（`strategy_helpers.py:961`）核查确认 **LIVE-reachable**：单 call-site `strategy.py:459`，无 shadow，3 个 opp 信号（`fold_to_open_preflop`/`threebet_vs_open`/`open_response_confidence`）producer（`opponent.py:314-317`）+ consumer 双侧验证。
- **v145 正在 live**（`pipeline_state source_v=144, stage=reviewed`，git status `?? bots/claude_v145/`）。Master plan 已 pivot 到第 3 条 preflop 轴 `bb_vs_raise_opp_sizing_delta()`（`strategy.py:521-551` bb_vs_raise 分支 + `choose_raise:279` 新增 sizing 分支）。review 已过。
- 活跃池 38（>30 上限，待 cleanup）。v144 `strategy.py=1691` 行（近 2000 上限），已有 13 个 .py 模块。

### 1.2 五个头号问题的**真实**状态（核查后）

| # | 问题 | 核查后真实状态（file:line） | 严重度 |
|---|---|---|---|
| 1 | **INERTNESS 验证缺口** | **三重根因**：(a) `battle.py:54` `_PersistentBot` 设 `stderr=subprocess.PIPE` 但 `call()`（`battle.py:78-90`）**只读 stdout.readline()，从不读 stderr** → 所有遥测在 daemon 路径永久丢弃；(b) placement shadow：v138 `_river_stackoff_guard`（`strategy.py:1041`）在 `to_call>=my_chips` early-return（`strategy.py:1007`）之后；(c) `code_verification.py:131` 只做 py_compile + AST dead-code，**不做 dispatch-reachability**；`decision_tester.py` 仅 15 个通用 fixture，`_CONSTANT_TEMPLATE_MAP`（`:230`）不含 `SB_OPEN_OPP_SIZE` 等新常量 | **CRITICAL** |
| 2 | **0%-fold + -20k stack-off** | allin-cover 块（`strategy.py:1007-1034`）用 `win_rate>=shove_odds+shove_buffer`（buffer 上限 +0.14）决定 call(-2) vs fold(-1)。`made_strength~0.40-0.50` vs polarized all-in（`shove_odds~0.40`）的 marginal 手**永远 call**。7 个 `should_fold_postflop` gates、`_river_stackoff_guard`、`check_raise_pressure`、`barrel_pressure_profile` **全部位于下游 `to_call>0`(但<my_chips) 子块**，never 见此分支。postflop bet-side 进攻轴（`_river_*_guard` 家族 4 次）已 EXHAUSTED | **CRITICAL（5 代未修）** |
| 3 | **MAP-Elites/PSRO/AIVAT/probe 全装饰性** | (a) `behavior_archive.json` 16 niches 全 `eval_mode='single'`，0 个 k=3 median 存活过 rebuild（write race）；(b) `PSRO_ENABLED` 默认 OFF（`tool_eval.py:330` env var），`mixture_config.json` 从未写入，145 代 0 事件；(c) AIVAT 488 行纯 dead code（`tool_eval.py:614 aivat_enabled=False` 生产默认）；(d) `exploitability_prober.py:42-49` 4 个 trivial probe（`always_caller`/`min_bettor` 等）全返回 1.0/1.0 无信号；(e) direction_auditor 是 advisory 非 binding（`tool_planning.py:306-308` exhausted 仅 `warnings.append('advisory')`） | **HIGH** |
| 4 | **FABRICATED replay（9 代复发）** | master/worker/critic prompt **无 `ls match_replay` 验证**；replay 文件实际 timestamp 命名（`20260620_160503_*.json`），agent 捏造 `G3H25` 格式。6 birth requirements + FABRICATED 要求**全是 `experience_pool.md` 自由文本，无代码强制** | **HIGH** |
| 5 | **评估噪声** | precommit 8-40 局，NLHE mirror 单局方差 ~100bb/100，`-2000` threshold 落噪声带内 → v141(25W-33L)、v142(22W-36L) 均 pass。CS early-stop 仅对 parent 触发（`tool_eval.py:602`）。要分辨 5bb/100 真实改进需 ~1400 局（数学下界），8-40 局只能做"崩溃粗筛" | **MEDIUM** |

### 1.3 mode collapse 的直接机制（核查确认）

- `_pick_crossover_parents`（`generation_scheduler.py:599-647`）按 `h2h_avg_wr` 排序 + 版本差≥3 选父。实测当前 ranked：v139/v138/v137/v136/v133/v129/v127/v117/v126/v143——**全部 v127+ 近代同源**。"版本差≥3" 是顺序过滤不是结构多样性度量 → **crossover 实质是同源近亲杂交**。
- `behavior_archive.json`（MAP-Elites 16 niches）写盘后**几乎无人消费**：唯一读它的 `generation_scheduler.py:351` 只把 top niche bot 写进 `priority_eval` 队列，**不进 Master prompt、不进 `_decide_strategy`、不进 `_pick_crossover_parents`**（grep `agent_master.py`+`master_prompt.md` = 0 命中）。QD fitness 完全旁路方向选择与父选择。

---

## 2. 文献资料库（2024-2026，按主题，URL 可追溯）

> 供 Master/Worker prompt 注入与选型。标注 **codability**（能否编码进规则 bot）。Jun20 报告已有文献此处不重复，仅列新增。

### 2.1 INERTNESS 检测（codability HIGH — 纯 Python）

| 文献 | URL | 核心可迁移内容 |
|---|---|---|
| **CoCoEvo（程序+测试协同进化）** | arxiv.org/abs/2502.10802 | Stop-n 早停=变异有效性检测器（连续 n 代 fitness+测试通过集不变=INERT）；line coverage 反馈驱动测试生成（未覆盖行→生成触发它的测试）；Discrimination 熵（pr≈0.5 的边界场景区分度最高）→ 构造 mutation-detection fixture |
| **Wesker（AST 变异测试）** | github.com/rohanvinaik/Wesker | 7 类变异（VALUE/BOUNDARY/ARITHMETIC/LOGICAL/SWAP/STATE/TYPE）；**等效变异体检测**（编译原函数+变异函数，合成边界输入跑，输出全同→等效/INERT）；有效杀伤率 `killed/(tested-equivalent)`。**这是检测"detector 是否 INERT"的最直接机制**——不是测"有没有被调用"，而是测"改了行为后测试能否发现差异" |

### 2.2 stack-off / SPR commitment（codability HIGH — 闭式数学）

| 文献 | URL | 核心可迁移内容 |
|---|---|---|
| **GTO Wizard: SPR** | blog.gtowizard.com/stack-to-pot-ratio/ | **break-even equity 闭式**：`required_eq = to_call/(pot+2*to_call)`（SPR1→33%, SPR2→40%, SPR3→43%）。SPR 档位×手牌档位 commitment 矩阵（SPR~2→顶对+commit；SPR~5→顶对 dicey；SPR~16→单 pair indifferent/fold）。robustness（set/draw 随对手范围变强 equity 下降速度不同）→ **`made_tier × board_draw × SPR` 三维矩阵决定哪些手值得 build pot**。**这正是修复 0%-fold 的数学锚点：river 面对全下 equity<pot_odds 必弃** |
| **PokerSkill（training-free HUNL）** | arxiv.org/html/2605.30094v1 | ATT/DEF Budget 系统：23 类 made-hand + 8 类 draw × pot_type × board_texture 的精确 ATT/DEF 表。**trash 类 DEF=0（fold to any bet）= 修复 0%-fold 的硬 gate**；ALL-IN RULE：facing all-in implied odds=0，必须 equity≥pot_odds；board-texture 罚分（one-card-flush overpair drop 3.5 级→weak/trash）。River raise-after-bet DEF-0.3 |

### 2.3 preflop 进攻（codability HIGH — solver-validated）

| 文献 | URL | 核心可迁移内容 |
|---|---|---|
| **GTO Wizard: HU SB 偏差 exploit** | blog.gtowizard.com/heads-up-exploiting-sbs-preflop-mistakes/ | 3 类 SB 偏差→BB exploit 矩阵（50bb solver nodelock）：(1) SB over-fold→iso-raise **更小 size**（4bb vs 6bb）+ 更宽 range；(2) SB open 太大→fold 更多 + 3-bet 更强 range + A/K blocker 3-bet + marginal shove；(3) SB open 太频繁→limp iso 更宽更小。**v144 sb_open 对 fold-prone 是 size UP（+0.30）可能错——over-folder 应 size DOWN 到 min-raise 扩宽 open 频率** |
| **GTO Wizard: raise sizing 系列** | blog.gtowizard.com/preflop-raise-sizing-examining-2-key-factors/ | Fold Equity vs Size 曲线（3bb 比 2bb risk 多 50% 只多得 5-10% folds；唯一显著受益于大 size 的是 BB fold）；短栈 iso 分桶（all-in iso/big 6bb iso/small 3bb iso，按 limp range equity 组成）；limper 剥削（limp-fold→iso wide 极化大 size；calling-station→更大 iso+thin value；limp-3bet→极强只 JJ+/AK）。blocker preflop（A/K blocker 做 3-bet/bluff） |

### 2.4 mode collapse / novelty（codability HIGH — 纯 numpy）

| 文献 | URL | 核心可迁移内容 |
|---|---|---|
| **AutoQD（ICLR 2026）** | openreview.net/forum?id=FNnJIf4ymV | occupancy measure RFF 嵌入自动生成行为描述符（D=100, k=4, γ=0.999, cwPCA fitness-weighted）；**Vendi Score VS=exp(-Σλlogλ)**="有效种群大小"可比不同大小种群；qVS=fitness×VS。扑克部分可观 caveat：用"决策指纹"（observable state-action 频次）替代真 occupancy。代码 github.com/conflictednerd/autoqd-code |
| **QDSP / FMSP（NeurIPS 2024 WS）** | openreview.net/forum?id=cY8jkxlNDi | "Dimensionless" MAP-Elites（FM 自判新颖性，不需手工 BD）；**novelty gate BINDING**（FM-as-judge 裁定不新颖且性能不比邻居高→丢弃，非 advisory）；local competition（只与行为最近邻比，"fast ants vs fast cheetahs"）；archive 250-300/侧 |

### 2.5 评估方差削减（codability MEDIUM — 需 judge.py MC）

| 文献 | URL | 核心可迁移内容 |
|---|---|---|
| **AIVAT 原始（Burch 2018 AAAI）** | arxiv.org/abs/1612.06915 | AIVAT HUNL SD↓85%（28 局 freezeout 即显著）；all-in adjusted winnings（MIVAT 子集）= 最便宜可编码运气修正（preflop/flop all-in 时用 MC 算 equity 替换实现盈亏，无偏降方差） |
| **Kim-Sandholm 2026（AIVAT p-hacking）** | arxiv.org/html/2605.14261v1 | **heuristic value function MUST 固定在观察评估数据之前**（否则可被梯度下降伪造 p<10⁻³⁷³ 结论）；uncertainty propagation + IVW 再降 43% 样本。**直接命中本系统 AIVAT heuristic p-hacking 隐患** |
| **AgentAssay（回归测试统计框架）** | arxiv.org/abs/2603.02601 | 三值 verdict（Pass/Fail/Inconclusive）；固定样本量公式 `n*=ceil((z_α+z_β)²·[p_b(1-p_b)+p_c(1-p_c)]/δ²)`；SPRT 序贯停止（最小化 E[N]）；自适应预算（先跑 5-10 校准方差，stable agent 减 4-7×）；行为指纹 Hotelling T² 多变量回归 |

### 2.6 证据完整性硬 gate（codability HIGH — 纯 Python regex+hash）

| 文献 | URL | 核心可迁移内容 |
|---|---|---|
| **jingu-trust-gate（确定性证据准入）** | github.com/ylu999/jingu-trust-gate | 零 LLM 4 步 pipeline（validateStructure/bindSupport/evaluateUnit/detectConflicts）；Claim 状态机（approved/downgraded/rejected/approved_with_conflict）；reason codes（MISSING_EVIDENCE/OVER_SPECIFIC/INFERRED_NOT_STATED/SCOPE_EXCEEDED）；**claim 强度阶梯 observation<symptom<hypothesis<root_cause，每升一级抬高 evidence bar**（"一个 log line 不足以断言 root cause"=对应"单条 replay 不足以断言结构改进"） |
| **NabaOS（Tool Receipts）** | arxiv.org/pdf/2603.10060 | HMAC-signed receipt（LLM 无 signing key 无法伪造）；检出 94.2% fabricated tool refs / 87.6% count misstatements；independent URL re-fetch 抓 78.4% URL 伪造；<15ms 开销。**落地为：replay 引用附加 SHA-256 前 8 字符锚，commit gate 重算比对** |
| **TRACE（implicit reward hacking）** | arxiv.org/abs/2510.01367 | 渐进截断 CoT 绘 reward vs %CoT-used 曲线；hacking model 早期急升后 plateau（high AUC）。"exploiting loophole 比解题容易"=可观测信号 |

### 2.7 检索进进化 + library drift 治理（codability HIGH — 用户重点需求）

| 文献 | URL | 核心可迁移内容 |
|---|---|---|
| **DeepEvolve（AlphaEvolve + Deep Research）** | arxiv.org/html/2510.06056v1 | 六模块 plan/search/reflect/write/code/eval+evolution。**防污染三重闸门**：(1) proposal 必须含伪代码；(2) s=0 兜底（调试 budget=5 失败判零分）；(3) reflection + eval 回流闭环。精确超参：MAP-Elites 10 archive/10 bins/3D(perf,diversity,complexity)/elite ratio 0.1；Island 5 islands/25 pop/exploit:explore=0.7:0.3/migration int=25 rate=0.1。Ablation：deep research vs pure evolution（Molecule 0.797→0.815, Circle 2.735→2.981）。**Debug agent Table3：成功率 0.13-0.65→0.49-1.00**。4 步落地：debug_worker > master_prompt early/late gating > research_strategy MCP+blacklist > MAP-Elites crossover parent2 |
| **Ratchet（library drift 诊断修复）** | arxiv.org/html/2605.19576v1 | **Library Drift**（累积技能使性能低于无技能基线，silent 失败）。三阶段（accumulation without quality signal → retrieval degradation → silent injection harm）。**治理三件套**：(1) outcome-driven retirement（n≥N_min=100 且 ĉ≤-τ=0.10 时驱逐）；(2) bounded active-cap C=50；(3) meta-skill authoring prior（强制结构同质）。**消融**：no injection +0.002（技能创建本身无增益）；retrieval-only +0.077（LLM gate 超 tf-idf）；no meta-skill +0.187（**meta-skill 是单一最值钱组件**）；harsh retirement(N_min=20) **-0.019（主动伤害）**；cap=100 仅增方差。SkillsBench：无治理 LLM 自写知识 +0.0pp vs human-curated +16.2pp |

### 2.8 exploit 框架 + adaptation safety（codability HIGH）

| 文献 | URL | 核心可迁移内容 |
|---|---|---|
| **GTO Wizard: 五类 exploitable imbalance** | blog.gtowizard.com/the-five-imbalances-of-exploitative-poker/ | Betting Volume=Σ(频率×尺寸)；5 类（Betting Volume/Equity Management/Polarity/Elasticity/Board Coverage）每类精确触发条件+sizing 映射（nodelock EV 差量化）。如：over-folder→size DOWN（even set 用 33%pot）；calling-station→thin value + size up；over-folder to overbet→提高 overbet 频率 52%→66%。**v144 sb_open 对 fold-prone size UP 可能与五类规则冲突** |
| **OX-Search（Adaptation Safety）** | proceedings.mlr.press/v235/ge24b.html | Adaptation Safety（exploit 策略可利用度≤不 exploit 时，弱化的 safety 定义）；Theorem 4.4：`u₂≥u₂^blueprint + (1-ε)δ`，ε=估计误差。**落地为置信度门**：exploit 强度按 (1-ε)=confidence 缩放，模型越不确定越保守但绝不反向亏损。直接对应本系统 exploit 函数无 safety floor 的 -20k 巨损来源 |

### 2.9 reward hacking / Goodhart（codability MEDIUM）

| 文献 | URL | 核心可迁移内容 |
|---|---|---|
| **Self-Improving Code Agents RSI** | arxiv.org/abs/2603.28063 | temporal 关键发现：优化步数 10→100，hacking 占比 26.4%→57.8%（越进化越易拟合评估噪声）。Kernel-Bench 73.8% 优化属纯 proxy gain。Retrospection（proxy metric 跳变时触发 self-critique）↓17-19pp hacking。**hold-out real test set 防 Goodhart** |
| **dietz2025 / cross-judge volatility** | arxiv.org/abs/2503.11926 | 每次用 diverse judges suite 打分，大 volatility=过拟合 benchmark；保留对开发者**隐藏**的 human-labeled relevance judgments 周期测试（hold-out） |

---

## 3. 可行性分析：文献与当前问题的交叉信号

### 3.1 INERTNESS 是头号问题（多方共识 + 根因已定位）

- **代码核查**：daemon 不读 stderr（`battle.py:78-90`）+ placement shadow（`strategy.py:1041` 在 `:1007` early-return 之后）+ code_verification 不查 dispatch-reachability。
- **CoCoEvo/Wesker**：Stop-n 早停 + AST 变异测试可直接检测 INERT（改行为后决策分布不变=INERT）。
- **Wesker 的关键洞察**：不是检测"有没有被调用"（那是 coverage），而是检测"**改了行为后测试能否发现差异**"——这正是本系统需要的。
- → **结论**：A1（battle.py stderr 捕获）+ A3（dispatch-reachability AST）+ 变异测试三件套是根治 6+ 代反复踩坑的根本，且 v144/v145 现成 detector 是首个验证目标。

### 3.2 0%-fold 是闭式数学问题（不靠 guard 手调）

- **GTO Wizard SPR** + **PokerSkill ALL-IN RULE**：river 面对全下 `required_eq=to_call/(pot+2*to_call)` 是数学事实，`made_strength` 映射 equity 后与 pot-odds 比 → equity<pot_odds 必弃。
- → **结论**：SPR commitment gate + placement 修复（gate 插在 `:1007` 之前）是 5 代泄漏的闭式修复，不需 solver、不碰被 dir-audit FORBADE 的 fold-gate 重调参（是"新函数 + 新 SPR 档位"=新轴）。

### 3.3 MAP-Elites 信号已计算但孤悬 → mode collapse 可治

- **AutoQD/QDSP**：Vendi Score（纯 numpy）+ cwPCA + binding novelty gate。
- 代码现状：`behavior_archive.json` 16 niches 已存在但不进选父/选方向。
- → **结论**：C1（接入选父）+ C2（fingerprint/VS）+ C3（novelty gate binding）把孤悬信号变 binding，是最低成本的 mode collapse 治理。

### 3.4 检索进进化：治理 > 检索本身（Ratchet 核心教训）

- **Ratchet 消融**：无治理检索=+0.0pp（SkillsBench），harsh retirement=主动伤害（-0.019）。
- **DeepEvolve**：检索作为 PLAN→SEARCH→WRITE 三步的**证据输入**（非直接进代码）+ reflection + eval 回流闭环。
- → **结论**：§E 的 literature_probe 必须**同时**上 Ratchet governance，否则复刻 +0.0pp。

---

## 4. 行动计划（分阶段，按 ROI，file:line 已验证）

### Phase A — 止血验证（立即，独立，ROI 最高）

| ID | 行动 | 解决 | 难度 | 收益 | 落地位置 |
|---|---|---|---|---|---|
| **A1** | `_PersistentBot.call()` 的 stdout readline 后**非阻塞读 stderr**（select/线程，timeout=0.05s），附加到返回值 `stderr_output`；elo_daemon 写入 `results/bot_telemetry/{bot}.jsonl`。**单一最高 ROI 改动**——解锁所有遥测验证 | INERTNESS #1 根因 | low | **high** | `web/core/engine/battle.py:78-90` + `web/core/elo_daemon.py:370` |
| **A2** | `strategy.py:1007 if to_call>=my_chips:` 块内**第一行**（shove_odds 计算之前）插 SPR fold gate：`round_idx==3 AND tier∈{thin,none} AND made_strength<0.55 AND win_rate<shove_odds+0.10 → return -1`。guard 必须 FIRST | 0%-fold 5 代泄漏 | medium | **high** | `bots/claude_v{N}/strategy.py:1007` |
| **A3** | `code_verification.py:verify_code()` 末尾加 `verify_dispatch_reachability()`：AST 解析 strategy.py，检查每个 `_*_guard`/`sb_open_*`/`bb_vs_*` 调用点是否在更早的 `to_call>=my_chips` return 之后（shadow 检测）。0 调用点→reject 'dead code' | placement shadow 自动化 | medium | **high** | `web/core/code_verification.py:131` + `decision_tester.py:230` |
| **A4** | `tool_planning._validate_master_plan` 后加 `verify_cited_replays`：正则抽 `G\d+H\d+#hash` 或 timestamp 引用，stat match_replay 文件存在性 + SHA-256 前 8 字符比对，缺失→`plan_errors`。`replay_spotlight.py` 生成时附加锚 | FABRICATED replay 9 代复发 | low | **high** | `web/core/tool_planning.py:296-310` + `replay_spotlight.py` |
| **A5** | 新增 `research_strategy()` MCP 工具，`run_direction_audit` 后、`run_master` 前（仅 stagnation 触发）。4 步 plan→search(Exa 白名单)→reflect gate→write(伪代码+四元组)。输出 `results/research_proposals/{gen}.json` + 注入 master_prompt 作 **hypothesis 来源**（非直接进代码） | 用户重点需求：Exa 进进化 | medium | **high** | `web/core/tool_planning.py` + `prompts/master_prompt.md` |
| **A6** | 新增 `research_governance.py`：(a) web 候选加 4 字段 `born_gen/trials/attributed_hurt/attributed_help`，`ĉ=(help-hurt)/max(trials,1)`；(b) daemon≥30g H2H vs 来源漏洞对手 WR 未提升→retire 进 `web_blacklist.jsonl` 永久禁同 pattern；(c) active-cap C=5（独立池）；(d) translation gate（claim 必须翻译成 target_fn+改法）+ cooldown（注入代 precommit FAIL→下 2 代禁检索） | 检索噪声污染（Ratchet） | medium | **high** | `web/core/experience_pool.py` + `agent_review.py` critic + 新建 `research_governance.py` |
| **A7** | exploitability_prober 4 个 trivial probe（全 1.0/1.0）替换为：(a) 当前 champion bot 作 probe；(b) pot-odds probe 测 fold 阈值；(c) overbet-bluff probe 按 bot sizing 校准；(d) gate probe 难度于 bot Glicko | probe 退化无信号 | medium | medium | `web/core/exploitability_prober.py:42-49` |

**Phase A 依赖**：全部独立。A1 是其他 telemetry 验证的前提（必须最先）。A5 必须配 A6 同时上（否则复刻 Ratchet +0.0pp）。

### Phase B — 策略丰富化（preflop 轴扩展 + SPR 修泄漏）

| ID | 行动 | 解决 | 难度 | 收益 | 落地位置 |
|---|---|---|---|---|---|
| **B1** | 新增 `_spr_commitment_gate(spr, made_strength, made_tier, round_idx, to_call, my_chips, has_draw, pot_odds, my_equity)`：ALL-IN 判定 `to_call>=my_chips*0.95→implied=0`；FOLD 条件 (a) 非{nut,strong} 且 equity<pot_odds；(b) river tier∈{thin,none} 且 equity<pot_odds+0.05；(c) SPR>4 单 pair→FOLD vs jam。equity 用 `simulation.py monte_carlo_weighted_equity(iterations=300, 固定种子)` | 0%-fold 闭式数学根因 | medium | **high** | `bots/claude_v{N}/strategy_helpers.py` + `constants.py` |
| **B2** | 把 `_river_stackoff_guard`（`strategy.py:1041`）及 `_spr_commitment_gate` 调用**复制/移动到 `strategy.py:1007 if to_call>=my_chips:` 块内 FIRST 行**（shove_odds 计算之前）。同时检查 preflop `:65` 的 to_call>=my_chips 是否也 shadow。新增 invariant 注释 + experience_pool 记录 'PLACEMENT SHADOW INVARIANT' | placement shadow（v138-v143） | low | **high** | `bots/claude_v{N}/strategy.py:1007` + `:65` |
| **B3** | v145 已在 bb_vs_raise 轴。顺延第 4 轴 `_bb_iso_limp_sizing()`：需 `opponent.py` 新增 `limp_freq` 采集（SB 位 first_action==call 频率，仿 `classify_sizing_tendency`）。limp-fold 系→iso wide+极化大 size(6-7BB)；calling-station→更大 iso+thin value；limp-3bet 系→只 JJ+/AK。conf<0.20→return None | preflop 轴顺延（sb_open+bb_vs_raise+bb_vs_limp） | medium | medium | `strategy_helpers.py` + `strategy.py` bb_vs_limp 分支 + `opponent.py` |
| **B4** | 新增 `_adaptation_safe_clamp(action, opp_signal, confidence, samples, blueprint_default)`：`effective=blueprint_default+(exploit_offset*confidence*sample_gate)`，`sample_gate=min(1,samples/20)`。包装所有 exploit 函数（sb_open/bb_vs_raise/bb_vs_limp/value_maximizer_overbet），硬阈值门→置信度衰减。低样本(<6)不触发激进 exploit→防被反利用 | exploit 无 safety floor | medium | medium | `strategy_helpers.py` + `opponent.py`(estimation_error=1-conf) |
| **B5** | `decision_tester.py:TEMPLATE_SCENARIOS` 加 `BB_VS_RAISE_OPP_SIZE`/`SPR_FOLD`/`adaptation_clamp` 等新常量；每个新 detector 构造输入恰好满足其条件的 scenario，跑 bot 验证 decision 与预期一致 | fixture≠live | low | medium | `web/core/decision_tester.py:230` |

**Phase B 依赖**：A2/B1/B2 可并行（A2 是 B1 最小 diff 版）；B3 依赖 v145 完成；B4 独立 infra。**注意**：fold-side 仍受 dir-audit FORBADE 约束——B1/B2 包装为"SPR-commitment 结构性新 gate"（新函数+新 SPR 档位），非重调 `_river_stackoff_guard`。

### Phase C — 进化架构治 mode-collapse（把装饰性信号变 binding）

| ID | 行动 | 解决 | 难度 | 收益 | 落地位置 |
|---|---|---|---|---|---|
| **C1** | `_pick_crossover_parents`（`generation_scheduler.py:599`）从 h2h_avg_wr+版本差≥3 改为**消费 behavior_archive**：parent_a=不同 niche 的 top bot，parent_b=不同 Glicko r-2*rd 区间的 bot | mode collapse 直接机制 | medium | **high** | `web/core/generation_scheduler.py:599-647` |
| **C2** | 新增 `behavior_diversity.py`：`compute_decision_fingerprint(bot_dir, n_games=200)→np.float32[D=256]`，从 decision_tester 200 盘 fixture 抽 7 维 key（round_idx/made_str_bucket/pot_odds_bucket/to_call_to_pot_bucket/is_opp_aggr/street/action_bucket），RFF 映射。post_generation_cleanup 末尾算 fingerprint 存 `fingerprints.jsonl`。Vendi Score=exp(-Σλlogλ) | mode collapse 无数值化度量 | medium | **high** | 新建 `web/core/behavior_diversity.py` + `generation_scheduler.py:post_generation_cleanup` |
| **C3** | `tool_commit.commit_bot` commit 前算"加入新 bot 后 pool VS" vs "替换 reap 后 pool VS"；`ΔVS<+0.05 AND rating 提升<30 Glicko→REJECT commit`+反馈 Master 'novelty INSUFFICIENT, pivot axis'（binding）。或：direction_auditor confidence=high 且 exhausted 命中硬 token 时从 warnings 升 plan_errors | direction_auditor advisory | medium | **high** | `web/core/tool_commit.py:commit_bot` 或 `tool_planning.py:296` |
| **C4** | `_validate_master_plan` 加 `verify_cited_replays`（复用 A4）+ 检查"NEW detector+NEW opponent-line signal+≥3 sites"6 birth requirements。`worker_failures`/`research_blacklist` 连续 2 次 precommit 失败的 URL+轴自动标 exhausted | 6 birth requirements 无代码强制 | low | medium | `web/core/tool_planning.py` |
| **C5** | Worker 失败时 capture py_compile/smoke error→LLM debug（budget 5 attempts）→失败 task score=0 进 worker_failures。**DeepEvolve Table3：debug 把成功率 0.13→0.99** | worker 失败无 systematic debug | medium | medium | `web/core/agent_workers.py` + `tool_gates.py` |

**Phase C 依赖**：C1/C2 可并行（C1 若用 VS 依赖 C2）；C3 依赖 C2；C4 复用 A4；C5 独立。

### Phase D — 多样性与评估质量（激活 PSRO/AIVAT/hold-out）

| ID | 行动 | 解决 | 难度 | 收益 | 落地位置 |
|---|---|---|---|---|---|
| **D1** | 修复 `qd_async_eval` worker thread 与 daemon `write_behavior_archive` 的 write race：daemon rebuild 用 bot-name 索引（已尝试 `prior_k3_by_bot`）保留 k3 fields，验证 k3 能 survive 一次 rebuild cycle | MAP-Elites k=3 从未 survive | medium | medium | `web/core/map_elites.py:38` + `qd_async_eval.py:370-388` |
| **D2** | 从 `bots/graveyard` 抽 5-8 个无血缘 reference bot 作固定 hold-out 锚点。每代 precommit 加跑 vs hold-out 集。commit gate 加 `daemon_rating_delta>0 AND holdout_wr_delta>-2%`；master/worker context mask 这些 id 为 `OPP_HELDOUT_1..5` | Goodhart（过拟合 daemon 配对） | medium | **high** | `web/core/tool_helpers.py:_select_precommit_opponents` + `tool_eval.py` + `tool_commit.py` |
| **D3** | `battle.py mirror_battle_generator` 当一手 preflop/flop all-in 且有公共牌未发时，用 `engine/judge.py judge()` 对剩余 deck 做 MC（**固定 384 次+固定种子**）算 equity，实现盈亏替换为 equity*pot。`net_chips_adjusted` 并行存，aggregate 改用 adjusted 进 bootstrap_ci | precommit 8-40 局噪声主导 | high | medium | `web/core/engine/battle.py` + `tool_eval.py:360` + 新建 `luck_adjust.py` |
| **D4** | 新增 `experience_attribution.py`：每条 experience 加 4 字段 `born_gen/trials/attributed_hurt/attributed_help`，`ĉ=(help-hurt)/max(trials,1)`。trim 从 'keep<120 lines' 改 'keep top C=50 by ĉ, retire n≥20 且 ĉ≤-0.10'。worker gate：经验行 `attributed_hurt≥3 或 ĉ<-τ_exp` 强制跳过注入 | experience_pool 无 retirement | medium | medium | `web/core/experience_pool.py` + 新建 `experience_attribution.py` |
| **D5** | 每 N=5 代或 stagnation 触发，对 archive BD 向量做 PCA 得 2-3 主方向，连续 3 代方差下降→触发多样性强制指令。Master prompt 注入"当前 pool VS=X.XX, 最近 5 代 ΔVS, 最拥挤 BD cell, NOVELTY BUDGET" | Master 决策无多样性硬约束 | low | medium | `web/core/orchestrator_context.py` + `prompts/master_prompt.md` |

**Phase D 依赖**：D1 独立；D2/D3 可并行（都改 eval）；D4 独立；D5 依赖 C2。D3 必须固定 MC 种子（防 p-hacking）。

---

## 5. §E 集成 Exa 检索进进化过程（用户重点新需求）

> **核心原则（Ratchet）**："bottleneck is the librarian, not the author"——检索治理 > 检索本身。无治理检索 = SkillsBench +0.0pp。

### 5.1 何时检索（trigger，非每代）

成本爆炸 + 噪声，三个 trigger（任一）：
- (a) `combined_analyst` 判 stagnation≥2 代
- (b) `direction_auditor` 返回 `repetition_detected=True`
- (c) 每 5 代周期性

检索 query 由**当前最大 H2H 漏洞类型**驱动（从 `replay_analysis` 提取），强制结构化轴（具体到牌型/阶段/street），禁止泛问。例："0%-fold river stack-off HUNL optimal fold frequency"、"SB open size vs 3bet-freq 2024 solver data"。

### 5.2 搜什么 + 谁搜（DeepEvolve plan/search/reflect/write，但 Master 不直接拿 raw 文献）

新增 `run_literature_probe` MCP 工具（`tool_planning.py`，放 direction_audit 后、master 前）：
1. **plan**：用现有 Master agent 生成 3-5 个研究问题
2. **search**：用 Exa（域名白名单 `pokertheory.org`/`upswing`/GTO Wizard blog/arxiv 扑克 AI），每问 top3
3. **reflect gate**（防污染闸）：检索结果与当前 bot H2H 弱点**无对应则 reject 为噪声**
4. **write**：合成 1 个 proposal，**强制带伪代码 + `(made_str band, board_state, sizing_ratio, target_freq)` 四元组**

产出 = 结构化候选行写入独立小池 `web_candidates.md`（cap=5），**不进主 `experience_pool.md`**。

### 5.3 怎么固化进经验池/skill library（Ratchet 三件套）

文献候选**不直接进 bot 代码**。Master 只能把候选作为 worker task 的 *hypothesis*，worker 实现后必须通过现有 quality gates（decision test≥70% + mirror smoke + precommit eval vs parent）。长期保留需满足：
- **contribution score**：4 字段 `born_gen/trials/attributed_hurt/attributed_help`，`ĉ=(help-hurt)/max(trials,1)`
- **outcome-driven retirement**：daemon≥30g H2H vs 来源漏洞对手后 WR 未提升→retire 进 `web_blacklist.jsonl`（pattern+失败原因），**永久禁同 pattern 再检索注入**
- **active-cap C=5**（独立 web_candidates 池）

### 5.4 怎么防检索噪声污染（最关键）

Ratchet 消融 A1/A2 证明无治理检索=+0.0pp 甚至负增益。三重 gate：
1. **Translation gate**：每个 numeric claim 必须由 Worker 翻译成具体函数改动提案（target_fn+具体改法），无 numeric/无 target_fn 直接 drop → 防"看似合理但错"策略
2. **Verification gate（capsule 归因）**：web-derived 改动 commit 后必须 daemon 跑≥N_min_web=30 局 H2H（vs 来源漏洞对手）才能保留
3. **Outcome-driven retirement + cooldown**：web_candidates 注入代 precommit mean<parent（FAIL）→**下两代禁 web 检索**防连续噪声注入；hurt 比例>40%→直接关闭 Web-Retrieval Stage

### 5.5 防 reward hacking via bad retrieval

不变量：检索条目**永远不能降低现有 H2H gate**（核心 grounding=Glicko-2 mirror battle）。每个 web-derived 改动对应 evidence_gate 的 receipt 锚（`research_source` 字段进 `worker_failures`），连续 2 次 precommit 失败→URL+轴进 `research_blacklist.json`。Master context 加一行"本代建议来自 web 检索：[claim]+[source_url]，fail 则 blacklist"。

### 5.6 与现有系统最大风险耦合

本系统最大风险是 **INERTNESS（策略写了不触发）而非知识不足**——deep research 增大假设池但若 dispatch-order shadow 不解决仍 INERT。**research 必须配合 stderr telemetry 捕获（A1）验证 firing**，否则检索来的策略也死在 placement shadow 里。

**落地优先级**：先 A1（stderr 捕获）解锁验证 → 再开 literature_probe → research_blacklist 机制随第一个 web-derived 改动上线。

---

## 6. 风险与陷阱（落地必读）

| 风险 | 缓解 |
|---|---|
| **AIVAT/MIVAT heuristic 被 p-hacking**（Kim-Sandholm §3：数据后调 MC 样本数/种子可伪造 p<10⁻³⁷³） | 严格执行铁律：heuristic value function（MC 样本数 384 + 基于 hand hash 的固定种子）在 prepare_generation Phase1 快照到 frozen `eval_constants_v{N}.json` 并 git add，运行时 `_audit_heuristic_fixed()` 读 frozen 比对不一致则拒绝运行；Worker prompt 禁止 import/read eval 常数（grep gate 拦截） |
| **检索/reward hacking via bad retrieval**（Ratchet：无治理 LLM 自写知识=+0.0pp） | Ratchet 消融 A4(N_min=20,τ=0)=-0.019 主动伤害三 seed 一致→N_min 绝不低于 20（用 30），τ=0.10 起步；active-cap C=50 不贪大（A7 cap=100 仅增方差）；**先建 governance（A6）再开 literature_probe（A5）** |
| **INERTNESS 误判**（A1 stderr 捕获 + fixture 不等于 live fire） | inertness gate 用 decision-distribution JS-distance<0.05 作 INERT 判据（非 stderr grep 依赖）；placement shadow 用 AST 静态检测（A3）；guard 放 early-return 块内 FIRST 行（B2）；fixture 加 dispatch-reachability 断言（grep 调用点≥4） |
| **SPR fold gate 过度收紧丢价值**（SPR>4 单 pair fold 可能丢 top-pair vs bluff 价值） | sticky/calling-station 对手仍 call（break-even equity 成立时 call）；gate 用 Monte Carlo equity 而非纯 made_strength（robustness）；保留 pot_odds 闭式作下界（equity<pot_odds 必弃是数学事实）；daemon≥100g vs sticky 对手验证 WR 不下降 |
| **桶表 overfit**（动态分位数边缘 rebuild 导致 cell 不稳定 + fingerprint 在 POMDP 下非真 occupancy） | σ 用 median heuristic（两两 fingerprint 距离中位数）动态计算不固定（AutoQD 推荐）；cwPCA 按 fitness 加权；crossover 父选从 behavior_archive 不同 niche 选（C1）引入异源基因 |
| **Goodhart**（bot 学对 daemon 配对分布过拟合而非真实改进） | hold-out 集 mask 所有 id 为 `OPP_HELDOUT`（Master/worker context 不可见）；commit gate 加 hold-out WR 条件（D2）；proxy_curve vs real_curve 分叉监控（GOODHART_DIVERGENCE 事件） |
| **exploit adaptation safety 缺失**（低置信度全量施加 exploit→被反利用） | confidence*sample_gate 衰减（低样本<6 不触发激进 exploit）；幅度∝(deviation×conf)弱信号小幅偏离接近 GTO 难被 counter；daemon vs 低样本对手验证 WR 不下降 |
| **direction_auditor 升 binding 误伤合法 novel plan**（`_EXHAUSTED_DIRECTION_TOKENS` 过窄/过宽） | 保留结构性新机制逃生口（cross-gen block 模板）；confidence=high + 硬方向 token 命中才升 plan_errors；exhausted list 用 research_blacklist+worker_failures 自动维护（连续 2 次 precommit fail 自动标）非人工经验池 |

---

## 7. 推荐实施顺序

```
第1周（止血，ROI 最高，全部独立）:
  A1 (battle.py stderr 捕获, 30分钟解锁 6+代失信号) ── 必须最先
    → A2 (strategy.py:1007 插 SPR fold gate 修 5代泄漏)
    → A4 (evidence_gate 防 fabricated replay)
  决策点1（A1 落地后 daemon≥30g）: grep 所有 stderr telemetry (SB_OPEN_OPP_SIZE/BB_VS_RAISE_OPP_SIZE/PROTECT_FLOOR)
    firing<5% → 确认 INERTNESS，进 B 阶段 relax
    firing 正常 → 验证机制工作

第2周（策略+架构并行）:
  B1/B2 (SPR commitment gate + placement 修复, 闭式数学不靠 guard 手调)
    + C4 (复用 A4 replay 校验 + birth requirements 硬校验)
    + C5 (debug sub-agent, DeepEvolve 0.13→0.99)
  B3/bb_vs_limp 等 v145 之后顺延
  决策点2（B2 落地后 daemon≥100g）: worst-swing 应从 -20k 收敛到 ≤-10k
    未收敛 → gate 仍在 shadow，relocate 不是 re-tune（MEMORY 反复教训）

第3-4周（进化架构）:
  A5/A6 (literature_probe + Ratchet governance, 用户新需求核心) ── 必须同时上
    + C1/C2/C3 (MAP-Elites 接入选父 + VS novelty gate binding)
  决策点3（A5/A6 首批 web-derived 改动 daemon 验证后）: 观察 web_candidates hurt 比例
    >40% → 关闭 Web-Retrieval Stage 调查
    <20% → 逐步扩大检索 trigger

第5周+（评估质量）:
  D2 (hold-out 对手集, 防 Goodhart)
    + D3 (all-in adjusted winnings, 降 precommit 噪声) ── 需固定 MC 种子
    + D4 (experience_attribution)
```

**关键约束**：
1. 每阶段落地必须 daemon≥100g 验证才推进下一阶段（MEMORY 一致流程）
2. **A1 是所有 telemetry 验证前提，必须最先**
3. **A5 检索必须配 A6 治理同时上**（否则复刻 Ratchet +0.0pp）
4. fold-side 改动仍受 dir-audit FORBADE 约束——B1/B2 包装为"SPR-commitment 结构性新 gate"（新函数+新 SPR 档位），非重调 `_river_stackoff_guard`
5. research_blacklist 机制随第一个 web-derived 改动上线，防 reward hacking via bad retrieval

---

## 8. 与现有 Jun20 报告 + Phase0-4 的衔接（避免重复）

| 现有 | 本计划如何增强（而非重复） |
|---|---|
| Jun20 Phase A（inertness 三件套） | 本计划 A1 精确定位根因到 `battle.py:78`（daemon 不读 stderr）+ `strategy.py:1007`（placement shadow），给出最小改动锚点；新增 CoCoEvo/Wesker 变异测试具体方法 |
| Jun20 Phase B（扑克策略） | 本计划 B1 用 GTO Wizard SPR + PokerSkill ATT/DEF 的**闭式数学**修 0%-fold（Jun20 只笼统提 commitment 矩阵）；B3 preflop 轴顺延到 bb_vs_limp（v144/v145 已开 sb_open/bb_vs_raise） |
| Jun20 Phase C（进化架构） | 本计划 C1 直接把已存在的 `behavior_archive.json` 接入选父（Jun20 只提 MAP-Elites 概念）；C3 novelty gate 用 Vendi Score 纯 numpy；新增 jingu evidence_gate 防 fabricated |
| Jun20 Phase D（多样性评估） | 本计划 D2 hold-out 集 + D3 all-in adjusted winnings + D4 experience_attribution，全部 file:line 锚定 |
| **集成 Exa 进进化（用户新需求）** | **Jun20 完全未覆盖，本文 §E 是全新专章**，以 DeepEvolve + Ratchet 为理论支撑，给出 trigger/search/governance/blacklist 完整方案 |

**核心原则**：Phase0-4 是"评估与对手建模基础设施"（但本次核查发现大量是装饰性/OFFLINE），本计划 Phase A/B 是"修止血 + 出策略"，Phase C/D 是"把进化从贪心爬山升级为种群进化 + 把装饰性信号变 binding"。不推倒重来，先激活已有 MVP。

---

## 附录：关键 file:line 索引（核查验证）

**流水线/评估**：`tool_eval.py:77,330,602,614`；`map_elites.py:38,270-334`；`psro_meta_solver.py:36`；`qd_async_eval.py:200,370-388`；`qd_fitness.py:31`；`engine/aivat.py:113`；`engine/battle.py:52,54,78-90,146-147`；`elo_daemon.py:370,515`；`exploitability_prober.py:42-49`

**stage gate**：`evolution_infra.py:93`；`tool_gates.py:100,452,649`；`tool_planning.py:296-310,458-468,830-883`；`tool_commit.py:54`

**策略决策**：`bots/claude_v144/strategy.py:459,1007,1034,1041,1179,1421-1466`；`strategy_helpers.py:961,744,797,836`；`opponent.py:280,298-304,314-317,327,640-683`

**方向选择/mechanics**：`generation_scheduler.py:380,539,557,599-647`；`direction_auditor.py:18-231,138-160`；`experience_pool.md:17,34`；`tool_bot_management.py:80`

**验证基础设施（待建）**：`code_verification.py:131`（加 dispatch-reachability）；`decision_tester.py:230`（扩展 _CONSTANT_TEMPLATE_MAP）
