import { withOperatorControlHeader } from "./operatorControl.js";
import { canonicalGenerationIdentityIssues } from "../lib/canonicalGenerationIdentity.js";

export type EvaluationEpoch = "national_tcp_policy_v1";

export type EpochState =
  | "reset_required"
  | "reset_evidence_requires_recovery"
  | "version_authority_requires_recovery"
  | "epoch_authority_unavailable"
  | "runtime_reconciliation_in_progress"
  | "publication_recovery_ready"
  | "fresh_bootstrap_ready"
  | "strict_published";

export type EpochOperatorAction =
  | "execute_policy_epoch_reset"
  | "inspect_policy_epoch_reset_evidence"
  | "inspect_strict_version_authority"
  | "inspect_epoch_authority"
  | "inspect_runtime_reconciliation_claim"
  | "archive_incompatible_checkpoint"
  | "complete_runtime_reconciliation"
  | "quarantine_legacy_ledger_and_abandon_checkpoint"
  | "operator_reconcile_checkpoint"
  | "finalize_recorded_abandon_checkpoint"
  | "run_first_strict_official_certification";

export type IgnoredCheckpointReason =
  | "checkpoint_unreadable_or_not_object"
  | "checkpoint_not_bound_to_strict_epoch"
  | "runtime_reconciliation_in_progress"
  | "abandon_receipt_ledger_requires_reconciliation";

export interface CanonicalGenerationIdentity {
  generation_ordinal: number;
  canonical_version: number;
  canonical_bot_name: string;
  canonical_tag: string;
}

export interface ActiveGeneration extends CanonicalGenerationIdentity {
  /** Primary-slot marker when projected inside ``active_generations``. */
  slot_id?: "primary";
  next_v: number;
  source_v: number | null;
  parent2_v: number | null;
  stage: string;
  run_id: string;
  workflow_run_id: string | null;
  checkpoint_revision: number;
  recovery_kind?:
    | "publication_reconciliation"
    | "recorded_abandon_checkpoint_finalize";
  abandon_receipt_digest?: string;
  attempt: {
    generation: number;
    audit: number;
    precommit: number;
  };
}

/** A draft-slot generation running concurrently with the primary (Phase 4b / Slice 2b). */
export interface DraftGeneration {
  slot_id: "draft";
  next_v: number;
  source_v: number | null;
  parent2_v: number | null;
  stage: string;
  workflow_run_id: string | null;
  /** Backend may omit or null a draft revision before first durable write. */
  checkpoint_revision: number | null;
  is_draft: true;
}

/** Multi-slot control projection: primary first, then optional draft. */
export type GenerationSlot =
  | (ActiveGeneration & { slot_id: "primary" })
  | DraftGeneration;

export interface PipelineModeProjection {
  enabled: boolean;
  consumer_parked: boolean;
  producer_may_prepare_next: boolean;
  producer_may_advance: boolean;
  in_flight_count: number;
  sealed_candidates: string[];
}

export interface FeatureFlagsProjection {
  slice2b_enabled: boolean;
  staging_as_parent: boolean;
  certified_tag_prefix: string;
  tag_prefix: string;
}

export interface VersionAuthorityProjection {
  high_water: number;
  paired_versions: number[];
  certified_versions: number[];
  unpaired_completion_versions: number[];
  unpaired_high_water_versions: number[];
}

export interface AsyncCertificationItem {
  version: number;
  bot_name: string;
  state: "passed" | "pending" | "running" | string;
  staging_tag: string;
  certified_tag: string | null;
  job_id: string | null;
  formal_authority: "signed_full_v5" | "staging_uncertified" | string;
}

export interface AsyncCertificationProjection {
  items: AsyncCertificationItem[];
  any_pending: boolean;
}

export interface EvalWaitProjection {
  waiting: boolean;
  bot: string | null;
  games: number | null;
  min_games: number;
  rd: number | null;
  rd_threshold: number;
  rd_min_games: number;
  daemon_alive: boolean | null;
  consecutive_prep_fails: number | null;
  degraded: boolean;
}

