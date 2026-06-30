# 最新代际失败与架构提示词审计报告（2026-06-30）

## 结论摘要

最新失败不是单个 bot 写坏，而是演化系统的状态机、提示词约束、工具边界和 git 纪律一起出现了漂移。核心问题有八个：

1. 失败代际仍能写入 tracked 知识库：`v230` 质量门失败后，`cross_gen_pivot` 仍向 `web/core/experience_pool.md` 写入 EXHAUSTED 标记。
2. Crossover 只验证语法和不可靠 smoke，缺少 import/dependency-closure 检查：`v230` 的 `strategy.py` import 了 `opponent.py` 不存在的函数。
3. Smoke test 对 bot 启动/import 崩溃不敏感：`v230` 直接 import 失败，但 `python web/core/smoke_tester.py bots/claude_v230/main.py` 最终仍打印 passed。
4. 阶段状态允许失败后继续在同一个 `next_v` 上反复 master/pivot：crossover 写入 `workers_done`，direction audit 又保留高级阶段，后续工具仍能继续改变状态。
5. 架构提示词对 blocking/advisory 的边界自相矛盾：`orchestrator.md` 同时写 reviewer/critic gate requirement，又把 LLM-gated rejections 降级为 advisory。
6. 裸提交污染已经发生：`v227/v228` 被普通提交 `ae2a13bc Clarify AGENTS guidance for repo structure` 混入，但没有 `bot-v227/bot-v228` tag。
7. abandon/cleanup 语义不完整：`v229` 已记录 abandoned，但 `removed_dir=null`，目录仍留在 `bots/` 下。

因此，下一步不应继续放任 orchestrator 自愈；应先暂停后台演化、清理失败现场、修复 gate 与 prompt，再恢复自动演化。报告编写期间后台又创建了 `bots/claude_v231/`，这证明“先停进程”不是洁癖，而是避免证据继续漂移的 P0 前置条件。

## 当前脏项来源

| 脏项 | 来源判断 | 证据 | 处理建议 |
|---|---|---|---|
| `web/core/experience_pool.md` | 系统副作用 | 新增 `v230` cross_gen_pivot auto-mark；`system_events.jsonl:1514` 显示由 `orchestrator` 写入 | 撤销该行，但在本报告保留证据；修复为 commit/archivist 后再写知识库 |
| `bots/claude_v229/` | 放弃代际残留 | `abandoned_versions.jsonl` 有 v229；日志显示 `abandoned/uncleaned cycle`、`removed_dir=null` | 移入 `bots/graveyard/failed_v229_*` 或删除，建议先归档 |
| `bots/claude_v230/` | 失败/未完成代际残留 | checkpoint 停在 `workers_done`；quality `decision_tests(0%)`；无 `bot-v230` tag | 移入 graveyard 或删除，建议先归档 |
| `bots/claude_v231/` | 报告编写期间后台继续生成的新代际残留 | `system_events.jsonl:1567-1572` 显示 v231 从 v224 prepared 并 direction_audited；无 `bot-v231` tag | 与 v229/v230 一并归档或删除；处理前必须停止后台进程 |
| `ref/DanLM` | 本次 ref 跟踪工作造成的 gitlink 变化 | 子仓库 `main...origin/main [ahead 2]`，HEAD `12f96c9` | 不直接推第三方 remote；父仓库保存 patch，或后续改成可推 fork |

`ref/DanLM` 的可复现 patch 已保存为 `docs/reference-patches/danlm-parallel-explorer.patch`。该 patch 对应子仓库本地 commit `85989b7 Add parallel exploration utilities`，包含：

- `danzero/explorer/__init__.py`
- `danzero/explorer/explorer.py`
- `scripts/parallel_explore.py`

子仓库远端是 `https://github.com/dashidhy/DanLM.git`。在没有权限确认前，不建议父仓库直接记录一个外部 remote 拉不到的 gitlink commit。

## 最新代际时间线

