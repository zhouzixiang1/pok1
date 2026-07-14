export type EvaluationEpoch = "national_tcp_policy_v1";

export type EpochState =
  | "reset_required"
  | "reset_evidence_requires_recovery"
  | "version_authority_requires_recovery"
  | "epoch_authority_unavailable"
  | "fresh_bootstrap_ready"
  | "strict_published";

export interface ActiveGeneration {
  next_v: number;
  source_v: number | null;
  stage: string;
  run_id: string;
  workflow_run_id: string | null;
  attempt: {
    generation: number;
    audit: number;
    precommit: number;
  };
}

export interface IgnoredCheckpoint {
  next_v: number | null;
  source_v: number | null;
  stage: string | null;
  reason: "checkpoint_not_bound_to_strict_epoch";
  issues: string[];
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
  reset_receipt_issues: string[];
  operator_action: string | null;
  operator_command: string | null;
  active_generation: ActiveGeneration | null;
  ignored_checkpoint: IgnoredCheckpoint | null;
  unpublished_candidate_versions: number[];
  status_sync_error?: string;
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
  status: () => fetchJSON<ControlStatus>(`${BASE}/status`),
  decisions: (limit = 50) => fetchJSON<Decision[]>(`${BASE}/decisions?limit=${limit}`),
  getConfig: () => fetchJSON<AppConfig>(`${BASE}/config`),
  setConfig: (config: Partial<AppConfig>) =>
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
import { withOperatorControlHeader } from "./operatorControl";
