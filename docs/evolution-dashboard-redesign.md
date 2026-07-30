# Evolution Dashboard 重构

> 原始交付分支：`codex/evolution-dashboard-redesign`（基于当时的 `origin/main` `fe39cfa6`）
> 当前集成审计：重放到 `origin/main` `8813819a` 后，与 inert producer-consumer Slice 1
> 一起在 detached worktree 中复核；本文件后半部记录 2026-07-19 语义修订。
> 任务：从真实 National TCP Poker Evolution 后端合同出发重构进化监控 Dashboard。

本分支**不合并 main**；完成后交主任务审计合入。

## 1. 参考项目与许可证清单

本重构**未 clone 任何外部仓库**，也未复制任何外部源码/图标/设计资产。设计参考仅
来自对成熟控制面板公开信息架构的一般性认识（任务第二节允许"借鉴信息架构、状态
可视化和操作模式"，但要求"最终实现必须适配本项目真实业务"）。具体决策全部基于本
仓库 `web/core` 与 `web/server` 的真实权威合同。

| 参考（概念性） | 借鉴的抽象模式 | 是否复制代码/资产 |
|---|---|---|
| Temporal / Dagster / Prefect UI | 非线性流水线状态图、retry/terminal 区分 | 否 |
| Argo Workflows | step 详情 + 失败分类 | 否 |
| MLflow | immutable run/cycle 身份绑定 | 否 |
| K8s 观察控制台 | daemon configured vs alive 区分 | 否 |

## 2. 当前问题与新信息架构

### 2.1 关键事实核对（origin/main fe39cfa6）

本分支起点是 `origin/main`，比任务最初基于的本地 checkout 领先 91 个提交。重新核对
后发现任务第五节 9 条"必须修正的语义差异"中 **8 条已实现**：

- ✅ `ActiveGeneration.parent2_v` 投影（`epoch_authority.py`）
- ✅ `canonicalGenerationIdentity`（区分 ordinal / canonical_version / bot_name / tag）
- ✅ `timed_out` / `infra_timed_out` 作为 `PIPELINE_TIMEOUT_LEASE_STAGE_CONTRACT`
- ✅ health.pipeline 消费（含 route / handoff / owner_scope / recovery / scheduler）
- ✅ `criticAdvisoryComplete`（schema/llm/executed + approved 精确判定）
- ✅ daemon 双语义（`configured` vs `alive` vs `heartbeat_status`）
- ✅ SSE paired status/health 观察 + `assertMatchingObservation`
- ✅ `stabilityView`（N/10 连续验收）

### 2.2 真实未覆盖的 gap（本分支解决）

1. **任务第六节 7 个主视图的信息架构**：旧前端只有 Overview / EvolutionMonitor 等
   10 个页面，缺少独立的 Pipeline Map（非线性）、Agent Activity（结构化）、Evidence &
   Gates（分层）、Failures & Recovery、Background Strength（70-hand job）视图。
2. **任务第八节测试基础设施**：前端已有 node:test 套件（`tests/sseController.test.mjs`），
   但缺少新视图/domain 层的 contract fixtures。
3. **后端缺结构化 agent / strength-job 只读端点**（worker 活动靠 SSE 文本正则解析）。

### 2.3 新信息架构

侧栏重组为 4 组（`web/frontend/src/layout/AppSidebar.tsx`）：

```
概览
  - 运行总览                     /
进化
  - 本代进度                      /pipeline   ← 唯一完整 stepper + handoff 八步
  - 研发协作                      /agents     ← 唯一实时 SSE
  - 发布资格                      /evidence
  - 异常与恢复                    /failures
  - 后台 70 手评测                /strength
  - 发布池                        /bots       ← Inventory + Manager 合并
对局（保留）
  - 对局回放 / 国赛对弈 / 评分趋势 / 对局矩阵
管理（保留只读契约）
  - 迭代日志 / 控制面板 / 严格发布 Bot / 提示词契约
```

Redirects（保留 path 字符串供契约测试）：

- `/evolution` → `/agents`
- `/bots-inventory` → `/bots`

被 `test_frontend_contract_closure.py` 守护的字符串（`"严格发布 Bot"`、`"提示词契约"`）
与旧路由 path（`/evolution`、`/bots`、`/prompts`）全部保留；兼容页入口从侧栏移除。

### 2.4 视觉契约 + handoff 八步投影（2026-07-30）

Evolution 局部设计原语在 `web/frontend/src/components/evolution/ui/`：

- `EvolutionSurface` / `EvolutionSection` / `EvolutionStatusBadge` / `EvolutionStepperTrack` / `EvolutionStreamShell`
- 令牌：`rounded-2xl` 表面、`rounded-md` badge、语义色 ok/warn/error/info/neutral/park
- 页头：`EvolutionPageHeader`（full|compact）+ `usePipelineCheckpoint`
- 契约条：`PhaseAProjectionStrip` + `AsyncCertificationQueue`；`manualRequired` 深链 `/control#abandon`
- Pipeline：`HandoffEightStep` + `PipelineDiagnostics`；Overview 只保留摘要链到 `/pipeline`
- 「非卡住」字典：`lib/notStuckReasons.ts`（consumer_parked / eval_wait / handoff / async cert）

