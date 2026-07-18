export interface BotRating {
  name: string;
  rank?: number;
  rating: number;
  rd: number;
  sigma: number;
  conservative_rating: number;
  confidence: string;
  last_period: string;
  win_rate?: number | null;
  games?: number;
  h2h_avg_wr?: number | null;
  h2h_weighted_wr?: number | null;
  h2h_games?: number;
  h2h_opponents?: number;
  h2h_opponents_total?: number;
  h2h_coverage?: number;
  h2h_source?: string;
  leaderboard_score?: number;
  selection_score?: number;
  selection_penalty?: number;
  primary_70_hand_match_score?: number | null;
  secondary_net_chips_total?: number | null;
  secondary_net_chips_mean?: number | null;
  strength_sample_count?: number;
  strength_order_contract?: string[];
  rank_basis?: string;
  strength_confidence?: string;
  strength_note?: string;
}

export interface MatchStats {
  /** Compatibility alias; one unit is a complete 70-hand native TCP match. */
  total_games: number;
  total_strength_samples: number;
  strength_sample_unit: "70_hand_match";
  hands_per_strength_sample: 70;
  total_pairs: number;
  total_periods: number;
  most_active_pair: string;
  most_active_count: number;
}

export interface MatchMatrix {
  bots: string[];
  matrix: (number | null)[][];
  source: "h2h";
  evidence_available: boolean;
}

export interface H2HEntry {
  games: number;
  a_wins: number;
  b_wins: number;
  draws: number;
  win_rate: number;
}

export interface BotStatsEntry {
  wins: number;
  losses: number;
  draws: number;
  games: number;
  win_rate: number;
}

export interface HistoryEntry {
  period: number;
  timestamp: string;
  ratings: Record<string, { r: number; rd: number }>;
  win_rates?: Record<string, { h2h_avg_wr?: number | null; games: number }>;
}

export interface GenerationLog {
  version: string;
  files: string[];
}

export interface LogContent {
  version: string;
  filename: string;
  content: string;
}

export interface DaemonStatus {
  status: "blocked" | "idle" | "active" | "degraded" | "stopped" | "disabled";
  reason?: string | null;
  epoch_state?: string;
  /** Age of the currently published strength cycle, not process liveness. */
  last_update_age_seconds: number;
  daemon_enabled: boolean;
  daemon_configured: boolean;
  process_alive?: boolean;
  heartbeat_stale?: boolean;
  heartbeat_age_seconds?: number | null;
  activity_state?: "waiting_for_first_published_bot" | "waiting_for_second_published_bot" | "scheduling_matches" | null;
  active_bot_count?: number;
  minimum_rating_pool_bots?: number;
  strength_evidence_available?: boolean;
  strength_evidence_status?: "active_pool_empty" | "active_pool_singleton" | "awaiting_first_complete_cycle" | "current_evaluation_cycle";
  strength_evidence_reason?: string | null;
}

export interface RateLimitStatus {
  blocked: boolean;
  reset_time?: string;
  wait_seconds?: number;
}

export interface MatchSummary {
  id: string;
  timestamp: string;
  execution_mode: "native_tcp";
  evaluation_epoch: "national_tcp_policy_v1";
  evaluation_identity_digest: string;
  bot0: string;
  bot1: string;
  bot0_wins: number;
  bot1_wins: number;
  draws: number;
  strength_sample_unit: "70_hand_match";
  hands_per_strength_sample: 70;
  strength_admitted: true;
  strength_complete: true;
  strength_compliance_passed: true;
  strength_sample_count: number;
  net_chips_bot0: number[];
}

export type NativeStreet = "preflop" | "flop" | "turn" | "river";
export type NativeActionName = "fold" | "call" | "check" | "raise" | "allin";

export interface NativeReplayAction {
  player_idx: 0 | 1;
  stage: NativeStreet;
  action: NativeActionName;
  amount: number | null;
  pot_before: number | null;
  pot_after: number | null;
  player_bets_before: [number, number] | null;
  decision_wait_sec?: number;
  timeout_budget_sec?: number;
}

export interface NativeHandSettlement {
  earnings: [number, number];
  pot?: number;
  is_showdown: boolean;
  winner_idx: 0 | 1 | null;
  reason: string;
}

