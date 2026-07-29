import type { BotRating } from "../api/types.js";
import type { PostPublicationHandoffStatus } from "../api/control.js";
import {
  createEventSourceController,
  type EventSourceController,
  type EventSourceControllerDependencies,
} from "./eventSourceController.js";
import { FIRST_STRICT_POLICY_VERSION } from "./canonicalGenerationIdentity.js";

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
  | "task_owner"
  | "task_authority_lost"
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

/**
 * Process-local WebUI status is not checkpoint evidence.  A browser may show
 * it only while this exact identity is still the canonical active generation.
 */
export interface EvolutionStatusIdentity {
  run_id: string;
  workflow_run_id: string;
  checkpoint_revision: number;
  stage: string;
  task_owner_id: string;
  /**
   * Monotonic task-lifecycle fence.  A checkpoint tuple alone is not enough:
   * an owner can enter shutdown without the checkpoint changing.
   */
  task_lifecycle_revision: number;
}

export interface EvolutionStatusEvent extends EvolutionStatusIdentity {
  msg: string;
  is_working: boolean;
  emitted_at: number;
}

/**
 * A process-local status phrase is deliberately short lived.  It is not
 * checkpoint evidence, and retaining it after the backend's replay window
 * would let a stopped Master/Worker phrase masquerade as current work.
 */
export const EVOLUTION_STATUS_MAX_AGE_SECONDS = 30;

/**
 * Return the latest local time at which an accepted transient status may be
 * rendered.  A delayed replay must not gain a fresh full TTL merely because
 * the browser happened to receive it late; a modest future-clock allowance
 * still expires no later than 30 seconds after local acceptance.
 */
export function evolutionStatusExpiryAt(
  status: EvolutionStatusEvent,
  acceptedAt: number,
): number {
  return Math.min(
    acceptedAt + EVOLUTION_STATUS_MAX_AGE_SECONDS,
    status.emitted_at + EVOLUTION_STATUS_MAX_AGE_SECONDS,
  );
}

/**
 * Guard the locally retained status independently of task/checkpoint
 * identity.  This is used by the UI expiry timer, so an otherwise matching
 * task cannot keep an old phrase visible indefinitely after SSE goes quiet.
 */
export function isAcceptedEvolutionStatusFresh(
  status: EvolutionStatusEvent | null | undefined,
  acceptedAt: number | null | undefined,
  observedAt: number = Date.now() / 1000,
): boolean {
  return Boolean(
    status
    && typeof acceptedAt === "number"
    && Number.isFinite(acceptedAt)
    && typeof observedAt === "number"
    && Number.isFinite(observedAt)
    && observedAt < evolutionStatusExpiryAt(status, acceptedAt),
  );
}

/** Explicit backend notice that no task projection can currently be verified. */
export interface TaskAuthorityLostEvent {
  reason: string;
}

export interface ActiveGenerationStatusIdentity {
  run_id: string;
  workflow_run_id: string | null;
  checkpoint_revision: number;
  stage: string;
}

export interface TransientStatusTask {
  present: boolean;
  done: boolean | null;
  shutdown_requested: boolean;
  /** True only while the backend permits process-local status text to render. */
  status_eligible: boolean;
  owner_id: string | null;
  lifecycle_revision: number;
}

/**
 * IO/tool events carry no independent checkpoint identity.  They may render
 * only while the last accepted status still binds the exact generation/task
 * and remains inside the same 30-second replay lifetime.
 */
export function acceptedEvolutionStatusAllowsIO(
  status: EvolutionStatusEvent | null | undefined,
  acceptedAt: number | null | undefined,
  active: ActiveGenerationStatusIdentity | null | undefined,
  task: TransientStatusTask | null | undefined,
  observedAt: number = Date.now() / 1000,
): boolean {
  return isAcceptedEvolutionStatusFresh(status, acceptedAt, observedAt)
    && evolutionStatusMatchesActiveGeneration(status, active, task);
}

