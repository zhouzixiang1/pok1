import type {
  BotRating,
  BotStatsEntry,
  BotSummary,
  DaemonStatus,
  GenerationLog,
  H2HEntry,
  HistoryEntry,
  MatchMatrix,
  MatchStats,
  MatchSummary,
  RateLimitStatus,
} from "../api/types.js";
import {
  createEventSourceController,
  type EventSourceController,
  type EventSourceControllerDependencies,
} from "./eventSourceController.js";
import { canonicalGenerationIdentityIssues } from "./canonicalGenerationIdentity.js";
// Re-export the shared validators so existing importers (and any future test
// fixture) keep working without coupling to the new module path.  The
// controllers are the historical owners of these fail-closed guards.
export {
  type JsonObject,
  isObject,
  isNumber,
  isInteger,
  isOptional,
  isNullableNumber,
  isNullableString,
  isStringArray,
  isHexDigest,
  isEpochBlocked,
  isBotRating,
} from "./validators.js";
import {
  type JsonObject,
  isObject,
  isNumber,
  isInteger,
  isOptional,
  isNullableNumber,
  isNullableString,
  isStringArray,
  isHexDigest,
  isEpochBlocked,
  isBotRating,
} from "./validators.js";

export type DataStore = {
  ratings: BotRating[];
  stats: MatchStats | null;
  daemon: DaemonStatus | null;
  rateLimit: RateLimitStatus | null;
  bots: { active: BotSummary[] };
  matches: MatchSummary[];
  matrix: MatchMatrix | null;
  history: HistoryEntry[];
  generations: GenerationLog[];
  h2h: Record<string, H2HEntry>;
  botStats: Record<string, BotStatsEntry>;
  stream: {
    state: "unavailable" | "connecting" | "connected" | "disconnected" | "blocked";
    last_event_at: number | null;
  };
};

export type DataStoreUpdate = DataStore | ((current: DataStore) => DataStore);
export type DataStoreUpdater = (update: DataStoreUpdate) => void;

export function createInitialDataStore(): DataStore {
  return {
    ratings: [],
    stats: null,
    daemon: null,
    rateLimit: null,
    bots: { active: [] },
    matches: [],
    matrix: null,
    history: [],
    generations: [],
    h2h: {},
    botStats: {},
    stream: { state: "unavailable", last_event_at: null },
  };
}

export const initialDataStore = createInitialDataStore();

const DATA_EVENTS = [
  "ratings",
  "daemon",
  "rate_limit",
  "bots",
  "stats",
  "matches",
  "generations",
  "matrix",
  "history",
  "h2h",
  "bot_stats",
] as const;

const isGenerationLogId = (value: unknown): value is string => (
  typeof value === "string"
  && (
    /^[A-Za-z0-9][A-Za-z0-9_.-]*\.txt(?:\.1)?$/.test(value)
    || /^strict@[0-9a-f]{32}@[a-z0-9_]+_io\.txt$/.test(value)
  )
);
const isObjectArray = (value: unknown): value is JsonObject[] => (
  Array.isArray(value) && value.every(isObject)
);

const DAEMON_STATUSES = new Set([
  "blocked",
  "idle",
  "active",
  "degraded",
  "stopped",
  "disabled",
]);
const DAEMON_ACTIVITIES = new Set([
  "waiting_for_first_published_bot",
  "waiting_for_second_published_bot",
  "scheduling_matches",
]);
const STRENGTH_EVIDENCE_STATUSES = new Set([
  "active_pool_empty",
  "active_pool_singleton",
  "awaiting_first_complete_cycle",
  "current_evaluation_cycle",
]);

const isDaemonStatus = (value: unknown): value is DaemonStatus => (
  isObject(value)
  && typeof value.status === "string"
  && DAEMON_STATUSES.has(value.status)
  && isNumber(value.last_update_age_seconds)
  && typeof value.daemon_enabled === "boolean"
  && typeof value.daemon_configured === "boolean"
  && isOptional(value.reason, (item) => item === null || typeof item === "string")
  && isOptional(value.epoch_state, (item) => typeof item === "string")
  && isOptional(value.process_alive, (item) => typeof item === "boolean")
  && isOptional(value.heartbeat_stale, (item) => typeof item === "boolean")
  && isOptional(value.heartbeat_age_seconds, isNullableNumber)
  && isOptional(value.activity_state, (item) => (
    item === null || (typeof item === "string" && DAEMON_ACTIVITIES.has(item))
  ))
  && isOptional(value.active_bot_count, isInteger)
  && isOptional(value.minimum_rating_pool_bots, isInteger)
  && isOptional(value.strength_evidence_available, (item) => typeof item === "boolean")
  && isOptional(value.strength_evidence_status, (item) => (
    typeof item === "string" && STRENGTH_EVIDENCE_STATUSES.has(item)
  ))
  && isOptional(value.strength_evidence_reason, (item) => (
    item === null || typeof item === "string"
  ))
);

