# LLM Evolution 参考项目深度分析与本系统改造报告

日期：2026-07-01

## 0. 结论先行

本次覆盖 `ref/llm_evolution/` 下 15 个已 clone 项目：

| 类别 | 项目 |
|---|---|
| 代码/程序进化 | OpenEvolve, ShinkaEvolve, Science-CodeEvolve, MadEvolve, DGM |
| 多 agent / workflow / sandbox | CORAL, evo, EvE, AFlow, GPTSwarm, AgentSquare |
| 通用多 agent 框架 | MetaGPT, ChatDev, AutoGen |
| 扑克专项 | PokerSkill |

核心判断：

1. 当前系统不是“没有架构”，而是已经有一条较完整的 `LLM 规划 -> worker 改 bot -> quality gates -> review/critic -> precommit battle -> commit/tag` 链路。主要短板不是多加一个通用 multi-agent 框架，而是把候选体、证据、国赛验收、MAP-Elites、prompt 版本和失败反馈变成一等公民。
2. 当前进化目标仍偏向本地 Botzone JSON battle。国赛协议规则已经写入 prompt、adapter 和 `sever/tests/test_national_alignment.py`，但候选 bot 还没有被强制放进国赛 TCP/in-process engine 做 acceptance gate。因此现在不能严肃地说“本地进化产物一定可直接用于国赛且足够强”；更准确的判断是：平台/adapter 层合规基础较好，候选 bot 级别的国赛合规与强度还需要硬 gate 证明。
3. 最值得先做的不是照搬 DGM 自我修改或 GPTSwarm 拓扑训练，而是四个基础设施：
   - candidate ledger / artifact store
   - candidate workspace isolation
   - national acceptance hard gate
   - structured stage/gate contracts
4. PokerSkill 对本项目最有价值的不是运行时 LLM，而是“离线技能标签体系”：preflop range、board texture、SPR、blocker/MDF、line template。这些应进入 bot 策略、Master/Worker prompt、decision harness 和经验池。
5. 目前 prompt 的国赛规则覆盖已经比较完整，但仍有两个误导点：`master_prompt.md` 说 workers 顺序执行，而代码支持不重叠文件并行；一些 poker prose 会说 bet，需要持续强调 wire-level 不允许 `bet`，只能经 Botzone 正整数或 TCP `raise <amount>` 表示。

一句话方案：先把系统从“每代只产一个被提交的 bot”升级为“持续产生、隔离评估、结构化记录、按国赛目标筛选的一批候选体”，再让 MAP-Elites/islands/frontier/prompt bandit 真正成为选择压力。

## 1. 当前系统深层诊断

### 1.1 模块边界

当前仓库有四条不能混淆的路径：

| 模块 | 角色 | 服务目标 |
|---|---|---|
| `engine/` | 本地 Botzone-style JSON subprocess battle engine | 快速评估 Python bot 的本地牌局强度；服务 evolution 主循环和 ladder |
| `web/` | 统一进化系统、FastAPI 后端、React dashboard | 调度 LLM、worker、quality gates、ratings、经验池、commit/tag |
| `sever/` | 国赛 TCP self-play platform | 验证并模拟国赛通信协议、动作合法性、THP 记录、dashboard |
| `rl/` | RL 实验 | 独立训练/评估实验，当前不是主进化链核心 |

`engine/` 与 `sever/` 最大区别：

- `engine/` 面向 Botzone JSON bot：stdin 读 JSON，stdout 返回 `{"response": int}`；`>0` 是本街 raise-to-total。
- `sever/` 面向国赛 TCP client：AI engine 连接平台，收发 line-delimited string，比如 `raise 200`、`call`、`check`、`fold`、`allin`。
- 现有 bot 主体仍应保持 Botzone JSON bot；国赛部署通过 `sever/bot_adapter.py` 桥接。

这意味着进化系统不能让 worker 直接把 bot stdout 改成 TCP text，也不能把 `sever/` 的 wire action 规则混进 Botzone bot 的输出层。

### 1.2 已经做得较强的地方

| 能力 | 当前状态 |
|---|---|
| Orchestrator 状态机 | `prepare_next_gen -> run_direction_audit -> run_master -> execute_workers -> run_quality_gates -> run_review -> run_critic -> run_precommit_eval -> commit_bot -> run_archivist` 已经清晰 |
| Worker 并行 | `agent_workers.py` 支持 target files 不重叠时并行执行，并能回滚目标文件 |
| 国赛规则提示 | `master_prompt.md` / `worker_prompt.md` / `reviewer_prompt.md` 已写入 70 hands、20000 chips、50/100 blinds、raise-to-total、strict >2x、postflop call/check、all-in 等关键规则 |
| 平台协议测试 | `sever/tests/test_national_alignment.py` 覆盖严格空格、`bet` 非法、postflop call/check、adapter suit mapping、all-in 转换、acceptance matrix |
| Quality gates | compile/import/smoke/decision/national tests/size/fix verification/precommit battle 已有骨架 |
| 行为多样性 | `map_elites.py`、`behavior_diversity.py`、`qd_fitness.py`、`qd_async_eval.py` 已存在 |
| 规则硬化意识 | prompt 和 gate 已经明确 critic advisory、precommit final、placement shadow、inert detector 等历史坑 |

