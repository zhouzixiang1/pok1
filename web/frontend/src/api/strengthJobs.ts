import type { StrengthJobsResponse, DaemonHealthSnapshot } from "./types.js";

type JsonObject = Record<string, unknown>;

const isObject = (value: unknown): value is JsonObject => (
  typeof value === "object" && value !== null && !Array.isArray(value)
);

const isDaemon = (value: unknown): value is DaemonHealthSnapshot => (
  isObject(value) && typeof value.alive === "boolean"
);

/**
 * Fail closed at the strength-jobs API boundary.
 *
 * Strength evidence is identity-bound: an admitted sample must come from the
 * current immutable evaluation cycle.  A structurally incomplete projection
 * must raise so the Background Strength page renders an empty state instead
 * of mixing in retired-epoch rows.
 */
export function expectStrengthJobs(value: unknown): StrengthJobsResponse {
  if (!isObject(value)) {
    throw new Error("strength jobs response is not an object");
  }
  if (value.evaluation_epoch !== "national_tcp_policy_v1") {
    throw new Error("strength jobs response evaluation_epoch is not national_tcp_policy_v1");
  }
  if (!isDaemon(value.daemon)) {
    throw new Error("strength jobs response is missing a daemon health snapshot");
  }
  if (value.available === false) {
    if (typeof value.reason !== "string" || value.reason.length === 0) {
      throw new Error("strength jobs unavailable response is missing a reason");
    }
    if (!Array.isArray(value.active_bots)) {
      throw new Error("strength jobs unavailable response active_bots is not an array");
    }
    return value as unknown as StrengthJobsResponse;
  }
  if (value.available !== true) {
    throw new Error("strength jobs response available flag is neither true nor false");
  }
  const identity = value.evaluation_identity_digest;
  if (
    typeof identity !== "string"
    || identity.length !== 64
    || !/^[0-9a-f]{64}$/.test(identity)
  ) {
    throw new Error("strength jobs projection evaluation_identity_digest is invalid");
  }
  if (
    !Array.isArray(value.active_bots)
    || !Array.isArray(value.admitted_samples)
    || !Array.isArray(value.staged_pending)
    || !Array.isArray(value.inadmissible_diagnostics)
    || !isObject(value.daemon_stats)
  ) {
    throw new Error("strength jobs projection is structurally incomplete");
  }
  return value as unknown as StrengthJobsResponse;
}
