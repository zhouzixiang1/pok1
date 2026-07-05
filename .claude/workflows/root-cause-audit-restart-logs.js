export const meta = {
  name: 'root-cause-audit-restart-logs',
  description: '重启后日志深度根因审计：6个问题域 证据收集→最深根因→对抗验证',
  phases: [
    { title: 'DeepRootCause', detail: '6域并行：读日志+代码，追到最底层根因，判真bug' },
    { title: 'AdversarialVerify', detail: 'skeptic独立查证每根因，找更深根因/误判' },
  ],
}

const workflowCwd = process.cwd().replace(/\/$/, '')
const REPO_ROOT = workflowCwd.endsWith('/.claude/workflows')
  ? workflowCwd.slice(0, -'/.claude/workflows'.length)
  : workflowCwd

const BACKGROUND = [
  `项目：德州扑克 AI 自进化框架。仓库路径 ${REPO_ROOT}。`,
  '任务：审计【系统重启后】(2026-06-17 15:22 起) 的日志，结合全量代码，找出逻辑问题与 bug，排查到最底层根因（不是表面现象）。',
  '关键日志文件：',
  '  - web/logs/app.log (2.6MB, 重启后 15:22-至今, 主日志)',
  '  - web/core/results/system_events.jsonl (结构化事件, 字段 ts/type/severity/message/data)',
  '  - web/core/results/worker_failures.jsonl (worker/critic/reviewer 失败)',
  '  - web/logs/orchestrator_20260617_142022.txt (最新 orchestrator 会话 272KB)',
  '  - web/core/results/pipeline_state.json (当前 checkpoint 状态)',
  '背景事实：最近一次相关 commit 是 ecad199 (6/17 13:37) "fix(llm): enable adaptive thinking + harden signature/auth error handling"，',
  '  把 thinking 从 {"type":"disabled"} 改成 {"type":"adaptive"}，并加了 _run_stream_with_signature_retry (5次retry, 5/10/20/30s backoff) 作"安全网"。',
  '  该 commit 注释声称 adaptive "no longer co-triggers the SDK signature-field bug"。',
  '工作方法：用 Bash 的 grep/sed 和 Read 工具读真实日志与代码。必须引用具体的 file:line 和日志时间戳/行作为证据。',
  '判断标准：区分【真正的代码缺陷 bug】vs【正常设计行为 by-design】vs【误报】。给出最底层根因。',
].join('\n')

const FINDINGS_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    domain: { type: 'string' },
    evidence_log: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          timestamp: { type: 'string' },
          log_excerpt: { type: 'string' },
          interpretation: { type: 'string' },
        },
        required: ['timestamp', 'log_excerpt', 'interpretation'],
      },
    },
    log_origin_code: { type: 'string', description: '产生该日志/事件的代码位置 file:line' },
    root_cause_code: { type: 'string', description: '最底层根因的代码位置 file:line' },
    root_cause: { type: 'string', description: '最底层根因，不是表面现象；说明因果链' },
    is_real_bug: { type: 'boolean', description: 'true=真正代码缺陷，false=正常设计或误报' },
    bug_category: { type: 'string', description: 'regression|logic-error|missing-handling|misdiagnosis|by-design|false-positive' },
    impact: { type: 'string', description: '实际影响：token浪费/超时/状态错乱/进化误判等' },
    confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
    suggested_fix: { type: 'string' },
  },
  required: ['domain', 'root_cause', 'is_real_bug', 'bug_category', 'confidence', 'impact'],
}

const VERDICT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    domain: { type: 'string' },
    verdict: { type: 'string', enum: ['confirmed', 'refuted', 'partial'] },
    is_actually_bug: { type: 'boolean' },
    deeper_root_cause: { type: 'string', description: '若发现比原报告更深的根因，写出；否则填 none' },
    missed_evidence: { type: 'string', description: '原报告遗漏的证据；无则 none' },
    correction: { type: 'string', description: '原报告哪里判断错了；无则 none' },
    reasoning: { type: 'string' },
  },
  required: ['domain', 'verdict', 'is_actually_bug', 'deeper_root_cause', 'reasoning'],
}

