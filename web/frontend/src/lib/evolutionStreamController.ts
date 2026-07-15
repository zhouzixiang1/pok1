import type { BotRating } from "../api/types.js";
import type { PostPublicationHandoffStatus } from "../api/control.js";
import {
  createEventSourceController,
  type EventSourceController,
  type EventSourceControllerDependencies,
} from "./eventSourceController.js";

export type StreamType = "prompt" | "claude" | "thinking" | "tool" | "tool_result" | "error" | "default";

export interface GenerationCostPolicyState {
  policy_id: string;
  enforcement_mode: "monitor_only" | "operator_hard_limit";
  warning_usd: number;
  hard_limit_usd: number | null;
  receipt_sha256: string;
  binding_sha256?: string;
  ledger_errors?: string[];
  configuration_from_llm_input: false;
  same_uid_llm_resistance: false;
  candidate_sandbox_mutable: false;
  workflow_guarded_paths: true;
}

export type EvolutionEventType =
  | "history"
  | "status"
  | "io"
  | "clear_io"
  | "eval_table"
  | "daemon_stats"
  | "header"
  | "cost"
  | "generation_cost_policy"
  | "metrics"
  | "tool_call"
  | "log_event"
  | "log_event_dropped"
  | "system_event"
  | "post_publication_handoff";

export interface IOLine {
  text: string;
  streamType: StreamType;
  ts: number;
  role?: string;
}

export type EvolutionHandlers = {
  onHistory?: (msg: string, status: string) => void;
  onStatus?: (msg: string, isWorking: boolean) => void;
  onIO?: (line: IOLine) => void;
  onClearIO?: () => void;
  onEvalTable?: (rows: BotRating[]) => void;
  onDaemonStats?: (data: { total_matches: number; total_periods: number; total_games: number; n_bots: number }) => void;
  onHeader?: (msg: string) => void;
  onCost?: (data: {
    role: string;
    cost_usd: number;
    input_tokens: number;
    output_tokens: number;
    gen_total: number;
    grand_total: number;
  }) => void;
  onGenerationCostPolicy?: (data: {
    generation_id: string | null;
    spent_usd: number;
    policy: GenerationCostPolicyState | null;
  }) => void;
  onMetrics?: (metrics: Record<string, number>) => void;
  onToolCall?: (data: { tool_name: string; args: Record<string, unknown>; ts: number; role?: string }) => void;
  onLogEvent?: (data: {
    level: "debug" | "info" | "warn" | "error";
    logger: string;
    msg: string;
    ts: number;
  }) => void;
  onLogEventDropped?: (data: {
    level: "warn";
    logger: string;
    msg: string;
    dropped_count: number;
    max_rate: number;
    ts: number;
  }) => void;
  onSystemEvent?: (data: {
    ts: number;
    type: string;
    severity: "info" | "warn" | "error" | "success";
    message: string;
    data?: Record<string, unknown>;
  }) => void;
  onPostPublicationHandoff?: (
    data: PostPublicationHandoffStatus & { stream_authority_digest: string },
  ) => void;
  onEpochBlocked?: (data: {
    evaluation_epoch: "national_tcp_policy_v1";
    epoch_state: string;
    epoch_initialized: boolean;
    epoch_reset_receipt_digest: string | null;
    stream_authority_digest: string | null;
  }) => void;
  onConnect?: () => void;
  onDisconnect?: (reason: "transport_error" | "epoch_blocked") => void;
};

const EVOLUTION_EVENTS: readonly EvolutionEventType[] = [
  "history",
  "status",
  "io",
  "clear_io",
  "eval_table",
  "daemon_stats",
  "header",
  "cost",
  "generation_cost_policy",
  "metrics",
  "tool_call",
  "log_event",
  "log_event_dropped",
  "system_event",
  "post_publication_handoff",
];

type JsonObject = Record<string, unknown>;