后端 `post_publication_handoff_projection` 在 pending/running/blocked 时投影白名单：

```text
steps: [{ id, ordinal, status, plan_digest, receipt_digest, updated_at }, ...]
current_step / completed_count
```

计入 `projection_digest`；SSE `post_publication_handoff` 同形。永不透出 plan/receipt 体。

## 3. 数据 authority / normalization 矩阵

| 字段/视图 | source endpoint | authority 等级 | fail-closed 行为 |
|---|---|---|---|
| 流水线 stage / milestone | `/api/pipeline/checkpoint` | strict checkpoint | `null` → "权威活动阶段为 X，详细 checkpoint 暂不可用" |
| route / next_tool / recovery | `/api/control/health.pipeline` | strict epoch projection | `controlPipelineBlocked` → 显示阻断 + issues |
| handoff owner_scope | `/api/control/health.pipeline` | post_publication_handoff_journal | `assertMatchingObservation` 校验一致 |
| agent 活动（Master/Workers/...） | `/api/pipeline/agents`（新增） | checkpoint-derived | `{available: false}` |
| 70-hand job | `/api/pipeline/strength-jobs`（新增） | evaluation_bundle | `{available: false, reason}` |
| daemon 配置 vs 实际进程 | `/api/control/health.daemon` | control-plane snapshot | `daemonLivenessView` 5 态 |
| canonical 身份三联 | `/api/control/status.strict_published_bot_identities` | backend-owned | 不配对 → 不合成 tag |
| critic advisory 完成度 | `gate_results.critic` | advisory-only | `criticAdvisoryComplete` 精确字段链 |
| 不可采纳强度样本 | `/api/pipeline/strength-jobs.inadmissible_diagnostics` | diagnostic（零强度权重） | 明确标注原因，不计入强度 |

三种 checkpoint 形状（`/api/pipeline/checkpoint`、`/api/control/health.pipeline`、
`/api/evolution/state`）严格分层，每个 domain 视图只消费其中一种 + paired status/health，
从不跨接口拼接。

## 4. 修改文件清单

### 后端（最小、只读、复用现有 authority 守护）
- `web/server/routes/pipeline.py` — 新增 `GET /agents`、`GET /strength-jobs` 端点
- `web/tests/test_routes_pipeline_agents.py` — 新增（5 测试）
- `web/tests/test_routes_pipeline_strength_jobs.py` — 新增（4 测试）
- `web/tests/test_frontend_contract_closure.py` — 新增 4 个守护测试

### 前端
- `src/api/types.ts` — `AgentActivityResponse` / `StrengthJobsResponse` 类型契约
- `src/api/agentActivity.ts` — fail-closed 校验器（新增）
- `src/api/strengthJobs.ts` — fail-closed 校验器（新增）
- `src/api/client.ts` — `pipelineAgents()` / `pipelineStrengthJobs()` 方法
- `src/domain/agentActivityView.ts` — Agent 角色/活动 normalization（新增）
- `src/domain/strengthJobView.ts` — daemon liveness + 不可采纳诊断（新增）
- `src/domain/evidenceAuthority.ts` — 五层证据 tier（新增）
- `src/domain/failureRecoveryView.ts` — 失败/恢复 disposition（新增）
- `src/pages/PipelineMap.tsx` — 非线性流水线地图（新增）
- `src/pages/AgentActivity.tsx` — 结构化 Agent 活动 + SSE 对话流（新增）
- `src/pages/EvidenceGates.tsx` — 分层证据视图（新增）
- `src/pages/BotInventory.tsx` — 身份三联 Bot 清单（新增）
- `src/pages/FailuresRecovery.tsx` — 失败与恢复（新增）
- `src/pages/BackgroundStrength.tsx` — 70-hand 强度任务（新增）
- `src/App.tsx` — 注册新视图路由（保留旧路由）
- `src/layout/AppSidebar.tsx` — 新侧栏分组（保留守护字符串）
- `tsconfig.sse-test.json` — 把新 domain/API 文件纳入测试编译
- `package.json` — `test` 脚本扩展为 `tests/*.test.mjs` glob
- `tests/domainViews.test.mjs` — domain 层 15 测试（新增）
- `tests/contractFixtures.test.mjs` — 任务第八节 21 状态 fixtures（新增）

### 文档
- `docs/evolution-dashboard-redesign.md` — 本文档
- `docs/dashboard-redesign-shots/*.png` — 7 张 1920×1080 截图

## 5. 页面与组件结构

每个新视图只消费 normalization 层（`hooks/useControlStatus` 的 paired status/health、
`DataProvider` SSE、`api.pipelineAgents()`、`api.pipelineStrengthJobs()`、`lib/`、
`domain/`），不在组件内解释后端字段。