const HYPOTHESES = [
  {
    key: 'signature-adaptive-thinking',
    prompt: [
      BACKGROUND,
      '',
      '=== 问题域 H1: signature 风暴的根因 — adaptive thinking 是否是元凶 ===',
      '现象：重启后日志中 "Missing required field in assistant message: \'signature\'" 错误大量、持续出现。',
      '日志证据（web/logs/app.log，grep "signature" 查看）：',
      '  - 22:03-22:06:53 CROSSOVER v111×v103→v115 连续 signature 错误 attempt 1-4，22:06:58 "Cycle timed out after 3600s — killing stuck session" (cycle 12 被强制kill)',
      '  - 22:11 CROSSOVER_COMPAT_111x102 signature 错误后1次retry恢复',
      '  - 22:14-22:26 CROSSOVER v111×v102→v115 2次signature错误后恢复',
      '  - 22:47 CROSSOVER v109×v102→v115 signature错误',
      '  - 22:56 MATCH ANALYST signature错误',
      '  - 22:34:33 / 22:55:15 "LLM infrastructure error (MessageParseError, NOT auth) — session cleared"',
      '代码：',
      '  - web/core/llm_query.py:135-184 (_run_stream_with_signature_retry, _SIGNATURE_MAX_ATTEMPTS=5)',
      '  - web/core/llm_query.py:253-259 (thinking={"type":"adaptive"} 启用点)',
      '  - web/core/orchestrator.py:113 (orchestrator 的 thinking adaptive)',
      '  - web/core/orchestrator.py:509-557 (infra error 清 session)',
      '  - web/core/llm_failure.py (is_llm_infra_error 分类)',
      '任务：',
      '1. 读 llm_query.py 完整，确认 thinking={"type":"adaptive"} 是否仍是 signature 错误的触发源（对比 ecad199 之前 thinking=disabled 时是否还有 signature 错误——可 grep 旧日志 app.log.1 或 git log 时间线推断）。',
      '2. 评估 _run_stream_with_signature_retry 这个"安全网"是否治标不治本：每次 signature 错误浪费多少时间(5/10/20/30s backoff)与token？为什么 cycle 12 跑满 3600s 被 kill？',
      '3. 判断：ecad199 启用 adaptive thinking 是否引入了回归？最底层根因是 adaptive thinking 配置本身，还是 SDK bug，还是 retry 策略不足？是否应该改回 disabled 或换 enabled+budget_tokens？',
      '4. 这是真 bug 还是已知/可接受的 transient？给出置信度。',
    ].join('\n'),
  },
  {
    key: 'infra-session-state-regression',
    prompt: [
      BACKGROUND,
      '',
      '=== 问题域 H2: infra error 后 session/状态机恢复缺陷 ===',
      '现象：signature infra error → _clear_orchestrator_session() → 下个 cycle fresh，但出现状态机倒退。',
      '日志证据（web/logs/app.log）：',
      '  - 22:32:00 "Illegal stage transition: workers_done -> direction_audited (backward_transition: workers_done -> direction_audited). Allowing but logging."',
      '  - 22:34:33 / 22:55:15 "LLM infrastructure error (MessageParseError) — session cleared, next cycle starts fresh"',
      '当前 pipeline_state.json: next_v=115, source_v=114, stage=direction_audited (本应前进到 master_planned/workers_done，却停在 direction_audited)',
      '代码：',
      '  - web/core/orchestrator.py:509-557 (except handler: infra 时 _clear_orchestrator_session，但不重置 checkpoint stage)',
      '  - web/core/evolution_infra.py (STAGE_ORDER, STAGE_GATE_ALLOWLIST, write_pipeline_checkpoint, 状态机迁移校验)',
      '  - web/core/orchestrator_session.py (session 持久化)',
      '任务：',
      '1. 追踪 infra error 清 session 后，checkpoint(pipeline_state.json) 的 stage 是否被正确重置？为什么会出现 workers_done→direction_audited 倒退？',
      '2. "Allowing but logging" 这个处理是否是 bug——倒退是否破坏了 stage gate 的不变量？会导致什么后果（worker output 丢失/重复执行/master plan 丢失）？',
      '3. 最底层根因：infra error 清 session 但不清/不重置 checkpoint，导致新 cycle 与旧 stage 冲突。这是真 bug 吗？',
      '4. 读 evolution_infra.py 找状态机迁移校验逻辑，确认倒退被允许的代码路径。',
    ].join('\n'),
  },
  {
    key: 'redundant-tool-call-loop',
    prompt: [
      BACKGROUND,
      '',
      '=== 问题域 H3: orchestrator 重复工具调用循环，检测却无缓解 ===',
      '现象：orchestrator 在一个 cycle 内反复调用同一工具（Bash 9x, TaskCreate 6x, TaskUpdate 3x），检测逻辑只 log warning 不 break。',
      '日志证据（web/logs/app.log）：',
      '  - 22:32:31 "Tool Bash called 2 times (possible redundant call)"',
      '  - 22:38:32-34 Bash 连续 2x, 3x',
      '  - 22:54:48 TaskUpdate 2x, 3x',
      '  - 22:54:58 "Tool Bash called 9 times (possible redundant call)"',
      '  - system_events: redundant_tool_call 多条',
      '代码：',
      '  - web/core/orchestrator.py:160-179 (ToolUseBlock 计数 + redundant 检测，只 log_system_event 不 break/abandon)',
      '任务：',
      '1. 为什么 orchestrator 会反复调用同一工具？读 orchestrator_20260617_142022.txt 看实际调用序列与上下文。是 adaptive thinking 让 orchestrator 更啰嗦，还是 session clear 后状态混乱让它反复重试同一检查？',
      '2. 检测到 N 次重复调用后，没有任何缓解措施（不 break, 不 abandon, 不告警升级），让 cycle 一直烧到 3600s timeout。这是 bug 吗？',
      '3. 最底层根因：是 H1(signature)→H2(session clear)→H3(循环) 连锁的一环，还是独立的 orchestrator 决策 bug？',
      '4. 评估 token 浪费量级（每次工具调用的 in/out token，从 llm_costs.jsonl 或日志 [COST] 行估算）。',
    ].join('\n'),
  },
  {
    key: 'prepare-cross-source-refused',
    prompt: [
      BACKGROUND,
      '',
      '=== 问题域 H4: prepare_cross_source_refused 状态衔接断裂 ===',
      '现象：crossover v109×v102→v115 成功后，v115 目录从 v109 准备；但 pipeline_state source_v=114，下次 prepare_next_gen(114) 拒绝覆盖。',
      '日志证据（system_events.jsonl）：',
      '  - "Refusing to overwrite v115: dir was prepared from v109 but request is from v114. Call abandon_generation first to clear it."',
      '  - 此前: "Crossover v109×v102 → v115 succeeded" 和 "Crossover v111×v102 → v115 succeeded"',
      '当前 pipeline_state.json: next_v=115, source_v=114, stage=direction_audited (但 dir 实际从 v109 crossover 准备)',
      '代码：',
      '  - web/core/tool_gates.py:384-409 (prepare_next_gen 的 cross-source guard + prior_source 检查)',
      '  - web/core/tool_commit.py (crossover 工具: 成功后写 checkpoint 时 source_v 设的是什么？parent_v/parent2_v 如何记录？)',
      '  - web/core/tool_gates.py prepare_next_gen 与 abandon_generation 的衔接',
      '任务：',
      '1. crossover 成功后，checkpoint 的 source_v 应该设成什么（crossover 的 base parent，如 v109/v111，还是 source_v=114）？读 tool_commit.py 的 crossover 实现确认。',
      '2. 为什么会出现 source_v=114 但 dir 从 v109 准备的不一致？是 crossover 没更新 source_v，还是 master/prepare 用了错误的 source_v？',
      '3. 这个 guard 本身是正确的（防 v107 silent overwrite），但它暴露了什么上游状态衔接 bug？最底层根因。',
      '4. 当前 v115 目录内容来自哪个 base（v109? v111? v114?）？读 bots/claude_v115/ 的 strategy.py 头部或 git 确认。这个不一致会导致用错误的 base 进化吗？',
    ].join('\n'),
  },
  {
    key: 'h2h-anomaly-v114',
    prompt: [
      BACKGROUND,
      '',
      '=== 问题域 H5: H2H anomaly v114 是真实回归还是误报 ===',
      '现象：system_events 报 "H2H anomalies for v114: 8 matchups deviate >15%"（从5增到8）。',
      '初步数据（head_to_head.json, v114 matchups games>=5）：',
      '  vs v77 WR=0.750(20g) +0.25, vs v50 WR=0.733(30g) +0.23, vs v14 WR=0.700(30g) +0.20, vs v25 WR=0.700(20g) +0.20',
      '  vs v104 WR=0.350(20g) -0.15, vs v96 WR=0.367(30g) -0.13, vs v89 0.400, vs v110 0.400, vs v92 0.400, vs v107 0.400',
      '代码：',
      '  - web/core/generation_scheduler.py:213-244 (anomaly 检测: games>=20 AND |wr-0.5|>0.15，不区分正负向)',
      '任务：',
      '1. v114 的 anomaly 大多是【正向】（v114 赢 v77/v50/v14/v25）。anomaly 检测把"v114 强势碾压弱bot"也当 anomaly 告警，注入 Master 上下文要求"attention"。这是否是逻辑错误——碾压弱对手不该被当问题？',
      '2. 对比 v113/v111 的 H2H（从 head_to_head.json），v114 是否真实回归？还是整体rating正常只是 matchup 方差？',
      '3. 8 个 anomaly 里真正负向(回归)的有几个？告警阈值 games>=20 + |wr-0.5|>0.15 在 29 个 matchup 下统计上必然命中几个，是否阈值设计不当导致持续误报？',
      '4. 最底层判断：这是真 bug（检测逻辑错/误导进化方向）还是 by-design 的合理告警？',
    ].join('\n'),
  },
  {
    key: 'fix-injection-skip',
    prompt: [
      BACKGROUND,
      '',
      '=== 问题域 H6: fix_injection 跳过 BOT-001a/002a/004 是否误跳 ===',
      '现象：每次 prepare 都 "Skipped fixes: BOT-001a, BOT-002a, BOT-004"（from v111 和 v109 都跳过）。',
      'fix 定义（web/core/fix_injection.py:40-100）：',
      '  - BOT-001a: card_utils.py wheel straight (A-2-3-4-5), guard="{14, 2, 3, 4, 5}"',
      '  - BOT-002a: state.py re-raise min strictly >2x, guard="2 * last_raise_to + 1 - my_round_bet"',
      '  - BOT-004: constants.py TOTAL_HANDS=70, guard="TOTAL_HANDS = 70"',
      'apply逻辑（fix_injection.py:121-174）：guard 存在则跳过(idempotency)；search string 找不到也跳过。',
      '任务：',
      '1. 读 bots/claude_v111/ 和 bots/claude_v109/ (以及 v114) 的 card_utils.py / state.py / constants.py，确认这三个 fix 的 guard 是否已存在（=已应用=正常跳过），还是 search string 找不到(=模板过时=误跳)。',
      '2. 特别注意：BOT-001a/002a/004 是 critical fix（wheel牌型错误/re-raise非法/TOTAL_HANDS=50bug）。如果误跳，bot 会带致命 bug 进化。必须确认。',
      '3. 若 guard 已存在=已修复=正常跳过，则 H6 是 by-design 非 bug。若 search 找不到=误跳，则 H6 是真 bug。',
      '4. 最底层判断 + 置信度。',
    ].join('\n'),
  },
]

