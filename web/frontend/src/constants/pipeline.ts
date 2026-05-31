export const PIPELINE_STAGES = ["prepared", "workers_done", "quality_passed", "reviewed", "critic_checked", "verified"] as const;

export const STAGE_LABELS: Record<string, string> = {
  prepared: "环境就绪",
  workers_done: "Worker 完成",
  quality_passed: "质量检查通过",
  reviewed: "代码审核通过",
  critic_checked: "策略审核通过",
  verified: "提交前验证",
};
