import type {
  ControlHealth,
  ControlStatus,
  DraftGeneration,
  PipelineRoute,
} from "../api/control.js";
import {
  controlPipelineBlocked,
  controlPipelineIssues,
  controlStartBlockedReason,
  draftGenerations,
  primaryGenerationSlot,
} from "../api/control.js";
import {
  PIPELINE_TIMEOUT_LEASES,
  PIPELINE_STAGE_CONTRACT,
  STAGE_LABELS,
  isPipelineTimeoutLeaseStage,
  pipelineStageProgress,
  type PipelineStage,
} from "../constants/pipeline.js";

export type OperatorSituationTone = "success" | "info" | "warning" | "error" | "neutral";

export interface OperatorSlotBadge {
  slot: "primary" | "draft";
  label: string;
  detail: string;
}

export interface OperatorSituationView {
  tone: OperatorSituationTone;
  headline: string;
  what: string;
  why: string;
  next: string;
  manualRequired: boolean;
  manualLabel: string;
  manualDetail: string;
  continuityNote: string | null;
  technical: Array<{ label: string; value: string }>;
  /** Dual-slot badges (primary + optional drafts); empty when no active slots. */
  slotBadges: OperatorSlotBadge[];
  /** Slice2b park / eval_wait / staging-parent tips; never inferred from logs. */
  contextNotes: string[];
}

type OperatorSituationCore = Omit<OperatorSituationView, "slotBadges" | "contextNotes">;

const TOOL_LABELS: Record<string, string> = {
  run_direction_audit: "重新核对研发方向",
  run_master: "让 Master 重新生成并裁决方案",
  run_workers: "执行代码实现任务",
  run_quality: "运行代码与策略质量检查",
  run_review: "执行独立代码审核",
  run_critic: "执行建议性 Critic",
  run_precommit_eval: "运行原生 TCP 预发布评测",
  run_official_certification: "运行官方平台认证",
  run_commit: "签名并发布 Bot",
  run_archivist: "完成发布收尾并准备下一代",
  abandon_generation: "按权威收据结束本次尝试并创建继任尝试",
};

const OPERATOR_ACTION_LABELS: Record<string, string> = {
  execute_policy_epoch_reset: "执行一次性策略 epoch 初始化",
  inspect_policy_epoch_reset_evidence: "检查策略 epoch 初始化证据",
  inspect_strict_version_authority: "检查版本权威",
  inspect_epoch_authority: "检查 epoch 权威",
  inspect_runtime_reconciliation_claim: "检查运行态恢复声明",
  archive_incompatible_checkpoint: "归档不兼容 checkpoint",
  complete_runtime_reconciliation: "完成运行态恢复",
  quarantine_legacy_ledger_and_abandon_checkpoint: "隔离旧账本并受控放弃 checkpoint",
  operator_reconcile_checkpoint: "由操作员核对 checkpoint",
  finalize_recorded_abandon_checkpoint: "完成已记录的受控放弃",
  run_first_strict_official_certification: "启动首个 Bot 的官方平台认证",
};

function stageLabel(stage: string | null | undefined): string {
  if (!stage) return "等待下一项工作";
  if (isPipelineTimeoutLeaseStage(stage)) return PIPELINE_TIMEOUT_LEASES[stage].label;
  return (STAGE_LABELS as Record<string, string>)[stage] ?? `未识别阶段 ${stage}`;
}

function toolLabel(tool: string | null | undefined): string {
  if (!tool) return "等待调度器给出下一动作";
  return TOOL_LABELS[tool] ?? `执行 ${tool}`;
}

function cleanReason(value: unknown): string | null {
  if (typeof value !== "string" || value.trim().length === 0) return null;
  const text = value.trim();
  const timeout = text.match(/LLM stall timeout after ([0-9.]+)s/i);
  if (timeout) return `模型调用在 ${timeout[1]} 秒内未返回合格结构化结果`;
  return text.length > 220 ? `${text.slice(0, 217)}…` : text;
}

