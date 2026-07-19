# Evolution Dashboard 重构

> 分支：`codex/evolution-dashboard-redesign`（基于 `origin/main` `fe39cfa6`）
> Worktree：`/home/zzx/project/pok/.codex_worktrees/evolution-dashboard-redesign`
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
  - 总览 (Command Center)        /
进化
  - 流水线地图                    /pipeline
  - Agent 活动                    /agents
  - 证据与质量门                  /evidence
  - 失败与恢复                    /failures
  - 后台强度任务                  /strength
  - Bot 清单                      /bots-inventory
  - 进化监控（旧，向后兼容）      /evolution
对局（保留）
  - 对局回放 / 国赛对弈 / 评分趋势 / 对局矩阵
管理（保留只读契约）
  - 迭代日志 / 控制面板 / 严格发布 Bot / 提示词契约
```

被 `test_frontend_contract_closure.py` 守护的字符串（`"严格发布 Bot"`、`"提示词契约"`）
与旧路由（`/evolution`、`/bots`、`/prompts`）全部保留，向后兼容且守护不弱化。

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