- **Pipeline Map** (`/pipeline`)：复用 `PipelineStatus` 线性 stepper + 超时租约卡片
  + health.pipeline route/recovery/handoff/scheduler。
- **Agent Activity** (`/agents`)：左 panel `agentActivityView` 角色状态，右 panel 复用
  `useEvolutionSSE` + `ToolCard` 的 LLM 对话流。
- **Evidence & Gates** (`/evidence`)：`evidenceAuthority` 五层 tier + gate 完成度
  + `OfficialCertificationProgress` + bootstrap job tier。
- **Bot Inventory** (`/bots-inventory`)：按 `generation_ordinal` 配对身份三联
  `第N代 · national_vX · national-bot-vX`，不重编号、不合成 tag。
- **Failures & Recovery** (`/failures`)：`failureRecoveryView` 投影 worker failure
  + pipeline recovery + infra overlay + terminal gate outcome，区分 auto_retry /
  awaiting_lease / needs_repair / authority_conflict / operator_action / terminal。
- **Background Strength** (`/strength`)：`strengthJobView` 投影 admitted/staged/
  inadmissible + daemon liveness + identity digest + 15 种拒绝原因中文标签。

## 6. 关键状态 fixture / test

| 状态 | 文件 | 断言要点 |
|---|---|---|
| 未初始化 epoch | `contractFixtures.test.mjs` | `{available: false, reason}` |
| fresh bootstrap v143 | 同上 | parent2=null |
| crossover 双亲 | 同上 | parent1/parent2 同时投影 |
| timed_out / infra_timed_out | 同上 + `domainViews.test.mjs` | timeout lease，非未知阶段 |
| Critic advisory | 同上 | advisory_only，非强度门 |
| first-strict bootstrap | 同上 | strength_evidence_weight=0 |
| normal official signed_full_v5 | 同上 | compliance tier |
| Reviewer infra timeout retry | 同上 | auto_retry，非 strategy rejection |
| terminal gate (review_rejected) | 同上 | terminal abandon |
| parent2 mismatch | 同上 | authority_conflict |
| daemon configured but dead | 同上 | configured_dead |
| daemon alive but stale heartbeat | 同上 | alive_stale_heartbeat |
| 69 手不可采纳 | 同上 | inadmissible diagnostic |
| active_pool_empty | 同上 | 不伪造强度 |
| malformed response | 同上 | fail closed |
| two published bots identity | 同上 | 双身份 distinct |

## 7. typecheck / lint / test / build 命令与结果

```bash
cd web/frontend
npm run lint          # exit 0
npx tsc -b            # exit 0
npm test              # 61 tests pass (PYTHON=$(which python))
npm run build         # built in 1.97s, static-build-receipt written

# 后端
PYTHONPATH="web/core:web/server:." python -m pytest \
  web/tests/test_routes_pipeline_agents.py \
  web/tests/test_routes_pipeline_strength_jobs.py \
  web/tests/test_routes_pipeline.py \
  web/tests/test_frontend_contract_closure.py -q
# 36 passed
```

## 8. 真实后端只读联调证据

启动临时 `python web/main.py --view-only --no-build --port 187xx`（不启动 orchestrator
或 daemon），只读 GET 联调：

```
GET /api/pipeline/agents          → {"available": false, "reason": "no_strict_workflow"}
GET /api/pipeline/strength-jobs   → {"available": false, "reason": "policy_epoch_reset_unavailable",
                                     "daemon": {"alive": false, "configured": false, ...}}
GET /api/control/health           → overall=stopped, pipeline.exists=False, daemon.alive=False
GET /api/pipeline/checkpoint      → null
GET /api/evolution/state          → epoch_state=reset_required, epoch_initialized=False
GET / /pipeline /agents /evidence /bots-inventory /failures /strength  → 全 HTTP 200
```

所有端点在无运行进化系统时正确 fail-closed，不伪造健康数据。**未调用任何写接口**
（Start/Stop/Abandon/Cancel/Certification/Publish）。

## 9. 桌面 / 宽屏截图（三档分辨率 × 双主题 × 7 视图）

42 张截图位于 `docs/dashboard-redesign-shots/`，覆盖任务第七节要求的 **1366×768 /
1920×1080 / 宽屏 2560×1440 三档分辨率 × 深色/浅色双主题 × 7 个新视图**。命名规则
`<view>-<resolution>-<theme>.png`，例如：

- `command-center-1920-dark.png`、`command-center-1920-light.png`
- `command-center-1366-dark.png`、`command-center-2560-light.png`
- 同理 `pipeline-map-*`、`agent-activity-*`、`evidence-gates-*`、
  `bot-inventory-*`、`failures-recovery-*`、`background-strength-*`

**验收方法**：Playwright headless（`/usr/bin/google-chrome`，`--no-sandbox`），对每个
组合导航到路由 → 设 `localStorage["theme"]` + `.dark` class → 等 networkidle + 1s →
截图；同时收集 `console.error` 与 `pageerror`。