/**
 * The browser must treat a malformed or missing task projection as an
 * authority loss, rather than trying to infer whether a prior task is still
 * live.  `lastVerified` is deliberately retained across that loss: an exact
 * same-revision owner can restore authority, while a contradictory owner at
 * that revision remains fail-closed until the backend advances the fence.
 */
export interface TransientStatusTaskAuthorityState {
  current: TransientStatusTask | null;
  lastVerified: TransientStatusTask | null;
  highWaterRevision: number | null;
  conflictRevision: number | null;
  trusted: boolean;
}

export type TransientStatusTaskObservation = {
  state: TransientStatusTaskAuthorityState;
  accepted: boolean;
  reason: "accepted" | "invalid" | "stale" | "conflict";
};

export function createTransientStatusTaskAuthorityState(): TransientStatusTaskAuthorityState {
  return {
    current: null,
    lastVerified: null,
    highWaterRevision: null,
    conflictRevision: null,
    trusted: false,
  };
}

/** Clear render authority without inventing a newer lifecycle revision. */
export function loseTransientStatusTaskAuthority(
  state: TransientStatusTaskAuthorityState,
): TransientStatusTaskAuthorityState {
  return {
    ...state,
    current: null,
    trusted: false,
  };
}

const isTaskOwnerId = (value: unknown): value is string => (
  typeof value === "string" && /^[0-9a-f]{32}$/.test(value)
);

export function evolutionStatusMatchesActiveGeneration(
  status: EvolutionStatusEvent | null | undefined,
  activeGeneration: ActiveGenerationStatusIdentity | null | undefined,
  task: TransientStatusTask | null | undefined,
): boolean {
  return Boolean(
    status
    && activeGeneration
    && task?.present === true
    && task.done === false
    && task.shutdown_requested === false
    && task.status_eligible === true
    && isTaskOwnerId(task.owner_id)
    && status.task_owner_id === task.owner_id
    && status.task_lifecycle_revision === task.lifecycle_revision
    && status.run_id === activeGeneration.run_id
    && status.workflow_run_id === activeGeneration.workflow_run_id
    && status.checkpoint_revision === activeGeneration.checkpoint_revision
    && status.stage === activeGeneration.stage,
  );
}

/** Exact lifecycle identity for ordering HTTP snapshots against SSE ownership. */
export function transientStatusTaskMatches(
  left: TransientStatusTask | null | undefined,
  right: TransientStatusTask | null | undefined,
): boolean {
  return Boolean(
    left
    && right
    && left.present === right.present
    && left.done === right.done
    && left.shutdown_requested === right.shutdown_requested
    && left.status_eligible === right.status_eligible
    && left.owner_id === right.owner_id
    && left.lifecycle_revision === right.lifecycle_revision,
  );
}

/**
 * Compare task ownership projections without trusting arrival order.  Same
 * lifecycle revisions must describe exactly the same projection; a conflict
 * is deliberately not resolved by whichever HTTP/SSE message arrived last.
 */
export function compareTransientStatusTaskProjection(
  candidate: TransientStatusTask,
  previous: TransientStatusTask | null | undefined,
): "newer" | "same" | "older" | "conflict" {
  if (!previous) return "newer";
  if (candidate.lifecycle_revision > previous.lifecycle_revision) return "newer";
  if (candidate.lifecycle_revision < previous.lifecycle_revision) return "older";
  return transientStatusTaskMatches(candidate, previous) ? "same" : "conflict";
}