export interface NativeHandRecord {
  hand: number;
  sb_idx: 0 | 1;
  bb_idx: 0 | 1;
  hole_cards: [[string, string], [string, string]];
  board: string[];
  actions: NativeReplayAction[];
  starting_pot: number;
  settlement: NativeHandSettlement;
}

export interface NativeExecutionIdentity {
  schema_version: 1;
  mode: "direct_content_bound_policy_artifact";
  label: string;
  artifact_hash: string;
  entrypoint: string;
  entry_digest: string;
  policy_digest: string;
  precompute_digest: string;
  runtime_manifest_digest: string;
  artifact_contract_digest: string;
  epoch_receipt_digest: string;
  identity_digest: string;
}

export interface NativeGameReplay {
  bot_a: string;
  bot_b: string;
  hands_requested: 70;
  hands_played: 70;
  net_chips_a: number;
  net_chips_b: number;
  execution_mode: "native_tcp";
  artifact_execution: {
    schema_version: 1;
    mode: "direct_content_bound_policy_artifact";
    by_player: Record<string, NativeExecutionIdentity>;
  };
  hand_records: NativeHandRecord[];
  settlements: Array<NativeHandSettlement & { hand: number }>;
  passed_compliance: true;
  issues: [];
}

export interface MatchReplayData extends MatchSummary {
  replay_schema_version: 1;
  games: NativeGameReplay[];
}

// Bot management
export type OfficialCertificationState =
  | "local-pass"
  | "official-smoke-pass"
  | "official-compliance-pass"
  | "official-pending"
  | "official-certified"
  | "official-inconclusive"
  | "official-failed"
  | "official-uncertified"
  | "official-unavailable";

export interface OfficialCertification {
  bot: string;
  status: OfficialCertificationState;
  status_label?: string;
  mode?: "smoke" | "compliance" | "full" | null;
  policy_id?: string | null;
  updated_at?: string | null;
  cache_hit?: boolean;
  queued?: boolean;
  cache_key?: string;
  reason?: string;
  issues?: string[];
  summary?: Record<string, unknown>;
  compliance_verdict?: Record<string, unknown>;
  result?: Record<string, unknown>;
  certification_root?: string;
  certificate_schema_version?: number;
  certificate_digest?: string;
  certificate_signature_sha256?: string;
  published_attestation_digest?: string;
  official_verdict_ledger_entry?: Record<string, unknown>;
  /** Backend-validated publication authority; the UI must not reconstruct it. */
  formal_certified?: boolean;
  formal_authority?: "signed_full_v5" | "none" | "pipeline_attached_full_v5_job";
  formal_summary?: {
    self_play_rounds: number;
    opponent_rounds: number;
    target_hands: number;
    rounds_requested: number;
    rounds_run: number;
    passed_rounds: number;
    failed_rounds: number;
  } | null;
  subject_kind?: "strict_published" | "active_candidate";
  evaluation_epoch?: "national_tcp_policy_v1";
  epoch_state?: string;
  epoch_initialized?: boolean;
  workflow_run_id?: string | null;
  candidate_version?: number | null;
  certification_profile?: string | null;
  opponent_authority?: "system_control" | "strict_published_pool" | string | null;
  strength_evidence_weight?: number;
  strategy_evidence_weight?: number;
}

export interface OfficialCertificationProgressRound {
  kind: "self_play" | "opponent";
  index: number;
  passed: boolean;
  hands_started: number;
  settlements: number;
  observed_bytes: number;
  duration_sec: number | null;
  issue_count: number;
}