**结果**：
- 截图数：**42/42**（3 分辨率 × 2 主题 × 7 视图，全部生成）
- 浏览器 console/pageerror：**0 错误**
- 主题切换验证：dark 21/21 正确应用（中心像素 `rgb(16,24,40)` 深背景）、
  light 21/21 正确应用（中心像素 `rgb(249,250,251)` 近白）
- 视觉抽查（analyze_image）：1920-light Command Center（白底/侧栏/中文/无对比度问题，
  生产质量 ✓）、1366-dark Pipeline Map（侧栏 collapse 为图标态、卡片堆叠、无溢出、
  文本可读 ✓）

**布局响应式**：所有视图用 `grid-cols-1 lg:grid-cols-2/3`，1366 小屏自动降级为单列，
侧栏 collapse；1920/2560 双列或三列。深色与浅色主题均达到生产质量。

**关于"有数据"态截图**：本 worktree 是干净的 `origin/main`，无运行的进化系统（任务
第一节"绝对禁止：启动、停止或重启扑克进化系统"），`.evolution_pok` 在本机不存在。
所有截图显示的是 **fail-closed 空状态**（如"严格国赛 epoch 尚未初始化"、
"无 strict workflow"），这是权威要求的正确行为而非 UI bug。"有数据"的真实运行态
截图需在主任务的真实 `.evolution_pok` 环境补做，不在本分支范围内。

## 10. 已知限制

1. **"有数据"态截图**：见 §9 末段——需真实运行的进化系统，本分支环境无法实现；
   所有截图是 fail-closed 空状态（权威正确行为）。
2. **Bot Inventory 的 `parent` 字段**：`BotSummary` 类型无 `parent`（只在 `BotDetail`），
   该视图暂不显示 parent；如需展示需调 `api.botDetail(version)`（未做以避免 N+1）。
3. **Agent Activity 对话流**：复用 `useEvolutionSSE`，与旧 `EvolutionMonitor` 行为
   一致；未抽成共享 hook（两视图各自维护，代价是少量重复）。
4. **后端 owner_scope 完全对齐**：任务第五节第 4 点期望 SSE 与 HTTP Start boundary
   一样校验 `owner_scope`。后端已有 `post_publication_handoff.owner_scope` 与
   `useControlStatus` 的 `assertMatchingObservation` 校验，但 SSE evolution stream
   本身的 owner_scope 校验维度仍以 `reset_receipt_digest` 为主——完全对齐需后端
   further work。
5. **未做组件 DOM 单测**（react-testing-library）：前端测试沿用现有 node:test 模式
   覆盖纯函数/domain 层；组件渲染测试未引入（避免引入 vitest/jsdom 重依赖）。如需
   组件级 DOM 测试，建议后续评估轻量方案。

## 11. commit SHA

本分支提交（按时间序）：
1. `feat(pipeline): add read-only agent activity and 70-hand strength-job endpoints`
2. `feat(frontend): add API clients and domain views for agents and strength jobs`
3. `feat(frontend): add seven structured dashboard views and redesigned navigation`
4. `docs(dashboard): redesign documentation, contract fixtures, and screenshots`

（最终 SHA 见 `git log codex/evolution-dashboard-redesign`。）

## 12. push 后的分支状态

分支 `codex/evolution-dashboard-redesign` 基于 `origin/main fe39cfa6`，**不合并 main**。
等待主任务审计合入。

## 13. 与最新 origin/main 的差异 / 冲突检查

- base：`fe39cfa6`（fetch 时最新 `origin/main`）
- 本分支仅新增文件 + 修改 `pipeline.py` / `client.ts` / `types.ts` / `App.tsx` /
  `AppSidebar.tsx` / `package.json` / `tsconfig.sse-test.json` /
  `test_frontend_contract_closure.py`
- 无对 `web/core` 的修改（后端端点全在 `web/server/routes/pipeline.py`）
- 无对 `sever/`、`bots/`、`scripts/`、`.evolution_pok` 的任何改动
- 不触碰 `archive/`、`pok-arena`、`codex/three-bot-consolidation`

## 14. 2026-07-19 集成语义审计（基于真实 workflow-v68）

### 14.1 为什么需要第二轮修订

原始页面虽然能安全展示 raw `stage`、`route`、`checkpoint`、`intent` 和 digest，但把操作员
最关心的问题拆散到了多个卡片：**当前发生什么、为什么、下一步是什么、需不需要人工**。
真实运行态证明这不是纯文案问题。复核时 `/api/control/status` 与配对的
`/api/control/health` 精确显示：

- 第 1 代 / `national_v143` / `generation:143:workflow-v68`；
- `direction_audited`，checkpoint revision 5；
- overall healthy，Web 编排任务仍持有运行权威；
- route 是 `infra_retry -> run_master`，Master counterfactual scout 的结构化重试在
  240 秒超时，当前记录 attempt 1/3，下一动作是同阶段第 2/3 次重试；
