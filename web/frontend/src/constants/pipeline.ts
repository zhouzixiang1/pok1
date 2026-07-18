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
  "quality_passed",
  "reviewed",
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
  "official_certifying",
  "publishing",
  "archived",
] as const;

export type PipelineMilestone = typeof PIPELINE_STAGES[number];

export const STAGE_TO_MILESTONE: Record<PipelineStage, PipelineMilestone> = {
  selected: "selected",
  preparing: "selected",
  prepared: "prepared",
  crossover_running: "prepared",
  direction_audited: "direction_audited",
  master_planned: "master_planned",
  workers_done: "workers_done",
  quality_failed: "quality_passed",
  quality_passed: "quality_passed",
  reviewed: "reviewed",
  critic_checked: "critic_checked",
  precommit_failed: "verified",
  repair_planned: "verified",
  rework_running: "verified",
  verified: "verified",
  official_bootstrap_required: "official_certifying",
  official_certifying: "official_certifying",
  official_failed: "official_certifying",
  official_inconclusive: "official_certifying",
  publishing: "publishing",
  archived: "archived",
};

export const STAGE_LABELS: Record<PipelineStage | PipelineMilestone, string> = {
  selected: "基线选定",
  preparing: "准备基线",
  prepared: "环境就绪",
  crossover_running: "交叉准备中",
  direction_audited: "方向审核",
  master_planned: "Master 规划",
  workers_done: "Worker 完成",
  quality_failed: "质量门失败",
  quality_passed: "质量检查通过",
  reviewed: "代码审核通过",
  critic_checked: "建议性 Critic 已完成",
  precommit_failed: "本地预提交失败",
  repair_planned: "修复计划",
  rework_running: "修复执行中",
  verified: "本地预提交通过",
  official_bootstrap_required: "等待首代官方引导",
  official_certifying: "官方 EXE 正式认证",
  official_failed: "官方认证失败",
  official_inconclusive: "官方认证无结论",
  publishing: "签名发布",
  archived: "Post-commit 归档",
};