export interface PipelineRoute {
  stage: string;
  next_v: number;
  source_v: number | null;
  parent2_v: number | null;
  next_tool: string | null;
  allowed_tools: string[];
  intent: string;
  /** Recovery action emitted by the backend when the route owns a retry. */
  action?: "retry_same_tool" | "abandon_generation" | string | null;
  failure_class?: string | null;
  /** Typed enough for presentation; the backend remains the classification authority. */
  infra_failure?: Record<string, unknown> | null;
  directive: string;
}

export interface OperatorTransition {
  schema_version: 1;
  kind: "first-strict-official-operator-transition";
  state: "bootstrap_required" | "bootstrap_running" | "bootstrap_failed" | "ready_to_finalize";
  action: string;
  command: string | null;
  reason: string | null;
  certification_profile: "first_strict_control_v1";
  opponent_authority: "system_control";
  strength_evidence_weight: 0;
  strategy_evidence_weight: 0;
  evaluation_epoch: EvaluationEpoch;
  workflow_run_id: string | null;
  // The first-strict candidate/source versions are branch-configurable
  // (national_cloud_v1 with source_v=null on the cloud branch; the
  // historical national_v143/source_v=142 on main). The frontend must not
  // pin these to a specific branch's literals — it validates the values
  // carried by the backend projection against the active generation.
  candidate_version: number | null;
  source_v: number | null;
  checkpoint_stage: "official_bootstrap_required" | null;
  checkpoint_revision: number | null;
  candidate_hash?: string | null;
  parked_request_digest?: string | null;
  job_id?: string | null;
  certificate_digest?: string | null;
  transition_digest: string;
}

export interface IgnoredCheckpoint {
  next_v: number | null;
  source_v: number | null;
  stage: string | null;
  reason: IgnoredCheckpointReason;
  issues: string[];
}

export interface StabilityObservation {
  schema_version: 1;
  kind: "national-tcp-uninterrupted-evolution-observation";
  authority: "operator_acceptance_only";
  strategy_evidence_weight: 0;
  strength_evidence_weight: 0;
  status: "not_started" | "observing" | "awaiting_strength_cycle" | "complete" | "reset_required";
  continuity_valid: boolean;
  count: number;
  target: number;
  remaining: number;
  complete: boolean;
  strength_cycle_ready: boolean;
  strength_cycle: {
    ready: boolean;
    reason: string;
    latest_bot?: string;
    evaluation_identity_digest?: string;
    cycle_manifest_digest?: string;
    cycle_save_num?: number;
    admitted_sample_id?: string;
  };
  continuity_id: string | null;
  last_reset_reason: string | null;
  /** Diagnostic context for the latest reset; never a rating/selection input. */
  last_reset_details?: Record<string, unknown> | null;
  identity_mismatches: string[];
  errors: string[];
  verification?: {
    state: "pending" | "fresh" | "stale" | "failed";
    checked_at: number | null;
    fresh_until: number | null;
    error: string | null;
    authority: {
      evaluation_epoch: "national_tcp_policy_v1";
      epoch_stream_authority_digest: string | null;
      repository_head: string;
      repository_branch: string;
    } | null;
  };
}

/** Whitelisted post-publication step row (no plan/receipt bodies). */
export interface HandoffStepProjection {
  id: string;
  ordinal: number;
  status: "pending" | "planned" | "running" | "completed";
  plan_digest: string | null;
  receipt_digest: string | null;
  updated_at: number | null;
}

export interface PostPublicationHandoffStatus {
  schema_version: 1;
  authority: "post_publication_handoff_journal";
  status: "none" | "pending" | "running" | "blocked";
  state: "pending" | "running" | "blocked" | null;
  blocked: boolean;
  version: number | null;
  source_v: number | null;
  workflow_run_id: string | null;
  identity_digest: string | null;
  publication_id: string | null;
  record_revision: number | null;
  owner_scope: "none" | "current_process" | "foreign_process";
  next_tool: "run_archivist" | null;
  issues: string[];
  projection_digest: string;
  /** Present when status is pending/running/blocked; empty when none. */
  steps?: HandoffStepProjection[];
  current_step?: string | null;
  completed_count?: number;
}

