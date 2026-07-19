import type {
  AgentActivityResponse,
  AgentActivityProjection,
  AgentGateView,
} from "../api/types.js";
import type { PipelineStage } from "../constants/pipeline.js";
import type { PipelineRoute } from "../api/control.js";
import {
  PIPELINE_STAGE_CONTRACT,
  PIPELINE_TIMEOUT_LEASE_STAGE_CONTRACT,
  isPipelineTimeoutLeaseStage,
} from "../constants/pipeline.js";

export type AgentRole =
  | "orchestrator"
  | "master"
  | "scouts"
  | "workers"
  | "reviewer"
  | "critic";

export type AgentActivityState = "running" | "terminal" | "not_reached" | "unknown";

export interface AgentRoleSummary {
  role: AgentRole;
  label: string;
  state: AgentActivityState;
  /** Free-form detail the dashboard renders verbatim (no field derivation). */
  detail: string;
}

const REACHED_AFTER_MASTER_PLAN: ReadonlySet<string> = new Set([
  "workers_done",
  "quality_failed",
  "quality_rejected",
  "quality_passed",
  "review_rejected",
  "reviewed",
  "critic_rejected",
  "critic_checked",
  "precommit_failed",
  "repair_planned",
  "rework_running",
  "verified",
  "official_bootstrap_required",
  "official_certifying",
  "official_failed",
  "official_inconclusive",
  "publishing",
]);

const REACHED_AFTER_QUALITY: ReadonlySet<string> = new Set([
  "quality_passed",
  "review_rejected",
  "reviewed",
  "critic_rejected",
  "critic_checked",
  "precommit_failed",
  "repair_planned",
  "rework_running",
  "verified",
  "official_bootstrap_required",
  "official_certifying",
  "official_failed",
  "official_inconclusive",
  "publishing",
]);

const REACHED_AFTER_REVIEW: ReadonlySet<string> = new Set([
  "reviewed",
  "critic_rejected",
  "critic_checked",
  "precommit_failed",
  "repair_planned",
  "rework_running",
  "verified",
  "official_bootstrap_required",
  "official_certifying",
  "official_failed",
  "official_inconclusive",
  "publishing",
]);

const gateHistorical = (gate: AgentGateView | null): boolean => (
  gate?.authority_state === "historical_invalidated"
);

const gateCurrent = (gate: AgentGateView | null): boolean => (
  gate?.authority_state === "current"
);

function stageReached(stage: string | null | undefined, set: ReadonlySet<string>): boolean {
  return typeof stage === "string" && set.has(stage);
}

/**
 * Project one agent role's activity state from the checkpoint-derived
 * projection.  No field is re-derived from raw gate dicts; completion comes
 * from the backend's typed ``complete`` flag, which mirrors the same field
 * chain the dashboard uses elsewhere.
 */