const STREAM_TYPES = new Set<StreamType>([
  "prompt",
  "claude",
  "thinking",
  "tool",
  "tool_result",
  "error",
  "default",
]);
const isObject = (value: unknown): value is JsonObject => (
  typeof value === "object" && value !== null && !Array.isArray(value)
);
const isNumber = (value: unknown): value is number => (
  typeof value === "number" && Number.isFinite(value)
);
const isInteger = (value: unknown): value is number => (
  isNumber(value) && Number.isSafeInteger(value)
);
const isOptional = (
  value: unknown,
  predicate: (candidate: unknown) => boolean,
): boolean => value === undefined || predicate(value);
const isNullableNumber = (value: unknown): boolean => value === null || isNumber(value);
const isStringArray = (value: unknown): value is string[] => (
  Array.isArray(value) && value.every((item) => typeof item === "string")
);
const isHexDigest = (value: unknown): value is string => (
  typeof value === "string" && /^[0-9a-f]{64}$/.test(value)
);
const isBotRating = (value: unknown): value is BotRating => (
  isObject(value)
  && typeof value.name === "string"
  && isNumber(value.rating)
  && isNumber(value.rd)
  && isNumber(value.sigma)
  && isNumber(value.conservative_rating)
  && typeof value.confidence === "string"
  && typeof value.last_period === "string"
  && isOptional(value.rank, isInteger)
  && [
    value.win_rate,
    value.h2h_avg_wr,
    value.h2h_weighted_wr,
    value.primary_70_hand_match_score,
    value.secondary_net_chips_total,
    value.secondary_net_chips_mean,
  ].every((item) => isOptional(item, isNullableNumber))
  && [
    value.games,
    value.h2h_games,
    value.h2h_opponents,
    value.h2h_opponents_total,
    value.h2h_coverage,
    value.leaderboard_score,
    value.selection_score,
    value.selection_penalty,
    value.strength_sample_count,
  ].every((item) => isOptional(item, isNumber))
  && [
    value.h2h_source,
    value.rank_basis,
    value.strength_confidence,
    value.strength_note,
  ].every((item) => isOptional(item, (candidate) => typeof candidate === "string"))
  && isOptional(value.strength_order_contract, isStringArray)
);
const numericRecord = (value: unknown): value is Record<string, number> => (
  isObject(value) && Object.values(value).every(isNumber)
);
const optionalString = (value: unknown): boolean => (
  value === undefined || typeof value === "string"
);
const isGenerationCostPolicy = (value: unknown): boolean => (
  isObject(value)
  && typeof value.policy_id === "string"
  && ["monitor_only", "operator_hard_limit"].includes(
    String(value.enforcement_mode ?? ""),
  )
  && isNumber(value.warning_usd)
  && isNullableNumber(value.hard_limit_usd)
  && isHexDigest(value.receipt_sha256)
  && isOptional(value.binding_sha256, isHexDigest)
  && isOptional(value.ledger_errors, isStringArray)
  && value.configuration_from_llm_input === false
  && value.same_uid_llm_resistance === false
  && value.candidate_sandbox_mutable === false
  && value.workflow_guarded_paths === true
);
const isEpochBlocked = (value: unknown): boolean => (
  isObject(value)
  && value.evaluation_epoch === "national_tcp_policy_v1"
  && typeof value.epoch_state === "string"
  && typeof value.epoch_initialized === "boolean"
  && (value.epoch_reset_receipt_digest === null || isHexDigest(
    value.epoch_reset_receipt_digest,
  ))
  && (value.stream_authority_digest === null || isHexDigest(
    value.stream_authority_digest,
  ))
);
const isPostPublicationHandoff = (value: unknown): boolean => {
  if (
    !isObject(value)
    || value.schema_version !== 1
    || value.authority !== "post_publication_handoff_journal"
    || !isStringArray(value.issues)
    || !isHexDigest(value.projection_digest)
    || !isHexDigest(value.stream_authority_digest)
  ) return false;
  if (value.status === "none") {
    return value.state === null
      && value.blocked === false
      && [
        value.version,
        value.source_v,
        value.workflow_run_id,
        value.identity_digest,
        value.publication_id,
        value.record_revision,
        value.next_tool,
      ].every((item) => item === null)
      && value.issues.length === 0;
  }
  if (value.status === "blocked") {
    return value.state === "blocked"
      && value.blocked === true
      && value.next_tool === null
      && value.issues.length > 0;
  }
  if (value.status !== "pending" && value.status !== "running") return false;
  return value.state === value.status
    && value.blocked === false
    && isInteger(value.version)
    && value.version >= 143
    && isInteger(value.source_v)
    && value.source_v < value.version
    && typeof value.workflow_run_id === "string"
    && value.workflow_run_id.length > 0
    && isHexDigest(value.identity_digest)
    && isHexDigest(value.publication_id)
    && isInteger(value.record_revision)
    && value.record_revision > 0
    && value.next_tool === "run_archivist"
    && value.issues.length === 0;
};

