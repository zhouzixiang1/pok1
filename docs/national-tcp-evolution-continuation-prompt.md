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
   official oracle 或发布校验换取绿灯。每次 precommit 必须把 attempt-local 单调取消
   token 传入真实 native loop；取消后迟到的完整 match 不得入 gate，也不得启动下一
   sample。first-strict execution scope 冻结进 checkpoint，infra retry 复用同一 journal。
6. Orchestrator 的 provider 会话 ID 不得持久化或 `resume`；每次恢复必须从已验证
   checkpoint、重新密封的 prompt 和 typed MCP projection 启动全新 provider stream。
   旧 `orchestrator_session.json` 只有待删除的历史 sidecar 身份，零提示词权威。
   checkpoint 消失只有在当前 authorized owner tool 返回唯一 canonical result（nested
   或 flattened，但不得重复）、包含 `workflow_run_id` 和精确 transaction、ledger/
   finalize/checkpoint identities，并重证完整 Worker/strict journal 后才是终态；否则
   recovery blocked。真正无 checkpoint 时 provider 必须 end_stream，只有外层 scheduler
   能执行非 MCP `prepare_generation`。`prepare_next_gen` 只允许 exact validated
   `selected` 首次物化或 `preparing` crash recovery；未绑定 target preimage 由 system
   prepare route 直接 canonical abandon/quarantine。ToolResult 必须通过显式 id/parent id
   或唯一 pending SDK form 绑定一个 route-mutating ToolUse，未知/复用/换 owner/未结算均
   阻断。两种 timeout 都是 active leases；`timed_out` 仅从固定 disposable stages 写入并
   canonical abandon，`infra_timed_out` 只有在 full artifact、三 gate identity、quality
   fingerprint=repair baseline=live bytes 全部重证并 exact CAS 后才重跑 native precommit。
   `--one-gen` 是一个完整
   workflow/generation，不是一个 provider session，abandon 后不得准备后继代。
7. Git/证书/标签发布后必须先完成同一 publication identity 的 schema-2 durable
   handoff，顺序固定为 stability_observation、reap_signal、priority_eval、
   archive_rotation、log_cleanup、pool_reap、cycle_annotation、housekeeping。每步 plan/
   output 都是 exact-key 且 digest-bound；finalize 必须重证 stability 行、锁保护的
   signal/priority、rotation/log archive、reap tombstone、annotation、HEAD/worktree。
   pending/running/blocked handoff 是后继代调度围栏，绝不能被当作 idle 或跳过；provider
   必须 end_stream，只有 outer deterministic recovery 能调用 `run_archivist`。进程启动
   另看 owner：pending/dead owner 可由唯一 runtime 恢复，live foreign owner 阻止第二
   runtime；HTTP 只暴露 bounded owner_scope。
8. 首个严格工作流的六个 Master 槽位共享第一条 durable effect 冻结的 phase revision，
   但每个槽位拥有自己的 context binding；accepted/rejected/unaccepted effects 都要重证。
   proposal/ballot/Reviewer/Critic 的调用证据必须绑定 accepted effect 的最终 provider
   prompt、terminal output/result/usage、role projection 和 generation-bound 的单次调用
   日志。每次调用只能落在 `RESULTS_DIR/v<N>/logs/strict_invocations/<id>/`，后端只通过
   opaque id 和 results-root no-follow fd walk 读取，前端不得推导路径。Review/Critic
   prompt 只能由 durable descriptor 渲染；v143 Critic 的 strength read scope 必须为空。
   Proposal Scout 只能收到紧凑 proposal contract 和冻结语义事实，绝不能注入完整
   final-Master 教程或 final-plan output schema；bootstrap read scope 仅 target，normal
   仅 exact source、target 和指定 frozen snapshot。系统应提供从真实 policy ABI
   entrypoint 可达的 verified preferred current chain，并拒绝 ABI 不可达的 dead-helper
   chain；future edge 只能写在 proposed diff，不能伪装成 current chain。bootstrap
   projection rejection 必须把 generic error 和稳定 field-level errors 一并持久化；
   normal evolution 必须把同一确定性错误 content-bind 到唯一 local repair prompt 和
   provenance。两者都不得扩展 read scope 或两次尝试预算。被拒绝的 docs/其他越权
   读取不得返回字节或形成证据。
   generation abandon 必须同时终止 Worker journal，并为不存在的 strict child 建立
   abandoned tombstone；真实/replay dispatch 都必须复核 running。任一权限/上下文/
   prompt/log 漂移走 canonical control-plane abandon，不能消耗 LLM infrastructure retry。
   proposal packet v4 必须从两份完整 ballots 重算 unanimous-veto 集合，正常代只能引用
   validator 认可的 strength-bearing snapshot node，并向 Critic 暴露 digest-bound bounded
   projection；metadata、active_bots、manifest 或 candidate 自身都不是测量对手。measurement
   必须是六个精确字段：冻结发布对手、complete_70_hand_wld、0<数值 delta<=1、至少 30
   场完整比赛、W/L/D interval 方法、net_chip_ci 次指标。singleton v144 只能以已发布 v143
   为 target，fresh v143 只能使用 fixed blueprint/official 5+3/no-strength 形式。packet 还要
   冻结 named source symbol 的 prepared-baseline AST digest；策略质量必须重证完整 selected
   binding、选中 reachable chain 上的真实 AST delta 和候选 typed check。该结果只证明有界
   mechanism/capability，不得声称自由文本 counterfactual 已执行或 Bot 已变强；强度只能由
   后续完整 native 70 手 W/L/D 样本证明。Master binding/schema 错误必须进入下一轮实际
   rendered prompt，不能写入未消费的局部字符串。
