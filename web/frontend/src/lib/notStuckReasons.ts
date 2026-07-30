/**
 * Stable reason codes that mean "waiting / parked", not "stuck".
 * Used by PhaseAProjectionStrip and OperatorSituation tips.
 *
 * These map backend capability booleans to operator-facing Chinese copy.
 * Every code here must correspond to a real backend-derived state surfaced
 * through the ControlStatus projection (pipeline_mode / eval_wait /
 * post_publication_handoff / async_certification).  Do NOT add a code whose
 * "not stuck" claim the frontend invents rather than derives — the earlier
 * quota_wait / draft_preparing entries were removed for exactly that reason
 * (no backend source-of-truth), since quota state arrives via the SSE
 * data-stream, not a control-status reason code.
 */
export const NOT_STUCK_REASON_CODES: Record<string, string> = {
  consumer_parked: "并行评测停在质量门，主车道仍在推进 — 不是卡住",
  eval_wait: "后台 70 手评测在等样本凑齐 — 不是卡住",
  eval_wait_degraded: "评测等待因后台未就绪降级，准备重试 — 不是卡住",
  post_publication_handoff_pending: "发布后交接排队中，等待归档八步 — 不是卡住",
  post_publication_handoff_running: "发布后交接八步执行中 — 不是卡住",
  official_certifying: "正式认证进行中（可能异步）— 不是卡住",
  staging_async_cert: "暂存发布后的异步正式认证排队 — 不是卡住",
};

export function notStuckLabel(code: string | null | undefined): string | null {
  if (!code) return null;
  return NOT_STUCK_REASON_CODES[code] ?? null;
}