- 上一 `workflow-v67` 已因 proposal strict-authority schema retry exhausted 经 canonical
  abandon 结束，v68 是同一网页第 1 代的独立 successor；v67 不构成已发布 Bot 或强度证据；
- rating daemon `configured=true`、`alive=true`、进程身份匹配且心跳 fresh，但
  `activity_state=waiting_for_first_published_bot`，所以“进程健康”并不等于“有对局在跑”。

### 14.2 新的统一操作员投影

七个主视图顶部现在都复用 `operatorSituationView` / `OperatorSituation`。它只消费配对的
control status/health，不从日志、目录或 SSE 文本猜测状态，并统一回答四个问题。raw stage、
route、workflow、revision 和 tag 被下沉到可展开的“技术身份与原始路由”。关键分支有纯函数
fixtures：

| 真实业务状态 | 主文案 | 人工介入 |
|---|---|---|
| Master/Reviewer/Worker infra retry | 哪个角色局部失败、第几次重试、候选保持不变 | 编排器在跑时不需要 |
| canonical abandon 后 successor | 上一次 workflow 已受控结束，当前是同一网页代次的新尝试 | 不把旧失败算发布或强度 |
| `timed_out` / `infra_timed_out` | 明确是超时恢复，不是成功进度或未知阶段 | route 存在且编排器运行时不需要 |
| 首代 operator certification | 一次性 system-control 5+3，只证明官方兼容 | 启动边界需要操作员；运行后不重复操作 |
| blocked authority/identity | 说明阻断字段并停止猜测下一工具 | 需要操作员按诊断处理 |
| publishing handoff / scheduler | 发布后收尾或准备下一代 | 正常时不需要 |
| Slice 2b `consumer_parked` | 主槽旁路等待（非卡住）；草稿槽可并行 | 编排器在跑时不需要 |
| `eval_wait.waiting` | 强度样本不足时的准备等待提示 | 不把等待当成恢复阻断 |
| staging 父本（`staging_as_parent`） | 主父本尚未 certified 时明确提示 | 证书/tag 仍是发布权威 |

Phase D（2026-07-30）在不重做视觉的前提下，把上述真值接到关键面板：
`OperatorSituation` 双槽徽章与 context tips；`PipelineStatus` primary+draft 并行态；
`ControlPanel` 受控放弃 / 异步认证队列 / daemon `effective_*`+`pairs_drift`；
发布概览与 BotManager 统一 `certificationView` 的 `publicationTier`/`certifiedTag`；
EvolutionMonitor 仅在 IO 事件带 `slot` 时标注 primary/draft。

### 14.3 七页文案与业务语义

- **运行总览**：把“权威样本”明确为“已采纳完整 70 手样本”，把严格代次明确为已发布代次。
- **本代进度**：主卡只讲当前研发/验收进度和下一动作，raw route 放进展开项。
- **研发协作**：`is_working=false` 但 control task 仍 active 时显示“等待局部重试/阶段切换”，
  不再错误显示“运行标志存在但任务未活动”。
- **发布资格**：Reviewer 是会阻断发布的代码审核门；只有 Critic 是建议。Official 5+3
  是零强度权重的合规认证，不能替代 native 70 手强度。
- **发布概览**：只列完成 certificate + commit + `.completed` + annotated tag 的 Bot；正在
  生产的候选不提前出现。详细源码/证书仍在受合同守护的“严格发布 Bot”页。
- **异常与恢复**：区分当前 recovery route 与本 workflow 的历史 Worker 失败行；历史行本身
  不再声称仍需修复。Reviewer feedback 是绑定审核门的解释文本，不再误称 advisory。
- **后台 70 手评测**：修正 `strength_sample_count` 的单位（完整 70 手样本，不是“手”）；
  staged-pending 明确为已落盘待原子发布，而不是正在对局。

### 14.4 Producer-consumer 与后台 job 的诚实边界

> 本节记录当时的运行能力事实，不是前端常量。14.9 已把该结论改成后端 capability
> 投影；未来 dispatcher 真正启用后，页面必须随权威字段变化，不能继续显示本段旧结论。

集成 worktree 含有 producer-consumer Slice 1，但它仍是 **inert shadow**：不被 launcher、
orchestrator 或 rating daemon import/start/call，不调度真实对局。Dashboard 因此不会展示它为
active。当前 `/api/pipeline/strength-jobs` 只能权威展示：

1. 已纳入当前 immutable cycle 的完整 native 70 手样本；
2. 已落盘、等待周期原子发布的 staged rows；
3. 因 69 手、旧 identity、off-pool 等原因拒绝的零权重诊断；
4. rating daemon 的 configured / alive / heartbeat / activity_state。

当前后端尚未提供真正异步队列的 `accepted / queued / running / verifying / terminal`、job id、
lease/heartbeat/cancel、资源占用与 barrier receipt 字段。前端明确显示该可见性缺口，不用
staged replay 猜测“正在运行”。这些字段应随 producer-consumer Slice 2 在统一 workflow
kernel 中实现后再接入，不能先造假 UI。