9. UI 必须把 Critic 的 `approved` 解释为 advisory 调用完成，只用
   `advisory_approved` 显示建议方向；独立 checkpoint 只有在 schema-2、正整数 revision
   及 epoch/version/stage/run/workflow 全部与 active generation 相同时才可显示。
   recovery 不可恢复或 operator action 时后端必须隐藏 route，`/start` 在 stability reset
   前返回 409，前端同时禁用。无 checkpoint 的运行态只能显示后端完整
   `scheduler_boundary`：provider end_stream、scheduler 非 MCP prepare_generation、权威
   next_v、`source_v=null`；不得从 current_v 猜父本，checkpoint 轮询失败必须清旧值。
   checkpoint 必须 before/read/after 观测；读中消失、不可读、terminal-looking 或缺字段
   都不是 clean absence。Start 必须精确匹配 active route、post-publication identity 或
   clean scheduler 三者之一。owner reservation 前后双采样同一 fence；未获 owner 的
   lifespan 不得改 live running/UI/ShutdownManager，全局 LLM manager 也必须 exact-owner CAS。
   `--no-daemon` 且 PID 不存在是健康的 `not_applicable`，但 enabled-missing 或
   disabled-live daemon 仍必须 degraded。`daemon_pairs` 在持久配置、API、stability
   identity、进程 argv/owner、elo CLI、前端和 restart script 中必须统一为 1..8；一对是
   一场完整 70 手样本预算，只影响采样量/吞吐，绝不是强度 verdict。
10. 需要 host process owner 的 Bubblewrap 启动必须先停在一次性 `--block-fd`
    屏障，宿主精确验证唯一 owner environment 后才释放；空值只允许在有界 setup
    窗口重试，任一非空不匹配/超时/读取或释放失败都必须 terminate/reap。owner
    marker 不得进入 sandbox；无 owner 启动不得改变 argv/FD/env。
11. `worker-mcp/**` 只是供 Codex desktop/CLI 会话手工启用的外部 control-plane
    helper，不是扑克进化 Worker。web/core、Orchestrator、WorkerWorkflow、web launcher、
    rating/evolution daemon、候选流程和 `.evolution_pok` 永远不得 import/start/supervise/
    call 或把它写入 checkpoint/prompt/evidence。合入源码不得触发进化重启或评估身份
    旋转；安装依赖、专用凭据和 MCP 注册是独立的操作员动作。

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
  the terminal strict-authority abandonment repair is
  `f7670341155de91f6f376f057a6d9ce11305254d`, followed by documentation-only
  bound-manifest correction `f7b19071c66b34a84eca0bd94592209da090c4e5`.
  The Scout-context/projection implementation is
  `007020a90f9829a9e0a5aa124ff65bb590009e44`. Singleton/precommit,
  proposal-packet-v4, executable proposal evidence and daemon-pair alignment are
  `0b99c7e6d6e46783828d79b370a1d6715ebefe16`. The separately scoped Codex-only
  helper hardening is `81b75070d550e9000aced1d79f909ccf843011e2`;
  it is not an evolution-runtime commit. Merged source/docs reached
  `8d623ca74e371ac4aa986b046da16eaa43c1ef18`; the final-Master zero-tool repair
  is `f6c1c86aeffce9f98970744237a242bda161eb30`. Query Git for the exact
  branch/main HEAD and require any following documentation-only closure to be
  present before runtime start;
- delivered-base verification: Web `2717 passed, 20 skipped`, sever
  `31 passed`, combined high-risk recovery/launch/precommit `435 passed`, prompt/
  documentation `55 passed`, frontend `18/18`, ESLint, TypeScript/Vite build,
  every tracked active Python file compilation, diff check, and official doctor
  `ok=true` all passed for the delivered base. The current frozen tree through
  `0b99c7e6` is Web `2753 passed, 20 skipped`, sever `31 passed`, frontend
  `18/18`, with focused Master/snapshot/compiler/quality/singleton shards,
  ESLint, TypeScript/Vite build, every tracked active Python file compilation,
  `bash -n`, diff check and official doctor `ok=true` also passing. The isolated
  Codex helper adds `102 passed`, compileall, entrypoint checks, byte-identical
  double wheel build/install and a tracked-code no-runtime-reference boundary
  test; reproducible wheel SHA-256 is
  `aa2060e732d2d4fa99dd9e37760d9f1f9a946b0e58933b8d1868695a1d40a400`.
  The current final-Master repair is Web `2757 passed, 20 skipped`, sever
  `31 passed`, frontend `18/18`, ESLint/TypeScript/Vite build (165 modules),
  focused zero-tool/strict-journal/proposal/timeout/prompt coverage, active
  Python/shell checks, diff check and official doctor `ok=true`;