export interface ControlStatus {
  mode: string;
  running: boolean;
  daemon_enabled: boolean;
  daemon_workers: number;
  daemon_pairs: number;
  /** Compatibility mirrors. Version authority is carried by the fields below. */
  current_v: number;
  next_v: number;
  generation_count: number;
  decisions: Decision[];
  evaluation_epoch: EvaluationEpoch;
  epoch_state: EpochState;
  epoch_initialized: boolean;
  version_authority_high_water: number;
  strict_generation_count: number;
  strict_published_versions: number[];
  strict_published_bot_identities: CanonicalGenerationIdentity[];
  active_bots: string[];
  reset_receipt_valid: boolean;
  reset_receipt_digest: string | null;
  stream_authority_digest: string | null;
  reset_receipt_issues: string[];
  operator_action: EpochOperatorAction | null;
  operator_command: string | null;
  runtime_reconciliation_claimed: boolean;
  runtime_reconciliation_kind: "legacy_quarantine" | "recorded_abandon_finalize" | null;
  runtime_reconciliation_claim_digest: string | null;
  runtime_reconciliation_claim_valid: boolean;
  runtime_reconciliation_claim_issues: string[];
  publication_recovery_ready?: boolean;
  unpaired_completion_versions?: number[];
  unpaired_high_water_versions?: number[];
  active_generation: ActiveGeneration | null;
  post_publication_handoff: PostPublicationHandoffStatus;
  ignored_checkpoint: IgnoredCheckpoint | null;
  unpublished_candidate_versions: number[];
  stability_observation: StabilityObservation;
  stability_observation_digest: string;
  operator_transition?: OperatorTransition | null;
  status_sync_error?: string;
  /**
   * Phase A multi-slot / slice2b / cert / eval-wait blocks. Optional for
   * backward compatibility with pre-Phase-A observers; missing means empty /
   * unavailable, never a fail-closed unknown-field rejection.
   */
  active_generations?: GenerationSlot[];
  pipeline_mode?: PipelineModeProjection;
  async_certification?: AsyncCertificationProjection;
  eval_wait?: EvalWaitProjection;
  feature_flags?: FeatureFlagsProjection;
  version_authority?: VersionAuthorityProjection;
}

export interface ControlTaskHealth {
  present: boolean;
  done: boolean | null;
  cancelled: boolean | null;
  shutdown_requested: boolean;
  /** Only an exact live, non-stopping owner may publish transient status. */
  status_eligible: boolean;
  owner_id: string | null;
  lifecycle_revision: number;
}

export interface ControlDaemonHealth {
  configured?: boolean;
  exists?: boolean;
  pid?: number | null;
  alive: boolean;
  process_identity?:
    | "match"
    | "missing"
    | "invalid"
    | "dead"
    | "reused"
    | "unavailable"
    | "unverifiable"
    | "owner_mismatch"
    | "command_mismatch"
    | "group_mismatch";
  heartbeat_status?: "fresh" | "missing" | "invalid" | "future" | "stale" | "not_applicable";
  heartbeat_stale?: boolean;
  heartbeat_age_sec?: number | null;
  health_error?: string | null;
  /** AppState / env / live cmdline pair-worker projection (Phase A). */
  configured_workers?: number;
  configured_pairs?: number;
  env_workers?: number | null;
  env_pairs?: number | null;
  effective_workers?: number | null;
  effective_pairs?: number | null;
  pairs_drift?: boolean;
}

