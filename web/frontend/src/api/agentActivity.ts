import type { AgentActivityResponse } from "./types.js";
import type { ActiveGeneration } from "./control.js";
import { isOfficialCertificationStage } from "./officialJobs.js";

type JsonObject = Record<string, unknown>;

const isObject = (value: unknown): value is JsonObject => (
  typeof value === "object" && value !== null && !Array.isArray(value)
);
const isInteger = (value: unknown): value is number => (
  typeof value === "number" && Number.isSafeInteger(value)
);
const nullableInteger = (value: unknown): boolean => value === null || isInteger(value);
const nullableString = (value: unknown): boolean => value === null || typeof value === "string";
const stringArray = (value: unknown): value is string[] => (
  Array.isArray(value) && value.every((item) => typeof item === "string")
);

const GATE_FIELDS: Record<string, ReadonlySet<string>> = {
  quality: new Set(["all_passed", "critical_scenarios_passed", "decision_pass_rate", "code_fingerprint", "workflow_profile_digest"]),
  review: new Set(["approved", "schema_valid", "llm_invoked", "reviewer_llm_executed", "llm_failed", "parse_failed", "quality_score", "receipt_digest"]),
  critic: new Set(["approved", "schema_valid", "llm_invoked", "critic_llm_executed", "llm_failed", "parse_failed", "advisory_approved", "advisory_score", "receipt_digest"]),
  precommit_eval: new Set(["passed", "attempt", "native_matches", "hands_per_match", "receipt_digest", "candidate_artifact_hash"]),
  official_full: new Set(["passed", "certificate_digest", "certification_profile", "opponent_authority", "strength_evidence_weight", "strategy_evidence_weight", "reused_existing_certificate"]),
};
const INFRA_FAILURE_FIELDS: ReadonlySet<string> = new Set([
  "schema_version",
  "failure_class",
  "component",
  "code",
  "operation",
  "owner_tool",
  "resume_stage",
  "attempt",
  "max_attempts",
  "reason",
  "retryable",
  "exhausted",
  "action",
  "identity_digest",
]);

const isBoundedScalar = (value: unknown): boolean => (
  value === null
  || typeof value === "boolean"
  || (typeof value === "number" && Number.isFinite(value))
  || (typeof value === "string" && value.length <= 256)
);

function isGate(value: unknown, name: string): boolean {
  return value === null || (
    isObject(value)
    && value.name === name
    && value.present === true
    && typeof value.complete === "boolean"
    && ["current", "historical_invalidated"].includes(String(value.authority_state))
    && !(value.authority_state === "historical_invalidated" && value.complete === true)
    && isObject(value.fields)
    && Object.keys(value.fields).every((key) => GATE_FIELDS[name]?.has(key))
    && Object.values(value.fields).every(isBoundedScalar)
  );
}

function isMaster(value: unknown): boolean {
  if (
    !isObject(value)
    || typeof value.started !== "boolean"
    || typeof value.completed !== "boolean"
    || typeof value.plan_present !== "boolean"
    || !nullableString(value.analysis)
    || !Array.isArray(value.tasks)
    || !isInteger(value.task_total)
    || value.task_total < value.tasks.length
    || typeof value.tasks_truncated !== "boolean"
    || value.tasks.length > 8
    || (value.completed === true && (value.started !== true || value.plan_present !== true))
  ) return false;
  return value.tasks.every((task) => (
    isObject(task)
    && nullableInteger(task.worker_id)
    && nullableString(task.role)
    && stringArray(task.target_files)
    && nullableString(task.difficulty)
    && nullableString(task.skill_layer)
    && task.target_files.length <= 8
    && (task.behavior_hypothesis === undefined || nullableString(task.behavior_hypothesis))
    && (task.expected_diff_shape === undefined || nullableString(task.expected_diff_shape))
    && (task.merge_policy === undefined || nullableString(task.merge_policy))
  ));
}

function isFailure(value: unknown): boolean {
  return isObject(value)
    && (value.worker_id === null || typeof value.worker_id === "string" || isInteger(value.worker_id))
    && nullableString(value.role)
    && nullableString(value.error)
    && nullableString(value.failure_type)
    && nullableString(value.category)
    && nullableInteger(value.gen)
    && (value.timestamp === null || (typeof value.timestamp === "number" && Number.isFinite(value.timestamp)))
    && value.record_state === "historical"
    && value.current_blocker === false;
}