function infraDetails(route: PipelineRoute | null | undefined): {
  component: string | null;
  attempt: number | null;
  max: number | null;
  reason: string | null;
} {
  const failure = route?.infra_failure;
  if (!failure || typeof failure !== "object") {
    return { component: null, attempt: null, max: null, reason: null };
  }
  const issues = Array.isArray(failure.issues) ? failure.issues : [];
  return {
    component: typeof failure.component === "string" ? failure.component : null,
    attempt: typeof failure.attempt === "number" ? failure.attempt : null,
    max: typeof failure.max_attempts === "number" ? failure.max_attempts : null,
    reason: cleanReason(issues[0]) ?? cleanReason(failure.code),
  };
}

function componentLabel(component: string | null): string {
  const labels: Record<string, string> = {
    master_llm: "Master 方案生成",
    reviewer_llm: "Reviewer 审核",
    critic_llm: "Critic 建议",
    worker_llm: "Worker 实现",
    native_precommit: "原生 TCP 预发布评测",
  };
  return component ? (labels[component] ?? component) : "当前步骤";
}

function successorNote(status: ControlStatus): string | null {
  const stability = status.stability_observation;
  if (stability?.last_reset_reason !== "generation_abandoned") return null;
  const details = stability.last_reset_details;
  const previous = details && typeof details.workflow_run_id === "string"
    ? details.workflow_run_id
    : null;
  const current = status.active_generation?.workflow_run_id ?? null;
  const abandonedVersion = details && typeof details.abandoned_v === "number"
    ? details.abandoned_v
    : null;
  const currentVersion = status.active_generation?.canonical_version ?? null;
  if (
    !previous
    || !current
    || previous === current
    || abandonedVersion == null
    || abandonedVersion !== currentVersion
  ) return null;
  return `上一工作流 ${previous} 已按权威记录结束；当前 ${current} 是同一用户代次的新尝试。失败尝试没有被算作已发布 Bot 或强度证据。`;
}

function technicalRows(status: ControlStatus | null, health: ControlHealth | null): Array<{ label: string; value: string }> {
  const active = primaryGenerationSlot(status) ?? status?.active_generation ?? null;
  const route = health?.pipeline?.route;
  const drafts = status ? draftGenerations(status) : [];
  const rows = [
    { label: "用户代次 / 真实标签", value: active ? `第 ${active.generation_ordinal} 代 / ${active.canonical_tag}` : "—" },
    { label: "workflow", value: active?.workflow_run_id ?? "—" },
    { label: "raw stage", value: active?.stage ?? health?.pipeline?.stage ?? "—" },
    { label: "route", value: route ? `${route.intent} → ${route.next_tool ?? "none"}` : "—" },
    { label: "checkpoint revision", value: String(active?.checkpoint_revision ?? health?.pipeline?.checkpoint_revision ?? "—") },
  ];
  if (drafts.length > 0) {
    rows.push({
      label: "draft slots",
      value: drafts.map((d) => `v${d.next_v}@${d.stage}`).join(", "),
    });
  }
  if (status?.pipeline_mode?.enabled) {
    rows.push({
      label: "pipeline_mode",
      value: [
        status.pipeline_mode.consumer_parked ? "consumer_parked" : "consumer_active",
        `in_flight=${status.pipeline_mode.in_flight_count}`,
      ].join(" · "),
    });
  }
  return rows;
}

function buildSlotBadges(status: ControlStatus | null): OperatorSlotBadge[] {
  if (!status) return [];
  const badges: OperatorSlotBadge[] = [];
  const primary = primaryGenerationSlot(status);
  if (primary) {
    badges.push({
      slot: "primary",
      label: `主槽 v${primary.next_v}`,
      detail: stageLabel(primary.stage),
    });
  }
  for (const draft of draftGenerations(status)) {
    badges.push({
      slot: "draft",
      label: `草稿 v${draft.next_v}`,
      detail: stageLabel(draft.stage),
    });
  }
  return badges;
}