/** Reject an out-of-order same-generation replay without trusting text order. */
export function shouldAcceptEvolutionStatus(
  candidate: EvolutionStatusEvent,
  activeGeneration: ActiveGenerationStatusIdentity | null | undefined,
  task: TransientStatusTask | null | undefined,
  previous: EvolutionStatusEvent | null | undefined,
): boolean {
  if (!evolutionStatusMatchesActiveGeneration(candidate, activeGeneration, task)) {
    return false;
  }
  if (!previous) return true;
  const sameIdentity = (
    previous.run_id === candidate.run_id
    && previous.workflow_run_id === candidate.workflow_run_id
    && previous.checkpoint_revision === candidate.checkpoint_revision
    && previous.stage === candidate.stage
    && previous.task_owner_id === candidate.task_owner_id
    && previous.task_lifecycle_revision === candidate.task_lifecycle_revision
  );
  return !sameIdentity || candidate.emitted_at > previous.emitted_at;
}

/** A degraded projection must expose both its observation time and reasons. */
export function formatDegradedHealth(
  issues: unknown,
  checkedAt: unknown,
): string {
  const safeIssues = Array.isArray(issues)
    ? [...new Set(issues.filter((issue): issue is string => (
      typeof issue === "string" && issue.length > 0
    )))]
    : [];
  let checkedAtText = "checked_at 不可用";
  if (typeof checkedAt === "number" && Number.isFinite(checkedAt)) {
    const observed = new Date(checkedAt * 1000);
    if (Number.isFinite(observed.getTime())) checkedAtText = observed.toISOString();
  }
  const issueText = safeIssues.length > 0
    ? safeIssues.join("；")
    : "后端未提供问题列表（按异常处理）";
  return `${checkedAtText} · ${issueText}`;
}

export type EvolutionHandlers = {
  onHistory?: (msg: string, status: string) => void;
  onStatus?: (status: EvolutionStatusEvent) => void;
  onTaskOwner?: (task: TransientStatusTask) => void;
  onTaskAuthorityLost?: (event: TaskAuthorityLostEvent) => void;
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
  "task_owner",
  "task_authority_lost",
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
export const isTransientStatusTask = (value: unknown): value is TransientStatusTask => (
  isObject(value)
  && typeof value.present === "boolean"
  && (value.done === null || typeof value.done === "boolean")
  && typeof value.shutdown_requested === "boolean"
  && typeof value.status_eligible === "boolean"
  && (value.owner_id === null || isTaskOwnerId(value.owner_id))
  && isInteger(value.lifecycle_revision)
  && value.lifecycle_revision >= 0
  && (value.present === false || (
    typeof value.done === "boolean" && isTaskOwnerId(value.owner_id)
  ))
  && (value.present === true || value.done === null)
  && (value.status_eligible === false || (
    value.present === true
    && value.done === false
    && value.shutdown_requested === false
    && isTaskOwnerId(value.owner_id)
  ))
);

/**
 * Advance a task-owner projection using its lifecycle fence, not arrival
 * order.  Invalid/missing input revokes render authority but keeps the last
 * verified fence intact so an exact same-revision SSE owner can recover.  A
 * contradictory same-revision owner is deliberately sticky until a newer
 * lifecycle revision arrives.
 */
export function observeTransientStatusTaskProjection(
  previous: TransientStatusTaskAuthorityState,
  candidate: unknown,
): TransientStatusTaskObservation {
  if (!isTransientStatusTask(candidate)) {
    return {
      state: loseTransientStatusTaskAuthority(previous),
      accepted: false,
      reason: "invalid",
    };
  }

  if (
    previous.highWaterRevision !== null
    && candidate.lifecycle_revision < previous.highWaterRevision
  ) {
    return { state: previous, accepted: false, reason: "stale" };
  }
  if (
    previous.highWaterRevision !== null
    && candidate.lifecycle_revision === previous.highWaterRevision
    && previous.conflictRevision === previous.highWaterRevision
  ) {
    return { state: previous, accepted: false, reason: "conflict" };
  }

  const comparisonBase = previous.current ?? previous.lastVerified;
  const order = comparisonBase
    ? compareTransientStatusTaskProjection(candidate, comparisonBase)
    : "newer";
  if (order === "older") {
    return { state: previous, accepted: false, reason: "stale" };
  }
  if (order === "conflict") {
    return {
      state: {
        ...previous,
        current: null,
        highWaterRevision: candidate.lifecycle_revision,
        conflictRevision: candidate.lifecycle_revision,
        trusted: false,
      },
      accepted: false,
      reason: "conflict",
    };
  }

  return {
    state: {
      current: candidate,
      lastVerified: candidate,
      highWaterRevision: candidate.lifecycle_revision,
      conflictRevision: null,
      trusted: true,
    },
    accepted: true,
    reason: "accepted",
  };
}
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
    || !["none", "current_process", "foreign_process"].includes(
      String(value.owner_scope ?? ""),
    )
  ) return false;
  if (value.status === "none") {
    return value.state === null
      && value.blocked === false
      && value.owner_scope === "none"
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
      && value.owner_scope === "none"
      && value.next_tool === null
      && value.issues.length > 0;
  }
  if (value.status !== "pending" && value.status !== "running") return false;
  return value.state === value.status
    && value.blocked === false
    && (
      (value.status === "pending" && value.owner_scope === "none")
      || (
        value.status === "running"
        && ["current_process", "foreign_process"].includes(String(value.owner_scope))
      )
    )
    && isInteger(value.version)
    // Branch-configurable strict floor (cloud: v1; main historically: v143).
    && value.version >= FIRST_STRICT_POLICY_VERSION
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