const isMatchStats = (value: unknown): value is MatchStats => (
  isObject(value)
  && isInteger(value.total_games)
  && isInteger(value.total_strength_samples)
  && value.strength_sample_unit === "70_hand_match"
  && value.hands_per_strength_sample === 70
  && isInteger(value.total_pairs)
  && isInteger(value.total_periods)
  && typeof value.most_active_pair === "string"
  && isInteger(value.most_active_count)
);

const isMatchSummary = (value: unknown): value is MatchSummary => (
  isObject(value)
  && typeof value.id === "string"
  && typeof value.timestamp === "string"
  && value.execution_mode === "native_tcp"
  && value.evaluation_epoch === "national_tcp_policy_v1"
  && isHexDigest(value.evaluation_identity_digest)
  && typeof value.bot0 === "string"
  && typeof value.bot1 === "string"
  && value.bot0 !== value.bot1
  && isInteger(value.bot0_wins)
  && isInteger(value.bot1_wins)
  && isInteger(value.draws)
  && value.strength_sample_unit === "70_hand_match"
  && value.hands_per_strength_sample === 70
  && value.strength_admitted === true
  && value.strength_complete === true
  && value.strength_compliance_passed === true
  && isInteger(value.strength_sample_count)
  && value.strength_sample_count > 0
  && Array.isArray(value.net_chips_bot0)
  && value.net_chips_bot0.length === value.strength_sample_count
  && value.net_chips_bot0.every(isNumber)
);

const isHistoryEntry = (value: unknown): value is HistoryEntry => (
  isObject(value)
  && isInteger(value.period)
  && typeof value.timestamp === "string"
  && isObject(value.ratings)
  && Object.values(value.ratings).every((rating) => (
    isObject(rating) && isNumber(rating.r) && isNumber(rating.rd)
  ))
  && isOptional(value.win_rates, (winRates) => (
    isObject(winRates)
    && Object.values(winRates).every((row) => (
      isObject(row)
      && isInteger(row.games)
      && isOptional(row.h2h_avg_wr, isNullableNumber)
    ))
  ))
);

const isH2HEntry = (value: unknown): value is H2HEntry => (
  isObject(value)
  && isInteger(value.games)
  && isInteger(value.a_wins)
  && isInteger(value.b_wins)
  && isInteger(value.draws)
  && isNumber(value.win_rate)
);

const isBotStatsEntry = (value: unknown): value is BotStatsEntry => (
  isObject(value)
  && isInteger(value.wins)
  && isInteger(value.losses)
  && isInteger(value.draws)
  && isInteger(value.games)
  && isNumber(value.win_rate)
);

const OFFICIAL_STATUSES = new Set([
  "local-pass",
  "official-smoke-pass",
  "official-compliance-pass",
  "official-pending",
  "official-certified",
  "official-inconclusive",
  "official-failed",
  "official-uncertified",
  // Two-tier publication: staging tag published, async official cert pending.
  "official-staging",
  "official-unavailable",
]);
const OFFICIAL_MODES = new Set(["smoke", "compliance", "full"]);
const FORMAL_AUTHORITIES = new Set([
  "signed_full_v5",
  "staging_uncertified",
  "none",
  "pipeline_attached_full_v5_job",
]);

const isFormalSummary = (value: unknown): boolean => (
  isObject(value)
  && [
    "self_play_rounds",
    "opponent_rounds",
    "target_hands",
    "rounds_requested",
    "rounds_run",
    "passed_rounds",
    "failed_rounds",
  ].every((key) => isInteger(value[key]))
);

