import type { AgentActivityResponse } from "./types.js";

type JsonObject = Record<string, unknown>;

const isObject = (value: unknown): value is JsonObject => (
  typeof value === "object" && value !== null && !Array.isArray(value)
);

/**
 * Fail closed at the agent-activity API boundary.
 *
 * The endpoint is checkpoint-derived, so a malformed response must not be
 * upgraded into a partial projection.  An unavailable workflow is a typed
 * `available: false` shape; anything structurally wrong raises so the caller
 * can render a fail-closed empty state instead of guessing agent state.
 */
export function expectAgentActivity(value: unknown): AgentActivityResponse {
  if (!isObject(value)) {
    throw new Error("agent activity response is not an object");
  }
  if (value.evaluation_epoch !== "national_tcp_policy_v1") {
    throw new Error("agent activity response evaluation_epoch is not national_tcp_policy_v1");
  }
  if (value.available === false) {
    if (typeof value.reason !== "string" || value.reason.length === 0) {
      throw new Error("agent activity unavailable response is missing a reason");
    }
    return value as unknown as AgentActivityResponse;
  }
  if (value.available !== true) {
    throw new Error("agent activity response available flag is neither true nor false");
  }
  // Available projection: identity fields must be coherent.  stage may be null
  // during a brief window; workflow_run_id is the binding key.
  const attempts = value.attempts;
  const rework = value.rework_counts;
  const gates = value.gates;
  if (
    !isObject(attempts)
    || !isObject(rework)
    || !isObject(gates)
    || !isObject(value.master)
    || !isObject(value.orchestrator)
    || !Array.isArray(value.gate_keys_present)
    || !Array.isArray(value.worker_failures)
  ) {
    throw new Error("agent activity projection is structurally incomplete");
  }
  return value as unknown as AgentActivityResponse;
}