export const isEvolutionStatusEvent = (value: unknown): value is EvolutionStatusEvent => (
  isObject(value)
  && typeof value.msg === "string"
  && typeof value.is_working === "boolean"
  && typeof value.run_id === "string"
  && value.run_id.length > 0
  && typeof value.workflow_run_id === "string"
  && value.workflow_run_id.length > 0
  && isInteger(value.checkpoint_revision)
  && value.checkpoint_revision > 0
  && typeof value.stage === "string"
  && value.stage.length > 0
  && isTaskOwnerId(value.task_owner_id)
  && isInteger(value.task_lifecycle_revision)
  && value.task_lifecycle_revision >= 0
  && isNumber(value.emitted_at)
  && value.emitted_at >= 0
);

export const isTaskAuthorityLostEvent = (value: unknown): value is TaskAuthorityLostEvent => (
  isObject(value)
  && typeof value.reason === "string"
  && value.reason.trim().length > 0
);

/** Match the server's short transient-status replay window for JSON snapshots. */
export function isFreshEvolutionStatusEvent(
  value: unknown,
  observedAt: number = Date.now() / 1000,
): value is EvolutionStatusEvent {
  return isEvolutionStatusEvent(value)
    && isNumber(observedAt)
    && value.emitted_at <= observedAt + 5
    && observedAt - value.emitted_at < EVOLUTION_STATUS_MAX_AGE_SECONDS;
}

export function validateEvolutionStreamEvent(eventType: string, value: unknown): boolean {
  if (!isObject(value)) return false;
  switch (eventType as EvolutionEventType) {
    case "history":
      return typeof value.msg === "string" && typeof value.status === "string";
    case "status":
      return isEvolutionStatusEvent(value);
    case "task_owner":
      return isTransientStatusTask(value);
    case "task_authority_lost":
      return isTaskAuthorityLostEvent(value);
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
          handlers.onStatus?.(data as unknown as EvolutionStatusEvent);
          break;
        case "task_owner":
          handlers.onTaskOwner?.(data as unknown as TransientStatusTask);
          break;
        case "task_authority_lost":
          handlers.onTaskAuthorityLost?.(data as unknown as TaskAuthorityLostEvent);
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
    onMalformed: (eventType) => {
      // A bad task/status envelope is itself a loss of authority.  Do not
      // retain the prior Master/Worker phrase merely because the malformed
      // event never reached the normal typed dispatch path.
      if (
        eventType === "status"
        || eventType === "task_owner"
        || eventType === "task_authority_lost"
      ) {
        getHandlers().onTaskAuthorityLost?.({
          reason: `malformed_${eventType}`,
        });
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