const isOfficialCertification = (value: unknown): boolean => {
  if (
    !isObject(value)
    || typeof value.bot !== "string"
    || typeof value.status !== "string"
    || !OFFICIAL_STATUSES.has(value.status)
  ) return false;
  if (!isOptional(value.status_label, (item) => typeof item === "string")) return false;
  if (!isOptional(value.mode, (item) => (
    item === null || (typeof item === "string" && OFFICIAL_MODES.has(item))
  ))) return false;
  for (const key of ["policy_id", "updated_at", "workflow_run_id", "certification_profile", "opponent_authority"]) {
    if (!isOptional(value[key], isNullableString)) return false;
  }
  for (const key of ["cache_hit", "queued", "formal_certified", "epoch_initialized"]) {
    if (!isOptional(value[key], (item) => typeof item === "boolean")) return false;
  }
  for (const key of [
    "cache_key",
    "reason",
    "certification_root",
    "certificate_digest",
    "certificate_signature_sha256",
    "published_attestation_digest",
    "epoch_state",
  ]) {
    if (!isOptional(value[key], (item) => typeof item === "string")) return false;
  }
  if (!isOptional(value.issues, isStringArray)) return false;
  for (const key of ["summary", "compliance_verdict", "result", "official_verdict_ledger_entry"]) {
    if (!isOptional(value[key], isObject)) return false;
  }
  if (!isOptional(value.certificate_schema_version, isInteger)) return false;
  if (!isOptional(value.formal_authority, (item) => (
    typeof item === "string" && FORMAL_AUTHORITIES.has(item)
  ))) return false;
  if (!isOptional(value.publication_tier, (item) => (
    item === "staging" || item === "certified"
  ))) return false;
  if (!isOptional(value.formal_summary, (item) => item === null || isFormalSummary(item))) return false;
  if (!isOptional(value.subject_kind, (item) => (
    item === "strict_published" || item === "active_candidate"
  ))) return false;
  if (!isOptional(value.evaluation_epoch, (item) => item === "national_tcp_policy_v1")) return false;
  if (!isOptional(value.candidate_version, (item) => item === null || isInteger(item))) return false;
  for (const key of ["strength_evidence_weight", "strategy_evidence_weight"]) {
    if (!isOptional(value[key], isNumber)) return false;
  }
  return true;
};

const isBotSummary = (value: unknown): value is BotSummary => {
  if (
    !isObject(value)
    || typeof value.name !== "string"
    || !isInteger(value.version)
    || value.name !== value.canonical_bot_name
    || value.version !== value.canonical_version
    || canonicalGenerationIdentityIssues(
      value as unknown as BotSummary,
      value.version,
    ).length > 0
    || typeof value.completed !== "boolean"
    || !isInteger(value.total_lines)
    || !isStringArray(value.files)
    || !(
      value.rating === null
      || (
        isObject(value.rating)
        && isNumber(value.rating.r)
        && isNumber(value.rating.rd)
        && isNumber(value.rating.conservative)
      )
    )
    || value.active !== true
    || value.tagged !== true
    || value.reaped !== false
    || value.protocol_eligible !== true
    || !Array.isArray(value.protocol_errors)
    || value.protocol_errors.length !== 0
    || value.lifecycle_status !== "active"
    || typeof value.strength_evidence_available !== "boolean"
    || !["current_evaluation_cycle", "awaiting_first_rating_cycle"].includes(
      String(value.strength_evidence_status ?? ""),
    )
  ) {
    return false;
  }
  if (!isOptional(value.status_label, (item) => typeof item === "string")) return false;
  if (!isOptional(value.status_reasons, isStringArray)) return false;
  if (!isOptional(value.win_rate, isNullableNumber)) return false;
  for (const key of [
    "h2h_avg_wr",
    "h2h_weighted_wr",
    "primary_70_hand_match_score",
    "secondary_net_chips_total",
    "secondary_net_chips_mean",
  ]) {
    if (!isOptional(value[key], isNullableNumber)) return false;
  }
  for (const key of [
    "games",
    "h2h_games",
    "h2h_opponents",
    "h2h_opponents_total",
    "h2h_coverage",
    "leaderboard_score",
    "selection_score",
    "selection_penalty",
    "strength_sample_count",
  ]) {
    if (!isOptional(value[key], isNumber)) return false;
  }
  for (const key of ["h2h_source", "rank_basis", "strength_confidence", "strength_note"]) {
    if (!isOptional(value[key], (item) => typeof item === "string")) return false;
  }
  if (!isOptional(value.strength_order_contract, isStringArray)) return false;
  if (!isOptional(value.official_certification, isOfficialCertification)) return false;
  return true;
};