### 14.5 截图说明

`docs/dashboard-redesign-shots/` 的 42 张图片仍是原始布局与双主题/三分辨率验收证据，
**早于本次操作员语义修订**，不能作为最新文案逐字一致的证据。本次提交不伪造新截图；
合入并在真实有数据运行态启用后，应重新捕获七页的 v68+ 状态与典型失败 fixtures。

### 14.6 本轮验证

```bash
cd web/frontend
PYTHON=/home/zzx/anaconda3/envs/pytorch/bin/python npm test  # 70 passed（14.6 时点）
npm run lint                                                # exit 0
npx tsc -b                                                  # exit 0
npm run build                                               # exit 0，source-bound receipt 已写入

cd ../..
PYTHONPATH='web/core:web/server:.' \
  /home/zzx/anaconda3/envs/pytorch/bin/python -m pytest \
  web/tests/test_frontend_contract_closure.py -q             # 21 passed
git diff --check                                             # exit 0
```

Vite 仍报告既有的单 bundle 大于 600 kB 提示；构建成功，且该提示不改变 runtime/证据合同。
本轮只修改 `web/frontend/**` 与本文档，没有修改或重启 Web backend、orchestrator、rating
daemon、`.evolution_pok`，也没有让 inert producer-consumer slice 开始调度任务。

### 14.7 合入前 P1 权威与观察性能闭合

独立审查和真实 v68 认证运行又发现了若干“页面可见但证据比生产门更松”的问题，本追加
修订按 fail-closed 原则闭合：

- `/api/pipeline/agents` 不再复制简化字段判断，而是直接复用生产
  `_quality_gate_ok` / `_review_gate_ok` / `_critic_gate_ok` 与
  `_precommit_gate_matches_active_workflow`。因此 workflow profile、native template、动态 probe、
  strict-bootstrap system receipt 任一漂移都会把 `complete` 降为 false。
- Official gate 只有重新打开签名证书、验证 verdict ledger、候选内容与正式 8 轮 profile 后才
  为 complete；checkpoint 中单独的 `passed=true` 没有展示权威。
- Worker failure 与 agent 字段使用同一个已验证 checkpoint 对象；浏览器再把 `/agents` 的
  next/source/parent2/stage/run/workflow/revision 七元组与配对 control active generation 精确
  交叉绑定。任一字段移动都会清空所有四个消费者。
- `.pending` 只展示 no-follow、大小/数量受限读取后，完整通过 `validate_native_replay`、当前
  evaluation identity、当前发布池和当前 artifact hash 的 70 手 envelope。69 手、旧身份、
  off-pool、false claim、symlink、读时替换或超限文件只进入零权重拒绝诊断。
- Master 的 `started` 与 `completed` 分离；`direction_audited + run_master/retry_same_tool` 是
  运行中的局部重试，不再画成 terminal。Critic 未完整执行时 evidence tier 为 zero。
- Agent Activity 使用与 EvolutionMonitor 相同的 task-owner lifecycle high-water、authority-lost、
  clear-IO 和 SSE status identity fence；successor/revision 变化在绘制前清除旧消息，HTTP
  evolution state 不再恢复未绑定的工作文案。
- 首代认证的 control transition 会在稳定 epoch 样本之后读取唯一 durable job，且仅在
  workflow/version/source/stage/revision 与候选请求全部匹配时从 required 细化为
  running/failed/ready；任何并发 identity movement 保留 epoch 的安全默认。
- 只读 `/status` 与 `/health` 增加 1 秒 TTL、deep-copy、异常不回退的进程内 singleflight。
  多个 Dashboard 同时轮询会共用一次完整签名/ledger/strict epoch 投影；health 复用同一 status
  authority。启动 barrier 始终调用 fresh 投影并继续双采样，所有生命周期写操作主动失效缓存。

这些修订仍不启用 producer-consumer Slice 1，也不改变候选、checkpoint、rating 或认证
状态机；它们只收紧观察证据并降低多客户端重复观察造成的 GIL 排队。

追加修订后的最终 focused 结果为：前端 `75 passed`；后端 agents/strength/control/
certification/frontend-contract 联合切片 `167 passed`（仅 1 条既有 Starlette dependency
deprecation warning）；Python compileall、lint、TypeScript、Vite build 与 diff-check 均通过。

### 14.8 第二轮 P1：有界观察、完整任务身份与修复态失效

第二轮独立审查发现首轮虽已收紧 authority，却仍可能在多个页面同时打开时把大型
checkpoint / replay 验证工作留在 asyncio 事件循环，并且 official job、SSE 生命周期和
repair gate 的显示合同还不完整。本追加提交只修改观察面与正负回归，不改变候选、评分、
认证或状态机写路径：

