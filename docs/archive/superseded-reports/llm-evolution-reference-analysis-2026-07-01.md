# LLM Evolution Reference Analysis - 2026-07-01

本报告记录本轮拉取到 `ref/llm_evolution/` 的外部参考项目，以及多 agent 只读调研后对本仓库扑克 bot 进化系统的设计建议。

## 本地参考项目

所有项目均以 shallow clone 方式放在 `ref/llm_evolution/`，用于本地研究，不纳入主仓库提交。

| 项目 | 本地目录 | HEAD |
|---|---|---|
| OpenEvolve | `ref/llm_evolution/openevolve` | `80945ed` |
| ShinkaEvolve | `ref/llm_evolution/shinkaevolve` | `85f174b` |
| CodeEvolve | `ref/llm_evolution/science-codeevolve` | `c077959` |
| MadEvolve | `ref/llm_evolution/MadEvolve` | `8b881d3` |
| CORAL | `ref/llm_evolution/CORAL` | `f6c8f67` |
| evo | `ref/llm_evolution/evo` | `1b9807c` |
| EvE | `ref/llm_evolution/eve` | `bc88058` |
| Darwin Godel Machine | `ref/llm_evolution/dgm` | `a565fd2` |
| AFlow | `ref/llm_evolution/AFlow` | `3f45721` |
| GPTSwarm | `ref/llm_evolution/GPTSwarm` | `c23a827` |
| AgentSquare | `ref/llm_evolution/AgentSquare` | `8f5b3fe` |
| MetaGPT | `ref/llm_evolution/MetaGPT` | `11cdf46` |
| ChatDev | `ref/llm_evolution/ChatDev` | `4fd4da6` |
| AutoGen | `ref/llm_evolution/autogen` | `027ecf0` |
| PokerSkill | `ref/llm_evolution/PokerSkill` | `e9237e0` |

调研分工：

- 代码进化框架：OpenEvolve、ShinkaEvolve、CodeEvolve、MadEvolve、DGM。
- 多 agent / workflow / 隔离基础设施：CORAL、evo、EvE、AFlow、GPTSwarm、AgentSquare。
- 通用软件工程多 agent 框架：MetaGPT、ChatDev、AutoGen。
- 扑克特化参考：PokerSkill，并对照本仓库 `engine/`、`sever/`、`web/core/`。

## 总体判断

本仓库不适合整体引入任何一个外部 runtime。当前系统已经有强约束流水线：

```text
prepare_next_gen / run_crossover
-> run_master / execute_workers
-> run_quality_gates
-> run_review
-> run_critic
-> run_precommit_eval
-> commit_bot
```

真正值得吸收的是外部项目的工程机制：

1. 候选实验隔离：每个 worker/candidate 独立 worktree 或 candidate workspace。
2. 候选 artifact store：统一记录 parent、prompt、diff、gate、对局、失败原因、成本。
3. MAP-Elites 由 telemetry 升级为 selection/promotion 的真实输入。
4. guidance/prompt 版本化和延迟归因，而不是只进化 bot 代码。
5. pipeline message envelope / gate ledger / typed verdict，减少 LLM 靠 prose 猜下一步。
6. PokerSkill 的分层技能库思想迁移为离线 prompt、策略模板、测试标签、经验池结构。

不建议直接吸收：

- MetaGPT / ChatDev / AutoGen 的完整 runtime。
- CORAL / evo 的远程 sandbox 和完整平台。
- GPTSwarm 式高样本边权强化学习。
- DGM 式任意自修改 agent 本体。
- PokerSkill 的运行时 LLM 决策。

扑克自对弈和确定性 benchmark 的本质不同：胜负高方差、评估昂贵、对手池动态变化，并且 Botzone/国赛有硬协议约束。外部项目中“单 scalar 分数更高就替换 elite”的逻辑不能直接照搬。

## 与现有系统的差距

### 已经具备

- `web/core/map_elites.py`：已有 5x5 aggression/looseness 行为档案。
- `web/core/behavior_diversity.py`：已有 fingerprint / delta Vendi 类多样性信号。
- `web/core/qd_fitness.py` 与 `web/core/qd_async_eval.py`：已有 post-commit k=3 中位数 QD 评估和后台异步执行。
- `web/core/generation_scheduler.py`：post-generation cleanup 已触发 exploitability probe、QD eval、fingerprint 保存。
- `web/core/tool_commit.py`：commit 前已有 novelty advisory。
- `web/core/tool_planning.py`：Master prompt 已注入 replay、action stats、opponent profiles、experience、exploitability 等证据。
- `web/core/code_verification.py` 与 `sever/tests/`：已有国赛协议回归测试。

### 部分具备但接线不足