function buildContextNotes(status: ControlStatus | null): string[] {
  if (!status) return [];
  const notes: string[] = [];
  // NOTE: Slice 2b consumer_park and eval_wait "not stuck" copy are owned by
  // PhaseAProjectionStrip (it renders the compact badges + the notStuckReasons
  // tips on every page, including BotManager which has no OperatorSituation).
  // Duplicating them here stacked two different Chinese wordings of the same
  // facts; only keep the notes unique to the operator situation below.
  const mode = status.pipeline_mode;
  if (mode?.enabled && mode.producer_may_prepare_next) {
    notes.push("并行验收已开启：已密封候选允许后台提前准备下一代草稿。");
  }

  const flags = status.feature_flags;
  const primary = primaryGenerationSlot(status) ?? status.active_generation;
  const sourceV = primary?.source_v ?? null;
  if (flags?.staging_as_parent && sourceV != null) {
    const certified = status.version_authority?.certified_versions ?? [];
    if (!certified.includes(sourceV)) {
      notes.push(
        `允许暂存父本：当前主父本 v${sourceV} 尚未取得正式证书；发布权威仍以证书/标签为准。`,
      );
    } else {
      notes.push(`允许暂存父本：当前主父本 v${sourceV} 已有正式认证身份。`);
    }
  }

  return notes;
}

function withPhaseDContext(
  core: OperatorSituationCore,
  status: ControlStatus | null,
): OperatorSituationView {
  return {
    ...core,
    slotBadges: buildSlotBadges(status),
    contextNotes: buildContextNotes(status),
  };
}

function unavailable(
  headline: string,
  what: string,
  why: string,
  next: string,
  manualDetail: string,
  status: ControlStatus | null,
  health: ControlHealth | null,
): OperatorSituationView {
  return withPhaseDContext({
    tone: "error",
    headline,
    what,
    why,
    next,
    manualRequired: true,
    manualLabel: "需要检查",
    manualDetail,
    continuityNote: null,
    technical: technicalRows(status, health),
  }, status);
}

function draftPrepareHint(drafts: DraftGeneration[]): string | null {
  if (drafts.length === 0) return null;
  return `草稿槽并行：${drafts.map((d) => `v${d.next_v}（${stageLabel(d.stage)}）`).join("；")}`;
}

/**
 * Convert the paired control status/health authority into operator language.
 * It never infers progress from logs, directories, or the evolution text SSE.
 */