export interface OfficialCertificationJob {
  job_id: string;
  state: "created" | "queued" | "starting" | "running" | "finalizing" | "cancel_requested" | "completed" | "failed" | "cancelled";
  phase?: string;
  pending?: boolean;
  attempt?: number;
  revision?: number;
  candidate?: string;
  workflow_run_id: string;
  candidate_version: number;
  evaluation_epoch: "national_tcp_policy_v1";
  epoch_initialized: true;
  formal_policy_id: "official-full-v5";
  formal_mode: "full";
  formal_authority: "pipeline_attached_full_v5_job" | "operator_bootstrap_full_v5_job";
  bootstrap_control_id?: string | null;
  read_only?: boolean;
  cancel_allowed?: false;
  progress?: {
    suite_attempt: number;
    rounds_requested: number;
    rounds_completed: number;
    rounds_passed: number;
    active_round: OfficialCertificationProgressRound | null;
    rounds: OfficialCertificationProgressRound[];
  };
  /** Exact terminal/running status projection; never infer it from job.state. */
  status?: Record<string, unknown> | null;
  official_status?: OfficialCertificationState | null;
  compliance_verdict?: {
    ok: boolean;
    classification: string;
    blocking: boolean;
    inconclusive: boolean;
  } | null;
  issues?: string[];
  certificate_digest?: string | null;
  certification_profile?: string | null;
  opponent_authority?: "system_control" | "strict_published_pool" | string | null;
  formal_profile?: {
    self_play_rounds: number;
    opponent_rounds: number;
    target_hands: number;
  } | null;
  strength_evidence_weight?: number;
  strategy_evidence_weight?: number;
}

export interface OfficialCertificationJobsProjection {
  schema_version: 1;
  evaluation_epoch: "national_tcp_policy_v1";
  epoch_state: string;
  epoch_initialized: boolean;
  workflow_run_id: string | null;
  candidate_version: number | null;
  formal_policy_id: "official-full-v5";
  formal_mode: "full";
  pending: number;
  running: number;
  jobs: OfficialCertificationJob[];
  operator_transition?: import("./control").OperatorTransition | null;
}

export interface BotSummary {
  name: string;
  version: number;
  /** Backend-owned dual identity; the browser must not recompute either field. */
  generation_ordinal: number;
  canonical_version: number;
  canonical_bot_name: string;
  canonical_tag: string;
  completed: boolean;
  total_lines: number;
  files: string[];
  rating: { r: number; rd: number; conservative: number } | null;
  win_rate?: number | null;
  games?: number;
  h2h_avg_wr?: number | null;
  h2h_weighted_wr?: number | null;
  h2h_games?: number;
  h2h_opponents?: number;
  h2h_opponents_total?: number;
  h2h_coverage?: number;
  h2h_source?: string;
  leaderboard_score?: number;
  selection_score?: number;
  selection_penalty?: number;
  primary_70_hand_match_score?: number | null;
  secondary_net_chips_total?: number | null;
  secondary_net_chips_mean?: number | null;
  strength_sample_count?: number;
  strength_order_contract?: string[];
  rank_basis?: string;
  strength_confidence?: string;
  strength_note?: string;
  strength_evidence_available?: boolean;
  strength_evidence_status?: "current_evaluation_cycle" | "awaiting_first_rating_cycle";
  active?: true;
  tagged?: true;
  reaped?: false;
  protocol_eligible?: true;
  protocol_errors?: [];
  lifecycle_status?: "active";
  status_label?: string;
  status_reasons?: string[];
  official_certification?: OfficialCertification;
}

export interface BotDetail extends BotSummary {
  parent?: string;
}

// Pipeline
export interface DirectionAudit {
  repetition_detected: boolean;
  exhausted_directions: string[];
  mandatory_constraints: string | null;
  suggested_direction: string | null;
  confidence: string;
  resolved: boolean;
}

export interface MasterPlanTask {
  worker_id?: string;
  role?: string;
  target_files?: string[];
  difficulty?: string;
  [key: string]: unknown;
}

export interface MasterPlanProjection {
  tasks: MasterPlanTask[];
  [key: string]: unknown;
}

export interface PipelineGateResult {
  passed?: boolean;
  all_passed?: boolean;
  /** Critic `approved` records successful advisory-role execution, not advice. */
  approved?: boolean;
  /** The actual non-authoritative Critic recommendation. */
  advisory_approved?: boolean;
  raw_approved?: boolean;
  advisory_score?: number;
  schema_valid?: boolean;
  llm_invoked?: boolean;
  critic_llm_executed?: boolean;
  llm_failed?: boolean;
  parse_failed?: boolean;
  score?: number;
  quality_score?: number;
  feedback?: string;
  strategic_assessment?: string;
  decision_pass_rate?: number;
  /** Typed native acceptance projection; UI must display, never recompute it. */
  national_acceptance?: NativeAcceptanceTimingProjection;
  [key: string]: unknown;
}

