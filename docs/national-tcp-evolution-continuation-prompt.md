# National TCP Evolution Continuation Prompt

> Operator handoff prompt only. This document has zero strategy, strength,
> rating, certification, lesson, or experience authority. It must never be
> injected into Master, Worker, Reviewer, Critic, candidate policy context, or
> an evaluation snapshot.

将下面整段交给后续执行代理。代理必须以仓库内当前 `AGENTS.md`、当前
`origin/main`、严格 epoch 投影和运行 checkout 的实时状态为准，不得把本文中的
版本示例当作权威状态。

```text
你接手 /home/zzx/project/pok 的 national_tcp_policy_v1 长期稳定交付任务。

先完整阅读 /home/zzx/project/pok/AGENTS.md、
docs/national-tcp-evolution-alignment-matrix.md、
docs/evolution-continuous-delivery-runbook.md 和
docs/evolution-system-delivery-ledger.md。根目录 archive/ 是 legacy-untrusted：
不得读取、扫描、导入、执行、复制、总结、评级或作为任何提示词/历史证据来源。

总目标：保持国赛原生 raw TCP 单一架构，让进化系统持续产生可发布的强 Bot；
代码、国赛协议、质量门、提示词、历史注入、证据冻结、前后端投影、发布事务、
评分 daemon 和恢复逻辑必须使用同一权威合同。完成基础设施测试、合并和 runtime
同步后，发布首个严格 Bot、形成至少两个 Bot 的 native 70-hand 强度池，并在最后
一次修复/重启之后连续完成 10 代。任何基础设施/配置修复、代次废弃、进程重启、
评估合同漂移、版本断档或不完整发布都把验收计数归零。

不可改变的边界：
1. 官方 Windows EXE 只拥有正式协议合规权；normal 认证严格为 5 轮 70 手自博弈
   加 3 轮 70 手合规对手博弈。v143 空池 bootstrap 只能由操作员执行一次性
   first_strict_control_v1，零强度、不可自动回退；证书成功后仍须第二个操作员
   `finalize-first-strict` 边界，LLM/HTTP 不得调用或模拟。
2. 官方 EXE 和 Web Arena 的胜负、筹码、THP 均不得进入 Glicko/H2H/selection。
   一个强度样本只能是一场完整 70 手本地 native raw TCP 比赛；最终净筹码符号
   决定 W/L/D，筹码幅度仅作同主指标后的次级排序。
3. 每个候选严格为五文件 ABI：系统拥有 national_bot.py、precompute.py，候选只
   拥有 policy.py，另有 national_runtime_manifest.json 和 policy_epoch_receipt.json。
   候选不得拥有 socket/TCP、FS、网络、subprocess、动态代码、外部 import-time I/O、
   每决策全历史扫描或未批准资产。
4. wire 动作为无分隔符 raw string，绝不追加换行；recv 边界不是消息边界。
   raise X 是本街总投入，精确 2x 边界合法；postflop 首动作 call 非法，已有动作后
   check 非法；called all-in 后不得再次行动；唯一可证明的省略 closer 必须先计入
   contribution/stack/pot 再清街；oppo_hands 只在 showdown；自然第 70 手由 69 对
   wire settlement 加严格 THP state 69/footer 证明，不得伪造第 70 对 earnChips。
5. Critic 仅 advisory；本地 native TCP precommit 是策略硬门。任何 required gate 的
   skipped/pending/inconclusive/env-disable 都不是 pass。不得放宽 validator、探针、
   official oracle 或发布校验换取绿灯。
6. Orchestrator 的 provider 会话 ID 不得持久化或 `resume`；每次恢复必须从已验证
   checkpoint、重新密封的 prompt 和 typed MCP projection 启动全新 provider stream。
   旧 `orchestrator_session.json` 只有待删除的历史 sidecar 身份，零提示词权威。
7. Git/证书/标签发布后必须先完成同一 publication identity 的 schema-2 durable
   handoff，顺序固定为 stability_observation、reap_signal、priority_eval、
   archive_rotation、log_cleanup、pool_reap、cycle_annotation、housekeeping。每步 plan/
   output 都是 exact-key 且 digest-bound；finalize 必须重证 stability 行、锁保护的
   signal/priority、rotation/log archive、reap tombstone、annotation、HEAD/worktree。
   pending/running/blocked handoff 是启动围栏，绝不能被当作 idle 或跳过。
8. 首个严格工作流的六个 Master 槽位共享第一条 durable effect 冻结的 phase revision，
   但每个槽位拥有自己的 context binding；accepted/rejected/unaccepted effects 都要重证。
   proposal/ballot/Reviewer/Critic 的调用证据必须绑定 accepted effect 的最终 provider
   prompt、terminal output/result/usage、role projection 和原始非空普通日志。Review/Critic
   prompt 只能由 durable descriptor 渲染；v143 Critic 的 strength read scope 必须为空。
   任一权限/上下文/prompt/log 漂移走 canonical control-plane abandon，不能消耗 LLM
   infrastructure retry。
9. UI 必须把 Critic 的 `approved` 解释为 advisory 调用完成，只用
   `advisory_approved` 显示建议方向；独立 checkpoint 只有在 schema-2、正整数 revision
   及 epoch/version/stage/run/workflow 全部与 active generation 相同时才可显示。
   `--no-daemon` 且 PID 不存在是健康的 `not_applicable`，但 enabled-missing 或
   disabled-live daemon 仍必须 degraded。
10. 需要 host process owner 的 Bubblewrap 启动必须先停在一次性 `--block-fd`
    屏障，宿主精确验证唯一 owner environment 后才释放；空值只允许在有界 setup
    窗口重试，任一非空不匹配/超时/读取或释放失败都必须 terminate/reap。owner
    marker 不得进入 sandbox；无 owner 启动不得改变 argv/FD/env。

工作方式：
- /home/zzx/project/pok 是 operator checkout；保留其 dirty 用户文件。
- /home/zzx/project/pok/.evolution_pok 是 autonomous runtime；候选/checkpoint/rating/
  结果只属于这里。基础设施只能经 origin/main 同步，禁止手工复制。
- Arena worktree 必须保留。只在干净临时 codex/ worktree 开发；先 fetch tags。
- 先用严格 epoch/checkpoint API 读取当前目标、stage、workflow、published tags、
  certificates、evaluation cycles 和 N/10 投影，绝不从最高目录名推断完成度。
- `scripts/pok_restart_observe.sh` 只允许重启与观察，不得清 checkpoint、移动候选或
  管理 provider 会话；所有废弃/隔离都必须走 canonical transaction。
- 变更必须更新 alignment matrix、delivery ledger 和本 continuation prompt 的实时
  handoff 段；这些文件仍不得进入策略提示词。

执行顺序：
1. 验证当前分支/HEAD/dirty 状态和进程；安全清理仅限已合并且干净的工作分支，
   不用 reset、clean、force，不碰 Arena/dirty/unmerged 数据。
2. 对每个协议行核对 authority→producer→consumer→dynamic gate→rendered prompt→
   frozen evidence→backend typed projection→frontend，并补正反测试和 fail-closed 语义；
   UI 文案、按钮可用性、API state/reason、实际工具路由必须逐项相同。
3. 运行 focused tests，然后运行 sever 全套、web 全套、frontend test/lint/build、
   active Python 精确文件 `py_compile`（排除 archive 与 runtime results，不做
   `compileall` 扫描）、git diff --check。
   host 缺少 Bubblewrap/NETLINK/Wine 时只能报告 probe_infra，不能算 Bot 失败或通过。
4. commit/push task branch，在干净 integration worktree 合并并 push origin/main。
5. 停止点 fast-forward .evolution_pok；合同变化时走 canonical abandon/re-prepare，
   不手删 checkpoint。验证复杂 Claude SDK 多工具调用，再启动长期 web/orchestrator/
   rating daemon。
6. v143 先核对 jobs API 的 digest-bound `ready_to_finalize`，再执行 acknowledged
   operator finalize；随后完成 v144 和首个 immutable rating cycle。核对
   certificate/tag/tree/source/cutoff/replay hash 和 official 零强度。
7. 每次发布后先从 active handoff pointer 恢复/完成八步 journal。确认 high-level
   rotation 已冻结全部 append-only source、strict logs 仅非破坏归档、pool schema-2
   snapshot/目标序列未重算、daemon signal 的 producer/consumer 共享 sidecar 锁；
   前后端 epoch/handoff/stability identity 一致后才能准备下一代。
8. 连续运行直到后端 operator-only stability_observation 的 unexpired
   `verification.state=fresh` 达到 10/10。每代核对进程
   boot identity、合同 hash、workflow journal、quality/precommit/official receipts、
   pushed tag/main、native samples、rating/H2H、daemon heartbeat、前后端状态。
9. 若任何修复或重启发生，先记录根因和证据，修复/测试/重新交付，然后确认计数
   已归零并重新开始，不能保留旧进度。
10. 达标后做最终协议/证据/前后端/运行审计；确认 origin/main 和 runtime 一致，
   再移除干净任务 worktree和已合并本地分支。保留 Arena、runtime 状态和用户工作。

不要只给审计报告。发现范围内缺口就实现、验证、记录并继续推进；只有需要新的
外部权限、操作员一次性官方动作或确实不可用的 host 能力时才停下并给出精确命令、
当前证据和恢复点。
```

