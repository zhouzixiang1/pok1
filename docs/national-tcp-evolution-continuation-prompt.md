# National TCP Evolution Continuation Prompt

> **Branch note (`tencent-cloud-runtime`).** Version references (v142/v143/v156,
> `national_v*`) below describe the `main` branch. The cloud runtime restarts
> from `national_cloud_v1` (high-water 0); substitute accordingly.

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
   check 非法；called all-in 后不得再次行动，2021 EXE 可省略尚未发送的 board
   并直接进入 settlement/oppo_hands，系统不得伪造缺牌；唯一可证明的省略 closer
   必须先计入 contribution/stack/pot 再清街；oppo_hands 只在 terminal showdown；
   自然第 70 手由 69 对
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
   场完整比赛、精确 `wilson_wld_interval`、net_chip_ci 次指标。任一 singleton successor
   只能以已发布 v143 为 target，并重开实时 allocation/abandon authority；fresh v143 只能
   使用 fixed blueprint/official 5+3/no-strength 形式。packet 还要
   冻结 named source symbol 的 prepared-baseline AST digest；策略质量必须重证完整 selected
   binding、选中 reachable chain 上的真实 AST delta 和候选 typed check。该结果只证明有界
   mechanism/capability，不得声称自由文本 counterfactual 已执行或 Bot 已变强；强度只能由
   后续完整 native 70 手 W/L/D 样本证明。Master binding/schema 错误必须进入下一轮实际
   rendered prompt，不能写入未消费的局部字符串。只有已完成但被确定性 projection 判为
   无效的输出可消耗一次 schema/distinctness retry；SDK/transport failure 必须进入
   `MasterInfrastructureError`，已确认 parent cancellation 是控制停机，二者都不得伪装为
   schema retry。连续循环对同一 `(next_v, source_v)` 的第三次已验证 canonical abandon
   必须停机并要求显式审查/重启，绝不准备第四个 workflow。
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
   lifespan 只停止它已注册的 owner，但自动 launch 和成功的后续 `/api/control/start`
   都必须注册；关闭时通过当前 `AppState` manager 而不是启动时缓存的 manager 覆盖这个
   后续 owner。无权威 task snapshot 只能发 `task_authority_lost`，HTTP null/畸形或
   SSE malformed 的 status/task_owner 都清 transient text，绝不伪造 `R+1`；保留最后
   验证的 fence 后，后续完全相同的有效 R 投影可恢复，冲突的同 R 必须等真正更高 R。
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
- 行为、ABI、协议、门禁、提示词、数据、生命周期或 test-harness 的变更，必须在
  同一批次同步更新 focused/full 测试流程、fixture、正反 regression anchor 与操作命令。
  任何 skip、弱化或重分类都必须有记录在案的 fail-closed 替代门，不能把未测路径
  写成通过。

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
5. 若 runtime checkpoint-free 且 active-stage contract 未变化，才可在停止点仅经
   `origin/main` fast-forward `.evolution_pok` 后诊断。若 checkpoint 存在且合同变化，
   必须保持 runtime 在记录的旧 HEAD，先 exact-CAS canonical abandon，验证 finalized
   handoff、quarantine 与 cleared checkpoint，再 fast-forward 并运行诊断；绝不手删
   checkpoint/candidate。随后才验证复杂 Claude SDK 多工具调用并启动长期
   web/orchestrator/rating daemon。
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

The later recovery audit is also included: detached commit
`1b57228e3aa9e53fb4ecf6d33d3ea4be747bfeb6` replays as
`2ade21159d12b551dcab46f0ee75b309125d7a2c`. It closes
the P1 where legacy durable accepted/queued/retry rows could enter `_recover()`
without current raw-secret, scope, repository-allowlist and canonical-base
validation. Invalid legacy rows now become `needs_review` before enqueue, with
no executor or new snapshot. Worker MCP focused/full evidence is `70/126`
passed. This source change neither installs nor restarts the Codex helper and
does not touch poker runtime state.

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

Pre-merge runtime baseline on 2026-07-17: the autonomous checkout is still at
`3bef73c1`, while the clean source freeze is
`ceb669f63f53a0ae85445b77cccd3fe94f0e0d11`. Web PID `54540` runs from
`.evolution_pok`, but control health is `overall=stopped`; the Orchestrator and
rating daemon are absent. The checkpoint is absent, recovery is
`active=false/recoverable=true/issues=[]`, epoch state is
`fresh_bootstrap_ready`, active Bots are empty, the scheduler owns fresh v143,
and stability is `0/10`. The last workflow-v30 was governed-abandoned for
`infrastructure_exhausted:national_acceptance_harness` after incomplete
70-hand coverage and has no publication or strength authority. Host-level
official doctor is green on managed-executor profile v7. Do not stop PID 54540,
sync, restart, or prepare v143 until the source is fast-forwarded through
`origin/main`; if any checkpoint appears meanwhile, re-run recovery diagnostics
and invalidate this clean-boundary assumption.

Post-sync repair note: the first reconciliation dry-run on `47fe89c8` failed
closed while revalidating the already-completed receipt because
`build_fresh_bootstrap_receipt` imported the unused full `national_native`
runtime. The operator CLI exposes only `web/core` on `sys.path`, so that import
transitively failed at `sever.*`. The repair removes only that unused import;
receipt fields/digests remain unchanged. Core-only isolated validation and a
completed-receipt replay that forbids `national_native` imports are regression
bound in a `128 passed` focused aggregate; the complete Web suite is `2902
passed, 20 skipped, 1 warning` in 162.53 seconds. The fixed source validates
the live 13-row ledger plus receipt digest
`af23c438202943b8c95b1a405a9c1f7f8ddb4a56e3802d4399b2e8156f2fd2f3`.
Keep all poker processes stopped until the repair reaches `origin/main`, the
runtime fast-forwards again, and the real reconciliation CLI dry-run succeeds.

## Latest superseding handoff: workflow-v31 and evaluation contract 33

Treat all earlier “next run is workflow-v31” wording as historical. The
reconciliation import repair is already in `origin/main=1ceeffa9`, the runtime
was stopped/synchronized, and the actual reconciliation CLI dry-run passed.
The subsequent fresh v143 `generation:143:workflow-v31` completed three Scouts,
two critics and two final-Master provider calls, then canonically abandoned with
receipt `f6724ecaf1a92e159fca0e89483a9142b6aa07c2e5668f7bc6adb32b350e9551`.
No checkpoint/candidate/Bot/tag/certificate/rating remains; stability is
`0/10`.

The failure was a cross-layer compiler contract, not proof that the configured
model or proposed poker mechanism is weak. Proposal `b6b193a1c026966e` was
terminal-response-specific but froze generic falsifier
`incremental_opponent_model`; the honest final task declared primary
`terminal_response`, whose typed check is `terminal_response_adaptation`.
Separately, an 8,181-character selected block left 3,817 characters of the old
12,000 cap before the later runtime-contract block, while provider prompts were
4,705/4,417 characters and the retry error exposed no arithmetic.

Evaluation contract 33 fixes this with proposal schema v3, packet schema v5 and
strict-authority v3. Scout admission binds falsifier→primary→exact mechanism
target→checks, requires that exact target in all executable proposal fields and
rejects foreign closed targets/aliases before voting. Old v2/v4 results cannot
replay. Final Master receives per-proposal Unicode character budgets, including
a bounded 2,048-character runtime-contract reserve; malformed/empty provider
prompts, wrong primary/check and overflow enter deterministic fail-closed retry.
Caps and retry count are unchanged. The reviewed source repair commit is
`a01f545e0d4eab9d60b6b6e67542a433b562e7b5`. Focused evidence is
`332 passed, 1 warning`; complete Web is `2934 passed, 20 skipped, 1 warning`
in 163.90 seconds, sever is
`33 passed`, the frontend production build passes, and touched-file
compile/diff checks pass.

Durable workflow-v31 evidence is under
`web/core/results/v143/logs/strict_invocations/`; final invocations
`103d05604a4a4141a34be170ce02b6c3` and
`8b6cf73bc64a46249e0bc3fa8426eb3b` bind canonical packet SHA-256
`b2c5d92c6ab0af3cda8efc8be426359dae6170e4760d3a7d820e3dd0d1da155e`.
Abandon transaction `71e9b5131c355a774903337aa56a12ab72491b2752e8558c0a9b87b03749b5b8`
binds the receipt above. These are diagnosis evidence only, not a replayable
plan, candidate, rating or strength result.

Resume order:

1. verify the repair commit is an ancestor of `origin/main` and the transient
   source branch is deleted;
2. stop the view-only Web process and fast-forward `.evolution_pok` only through
   `origin/main`;
3. rerun recovery,
   epoch, reconciliation, blueprint/control and official doctor diagnostics;
4. because evaluation contract changed from 32 to 33, never replay workflow-v31;
   checkpoint is absent, so freshly prepare workflow-v32+;
5. continue v143 publication, v144 normal full-v5, immutable rating cycle and
   resettable ten-generation observation. Any further repair/restart resets
   observation to `0/10`.