### 1.3 当前最大问题

#### 问题 A：国赛目标还不是候选 bot 的主目标

现在 `sever/tests/test_national_alignment.py` 主要验证平台/adapter/协议对齐，不等于每个候选 bot 都在国赛环境中通过实战验收。一个 bot 可以本地 battle 表现不错，但在国赛 TCP 平台上被 adapter 大量 clamp，甚至策略语义被改写。

直接风险：

- bot 内部以为自己在执行某个 raise sizing，adapter 实际把它 clamp 到合法最小 raise；
- bot 本地 JSON 行为合法，但通过 TCP 语义后 postflop `check/call` 边界不同；
- all-in 用正数 raise 表示时，adapter 转成 `allin`，合规但可能隐藏策略实现错误；
- 本地 engine 强度不等于国赛平台强度。

#### 问题 B：MAP-Elites 仍是仪表盘，不是选择压力

`web/core/map_elites.py` 自身注释明确写着当前是 advisory/write-only，archive 被持久化和记录，但 gate/reap/source selection 尚未读取它。也就是说多样性信息已经被算出来，但没有真正影响谁被选择、谁被保护、谁被淘汰。

#### 问题 C：候选体没有统一 ledger

现在有 checkpoint、worker failures、gate results、ratings、H2H、experience pool、archive，但它们是分散的。系统缺少一个统一的 `candidate_id` 把以下信息串起来：

- source bot / parent / inspiration
- Master 计划假设
- worker task 和实际 diff
- quality gate 结果
- national acceptance 结果
- precommit 对局样本和置信区间
- commit 后 daemon 后验评级
- 是否进入经验池，证据等级多少

没有 ledger 时，经验池容易把“小样本好运”当成策略规律，也容易让 LLM 重复做已经失败的方向。

#### 问题 D：prompt 已经很长，但合同化不够

当前 prompt 包含大量规则和历史坑，优点是上下文充分，缺点是：

- 规则和历史经验混在一起，模型不容易分辨 hard invariant 与 advisory lesson；
- Master 输出的 worker task 没有足够结构化字段，比如 `files_allowed`、`prohibited_files`、`behavior_hypothesis`、`checks_required`；
- reviewer/critic verdict 仍可能受自然语言影响，应该以 Pydantic model 为准；
- stage termination 更多依赖 prompt 约束，而不是机器可执行的 `MaxAttempts | Timeout | CostCap | SchemaValid | GatePassed`。

#### 问题 E：poker 技能层还不够系统化

当前 bot 已有不少策略逻辑，但 evolution prompt 和 decision scenarios 还没有统一的技能词表。缺少稳定标签会导致：

- Master 说“加强 postflop”，worker 不知道具体是哪类 spot；
- decision tests 更容易覆盖灾难性错误，而不是覆盖 preflop range、board texture、SPR、blocker、line template；
- 经验池难以结构化归因。

## 2. 参考项目逐项提炼

### 2.1 OpenEvolve

好思想：

- `Program` 是候选体的一等公民，记录 code、parent、generation、metrics、prompts、artifacts、embedding、changes description。
- `ProgramDatabase` 同时管理 MAP-Elites、islands、archive，不只保存最后赢家。
- evaluator 支持 cascade：便宜阶段先筛，昂贵阶段后跑；失败也写 artifact。
- prompt sampler 注入父代、top performers、diverse inspirations、metrics、artifacts、feature coords。

可迁移到本系统：

- 新增 `web/core/candidate_store.py` 或 `candidate_events.jsonl`，每个候选有稳定 `candidate_id`。
- Master prompt 不只读 leaderboard，还读候选失败 artifact 和 niche summary。
- `run_quality_gates` / `run_precommit_eval` 采用显式 cascade scorecard。

不要照搬：

- 单一 benchmark score 晋级不适合扑克高方差。
- 代码 embedding 多样性不能替代真实行为多样性。

### 2.2 ShinkaEvolve

好思想：

- SQLite candidate DB 记录 parent、inspiration、island、diff、public/private metrics、text feedback、correct、children_count、system_prompt_id。
- `correct` 与失败候选分流：只有正确候选进入 parent/inspiration 池，失败候选进入 fix/diagnosis。
- parent selection 惩罚 children_count，避免过度榨干同一个祖先。
- system prompt 也有 DB、archive、UCB sampling、fitness attribution。

可迁移到本系统：