function verifyPrompt(h, finding) {
  return [
    BACKGROUND,
    '',
    '=== 对抗验证任务: ' + h.key + ' ===',
    '一个 agent 对问题域 "' + h.key + '" 给出了根因分析报告（见下）。你的任务是【独立查证】，默认怀疑，找出它的错误或遗漏。',
    '',
    '原始报告 JSON:',
    JSON.stringify(finding, null, 2),
    '',
    '对抗验证要求：',
    '1. 独立用 grep/Read 读相关日志与代码，核实报告的 evidence_log、log_origin_code、root_cause_code 是否真实存在且引用正确。',
    '2. 判断 root_cause 是否真的是【最底层根因】，还是只是表面现象？有没有更深的根因报告没发现？（deeper_root_cause 字段写出）',
    '3. 判断 is_real_bug 是否正确？报告有没有把 by-design/误报误判成 bug，或把真 bug 漏判成 by-design？',
    '4. 检查报告是否遗漏关键证据（missed_evidence），或因果链是否断裂（correction）。',
    '5. 特别警惕连锁关系：H1(signature)→H2(state倒退)→H3(工具循环)→H4(source不一致) 是否真的是同一根因链，还是各自独立？',
    '6. verdict: confirmed(根因成立)/refuted(根因错)/partial(部分成立)。',
    '',
    h.prompt.split('===')[1] || '',
  ].join('\n')
}