- `/api/pipeline/agents` 的 checkpoint/failure 读取及生产 gate 验证全部经独立 blocking
  worker 执行。进程内缓存最多 4 个条目、TTL 0.75 秒，条目同时绑定 checkpoint 文件身份与
  `workflow_run_id/checkpoint_revision/next/source/parent2/stage/run`；同一浏览器轮询窗口
  singleflight，revision 或文件身份变化立即 miss。缓存从不用于 Start 或任何 mutation。
- Agent gate HTTP 字段改成按 gate 名固定 scalar allowlist；完整 status、receipt、stdout、
  certificate body 不再发送给浏览器。Master 最多 8 个 task、每 task 最多 8 个 target，
  failure 最多 10 条，字符串和总 response 均有硬上限。超出总响应预算返回 typed
  `available:false`，不会截取一段大型 receipt 冒充完整投影。
- `repair_planned/rework_running` 中，修复前 quality/review/critic/precommit/official 记录
  统一投影为 `authority_state=historical_invalidated, complete=false`。页面显示“历史记录已
  失效”，Workers 显示正在修复，修复完成后必须重新运行门禁，不再出现旧绿色合规。
- `/api/pipeline/strength-jobs` 对 immutable bundle 只验证/读取一次，然后把同一对象交给
  strength authority helper 和诊断投影。全部工作 offload，并共享全局预算：256 个目录项、
  64 个 pending JSON、80 个总文件、8 MiB 总读取、0.75 CPU 秒、3 墙钟秒、1000 行；任一
  超限清空 partial staged/diagnostic rows，返回明确 observer issue。三类 row 均支持
  `offset/limit`（limit 最大 100）并返回各自 total/has_more，daemon stats 也仅投影固定摘要。
- `/api/certification/jobs` 现在携带完整 current checkpoint identity：next/source/parent2、
  stage、workflow、run 和 revision。前端逐字段绑定；即使 stage/workflow/version 相同，旧
  revision 也会失效并清空任务投影。
- Agent Activity 沿用 EvolutionMonitor 的 `acceptedAt` 与 source/local 双界 30 秒 expiry。
  status 超时后立即回到中性文案；即使浏览器 timer 被节流，后续 IO/tool 仍按当前时间再次
  校验并拒绝。task owner、successor、clear-IO、disconnect 也同步清除这组权威。
- Evidence & Gates 只创建一次 `useOfficialCertificationJobs`；其投影作为 props 交给纯渲染
  子组件，消除同页双轮询。

旧 `docs/dashboard-redesign-shots/` 截图早于这些最终语义，明确排除在本轮验收外；本轮未
生成或修改截图，避免用旧空状态图片证明新运行态行为。新增回归覆盖 raw receipt 不外泄、
响应上限、cache revision invalidation、repair 历史门、目录/文件/字节/CPU 预算、分页 totals、
慢扫描期间 control health 响应、official same-stage stale revision、30 秒 IO/tool expiry 和
Evidence 单 hook。

最终验证：前端 Node 合同/领域/SSE 测试 `76 passed`，TypeScript、ESLint 与生产构建通过；
后端 pipeline/agent/strength/certification/control/observability 联合切片 `220 passed, 3 skipped`
（仅既有 Starlette dependency deprecation warning）；Python compileall 与 `git diff --check`
通过。Vite 的既有大 chunk 提示仍为非阻断 warning，不作为运行态正确性证据。

### 14.9 第三轮 P1：边界时态、跨接口身份与观察成本

本轮闭合的是观察面与实际状态机之间最后一组 P1 差异，仍然不改变候选、评分、认证或
checkpoint，也不启用 producer-consumer 调度：

- checkpoint `stage` 不再一律解释为“正在”。前端维护覆盖全部 `STAGE_ORDER` 的
  `PIPELINE_STAGE_PROGRESS`：成功 stage 是**已落盘完成边界**，配对
  `health.pipeline.route.next_tool` 才是下一动作；transitional 与 failed/rejected stage
  分别显示“执行中边界”和“失败/拒绝边界”，未通过的目标节点不会变绿。repair 只保留
  Master 规划前缀，修复后的 Worker 与门禁必须重新完成。
- Agent role 状态使用当前 gate/plan 高水位；后端把 checkpoint-owned Master plan presence
  作为完成证据，不因 checkpoint 改写成 `timed_out` 或
  `infra_timed_out` 倒退为“未开始”。`review_rejected` 明确是 Reviewer 已形成绑定拒绝，
  不是 Reviewer 仍在运行；无法从超时租约证明的角色显示 unknown，而不是猜测。
- Worker failure JSONL 行由后端明确标记
  `record_state=historical,current_blocker=false`，并保留时间；前端使用“历史记录” disposition。
  `infra_failure` 的 action/code/owner_tool/resume_stage/attempt/max/exhausted 完整 allowlist
  与浏览器 validator 一致。只有配对 health 中的 timeout stage + exact route 才生成
  `awaiting_lease`；route 不匹配时拒绝猜恢复工具。