- 对候选标记 `correct = compile/import/smoke/decision/national_acceptance passed`。
- parent/source selection 加 `children_count` 惩罚，避免某个 vN 被反复小修小补。
- prompt 不先自由演化，先做少量固定 prompt variants 的 UCB 选择，指标包括 pass rate、precommit lift、national illegal rate、cost。

不要照搬：

- 无约束 prompt co-evolution 风险高，可能弱化国赛硬规则。
- private/public benchmark 划分可借鉴，但本项目更需要 hidden decision scenarios 与 national acceptance。

### 2.3 Science-CodeEvolve

好思想：

- `EVOLVE-BLOCK` / `PROMPT-BLOCK` 保护不可变 harness 和协议壳。
- patch 解析后限制到 mutable block 内。
- evaluator 在临时目录运行，带 timeout、内存/CPU 监控、进程树清理、result JSON。
- meta-prompting 有边界，只改 prompt block。

可迁移到本系统：

- 对 bot 的协议壳设 protected contract：stdin/stdout、action encoding、sanitize、card mapping、adapter 边界不能被普通 worker 改。
- 对 `sever/`、`engine/`、quality gate 文件设置 boundary guard；只有明确 profile 允许时才能改。
- 先用 gate 检查 protected regions，而不是马上重构所有 bot 文件。

不要照搬：

- 整文件 rewrite 不适合生产 bot；协议壳太容易坏。
- prompt block 自由演化也必须在国赛 invariant 下运行。

### 2.4 MadEvolve

好思想：

- LLM producer 与 evaluator dispatcher 解耦。
- candidate queue、parallel eval、artifact store、hybrid population、model selector 分层明确。
- SQLite artifact store 记录 program、score、metrics、feedback、embedding、metadata、lineage。
- prompt composer 注入 parent、feedback、elite archive、top performers、embedding-diverse neighbors。
- protected block 与 patch result 记录提供了好的失败可观察性。

可迁移到本系统：

- `candidate_workspace + candidate_store + eval_schema` 三件套。
- 每个 worker/gate/precommit 产出 artifact refs，dashboard 可以看到候选 lineage。
- patch 失败、无改动、fuzzy mismatch、gate fail 都归类写入 ledger。

不要照搬：

- fuzzy 全局 SEARCH/REPLACE patcher 不适合扑克 bot，相似策略片段太多，容易改错位置。

### 2.5 DGM

好思想：

- 把 coding agent 自己作为可进化对象。
- 失败日志先进入 diagnosis prompt，再形成改进建议。
- parent selection 有 children_count 惩罚。
- 对 empty patch、stochasticity、context length、unresolved issue 等失败类型有专门提示。
- harness 收集 patch、chat、logs、benchmark result。

可迁移到本系统：

- 不让系统自动改 `web/core/`，但可以增加“pipeline diagnosis mode”：连续失败后生成结构化 issue。
- 对 worker failure 增加分类：`no_change`、`compile`、`protocol`、`decision_critical`、`telemetry_inert`、`precommit_loss`、`timeout`、`schema_error`。
- 将失败诊断摘要注入下一轮 Master，而不是只把长日志塞进 prompt。

不要照搬：

- DGM 式自动修改 orchestrator/gates 风险过高。本仓库有 git tag、Botzone、国赛、daemon、质量门，自动自改会放大不可控性。

### 2.6 CORAL

好思想：

- 每个 agent 一个 git worktree。
- `.coral/public` / island 目录共享 attempts、notes、skills、logs、heartbeat。
- grader daemon 监控 attempt JSON，在隔离 checkout 中评分。
- attempt ledger、leaderboard、heartbeat、crash restart、island migration 分开。

可迁移到本系统：

- `web/core/candidate_workspace.py`：为候选/worker 创建隔离 worktree 或复制目录。
- `web/core/candidate_store.py`：attempt record 原子写入。
- 评估在 detached workspace 中跑，主仓库只在 commit_bot 成功时落地。

不要照搬：

- island migration 按近期最好 attempt 迁移不适合扑克小样本；必须绑定 Glicko RD、最低局数、mirror/H2H、保守 rating。

### 2.7 evo

好思想：

- `.evo/run_xxx/graph.json` 把实验做成图：parent/child/status/score/worktree/commit/gates。
- backend 支持 worktree、warm pool、remote sandbox。
- frontier strategies 有 `argmax/top_k/epsilon_greedy/softmax/pareto_per_task`。
- trace/result/checkpoint 通过环境变量约束输出位置。
- explorer cache：先只读探索，再 fork 到 execute 阶段。

可迁移到本系统：

- 新增 `frontier.py`：按 conservative Glicko、H2H cluster、skill scenario、national acceptance、novelty 做 Pareto/softmax source selection。
- 每代不只看“最高综合分”，还保留某些对手/spot 的 specialist。
- agent read-only exploration 可缓存：Master 计划前读当前 bot 和数据，执行阶段不重复读大块背景。

不要照搬：

- warm pool 保留未跟踪状态，可能污染可复现性。
- remote sandbox 会引入环境差异；先做本地 worktree 隔离更稳。