- `origin/main`: require the implementation plus following documentation
  closure to be the exact current remote HEAD, and require runtime HEAD to equal
  it before start;
- runtime HEAD: tracked `8d623ca74e371ac4aa986b046da16eaa43c1ef18`, stopped and clean with no
  evolution process, active checkpoint, or candidate after workflow-v29
  canonical abandon;
- strict epoch/checkpoint: legacy workflow-v18 was durably quarantined and
  abandoned; workflow-v19 was later canonically quarantined after contract HEAD
  drift. Workflow-v20 was canonically abandoned with receipt
  `3782a404bf8d6450ce2855f90809d07ed7552b8450c0a146eee608c995fd0c22`.
  Workflow-v21 accepted one counterfactual proposal only after its schema retry,
  then the legacy flat log's multiple evidence markers correctly failed closed;
  it was canonically abandoned with ledger head
  `0673012c2aee294f006d8b388e291d17721588901de3f8da61b67e16b33c12c6`
  and finalize receipt
  `7ddd059c78a2de449438c8147b9d312413d052ed39454b9cdef01a76a79a42ec`.
  Workflow-v22 accepted only compute-memory and mechanism proposals; its
  counterfactual slot exhausted before ballots, plan, Worker or gates. It was
  canonically abandoned with ledger head
  `406bd93ee9391bbb7a5314a5f1e20c8f86619280c98f2ee573ddc5687f159c07`,
  transaction
  `fd99b1ad95aa3b11eae7b1235600a40dcaab1cc1a6d39d83c8c0992d2179f489`
  and finalize receipt
  `3161b9ae6efb9f553a2579c2d4c747d4188e27c3c2f918494a64941dcecf14da`.
  Workflow-v23 was prepared dynamically on delivered `main`, reached
  `direction_audited` revision 5/audit attempt 1, and retained accepted
  mechanism and compute-memory proposals. Its counterfactual schema-repair
  budget exhausted before ballots, plan, Worker, or gates. The tool correctly
  classified the strict journal as terminal but the central abandon allowlist
  refused the exact `system_strict_authority_invalid:` reason, causing a
  no-progress `run_master` loop. The service was stopped, then workflow-v23 was
  canonically fenced and abandoned on its original HEAD with ledger head
  `04a5cdd934d275d738e13cdc276f8ea1119e828a6d4f855f7ad935052085959c`,
  transaction
  `a26048886df4fe3410a633c4c7c6eb05a3d2c5b29a3431b090898345688372db`
  and finalize receipt
  `06a95624eb1589e52f3c291ef0918a9b04a19ccb7d26d76cb67ecfa04718a812`.
  Workflow-v24 then exhausted compute-memory and completed exactly one canonical
  abandon with ledger receipt
  `f9eb8cf5c87c848df546ac1a0dfb1fdb14ecd54cafb6406c96c5ff75356999de`.
  Workflow-v25 exhausted mechanism and completed exactly one canonical abandon
  with receipt
  `c58fb7fec0eee9d66d4c5688cd57486e8e24a4599f6f0e6e3b0a222875988faf`.
  Workflow-v26 exhausted compute-memory and completed exactly one canonical
  abandon with receipt
  `38a7754cb2b67da865ac87646d50bf49c7c94825ac7c630c346a1da58a2c86b1`.
  Across v24-v26 the read-scope guard blocked 12 documentation reads (2/2/8),
  and no rejected bytes entered prompt projection or evidence. This proved the
  one-shot terminal abandon fix but exposed that Scouts still received the full
  final-Master tutorial and lacked exact field-level repair diagnostics.
  Workflow-v27 was then canonically abandoned on the unchanged old contract HEAD
  from `direction_audited`, revision 4, audit attempt 0. Its ledger receipt is
  `40f2fecb8ec3524bc1632d54380a030140d5842f41a166a1e03a5a35880f1f09`,
  transaction id
  `ddc338ed1f1d876112ee72c6725dae6166d522e0b83633a1e50161206d23be85`,
  and finalize receipt
  `627869541aab54b63dcdb85f839c3a31e44ba5a8330913611c571ddaff4d8706`.
  Both journals were fenced, the candidate quarantined, and the checkpoint
  cleared. Workflow-v28 then proved exactly three valid Scouts and two valid
  anonymous critics, but its final Master redundantly read a 25k-token partial
  system runtime and exhausted three identical 132-second stalls. It was
  canonically abandoned with receipt
  `7953e317aecc28ce1ef3659837fad4c02c8491615cec7bc6f10a5cd04a3fd6eb`,
  transaction
  `63ace03409fd33d351021cdb6bac693f24c5a8a896a64065fe62340de6ace462`,
  and finalize receipt
  `fdc59860bea80740dc01133bde8fcc86c8702a01220a7ef2dfbff6eec2d019d8`.
  The automatically prepared workflow-v29 was stopped before repeating that
  cost and canonically abandoned with receipt
  `0e9ea4843761e42a3ecf410aac6b4f92718e3b53c643a96812e57690ec24f1f3`,
  transaction
  `db52589abcf72d42d6b356299568cfc1fc45fa3761267a23be958e9b558176d1`,
  and finalize receipt
  `41ae0d93611eb7ef900c8d188007630bd1e021b72ec7d76f354e5310ffaa4695`.
  The next allocation after stopped-runtime sync must be fresh workflow-v30;