export interface NativeAcceptanceTimingProjection {
  timing_ok?: boolean;
  coverage_ok?: boolean;
  conclusive?: boolean;
  expected_hands?: number;
  observed_hands?: number[];
  native_match_timing_plan_digest?: string;
  native_match_timeout_phase?: string | null;
  native_terminal_abort?: { code?: string } | null;
  issues?: string[];
  [key: string]: unknown;
}

export interface PipelineCheckpoint {
  checkpoint_schema_version: 2;
  /** Monotonic CAS identity; required before an API checkpoint may be shown. */
  checkpoint_revision: number;
  evaluation_epoch: "national_tcp_policy_v1";
  workflow_run_id: string;
  run_id: string;
  next_v: number;
  source_v: number | null;
  stage: string;
  master_plan?: MasterPlanProjection | null;
  reviewer_feedback?: string;
  /** Append-only, content-bound Reviewer verdicts. Infrastructure/schema
   * failures never appear here and therefore never consume the two-verdict
   * budget for one immutable artifact/Quality cycle. */
  review_attempt_journal?: PipelineReviewAttempt[];
  generation_attempt?: number;
  gate_results?: Record<string, PipelineGateResult>;
  direction_audit?: DirectionAudit;
  worker_failure_count?: number;
  parent2_v?: number | null;
  timestamp?: string;
  audit_context?: Record<string, unknown>;
  last_stage_change_ts?: number;
}

export interface PipelineReviewAttempt {
  schema_version: 1;
  kind: "pipeline-review-verdict-attempt-v1";
  workflow_run_id: string;
  attempt: 1 | 2;
  cycle_digest: string;
  authority_slot: "review" | "review:retry";
  approved: boolean;
  input_checkpoint_revision: number;
  candidate_artifact_hash: string;
  quality_gate_digest: string;
  receipt_digest: string;
}

export interface WorkerFailure {
  gen: number;
  worker_id: number | string;
  role: string;
  error: string;
  timestamp?: number;
  // Current rows are shown only when these identities match the active strict
  // workflow. The backend never backfills them onto old JSONL records.
  evaluation_epoch: "national_tcp_policy_v1";
  workflow_run_id: string;
  category: "worker" | "gate";
  failure_type?: string;
}

// Prompts
export interface PromptInfo {
  name: string;
  filename?: string;
  exists: boolean;
  lines: number;
  mtime: number | null;
  mtime_str?: string;
  role: string;
  editable: false;
  mutation_authority: "source_control_only";
}

// Orchestrator session
export interface OrchestratorSession {
  session_id: null;
  active: false;
  blocked?: boolean;
  resume_supported: false;
  provider_history_persisted: false;
  recovery_authority: "validated_checkpoint_only";
  history_policy: "fresh_provider_session_from_checkpoint_projection_only";
  epoch_state?: string;
  operator_action?: string | null;
}

// Orchestrator log file
export interface OrchestratorLogFile {
  filename: string;
  size_bytes: number;
  mtime: number;
}

// System events
export interface SystemEvent {
  ts: number;
  type: string;
  severity: "info" | "warn" | "error" | "success";
  message: string;
  data?: Record<string, unknown>;
  // Phase 2+3 log redesign: correlation/category fields emitted by the unified
  // event bus and auto-injected. Old records are backfilled by the backend
  // reader, so these are all optional. category may also be present at the top
  // level (mirrored from data by the reader) for convenience.
  category?: string;
  run_id?: string;
  stage?: string | null;
  attempt?: number | object;
  pid?: number;
  failure_mode?: string;
}

export interface SystemEventsResponse {
  events: SystemEvent[];
  total: number;
  authority_status?: "policy_epoch_not_initialized" | "current_epoch_empty" | "current_epoch";
  evaluation_epoch?: "national_tcp_policy_v1";
  epoch_reset_receipt_digest?: string;
}

export interface WorkerFailuresResponse {
  failures: WorkerFailure[];
  total: number;
}

// National Web Arena (local diagnostics/presentation; official EXE remains authoritative)
export type ArenaMode = "external_tcp" | "managed_bots";
export type ArenaStatus =
  | "created"
  | "starting"
  | "listening"
  | "waiting_for_players"
  | "ready"
  | "running"
  | "stopping"
  | "finalizing"
  | "finished"
  | "failed"
  | "stopped"
  | "quarantined";