### 2.8 EvE

好思想：

- solver/optimizer 双 population co-evolution。
- candidate files/logs/score 存 artifact store + SQLite lineage。
- evaluation workspace 从 snapshot 重建，solver/optimizer logs 只读。
- boundary/workspace guard 限制 editable paths。
- 用 optimizer Elo 评价“哪个 optimizer 更会产生好 solver”。

可迁移到本系统：

- 先不要上双种群，但可以借鉴“planner profile 的后验评分”：Master prompt/profile 是否真的产出更好候选。
- workspace guard 直接适配到 `web/core/boundary_guard.py`。
- evaluation workspace 从 candidate snapshot 重建，避免主仓库状态污染。

不要照搬：

- solver/optimizer 双种群全量引入过早，会在当前高方差评估上放大噪声和 prompt 过拟合。

### 2.9 AFlow

好思想：

- workflow graph + prompt 一起优化。
- 每轮从 top rounds 采样父图，结合 success/failure experience 做单点修改。
- XML/structured output 校验，重复 modification 检查。
- operators 固定：Custom、AnswerGenerate、ScEnsemble、Programmer、Test。

可迁移到本系统：

- 把“改 workflow”降级成 `workflow_profiles.py`：探索型、保守型、国赛协议加固型、postflop 专项型、preflop 专项型。
- profile 控制 worker 数、allowed paths、gate budget、national acceptance 是否 hard、hidden scenarios 比例。
- profile 成功率进入 candidate ledger，后续用 bandit 选择。

不要照搬：

- 让 LLM 自动生成并执行新的 workflow Python 不适合当前系统；它会绕过现有 gate/commit/tag 纪律。

### 2.10 GPTSwarm

好思想：

- agent 是 graph node，多个 agent graph 可组合成 composite graph。
- `FinalDecision` 支持 majority/self-consistency/select-best 等聚合。
- edge logits 学习 agent 间连接，采样无环图。

可迁移到本系统：

- 不训练拓扑，但可定义少数固定 multi-agent topology profile：
  - `single_master_2_workers`
  - `master_protocol_guard_strategy_worker`
  - `planner_reviewer_then_worker`
  - `parallel_specialists_then_select_best`
- 对 worker 输出的策略方案可做 select-best/adjudication，而不是简单合并。

不要照搬：

- REINFORCE 式 edge optimization 成本高、噪声大，扑克 battle utility 不稳定。

### 2.11 AgentSquare

好思想：

- 将 agent 能力拆成 planning/reasoning/tooluse/memory 模块。
- 新模块先进 benchmark，再进入 archive。
- recombination 根据任务和 tested cases 选择模块组合。
- predictor 用历史组合估计哪个组合值得真实测试。

可迁移到本系统：

- 将扑克 bot 策略拆成模块化 skill layers：
  - preflop range
  - postflop texture
  - SPR/commitment
  - blocker/MDF
  - opponent model
  - action sanitizer
  - telemetry
- Master 计划要声明本代修改哪一层，Reviewer 检查是否真的改了这层。

不要照搬：

- 通用 planning/reasoning/tooluse/memory 模块代码面向 WebShop/ALFWorld，不适合 Botzone/国赛协议。

### 2.12 MetaGPT

好思想：

- Role/Action 状态机：observe -> think -> act -> publish。
- message 带 `cause_by/sent_from/send_to`，可路由。
- `ActionNode` 将 prompt 拆成 context/example/instruction/constraint，并用 Pydantic 模型校验输出。
- review/test action 有 typed verdict。

可迁移到本系统：

- 新增统一 `llm_stage_runner.py`：所有 master/reviewer/critic/audit 走同一个渲染、调用、JSON 修复、Pydantic 校验、重试、日志关联流程。
- 扩展 `output_schema.py`：每个 LLM 输出带 `schema_version/stage/role/verdict/confidence/evidence/blockers/retry_recommended/next_action`。

不要照搬：

- 不需要 MetaGPT `Team/Environment/Role` runtime；它和现有 MCP tools + git/gate 流程重叠。

### 2.13 ChatDev

好思想：

- SOP 是 YAML graph，节点/边/循环/条件明确。
- edge 有 keyword/function conditions、carry data、keep/clear context。
- loop counter 限制循环。
- workspace 文件工具有边界检查。

可迁移到本系统：

- 用 Python dataclass 实现 `stage_specs.py`，声明每个阶段的前置条件、最大尝试、失败转移、缓存条件、下一阶段。
- termination 不靠 `<INFO> Finished`，而靠机器可执行 `TerminationCondition`。

不要照搬：

- 不采用 sentinel-only verdict。
- 不引入动态函数目录加载。
- 不替换 `generation_scheduler.py` 的现有状态机。

### 2.14 AutoGen

好思想：