Post-merge update: steps 1–3 above are complete. `origin/main` and the stopped
runtime reached `e83d99634c581551995d7838a012f00fcb92eecb`; the transient source
branch was deleted. Recovery is inactive/recoverable with no issues, epoch is
`fresh_bootstrap_ready` at v142→v143 with an empty Bot pool, blueprint/reset
receipt/first-control are valid, reconciliation revalidated completed receipt
`af23c438202943b8c95b1a405a9c1f7f8ddb4a56e3802d4399b2e8156f2fd2f3`,
and official doctor is green on managed-executor profile v7. No checkpoint
exists, so contract 33 requires a fresh prepare rather than abandon or replay.
All poker processes are intentionally stopped; the next task begins at step 4
with stability `0/10`.

## Latest superseding handoff: contract 34 and fenced workflow-v34

All earlier wording that says the next runtime is a clean prepare with no
checkpoint is historical. The reviewed source series is now `19db5485` →
`a5f6f8fe` → `c3c00ae5` → `2cc426eb`, based on
`origin/main=7e90f1e9`; documentation is the next commit. Evaluation contract 34
repairs the Master target namespace and error-packet contract. It is not yet in
`origin/main`, so no runtime mutation is authorized from this source state.

The stopped autonomous checkout remains on tracked-clean `main` at `7e90f1e9`.
Poker Web, Orchestrator and rating daemon have no live process. Its active,
recoverable checkpoint is exact:

- workflow `generation:143:workflow-v34`;
- v143←v142, stage `direction_audited`, revision 4;
- checkpoint digest
  `fa631900c029ef7e2dd6979c64254e42211e037a646e2ef366710d7433dece66`;
- prepared artifact
  `0ad1dd758ebc0b62f86f19bdc645abaeb5b7d48fee7513aa8a5c0c65a2721a17`;
- candidate transaction manifest
  `f70128c977f8784c53e5301ae154d95aaf3afdb467f66db5282d2b9869d5f5e2`.

The candidate is strict five-file unpublished state only: no `.completed`,
certificate, tag, rating or stability credit. v34's three provider effects are
exhausted after the controlled stop cancelled their streams; this is shutdown
cleanup, not a proposal verdict. Never resume, delete or move v34 manually.

Contract 34 requires the exact typed equalities `mechanism_target =
mapping.mechanism_target = mapping.intervention_target =
falsifier.intervention_target` and `falsifier.state_learning_primary =
mapping.state_learning_primary`. Each executable field must carry
the exact bounded target literal. `opponent.rates.fold_to_raise` and
`opponent.terminal_response.fold_to_raise` have different owners; bare
underscore/hyphen/space/compact leaves, lookalike identifiers, owner/foreign
concatenation and long foreign-alias suffixes fail closed at both Scout
admission and packet-v5 replay. Historical v32/v33 outputs are diagnosis only:
owner-qualified outputs may revalidate, but any ambiguous bare-leaf output
remains rejected and cannot be rewritten or replayed. Invalid error packets
return their primary reason without success-packet cascade errors.

Current source evidence is Master-focused `99 passed`, expanded
Master/strict/evidence `217 passed`, full Web `2952 passed, 20 skipped, 1
dependency warning` in 147.38 seconds, Sever `33 passed`, frontend production
build (165 modules), compileall and diff check. Independent adversarial review
found no remaining P0/P1/P2 in this repair.

The exact resume order is mandatory:

1. commit these three active documents, independently inspect the clean
   detached series, and integrate it into `origin/main` without creating a new
   long-lived ref;
2. keep `.evolution_pok` on old HEAD `7e90f1e9`; re-read the checkpoint and
   require exact tuple
   `("generation:143:workflow-v34",143,142,4,"direction_audited")`;
3. invoke only `_do_abandon_generation(reason="abandon_generation", **
   expected_abandon_identity(checkpoint))`, then require
   `validate_completed_abandon_handoff`; the expected preimage transaction is
   `ac02c50c25b9239322b72b710159ba30f33c7e88a66c5fb960a7bbe11754f21c`;
4. require canonical proof of `abandoned=true`, `cleared_checkpoint=true`,
   workflow fencing, removed `national_v143`, and matching claim/receipt; any
   identity drift stops the operation;
5. only then fetch tags and `git pull --ff-only --tags` to the new
   `origin/main`;
6. run epoch reconciliation in the required quarantine selector, evaluation
   identity validation, official doctor, checkpoint recovery diagnostics,
   epoch/reset/blueprint/first-control validation; require no checkpoint,
   `active=false/recoverable=true/issues=[]`, `fresh_bootstrap_ready`, v142→v143
   and an empty strict pool;
7. restart through `pokctl.sh`, then require new Web/Orchestrator/rating PIDs,
   `/api/control/health overall=healthy`, bound rating runtime profile and a
   cycle identity equal to the live evaluation manifest;
8. allow the scheduler to allocate only a fresh workflow (expected v35), then
   continue v143 first-strict publication, v144 normal full-v5, immutable
   two-Bot rating and the resettable ten-generation observation.

The evaluation identity mismatch that caused the earlier frontend health
failure has been handled only through an explicit empty-state archive at
`web/core/results/archive/evaluation_identity/20260717_084012`. The live
pre-daemon identity is instance `d31950778dbd425cbed217b539121a13`, base
`78cac3e7e7d21fbcdddc520674d37f73f5e0ee97558650858443b0c523682982`,
manifest `4f433f912817f3218860ae9b6a26bd96f5fbfef18b961700df81483433be333b`
and `runtime_profile=null`. Do not claim it is runtime-bound until restart
readback proves that fact; do not rotate again unless the final main actually
changes a semantic evaluation file and the validator requests controlled
archive/initialize.

Worker MCP remains a Codex-only operator helper. Installed recovery evidence
and repository contract `a5f6f8fe` grant it no poker runtime/evidence authority.
A pending separate Worker MCP list/history fix is not part of this poker
checkpoint transition; until delivered, every new objective must fresh submit
and consume only the returned task ID.

No v143/v144 publication, certificate, rating row, immutable two-Bot cycle or
N/10 observation exists at this handoff. Any repair, manual state cleanup or
restart resets the observation count; current value is `0/10`.

## Latest superseding handoff: contract 35 and stopped workflow-v38

All earlier directions to resume workflow-v34 or treat v35–v38 provider output
as reusable are historical. The source repair is detached and not yet in
`origin/main`; the runtime must remain stopped on old HEAD
`c63253c5ef41f6a41992dcca3d5488706a6e63a8` until the reviewed commit is merged.

Before any runtime mutation, re-read the current checkpoint and require exactly:

- workflow `generation:143:workflow-v38`;
- `next_v=143`, `source_v=142`, `checkpoint_revision=4`,
  `stage=direction_audited`;
- transaction identity digest
  `6f7da5bef18c03841cd0094cc75ba67d7cabd30efae439880089f72e20aba190`;
- candidate quarantine preimage digest
  `6bd0374559da9bac75f723bfebdc4da900935fd726ef0cf3f9254a2f96250559`.

`national_v143` remains only an unpublished strict five-file candidate. It has
no `.completed`, certificate, annotated completion tag, rating, strength
sample or stability credit. Its incidental `__pycache__` files are excluded by
the strict ABI validator but included in the canonical abandonment preimage;
never delete them or the candidate manually.

Contract 35 separates failures that had previously been confused: a completed
deterministic-invalid Master output alone can receive the one schema/distinctness
repair; provider/SDK/transport errors are `MasterInfrastructureError`; a parent
cancellation is clean only after exact owned cleanup proof. Proposal Scouts use
their bounded 120/240/240/900 timeout policy and receive only ABI-reachable
source edges, typed capability feedback and targeted repair hints. They must
still reject foreign targets even in disclaimers and bare shared leaves.

The scheduler additionally requires an exact finalized canonical-abandon proof
before it can prepare another workflow. It may hand off only two abandons for
the same `(next_v, source_v)`; a third stops with exit 7 before successor
prepare. A numeric sentinel, missing receipt, mismatched target or uncertain
cleanup is recovery-blocked, not a reason to retry or advance.

The mandatory recovery order is:

1. commit, independently review, push and merge the contract-35 source;
2. keep `.evolution_pok` stopped on old HEAD, exact-CAS canonical-abandon v38
   using `_do_abandon_generation(reason="abandon_generation", **expected_abandon_identity(checkpoint))`;
3. validate the returned finalized handoff, candidate quarantine and cleared
   checkpoint; never use reconciliation `--execute` for this live v38 state;
4. only then fetch tags and fast-forward the stopped runtime to `origin/main`;
5. run read-only epoch reconciliation, evaluation identity validation, official
   doctor, reset/blueprint/first-control validators and checkpoint recovery
   diagnostics; require clean `fresh_bootstrap_ready`, v142→v143 and empty pool;
6. restart through `pokctl.sh`, verify live Web/Orchestrator/rating ownership,
   health/runtime evaluation identity, then let the scheduler prepare a fresh
   v143 workflow.

No v35/v36/v37/v38 provider effect may replay. Every repair, canonical abandon,
runtime restart or manual state cleanup keeps stability at `0/10`; v143, v144,
the immutable two-Bot native rating cycle and ten verified generations are all
still unclaimed.

## Latest superseding handoff: contract 36 river-baseline boundary