export function agentRoleSummaries(
  projection: AgentActivityProjection,
  route?: PipelineRoute | null,
): AgentRoleSummary[] {
  const stage = projection.stage;
  const isTimeout = typeof stage === "string" && isPipelineTimeoutLeaseStage(stage);
  const attempts = projection.attempts;
  const rework = projection.rework_counts;
  const master = projection.master;
  const gates = projection.gates;
  const workerFailures = projection.worker_failures;
  const routeFailure = route?.infra_failure;
  const infraComponent = routeFailure && typeof routeFailure.component === "string"
    ? routeFailure.component
    : null;
  const infraAttempt = routeFailure && typeof routeFailure.attempt === "number"
    ? routeFailure.attempt
    : null;
  const infraMax = routeFailure && typeof routeFailure.max_attempts === "number"
    ? routeFailure.max_attempts
    : null;
  const masterRetry = route?.stage === stage
    && route.next_tool === "run_master"
    && route.action === "retry_same_tool"
    && route.failure_class === "infrastructure"
    && infraComponent === "master_llm";

  const summaries: AgentRoleSummary[] = [];

  summaries.push({
    role: "orchestrator",
    label: "流程协调（Orchestrator）",
    state: isTimeout ? "terminal" : "running",
    detail: isTimeout
      ? `系统已进入 ${stage} 超时恢复状态，只会走受控恢复入口。`
      : (stage ? "状态机正在守护本代并选择唯一下一动作。" : "等待状态机给出下一动作。"),
  });

  const masterCompleted = master.completed && master.plan_present;
  const masterStarted = master.started || stage === "direction_audited";
  summaries.push({
    role: "master",
    label: "方案负责人（Master）",
    state: masterCompleted ? "terminal" : masterStarted ? "running" : "not_reached",
    detail: masterRetry
      ? `上次模型调用没有形成合格方案；系统将进行第 ${infraAttempt != null ? infraAttempt + 1 : "?"}${infraMax != null ? `/${infraMax}` : ""} 次同阶段重试。`
      : master.plan_present
      ? `${master.tasks.length} 个 Worker 任务已规划${attempts.audit ? `（方向审核第 ${attempts.audit} 次）` : ""}`
      : (masterStarted ? "正在生成、比较并裁决候选方案。" : "尚未进入 Master 方案阶段。"),
  });

  // Scouts/ballots happen inside Master; surface only when Master has reached.
  summaries.push({
    role: "scouts",
    label: "方案探索与匿名投票",
    state: masterCompleted ? "terminal" : masterStarted ? "running" : "not_reached",
    detail: masterCompleted
      ? "多个候选方案已完成独立探索和匿名表决。"
      : (masterRetry ? "一条方案探索调用超时；将在 Master 的局部重试中重新执行。" : "随 Master 规划一起运行，当前尚未完成。"),
  });

  const repairActive = stage === "repair_planned" || stage === "rework_running";
  const downstreamGatePresent = Object.values(gates).some(gateCurrent);
  const workersReached = stageReached(stage, REACHED_AFTER_MASTER_PLAN) || downstreamGatePresent;
  const workersState: AgentActivityState = repairActive
    ? "running"
    : workersReached
      ? "terminal"
      : master.plan_present
        ? (isTimeout ? "unknown" : "running")
        : "not_reached";
  summaries.push({
    role: "workers",
    label: "代码实现（Workers）",
    state: workersState,
    detail: repairActive
      ? "正在为当前候选执行受控修复；修复前门禁已失效，完成后必须重新验证。"
      : workersReached
      ? `实现阶段已结束；累计失败记录 ${rework.worker_failure} 次${
          workerFailures.length > 0 ? `（最近 ${workerFailures.length} 条已记录）` : ""
        }`
      : (master.plan_present
        ? (isTimeout
          ? "超时租约抹去了当前执行角色；已批准任务仍存在，但页面不会把它倒退成未开始或猜成仍在运行。"
          : `${master.tasks.length} 个已批准任务正在或即将执行。`)
        : "等待方案负责人给出已批准任务。"),
  });

  const qualityReached = stageReached(stage, REACHED_AFTER_QUALITY) || gateCurrent(gates.quality);
  const reviewAttemptTerminal = gateCurrent(gates.review)
    || stage === "review_rejected"
    || stageReached(stage, REACHED_AFTER_REVIEW);
  const reviewerState: AgentActivityState = repairActive
    ? "not_reached"
    : reviewAttemptTerminal
      ? "terminal"
      : qualityReached
        ? (isTimeout ? "unknown" : "running")
        : "not_reached";
  summaries.push({
    role: "reviewer",
    label: "独立代码审核（Reviewer）",
    state: reviewerState,
    detail: stage === "review_rejected"
      ? "独立审核已形成绑定拒绝结论；它是终态结果，不是仍在运行的 Reviewer。"
      : gates.review
      ? (gateHistorical(gates.review)
        ? "修复前审核记录仅保留为历史诊断，当前候选必须重新审核。"
        : gates.review.complete
        ? "独立审核已形成结构完整、内容绑定的结果。"
        : "审核尚未形成完整有效的结果。")
      : (qualityReached
        ? (isTimeout
          ? "超时租约下只能证明质量边界已到达；Reviewer 是否曾启动不可从 stage 猜测。"
          : "流程已到审核位置，但没有可验证审核记录。")
        : "等待代码实现和质量检查通过。"),
  });

  const reviewReached = stageReached(stage, REACHED_AFTER_REVIEW)
    || (gateCurrent(gates.review) && gates.review?.complete === true);
  const criticAttemptTerminal = gateCurrent(gates.critic) || stage === "critic_rejected";
  const criticState: AgentActivityState = repairActive
    ? "not_reached"
    : criticAttemptTerminal
      ? "terminal"
      : reviewReached
        ? (isTimeout ? "unknown" : "running")
        : "not_reached";
  summaries.push({
    role: "critic",
    // Backend contract name: advisory. User-facing copy explains the effect
    // instead of requiring the operator to understand that internal term.
    label: "建议复核（Critic）",
    state: criticState,
    detail: stage === "critic_rejected"
      ? "Critic 控制链已形成绑定失败结论；建议角色不再运行，流程只能走受控结束。"
      : gates.critic
      ? (gateHistorical(gates.critic)
        ? "修复前 Critic 建议已失效；它不能表示当前候选已完成复核。"
        : gates.critic.complete
        ? "建议已形成；它帮助发现风险，但不单独决定合规、强度或发布。"
        : "建议复核尚未形成完整结果。")
      : (reviewReached
        ? (isTimeout
          ? "超时租约下只能证明审核边界已到达；Critic 是否曾启动不可从 stage 猜测。"
          : "流程已到建议复核位置，但没有可验证记录。")
        : "等待独立代码审核完成。"),
  });

  return summaries;
}