export function validateEvolutionStreamEvent(eventType: string, value: unknown): boolean {
  if (!isObject(value)) return false;
  switch (eventType as EvolutionEventType) {
    case "history":
      return typeof value.msg === "string" && typeof value.status === "string";
    case "status":
      return typeof value.msg === "string" && typeof value.is_working === "boolean";
    case "io":
      return typeof value.msg === "string"
        && typeof value.stream_type === "string"
        && STREAM_TYPES.has(value.stream_type as StreamType)
        && isNumber(value.ts)
        && optionalString(value.role);
    case "clear_io":
      return isNumber(value.ts);
    case "eval_table":
      return Array.isArray(value.rows) && value.rows.every(isBotRating);
    case "daemon_stats":
      return ["total_matches", "total_periods", "total_games", "n_bots"]
        .every((key) => isInteger(value[key]));
    case "header":
      return typeof value.msg === "string";
    case "cost":
      return typeof value.role === "string"
        && ["cost_usd", "input_tokens", "output_tokens", "gen_total", "grand_total"]
          .every((key) => isNumber(value[key]));
    case "generation_cost_policy":
      return (value.generation_id === null || typeof value.generation_id === "string")
        && isNumber(value.spent_usd)
        && (value.policy === null || isGenerationCostPolicy(value.policy));
    case "metrics":
      return numericRecord(value);
    case "tool_call":
      return typeof value.tool_name === "string"
        && isObject(value.args)
        && isNumber(value.ts)
        && optionalString(value.role);
    case "log_event":
      return ["debug", "info", "warn", "error"].includes(String(value.level ?? ""))
        && typeof value.logger === "string"
        && typeof value.msg === "string"
        && isNumber(value.ts);
    case "log_event_dropped":
      return value.level === "warn"
        && typeof value.logger === "string"
        && typeof value.msg === "string"
        && isInteger(value.dropped_count)
        && value.dropped_count > 0
        && isInteger(value.max_rate)
        && value.max_rate > 0
        && isNumber(value.ts);
    case "system_event":
      return isNumber(value.ts)
        && typeof value.type === "string"
        && ["info", "warn", "error", "success"].includes(
          String(value.severity ?? ""),
        )
        && typeof value.message === "string"
        && (value.data === undefined || isObject(value.data));
    case "post_publication_handoff":
      return isPostPublicationHandoff(value);
    default:
      return false;
  }
}

export function createEvolutionStreamController(
  getHandlers: () => EvolutionHandlers,
  authorityKey: string,
  dependencies: EventSourceControllerDependencies = {},
): EventSourceController {
  return createEventSourceController({
    url: `/api/evolution/stream?authority=${encodeURIComponent(authorityKey)}`,
    events: EVOLUTION_EVENTS,
    pingEvent: "ping",
    epochBlockedEvent: "epoch_blocked",
    validatePing: isObject,
    validateEvent: validateEvolutionStreamEvent,
    validateEpochBlocked: isEpochBlocked,
    onOpen: () => getHandlers().onConnect?.(),
    onEvent: (eventType, value) => {
      const data = value as JsonObject;
      const handlers = getHandlers();
      switch (eventType as EvolutionEventType) {
        case "history":
          handlers.onHistory?.(data.msg as string, data.status as string);
          break;
        case "status":
          handlers.onStatus?.(data.msg as string, data.is_working as boolean);
          break;
        case "io":
          handlers.onIO?.({
            text: data.msg as string,
            streamType: data.stream_type as StreamType,
            ts: data.ts as number,
            role: data.role as string | undefined,
          });
          break;
        case "clear_io":
          handlers.onClearIO?.();
          break;
        case "eval_table":
          handlers.onEvalTable?.(data.rows as BotRating[]);
          break;
        case "daemon_stats":
          handlers.onDaemonStats?.(data as Parameters<NonNullable<EvolutionHandlers["onDaemonStats"]>>[0]);
          break;
        case "header":
          handlers.onHeader?.(data.msg as string);
          break;
        case "cost":
          handlers.onCost?.(data as Parameters<NonNullable<EvolutionHandlers["onCost"]>>[0]);
          break;
        case "generation_cost_policy":
          handlers.onGenerationCostPolicy?.(data as Parameters<NonNullable<EvolutionHandlers["onGenerationCostPolicy"]>>[0]);
          break;
        case "metrics":
          handlers.onMetrics?.(data as Record<string, number>);
          break;
        case "tool_call":
          handlers.onToolCall?.(data as Parameters<NonNullable<EvolutionHandlers["onToolCall"]>>[0]);
          break;
        case "log_event":
          handlers.onLogEvent?.(data as Parameters<NonNullable<EvolutionHandlers["onLogEvent"]>>[0]);
          break;
        case "log_event_dropped":
          handlers.onLogEventDropped?.(data as Parameters<NonNullable<EvolutionHandlers["onLogEventDropped"]>>[0]);
          break;
        case "system_event":
          handlers.onSystemEvent?.(data as Parameters<NonNullable<EvolutionHandlers["onSystemEvent"]>>[0]);
          break;
        case "post_publication_handoff":
          handlers.onPostPublicationHandoff?.(
            data as unknown as Parameters<NonNullable<EvolutionHandlers["onPostPublicationHandoff"]>>[0],
          );
          break;
      }
    },
    onTransportError: () => getHandlers().onDisconnect?.("transport_error"),
    onEpochFence: () => getHandlers().onDisconnect?.("epoch_blocked"),
    onEpochBlocked: (value) => {
      const handlers = getHandlers();
      handlers.onEpochBlocked?.(value as Parameters<NonNullable<EvolutionHandlers["onEpochBlocked"]>>[0]);
    },
  }, dependencies);
}