| 版本 | 主要事件 | 失败模式 |
|---|---|---|
| v225 | 从 v224 master；master accepted；cross-gen pivot 写入 exhausted；随后 abandoned | 计划/方向未收敛，失败代际已开始污染经验池 |
| v226 | master 多次 accepted，但 master audit 第 2/3 次 rejected；随后 abandoned | “审核拒绝但继续尝试”的状态混乱 |
| v227 | 多次 prepared；quality passed；review rejected score=2；abandoned | 同一版本反复 prepare，review 阻塞后残留 |
| v228 | crossover v203×v224；quality passed；review rejected score=4；后续 master audit rejected；timed_out | crossover 成功后仍回到 master 规划，阶段语义不清 |
| v229 | crossover v203×v195；quality passed；review rejected score=5/3；worker 后续失败；abandoned 但目录未清 | review 拒绝后重复 retry，放弃清理不完整 |
| v230 | crossover v195×v203；quality failed `decision_tests(0%)`；仍发生 master accepted 和 cross-gen pivot 写 experience_pool | import 依赖缺失 + 失败后副作用写入 |

关键事件：

- `system_events.jsonl:1472-1473`：`v230` crossover 只改了 `strategy.py` 和 `state.py`。
- `system_events.jsonl:1490`：`v230` quality failed，`decision_tests(0%)`。
- `system_events.jsonl:1514`：失败后的 `v230` 仍写入 `experience_pool.md`。
- `system_events.jsonl:1531`：master audit 指出 generation identity mismatch：上下文说 `v195 -> target v196`，plan 却围绕 `v230` 修 import crash。
- `system_events.jsonl:1426`：系统检测到 `v227/v228` 是 git-tracked but untagged，绕过了 `commit_bot`。

## v230 直接根因

`bots/claude_v230/strategy.py` 导入：

```python
from opponent import ..., _river_potodds_equity_margin, _allin_polarized_equity_fold
```

但 `bots/claude_v230/opponent.py` 没有 `_allin_polarized_equity_fold`。直接验证：

```bash
python -c "import sys; sys.path.insert(0,'bots/claude_v230'); import strategy"
```

结果：

```text
ImportError: cannot import name '_allin_polarized_equity_fold' from 'opponent'
```

这说明 crossover 进行了文件级拼接，但没有验证跨文件依赖闭包。`py_compile` 不会执行 import 解析到这个程度，当前 smoke test 也没有可靠失败，因此只有 decision tests 抓住了 crash。

## 架构提示词与流程缺陷

### 1. 状态机缺少硬性“当前代际身份”不变量

`agent_master.py` 只在文本里注入：

- `Current evolution: v{source_v} → v{next_v}`
- `Bot directory: bots/claude_v{source_v}/`

但 master 输出 schema 没有强制回填 `next_v/source_v/target_dir`，audit 侧用 `master_plan.get("next_v", source_v + 1)` 兜底。结果是：当 checkpoint、resume、crossover 和失败修复混在一起时，LLM 可以把“正在修 v230”与“从 v195 生成 target v196”的身份混淆。`v230` 的 `master_audit_rejected` 正是这种情况：checkpoint 是 `next_v=230, source_v=195`，但 audit feedback 写成 “source v195 -> target v196”。

修复：

- master plan schema 必须包含 `source_v`, `target_v`, `target_dir`, `parent2_v`，且工具层硬校验等于 checkpoint。
- 所有 gate 只接受当前 checkpoint 的 exact identity；不允许从 plan 文本推断。
- `_run_master_plan_audit` 必须显式接收 authoritative `next_v`，禁止用 `source_v + 1` 推断目标版本。
- audit 日志应写入 `logs/v{next_v}/`，而不是只按 `source_v` 归档，否则分支生成时证据会落到错误目录。
- audit prompt 应把 “target is v{next_v}” 提升为 reject-if-mismatch 的第一条，而不是泛泛 branch-from semantics。

### 2. Advisory 与 Blocking 边界被提示词写乱

