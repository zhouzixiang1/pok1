/**
 * Stable reason codes that mean "waiting / parked", not "stuck".
 * Used by PhaseAProjectionStrip and OperatorSituation tips.
 *
 * These map backend capability booleans to operator-facing Chinese copy.
 * Every code here must correspond to a real backend-derived state surfaced
 * through the ControlStatus projection (pipeline_mode / eval_wait).  Do NOT
 * add a code whose "not stuck" claim the frontend invents rather than
 * derives — the earlier quota_wait / draft_preparing / eval_wait_degraded /
 * post_publication_handoff_* / official_certifying / staging_async_cert
 * entries were all removed for exactly that reason (no backend
 * source-of-truth the control-status projection can observe):
 *  - quota state arrives via the SSE data-stream, not a reason code;
 *  - eval_wait degraded-min-games is loop-local orchestrator state a
 *    cross-process observer cannot see (control.py hardcodes degraded=False);
 *  - post_publication_handoff / async_certification states are read from the
 *    handoff/async_certification projection fields directly, not from here.
 */
export const NOT_STUCK_REASON_CODES: Record<string, string> = {
  consumer_parked: "并行评测停在质量门，主车道仍在推进 — 不是卡住",
  eval_wait: "后台 70 手评测在等样本凑齐 — 不是卡住",
};

export function notStuckLabel(code: string | null | undefined): string | null {
  if (!code) return null;
  return NOT_STUCK_REASON_CODES[code] ?? null;
}
