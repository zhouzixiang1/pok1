# 接手任务提示词：national_native 进化系统运行态加固

直接复制以下内容到新对话即可接手。权威交接文档：`docs/handoff-2026-07-09-national-runtime-hardening.md`（已推送 origin/main）。

---

你接手 `/home/zzx/project/pok` 项目，继续把 national_native 进化系统推进到可长期稳定运行的状态。这是一个**长期运行任务**，不是单点修 bug。

## 上一窗口已完成（不要重做）

原始阻塞是**无限交叉超时死锁**，已用 4 个修复完全解决并验证（均在 `origin/main`，已同步 `.evolution_pok`）：

- **Fix A** (`6afa1fc0`, `tool_commit.py`+`orchestrator.py`)：交叉重试耗尽 → 干净放弃（`CROSSOVER_LLM_EXHAUSTED`），不再死循环。
- **Fix B** (`6afa1fc0`, `llm_query.py` `substantive_activity_logged`)：`SystemMessage(init)` 不再满足实质性首活动门 → init 后卡顿在 `first_activity_timeout` 捕获。
- **Fix C** (`d881658c`, `llm_query.py` `stall_timeout`)：子角色中途工具循环卡顿 → ~55% idle 的 stall_timeout 快速恢复。
- **Fix D** (`680b3292`, `orchestrator.py` `ORCH_STREAM_STALL_TIMEOUT`)：orchestrator 主代理早期无 checkpoint 时的卡顿 → 300s 上限（原为 5400s `CYCLE_TIMEOUT`）。

全部经单元测试 + 实测验证。**系统现在安全**（无死锁、卡顿快速捕获并优雅恢复）。不要再改这些，除非有新证据表明有问题。

## 当前运行态

- 进化**已暂停**（`/api/control/stop`，`running=False`），web 服务器仍在 :8000（pid 可能已变，用 `pgrep -af web/main.py` 确认）。
- `.evolution_pok` 在 `main@680b3292`，无 checkpoint（干净暂停）。
- 暂停原因：**LLM 后端间歇性卡顿**——`claude_agent_sdk → cc-switch(127.0.0.1:15721) → deepseek-v4-pro`。简单 curl 正常（http=200 ~1-2s），但复杂多轮工具角色（MASTER/CROSSOVER/WORKER/甚至 COMBINED ANALYST）频繁卡死。cc-switch 本身健康（用户已确认，日志无错）。系统负载持续高（10.5-12.5），本会话期间机器重启过 3 次。
- 后端是**间歇可用**的：v130 曾一路走到 `reviewed`（CROSSOVER+quality+review 全过）才在 critic-rework 卡住。所以后端有稳定窗口时代次能完成。

## 你要做的事（按优先级）

### 0. 先读交接文档
`docs/handoff-2026-07-09-national-runtime-hardening.md`（在 origin/main）。含完整证据、恢复步骤、gotchas、禁做清单。

### 1. 验证后端再决定是否恢复进化
**恢复进化前必须确认后端不卡了**。简单 curl 不够，要跑多工具 SDK 探测（健康时 6 次工具调用 ~30s 完成；卡死则超时无输出）。探测脚本可在本会话 git 历史找，或自己写：`claude_agent_sdk.query(prompt=多步读取任务, options=ClaudeAgentOptions(tools=['Bash','Read'], thinking={'type':'adaptive'}))`。
- 若仍卡死：**不要恢复进化**。转去做任务 2/3/4（这些不需要进化 LLM）。
- 若已恢复：`curl -X POST http://127.0.0.1:8000/api/control/start`，或重启 web，开始跟踪代次。

### 2. 官方 EXE 硬门槛（目标要求，未开始）
当前官方 EXE 认证是**advisory/async**（只有 `blocking` 的 `official-failed` 才追溯淘汰 parent，`web/core/official_certification.py` ~509-513 `parent_eligible`）。`national_v30` 自 2026-07-08 卡在 `official-pending`（队列 worker 从未处理）。目标要把它变成 active pool / commit-tag / 对手选择的**硬前置**。
- **EXE harness 可用**：Wine 9.0 + Xvfb + 有效 PE32+ EXE + `scripts/official_platform_acceptance.py --check-env` 返回 `{ok:true}`。
- **关键设计风险**：做成硬 pool 门槛会**同时淘汰全部 30 个 active bot**（都没过 EXE），会核爆 pool/ratings/H2H/对手。必须先分析再设计（grandfathering 现有 tagged bot，或 flag，或仅对新 bot 生效）。**编码前先充分调研**。