`prompts/orchestrator.md` 的 gate requirements 写：

- `run_review` approved true
- `run_critic` approved true
- `run_precommit_eval` passed true

但同文件又写：

- LLM-gated rejections are advisory
- critic score does not block

代码里 `run_critic` 已经明确 `approved = True`，critic 是 advisory；`run_review` 仍是 blocking。这种混写会使执行代理把 reviewer rejection、critic rejection、direction audit rejection 混成一类。

修复：

- 文档分成三层：hard gates、soft advisories、telemetry-only。
- reviewer rejection 必须进入 retry/abandon，不得继续 critic/precommit/commit。
- critic rejection 可以进入 precommit，但必须作为下一代 context，而不是在同代反复让 worker 修。

### 3. Master audit 拒绝后仍可能继续

`tool_planning.py` 中 master audit 不是真正硬门：当 `overall_pass=false` 但 retry 耗尽时，系统会记录 “accepting plan to avoid retry loop”，并继续执行；retry 期间还会把 draft plan 写成 `stage="master_planned"`。这解释了 v226/v227/v228 中“audit rejected 但后续还有 workers/quality/review”的链路。

修复：

- `overall_pass=false` 应成为 hard block。
- retry 期间只能写 `master_audit_retrying` 草稿态，不能写 `master_planned`。
- retry 耗尽应 `abandon_generation` 或进入 `master_blocked`，不能接受已拒绝计划。

### 4. Cross-gen pivot 写入时机错误

`tool_planning.py::_mark_axis_exhausted_in_pool()` 会直接 append 到 `experience_pool.md`。它没有检查：

- 当前 bot 是否已 commit/tag；
- quality/review/precommit 是否通过；
- generation 是否已经 abandoned；
- 当前 stage 是否允许 tracked 知识库写入。

这导致 `v230` 质量失败后仍污染 tracked 经验池。

修复：

- pivot 只写 runtime jsonl：`cross_gen_exhausted_history.jsonl`。
- 只有 `commit_bot` 后的 `run_archivist` 可以写 tracked `experience_pool.md`。
- 若确实要记录失败经验，写入 `web/core/results/failed_generation_lessons.jsonl`，由人工或 archivist 审核后合并。

### 5. Crossover 验证缺少依赖闭包

`agent_review._run_crossover()` 只跑：

- `verify_code(target_dir)`，实际是逐文件 `py_compile`
- `run_smoke_test(target_dir)`

但 v230 证明二者不足。`py_compile` 不保证导入依赖存在，当前 smoke test 可在 BrokenPipe 后仍 passed。

修复：

- 新增 blocking gate：`python -c` 在 bot 目录前置 `sys.path` 后 import `main`, `strategy`, `postflop`, `opponent`, `state`。
- 对 changed files 做 AST import/function existence 检查：从 `strategy.py` 收集 `from opponent import X`，确认目标模块存在 `X`。
- crossover prompt 增加“若从 Parent B 搬函数调用，必须同时搬依赖函数或改 import”。
- smoke test 遇到 BrokenPipe、子进程 import traceback、invalid JSON 时必须返回非 0。

### 6. Stage 保护防止回退，但没有防止失败后副作用

`run_crossover` 成功后直接 checkpoint `stage="workers_done"`。随后 direction audit 看到会记录 “keeping advanced stage 'workers_done'”。这个保护防止了阶段倒退，但没有阻止后续 master/pivot 在同一个失败 generation 上继续产生副作用。

更具体地说，`evolution_infra.validate_stage_transition()` 明确允许 `workers_done/quality_passed/reviewed/critic_checked -> master_planned` 作为 retry reset。这对 reviewer/precommit retry 有意义，但对 crossover 已完成、quality 已失败、或 direction pivot 的路径太宽，会让系统从后期阶段回到 master 并重写计划。

修复：