export function operatorSituationView(
  status: ControlStatus | null | undefined,
  health: ControlHealth | null | undefined,
): OperatorSituationView {
  const s = status ?? null;
  const h = health ?? null;
  if (!s) {
    return unavailable(
      "无法确认进化状态",
      "控制状态尚未返回。",
      "缺少 /api/control/status 权威投影。",
      "等待刷新；若持续缺失，检查 Web 服务。",
      "当前不要根据旧日志判断系统是否在运行。",
      s,
      h,
    );
  }
  if (!h) {
    return unavailable(
      "无法确认进化健康",
      "已取得状态，但没有与之配对的健康快照。",
      "缺少 /api/control/health，无法证明 route、进程和恢复边界。",
      "等待下一次配对刷新；若持续缺失，检查 Web 服务。",
      "当前不要启动、停止或手工修改 checkpoint。",
      s,
      h,
    );
  }

  const active = primaryGenerationSlot(s) ?? s.active_generation;
  const drafts = draftGenerations(s);
  const pipeline = h.pipeline;
  const route = pipeline.route ?? null;
  const continuityNote = successorNote(s);
  const technical = technicalRows(s, h);
  const consumerParked = Boolean(s.pipeline_mode?.enabled && s.pipeline_mode.consumer_parked);

  if (!s.epoch_initialized) {
    const action = s.operator_action;
    return withPhaseDContext({
      tone: "warning",
      headline: "严格进化尚未初始化",
      what: "当前没有可运行的 National TCP 严格代次。",
      why: "一次性 epoch 或版本权威尚未完成验证。",
      next: action ? (OPERATOR_ACTION_LABELS[action] ?? action) : "等待权威恢复诊断给出动作。",
      manualRequired: true,
      manualLabel: "需要操作员",
      manualDetail: "只执行控制面给出的受控命令，不手工删除 checkpoint 或状态文件。",
      continuityNote,
      technical,
    }, s);
  }

  const transition = s.operator_transition;
  const transitionMatches = Boolean(
    transition
    && transition.kind === "first-strict-official-operator-transition"
    && transition.certification_profile === "first_strict_control_v1"
    && transition.opponent_authority === "system_control"
    && transition.strength_evidence_weight === 0
    && transition.strategy_evidence_weight === 0
    && active
    && transition.workflow_run_id === active.workflow_run_id
    && transition.candidate_version === active.next_v
    && transition.source_v === active.source_v
    && transition.checkpoint_stage === active.stage
    && transition.checkpoint_revision === active.checkpoint_revision
    && /^[0-9a-f]{64}$/.test(transition.transition_digest),
  );
  if (transition && transitionMatches) {
    const state = transition.state;
    const running = state === "bootstrap_running";
    const failed = state === "bootstrap_failed";
    const ready = state === "ready_to_finalize";
    return withPhaseDContext({
      tone: failed ? "error" : running ? "info" : ready ? "success" : "warning",
      headline: failed
        ? "首代官方认证没有形成可发布结果"
        : ready
          ? "首代官方证书已验证，等待完成发布"
          : running
            ? "首个 Bot 正在做官方平台认证"
            : "首个 Bot 等待操作员启动官方认证",
      what: failed
        ? "绑定当前候选的系统控制台认证任务已终态失败；候选尚未发布。"
        : ready
          ? "8 个认证回合已闭合并形成当前候选的有效证书；仍未完成提交、.completed 与标签。"
          : running
            ? "首代一次性任务正在执行 5 轮自对弈 + 3 轮系统控制台对手认证。"
            : "本地质量和原生预发布门已完成，首代候选停在一次性官方认证边界。",
    why: "首个严格 Bot 没有合格已发布对手，第三方对手的 3 轮由一次性系统控制台提供；它只证明官方兼容，强度与策略证据权重均为 0。",
      next: failed
        ? "按 transition 给出的受控命令处理失败；不要自动降级或复用旧任务。"
        : ready
          ? "由操作员执行绑定该证书的完成发布命令。"
          : running
            ? "等待全部 8 个 70 手认证回合终态。"
            : "由操作员启动 transition 给出的只读认证命令。",
      manualRequired: !running,
      manualLabel: running ? "无需人工" : "需要操作员",
      manualDetail: running
        ? "不要重复启动或取消认证。"
        : "只执行 transition 中内容绑定的命令；Official 结果不能替代 native 强度评估。",
      continuityNote,
      technical,
    }, s);
  }

  if (s.operator_action) {
    const actionLabel = OPERATOR_ACTION_LABELS[s.operator_action] ?? s.operator_action;
    const firstStrictCertification = s.operator_action === "run_first_strict_official_certification";
    return withPhaseDContext({
      tone: "warning",
      headline: firstStrictCertification ? "首个 Bot 等待操作员启动官方认证" : "系统正在等待操作员动作",
      what: active ? `第 ${active.generation_ordinal} 代已推进到“${stageLabel(active.stage)}”，自动流程暂时停在安全边界。` : "自动流程停在受控边界。",
      why: firstStrictCertification
        ? "首代没有合格已发布对手，因此执行 5 轮自对弈 + 3 轮一次性系统控制台对手认证；它证明官方兼容，但强度与策略证据权重均为 0。"
        : `后端明确要求：${actionLabel}。`,
      next: s.operator_command ? "核对并执行后端给出的操作员命令。" : actionLabel,
      manualRequired: true,
      manualLabel: "需要操作员",
      manualDetail: firstStrictCertification
        ? "只启动绑定当前 workflow 的只读任务；不要把 Official 结果当 native 强度，也不要重复提交。"
        : "完成动作并取得新健康快照后，编排器才会继续。",
      continuityNote,
      technical,
    }, s);
  }

  if (controlPipelineBlocked(pipeline)) {
    const issues = controlPipelineIssues(pipeline);
    return withPhaseDContext({
      tone: "error",
      headline: "流水线恢复被阻断",
      what: active ? `第 ${active.generation_ordinal} 代停在“${stageLabel(active.stage)}”。` : "当前没有可安全执行的下一步。",
      why: issues.length > 0 ? issues.join("；") : "健康投影没有证明恢复路径。",
      next: "先解决权威或身份诊断，再使用后端允许的恢复动作。",
      manualRequired: true,
      manualLabel: "需要操作员",
      manualDetail: "不要绕过权威路径、复制候选或手工推进阶段。",
      continuityNote,
      technical,
    }, s);
  }

  if (active && isPipelineTimeoutLeaseStage(active.stage)) {
    const lease = PIPELINE_TIMEOUT_LEASES[active.stage];
    const automatic = s.running && route?.next_tool === lease.nextTool;
    return withPhaseDContext({
      tone: "warning",
      headline: lease.label,
      what: `第 ${active.generation_ordinal} 代进入超时恢复状态；这不是成功进度，也不是未知阶段。`,
      why: lease.description,
      next: automatic ? `${toolLabel(lease.nextTool)}；当前编排器会按既定路径自动执行。` : `${toolLabel(lease.nextTool)}。`,
      manualRequired: !automatic,
      manualLabel: automatic ? "无需人工" : "需要操作员",
      manualDetail: automatic ? "观察恢复结果即可，不要重放原阶段。" : "先恢复编排器，再只走权威路径。",
      continuityNote,
      technical,
    }, s);
  }

  if (active && route?.intent === "infra_retry") {
    const infra = infraDetails(route);
    const currentAttempt = infra.attempt;
    const nextAttempt = currentAttempt != null ? currentAttempt + 1 : null;
    const attemptText = nextAttempt != null && infra.max != null ? `第 ${nextAttempt}/${infra.max} 次` : "下一次";
    const automatic = s.running && route.action === "retry_same_tool";
    return withPhaseDContext({
      tone: "warning",
      headline: `${componentLabel(infra.component)}正在局部重试`,
      what: `第 ${active.generation_ordinal} 代仍停在已落盘的“${stageLabel(active.stage)}”边界；候选与已完成步骤保持不变。`,
      why: infra.reason ?? "当前工具发生可重试的基础设施故障，不属于策略质量拒绝。",
      next: `${attemptText}${toolLabel(route.next_tool)}。`,
      manualRequired: !automatic,
      manualLabel: automatic ? "无需人工" : "需要操作员",
      manualDetail: automatic
        ? "编排器会在同一阶段重试；达到上限后才会受控放弃并创建继任尝试。"
        : "编排器当前未运行；恢复运行后只执行权威路径指定的工具。",
      continuityNote,
      technical,
    }, s);
  }

  if (active?.stage === "official_bootstrap_required") {
    return withPhaseDContext({
      tone: "warning",
      headline: "首个 Bot 已到官方认证边界",
      what: "本地合规、代码门和原生 TCP 预发布评测已完成；候选尚未发布。",
      why: "首代必须由操作员启动一次性系统控制台 5+3；它只证明官方兼容性，强度权重为 0。",
      next: "等待后端发布操作员交接指令，并由操作员启动认证。",
      manualRequired: true,
      manualLabel: "需要操作员",
      manualDetail: "不要把官方结果当作原生强度，也不要自动降级为首代引导。",
      continuityNote,
      technical,
    }, s);
  }

  if (active && consumerParked) {
    const draftHint = draftPrepareHint(drafts);
    return withPhaseDContext({
      tone: "info",
      headline: `第 ${active.generation_ordinal} 代主槽旁路等待（非卡住）`,
      what: `${active.canonical_bot_name} 已密封；后台质量门链正在并行验收候选（质量→评审→批判→预提交），主槽故意停在“${stageLabel(active.stage)}”。`,
      why: "这是并行验证的设计态（后台验收候选、主槽提前准备下一代），不是恢复阻断或失败。",
      next: draftHint ?? "等待后台验收完成，在发布晋升屏障通过后继续发布。",
      manualRequired: !s.running,
      manualLabel: s.running ? "无需人工" : "需要操作员",
      manualDetail: s.running
        ? "观察后台验收与草稿槽即可；不要把旁路等待当成卡死。"
        : (controlStartBlockedReason(s, h) ?? "可在控制面板恢复编排器。"),
      continuityNote,
      technical,
    }, s);
  }

  if (active) {
    const nextAction = toolLabel(route?.next_tool);
    const healthy = h.overall === "healthy" && s.running;
    const contractStage = (PIPELINE_STAGE_CONTRACT as readonly string[]).includes(active.stage)
      ? active.stage as PipelineStage
      : null;
    const progress = contractStage ? pipelineStageProgress(contractStage) : null;
    const completedBoundary = progress?.kind === "completed_boundary";
    const draftHint = draftPrepareHint(drafts);
    return withPhaseDContext({
      tone: healthy ? "info" : "warning",
      headline: completedBoundary
        ? `第 ${active.generation_ordinal} 代已完成“${stageLabel(active.stage)}”边界`
        : `第 ${active.generation_ordinal} 代正在处理“${stageLabel(active.stage)}”`,
      what: completedBoundary
        ? `${active.canonical_bot_name} 已把该边界写入 checkpoint；下一工具尚未完成，Bot 仍未发布。`
        : `${active.canonical_bot_name} 尚在生产与验收流程中，未计为已发布 Bot。`,
      why: route?.directive || "当前阶段与健康路由已配对。",
      next: draftHint ? `${nextAction}；${draftHint}` : nextAction,
      manualRequired: !s.running,
      manualLabel: s.running ? "无需人工" : "需要操作员",
      manualDetail: s.running ? "系统会自动推进；只在出现明确需要操作员动作的提示时介入。" : (controlStartBlockedReason(s, h) ?? "可在控制面板恢复编排器。"),
      continuityNote,
      technical,
    }, s);
  }

  if (s.post_publication_handoff.status !== "none") {
    const blocked = s.post_publication_handoff.blocked;
    return withPhaseDContext({
      tone: blocked ? "error" : "info",
      headline: blocked ? "发布收尾被阻断" : "Bot 已发布，正在完成收尾",
      what: "提交、证书与 tag 已形成，系统正在归档并交接下一代。",
      why: blocked ? (s.post_publication_handoff.issues.join("；") || "handoff 权威未通过") : "发布后的清理必须完成，下一代才能取得准备权。",
      next: blocked ? "检查 handoff 身份和 owner，再执行允许的恢复动作。" : "等待归档完成，调度器随后准备下一代。",
      manualRequired: blocked,
      manualLabel: blocked ? "需要操作员" : "无需人工",
      manualDetail: blocked ? "不要跳过 handoff 或直接准备下一代。" : "系统会自动交接。",
      continuityNote,
      technical,
    }, s);
  }

  const schedulerReady = pipeline.scheduler_boundary?.state === "ready_to_prepare";
  if (schedulerReady && s.running) {
    return withPhaseDContext({
      tone: "success",
      headline: "上一工作单元已结束，正在准备下一代",
      what: "当前没有活跃 checkpoint；外层调度器持有下一代准备权。",
      why: "发布后 handoff 已清空，启动边界已由 health.pipeline 证明。",
      next: "自动创建下一代 checkpoint。",
      manualRequired: false,
      manualLabel: "无需人工",
      manualDetail: "不要手工创建 Bot 目录或重算版本号。",
      continuityNote,
      technical,
    }, s);
  }

  const blockedReason = controlStartBlockedReason(s, h);
  return withPhaseDContext({
    tone: blockedReason ? "warning" : "neutral",
    headline: s.running ? "编排器正在等待下一项工作" : "进化系统当前已停止",
    what: "当前没有活跃代次。",
    why: blockedReason ?? "启动边界已满足，但尚未启动。",
    next: blockedReason ? "先处理启动边界诊断。" : "可从控制面板启动编排器。",
    manualRequired: !s.running,
    manualLabel: s.running ? "无需人工" : "需要操作员",
    manualDetail: s.running ? "等待调度器准备下一代。" : "启动前再次确认 health.pipeline 无阻断。",
    continuityNote,
    technical,
  }, s);
}