- strength observer 先以 no-follow stat/manifest 对文件、字节、行、CPU 和墙钟做完整预留，
  超预算时生产 authority loader 调用数为 0。一个 evaluation identity 只做一次重型观察，
  `offset/limit` 在其深拷贝上切片；翻第二页不再重读完整 bundle/replay。FastAPI 在调度
  blocking worker 前拒绝 offset/limit 越界。
- strength 的 available/unavailable/预算失败响应都携带同一个
  `authority_binding`（epoch、active_bots、reset receipt、evaluation/manifest digest）及
  显式 `capabilities`。浏览器与 `/api/control/status` 的 active pool + reset digest 任一变化
  即隐藏旧观察。非法 Bot 名、snapshot/bundle 双源漂移、旧 reset、旧 pool 和不完整 binding
  都 fail closed。
- Background Strength 不再把 `inert shadow` 写死在 JSX。文案仅由
  `durable_job_lifecycle`、`queued_running_leases`、`producer_consumer_dispatch` 三项后端能力
  决定；当前三项均为 false，所以诚实显示“尚未启用”。未来只有三项同时为 true 才能声称
  job lifecycle 可见。
- official durable jobs 只允许在
  `official_bootstrap_required/official_certifying/official_failed/official_inconclusive` 四个认证
  stage 轮询。后端 `/agents.orchestrator.official_jobs_polling_supported`、浏览器 stage enum 和
  hook 内部硬门三者一致；普通 planning/Worker/Quality/verified/publishing/archived 页面不会
  周期性重开认证账本。

正负 fixture 不依赖运行 daemon：Python 测试通过真实 endpoint builder、严格 bundle loader
sentinel 与 observer cache 验证“预算前拒绝、一次重读、多页切片、身份漂移”；新增
`captureDashboardAuthority.py` 直接调用生产 Python route builder，再由 Node 的正式 validator
接收同一 payload。其余 TypeScript fixture 验证 timeout、review rejection、reset/pool rotation、
capability false/true、非法 nested infra 字段及 official stage allowlist。
它们证明的是代码合同，不替代合入后的真实有数据截图和运行验收。

本轮隔离验收：focused backend + cross-language contract `55 passed`；前端 producer/domain/SSE
`84 passed`；TypeScript build、ESLint 与 Vite production build 通过。更大
routes/evaluation/observability/control/certification 切片分别达到 `303 passed, 18 skipped`
与 `198 passed`。合入更新 main 后的完整 Web suite 与有数据只读联调仍须由集成主线复验。

完整 `web/tests` 在本分支旧基线得到 `3893 passed, 24 skipped, 2 failed`。两项失败均位于未改动
的 `test_epoch_authority.py`；在干净 parent `51a9b3d3` detached worktree 单独复跑同样 2/2
失败，证明不是本轮 Dashboard diff 引入。集成到更新的 `origin/main` 后仍必须重新跑完整套件，
不能把该基线归因证明替代最终 main 绿色验收。

### 14.10 真实负载 P1：观察权威不得阻塞 ASGI

v147 Master/Worker 活跃期间的并发只读实测暴露了新的性能因果链：
`/api/control/status` 与 `/health` 可超过 20 秒，`/api/evolution/state` 和原始 checkpoint
也会超过 10 秒；Web/Elo 进程本身仍存活。根因不是工具持有一把长期业务锁，而是
`evolution/state`、SSE 每次投递和原始 pipeline 端点在 ASGI event loop 内同步执行完整
Git/checkpoint 权威读取；五秒远端发布证明过期后，多条观察路径还会并发执行
`git ls-remote origin`，把一次慢网络证明放大为全站排队。

闭合合同如下：

- checkpoint、failure、evolution state 和 SSE 的完整权威读取全部转移到隔离 blocking
  worker；事件循环仍可服务 health、静态资源及其他只读请求。
- 远端 completion/high-water/main 发布证明增加进程级 singleflight。所有调用者共享一次
  fresh 远端事务；cache invalidation 期间返回的旧事务结果丢弃。该层不提供 stale 结果，
  因此 launch、publication 与 mutation 的远端证明严格性不变。
- `/api/control/status` 的只读缓存采用本地内容键控的 bounded stale-while-revalidate：只有
  本地 paired tag/commit、checkpoint、reset/abandon/reap、handoff、稳定性/evaluation
  manifest、已发布 Bot manifest/receipt/sentinel 与证书身份均未变化时，才可在后台刷新
  期间短暂复用上一份完整投影。本地内容键一旦变化，旧投影立即 fail closed。
- `_control_launch_authority_snapshot()` 继续直接调用 fresh status 双采样，完全绕过只读缓存；
  本修复不允许 stale 观察进入 Start 或任何 effect boundary。

并发回归使用慢远端证明和阻塞 authority sentinel 验证：四个发布证明调用只产生一次
`ls-remote`；同内容 status/health 立即返回有界旧投影；内容键漂移不会等待或消费旧字节；
slow checkpoint、evolution state 与 SSE authority 工作期间 asyncio heartbeat 仍可调度。