- last completed strict tag/certificate: none for v143+; no v143 or v144 has
  been published. The stopped runtime validates `first_strict_control_v1`
  artifact `2a0d58ed7126e46a04107903633ae7667e8196ae4d6a26b8aca60c8e18245c33`;
  its signed-ledger consumption is valid, unused and `0/1`, so no v143 formal
  bootstrap dependency is missing. Official doctor is green. The 5+3 operator
  action remains locked until the fresh post-repair workflow reaches
  `official_bootstrap_required`;
- immutable rating cycle: none for the new strict two-bot pool;
- stability observation: backend `/api/control/status`; only an unexpired
  background-verified `fresh` snapshot may expose N/10; no post-delivery
  consecutive generation has run, so the live acceptance remains `0/10`;
- legacy branch verdict: `fc7d62d30783d2ae8710dc8f331d717f3d902e36`
  is semantically superseded, history-only, and must not be cherry-picked;
- Codex helper boundary: `worker-mcp/**` is source-only and inert. It must never
  be imported, started, supervised, called, or recorded by web/core,
  Orchestrator, WorkerWorkflow, daemons, candidate generation or
  `.evolution_pok`; it is neither checkpoint/evidence authority nor an
  evaluation-identity input. Baseline `38792977` was incorporated as
  `4470f550`; overlapping real-link fix `46f79b8e` was semantically reviewed
  into `81b75070`, not blindly replayed. Manual Codex MCP install/registration
  later completed as a separate user-side action from the exact epoch-pinned
  `aa2060e7...` wheel. `bwrap`/`socat`, deep health, two cold starts, six-tool
  discovery and STDIO task `63b1af95-e391-483b-8fd0-1a91cf251e4c` are green;
  the task made zero changes. Credentials stay outside repository/config/log
  plaintext and use the user's encrypted login keyring. This remains zero
  evolution authority and caused no poker restart/identity rotation;
- current evaluation identity was explicitly rotated after runtime sync. The
  current empty instance is `771bfaeb48b64b248ce3fd3be6c4a906`.
  The prior identity is archived at
  `web/core/results/archive/evaluation_identity/20260716_141841`.
  Daemon start bound the canonical `national_native`, 70-hand, five-match,
  direct-artifact runtime profile, yielding manifest
  `f8ef8c2aa6ab28b13c9b5bcec947d4e980d1ddc98f0de1dfdbe53f469da45de1`
  while base identity is
  `0f3094ac881e0873f8776d6a12e96ea5ca74d8994a1e7bedfc26a03a85f2f996`;
  only this empty instance may receive future native samples;
- stopped-runtime sync/recovery: the clean runtime fast-forwarded only through
  `origin/main` to `f6c1c86aeffce9f98970744237a242bda161eb30`. Post-sync epoch
  projection is `fresh_bootstrap_ready`, `current_v=142`, `next_v=143`, zero
  active bots and no active generation; checkpoint recovery is
  `active=false/recoverable=true/issues=[]`; no candidate, checkpoint,
  reconciliation claim or evolution process exists. The evaluation instance,
  base identity and manifest above revalidate exactly, so no rotation occurred.
  Runtime official doctor remains `ok=true`, and the exact final-Master/
  strict-journal/proposal/prompt/abandon rerun is `232 passed`;
- known next action: after the documentation-only closure is merged and the
  stopped runtime is fast-forwarded to that exact `origin/main`, start the
  single long-running Web/Orchestrator/rating ownership and allocate fresh
  workflow-v30. Do not start it from two Codex tasks. Dynamically require final
  Master `tools=[]`, zero read dirs/files/TOOL_CALL,
  `strict-authority-v2`, one accepted 3+2 packet, and
  `pipeline.master_plan_accepted`; thinking telemetry alone is never success.
  Publish fixed v143 only after the exact
  checkpoint reaches `official_bootstrap_required` and the operator completes
  both official bootstrap and finalize. Then prove singleton v144, normal 5+3,
  the first two-Bot immutable native cycle and continued strong-Bot selection.
  Track but do not fake completion of source-specific post-selection evidence,
  confidence eligibility, generation-vs-daemon stagnation, independent holdout
  seeds, crossover behavioral diversity, sequential reaping, direction/literature
  outcome feedback and useful two-Bot Worker utilization.

## 2026-07-16 superseding runtime handoff — v143 quality liveness repair

The preceding “known next action” has been executed and superseded. Runtime
started fresh `generation:143:workflow-v30` on `3bef73c1`, completed the
three-Scout/two-critic/zero-tool-final-Master/Worker sequence, then stopped at
`workers_done` after quality surfaced a deterministic contract error. The first
native self-play quality sample was cut off at 46/70 hands by the 600 s global
timer despite zero illegal actions, zero per-action timeouts and zero process
failures. Do not call this a candidate, model, official-protocol or network
failure; it is a full-match liveness-budget defect.

