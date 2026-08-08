/**
 * Exact browser copy of pipeline_state.STAGE_ORDER. A Python contract test
 * compares this list with the backend so newly introduced durable stages
 * cannot silently disappear from the dashboard.
 */
export const PIPELINE_STAGE_CONTRACT = [
  "selected",
  "preparing",
  "prepared",
  "crossover_running",
  "direction_audited",
  "master_planned",
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
  "publishing",
  "archived",
] as const;

export type PipelineStage = typeof PIPELINE_STAGE_CONTRACT[number];

/**
 * Recovery-only timeout leases are real checkpoint stages, but deliberately
 * do not participate in the ordered success/failure pipeline above.  Keeping
 * an explicit, disjoint browser contract lets the dashboard render them as
 * recovery authority instead of either inventing progress or calling them an
 * unknown backend stage.
 */
export const PIPELINE_TIMEOUT_LEASE_STAGE_CONTRACT = [
  "timed_out",
  "infra_timed_out",
] as const;

export type PipelineTimeoutLeaseStage = typeof PIPELINE_TIMEOUT_LEASE_STAGE_CONTRACT[number];

export const PIPELINE_TIMEOUT_LEASES: Record<PipelineTimeoutLeaseStage, {
  label: string;
  nextTool: "abandon_generation" | "run_precommit_eval";
  description: string;
}> = {
  timed_out: {
    label: "代次超时恢复租约",
    nextTool: "abandon_generation",
    description: "当前代次只能经权威 abandon 收据结束；不能重新准备或假装继续原阶段。",
  },
  infra_timed_out: {
    label: "基础设施超时恢复租约",
    nextTool: "run_precommit_eval",
    description: "候选字节保持不变，只能从受控原生预提交恢复入口重试。",
  },
};

export function isPipelineTimeoutLeaseStage(value: string): value is PipelineTimeoutLeaseStage {
  return PIPELINE_TIMEOUT_LEASE_STAGE_CONTRACT.includes(value as PipelineTimeoutLeaseStage);
}

/** Successful-path milestones. Failure/rework/transitional stages map to the
 * milestone they are currently trying to complete, without being painted as
 * successful completed steps. */
export const PIPELINE_STAGES = [
  "selected",
  "prepared",
  "direction_audited",
  "master_planned",
  "workers_done",
  "quality_passed",
  "reviewed",
  "critic_checked",
  "verified",
  "publishing",
  "archived",
] as const;

export type PipelineMilestone = typeof PIPELINE_STAGES[number];

/**
 * A durable checkpoint stage is not automatically "work currently running".
 * Most successful stages are committed boundaries: the next tool, carried by
 * the paired health route, owns the work that follows.  Transitional and
 * failed stages remain visually active but never turn their target milestone
 * green.
 */
export type PipelineStageProgressKind =
  | "completed_boundary"
  | "in_progress"
  | "failed_boundary";

export interface PipelineStageProgress {
  kind: PipelineStageProgressKind;
  completedThrough: PipelineMilestone | null;
  activeMilestone: PipelineMilestone | null;
}

export const PIPELINE_STAGE_PROGRESS: Record<PipelineStage, PipelineStageProgress> = {
  selected: { kind: "completed_boundary", completedThrough: "selected", activeMilestone: "prepared" },
  preparing: { kind: "in_progress", completedThrough: "selected", activeMilestone: "prepared" },
  prepared: { kind: "completed_boundary", completedThrough: "prepared", activeMilestone: "direction_audited" },
  crossover_running: { kind: "in_progress", completedThrough: "selected", activeMilestone: "prepared" },
  direction_audited: { kind: "completed_boundary", completedThrough: "direction_audited", activeMilestone: "master_planned" },
  master_planned: { kind: "completed_boundary", completedThrough: "master_planned", activeMilestone: "workers_done" },
  workers_done: { kind: "completed_boundary", completedThrough: "workers_done", activeMilestone: "quality_passed" },
  quality_failed: { kind: "failed_boundary", completedThrough: "workers_done", activeMilestone: "quality_passed" },
  quality_rejected: { kind: "failed_boundary", completedThrough: "workers_done", activeMilestone: "quality_passed" },
  quality_passed: { kind: "completed_boundary", completedThrough: "quality_passed", activeMilestone: "reviewed" },
  review_rejected: { kind: "failed_boundary", completedThrough: "quality_passed", activeMilestone: "reviewed" },
  reviewed: { kind: "completed_boundary", completedThrough: "reviewed", activeMilestone: "critic_checked" },
  critic_rejected: { kind: "failed_boundary", completedThrough: "reviewed", activeMilestone: "critic_checked" },
  critic_checked: { kind: "completed_boundary", completedThrough: "critic_checked", activeMilestone: "verified" },
  precommit_failed: { kind: "failed_boundary", completedThrough: "critic_checked", activeMilestone: "verified" },
  repair_planned: { kind: "in_progress", completedThrough: "master_planned", activeMilestone: "workers_done" },
  rework_running: { kind: "in_progress", completedThrough: "master_planned", activeMilestone: "workers_done" },
  verified: { kind: "completed_boundary", completedThrough: "verified", activeMilestone: "publishing" },
  publishing: { kind: "in_progress", completedThrough: "verified", activeMilestone: "publishing" },
  archived: { kind: "completed_boundary", completedThrough: "publishing", activeMilestone: "archived" },
};

export function pipelineStageProgress(stage: PipelineStage): PipelineStageProgress {
  return PIPELINE_STAGE_PROGRESS[stage];
}

export const STAGE_LABELS: Record<PipelineStage | PipelineMilestone, string> = {
  selected: "基线选定",
  preparing: "准备基线",
  prepared: "环境就绪",
  crossover_running: "交叉准备中",
  direction_audited: "方向审核",
  master_planned: "Master 规划",
  workers_done: "Worker 完成",
  quality_failed: "质量门失败",
  quality_rejected: "质量门终止，等待受控放弃",
  quality_passed: "质量检查通过",
  review_rejected: "代码审核拒绝，等待受控放弃",
  reviewed: "代码审核通过",
  critic_rejected: "Critic 控制链失败，等待受控放弃",
  critic_checked: "建议性 Critic 已完成",
  precommit_failed: "本地预提交失败",
  repair_planned: "修复计划",
  rework_running: "修复执行中",
  verified: "本地预提交通过",
  publishing: "签名发布",
  archived: "已发布，等待 Archivist 收尾",
};
