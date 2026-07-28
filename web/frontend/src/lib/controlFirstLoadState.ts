/**
 * Pure first-load control-status state machine.
 *
 * The control-plane /api/control/health endpoint returns a deliberate
 * retryable HTTP 503 ("observer_projection_refreshing") while the read-only
 * strict-epoch projection is being built.  That build takes many seconds, so on
 * a fresh page load the first one or more health polls are retryable 503s with
 * no usable status yet.  Surfacing those as the red "无法确认版本与运行权威"
 * banner misrepresents a transient backend build as an authority failure.
 *
 * This module centralizes the three observable control-load phases so the hook
 * and the UI render the same truth, and so the transitions are unit-testable
 * without a React runtime:
 *
 *   - "first_load_refreshing": no observation has resolved yet and the latest
 *     error is retryable.  The UI renders a neutral "refreshing" state.
 *   - "fail_closed": the latest error is genuinely non-retryable (or a
 *     structural identity mismatch).  The UI renders the red banner and the
 *     page must not retain any former task/route/authority.
 *   - "resolved": at least one coherent observation has resolved.  Subsequent
 *     retryable refreshes keep the last known authority (handled by the hook);
 *     only a non-retryable error here can flip back to fail_closed.
 *
 * The backend's retryable contract is the single input that distinguishes the
 * first two phases; nothing else about the status object changes the phase.
 */

export type ControlLoadPhase = "first_load_refreshing" | "fail_closed" | "resolved";

export interface ControlFirstLoadStateInput {
  /** True once a single coherent status observation has resolved. */
  seenResolved: boolean;
  /** True if the latest error is a retryable observer 503 (transient build). */
  retryable: boolean;
  /** True if the latest refresh errored at all (retryable or not). */
  errored: boolean;
}

/**
 * Derive the observable load phase from the hook's tracking state.  The result
 * is intentionally exhaustive over the three cases so callers cannot forget a
 * branch.
 */
export function controlFirstLoadPhase(input: ControlFirstLoadStateInput): ControlLoadPhase {
  const { seenResolved, retryable, errored } = input;
  if (seenResolved) {
    // A previously resolved page only reaches fail_closed on a genuine
    // non-retryable error; a retryable refresh keeps the resolved phase (the
    // hook retains the last good status).
    return retryable || !errored ? "resolved" : "fail_closed";
  }
  // First load (no observation yet): a retryable build stays neutral, every
  // other terminal condition fails closed.
  return retryable ? "first_load_refreshing" : "fail_closed";
}

/**
 * Whether the red "无法确认版本与运行权威" fail-closed banner should render.
 * Only the genuine non-retryable cases ever show it.
 */
export function isControlFailClosed(input: ControlFirstLoadStateInput): boolean {
  return controlFirstLoadPhase(input) === "fail_closed";
}