The repair in the alignment worktree centralizes an effective full-70-hand
budget in `web/core/national_native.py`, derived from the exact managed local
timing envelope and the active engine's four betting streets × 100-action
safety cap. It must be the single authority for quality smoke/acceptance,
precommit (including the first-strict journal lease) and immutable rating.
Never “fix” this by shortening the 2.0 s hard / 1.8 s refinement strength path,
by accepting an incomplete match, or by merely changing one profile constant.
Result receipts must show requested/effective/floor budget; a timeout or 69/70
result remains failed and inconclusive.

The engine cap itself is fail-closed: reaching it before a street closes emits
an explicit action-limit failure and cannot silently advance the game. The same
budget snapshot must enter a first-strict execution before terminal validation,
runner sealing and SQLite journal completion; the outer idempotent completion
must receive identical bytes. Only the outer whole-match watchdog adds
`native_full_match_liveness_budget_exceeded`; connection/name handshake
timeouts remain separately fail-closed.

Also preserve the fixture correction: `raise 208` is a legal preflop candidate
choice, so a candidate test cannot force an unrelated warmup `call`. System-
owned reducer fixtures alone own the exact postflop `pass→check` and
`pass→call` assertions and must prove the inverse action invalid with the
authoritative validator. Candidate fixtures must continue to use real isolated
raw TCP, parser, canonical wire tokens and validator legality, without imposing
a strategy choice.

Current runtime is stopped through `/api/control/stop`; Web PID may remain as a
control surface, but the evolution task and rating daemon are stopped. The
durable checkpoint is still `generation:143:workflow-v30` / `workers_done` with
its inconclusive quality infrastructure receipt. Stability is reset to `0/10`
with `orchestrator_stopped`. Do not delete `.evolution_pok/bots/national_v143`,
checkpoint, evidence or journal files. After the repair's full source and real
70-hand replay evidence passes, commit/push/merge it, synchronize the stopped
runtime only via `origin/main`, run checkpoint recovery diagnostics, and use
the canonical contract-changing abandon/re-prepare path before another v143
attempt. The first strict official bootstrap/finalize remains operator-only and
must stay locked until the re-prepared checkpoint reaches its exact stage.

## 2026-07-16 superseding source handoff — immutable timing-plan and bounded-progress repair

The preceding 400-request-envelope proposal is **not** an accepted repair. An
independent active-rule review proved that the generic `100` request/street
engine guard would create about a 17.5-hour envelope and conflict with the
orchestrator's 90-minute provider bound. The accepted source direction is a
system-owned `NativeMatchTimingPlan`: fixed local-strength timing (`0` send
delay, `2.0 s` hard decision, `1.8 s` refinement, `0.2 s` baseline), integer
snapshot/digest, and a tight **34 request/hand** upper bound derived from the
active 20,000-chip validator (50/100 blinds, raise-to/doubling, four streets).
The generic 100-request street guard remains a separate typed fail-closed
engine safety abort.

The plan is the sole timing authority for child launch, connect/name/action
timeouts, whole-match watchdog, first-strict ticket/match identity/lease,
runner seal, quality acceptance, frozen precommit plan/sample rows, rating
replay admission, immutable replay analysis, evidence snapshot/history
injection, and final commit reproof. Ambient `POK_NATIVE_*` timing inheritance,
per-seat timing overrides and raw liveness-budget dictionaries are rejected.
Every complete result carries the full plan + digest + human-readable projection
and only admits if `native_match_timeout_phase is None` and
`native_terminal_abort is None`.

An engine event may publish a sanitized runtime-only heartbeat for the exact
checkpoint/PID/start-token/route and plan/match digest. It contains no cards,
wire actions, model text, checkpoint mutation, evidence or rating data. At the
ordinary provider-cycle boundary the orchestrator may grant at most **one**
extension, capped by the frozen match deadline plus cleanup and a hard absolute
cap. Missing, stale, wrong-route, old-stream, malformed or repeatedly rewritten
heartbeats never extend the cycle. This is not a blanket `CYCLE_TIMEOUT`
increase and cannot be initiated by any LLM role.

Current code remains unmerged until focused/full Web, `sever`, frontend and a
fresh real 70-hand v143 acceptance replay pass. Before any runtime recovery,
read the stopped checkout's current checkpoint and heartbeat afresh; do not
trust this historical handoff to claim `workers_done`, and never delete/reset a
checkpoint manually. If the frozen evaluation contract or timing digest differs
after merge, use canonical controlled abandon/re-prepare, reset stability to
`0/10`, and begin observation from the first recovered generation.

## 2026-07-17 current source handoff — schema-5 timing, bounded completion and replay causality

This section supersedes the still-historical wording above wherever it implies
that the initial frozen plan alone was sufficient. A second source audit found
three further causal gaps and repaired them in the same unmerged alignment
worktree:

