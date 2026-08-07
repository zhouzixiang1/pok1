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
  // Slice-2b one-ahead: the consumer lane runs the full gate chain
  // (quality→review→critic→precommit) while the PRIMARY lane is parked
  // waiting. Primary is parked (NOT advancing); the background lane is busy.
  consumer_parked: "后台质量门链正在并行验收候选，主槽故意旁路等待 — 不是卡住",
  // eval_wait counts COMPLETED matches toward `min_games` (default 24). The
  // badge above already renders games/min_games, so the threshold is shown
  // there directly — this prose must not restate a different number.
  eval_wait: "后台评测正在累积对局样本（达到配置的最少对局数即可继续）— 不是卡住",
};

export function notStuckLabel(code: string | null | undefined): string | null {
  if (!code) return null;
  return NOT_STUCK_REASON_CODES[code] ?? null;
}