- MAP-Elites 主要还是 telemetry/advisory，没有稳定进入 source selection、Master planning、promotion decision。
- worker 并行有并发控制，但多个 worker 仍围绕同一个目标 bot 目录协作，缺少 per-candidate isolation 和 patch competition。
- 经验池有自然语言归档，但缺结构化失败类型、成功模式、prompt/guidance lineage。
- precommit / daemon / QD eval 都能提供信号，但缺统一 candidate artifact DB。
- prompt 已包含国赛完整规则，但 prompt/guidance 本身没有评分、版本、归因和 bandit 选择。

### 基本缺失

- `children_count` 或 parent exploitation penalty，容易反复压榨同一祖先。
- prompt/model bandit，无法量化哪类 prompt 或模型调用策略最会产出合规强 bot。
- protected mutable block / invariant block，worker 可能无意破坏 stdin/stdout、action sanitizer、协议桥接假设。
- detached grader，对不可变 candidate commit/worktree 评测，而不是评测共享工作区状态。
- typed message envelope / gate ledger，很多关键判断仍散落在工具返回文本和 prompt 里。

## 外部项目启发

### OpenEvolve / CodeEvolve / ShinkaEvolve / MadEvolve / DGM

可迁移机制：

- island + migration：按不同策略目标维护小种群，而不是只线性推进最高分 bot。
- artifact feedback：把编译失败、decision regression、协议失败、precommit 崩盘等变成下一轮可检索材料。
- prompt population：让 master/worker/reviewer prompt 片段有版本和产出质量。
- parent selection penalty：候选父代评分乘以 `1 / (1 + children_count)` 或类似衰减，减少早熟。
- protected blocks：标记协议和 sanitizer 不可变区，允许 worker 只改策略区。
- async proposal/eval/db：显式区分 LLM 生成、对局评估、DB 写入，避免同一个循环里资源互相堵塞。

不直接照搬：

- deterministic benchmark 的 early stopping 阈值。
- 单次高分直接进入 elite。
- holistic rewrite 直接重写大文件。
- 大量预取候选导致 ratings/opponent pool 过时。

扑克适配原则：

- fitness 必须带样本数、对手覆盖、RD/CI、raw chips、AIVAT chips。
- QD 替换规则必须置信度感知，不能让 lucky bot 覆盖 niche。
- exploration/exploitation scheduler 调整的是对手覆盖、手数、策略新颖性和 prompt profile，不是放宽协议或放宽 60s 决策限制。

### CORAL / evo / EvE / AFlow / GPTSwarm / AgentSquare

可迁移机制：

- worker 独立 worktree/candidate workspace。
- detached grader 只评测不可变提交或 candidate snapshot。
- `.public` / `.private` 状态分层：worker 可见 attempts/notes/skills，不可见隐藏测试和 grader 私有材料。
- experiment graph/frontier：从线性 next generation 升级到候选图，支持保留失败分支和 specialist 分支。
- guidance population：评估的不只是 bot，也包括产生 bot 的 agent guidance。
- workflow profile bandit：在少数固定流程模板之间选择，而不是让 LLM 自由改流程。

不直接照搬：

- 完整 CORAL/evo runtime。
- 远程 sandbox 后端。
- AFlow 任意代码化 workflow MCTS。
- GPTSwarm 高样本图边优化。
- AgentSquare 通用 planning/reasoning/tool/memory 模块库。

本仓库优先实现的形态：

```text
source bot
  -> candidate workspace A -> cheap gates -> candidate event
  -> candidate workspace B -> cheap gates -> candidate event
  -> candidate workspace C -> cheap gates -> candidate event
  -> selected patch merge
  -> existing quality/review/critic/precommit/commit
```

### MetaGPT / ChatDev / AutoGen

可迁移机制：

- SOP by order：Orchestrator 应执行代码状态机，而不是自由 ReAct。
- typed message envelope：MasterPlan、WorkerPatchReport、GateResult、ReviewResult、CriticResult 使用统一 envelope。
- termination / gate 条件对象化：最大重试、fingerprint 过期、gate 缺失、commit 前置条件不靠 prompt 约束。
- event replay：每个 stage、tool call、agent I/O、gate verdict 都可按 run_id 回放。
- edge processor：把 Reviewer/Critic/Precommit prose 抽取成机器字段。

不直接照搬：

- MetaGPT 的软件公司 Team/Environment。
- ChatDev 的 YAML runtime 和自由 function tool。
- AutoGen distributed runtime / group chat selector / generic code executor。

建议新增两个非 LLM 概念：

- `PipelineGatekeeper`：集中检查 stage、allowed_next_tools、gate ledger、code fingerprint、commit prerequisites。
- `ProtocolGuardian`：作为质量门/验证器，负责 Botzone JSON、national TCP adapter、engine/sever 边界，不作为自由 LLM 角色。

### PokerSkill

PokerSkill 的运行时 LLM 决策不适合本仓库，但其分层技能库适合离线迁移：