1. A first-strict journal ticket is claimed before its shared native capacity
   slot is acquired. `NativeMatchTimingPlan` schema 5 therefore binds one exact
   5,960-second operation and lease: 300-second capacity queue, two enforced
   30-second read-only artifact preparations, 120-second startup, 5,415-second
   engine envelope, 35-second cleanup and 30-second post-execution durable
   completion. Fixed phase ceilings are `launching=480`,
   `engine_running=5,415` and `finalizing=65` seconds. Launch progress begins
   before the queue, uses a runtime-only nonce-bound identity that never enters
   replay/evidence, and emits every 30 seconds without rolling the original
   start/deadline. An over-ceiling queue, preparation, startup or completion
   fails closed and never gains an arbitrary lease extension.
2. A runtime heartbeat is no longer trusted merely because its plan field is
   hex-shaped. Quality first persists its exact strict acceptance plan in the
   active `workers_done` checkpoint; precommit reads its already-frozen plan
   from the `critic_checked` checkpoint. Runtime-heartbeat schema 4 and nested
   native-progress schema 4 rederive and compare that plan, effective timeout,
   route, PID/start token, provider nonce and runtime-only prelaunch identity.
   Only launching may carry `hand=null`; all engine progress requires hand
   1..70, while hand 70 transitions through `finalizing`. Authority reproof is
   repeated every 5 seconds against the same checkpoint/plan/match/route/PID/
   provider identity. Only the `runner_returned` handoff may authorize
   completion, after applicable result annotation, exact-byte rehash, terminal
   validation and durable journal seal. `runner_raised`/`runner_cancelled` also
   publish from outer `finally` after resource release for cleanup, but the
   consumer rejects them. A rejected clear immediately revokes its provider
   nonce and is retried. A
   settled match therefore
   cannot lend its last heartbeat to a later non-engine stall. The outer stream
   still grants at most one exact 5,960-second-cap extension.
3. An admitted history row is now only a projection of exact raw replay bytes.
   The Elo producer writes `replay_sha256`; every H2H/chip/history/snapshot
   consumer safely reopens the named non-symlink replay, compares SHA-256,
   runs `validate_native_replay`, and requires header/outcome/plan equality.
   Missing, replaced or header-drifted raw bytes have zero strength authority.
   Replay cleanup keeps all rows cited by live history or retained immutable
   cycles, even if that exceeds the operational file-count preference.

The one-hand liveness projection now reports the same real 34-action/4-street
derivation used to calculate its timeout; it no longer reports a contradictory
zero action cap. `evaluation_bundle` schema 3, evidence-snapshot schema 9,
first-strict execution definition 3, timing-plan schema 5, runtime-heartbeat
schema 4 and native-progress schema 4 deliberately make old byte formats
ineligible. This is a contract change: it must be handled by
the canonical controlled abandon/re-prepare path after merge, never by manual
state deletion.

The two timing P1s found by that audit are now repaired and verified in focused
source tests. `asyncio.start_server()` bind/create is inside the same absolute
120-second startup watchdog, with typed timeout and safe cleanup before any
client launch (`17 passed` in the focused native suite). The first-strict
flock/SQLite completion path consumes only the remaining absolute 30-second
budget and leaves no detached writer: timeout preserves the running effect,
creates no inbox/event or false completed seal, and the same ticket recovers.
Its workflow/native/hidden recovery aggregate is `60 passed`. These repairs are
still unmerged. Later runtime/precompute/evaluation-contract edits invalidated
the earlier complete-suite result, but the replacement final Web run is green
at `2901 passed, 20 skipped, 1 warning` in 156.14 seconds. They create no
runtime, v143, certificate, rating or N/10 evidence.

The superseding completion bridge is a schema-1 process-local one-shot terminal
handoff, not a final-live-only reproof. Lock order is dispatch then heartbeat;
the receipt binds exact checkpoint, owner, nonce, match, timing, operation,
event and terminal outcome. Live converts to a receipt and then unlinks; unlink
failure rolls the receipt back, the next live match clears old receipt state,
consumption revokes the nonce, and expiry is fixed at no more than 30 seconds
without rolling. The runner publishes from its outer return/raise/cancel
boundary only after resource release. Only `runner_returned` may authorize a
provider result, and that outcome follows the applicable annotation, rehash,
terminal validation and durable seal. `runner_raised` and `runner_cancelled`
also publish from outer `finally` for cleanup, but the consumer rejects them.
Orchestrator periodic and done paths try current live authority first and
otherwise the receipt; current checkpoint proof allows only the same workflow/
version, non-regressing revision and explicit owner stage. Focused results are
terminal handoff `11 passed`, timeout file `76 passed`, native `17 passed`,
nonworker `13 passed`, cleanup `9 passed`, recovery `7 passed`, and services
`32 passed`.

The first-strict control journal also no longer relies solely on a
cross-thread executor callback. `_await_first_strict_control_completion`
creates the `asyncio.to_thread(complete_control_execution)` task and polls it
every 50 ms with `asyncio.wait`, covering the observed remote app-server case
where the concurrent future completed but its wakeup was lost. Cancellation
drains the durable writer before re-raising; it never detaches or cancels that
writer, and only a successful durable `COMMIT` authorizes completion.