Contract 36 is an additional source-side repair on top of the stopped v38
handoff. The runtime remains stopped on old HEAD
`c63253c5ef41f6a41992dcca3d5488706a6e63a8`; it must not resume v38 or edit
`national_v143` in place. The only strict candidate is still unpublished and
has no completion, certificate, rating, native strength or observation credit.

The failed checked-in runtime probe was genuine: river baseline evaluation
synchronously visited all `C(45,2)=990` opponent holes and repeatedly exceeded
the 200 ms socket-owner target. Do not relax the target, pre-warm around the
test, or accept a worker-side timestamp. Contract 36 makes the source-policy
boundary explicit: `get_baseline_decision` uses a compact posterior-aware river
prior and publishes a legal typed intent; `iter_decisions` retains the same
weighted complete river enumeration under its monotonic refinement deadline.
This is not a strength-equivalence claim. Post-reprepare native 70-hand and
official evidence must decide the resulting policy quality.

The exact new system identities are policy SHA-256
`8c7ef2c8b128ebf53532be6d1cce7f8e530ffefa4390989872df92c4fa629780` and
output artifact SHA-256
`85b5d438360b06cafcc2e199b59d72155ed7e3613af94600fd7163784c17a826`.
The strict-v1 manifest and evaluation contract version 36 bind both. The
Master, Worker, Reviewer, Critic and Orchestrator prompt sources as well as the
strategy reference now prohibit full river enumeration in the synchronous
baseline and require it only in bounded refinement. Focused evidence is the
structural no-exact-baseline regression plus the real checked-in runtime probe,
`2 passed`; broader strict-policy/Master/scheduler aggregates are
`111/228/202 passed`, complete Web is `2971 passed, 20 skipped, 1 warning` in
181.29 seconds, Sever is `33 passed`, and `py_compile`, manifest validation,
`git diff --check` and the 165-module frontend production build pass. These are
source-only evidence, not Bot strength or publication.

After the contract-36 source is committed, reviewed, merged and pushed, follow
the already-recorded exact-CAS v38 canonical-abandon command against the
unchanged old runtime identity, then fast-forward the stopped checkout, run all
post-sync diagnostics, and let the scheduler create one fresh v143 workflow.
No v38 provider output, candidate byte, checkpoint or historical task result
may be reused. This transition resets the stability observation to `0/10`.

## Latest superseding handoff: Contract 37 bounded baseline and name handshake

All earlier “latest Contract-36” instructions, hashes, compact-river-prior
descriptions, and full-suite totals are historical. Do not use them as the
current source identity or as permission to resume v38.

Current source identity is evaluation contract 37; policy
`811f06007e979daaba278885607dee2db1ceac4aff8465bbb220eeeb3a0e5641`;
prepared artifact
`ccbe7d8b0fbbd47e337d95c715a3676e347de64a1cf483b09dfb647aefd8abe8`;
output artifact
`b86df2e70f756c8d9e76dc490c77da015b3c551bf2bce89d7c6ae9163f8dfa46`; and
national runtime
`0f8aebc7d9c7d5dd0a5cfea0ac7b50b520a24f387c3eb6db8924e4661dc0d7eb`.

The baseline is fixed deterministic `192/256/96` flop/turn/river work. Full
`C(45,2)` river enumeration is refinement-only, deadline-checked, and
publishes its exact completed posterior without a prior blend. Static schema-4
and dynamic 800-call gates reject enumeration/evaluator-alias bypasses. Probe
scenario v8 sends the actual raw `name` handshake first and requires the
system-owned worker to exist before the first decision; this is not a timing
exemption or a synthetic prewarm.

Focused source evidence is green, but full verification and merge are pending.
Keep `.evolution_pok` stopped. The required order is: finish all source gates;
commit/review/push/merge; exact-CAS canonical-abandon the recorded v38 identity
on its old HEAD; validate the finalized handoff; fast-forward only through
`origin/main`; run reconciliation, identity, official, blueprint/control and
checkpoint diagnostics; then start through `pokctl.sh` and allow one fresh
v143 prepare. No old provider result, candidate byte, checkpoint, or historical
test total is reusable. Stability is `0/10`.

## Latest superseding handoff: Contract 38 live-template admission and loaded-host timing

All earlier “latest Contract-37” identity, cache, readiness, and clean-host
wording is historical. It does not permit reuse of a v38 worker result, quality
receipt, precommit receipt, official job, candidate byte, checkpoint, or test
total.

The live source identity is evaluation contract 38; capability schema 5 /
`national-policy-static-v4`; probe schema/orchestrator/worker/scenario
`16/17/18/8`; strict policy
`811f06007e979daaba278885607dee2db1ceac4aff8465bbb220eeeb3a0e5641`;
strict prepared-policy
`28bcce8753c4f752c26c7491a81c6e3c6e0df18041f9333bd90e0096dc384816`;
prepared/output artifact
`ff388a3d88b67b2bc93e2968114aa1669aef7596ffeef78c1b75f42cfc873278` /
`39d623f5cfa3a1792edbc217e34b4f6a244afba9854a815cc79623b84e221fb4`; and
national runtime 10 /
`ec9e17951cc4c8070856432128492a5ae09eed146ea24fd86ce664a0bea2e366`.
The separate first-strict control binds policy
`d03317ec9c06081c143be84fa95bebf941cb724d08c4aea134add73d8fc388e4` and
expected artifact
`1cfe42b96566017ba470573b0aa9bc46a992c966779ff63db2470248d7440db2`.
These are source-review identities only, not a published Bot identity.

The policy baseline remains fixed deterministic `192/256/96`; full
`C(45,2)=990` river work is refinement-only and deadline-checked. Static
alias/value/deck-sweep gates and the 800-call dynamic limit apply to all
top-level system evaluator leaves. The local 200 ms native quality target is
stricter than, and does not replace, the formal 250 ms policy budget.

The raw `name` wire is a one-time system-worker **launch initiation** event.
It proves neither import completion nor a ready worker. Duplicate, malformed
or failed launch evidence is a compliance failure, and unfinished startup is
charged to the real first-decision socket-owner clock. Never manufacture a
reply, prewarm around the test, or call launch evidence a readiness proof.

The quality/probe/precommit/commit chain now carries a schema-2 **composite
system runtime identity**: `national_bot.py` and `precompute.py`, each with
its exact SHA-256 and size, plus a canonical `combined_digest`. Any missing,
malformed, mismatched, or precompute-only-drifted cache evidence is stale:
quality must refresh, precommit may not reuse it, and final commit admission
rejects it. Normal official full-v5 rechecks current
quality/probe/runtime/artifact identity both before creating the job and before
invoking the official EXE. The v143 first-strict control is an explicit,
zero-strength separate route and can never satisfy normal-v5 admission for
v144 or later.

Models may inspect frozen evidence, flag an anomaly and propose one falsifiable
repair. They cannot approve a failure, choose a cache hit, restart a runtime,
abandon a checkpoint, emit a certificate, or reclassify an infrastructure
failure as semantic success. Deterministic validators, raw TCP/native receipts
and the signed official chain decide those effects. Timing acceptance is run
with unrelated evaluation load left running; do not pause it or relax a gate to
obtain a clean-only result.

An earlier manual-entry official-admission shard (`335 passed`) is historical:
the automatic commit→job path was subsequently found to omit the frozen
receipt. The superseding end-to-end official admission shard, including
automatic commit, request, schema-5 envelope, job worker and harness rejection,
is `187 passed`. The current source-freeze P1 evidence is recorded as exact
commands rather than one additive count (the shards overlap): matrix/prompt/
identity `75 passed`; authority/probe/quality/pipeline/precommit/capability
`146 passed`; official admission/harness/job/CLI/sandbox `210 passed`; and the
post-full-suite compatibility fixtures `37 passed`. The older `27` targeted,
`157` chain, and `355` precursor counts are historical subsets, not the final
evidence total.
Repeated bounded raw-wire timing
checks and a 74-test probe/capability/bootstrap/control shard ran under
concurrent LLL evaluation load. Those are scoped source checks, not a full
suite, merge, runtime, certificate, rating, or strength result. Finish the
complete source gate and review before any runtime mutation.

The required recovery sequence is unchanged and must be executed only after
the Contract-38 source is committed, reviewed, pushed and merged:

1. Keep `.evolution_pok` stopped at old HEAD
   `c63253c5ef41f6a41992dcca3d5488706a6e63a8`; reread and exact-match
   `generation:143:workflow-v38`, v143←v142, revision 4,
   `direction_audited`, transaction digest
   `6f7da5bef18c03841cd0094cc75ba67d7cabd30efae439880089f72e20aba190`, and
   preimage digest
   `6bd0374559da9bac75f723bfebdc4da900935fd726ef0cf3f9254a2f96250559`.
2. Invoke only the governed exact-CAS canonical abandon using
   `_do_abandon_generation(reason="abandon_generation",
   **expected_abandon_identity(checkpoint))`; validate the finalized handoff,
   candidate quarantine, cleared checkpoint, and no identity drift. Never
   delete or edit the checkpoint/candidate manually.