- message 分为给 agent 的 chat message 和给观察者的 event。
- `StructuredMessage` 可直接承载 Pydantic 内容。
- `AssistantAgent` 支持 structured output、tool schema、max tool iterations、tool-use reflection。
- termination conditions 可组合：max message、text mention、token usage、timeout、external、function call。

可迁移到本系统：

- 区分 stage message 与 observability event：前者进入 LLM context，后者进入 dashboard/trace store。
- 实现轻量 `stage_termination.py`：`MaxAttempts | Timeout | CostCap | SchemaValid | GatePassed | ExternalShutdown`。
- 所有 gate result 统一成 typed `GateResult`。

不要照搬：

- 不接入完整 AutoGen runtime；额外 runtime 会把关键决策藏在 group chat 消息流里，且与当前 `claude_agent_sdk`/MCP 工具链重复。

### 2.15 PokerSkill

好思想：

- 核心是离线 poker skills + LLM action grounding，不是在推理时查 solver。
- prompt 分 P1-P5：
  - P1 规则/输出格式
  - P2 preflop range
  - P3 postflop 原则和手牌强度
  - P4 目标策略
  - P5 river bluff / bluff-catch
- 内置能力包括 169 手牌类、BTN/BB preflop spots、board texture、SPR、blocker/draw、line templates、sanitizer。

可迁移到本系统：

- 新建 skill tag vocabulary：`preflop_spot`、`range_class`、`texture_tag`、`spr_band`、`blocker_tag`、`line_template`、`legal_action_contract`。
- `decision_tester.py` 从单纯 expected action 扩展为 skill-matrix harness。
- `master_prompt.md` 和 `worker_prompt.md` 要求每个策略改动声明所属 skill layer。
- bot 代码中逐步引入 deterministic 169-hand preflop frequency、board texture classifier、SPR commitment helper、blocker/MDF guardrail、line classifier。

不要照搬：

- 不把 PokerSkill 作为 bot runtime dependency。它有编译扩展和 LLM/API 依赖，不符合 Botzone/国赛可移植、确定性、stdout 干净的要求。
- 不在比赛决策时调用 LLM。
- 不让内部抽象 action `b` 泄漏到国赛 TCP；wire-level 只有 `raise <amount>`。

## 3. 横向好思想总结

### 3.1 候选体是一等公民

来自：OpenEvolve、ShinkaEvolve、MadEvolve、CORAL、evo、EvE。

本系统当前每代最终只沉淀成功 bot，而失败候选、worker 计划、gate 证据、precommit 方差、国赛验收没有统一对象。应该引入 `CandidateRecord`：

```text
candidate_id
source_version
generation
profile_id
parent_ids
prompt_variant_ids
worker_tasks
changed_files
diff_hash
quality_gate_scorecard
national_acceptance_scorecard
precommit_scorecard
post_commit_daemon_delta
behavior_descriptor
qd_niche
failure_class
correct
artifact_refs
```

预期增益：

- 减少重复失败方向，预计能明显降低 `no_change/compile/protocol/decision_critical` 类无效循环。
- 经验池从“故事”变成有证据等级的 claim。
- Dashboard 可以显示候选 lineage，而不是只显示已提交 bot。

### 3.2 分层 cascade harness

来自：OpenEvolve、ShinkaEvolve、Science-CodeEvolve、MadEvolve。

建议 gate 层级：

| 阶段 | 内容 | 失败处理 |
|---|---|---|
| Stage 0 | diff 非空、文件边界、protected contract、py_compile、stdout contract | 立即失败 |
| Stage 1 | smoke mirror、decision critical、national protocol tests、adapter strict telemetry | 立即失败 |
| Stage 2 | short mirror vs parent/top、hidden skill scenarios、national acceptance mini-match | 失败进入 candidate ledger |
| Stage 3 | precommit expanded H2H、k3/QD median、Glicko/RD confidence | 决定是否 commit |
| Stage 4 | daemon 后验 rating、MAP-Elites update、experience promotion | 影响后续 source/prompt/profile |

预期增益：

- 昂贵 battle 前挡掉协议/无效/死代码候选。
- 每个失败都有结构化原因。
- 高方差收益不会直接污染经验池。

### 3.3 隔离工作区

来自：CORAL、evo、EvE、Science-CodeEvolve、MadEvolve。

当前 worker 在目标 bot 目录上直接改，虽然有回滚和 checkpoint，但候选之间没有天然隔离。建议：

- Phase 1：每个候选复制 bot 目录到 ignored runtime workspace，worker 改候选副本。
- Phase 2：用 git worktree 隔离整仓库，评估也在 worktree 内跑。
- Phase 3：commit_bot 成功后才把候选 patch 应用回主线。

预期增益：

- 并行 worker/candidate 更安全。
- 失败候选可保留并复盘。
- 避免污染 `bots/` 和 runtime state。

风险：

- git worktree 清理复杂。
- 当前 bot generation/tag 流程要适配。

### 3.4 Protected contract