export interface ArenaCertificationSnapshot {
  status?: string | null;
  mode?: string | null;
  official_full_certified?: boolean;
  official_exe_passed?: boolean;
  arena_launch_eligible?: boolean;
  eligibility_basis?: "official_full" | "ineligible";
  authority: "windows_exe";
  error?: string;
}

export interface ArenaBot {
  id: string;
  version: number | null;
  display_name: string;
  launchable: boolean;
  native_contract: "passed";
  certification: ArenaCertificationSnapshot;
  artifact_identity: Record<string, string | null>;
  result_authority: "diagnostic_only";
  selection_authority: "official_windows_exe";
}

export interface ArenaSession {
  session_id: string;
  mode: ArenaMode;
  status: ArenaStatus;
  host: string;
  port: number;
  hands_total: number;
  hands_completed: number;
  action_timeout_seconds: number;
  official_action_delay: number;
  top_bot: string | null;
  bottom_bot: string | null;
  top_player_name: string | null;
  bottom_player_name: string | null;
  connected_players: number;
  top_total_earnings: number;
  bottom_total_earnings: number;
  winner: string | null;
  illegal_actions: [number, number];
  timeouts: [number, number];
  last_event_id: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  failure_reason: string | null;
  cleanup_completed: boolean;
  resource_fence_held: boolean;
  quarantine_reason: string | null;
  artifacts: Record<string, string>;
  managed_bot_identities: Record<string, Record<string, string | null>>;
  official_certification: Record<string, ArenaCertificationSnapshot | string>;
  result_authority: "diagnostic_only";
  affects_glicko: false;
  official_exe_certification: false;
  compliance_oracle: "official_windows_exe";
  wire_log_complete: boolean;
  schema_version: 3;
  requested_port: number | null;
  capacity_wait_seconds: number;
  evaluation_epoch: "national_tcp_policy_v1";
  epoch_authority_identity: string;
  epoch_reset_receipt_digest: string;
  epoch_authority_state: string;
  workflow_run_id: string | null;
}

export interface ArenaEvent {
  event_id: number;
  session_id: string;
  type: string;
  timestamp: string;
  hand_no: number;
  payload: Record<string, unknown>;
}

export interface ArenaWireRecord {
  sequence: number;
  session_id: string;
  player_idx: 0 | 1;
  peer: string;
  timestamp: number;
  direction: "server_to_bot" | "bot_to_server";
  phase: "chunk" | "message";
  payload: string;
  byte_count: number;
  message_type?: "name" | "action";
}

export interface ArenaCreatePayload {
  mode: ArenaMode;
  host: string;
  port: number;
  hands: number;
  action_timeout_seconds: number;
  official_action_delay: number;
  top_bot?: string | null;
  bottom_bot?: string | null;
}

export interface ArenaEpochAuthority {
  evaluation_epoch: "national_tcp_policy_v1";
  state: string;
  initialized: boolean;
  reset_receipt_valid?: boolean;
  reset_receipt_digest?: string | null;
  workflow_run_id?: string | null;
}

export interface ArenaEpochMetadata {
  evaluation_epoch: "national_tcp_policy_v1";
  epoch_state: string;
  epoch_reset_receipt_digest?: string | null;
  epoch_initialized: boolean;
  epoch_authority: ArenaEpochAuthority;
  result_authority: "diagnostic_only";
  affects_glicko: false;
  official_exe_certification: false;
  can_certify: false;
}

export interface ArenaBotsResponse extends ArenaEpochMetadata {
  bots: ArenaBot[];
  selection_contract: "strict_epoch_unavailable" | "active_tagged_native_and_official_eligible";
  selection_authority: "official_windows_exe";
}

export interface ArenaSessionsResponse extends ArenaEpochMetadata {
  sessions: ArenaSession[];
}

export interface ArenaSessionUnavailable extends ArenaEpochMetadata {
  session: null;
  requested_session_id: string;
}

export interface ArenaEventHistoryResponse extends ArenaEpochMetadata {
  events: ArenaEvent[];
  after_event_id: number;
  high_watermark: number;
  next_after_event_id: number;
}

export interface ArenaWireHistoryResponse extends ArenaEpochMetadata {
  records: ArenaWireRecord[];
  after_sequence: number;
  complete: boolean;
}