The historical complete Web result was `2853 passed, 20 skipped, 1 warning` in
145.30 seconds, covering the terminal-handoff conversion at that source state.
It is now superseded and does not close the current full-suite gate. Current
independent non-Web evidence is:

```text
python -m pytest sever/tests -q
# 33 passed
cd web/frontend && npm test && npm run lint
# 18 tests passed; lint passed
cd web/frontend && npm run build
# TypeScript/Vite production build passed (165 modules)
python -m py_compile <all touched active Python modules>
git diff --check
# passed
```

These source results do **not** certify v143 or resume the runtime. Required
next actions remain: review/commit/push/merge the exact worktree; stop-state
sync `.evolution_pok` only through `origin/main`; run checkpoint recovery;
because evaluation/timing/evidence contracts changed, canonically
abandon/re-prepare; then obtain a fresh 70/70 v143 quality/precommit/official
bootstrap trail before any v144, rating-cycle or N/10 claim.

## 2026-07-17 definitive Bot numbering and LLL clean-room authority

There is no pending physical-v1 or new-namespace migration. Do not create one,
wait for one, archive current strict identities for one, or treat it as a
runtime blocker. The user definitively narrowed `Bot 1..N` to a Web presentation
sequence. Preserve
all real identities: `national_tcp_policy_v1`, `national_v143+`, paired
annotated completion/high-water tags, manifests, receipts, certificates,
rating keys, evidence digests and checkpoint versions.

On the strict published-Bot page, assign display ordinals from the exact
current published pool in ascending physical version order and show the real
annotated completion tag next to every ordinal. The ordinal must remain stable
when the user changes strength/H2H/version sorting, and must never enter API or
SSE DTOs, rating/evidence/selection rows, checkpoints, prompts, certificates or
publication logic. A candidate or untracked directory has neither a published
Bot ordinal nor a completion tag.

The permitted clean-room strategy reference is exactly
`/home/zzx/project/pok/lll/lll/bot/国赛平台代码.py`, SHA-256
`a7aef0b3b8b1a0096164631e87f9f1dd0c57b1a95c2738762c9f6301bc434dfb`.
It is not a strict candidate or production dependency. Manifest schema 3 binds
it as semantic-reference-only with zero source bytes, strength, history and
runtime import. The current source under review is runtime 10, evaluation
contract 32, precompute schema 4 / generator `national-precompute-v3`, and
runtime-probe schema/orchestrator/worker/scenario `15/15/16/7`. TCP parsing,
legality, fallback, tracker, socket send, timing and evidence remain system
owned. LLL code, local harness, logs, ratings, H2H, THP, prompts and history
must never be copied, imported or injected.

The latest strength audit found and repaired five P1s:

1. The prior 169 preflop values were a deterministic hand-written class-order
   heuristic, not calibrated heads-up equity. Schema 4 now carries a generated
   169-float table. Generator `national-precompute-v3` uses the official
   evaluator and a fixed seed for 65,536 uniformly sampled opponent-and-board
   completions per canonical class; generator/environment/evaluator/deck/random
   hashes are pinned. Regression anchors include `A2o > K2o > 76o`, suited over
   offsuit and ordered pairs. Generator script SHA-256 is
   `5aa6808974f9af67ac7bb5189c431791d9aed9e791869f9428b1ab8e04cf62d3`.
2. Preflop decisions now distinguish the four real actionable spots:
   `sb_open`, `bb_vs_limp`, `bb_vs_raise` and `sb_vs_reraise`. Tested
   raise-to-total bands are `225–300`, `325–450`, `650–900` and `900–1200`.
   A strong shallow-stack hand emits typed `allin` when the desired target is
   exactly the hero total even if `legal.max_raise_to` is one chip lower. The
   final policy also covers ultrashort **allin-only** contexts where `allin` is
   legal but `raise` is absent and both raise bounds are null: AA jams in all
   four spots while the weak control never leaks a jam. Postflop is neutral to
   these spot controls.
3. Runtime 10 produces schema-1 `hand.match_control`, binding initial chips,
   blinds, current position/exposure, future forced blinds, forced-fold loss
   bound and hero net. The policy folds even AA and skips refinement only when
   the strict proof `hero_net_earned > forced_fold_loss_bound` holds. Equality,
   missing or malformed evidence is neutral.
4. Runtime produces consistent `hero_in_position_postflop` and
   `acts_first_postflop`. Position/EQR changes only marginal flop/turn calls
   that leave future-street equity realization. It is neutral on river and when
   `betting.call_closes_allin_runout=true`; action text such as `allin` is not
   authority, and missing/inconsistent facts are neutral.
5. Postflop opponent pressure previously reused preflop class ordering and
   could perversely improve hero equity after a raise. The policy now weights
   opponent holdings on the current public board. A raise-conditioned range
   cannot improve hero equity, and flop/turn weighting never observes sampled
   future runout cards. Malformed board evidence is neutral; preflop still uses
   the calibrated 169-class table.

The system precompute generator/content/environment/hash contract, runtime-10
exact source bytes/manifest/control identity, the five positive/negative
strength-control families, runtime-probe consumer reachability and five-role
prompt contract are dynamic gates. Master, Worker, Reviewer, Critic and
Orchestrator all explicitly require calibrated 169-class equity, spot-specific
raise-to-total/exact typed all-in, the mathematical match lock, position only
for nonclosing future-street calls and current-board opponent weighting.
Keyword search or a stored `passed` flag is insufficient.