来自：Science-CodeEvolve、EvE、PokerSkill 的 action grounding。

应该保护的边界：

- Botzone JSON stdout：只能输出 `{"response": int, "data": ...}`。
- action encoding：`0=call/check`、`-1=fold`、`-2=allin`、`>0=raise-to-total`。
- 国赛 wire action：`raise <amount>`、`fold`、`call`、`check`、`allin`；不能有 `bet`。
- card mapping：local suits 与 TCP suits 的映射不能被普通策略 worker 改。
- postflop `check/call` 国赛语义。
- all-in 不能用等于剩余筹码的 positive raise 表示到 TCP。

预期增益：

- 降低协议回归。
- 让 worker 集中改策略，不破坏平台边界。

### 3.5 Prompt engineering 从“长提示词”转向“合同 + 证据 + schema”

来自：MetaGPT、AutoGen、AFlow、DGM、OpenEvolve。

建议 prompt 固定结构：

```text
role contract
allowed tools / prohibited edits
immutable protocol invariants
input bundle
recent candidate evidence
task-specific skill tags
output schema
stop/failure rules
```

同时引入：

- `llm_stage_runner.py`
- `llm_contracts.py`
- `gate_schema.py`
- `stage_termination.py`
- `llm_replay.py`

预期增益：

- verdict 更稳定。
- 失败可回放，不必重新调用 LLM。
- prompt drift 可测量。

### 3.6 Prompt/profile bandit，而不是自由 prompt evolution

来自：ShinkaEvolve、AFlow、DGM。

建议维护少量人工审核过的 variants：

- `default`
- `national_strict`
- `postflop_skill`
- `preflop_range`
- `risk_conservative`
- `exploration_diversity`

选择指标：

- quality gate pass rate
- national illegal/clamp rate
- precommit lift
- post-commit daemon delta
- LLM cost
- retry count

先用 UCB/epsilon-greedy 选 variant，不让 LLM 自由修改 prompt。

### 3.7 MAP-Elites 与 frontier 真正进入选择

来自：OpenEvolve、MadEvolve、evo、ShinkaEvolve。

当前行为 archive 是 advisory。建议接入：

- source selection：全局 top、同 niche elite、欠填充 niche、H2H specialist 混合采样。
- reap policy：不要只按全局 rating 淘汰；保护一些对特定对手/场景有优势的 specialist。
- parent overuse penalty：按 children_count 降权。
- archive confidence：每个 niche elite 带 games/RD/CI/national acceptance/clamp rate。

预期增益：

- 降低单一路线内卷。
- 保留对特定 bot/spot 有价值的策略。
- 帮助跳出局部最优。

风险：

- 行为 descriptor 如果噪声大，会保护伪多样性。
- 需要先把 descriptor 改成 probe-based，而不是仅靠聚合 match stats。

### 3.8 Poker skill harness

来自：PokerSkill + 当前 decision_tester。

建议新增 skill-matrix 场景：

| Skill layer | 场景例子 |
|---|---|
| Preflop BTN open | A2s-A5s、K9s、QTo、72o，不只 AA/KK |
| BB vs open | KQs/JTs/small pair vs 2.5x；小 offsuit vs 大 raise |
| BB vs limp | 国赛语义下不能 `call`，应 check/raise/fold |
| Texture | monotone、two-tone connected、paired dry、river four-flush |
| SPR | SPR < 1 nuts 不 slowplay；SPR > 10 weak draw 不乱 commit |
| Blocker/MDF | nut blocker bluffcatch、bad blocker fold、pot-size bet 下不能极端 overfold |
| Line template | cbet、delayed cbet、probe、donk、check-raise、barrel、block bet |
| Protocol | postflop check 后第二人 pass 用 TCP `call`；raise-to-total；allin conversion |

预期增益：

- 让进化方向从“泛泛加强 postflop”变为“明确改某个技能层”。
- 更容易发现策略改动是否真的生效。
- 减少 prompt 过拟合公开场景，可以设置一部分 hidden scenarios。

## 4. 对本系统的具体改造矩阵