## Live handoff fields

每次交付收口时更新以下字段；它们只帮助操作员定位，不替代实时权威查询。

- infrastructure branch/commit: `codex/national-protocol-evolution-alignment`;
  merged stream-ownership parent is
  `0a26795aa71fa92049acebef94968a0c9f7553d7`; this document travels in the
  strict-authority recovery repair, so query the branch for its final
  commit rather than guessing a SHA;
- final frozen-tree verification after the strict-authority recovery repair: Web
  `2554 passed, 20 skipped`; sever
  `31 passed`; frontend `15 passed` plus lint/build; active-source `py_compile`
  and `git diff --check` passed. The repair has `242 passed` across its focused
  authority/Master/role shards and `94 passed` across the final backend/frontend
  route and presentation-contract shard;
- `origin/main`: before this repair is integrated it is
  `0a26795aa71fa92049acebef94968a0c9f7553d7`; runtime resume requires the
  commit carrying this document to be present in `origin/main`;
- runtime HEAD: stopped at the same stream-ownership parent; re-query after
  Git-only synchronization;
- strict epoch/checkpoint: legacy workflow-v18 was durably quarantined and
  abandoned; workflow-v19 was later canonically quarantined after contract HEAD
  drift. The live checkpoint is v143 `direction_audited`, workflow-v20,
  revision 6, with `master_plan=null` and a retryable-but-misclassified
  `master_llm` overlay. Its strict journal has exactly two accepted proposals
  at authority revision 4 and one missing-slot schema rejection; no complete
  packet, ballot result, selected mechanism or Worker plan was accepted. After
  the strict-authority recovery repair reaches `origin/main`, canonically abandon
  workflow-v20 and prepare a fresh v143 workflow; do not retry the stale overlay;
- last completed strict tag/certificate: none for v143+; no v143 or v144 has
  been published;
- immutable rating cycle: none for the new strict two-bot pool;
- stability observation: backend `/api/control/status`; only an unexpired
  background-verified `fresh` snapshot may expose N/10; no post-delivery
  consecutive generation has run, so the live acceptance remains `0/10`;
- legacy branch verdict: `fc7d62d30783d2ae8710dc8f331d717f3d902e36`
  is semantically superseded, history-only, and must not be cherry-picked;
- known operator action: none may be inferred from this document.