export interface ControlPipelineHealth {
  exists: boolean;
  stage: string | null;
  blocked?: boolean;
  next_v?: number | null;
  source_v?: number | null;
  parent2_v?: number | null;
  run_id?: string | null;
  workflow_run_id?: string | null;
  checkpoint_revision?: number | null;
  authority?: "strict_epoch_projection" | "post_publication_handoff_journal";
  error?: string;
  issues?: string[];
  identity_changed?: boolean;
  identity_mismatches?: string[];
  recovery_blocked?: boolean;
  operator_action_required?: boolean;
  admission_blocked?: boolean;
  terminalization_pending?: boolean;
  gate_outcome?: {
    schema_version: number;
    kind: string;
    gate_name: "quality" | "review" | "critic";
    terminal_stage: "quality_rejected" | "review_rejected" | "critic_rejected";
    reason_code: string;
    failure_class: string;
    disposition: "abandon_generation";
    receipt_digest: string;
  } | null;
  ignored_checkpoint?: IgnoredCheckpoint | null;
  handoff_identity_digest?: string | null;
  handoff_projection_digest?: string | null;
  publication_id?: string | null;
  handoff_owner_scope?: "none" | "current_process" | "foreign_process" | "unknown";
  scheduler_boundary?: {
    authority: "outer_scheduler";
    state: "ready_to_prepare";
    provider_action: "end_stream";
    scheduler_action: "prepare_generation";
    next_v: number | null;
    source_v: number | null;
  } | null;
  route?: PipelineRoute | null;
  recovery?: {
    active?: boolean;
    recoverable?: boolean;
    issues?: string[];
    [key: string]: unknown;
  };
  [key: string]: unknown;
}

export interface ControlHealth {
  overall: "healthy" | "degraded" | "stopped";
  issues: string[];
  status: ControlStatus;
  running: boolean;
  active_generation: ActiveGeneration | null;
  /** Mirrored Phase A multi-slot list (same bytes as ``status.active_generations``). */
  active_generations?: GenerationSlot[];
  task: ControlTaskHealth;
  daemon: ControlDaemonHealth;
  pipeline: ControlPipelineHealth;
  checked_at: number;
}

export interface ControlAbandonResult {
  status: "abandoned" | string;
  operation: "control_abandon_generation" | string;
  transaction_id?: string | null;
  abandoned_v?: number | null;
  abandon_receipt_digest?: string | null;
  message?: string;
  [key: string]: unknown;
}

export function controlPipelineBlocked(
  pipeline: ControlPipelineHealth | null | undefined,
): boolean {
  return Boolean(
    !pipeline
    || pipeline.blocked === true
    || pipeline.recovery?.recoverable === false
    || pipeline.error,
  );
}

export function controlPipelineIssues(
  pipeline: ControlPipelineHealth | null | undefined,
): string[] {
  if (!pipeline) return ["pipeline_health_unavailable"];
  const issues = [
    ...(pipeline.issues ?? []),
    ...(pipeline.recovery?.issues ?? []),
    ...(pipeline.identity_mismatches ?? []).map((field) => `identity_mismatch:${field}`),
    ...(pipeline.error ? [pipeline.error] : []),
  ];
  return [...new Set(issues.filter((issue) => typeof issue === "string" && issue.length > 0))];
}

export function controlPipelineRouteAllowed(
  pipeline: ControlPipelineHealth | null | undefined,
): boolean {
  return Boolean(
    pipeline?.exists === true
    && pipeline.route
    && !controlPipelineBlocked(pipeline),
  );
}

export function controlStartBlocked(
  status: ControlStatus | null | undefined,
  health: ControlHealth | null | undefined,
): boolean {
  return controlStartBlockedReason(status, health) !== null;
}

/**
 * Explain why the read-only health projection cannot authorize a launch.
 * These strings intentionally name the exact failed projection fields so a
 * disabled Start button is diagnosable without attempting the mutation.
 */