3. Only then fast-forward the stopped runtime through merged `origin/main`,
   run recovery, epoch/reconciliation, evaluation-identity, blueprint,
   first-control and official diagnostics, and require a fresh-bootstrap-safe
   result.
4. Start through the controlled launcher, verify Web/Orchestrator/rating
   ownership and health, then let the scheduler create a fresh v143 workflow.
   Complete v143 first-strict control publication, v144 normal 5+3×70
   full-v5, the immutable two-Bot native rating cycle, and ten uninterrupted
   verified generations.

Every source repair, canonical abandon, manual state cleanup or runtime restart
resets observation to `0/10`. It remains `0/10` now.

## 下一主代理交接摘要（必须以当前工作树复核）

这是给接续智能体的操作摘要，不是运行态完成声明。先执行
`git status --short`、读取本节之后的最新 ledger 条目、核对
`origin/main` 与 `.evolution_pok` HEAD；任何聊天记录、旧测试总数或
目录名都不能替代这些事实。

- **目标不变：** 完成可执行跨层对齐，安全恢复 strict v143，完成 v144、
  两 Bot immutable native rating cycle，并从最后一次修复或重启起观察
  连续 `10/10` 代。当前尚未发布任何 strict Bot，所有这些运行态目标仍为
  pending，计数为 `0/10`。
- **工作位置：** 唯一可编辑实现工作树是
  `/home/zzx/project/pok/.codex_worktrees/national-protocol-evolution-alignment`
  （此刻为未提交集成树，最初基线 `d4cb9151`）。运行 checkout
  `/home/zzx/project/pok/.evolution_pok` 与 `origin/main` 仍是
  `c63253c5ef41f6a41992dcca3d5488706a6e63a8`；不要改动
  `pok-arena`、`three-bot-consolidation` 或运行 checkout 的候选/状态。
- **已冻结的源级成果：** Contract-38 strict baseline、静态/动态 evaluator
  cap、真实 raw TCP `name` launch-not-ready 门、前后端 transient-status
  fence、五角色最终 renderer overlay、可执行 alignment matrix、手工与
  automatic normal-full quality admission 都已进入当前工作树。代表性负载下
  证据包括 Sever `33`、协议/quality/matrix `141`、official automatic
  admission `187`、前端 `20`、状态链 `62`、提示词 `55`；它们都是 source
  evidence，不是证书或强度。
- **P1 已在当前 source tree 闭合、但尚未交付：** runtime cache identity 已由
  单一 `national_bot.py` 升级为 schema-2 两文件组合身份：
  `national_bot.py` 与 `precompute.py` 的 SHA-256/size 和
  `combined_digest`。probe、quality、precommit、commit 与 formal admission
  都必须对该对象 fail-closed；precompute-only drift 也必须刷新或拒绝。P1
  shard 证据是 `75`、`146`、`210` 与 post-full-suite `37 passed`（测试集合
  有重叠，禁止相加冒充强度）；完整 Web suite 已以 exit `0` 结束。仍须在
  Sever/frontend/compile/diff 及独立审查中保持复核；它不是 merge、runtime
  或认证证明。
- **模型与门禁：** Master/Worker/Reviewer 可用模型提出单一可证伪变更，
  Critic 仅 advisory；模型不能批准失败、制造缓存命中、重启服务、清理
  checkpoint 或签发证书。Worker MCP 当前要先通过其自身健康/版本门，且
  永远不得被扑克 runtime import/start/call。
- **并发负载：** 用户明确要求保持后台对局/评测运行，把它视为实际比赛
  主机负载。不要停止它、不要为了干净结果降低 200 ms/250 ms 或其他协议门；
  最终 focused/full 验证都要在该负载下重新记录。
- **完成源代码后的唯一恢复顺序：** 独立审查 → 完整测试/编译/diff → commit
  → push/merge `origin/main` → 在旧 HEAD、停机的 runtime 上 exact-CAS
  canonical abandon v38（绝不手删）→ 验证 handoff → fast-forward → recovery
  diagnostics → fresh v143 re-prepare → controlled launch。任何本轮修复、
  abandon、人工清理或重启都保持/重置 `0/10`。
- **三 Bot 研究到未来 canonical 的边界：** A1（range/PBS/价值--策略搜索
  闭环）、A2（线性 CFR 蓝图/抽象/off-tree resolve）、B（街级在线求解/神经
  CFV/动态动作/对手后验/比赛控制）是三项独立、可审查的未来 component
  migrations，不是三选一。当前 v5 研究线只可给出行为级结论，绝不迁移源码、
  权重/资产、H2H、rating、experience、tag 或 certificate。future-main 必须让
  每项重新物化为新的严格五文件产物、系统资产路径、identity/probe/gates 和
  零强度设计证据，并重新走 native/official 评测与认证；最终组合 Bot 仍须是
  另一个新 artifact 与新 immutable rating cycle。

## 2026-07-17 — 最新未合并 P0/P1 交接：task owner 与 live official admission

在任何恢复 runtime 前，接续代理必须保留本工作树刚完成的两项闭环。第一项是
前端状态所有权：`WebUI.set_status` 只能在 canonical checkpoint 和 active
`AppState.task_snapshot().owner_id`（32 位 UUID hex）同时有效时发布状态；SSE
replay、`/api/evolution/state`、TypeScript schema/controller 和页面都必须精确
比较 `task_owner_id` 与 `task_lifecycle_revision`。同一 checkpoint/revision/stage
下的旧 owner A 不能显示为 replacement owner B 的 “Master planning”。缺失、非
活动、stopping、格式错误或不相等一律清空 transient status；这不是对任务进度的
推断。任务 owner 的每个生命周期边，包含直接
`ShutdownManager.request_shutdown()`，必须通过最小 `{present, done,
shutdown_requested, status_eligible, owner_id, lifecycle_revision}` SSE 事件立即使
旧状态失效；revision 必须单调。route 重放前重新比对实时 owner/revision；`/state`
返回的同采样 `transient_status_task` 只用于 lifecycle high-water/invalidation，
不授予文本显示权。连接中的页面只接受当前 SSE `status` 事件的人类状态文本；HTTP
snapshot 即使 owner/revision 相同也不能复活或覆盖文本。低 revision、相同 revision
但投影冲突、shutdown/stopping、SSE 断开均清空文本，并显示中性或非绿色的
“正在安全停止，等待清理”/degraded 状态，不能因 HTTP/SSE 反向到达或五秒轮询而
显示 A。若 backend 无法形成权威 projection，必须发 `task_authority_lost` 而不是
伪造 revision `R+1`；HTTP null/畸形与 SSE malformed `status`/`task_owner` 都只清
文本、保留最后验证的 high-water。后续 exact valid 同 R 投影可以恢复 authority；同 R
但不同字段的冲突保持 fail-closed，直到真实更高 revision。

App lifespan 只停止其 registered-owner 集中的 owner；自动 launch 与后续成功的
`/api/control/start` 必须都登记。lifecycle shutdown 读取当前 `AppState` 的 manager，
不能使用 startup 时缓存的 manager，因此后启动的 registered owner 会被正常 graceful
stop，而未登记/foreign owner 永远不被该 lifespan 停止。

第二项是 normal full-v5 admission 的 live rebind：
`official_certification_job._live_normal_full_admission_issues` 在新 request
落盘前、queued/retry 取队列前、以及 `_spawn_worker` 的 `Popen` 前重算并比较
current quality/probe/runtime/artifact admission；worker claim 后、进入 runner 前
也必须重算，harness 继续在 EXE 前重算。任何这类 drift 必须记录为 terminal
`quality_admission`。EXE-adjacent formal admission 失败必须以 typed quality failure
穿透 generic infrastructure 捕获；`tool_commit` 不得降级成 infrastructure retry，
只能在完整 marker `official_full.outcome=quality_admission_blocked`、
`failure_class=quality` 与 `quality_admission_refresh=true` 同时存在时保持
`official_certifying` 并路由回 deterministic quality refresh。普通
`official_certifying`（包括普通 HEAD drift）只允许 `commit_bot`；此 exception 必须
保持 contract-unchanged 与 exact revision+stage+workflow CAS，且不授权 Worker、旧 job
retry 或 EXE。quality refresh 后才允许一个新的 official request。
stale fresh 请求不得产生 `request.json`/`state.json`，stale queued job 必须写为
terminal `failed/quality_admission`，不得占 worker 或 official EXE。v143 explicit
bootstrap 不走 normal admission。pytest-only envelope 必须带 `spec_record(spec)`，
从而保留 `quality_admission`、digest 和 required flag。

相应矩阵、五角色 overlay、runbook 和 ledger 已记录；最新 focused 后端证据为
`245 passed`，frontend SSE 为 `20 passed`、lint/build 通过。它们不是 full suite、merge、运行态、
证书或 strength 证据；仍需在真实后台负载下完成 full Web/Sever/frontend build/
compile/diff、独立审查、commit/push/merge，之后才执行 stopped v38 的 exact-CAS
abandon、同步和 fresh v143 re-prepare。稳定计数仍为 `0/10`。

## 最新 superseding handoff — v39 已安全 abandon，provider handoff P1 待合入