phase('DeepRootCause')
log('启动 6 个问题域并行根因深挖 + 对抗验证 (pipeline)')
const results = await pipeline(
  HYPOTHESES,
  (h) => agent(h.prompt, { label: 'dig:' + h.key, phase: 'DeepRootCause', schema: FINDINGS_SCHEMA }),
  (finding, h) => agent(verifyPrompt(h, finding), { label: 'verify:' + h.key, phase: 'AdversarialVerify', schema: VERDICT_SCHEMA })
    .then(v => ({ hypothesis: h.key, finding: finding, verdict: v }))
)

const clean = results.filter(Boolean)
log('完成：' + clean.length + '/' + HYPOTHESES.length + ' 问题域根因+验证')

// 汇总：按 is_real_bug 分组，标注对抗验证结论
const confirmed = clean.filter(r => r.verdict && r.verdict.is_actually_bug)
const byDesign = clean.filter(r => r.verdict && !r.verdict.is_actually_bug)
return {
  summary: {
    total_domains: clean.length,
    confirmed_real_bugs: confirmed.length,
    by_design_or_false_positive: byDesign.length,
    confirmed_domains: confirmed.map(r => r.hypothesis),
    non_bug_domains: byDesign.map(r => r.hypothesis),
  },
  confirmed_bugs: confirmed.map(r => ({
    domain: r.hypothesis,
    root_cause: r.finding.root_cause,
    root_cause_code: r.finding.root_cause_code,
    impact: r.finding.impact,
    confidence: r.finding.confidence,
    suggested_fix: r.finding.suggested_fix,
    adversarial_verdict: r.verdict.verdict,
    deeper_root_cause: r.verdict.deeper_root_cause,
  })),
  non_bugs: byDesign.map(r => ({
    domain: r.hypothesis,
    original_claim: r.finding.root_cause,
    why_not_bug: r.verdict.reasoning,
    correction: r.verdict.correction,
  })),
  full_findings: clean,
}