export function controlStartBlockedReason(
  status: ControlStatus | null | undefined,
  health: ControlHealth | null | undefined,
): string | null {
  if (!status) return "控制状态权威不可用，暂不能启动";
  if (!health) return "控制健康权威不可用，暂不能启动";
  if (!status.epoch_initialized) return "完成操作员一次性 epoch reset 后才能启动";
  if (status.running) return "编排器已在运行";
  const taskActive = Boolean(health?.task.present && health.task.done === false);
  if (taskActive) return "编排器任务仍持有运行权威";
  if (status.operator_action) return `当前需要操作员动作：${status.operator_action}`;
  if (controlPipelineBlocked(health.pipeline)) {
    const issues = controlPipelineIssues(health.pipeline);
    return `流水线恢复已阻断：${issues.join("、") || "请检查权威诊断"}`;
  }
  const boundaryIssues = controlLaunchBoundaryIssues(status, health);
  if (boundaryIssues.length > 0) {
    return `启动边界未由 health.pipeline 证明：${boundaryIssues.join("、")}`;
  }
  return null;
}

/** Mirror and diagnose the backend's three mutually-exclusive launch boundaries. */
export function controlLaunchBoundaryIssues(
  status: ControlStatus,
  health: ControlHealth,
): string[] {
  const pipeline = health.pipeline;
  const route = pipeline.route;
  const active = status.active_generation;
  const handoff = status.post_publication_handoff;
  const issues: string[] = [];
  const requireField = (ok: boolean, issue: string) => {
    if (!ok) issues.push(issue);
  };

  if (active) {
    for (const issue of canonicalGenerationIdentityIssues(active, active.next_v)) {
      issues.push(`active.canonical_identity.${issue}`);
    }
    requireField(pipeline.exists === true, "active.pipeline.exists");
    requireField(pipeline.authority === "strict_epoch_projection", "active.pipeline.authority");
    requireField(Boolean(route), "active.route");
    requireField(pipeline.next_v === active.next_v, "active.next_v");
    requireField(pipeline.source_v === active.source_v, "active.source_v");
    requireField(pipeline.parent2_v === active.parent2_v, "active.parent2_v");
    requireField(pipeline.stage === active.stage, "active.stage");
    requireField(pipeline.run_id === active.run_id, "active.run_id");
    requireField(pipeline.workflow_run_id === active.workflow_run_id, "active.workflow_run_id");
    requireField(
      pipeline.checkpoint_revision === active.checkpoint_revision,
      "active.checkpoint_revision",
    );
    if (route) {
      const allowedTools = Array.isArray(route.allowed_tools) ? route.allowed_tools : [];
      requireField(route.stage === active.stage, "active.route.stage");
      requireField(route.next_v === active.next_v, "active.route.next_v");
      requireField(route.source_v === active.source_v, "active.route.source_v");
      requireField(route.parent2_v === active.parent2_v, "active.route.parent2_v");
      requireField(Array.isArray(route.allowed_tools), "active.route.allowed_tools");
      requireField(
        route.next_tool === null || allowedTools.includes(route.next_tool),
        "active.route.next_tool",
      );
    }
    return [...new Set(issues)];
  }

  if (!handoff) return ["post_publication_handoff.missing"];
  if (handoff.status !== "none") {
    requireField(handoff.status !== "blocked", "handoff.status");
    requireField(handoff.blocked !== true, "handoff.blocked");
    requireField(pipeline.exists === true, "handoff.pipeline.exists");
    requireField(
      pipeline.authority === "post_publication_handoff_journal",
      "handoff.pipeline.authority",
    );
    requireField(
      pipeline.handoff_projection_digest === handoff.projection_digest,
      "handoff.projection_digest",
    );
    requireField(
      pipeline.handoff_identity_digest === handoff.identity_digest,
      "handoff.identity_digest",
    );
    requireField(
      pipeline.handoff_owner_scope === handoff.owner_scope,
      "handoff.owner_scope",
    );
    requireField(Boolean(route), "handoff.route");
    if (route) {
      const allowedTools = Array.isArray(route.allowed_tools) ? route.allowed_tools : [];
      requireField(route.stage === "post_publication_handoff", "handoff.route.stage");
      requireField(route.next_v === handoff.version, "handoff.route.next_v");
      requireField(route.source_v === handoff.source_v, "handoff.route.source_v");
      requireField(route.parent2_v == null, "handoff.route.parent2_v");
      requireField(route.next_tool === "run_archivist", "handoff.route.next_tool");
      requireField(Array.isArray(route.allowed_tools), "handoff.route.allowed_tools");
      requireField(
        allowedTools.length === 1 && allowedTools[0] === "run_archivist",
        "handoff.route.allowed_tools_exact",
      );
      requireField(route.intent === "post_publication_handoff", "handoff.route.intent");
    }
    return [...new Set(issues)];
  }

  const scheduler = pipeline.scheduler_boundary;
  requireField(pipeline.exists === false, "scheduler.pipeline.exists");
  requireField(pipeline.authority === "strict_epoch_projection", "scheduler.pipeline.authority");
  requireField(!route, "scheduler.route_absent");
  requireField(Boolean(scheduler), "scheduler.boundary");
  if (scheduler) {
    requireField(scheduler.authority === "outer_scheduler", "scheduler.authority");
    requireField(scheduler.state === "ready_to_prepare", "scheduler.state");
    requireField(scheduler.provider_action === "end_stream", "scheduler.provider_action");
    requireField(scheduler.scheduler_action === "prepare_generation", "scheduler.scheduler_action");
    requireField(scheduler.next_v === status.next_v, "scheduler.next_v");
    requireField(scheduler.source_v === null, "scheduler.source_v");
  }
  return [...new Set(issues)];
}

