import { withOperatorControlHeader } from "./operatorControl";

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

export interface ActiveGeneration {
  next_v: number;
  source_v: number | null;
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

export interface PipelineRoute {
  stage: string;
  next_v: number;
  source_v: number | null;
  parent2_v: number | null;
  next_tool: string | null;
  allowed_tools: string[];
  intent: string;
  failure_class?: string | null;
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
  candidate_version: 143 | null;
  source_v: 142 | null;
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
  next_tool: "run_archivist" | null;
  issues: string[];
  projection_digest: string;
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
}

export interface ControlTaskHealth {
  present: boolean;
  done: boolean | null;
  cancelled: boolean | null;
  shutdown_requested: boolean;
  owner_id?: string | null;
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
}

export interface ControlPipelineHealth {
  exists: boolean;
  stage: string | null;
  next_v?: number | null;
  source_v?: number | null;
  run_id?: string | null;
  workflow_run_id?: string | null;
  checkpoint_revision?: number | null;
  authority?: "strict_epoch_projection" | "post_publication_handoff_journal";
  error?: string;
  issues?: string[];
  route?: PipelineRoute | null;
  recovery?: {
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
  task: ControlTaskHealth;
  daemon: ControlDaemonHealth;
  pipeline: ControlPipelineHealth;
  checked_at: number;
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

async function extractError(res: Response): Promise<never> {
  let msg = `HTTP ${res.status}`;
  try {
    const b = await res.json();
    if (b.detail) {
      const detail = typeof b.detail === "string"
        ? b.detail
        : b.detail.message || b.detail.code || JSON.stringify(b.detail);
      msg += `: ${detail}`;
    }
  } catch {
    // Keep the status-only message when the error body is not JSON.
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
  listTools: () => fetchJSON<{
    tools: string[];
    enabled_tools?: string[];
    blocked_tools?: string[];
    epoch_state?: EpochState;
  }>(`${BASE}/tools`),
};
