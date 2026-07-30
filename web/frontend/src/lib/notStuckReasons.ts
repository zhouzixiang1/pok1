/**
 * Stable reason codes that mean "waiting / parked", not "stuck".
 * Used by PhaseAProjectionStrip and OperatorSituation tips.
 */
export const NOT_STUCK_REASON_CODES: Record<string, string> = {
  consumer_parked: "Slice 2b 消费者停泊在质量门，主车道仍在推进草稿 — 不是卡住",
  eval_wait: "等待后台 70 手评测样本凑齐 — 不是卡住",
  eval_wait_degraded: "评测等待降级（daemon 未就绪），准备会重试 — 不是卡住",
  post_publication_handoff_pending: "发布后交接排队中，等待 Archivist — 不是卡住",
  post_publication_handoff_running: "发布后交接八步执行中 — 不是卡住",
  official_certifying: "正式认证进行中（可能异步）— 不是卡住",
  staging_async_cert: "暂存发布后的异步正式认证队列 — 不是卡住",
  draft_preparing: "草稿代次在 one-ahead 准备中 — 不是卡住",
  quota_wait: "LLM 配额窗口等待自动恢复 — 不是卡住",
};

export function notStuckLabel(code: string | null | undefined): string | null {
  if (!code) return null;
  return NOT_STUCK_REASON_CODES[code] ?? null;
}