export function controlLaunchBoundaryAllowed(
  status: ControlStatus,
  health: ControlHealth,
): boolean {
  return controlLaunchBoundaryIssues(status, health).length === 0;
}

export function controlSchedulerOwnsPrepareBoundary(
  status: ControlStatus | null | undefined,
  health: ControlHealth | null | undefined,
): boolean {
  const taskActive = Boolean(health?.task.present && health.task.done === false);
  const scheduler = health?.pipeline.scheduler_boundary;
  return Boolean(
    status
    && health
    && status.epoch_initialized
    && status.running
    && health.running
    && health.overall === "healthy"
    && taskActive
    && health.task.shutdown_requested !== true
    && !status.operator_action
    && !status.active_generation
    && status.post_publication_handoff.status === "none"
    && health.pipeline.exists === false
    && scheduler?.authority === "outer_scheduler"
    && scheduler.state === "ready_to_prepare"
    && scheduler.provider_action === "end_stream"
    && scheduler.scheduler_action === "prepare_generation"
    && scheduler.next_v === status.next_v
    && scheduler.source_v === null
    && !controlPipelineBlocked(health.pipeline),
  );
}

/** Primary slot from ``active_generations``, else legacy ``active_generation``. */
export function primaryGenerationSlot(
  status: ControlStatus | null | undefined,
): (ActiveGeneration & { slot_id: "primary" }) | null {
  const slots = status?.active_generations;
  if (Array.isArray(slots)) {
    const primary = slots.find(
      (slot): slot is ActiveGeneration & { slot_id: "primary" } => slot.slot_id === "primary",
    );
    if (primary) return primary;
  }
  if (status?.active_generation) {
    return { ...status.active_generation, slot_id: "primary" };
  }
  return null;
}

/** Draft slots from Phase A ``active_generations`` (empty when absent/legacy). */
export function draftGenerations(
  status: ControlStatus | null | undefined,
): DraftGeneration[] {
  const slots = status?.active_generations;
  if (!Array.isArray(slots)) return [];
  return slots.filter(
    (slot): slot is DraftGeneration => (
      slot.slot_id === "draft" && (slot as DraftGeneration).is_draft === true
    ),
  );
}

/** Stages where HTTP/MCP generic abandon is refused (mirrors backend never_disposable). */
export const CONTROL_NEVER_DISPOSABLE_STAGES = new Set([
  "verified",
  "official_bootstrap_required",
  "official_certifying",
  "official_inconclusive",
  "publishing",
  "archived",
]);