The earlier causal-wire safeguards remain part of that gate: normalized
positive/matched and positive/mixed hashes, raw `positive/negative/mixed`
values, exact delimiter-free wire, bounded same-line `raise N`/`check` mixing
and profile-specific negative-control kinds are recomputed. `allin` cannot
stand in for the aggressive mixing control, and a fixed-100% policy receives no
dynamic line credit. `MAX_EQUITY_SAMPLES=32768` remains a finite postflop
rollout cap; bounded-weight worst-case effective sample size is about 16,614
and standard error is below 0.4 percentage point, while cap exhaustion does not
assert convergence. The candidate runtime probe now enforces the match-control
consumer on actual final wire: a strict proved lead must fold, while equality
and malformed proof must not fold. The current whole runtime-probe shard is
`18 passed`; the baseline no longer carries the prior consumer
`candidate_contract` failure.

The final full run also caught the generated system runtime at 2519 lines,
above the unchanged 2500-line publication hard cap. The template now collapses
only redundant triple source separators, materializing to 2493 lines. An
independent AST comparison is identical, the crossover regression passes, and
the hard cap remains fail closed.

Current unmerged source identities are:

- policy `f7c6a14a0b6fdceb6f47016ba9f8048d3ce82d4baa9dfa1b88c3a74e2b24f956`;
- prepared artifact `0ad1dd758ebc0b62f86f19bdc645abaeb5b7d48fee7513aa8a5c0c65a2721a17`;
- output artifact `db439a8b92e737663951814d918ab16dfabef454c5559f87fee60ca76061d327`;
- first-control artifact `b37cd019fe6b635a119950adb5f7ecf10ddceeafacfbed6b4c3a0955064516e2`;
- system national bot `0115c5844961011d920d012edbba30eb23171de0f5649f5b46e75a0e6bd94bef`;
- system precompute `8adeab7e8122465e1a76231a32fa34d1c08c30f77e70ef978bb8093920f00627`.

These are review inputs, not a published Bot, certificate or strength sample.
Focused positive/negative regressions exist for all five repairs and the final
focused shard is `148 passed`. The prior `2853 passed` result is historical;
the current source identities passed the complete Web suite at `2901 passed,
20 skipped, 1 warning` in 156.14 seconds. Merge remains unclaimed.

## Codex-only Worker MCP boundary and included three-commit series

`worker-mcp/**` is an inert external control-plane helper for Codex desktop/CLI
sessions. It is not an evolution Worker or poker component: `web/core`, the
Orchestrator, WorkerWorkflow, launchers, daemons, `.evolution_pok`, candidate
generation, checkpoints, evidence and ratings must never import, start,
supervise or call it. Source merge and operator installation/restart remain
separate actions and supply zero poker acceptance credit.

The reviewed candidate series is included in this exact original order:

1. `4a458dc8fdf65a6105cb3d06f435a76a05576a1e` — fixes the root cause that
   per-session STDIO processes contend for the singleton TaskService state lock
   by providing one shared authenticated loopback Streamable HTTP daemon for
   multi-Codex sessions;
2. `7bd7c78ce72924c4899fd5403c188c14ea98deec` — credential separation and
   validate/pre-bind hardening, but with the escaped-secret gap fixed by its
   child;
3. `c7a254ce14863926c5da31a9387288170d7fb05d` (parent exactly
   `7bd7c78ce72924c4899fd5403c188c14ea98deec`) — recursively scans raw strings
   before task/audit serialization and covers escaped/nested credentials.

Independent review found that `7bd7c78c` compared the raw access secret with
`model_dump_json()` output, allowing JSON escaping to hide the same secret.
Child `c7a254ce` closes that P1 and the complete Worker MCP suite reports
`118 passed`. The original-to-branch mapping is `4a458dc8` → `f55a9d13`,
`7bd7c78c` → `db8cb175`, and `c7a254ce` → `89b71101`; no source subset was
used. No wheel installation, operator-config mutation or service restart was
part of source inclusion. Installed MCP health remains separate from source
review and supplies no poker acceptance evidence.

Next work, in order:

1. Push the reviewed commit series and fast-forward `origin/main`.
2. Stop-state synchronize `.evolution_pok`, run recovery diagnostics, and use
   controlled abandon/re-prepare if the evaluation contract changed.
3. Re-prove the provenance-bound clean-room strict-v1 blueprint and use it to
   prepare strict v143; do not import LLL source/history or change the pinned
   source/policy/artifact identities without a new reviewed provenance record.
4. Complete Master → Worker → quality → review → advisory Critic → native TCP
   precommit → operator first-strict bootstrap → commit → `.completed` → signed
   certificate → paired annotated tags. It then appears as Web `Bot 1` with
   real tag `national-bot-v143`.
5. Publish v144 through normal 5+3×70 official-full-v5, establish the immutable
   two-Bot native rating cycle, and only then begin the resettable 10-generation
   stability observation from `0/10`.