- 给 crossover 单独阶段，例如 `crossover_done`，成功后只允许进入 `quality`。
- `workers_done -> master_planned` 只能在显式 `retry_after_review` 或 `retry_after_quality_failure` 时允许，且必须携带 failure feedback。
- quality failed 后 stage 应进入 `quality_failed`，并禁止 `run_master`、`run_direction_audit`、`cross_gen_pivot` 写 tracked 状态。
- 失败修复必须走 `execute_workers(reviewer_feedback/failure_feedback)` 或 `abandon_generation`，不能从 `workers_done` 再规划一个新 master。
- checkpoint 增加 `terminal_failure_reason` 和 `side_effects_allowed=false`。

### 7. Commit/tag 纪律仍有旁路

`v227/v228` 已经被普通提交纳入仓库，但没有 bot tag。系统能检测裸提交，却只能提高 `next_v` floor，不能修复污染。

修复：

- repo agent 文档继续要求：非 `commit_bot` 不得 stage `bots/claude_v*/`。
- 增加 pre-commit/status guard：若 staged 文件包含 `bots/claude_vN/` 且没有对应 `bot-vN` commit path metadata，拒绝提交。
- `git_commit_bot` 遇到已有 tag 时应拒绝或要求人工确认，不应自动删除再重建 tag。
- 对历史 `v227/v228` 做一次单独清理或补 tag 决策，不和本次报告混入。

### 8. Abandon cleanup 不完整

`v229` 的 abandon 事件显示 `removed_dir=null`，这类目录会继续干扰人工审计和后续工具判断。

修复：

- `abandon_generation` 必须有明确策略：`remove`, `graveyard`, `preserve-for-debug` 三选一。
- 默认应移动到 `bots/graveyard/failed_v{N}_{timestamp}/`，并记录 manifest。
- `.completed` 对未 tag bot 不应保留在 active path。

## 外部参考文献映射

这些参考不直接决定代码，但给出架构原则：