export interface AgentActivityView {
  available: true;
  evaluationEpoch: "national_tcp_policy_v1";
  workflowRunId: string | null;
  runId: string | null;
  nextV: number | null;
  sourceV: number | null;
  parent2V: number | null;
  checkpointRevision: number | null;
  stage: string | null;
  stageIsTimeoutLease: boolean;
  stageKnown: boolean;
  roles: AgentRoleSummary[];
  attempts: AgentActivityProjection["attempts"];
  reworkCounts: AgentActivityProjection["rework_counts"];
  master: AgentActivityProjection["master"];
  gates: AgentActivityProjection["gates"];
  gateKeysPresent: string[];
  workerFailures: AgentActivityProjection["worker_failures"];
  reviewerFeedback: string | null;
  infraFailure: Record<string, unknown> | null;
  officialJobsPollingSupported: boolean;
  directionAudit: Record<string, unknown> | null;
}

export interface AgentActivityViewUnavailable {
  available: false;
  reason: string;
}

export function agentActivityView(
  response: AgentActivityResponse,
  route?: PipelineRoute | null,
): AgentActivityView | AgentActivityViewUnavailable {
  if (!response.available) {
    return { available: false, reason: response.reason };
  }
  const stage = response.stage;
  const knownStage = (
    typeof stage === "string"
    && (PIPELINE_STAGE_CONTRACT as readonly string[]).includes(stage)
  ) || (
    typeof stage === "string"
    && (PIPELINE_TIMEOUT_LEASE_STAGE_CONTRACT as readonly string[]).includes(stage)
  );
  return {
    available: true,
    evaluationEpoch: response.evaluation_epoch,
    workflowRunId: response.workflow_run_id,
    runId: response.run_id,
    nextV: response.next_v,
    sourceV: response.source_v,
    parent2V: response.parent2_v,
    checkpointRevision: response.checkpoint_revision,
    stage,
    stageIsTimeoutLease: typeof stage === "string" && isPipelineTimeoutLeaseStage(stage),
    stageKnown: knownStage,
    roles: agentRoleSummaries(response, route),
    attempts: response.attempts,
    reworkCounts: response.rework_counts,
    master: response.master,
    gates: response.gates,
    gateKeysPresent: response.gate_keys_present,
    workerFailures: response.worker_failures,
    reviewerFeedback: response.orchestrator.reviewer_feedback,
    infraFailure: response.orchestrator.infra_failure,
    officialJobsPollingSupported: response.orchestrator.official_jobs_polling_supported,
    directionAudit: response.direction_audit,
  };
}

export function isStageKnown(stage: string | null): stage is PipelineStage | "timed_out" | "infra_timed_out" {
  if (stage === null) return false;
  return (
    (PIPELINE_STAGE_CONTRACT as readonly string[]).includes(stage)
    || (PIPELINE_TIMEOUT_LEASE_STAGE_CONTRACT as readonly string[]).includes(stage)
  );
}
