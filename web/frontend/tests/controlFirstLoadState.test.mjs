import assert from "node:assert/strict";
import test from "node:test";

import {
  controlFirstLoadPhase,
  isControlFailClosed,
} from "../node_modules/.tmp/sse-tests/lib/controlFirstLoadState.js";

// The control /api/control/health endpoint emits a deliberate retryable 503
// while the strict-epoch projection is being built.  On a fresh page load there
// is no prior status, so the retryable refresh must NOT render the red
// "无法确认版本与运行权威" fail-closed banner — it is a transient build, not an
// authority failure.  Only a genuine non-retryable error fails closed.

test("controlFirstLoadPhase: first-load retryable refresh stays neutral, not fail-closed", () => {
  // No observation yet, but the backend said the projection is refreshing
  // (retryable 503).  This is the exact homepage symptom: the banner must
  // stay hidden.
  const firstLoadRefreshing = { seenResolved: false, retryable: true, errored: true };
  assert.equal(controlFirstLoadPhase(firstLoadRefreshing), "first_load_refreshing");
  assert.equal(isControlFailClosed(firstLoadRefreshing), false);
});

test("controlFirstLoadPhase: first-load non-retryable error fails closed", () => {
  // A genuine authority failure / structural identity mismatch on first load
  // surfaces the red banner.
  const firstLoadFatal = { seenResolved: false, retryable: false, errored: true };
  assert.equal(controlFirstLoadPhase(firstLoadFatal), "fail_closed");
  assert.equal(isControlFailClosed(firstLoadFatal), true);
});

test("controlFirstLoadPhase: first-load success resolves", () => {
  const firstLoadOk = { seenResolved: true, retryable: false, errored: false };
  assert.equal(controlFirstLoadPhase(firstLoadOk), "resolved");
  assert.equal(isControlFailClosed(firstLoadOk), false);
});

test("controlFirstLoadPhase: resolved page survives a retryable refresh", () => {
  // Once the dashboard has a coherent observation, a later retryable 503 keeps
  // the last known authority (the hook retains the previous status); the red
  // banner must not reappear.
  const resolvedThenRefreshing = { seenResolved: true, retryable: true, errored: true };
  assert.equal(controlFirstLoadPhase(resolvedThenRefreshing), "resolved");
  assert.equal(isControlFailClosed(resolvedThenRefreshing), false);
});

test("controlFirstLoadPhase: resolved page fails closed only on a genuine error", () => {
  const resolvedThenFatal = { seenResolved: true, retryable: false, errored: true };
  assert.equal(controlFirstLoadPhase(resolvedThenFatal), "fail_closed");
  assert.equal(isControlFailClosed(resolvedThenFatal), true);
});

test("controlFirstLoadPhase: first-load retryable error is not treated as no-error", () => {
  // Sanity: a retryable error is still an error (errored=true); the neutral
  // phase is chosen purely because it is retryable on first load.
  const input = { seenResolved: false, retryable: true, errored: true };
  assert.equal(input.errored, true);
  assert.equal(controlFirstLoadPhase(input), "first_load_refreshing");
});