先以工作树和 runtime 的真实状态为准，不要执行本文件较早的 “stopped v38”
操作说明。v38 已按其旧 HEAD exact-CAS 规则完成 canonical abandon；runtime 已
fast-forward 到 `7b90425900fec88181f5c2c4bc655fb8d8b7d879`，并因 formal runtime
identity 变化进行 controlled evaluation-data archive/initialize。随后 fresh
`generation:143:workflow-v39`（v143←v142）运行到 Master Proposal 阶段。

v39 没有产生 Worker、quality、review、Critic、precommit、official、commit、tag、
rating 或证书。它被严格 schema contract 正确终止：mechanism/compute 初稿把
`mechanism_target` 错放进 closed `falsifier`，mechanism 唯一 repair 又在 executable
text 使用 bare `fold_to_raise`；只有 2/3 Scout 合法，strict-authority schema retry
耗尽后由系统 canonical abandon。其 transaction 是
`379919048a9db4536b9727e02a259671225cab139000cccd9f7349c48a3d24ca`，candidate
已在该 transaction 的 quarantine 中，checkpoint 已清除，两个 workflow journal
terminal fence 已验证。绝不手动移动、删除或恢复该 transaction/candidate。

当前 runtime 的 Web/daemon process 可以存活，但 evolution loop 必须视为 **stopped**，
不是可启动状态。根因不是 transaction、checkpoint 或模型能力：SDK 可将
`ToolUseBlock` 放在 `UserMessage`，并且真实 MCP handler 可能先于 outer stream 登记
该 ToolUse 执行。旧 outer Orchestrator 只登记 `AssistantMessage` 的 pending tool ID，
真实 `run_master` 的 terminal result 因而未被 handoff consumer 保留；consumer 用 `None`
再验证时正确 fail-closed。不得用“交易在磁盘上已存在”或手工重建 result 来启动 successor。

唯一可编辑位置仍是
`/home/zzx/project/pok/.codex_worktrees/national-protocol-evolution-alignment`，当前
基线 `7b904259` 上有未提交 P1 修复。修复必须同时做到：

1. `UserMessage` 内的 `ToolUseBlock` 与 `AssistantMessage` 使用同一 explicit-id、
   Evolution MCP owner、canonical arguments、duplicate 和 pending-result contract；
2. 仅真实 guarded mutating owner 在返回后、以 pre-call checkpoint 成功调用
   `validate_completed_abandon_handoff` 时，才可在 **同一 active provider attempt**
   保存一个内存 terminal record；若 handler 先于 stream 登记，只能保存没有 ToolUse id、
   对 consumer 不可见的 provisional record，且仅一个后续 exact owner/arguments
   registration 可原子绑定它；
3. 缺 SDK ToolResult 时，仅一个已经绑定的 **同 id、同 owner、同 canonical arguments、同
   checkpoint** 的 pending ToolUse 缓存可补足，且 outer handoff 再次 revalidate；
   SDK result 与缓存同时存在必须 canonical byte-identical。缓存生产/消费共享
   terminal-owner whitelist，`run_archivist` 等非 terminal owner 永不接受。无缓存、
   still-provisional、重复、settled-history、owner/arguments/id/identity/proof 不匹配、
   进程重启或裸 receipt 一律
   recovery-blocked；
4. Master Scout prompt 输出 closed JSON skeleton，明确 falsifier 是六键 closed object，
   `mechanism_target` 仅顶层；不要放宽 falsifier validator、shared-leaf namespace
   validator 或两次 schema-attempt 上限。

当前已新增的 focused regressions 覆盖 UserMessage ToolUse→canonical result、handler-before-
stream provisional→unique exact bind→lost SDK result、missing/owner-mismatch/wrong-args/
settled-history cache fail-closed、side-channel legacy result ignore、cache duplicate rejection、
closed falsifier prompt/repair contract；已运行
`web/tests/test_master_proposal_ensemble.py` +
`web/tests/test_orchestrator_timeout_extension.py`（`165 passed`）和
route/one-gen/prompt/role shard（`210 passed`）。在代码、matrix、ledger 和本节都
更新后仍必须跑完整 Web/Sever/frontend/compile/diff 与独立审查，才可以 commit、push、
merge。

合入后，确认 `.evolution_pok` 无 active checkpoint/candidate、停止当前 launcher，
只从 merged `origin/main` fast-forward；再运行 checkpoint recovery、epoch/evaluation
identity、blueprint/control/official diagnostics。若诊断干净，受控启动 fresh v143，
不得 replay v39。每次源码修复、canonical abandon 或重启都保持 stability `0/10`。

三-Bot v5 研究线已在 `d53e5e43` 完成 480/480 valid direct-H2H cells，但 12/12
Holm-adjusted comparisons均不显著；仍隔离、零 authority。future canonical Bot 只能
吸收 A1/A2/B 的 clean-room behavior proposal，重新生成 strict five-file artifact、
system asset/identity/probe/gates、官方认证和 immutable rating cycle，不能复制源码、
资产或研究强度。

后续的“系统经验”必须是单独的 evidence-derived lesson contract，而不是 Master 的可变
自由文本文件：每张经验卡需绑定 active artifact identity、完整 replay/validator/runtime
identity、immutable evaluation cycle、producer/derivation digest、适用/失效条件和正负
回归；只将冻结快照注入下一代。无来源、冲突、过期、archive 或模型自述的内容均不消费。
最小实现应把内容寻址的 `lesson_cards.json` 放在每代 `evidence_snapshot` 中，由 scheduler
在既有 snapshot/evaluation lock 内从冻结、已验证、完整 70-hand native replay 确定性导出
至多三张 `advisory_only` 卡；v143 的零池或证据不足必须产出显式空包。Master 只能引用
冻结 `card_id+digest` 提出可证伪假设，Worker/Reviewer 重新验证来源；rating、selection、
official/certification 和 live 历史均不得直接消费或重导它。
v39 的 schema 失败已经以 closed skeleton、提示词、validator 和负回归修复，而不是作为
可迁移策略或强度经验。该 facility 仍是 future source work，不授权当前 runtime 启动或
改变 `0/10`。

## 最新接续状态 — workflow-v40/v41/v42 后的唯一启动路径

本文较早的 v39/v38 操作描述仅保留历史审计价值。当前事实是：v143 的
`workflow-v40`、`v41`、`v42` 都已通过 canonical abandon 安全结束；没有 Worker、质量、
review、Critic、precommit、official、commit、tag、rating 或 strength 产物。v42 的首个
final Master 已被严格 authority 接受，但重复外层 Master 入口重新拼装 packet 后触发
`master:final` context drift；系统正确拒绝该重复 effect。三次同 `(142,143)` canonical
abandon 已达上限，scheduler 已停，不得手工删 checkpoint/候选或创建 v43。

新的源代码修复把 architecture-policy 选择的 primary 同时绑定到 Scout reference card、
falsifier mapping、strict-authority call context 和 deterministic projection；action-profile
bootstrap 只能看到自己的 root。schema retry 不回显旧 provider 原文。若 final Master 已
sealed，则先用 sealed packet + 同一 immutable architecture policy 重建 exact binding 并
replay，不再调用 Scout/Critic/provider；缺包、策略漂移、重复 final 或 journal 不一致都
fail-closed。

严格 ABI 的正确表述是“Bot 目录五个可执行/身份文件”，不是永久禁止模型或预加载表。
候选仍不能携带资产或执行文件 I/O。未来 R0 资产 ABI 只能把模型/表置于 Bot 目录外的
系统拥有根，要求 registry/issuance receipt、内容绑定 manifest、大小/查询上限、no-follow
只读验证、带 nonce/quota 的 broker、native/precommit/probe/Arena/official 同一 resolver，
以及系统观察到的决策影响正负测试。未完成这些条件前，v143 只使用 system-owned
`precompute.py`，绝不把资产放行给 Worker 或 policy 路径。

下一步顺序不得改变：完成 full Web/Sever/frontend/compile/diff 与独立审阅 → commit/push/merge
到 `origin/main` → 在 runtime 停机且无 active checkpoint/candidate 时只通过 git fast-forward
同步 `.evolution_pok` → checkpoint/evaluation/official diagnostics → fresh v143 prepare/start。
任何源码修复、同步或重启都使稳定观察保持 `0/10`；第一代正式上线后再按每个已验证发布代次
累加。

## 最新接续补充 — 资产边界 P1 与可执行验证状态

已经发现并修复一个不能忽略的假绿：最终 publication shape 原本会拒绝 Bot 目录中的
`foreign-model.bin`，但 `evaluate_national_capabilities()` 只扫描 Python、
`check_native_contract()` 也未检查严格布局，因此两个较早门会误报通过。现在它们都调用
`strict_artifact_layout_errors`；完整五文件 Bot 只要多出模型/表/缓存/软链/辅助文件，就在
静态能力和 native TCP 合同阶段 fail-closed。`national_alignment_matrix.py` schema 6 已把
`system_asset_boundary` 纳入生成式跨层矩阵，绑定五角色 overlay、当前所有者、正反回归及
“assetless-v1”状态。

