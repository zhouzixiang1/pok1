import type {
  AgentActivityResponse,
  AgentActivityProjection,
  AgentGateView,
} from "../api/types.js";
import type { PipelineStage } from "../constants/pipeline.js";
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

function gateComplete(gate: AgentGateView | null): boolean {
  return gate?.complete === true;
}

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
): AgentRoleSummary[] {
  const stage = projection.stage;
  const isTimeout = typeof stage === "string" && isPipelineTimeoutLeaseStage(stage);
  const attempts = projection.attempts;
  const rework = projection.rework_counts;
  const master = projection.master;
  const gates = projection.gates;
  const workerFailures = projection.worker_failures;

  const summaries: AgentRoleSummary[] = [];

  summaries.push({
    role: "orchestrator",
    label: "Orchestrator",
    state: isTimeout ? "terminal" : "running",
    detail: isTimeout
      ? `恢复租约 ${stage}；下一步受控恢复，不重放原阶段。`
      : (stage ? `当前阶段 ${stage}` : "等待阶段投影"),
  });

  const masterReached = master.stage_reached || stageReached(stage, REACHED_AFTER_MASTER_PLAN);
  summaries.push({
    role: "master",
    label: "Master 规划",
    state: masterReached ? "terminal" : stage === "direction_audited" ? "running" : "not_reached",
    detail: master.plan_present
      ? `${master.tasks.length} 个 Worker 任务已规划${attempts.audit ? `（方向审核第 ${attempts.audit} 次）` : ""}`
      : (masterReached ? "Master 已完成（无任务详情）" : "Master 尚未运行"),
  });

  // Scouts/ballots happen inside Master; surface only when Master has reached.
  summaries.push({
    role: "scouts",
    label: "Scouts / Ballots",
    state: masterReached ? "terminal" : "not_reached",
    detail: masterReached
      ? "Master 阶段已对候选 proposal 做双匿名 ballot；细节见代次日志。"
      : "Scouts 在 Master 内部运行，尚未到达。",
  });

  const workersReached = stageReached(stage, REACHED_AFTER_MASTER_PLAN);
  summaries.push({
    role: "workers",
    label: "Workers",
    state: workersReached ? "terminal" : master.plan_present ? "running" : "not_reached",
    detail: workersReached
      ? `Worker 阶段完成；累计失败 ${rework.worker_failure} 次${
          workerFailures.length > 0 ? `（最近 ${workerFailures.length} 条已记录）` : ""
        }`
      : (master.plan_present
        ? `${master.tasks.length} 个任务待执行`
        : "等待 Master 规划"),
  });

  const qualityReached = stageReached(stage, REACHED_AFTER_QUALITY);
  summaries.push({
    role: "reviewer",
    label: "Reviewer",
    state: qualityReached ? (gateComplete(gates.review) ? "terminal" : "running") : "not_reached",
    detail: gates.review
      ? (gates.review.complete
        ? "代码审核 schema-valid 完成"
        : "代码审核未完成（schema/LLM/执行链不完整）")
      : (qualityReached ? "Reviewer 阶段已过但无 gate 记录" : "Reviewer 尚未运行"),
  });

  const reviewReached = stageReached(stage, REACHED_AFTER_REVIEW);
  summaries.push({
    role: "critic",
    label: "Critic（advisory）",
    state: reviewReached ? (gateComplete(gates.critic) ? "terminal" : "running") : "not_reached",
    detail: gates.critic
      ? (gates.critic.complete
        ? "建议性 Critic schema-valid 完成（advisory，非强度/认证门）"
        : "Critic 未完成；advisory 结论不可用")
      : (reviewReached ? "Critic 阶段已过但无 gate 记录" : "Critic 尚未运行"),
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
  directionAudit: Record<string, unknown> | null;
}

export interface AgentActivityViewUnavailable {
  available: false;
  reason: string;
}

export function agentActivityView(
  response: AgentActivityResponse,
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
    roles: agentRoleSummaries(response),
    attempts: response.attempts,
    reworkCounts: response.rework_counts,
    master: response.master,
    gates: response.gates,
    gateKeysPresent: response.gate_keys_present,
    workerFailures: response.worker_failures,
    reviewerFeedback: response.orchestrator.reviewer_feedback,
    infraFailure: response.orchestrator.infra_failure,
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