function isInfraFailure(value: unknown): boolean {
  return value === null || (
    isObject(value)
    && Object.keys(value).every((key) => INFRA_FAILURE_FIELDS.has(key))
    && Object.values(value).every(isBoundedScalar)
  );
}

export function agentActivityBindingIssues(
  value: AgentActivityResponse | null | undefined,
  active: ActiveGeneration | null | undefined,
): string[] {
  if (!value || !active || value.available !== true) return ["authority_unavailable"];
  const fields: Array<[string, unknown, unknown]> = [
    ["next_v", value.next_v, active.next_v],
    ["source_v", value.source_v, active.source_v],
    ["parent2_v", value.parent2_v, active.parent2_v],
    ["stage", value.stage, active.stage],
    ["run_id", value.run_id, active.run_id],
    ["workflow_run_id", value.workflow_run_id, active.workflow_run_id],
    ["checkpoint_revision", value.checkpoint_revision, active.checkpoint_revision],
  ];
  return fields.filter(([, observed, expected]) => observed !== expected).map(([name]) => name);
}

export function agentWorkflowIdentityKey(
  active: ActiveGeneration | null | undefined,
): string {
  if (!active) return "none";
  return [
    active.next_v,
    active.source_v ?? "none",
    active.parent2_v ?? "none",
    active.stage,
    active.run_id,
    active.workflow_run_id ?? "none",
    active.checkpoint_revision,
  ].join(":");
}

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
  const observerLimits = value.observer_limits;
  if (
    !isObject(attempts)
    || !isObject(rework)
    || !isObject(gates)
    || !isMaster(value.master)
    || !isObject(value.orchestrator)
    || !Array.isArray(value.gate_keys_present)
    || !Array.isArray(value.worker_failures)
    || !isObject(observerLimits)
  ) {
    throw new Error("agent activity projection is structurally incomplete");
  }
  if (
    !isInteger(value.next_v)
    || value.next_v < 143
    || !nullableInteger(value.source_v)
    || !nullableInteger(value.parent2_v)
    || typeof value.stage !== "string"
    || value.stage.length === 0
    || typeof value.run_id !== "string"
    || value.run_id.length === 0
    || typeof value.workflow_run_id !== "string"
    || value.workflow_run_id.length === 0
    || !isInteger(value.checkpoint_revision)
    || value.checkpoint_revision <= 0
    || value.orchestrator.stage !== value.stage
    || !nullableString(value.orchestrator.reviewer_feedback)
    || !isInfraFailure(value.orchestrator.infra_failure)
    || typeof value.orchestrator.official_jobs_polling_supported !== "boolean"
    || value.orchestrator.official_jobs_polling_supported !== isOfficialCertificationStage(value.stage)
    || !(value.direction_audit === null || isObject(value.direction_audit))
    || !["generation", "audit", "precommit"].every((key) => isInteger(attempts[key]) && Number(attempts[key]) >= 0)
    || !["worker_failure", "precommit", "official"].every((key) => isInteger(rework[key]) && Number(rework[key]) >= 0)
    || !isGate(gates.quality, "quality")
    || !isGate(gates.review, "review")
    || !isGate(gates.critic, "critic")
    || !isGate(gates.precommit_eval, "precommit_eval")
    || !isGate(gates.official_full, "official_full")
    || !stringArray(value.gate_keys_present)
    || !value.worker_failures.every(isFailure)
    || value.worker_failures.length > 10
    || typeof value.worker_failures_truncated !== "boolean"
    || ![observerLimits.max_tasks, observerLimits.max_target_files_per_task, observerLimits.max_worker_failures, observerLimits.max_response_bytes].every(isInteger)
    || observerLimits.max_tasks !== 8
    || observerLimits.max_target_files_per_task !== 8
    || observerLimits.max_worker_failures !== 10
    || observerLimits.max_response_bytes !== 64 * 1024
  ) {
    throw new Error("agent activity projection nested contract is invalid");
  }
  return value as unknown as AgentActivityResponse;
}