export function validateDataStreamEvent(eventType: string, value: unknown): boolean {
  switch (eventType) {
    case "ratings":
      return Array.isArray(value) && value.every(isBotRating);
    case "daemon":
      return isDaemonStatus(value);
    case "rate_limit":
      return isObject(value)
        && typeof value.blocked === "boolean"
        && isOptional(value.reset_time, (item) => typeof item === "string")
        && isOptional(value.wait_seconds, isNumber);
    case "bots":
      return isObject(value)
        && Array.isArray(value.active)
        && value.active.every(isBotSummary);
    case "stats":
      return isMatchStats(value);
    case "matches":
      return Array.isArray(value) && value.every(isMatchSummary);
    case "generations":
      return isObjectArray(value)
        && value.every((item) => (
          /^v[1-9][0-9]*$/.test(String(item.version || ""))
          && Array.isArray(item.files)
          && item.files.every(isGenerationLogId)
        ));
    case "matrix": {
      if (!isObject(value) || !isStringArray(value.bots) || !Array.isArray(value.matrix)) {
        return false;
      }
      const bots = value.bots;
      const matrix = value.matrix;
      return matrix.every((row) => (
          Array.isArray(row)
          && row.every((item) => item === null || isNumber(item))
        ))
        && matrix.length === bots.length
        && matrix.every((row) => row.length === bots.length)
        && value.source === "h2h"
        && typeof value.evidence_available === "boolean";
    }
    case "history":
      return Array.isArray(value) && value.every(isHistoryEntry);
    case "h2h":
      return isObject(value)
        && Object.values(value).every(isH2HEntry);
    case "bot_stats":
      return isObject(value)
        && Object.values(value).every(isBotStatsEntry);
    default:
      return false;
  }
}

export function createDataStreamController(
  updateStore: DataStoreUpdater,
  authorityKey: string,
  dependencies: EventSourceControllerDependencies = {},
): EventSourceController {
  return createEventSourceController({
    url: `/api/data/stream?authority=${encodeURIComponent(authorityKey)}`,
    events: DATA_EVENTS,
    pingEvent: "ping",
    epochBlockedEvent: "epoch_blocked",
    validatePing: isObject,
    validateEvent: validateDataStreamEvent,
    validateEpochBlocked: isEpochBlocked,
    onConnecting: () => updateStore((value) => ({
      ...value,
      daemon: null,
      stream: { state: "connecting", last_event_at: null },
    })),
    onOpen: () => updateStore((value) => ({
      ...value,
      stream: {
        state: "connected",
        last_event_at: value.stream.last_event_at,
      },
    })),
    onEvent: (eventType, data) => {
      switch (eventType) {
        case "ratings":
          updateStore((value) => ({ ...value, ratings: data as BotRating[] }));
          break;
        case "daemon":
          updateStore((value) => ({ ...value, daemon: data as DaemonStatus }));
          break;
        case "rate_limit":
          updateStore((value) => ({ ...value, rateLimit: data as RateLimitStatus }));
          break;
        case "bots":
          updateStore((value) => ({ ...value, bots: data as DataStore["bots"] }));
          break;
        case "stats":
          updateStore((value) => ({ ...value, stats: data as MatchStats }));
          break;
        case "matches":
          updateStore((value) => ({ ...value, matches: data as MatchSummary[] }));
          break;
        case "generations":
          updateStore((value) => ({ ...value, generations: data as GenerationLog[] }));
          break;
        case "matrix":
          updateStore((value) => ({ ...value, matrix: data as MatchMatrix }));
          break;
        case "history":
          updateStore((value) => ({ ...value, history: data as HistoryEntry[] }));
          break;
        case "h2h":
          updateStore((value) => ({ ...value, h2h: data as Record<string, H2HEntry> }));
          break;
        case "bot_stats":
          updateStore((value) => ({ ...value, botStats: data as Record<string, BotStatsEntry> }));
          break;
      }
    },
    onLiveness: (_eventType, observedAt) => updateStore((value) => ({
      ...value,
      stream: { state: "connected", last_event_at: observedAt },
    })),
    onTransportError: () => updateStore((value) => ({
      ...value,
      daemon: null,
      stream: { state: "disconnected", last_event_at: null },
    })),
    onEpochFence: () => updateStore({
      ...createInitialDataStore(),
      stream: { state: "blocked", last_event_at: null },
    }),
  }, dependencies);
}