这不是把大模型/预加载表永久禁止：**五文件限制的是 Bot 目录的 executable/identity
surface，不是整个系统的文件数。** 当前 v143 只能使用 `precompute.py`。未来 R0 才可通过
系统拥有的 Bot 外资产根放行表/模型，并必须同时绑定 registry/issuance receipt、manifest/blob/
resolver digest、大小/查询/解码上限、no-follow、sealed broker、所有 launcher 的同一 resolver、
runtime/evidence/rating/official identity 和决策影响正反探针；禁止 Worker 写资产、policy 读路径
或将其作为第六 Bot 文件。未完成这整套 ABI 前不启用。

本机 Python 3.14 的 Starlette TestClient 停滞已通过仅测试侧 uvloop portal compatibility
闭环（有/无 uvloop 和旧版本回归均覆盖），所以 full Web 能跑到终态；但当前 Codex 沙箱禁止
AF_INET/AF_INET6 loopback 与 bwrap NETLINK_ROUTE。最新 aggregate 结果为 `3044 passed, 20
skipped, 41 failed`，41 项均在真实 socket/bwrap 产品集成测试的宿主能力前置处失败，不能称
为代码失败或 full-green，也不得通过 skip 绕过。已跑过的 source focused shard 为 `398 passed`，
Sever `33 passed`，frontend build、compile、diff-check 均通过。必须在允许 loopback+bwrap 的
宿主重跑完整 Web 后，才允许 merge/sync/restart。

## 最新 superseding handoff — 宿主全量验证已通过，待正常发布

上节 `3044 passed, 20 skipped, 41 failed` 仅描述受限 Codex sandbox；不要再把它
当作当前源码 blocker。相同源码已在允许 loopback socket 与 bwrap NETLINK_ROUTE 的宿主
执行 `PYTHONPATH=web/core <project-python> -m pytest -q web/tests`，结果为 **`3085
passed, 20 skipped, 1 warning`**。Sever 是 **`33 passed`**。这覆盖此前 41 个 native
TCP/managed-executor/official-wire/import-contract 测试，不得把它们重新标为 skip。

前端还修复了一处真实测试流程错配：SSE Python 生产者→TypeScript validator 测试现在要求
`PYTHON` 指向含 Web/FastAPI/Claude SDK 依赖的项目解释器，禁止回退 bare `python` 或
伪造生产者。正确命令是：

```bash
cd web/frontend
PYTHON=/home/zzx/anaconda3/envs/pytorch/bin/python npm test
npm run lint
npm run build
```

该真实生产者链路为 **22/22**，lint/build 通过。当前工作树与 fetched
`origin/main=855861bf85221a13f841593d6690cc9990ece611` 同基线；完成最后
compile/diff 后，下一步是正常 commit/push/merge，再在 runtime 停机且无 active checkpoint/
candidate 时只通过 Git fast-forward 同步 `.evolution_pok`，重跑 recovery/epoch/evaluation/
official diagnostics，随后 fresh v143 prepare/start。`.evolution_pok` 当前停止、HEAD 同为
`855861bf`，其已有的 `national_arena/storage_owner.lock` 不得手删。v143/v144/rating/N=10
均仍未产生，观察计数保持 **0/10**。

## 最新运行接续 — `5a6cf7ef` 上的 active workflow-v43

This section supersedes the stopped-`855861bf` restart wording above. The
runtime checkout was safely fast-forwarded to
`5a6cf7ef67c959bbe5d91dcc1dd869736728719e` after a stopped-state recovery
diagnostic (`active=false/recoverable=true/issues=[]`), explicit evaluator
identity archive/initialize, and a green official doctor. It is now running
one fresh `generation:143:workflow-v43` from `v142`; Web, Orchestrator and the
12-worker/5-pair native daemon are live. The daemon is correctly waiting for
the first two published strict Bots and supplies no strength evidence yet.

Do not stop, restart, re-submit, edit, copy, or delete this active checkpoint.
The three Master proposals and both critics completed; a first final-Master
proposal exceeded the worker-prompt budget and was rejected by the strict
binding gate, then the same sealed proposal entered its governed schema repair.
The second final Master was accepted. Treat this as live progress, not a Bot,
certificate, tag, rating, official result, or stability credit. Follow the
current checkpoint and canonical transactions only.

The first `health` request after a 30-second verification-cache expiry can
briefly return `degraded` while a background verifier refreshes; a follow-up
projection becomes `healthy`. This does not permit calling stale healthy. A
source-only pre-expiry single-flight maintenance repair is prepared with
stability/matrix regressions, but **must not be synchronized or restart v43**.
Commit/review it in the alignment worktree, wait for v43's canonical terminal
safe boundary, then use the normal stopped-checkout git/diagnostic/restart path.
Any such sync or restart keeps observation at `0/10`.

## Latest superseding handoff — v43 Master receipt repair is source-pending

Supersede the preceding instruction to wait passively for `workflow-v43`: it
cannot reach Worker with its current runtime source. The accepted final Master
was deterministically rejected by a split bootstrap receipt recipe, not by a
model, policy, reset, or certification failure. The exact errors are
`system_bootstrap_proposal_contract_digest_mismatch` and
`system_bootstrap_worker_selected_proposal_block_missing`. Its checkpoint is
still pre-Worker at `direction_audited`; do not replay its final output, edit
its candidate, add a receipt, or delete/checkpoint-clean it by hand.

The source repair has three coupled parts: use
`agent_master._selected_proposal_binding` as the bootstrap validator's one
canonical packet-to-plan projection; retain a compact selected-proposal identity anchor
in compiler-externalized Worker stubs while the executable full prompt remains
in the temporary system-owned brief and the strict final-Master journal seals
the authoritative source; and authorize both the deterministic `..._invalid:`
route and unexpected `..._error:` route to canonically abandon only at
`direction_audited`, preventing an outer recovery loop. The Bot ABI stays five
executable/identity files. The compiler
brief and any future table/model are not candidate-owned sixth files: only the
separate future R0 system-asset ABI may introduce a table/model outside the Bot
directory after its own resolver/influence gates.

Source evidence: focused cross-layer contract shards `237 passed, 1 warning`,
host full Web `3090 passed, 20 skipped, 1 warning` in 175.22 seconds, Sever
`33 passed`, and frontend lint + `22/22` production-stream tests + build;
an isolated replay of the real v43 10,239-character sealed plan has zero
repaired bootstrap errors. Complete independent review plus final
compile/diff checks before any runtime mutation. Then stop the
old runtime, use its schema-2 exact-CAS
canonical abandon transaction for v43, fast-forward only from merged
`origin/main`, run checkpoint/evaluation/official diagnostics, and launch a
fresh v143. This reset/restart starts stability again at `0/10`; it does not
create a Bot, certificate, rating cycle, or strength claim.

## Latest operational status — v45 is live; restart helper hardening is pending

After the schema-2 v43 abandon and the `2d182c89` fast-forward, stopped-state
recovery diagnostics reported `active=false/recoverable=true/issues=[]`,
evaluation identity was consistent, and the official doctor was green. The
runtime started `generation:143:workflow-v44` on `2d182c89`, but that attempt
canonically abandoned before a Worker when strict-authority proposal validation
exhausted its `compute_memory` schema retry. Its checkpoint and candidate were
removed only through the schema-2 transaction; it produced no Bot, certificate,
tag, rating, or strength sample. The healthy scheduler then started fresh
`generation:143:workflow-v45` on the same merged HEAD. v45 has completed its
direction audit and is conducting Master proposal work. It remains actual
in-flight work, not yet a Worker, candidate delivery, certificate, tag, rating,
or strength sample.

The first restart attempt revealed that `scripts/pok_restart_observe.sh` used a
bare `python` after safely stopping the service. It was retried with the exact
verified project interpreter and is now healthy. A source-pending operational
patch introduces `pokctl.sh resolve-python`, resolves/import-preflights it
before stop, and reuses it for config/health/observer inline calls; regression
coverage includes both resolved-path and missing-path fail-closed cases. Do not
sync or restart v45 merely for this helper change. Let the active evaluation
reach its canonical safe boundary first; any later restart resets `N/10`.

## Latest superseding handoff — v46 is terminal; repair and restart are pending

All previous instructions that describe v45 as live are historical. v44, v45,
and v46 canonically abandoned at Master before Worker/candidate/quality/
certificate/tag/rating/H2H. v46 consumed its bounded abandonment allowance and
the outer scheduler stopped cleanly: there is no active `pipeline_state.json`,
no active evolution service process, no candidate directory to hand-edit, and
no strength or stability credit (**0/10**). Do not resume a historical raw
Master response, hand-create a Bot, delete checkpoints, or copy files into the
runtime checkout.

The alignment worktree contains the pending paired source repairs: (1) an
owner-proof grammar for shared decision-context leaves—only a complete selected
owner or exact selected-root allowlisted list is executable; all bare,
hyphenated, spaced, compact, foreign, unknown-child and continuation forms
reject; (2) fresh-control measurement keeps a required six-field shape but is
system-bound to the fixed no-strength contract; (3) final selected-proposal
metadata is derived from the sealed proposal ID; and (4) a late, digest-bound
emission guard makes the real Worker hard cap visible without weakening that
gate. The companion restart helper preflights the exact project interpreter
before stopping any service.