/** True when control status projects an active primary that may accept operator abandon. */
export function controlAbandonAvailable(
  status: ControlStatus | null | undefined,
): boolean {
  const active = primaryGenerationSlot(status) ?? status?.active_generation ?? null;
  if (!active?.stage) return false;
  return !CONTROL_NEVER_DISPOSABLE_STAGES.has(active.stage);
}

export interface Decision {
  tool: string;
  summary: string;
  ts: number;
}

export interface AppConfig {
  mode: string;
  daemon_enabled: boolean;
  daemon_workers: number;
  daemon_pairs: number;
}

export type ControlConfigUpdate = Pick<AppConfig, "daemon_enabled" | "daemon_workers" | "daemon_pairs">;

const BASE = "/api/control";
const CONTROL_TIMEOUT = 30_000;

/** A retryable 503 from the observer (projection refreshing, not a real error). */
export class RetryableControlError extends Error {
  readonly retryAfter: number | null;
  constructor(message: string, retryAfter: number | null) {
    super(message);
    this.name = "RetryableControlError";
    this.retryAfter = retryAfter;
  }
}

async function extractError(res: Response): Promise<never> {
  let msg = `HTTP ${res.status}`;
  let detailObj: { retryable?: boolean; code?: string; message?: string } | null = null;
  try {
    const b = await res.json();
    if (b.detail) {
      const detail = typeof b.detail === "string"
        ? b.detail
        : b.detail.message || b.detail.code || JSON.stringify(b.detail);
      msg += `: ${detail}`;
      if (typeof b.detail === "object" && b.detail !== null) detailObj = b.detail;
    }
  } catch {
    // Keep the status-only message when the error body is not JSON.
  }
  // A retryable observer 503 (projection refreshing during active generation)
  // must NOT be treated as a hard authority failure. Throw a typed error so the
  // caller can keep the previous good status instead of wiping the dashboard.
  if (res.status === 503 && detailObj?.retryable === true) {
    const retryAfter = res.headers.get("Retry-After");
    throw new RetryableControlError(msg, retryAfter ? parseInt(retryAfter, 10) : null);
  }
  throw new Error(msg);
}

async function fetchJSON<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, { ...init, signal: init?.signal ?? AbortSignal.timeout(CONTROL_TIMEOUT) });
  if (!res.ok) return extractError(res);
  return res.json();
}

export const controlApi = {
  status: (signal?: AbortSignal) => fetchJSON<ControlStatus>(`${BASE}/status`, { signal }),
  health: (signal?: AbortSignal) => fetchJSON<ControlHealth>(`${BASE}/health`, { signal }),
  decisions: (limit = 50) => fetchJSON<Decision[]>(`${BASE}/decisions?limit=${limit}`),
  getConfig: () => fetchJSON<AppConfig>(`${BASE}/config`),
  setConfig: (config: Partial<ControlConfigUpdate>) =>
    fetchJSON<AppConfig>(`${BASE}/config`, {
      method: "PUT",
      headers: withOperatorControlHeader({ "Content-Type": "application/json" }),
      body: JSON.stringify(config),
    }),
  start: () => fetchJSON<{ status: string; mode: string }>(`${BASE}/start`, {
    method: "POST",
    headers: withOperatorControlHeader(),
  }),
  stop: () => fetchJSON<{ status: string }>(`${BASE}/stop`, {
    method: "POST",
    headers: withOperatorControlHeader(),
  }),
  /** Operator escape hatch: stop runtime then canonical abandon (leave stopped). */
  abandon: (body?: { reason?: string }) =>
    fetchJSON<ControlAbandonResult>(`${BASE}/abandon`, {
      method: "POST",
      headers: withOperatorControlHeader({ "Content-Type": "application/json" }),
      body: JSON.stringify(body ?? {}),
    }),
  listTools: () => fetchJSON<{
    tools: string[];
    enabled_tools?: string[];
    blocked_tools?: string[];
    epoch_state?: EpochState;
  }>(`${BASE}/tools`),
};