### 3. Phase-1 smoke（目标要求，未开始）
- review-rejection → `auto_review_repair` checkpoint 合成（target_files 来自 reviewer 主 blocker；不生成 `auto_quality_repair_gate_constants_py`；不走 quality repair 裁剪）。
- national native TCP 70 手 smoke。
- 官方 EXE 合规 smoke（无非法 check/call/allin/raise、无 60s 超时、无粘包解析失败、无 stdout 污染）。

### 4. 历史 bot 协议审计/整改
审计 active `national_v*` 的国赛 TCP 协议问题（非法 check/call、raise 格式、allin 时机、粘包拆分、postflop 首动作规则、card suit/rank 映射）。修复或隔离不合规 bot。**绝不放宽 `sever/engine/validator.py` 标准让坏 bot 通过。**

### 5. 恢复进化 + 跟踪 10 代
后端稳定后，从干净重启的第一代开始计数，目标连续 10 代无问题（观察 prepare/master/crossover/workers/quality/review/critic/precommit/commit-tag，以及跨代版本号/tag/active pool/ratings/H2H/经验池/source selection/reap/daemon 异步评估）。任一代需修复/重启则计数清零。

## 必须遵守（AGENTS.md）

- 双 checkout：`/home/zzx/project/pok`（operator）和 `.evolution_pok`（长期运行进化）。基础设施改动在 operator（或临时 worktree）完成、测试、commit、push，再 `git pull --ff-only` 同步到 `.evolution_pok`。**绝不在运行中的 `.evolution_pok` 里开发基础设施。**
- `.evolution_pok` 必须在 `main` 分支运行（runtime branch guard 在其他分支会停进化）。
- 推新 infra commit 并 pull 进 `.evolution_pok` 后，runtime guard 会停进化，且在途 generation 的 `repo_baseline.head` 不匹配 → **重启时必须废弃该 generation**（清 checkpoint + 删未完成 `bots/national_vN/`）。
- bot 只在 orchestrator `commit_bot` 流程（过 gate + `national-bot-v{N}` tag）后才算完成。不要手工完成/tag。
- 不绕过官方 EXE 硬门槛（没跑 EXE 不能标"通过"）。
- 工作原则：先理解运行态和代码边界不盲目改；先小规模验证再全量接入；修复找根因不做表层补丁。
- `dc8700e2 "Add neural national v140 candidate"` 是**另一会话的无关提交**（neural_national_lab 实验），不要 revert。
- shell 里 `rg`/`grep` 别名在某些管道下会报"互相冲突的匹配器"；用 `find`/`pgrep`/python 或绝对路径，且注意 bash cwd **不跨工具调用持久化**。
- 完整 web 测试 `cd web && python3 -m pytest tests/ -q` 很慢（>5min）。部分测试需已提交 rated bot（`active_bot_version` fixture），在 operator checkout（无 bot）会 FAIL、在 `.evolution_pok`（30 active bot）PASS——这是**预存环境问题，非回归**。

## 不要做

- 不要标记目标完成——10 代未达成。
- 不要绕过官方 EXE 硬门槛。
- 不要放宽 validator 让坏 bot 通过。
- 不要手工完成/tag bot 版本。
- 不要 revert `dc8700e2`。

## 快速状态检查（第一步）

```bash
cd /home/zzx/project/pok/.evolution_pok
git log --oneline -1                          # 应为 680b3292（或更新）
git branch --show-current                     # 应为 main
pgrep -af 'web/main.py|elo_daemon' | grep -v grep
curl --max-time 10 -s http://127.0.0.1:8000/api/control/status   # running=?
# 后端健康（简单）：
curl --max-time 15 -s -o /dev/null -w 'http=%{http_code} t=%{time_total}s\n' \
  -X POST http://127.0.0.1:15721/v1/messages -H 'Content-Type: application/json' \
  -H 'x-api-key: PROXY_MANAGED' -H 'anthropic-version: 2023-06-01" \
  -d '{"model":"sonnet","max_tokens":20,"messages":[{"role":"user","content":"Say OK"}]}'
```

接手后先做状态检查 + 读交接文档，再按优先级推进。优先做不需要进化 LLM 的任务（2/3/4），直到后端稳定再恢复进化跟踪 10 代。