- P1：协议、动作语义、下注边界、输出格式。
- P2：翻前手牌类、位置、open/3bet/defense 静态表。
- P3：牌力、听牌、SPR、底池类型、公共牌面纹理。
- P4：行动线模板，如 c-bet、delayed c-bet、stab、probe、block bet、check-raise。
- P5：河牌 bluff/bluff-catch，包含 blocker、pot odds、MDF、range capped 判断。

迁移方式：

- Prompt：Master/Worker 任务必须声明 `skill_layer`、`spot_tags`、`action_contract`、`test_contract`。
- 策略模板：bot 内部统一抽象动作对象，最终只由 sanitizer 转成 Botzone int。
- 测试：`decision_tester.py` 的场景库按 P2/P3/P4/P5 分层扩展。
- 经验池：从自由文本升级为 `id/layer/applies_when/do/dont/evidence/test_tags/status`。
- MAP-Elites：行为描述符从泛化 aggression/vpip 扩展为 poker-specific axes。

国赛合规增强点：

- prompt 禁止把协议动作写成 TCP 字符串；bot 仍只输出 JSON int。
- 明确区分概念上的 bet 与国赛 wire action 里的 `raise <amount>`。
- national action matrix 检查典型 Botzone int 决策经过 adapter 和 validator 后是否仍合法。
- adapter clamping 不能掩盖策略 bug；clamp/降级频率应进入 telemetry 和 gate。

## 建议路线

### 立即可做

1. 将 `ref/llm_evolution/` 保持为本地忽略目录，避免误提交第三方仓库。
2. 让 Master prompt 读取 `behavior_archive.json` 的摘要：
   - 空 niche。
   - 过度拥挤 niche。
   - 每个 niche elite、fitness、games/RD/CI。
   - 当前 source bot 所在 niche 与相邻 niche。
3. 新增轻量 `candidate_events.jsonl`：
   - parent/source。
   - prompt/guidance id。
   - target files。
   - diff hash。
   - gate result。
   - failure_class。
   - replay/artifact paths。
   - cost/time。
4. 给 source selection 加 `children_count` 降权。
5. 在 worker prompt 和 gate 中引入 protected contract：
   - JSON stdin/stdout contract。
   - action encoding/sanitizer。
   - national adapter boundary。
   - no TCP strings from bot。

### 短期工程化

1. Candidate workspace MVP：
   - 每个 worker 在 candidate copy/worktree 中改。
   - cheap gates 通过后导出 patch。
   - orchestrator 选择 patch 合并到目标 bot。
2. 结构化失败分类：
   - `compile`。
   - `boundary`。
   - `zero_change`。
   - `decision_regression`。
   - `protocol_regression`。
   - `precommit_regression`。
   - `infra`。
3. Skill-layer 测试标签：
   - P2 preflop。
   - P3 board texture / hand strength。
   - P4 postflop line。
   - P5 river bluffcatch。
4. National action matrix：
   - all-in。
   - postflop first action。
   - check 后第二人 pass = call。
   - strict re-raise `>2x`。
   - positive raise consuming all chips must become allin。

### 中期升级

1. SQLite candidate/artifact store：
   - candidate lineage。
   - gate metrics。
   - prompt profile。
   - behavior features。
   - replay summaries。
   - LLM cost。
2. 真正的 island selection：
   - compliance island。
   - preflop island。
   - postflop line island。
   - river defense island。
   - exploit/nemesis island。
   - robust/low-variance island。
3. Prompt/guidance bandit：
   - reward = gate pass + confidence-discounted precommit/daemon result - cost。
   - 延迟归因，不用单场胜负。
4. QD 替换规则置信度感知：
   - games。
   - RD/CI。
   - raw/AIVAT。
   - illegal/clamp rate。
   - target skill tag regression。

### 长期方向

1. Experiment graph/frontier：
   - 保留 specialist 分支。
   - Pareto per opponent cluster。
   - 失败分支可检索但不污染 active pool。
2. Co-evolve bot 与 guidance：
   - worker guidance、review checklist、master planning profile 有 lineage 和 score。
3. PSRO / MAP-Elites 结合：
   - 不同 niche elite 不只保留，还参与 meta-opponent 构造。
4. DGM 式 meta-evolution 只限 prompts/tools/gates：
   - 不允许任意自改 orchestrator。
   - 必须人审和硬 gate。

## 推荐优先级

最高优先级：

1. `candidate_events.jsonl`。
2. `behavior_archive` 进入 Master 输入。
3. `children_count` parent 降权。
4. protected protocol/action contract。
5. national action matrix + clamp telemetry。

这些改动最贴近现有架构，风险低，并且直接服务当前目标：持续生成能在 Botzone/local 与国赛 TCP adapter 下合规、稳定、足够强的 heads-up NLHE bot。