Required next sequence: finish focused/full source tests and independent
review; commit/push and merge the source changes to `origin/main`; from the
stopped runtime use only Git fast-forward to that merged SHA; run checkpoint
recovery, evaluator identity, and official-doctor diagnostics; then use the
preflighted restart helper to create one fresh v143 workflow. Observe
Master→Worker→quality→review→advisory Critic→native TCP precommit, then park
for the explicit operator-only first-strict bootstrap. No v143 certificate,
tag, rating, v144 5+3, or N/10 increment may be reported until its respective
hard gate completes.

## Latest superseding handoff — v47/v48 are terminal; source P1 repairs await final integration verification

Supersede all wording that describes any v44–v48 workflow as active. The Web
service may still be listening, but `/api/control/health` reports the
Orchestrator stopped, there is no provider/Worker process, and the strict epoch
has no published Bot, certificate, tag, rating, H2H, or native strength sample.
`workflow-v47` and `workflow-v48` are schema-2 canonical abandons; never delete
or rewrite their checkpoint, quarantine, journal, or receipt by hand. Stability
is **0/10**, with the latest reset reason `worker_terminal_abandon`.

Four independently reviewed source repair groups are staged in the alignment branch:

1. strict final-Master replay now runs the normalized accepted role result
   through one production-equivalent compiler invocation, using a separate
   comparison-only binding for task-context validation and rebasing both
   temporary path and `compiled_chars` metadata;
2. capability schema 8 / detector v7 distinguishes a public input literal such
   as `opponent_action == "check"` from a bare action value returned by a policy
   entrypoint. It follows bounded module-local helper return/yield, literal
   `.format`, tuple/list subscript and `+=` string aliases in addition to
   direct/alias/conditional/branch paths. Recursive/depth-exhausted helpers at
   output fail closed; dynamic forms remain runtime-sanitizer/probe territory;
   typed `{"kind":"check"}` remains invalid;
3. canonical abandon recovery distinguishes strict outer reason from an
   already-terminal Worker's causation-bound inner reason. New Worker events
   bind the same bounded reason in payload and causation, and existing schema-2
   outer reason lengths 999/1000/1001/4096 remain read-only compatible.
4. after a source fast-forward, a completed checkpoint-free v48 abandon is
   reproved only through `scripts/reprove_completed_abandon.py` using its exact
   immutable transaction. It requires clean fetched `main` descendant lineage,
   terminal receipts/tip ledger/fenced journals and no live state or published
   candidate; the returned result is terminal evidence only
   (`prepare_authorized=false`, `next_tool=null`), never a checkpoint rewrite,
   provider replay or prepare authorization.

Reference-pack v6 injects the matching contract into every rendered role:
`call`/`check` can be observed public state, but policy output is only a typed
intent and never a bare wire string or integer.

The same delivery makes frontend transient status expire after 30 seconds and
requires a source-bound static build receipt before `--no-build` may stop or
restart Web. A restart therefore requires `cd web/frontend && npm run build`
in the exact target checkout before the preflighted restart helper; never bypass
the receipt or use a stale static bundle.

Required next sequence: complete the combined integration tests and generated
matrix/document checks; commit, push alignment, and fast-forward `origin/main`;
stop the currently idle runtime through `pokctl.sh`, Git fast-forward it only
from that merged main SHA, build the frontend receipt, and run checkpoint
recovery/evaluation identity/official doctor diagnostics. From `.evolution_pok`,
run `python scripts/reprove_completed_abandon.py 6320ac2f4ebe24cd7964cfd10fa7f5b5f4712c361016cc0ec3808e2f73584b33`;
it must return terminal-only proof before a fresh
current-contract v143 is prepared. Follow its actual Master → Worker → quality
→ review → advisory Critic → native TCP precommit path. Only a successful
explicit operator first-strict bootstrap can create v143; nothing in this
handoff is Bot, certification, rating, official, or strength evidence.

## Latest superseding handoff — v49/v50/v51 are terminal; P1 source is ready for final merge review

All earlier instructions to restart a generic current-contract `v143` after the
P1 source merge are superseded by the user's literal initial-product route:
`v143=A1`, `v144=A2`, `v145=B`. The runtime remains at
`71673a18de42f20fc21d7d5f79a7e9cb826343db`; its Orchestrator stopped after
the third schema-2 canonical abandon, Web/idle rating infrastructure may still
listen, and there is no active checkpoint, candidate, published Bot, certificate,
tag, rating/H2H, or 70-hand strength sample. Stability remains **0/10**.

The exact final v51 terminal transaction is
`316ffd7211d7c9ec44ed6e38766eebc9ca11661d99cab4a8e40754b4e188ec44`, with
abandon receipt `6d22582c97ba92d6c5e805bdd1462d116842ba877d978bff681f77cf72fe8c22`
and finalize receipt
`e85398272b1d871dd9de71fe7fdbd334c42965a869c1fa52ea926f00bdbe2db0`.
`v49`/`v50` failed only the former over-strict dynamic repeatability gate after
real Worker/native acceptance; they are not retroactively green or strength
evidence. `v51` accepted a visible 12k Master contract but then hit a hidden
10k compiler compaction and correctly abandoned before a Worker candidate.

The current source integration chain is `71673a18 → dc6bd38b → fb6120fc →
2243468a` (detached alignment worktree). It is still unpushed/unmerged. The
two P1 repairs are coupled:

1. strict Master planning has exactly one default 12,000-character authority
   form; selected binding and final emission use the same Unicode `rstrip()`
   normalization; strict plans cannot use compiler task-brief compaction;
2. runtime probe schema 18 / worker 19 / repeatability schema 3 keeps only a
   bounded redacted semantic receipt, but requires complete official scenarios,
   per-scenario typed intent/canonical wire/runtime rows, clean nested evidence,
   canonical managed-isolation binding, and matching repeated view digests.
   The same validator gates fresh quality, cache/recovery/precommit reuse,
   commit, v143 bootstrap, formal admission, and durable official rebind.

Every rendered Master/Worker/Reviewer/Critic/Orchestrator prompt now says that
the repeatability receipt, per-scenario transcript, and fenced writer provenance
are system evidence rather than model-replaceable evidence. The executable
matrix is schema 11 and the delivery ledger records the v49-v51 causes. Before
merging, rerun matrix/prompt tests after rendering the human document, full Web,
Sever, relevant frontend checks, compile/diff checks, and independent review.

Do **not** synchronize or restart `.evolution_pok` merely because these P1
repairs merge. The user requires actual first three products, not a generic
baseline relabeled afterward. The research line
`codex/three-bot-consolidation@5b02663908251f6c6c1ee5f7cdc4840508598d31` is
docs-only input: research code, assets, receipts, seeds, v5 H2H and strength
never enter main/runtime. Its binding schedule is R0 (non-Bot system asset ABI
v2) → `v143=A1` clean-room bootstrap → `v144=A2` normal 5+3 → first immutable
v143/v144 rating snapshot → `v145=B` using that snapshot's actual parent.

R0 must add a system-owned external asset store/manifest+receipt v2, no-follow
resolver, broker-held query-only facade, launch parity across managed/native/
probe/precommit/official paths, binding/worker-load receipts and causal influence
gates. Current manifest v1 and the fixed `strict_v1` bootstrap cannot produce
A1 merely through a prompt: `v143=A1` also requires a fresh reviewed system
bootstrap blueprint, hash/receipt/probe binding and asset providers
`policy_logits` plus `value_lookup`. Implement R0 and the digest-bound initial
three generation-directive contract as a separate reviewed main change after
P1; only then use the stopped-checkout Git sync, v51 terminal reproof, recovery
diagnostics and a fresh `v143=A1` start. Any source sync/restart continues to
reset observation to **0/10**.

## Operator supersession — interim strict-v1 bootstrap is authorized before A1

The user subsequently authorized an explicit delivery-first exception: after
the P1 repair is merged and the stopped runtime has been safely synchronized,
the existing system-owned `strict_v1` first-strict blueprint may create
`national_v143` through the one-time `bootstrap-first-strict` control. This is
an **interim bootstrap Bot**, not A1. Its certificate/tag/identity and any
future native evidence remain its own; it receives no A1/A2/B research bytes,
assets, receipts, ratings, H2H rows, or capability claim.

This exception supersedes only the timing constraint in the preceding section.
It does not relax the exact five-file ABI, stopped-checkout execute receipt,
terminal-v51 reproof, dynamic runtime quality, native TCP precommit, one-time
bootstrap control, signed certificate, annotated tag, or fail-closed recovery
rules. Record the actual v143 identity before assigning a later A1 migration.
After v143 is published and the system is operating normally, A1 is introduced
as a fresh clean-room candidate under the reviewed R0/directive contract and
normal 5+3 certification; its version/parent selection must be recorded from
the then-current immutable state rather than relabeling v143. A source sync or
restart still resets the verified-observation counter to **0/10**.

## Latest superseding handoff — v53/v54/v55 P0 and checkpoint-free restart boundary