| 优先级 | 改造 | 主要文件 | 思想来源 | 预计增益 | 风险/难度 | 验证方式 |
|---|---|---|---|---|---|---|
| P0 | 国赛 candidate acceptance hard gate | `web/core/tool_gates.py`, `web/core/code_verification.py`, `scripts/national_acceptance_matrix.py`, `sever/` | PokerSkill action grounding, CORAL grader, 当前国赛 tests | 直接回答“能不能上国赛平台”；减少非法/被 adapter 改写动作 | gate 变慢；旧 bot 可能暴露问题；中 | mini national match；`illegal_actions/timeouts/bot_failures/invalid_actions == 0` |
| P0 | Adapter strict telemetry | `sever/bot_adapter.py`, `sever/tests/test_national_alignment.py` | protected contract | 揭露 clamp/allin conversion/非法原始意图，防止合规假象 | 初期会出现大量 warning；低中 | 新增 `clamped_raises`, `allin_conversions`, `would_be_illegal_raise` 计数 |
| P0 | Candidate ledger MVP | `web/core/candidate_store.py`, `tool_gates.py`, `tool_eval.py`, `agent_workers.py` | OpenEvolve/Shinka/MadEvolve/CORAL | 失败不丢、经验可审计、减少重复踩坑 | schema 演进；中 | JSONL/SQLite 写入；每代候选可按 id 追踪 |
| P1 | Cascade ScoreCard | `web/core/gate_schema.py`, `tool_gates.py`, `tool_eval.py`, `eval_stats.py` | OpenEvolve evaluator, Shinka repeated eval | 高方差结果更可靠，失败原因清楚 | 旧 checkpoint 兼容；中高 | 每个 gate 返回统一 `GateResult` |
| P1 | Protected contract gate | `web/core/protected_blocks.py`, `tool_gates.py`, `orchestrator_context.py` | Science-CodeEvolve, EvE | 降低协议壳/adapter/engine 被误改概率 | 过严会挡架构修复；中 | diff 扫描 + allowlist exception |
| P1 | Workflow profiles | `web/core/workflow_profiles.py`, `generation_scheduler.py`, `agent_master.py`, prompts | AFlow, GPTSwarm, AgentSquare | 控制探索/保守/国赛专项等模式，避免 prompt 漂移 | profile 膨胀；中 | profile_id 写入 candidate ledger，按后验评分 |
| P1 | Prompt/stage runner | `web/core/llm_stage_runner.py`, `llm_contracts.py`, `llm_query.py`, `output_schema.py` | MetaGPT, AutoGen | verdict/schema 更稳定，可重放 | 改动面较大；中 | parse fail 分类；无 LLM replay 测试 |
| P1 | Prompt 规则清理 | `web/core/prompts/*.md` | 当前系统内审 | 减少误导：并行/顺序、critic advisory、国赛 hard gate 状态 | 删除规则不能早于机器 gate；低中 | prompt lint + national keywords tests |
| P2 | MAP-Elites selection | `map_elites.py`, `generation_scheduler.py`, `reap_manager.py` | OpenEvolve/MadEvolve/evo | 多样性真正影响 parent/source/reap | descriptor 噪声；中高 | niche champion 被选中/保护的 telemetry |
| P2 | Probe-based behavior descriptor | `behavior_diversity.py`, `decision_tester.py` | PokerSkill, evo frontier | 行为差异更真实，不靠代码相似度 | probe 过拟合；中 | 固定 seed probe suite，descriptor stability test |
| P2 | Skill-matrix decision harness | `decision_tester.py`, `test_scenarios.json`, prompts | PokerSkill | 提升 poker 专项强度，防止策略空改 | 场景维护成本；中 | skill tag coverage report |
| P2 | Structured experience pool | `experience_pool.py`, `experience_archivist.py` | Shinka/DGM | 经验从自然语言变成 claim/evidence/status/confidence | LLM 写弱证据；中 | 低证据经验不得进入 hard rule |
| P3 | Prompt/profile bandit | `llm_query.py`, `workflow_profiles.py`, `candidate_store.py` | Shinka prompt DB, AFlow | 降低 cost，提高有效候选率 | prompt drift；高 | UCB 指标：pass/lift/national/cost |
| P3 | Pipeline diagnosis mode | `generation_scheduler.py`, `agent_review.py`, `candidate_store.py` | DGM | 连续失败时生成系统改进 issue | 诊断质量依赖日志；中高 | 只读，不自动改 `web/core` |

## 5. 国赛合规与强度判断

### 5.1 规则是否一致

平台层面基本一致：

- `sever/server/protocol.py` 对 `raise <amount>` 空格格式严格。
- `sever/engine/validator.py` 覆盖 `bet` 非法、postflop `call/check` 限制、raise-to-total、strict `> 2x`、all-in 规则。
- `sever/bot_adapter.py` 明确 local JSON action 与 TCP action 的映射，并处理 suit mapping。
- `sever/tests/test_national_alignment.py` 覆盖关键回归。

Prompt 层面大体一致：

- `master_prompt.md` / `worker_prompt.md` 已写明 Botzone action encoding、国赛 wire protocol、`bet` 非法、raise-to-total、strict re-raise、postflop call/check、all-in。

仍需修正/强化：

- `master_prompt.md` 中 “Workers execute SEQUENTIALLY” 与当前 worker 可并行的实现不一致，会误导 Master 分配任务。
- “bet” 可以作为 poker prose，但 prompt 必须持续区分 prose 与 wire action；worker 不能输出 TCP-only text。
- 当前 prompt 规则长且混杂，建议分为 immutable invariant、advisory lesson、current known bug 三层。

### 5.2 通过 engine 得到的 bot 能否用于国赛平台