- ReAct 强调 reasoning 与 acting 交错，但前提是 action 结果被环境反馈约束。本项目的问题是 action 能写 tracked 状态，而失败反馈没有阻止后续副作用。参考：[ReAct: Synergizing Reasoning and Acting in Language Models](https://arxiv.org/abs/2210.03629)。
- Reflexion 把语言反馈作为下一轮学习信号，但反馈应来自明确结果。本项目把失败代际的 pivot 直接写进长期经验池，等于把未验证反馈固化。参考：[Reflexion: Language Agents with Verbal Reinforcement Learning](https://arxiv.org/abs/2303.11366)。
- Self-Refine 强调 iterative feedback/refinement，但每轮要有可检查反馈。本项目缺少 import closure/smoke failure 的硬反馈。参考：[Self-Refine](https://arxiv.org/abs/2303.17651)。
- Voyager 的 skill library 是成功技能累积，不是失败中间态随意入库。本项目的 `experience_pool.md` 应更像成功/审定 archive。参考：[Voyager](https://arxiv.org/abs/2305.16291)。
- SWE-agent 与 Agentless 都说明软件代理需要明确接口、测试和定位。本项目应把 bot 文件写入、git commit、pipeline state 全部收束到工具，不让 LLM 直接旁路。参考：[SWE-agent](https://arxiv.org/abs/2405.15793)、[Agentless](https://arxiv.org/abs/2407.01489)。
- LATS 把搜索、环境反馈、自反思结合；对应到本项目，应把失败 generation 作为搜索树分支丢弃或归档，而不是污染主干状态。参考：[Language Agent Tree Search](https://arxiv.org/abs/2310.04406)。
- MAP-Elites/Quality-Diversity 的 archive 语义是保存经过评价的精英或行为多样性样本，不是保存未通过 gate 的临时代码。参考：[MAP-Elites](https://arxiv.org/abs/1504.04909)。
- Deep CFR/ReBeL 说明扑克智能体改进应基于可评估的策略质量和反事实/信念反馈；本项目的 LLM 经验池也应绑定评估证据，而不是只绑定方向关键词。参考：[Deep CFR](https://arxiv.org/abs/1811.00164)、[ReBeL](https://arxiv.org/abs/2007.13544)。

## 修复计划

### P0：止血和现场整理

1. 暂停当前 `orchestrator.py` 与 `elo_daemon.py`，防止继续写入新脏项。
2. 撤销 `experience_pool.md` 中 `v230` 自动标记行，证据保留在本报告。
3. 将 `bots/claude_v229/`、`bots/claude_v230/`、`bots/claude_v231/` 移入 `bots/graveyard/failed_v229_v231_20260630/`，附 manifest；或按用户确认直接删除。
4. 保留 `docs/reference-patches/danlm-parallel-explorer.patch`，暂不把父仓库 gitlink 指到外部 remote 不可达 commit，除非先推到可访问 fork。

### P1：硬 gate 修复

1. 增加 `import_closure` gate，覆盖 `main/strategy/postflop/opponent/state`。
2. 修复 smoke tester：BrokenPipe、bot stderr traceback、子进程启动失败、invalid JSON 必须 fail。
3. crossover 完成后立即跑 import closure；失败则重试 crossover，不写 `workers_done`。
4. `_run_master_plan_audit` 接收并使用 checkpoint `next_v`，禁止 `source_v + 1` target 推断。
5. audit rejection 改为硬门；retry 耗尽 abandon/block，不接受 rejected plan。
6. quality failed 后 checkpoint stage 改为 `quality_failed`，阻止 `run_master` 和 `cross_gen_pivot` 写 tracked 文件。
7. `cross_gen_pivot` 只写 runtime jsonl；`experience_pool.md` 只由 archivist 在 commit 后写。

### P2：提示词重写

1. `orchestrator.md` 拆清 hard gates/advisory/telemetry。
2. `master_prompt.md` 明确输出必须声明并匹配 `source_v/target_v/target_dir`。
3. `master_plan_audit.md` 将 generation identity mismatch 升级为最高优先级 hard reject。
4. `crossover_prompt.md` 增加依赖闭包要求：搬调用必须搬定义，改 import 必须验证模块导出。
5. `worker_prompt.md` 把验证命令从 `py_compile main.py + smoke` 升级为 `py_compile all + import_closure + decision smoke subset`。
6. `orchestrator_context.py` 的 stage hints 对 crossover 路径应写成“crossover 成功后直接 quality”，避免 fresh/resume session 再调用 master。

### P3：git 纪律修复

1. 非 `commit_bot` 路径禁止提交 `bots/claude_v*/`。
2. 增加 staged-file audit：提交中含 bot 目录但无 bot tag 元数据时 fail。
3. 明确 agent 工作流：普通 docs/code 任务不得 stage evolution runtime artifacts、ladder outputs、bot generation dirs。
4. 对 `v227/v228` 做单独历史清理决策：补 tag、移 graveyard，或保留并记录为 bare bot 污染。

### P4：恢复演化前验收

1. `python -m py_compile web/core/*.py`
2. `cd web && python -m pytest tests/test_logic_*.py -v`
3. 手动构造 broken import bot，确认 import closure gate fail。
4. 对 `v230` 当前目录确认：`py_compile` 可过但 import closure fail，防止回归。
5. 恢复 orchestrator 后先 dry-run/one-gen，确认失败代际不再写 tracked knowledge。

## 建议的处理决策

建议采用保守方案：

1. `v229/v230`：先移入 graveyard 保留现场，不直接删除。
2. `v231`：因后台在报告期间新生成，也一并归档或删除；处理前先停后台进程。
3. `experience_pool.md`：撤销 `v230` 自动标记行。
4. `ref/DanLM`：父仓库提交 patch 文件作为可复现记录；暂不推第三方 remote，不提交不可达 gitlink。后续如果有 fork，再把 `ref/DanLM` remote 改为可推地址并记录 gitlink。
5. `v227/v228`：本次只报告，不混入清理；另起任务处理历史污染。