All wording above that describes v49--v51 as the latest runtime state is now
historical.  Runtime and `origin/main` are
`a0f63d5c13d53b216d690523c66ff91451908737`.  Web PID `2452378` and its
idle rating child PID `2452533` remain alive, but the in-process evolution task
is stopped.  The rating daemon is
`waiting_for_first_published_bot`; it has no match.  There is no checkpoint,
active generation, strict Bot, certificate, official job, Arena session,
rating/H2H row or strength sample.  Read-only recovery reports
`active=false/recoverable=true/issues=[]`; epoch is
`fresh_bootstrap_ready`, current/next authority is v142 → v143, the first
control is valid and unused 0/1, official doctor and evaluator identity are
green, and stability is **0/10**.

The next Bot is still the delivery-first interim strict-v1 `national_v143`
(Dashboard generation 1), not A1.  A1/A2/B and the open experiment plane remain
later clean-room work and do not delay this P0 recovery.  None of the
quarantined v53/v55 bytes may be restored or relabeled.

The three actual attempts are canonical terminal evidence:

- v53 ran 699.199 seconds, passed Quality including one 70-hand native
  candidate acceptance, and received Reviewer `approved=true`, score 9.  It
  then hit `SYSTEM_STRICT_BOOTSTRAP_REVIEW_RECEIPT_INVALID` because historical
  Master receipt reconstruction rejected the newly accepted legal `review`
  suffix.  Transaction `8c2e1cfa239b2d7171b38be4231af5a1fa1eec1f616c395fc086390234e69d76`
  canonically fenced and removed it.
- v54 ran 336.946 seconds and stopped before Worker.  Two Scouts were valid;
  mechanism schema retry invocation `3d23977734854c17b8a581c20a3db589`
  returned 5,567 characters: 930 characters of brace/bracket-free prose plus
  one complete 4,637-character JSON object through exact EOF.  Strict parsing
  rejected the mixed output, so proposal distinctness remained two and the
  canonical reason was
  `strict_authority_schema_retry_exhausted:proposal:mechanism`.  Transaction
  `7229deb13d5f280328158b63119709d68708851ee5c564294024be5b44f5e2da`
  closed it.
- v55 ran 744.328 seconds, again passed Quality and 70-hand candidate
  acceptance, and received Reviewer `approved=true`, score 7.  The same
  receipt replay bug caused canonical transaction
  `fae4251aa4dab823264d4089a5abbc2e45de5caf6f18f1ab2a70cb6d6c6a4348`.
  That was the third same-target abandon and stopped the outer scheduler.

Unique provider accounting across the three is $3.566307, 460,717 input and
117,028 output tokens.  v54 recovery projections duplicate two completed-call
cost fields and must not be counted twice.  The v53/v55 70-hand matches are
unpublished candidate-quality/compliance evidence only.  Without a published
artifact/opponent and immutable evaluation-cycle receipt, they have zero
strength, rating, H2H, selection and history-injection authority.

The P0 receipt repair keeps current receipt construction exact, while a stored
Master receipt may observe only the legal ordered later Review/Critic suffix
and a stored Review receipt only the later Critic.  Every accepted event still
revalidates effect/provider/receipt/role/context/revision.  The historical call
revalidates invocation evidence for its required gate; each permitted later
gate is separately revalidated by the corresponding current Review/Critic
helper, including that gate's invocation evidence.  Unknown, duplicate,
missing, reversed or drifted slots still fail closed.  The detached integration
contains that repair through `1c7e0709fc099bfc8ad500bc5d543b9f1462f0c5`,
but it is not merged or running.

The proposal repair keeps existing global parser behavior and appends the
provider-last raw-JSON emission gate.  Only if the global parser fails, and
only for a sealed proposal `SCHEMA RETRY` or `DISTINCTNESS RETRY`, may the
bounded fallback accept one unambiguous top-level object from a prefix with no
object/array delimiter; after the JSON value only JSON whitespace may remain.
Existing global-parser successes, including historical fenced/raw shapes,
remain compatible.  Initial Scouts, non-proposal roles, trailing non-whitespace
prose, multiple candidates, malformed JSON, semantic/distinctness failure and a
third attempt remain rejected.

The combined source remains pending final integration, full tests, independent
review, commit/push/merge and runtime sync.  Do not report a final green gate
from this handoff.  After the exact reviewed SHA is on `origin/main`, the only
safe order is:

1. re-read checkpoint/task/certification/Arena/native/rating state and require
   all empty/terminal; freeze the remote SHA;
2. from `.evolution_pok`, preflight the project interpreter and current static
   frontend receipt, then use `pokctl.sh stop`; require both old PIDs and port
   8000 gone and checkpoint still absent;
3. preserve `web/core/national_arena/storage_owner.lock` and the historical
   SQLite rows; fast-forward only with `git fetch --tags origin` and
   `git pull --ff-only --tags`; require `HEAD == origin/main`, `main`, and clean
   tracked/index state;
4. rerun checkpoint recovery, epoch/reset/blueprint/first-control validation,
   `scripts/evaluation_data_identity.py`, official doctor and static receipt.
   Any mismatch remains stopped; this P0 does not justify automatic evaluator
   rotation;
5. fully restart Web and its rating child with the controlled observe helper.
   `POST /api/control/start` alone is forbidden because the live Web interpreter
   cached the old strict/LLM modules;
6. require new process identities, exact merged HEAD, healthy running state and
   a fresh workflow identity.  Never replay v53--v55.  The restart begins the
   stability count again at **0/10**.

The checked-in ordered stage contract remains unchanged.  `timed_out` is a
canonical-abandon lease and `infra_timed_out` is a native-precommit-retry lease;
neither may be rendered as an unknown success-stage or used to bypass this
checkpoint-free restart boundary.

## Latest superseding handoff: workflow-v65 and Contract 42

The source/run state above has advanced. `origin/main` reached
`3d3162844e42cae72905e15d2a297c0dd2b0e93a`; the stopped autonomous checkout
was migrated through canonical Contract-40→41 abandon plus atomic empty
evaluation-identity rotation. Fresh `generation:143:workflow-v65` then ran
Master, Workers, Quality, Reviewer, advisory Critic and eight native 70-hand
precommit matches. It remains unpublished at `official_bootstrap_required`.

The operator 5+3 job
`b4575bb7163f551cb586f6391f728c1e6dc1671b11a279a4392504af8a4c7ebf`
naturally completed all eight rounds (2 pass / 6 old-contract fail), result
digest `fb7846b74c7c237226b99d2b4e8647c8b82ad9801917e59baceadd8d83424ce1`.
It produced no certificate/tag/rating and did not consume
`first_strict_control_v1`. Four failures are delimiter-free live-capture races:
the exact raw action awaited its bounded idle flush while the EXE had already
emitted the next street; finalized causal replay is clean. Two complete
70-hand failures prove official THP may contain the exact 3/4-card wire prefix
after called all-in rather than five public cards.

Contract 42 is the required successor. Only a same-connection raw action
awaiting its exact causal flush may project a provisional live warning;
legacy/source-less/finalized boundaries remain strict. Called-all-in THP must
be either the exact legal wire prefix (0/3/4) or a full five-card board with all
existing cross-wire/THP-action/blind/name/hole/earnings/state/footer bindings. Never infer
missing bytes/cards/actions. Workflow-v65, its 2 passed rounds and all gate
receipts remain immutable Contract-41 history. After source tests/review/merge,
use the dedicated v65 diagnosis and canonical abandon/quarantine, synchronize
only through `origin/main`, then fresh-materialize and rerun every gate plus a
new 5+3. Stability remains **0/10**.

The separate producer/consumer Slice 1 is an inert, independently reviewed
shadow branch `codex/producer-consumer-evolution-pipeline@214518888791761ff6d4a3319b97e02e5f10eb46`
with 134 focused tests. It must not be imported or activated by production
until durable Slice 2 journal/CAS/lease/resolver/migration work is separately
reviewed after first-Bot recovery.

## Latest superseding handoff: published v143 and recoverable v147

The first strict Bot is now published at canonical `national_v143` / annotated
`national-bot-v143`, with a signed official-full-v5 certificate covering eight
complete 70-hand executions and zero protocol issue. Official evidence remains
zero-strength. The next strategy workflow exposed two system-contract bugs:
v144 Scouts followed the rendered natural-language W/L/D uncertainty request
but the validator required a snake_case token; after v144 was governed-abandoned,
the evidence producer still hard-coded target v144, so v145/v146 were also
governed-abandoned. These labels are immutable abandon history and cannot be
reused or displayed as Bot generations.

Runtime is controlled-stopped at `generation:147:workflow-v1`, source v143,
stage `direction_audited`, revision 5, with recovery diagnostics green. The
same canonical v147 target must resume after the source repair; it is potential
Web generation 2 while preserving `national_v147` / `national-bot-v147`.
The repair must use one exact prompt/validator literal, accept any content- and
live-allocation-bound successor above v143, classify deterministic Master
authority defects as recovery-blocked rather than `master_llm`, and derive
ordinals from immutable paired publication history so abandon/reap never
renumbers a Bot. Since this checkpoint is already `direction_audited`, only the
pre-Master HEAD-drift route may accept the reviewed Master-contract change; it
must revalidate the exact target and all live authorities, and the subsequent
`master_planned` transition must refresh the repository baseline. Quality and
later stages retain unchanged-contract fail-closed behavior. Stability remains
**0/10** until the controlled restart.