技术上：可以通过 `sever/bot_adapter.py` 接入国赛平台，因为 adapter 负责 Botzone JSON bot 到 TCP action 的桥接。

工程上：还不能只凭本地 `engine/` battle 结论认定可用于国赛。原因：

- 本地 battle 不覆盖 TCP 交互和 wire protocol edge cases。
- adapter 的 clamp/conversion 会让非法或不合适的原始策略变成合法 TCP action，合规但可能扭曲策略。
- precommit eval 主要看本地 mirror/H2H，不是国赛 acceptance matrix。

结论：当前产物“有桥接基础”，但每个候选 bot 需要国赛 acceptance hard gate 才能声明合规。

### 5.3 是否够强

不能仅凭当前本地 evolution 分数断言“国赛够强”。强度要拆成三类：

| 维度 | 当前证据 | 结论 |
|---|---|---|
| 本地 engine 强度 | 有 ladder/Glicko/precommit battle | 有一定证据 |
| 国赛协议合规 | 平台/adapter tests 有证据；候选 bot acceptance 不足 | 需要 hard gate |
| 国赛环境强度 | 缺 national in-process H2H / acceptance score | 证据不足 |

建议把“够强”的定义改成可测门槛：

- national acceptance：0 illegal、0 timeout、0 bot failure、0 adapter critical clamp。
- national mini-H2H：vs source/top/baseline 的 net chips 不显著劣化。
- post-commit daemon：至少 `MIN_GAMES_FOR_EVAL` 且 RD 降到阈值后再进入经验池。
- skill harness：critical skill scenarios 无失败，hidden scenarios pass rate 达标。

## 6. 建议路线图

### Phase 0：1-3 天，先补目标一致性

1. 接入 national acceptance hard gate。
2. `sever/bot_adapter.py` 增加 strict telemetry。
3. 建 `candidate_events.jsonl` MVP，至少记录每代 source、changed files、gate/precommit/national 结果。
4. 修正 prompt 中 worker 顺序/并行不一致。
5. 在 Master/Worker prompt 加 skill tag 字段，但暂不大改 bot。

### Phase 1：1 周，候选体工程化

1. 新增 `CandidateRecord` / `GateResult` / `ScoreCard` schema。
2. 质量门改为 cascade 输出。
3. 引入 protected contract gate。
4. 将 national acceptance mini-match 接入 precommit。
5. experience pool 改为 claim/evidence/status/confidence。

### Phase 2：2-3 周，让多样性与技能层生效

1. MAP-Elites 从 advisory/write-only 改为 source/reap selection 的软约束。
2. behavior descriptor 改为 deterministic probe suite。
3. skill-matrix decision harness 覆盖 preflop/texture/SPR/blocker/line/protocol。
4. workflow profiles 上线，记录 profile 后验收益。

### Phase 3：长期，提高上限

1. SQLite candidate/artifact store。
2. prompt/profile bandit。
3. Pareto frontier by opponent cluster / skill task / national score。
4. 只读 pipeline diagnosis mode，用于发现进化系统自身瓶颈。

## 7. 最小可执行改造方案

如果只做一个最小闭环，建议如下：

1. 新增 `web/core/candidate_store.py`，先用 locked JSONL，不上 SQLite。
2. 在 `run_quality_gates` 开始和结束写 `candidate_event`。
3. 在 `run_precommit_eval` 写 precommit scorecard。
4. 在 `sever/bot_adapter.py` 加 clamp/allin conversion telemetry。
5. 把 `scripts/national_acceptance_matrix.py` 抽成可被 `tool_gates.py` 调用的小规模 runner。
6. 修改 prompt：
   - workers 可并行，任务必须声明 disjoint target files；
   - 每个 worker task 必须声明 skill layer；
   - 国赛 acceptance 是 hard gate；
   - 不允许把 adapter clamp 当作策略成功。
7. 新增 10-20 个 skill-matrix decision scenarios。

这个闭环完成后，系统就能回答：

- 这个候选改了什么？
- 为什么改？
- 哪些 gate 过了？
- 国赛平台实际能不能跑？
- adapter 有没有替它擦屁股？
- precommit 的收益置信度如何？
- 这条经验是否值得写入长期经验池？

## 8. 总体优先级

最高优先级：

1. 国赛 candidate acceptance hard gate。
2. Adapter strict telemetry。
3. Candidate ledger / artifact store MVP。
4. Prompt 与真实流程一致化。

第二优先级：

5. Cascade ScoreCard。
6. Protected contract gate。
7. Skill-matrix decision harness。
8. Probe-based behavior descriptor。

第三优先级：

9. MAP-Elites selection。
10. Workflow profiles。
11. Prompt/profile bandit。
12. Pipeline diagnosis mode。

最终目标不是“堆更多 agent”，而是让每个 agent 的产物都被隔离、记录、验收、归因，并且验收目标与国赛平台一致。这样多 agent 并行才会变成有效搜索，而不是更快地产生不可复盘的噪声。
