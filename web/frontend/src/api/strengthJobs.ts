import type { StrengthJobsResponse, DaemonHealthSnapshot } from "./types.js";

type JsonObject = Record<string, unknown>;

const isObject = (value: unknown): value is JsonObject => (
  typeof value === "object" && value !== null && !Array.isArray(value)
);

const isDaemon = (value: unknown): value is DaemonHealthSnapshot => (
  isObject(value)
  && typeof value.alive === "boolean"
  && (value.configured === undefined || typeof value.configured === "boolean")
  && (value.pid === undefined || value.pid === null || (typeof value.pid === "number" && Number.isSafeInteger(value.pid)))
  && (value.heartbeat_status === undefined || typeof value.heartbeat_status === "string")
  && (value.health_error === undefined || value.health_error === null || typeof value.health_error === "string")
);
const hex64 = (value: unknown): value is string => (
  typeof value === "string" && /^[0-9a-f]{64}$/.test(value)
);
const nullableHex64 = (value: unknown): boolean => value === null || hex64(value);
const nullableString = (value: unknown): boolean => value === null || typeof value === "string";
const nullableNumber = (value: unknown): boolean => (
  value === null || (typeof value === "number" && Number.isFinite(value))
);
const stringArray = (value: unknown): value is string[] => (
  Array.isArray(value) && value.every((item) => typeof item === "string" && item.length > 0)
);
const strictBotArray = (value: unknown): value is string[] => stringArray(value) && value.every((item) => {
  const match = item.match(/^national(?:_cloud)?_v([1-9][0-9]*)$/);
  if (!match) return false;
  const version = Number(match[1]);
  return Number.isSafeInteger(version) && version >= 1;
});

function sameStrings(left: readonly string[], right: readonly string[]): boolean {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

function isCapabilities(value: unknown): boolean {
  if (!isObject(value)) return false;
  if (!sameStrings(Object.keys(value).sort(), [
    "durable_job_lifecycle",
    "producer_consumer_dispatch",
    "queued_running_leases",
  ])) return false;
  const durable = value.durable_job_lifecycle;
  const leases = value.queued_running_leases;
  const dispatch = value.producer_consumer_dispatch;
  return typeof durable === "boolean"
    && typeof leases === "boolean"
    && typeof dispatch === "boolean"
    && (!leases || durable === true)
    && (!dispatch || durable === true);
}

function isAuthorityBinding(value: unknown): boolean {
  if (!isObject(value)) return false;
  if (!sameStrings(Object.keys(value).sort(), [
    "active_bots",
    "complete",
    "epoch_reset_receipt_digest",
    "evaluation_epoch",
    "evaluation_identity_digest",
    "evaluation_manifest_digest",
  ])) return false;
  return value.evaluation_epoch === "national_tcp_policy_v1"
    && strictBotArray(value.active_bots)
    && new Set(value.active_bots).size === value.active_bots.length
    && nullableHex64(value.epoch_reset_receipt_digest)
    && nullableHex64(value.evaluation_identity_digest)
    && nullableHex64(value.evaluation_manifest_digest)
    && typeof value.complete === "boolean"
    && (value.complete === false || hex64(value.epoch_reset_receipt_digest));
}

const nonNegativeInteger = (value: unknown): value is number => (
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0
);

function isObserver(value: unknown): boolean {
  if (!isObject(value) || typeof value.complete !== "boolean" || !stringArray(value.issues)) return false;
  const usage = value.usage;
  const limits = value.limits;
  return isObject(usage)
    && isObject(limits)
    && [usage.directory_entries, usage.files_read, usage.total_read_bytes, usage.rows_seen].every(nonNegativeInteger)
    && [limits.directory_entries, limits.files_read, limits.total_read_bytes, limits.rows].every((item) => nonNegativeInteger(item) && item > 0)
    && [limits.cpu_seconds, limits.wall_seconds].every((item) => typeof item === "number" && Number.isFinite(item) && item > 0)
    && (value.complete === true || value.issues.length > 0);
}

type PaginationShape = JsonObject & {
  offset: number;
  limit: number;
  admitted_total: number;
  staged_pending_total: number;
  inadmissible_total: number;
  admitted_has_more: boolean;
  staged_pending_has_more: boolean;
  inadmissible_has_more: boolean;
};

function isPagination(value: unknown): value is PaginationShape {
  if (!isObject(value)) return false;
  const totals = [value.admitted_total, value.staged_pending_total, value.inadmissible_total];
  return nonNegativeInteger(value.offset)
    && nonNegativeInteger(value.limit)
    && value.limit > 0
    && value.limit <= 100
    && totals.every(nonNegativeInteger)
    && [value.admitted_has_more, value.staged_pending_has_more, value.inadmissible_has_more].every((item) => typeof item === "boolean");
}

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
  if (
    !strictBotArray(value.active_bots)
    || new Set(value.active_bots).size !== value.active_bots.length
    || !nullableHex64(value.epoch_reset_receipt_digest)
    || !isCapabilities(value.capabilities)
    || !isAuthorityBinding(value.authority_binding)
  ) {
    throw new Error("strength jobs response authority binding is invalid");
  }
  const authorityBinding = value.authority_binding as JsonObject;
  if (
    !sameStrings(value.active_bots, authorityBinding.active_bots as string[])
    || value.epoch_reset_receipt_digest !== authorityBinding.epoch_reset_receipt_digest
  ) {
    throw new Error("strength jobs response authority binding does not match top-level identity");
  }
  if (value.available === false) {
    if (typeof value.reason !== "string" || value.reason.length === 0) {
      throw new Error("strength jobs unavailable response is missing a reason");
    }
    if (!isObserver(value.observer)) {
      throw new Error("strength jobs response is missing a bounded observer receipt");
    }
    return value as unknown as StrengthJobsResponse;
  }
  if (value.available !== true) {
    throw new Error("strength jobs response available flag is neither true nor false");
  }
  const identity = value.evaluation_identity_digest;
  const pagination = value.pagination;
  if (!hex64(identity)) {
    throw new Error("strength jobs projection evaluation_identity_digest is invalid");
  }
  if (!isObserver(value.observer)) {
    throw new Error("strength jobs response is missing a bounded observer receipt");
  }
  if (
    !Array.isArray(value.active_bots)
    || !Array.isArray(value.admitted_samples)
    || !Array.isArray(value.staged_pending)
    || !Array.isArray(value.inadmissible_diagnostics)
    || !isPagination(pagination)
    || !isObject(value.daemon_stats)
  ) {
    throw new Error("strength jobs projection is structurally incomplete");
  }
  if (
    authorityBinding.complete !== true
    || authorityBinding.evaluation_identity_digest !== identity
    || authorityBinding.evaluation_manifest_digest !== value.evaluation_manifest_digest
  ) {
    throw new Error("strength jobs projection is not bound to its immutable cycle identity");
  }
  if (
    !hex64(value.evaluation_manifest_digest)
    || !nullableHex64(value.epoch_reset_receipt_digest)
    || !stringArray(value.active_bots)
    || new Set(value.active_bots).size !== value.active_bots.length
  ) {
    throw new Error("strength jobs projection identity fields are invalid");
  }
  const active = new Set(value.active_bots);
  if (
    !isPagination(pagination)
    || value.admitted_samples.length > pagination.limit
    || value.staged_pending.length > pagination.limit
    || value.inadmissible_diagnostics.length > pagination.limit
    || pagination.admitted_total < value.admitted_samples.length
    || pagination.staged_pending_total < value.staged_pending.length
    || pagination.inadmissible_total < value.inadmissible_diagnostics.length
  ) {
    throw new Error("strength jobs projection pagination contract is invalid");
  }
  const admittedValid = value.admitted_samples.every((sample) => (
    isObject(sample)
    && typeof sample.id === "string"
    && sample.id.length > 0
    && typeof sample.bot0 === "string"
    && typeof sample.bot1 === "string"
    && sample.bot0 !== sample.bot1
    && active.has(sample.bot0)
    && active.has(sample.bot1)
    && sample.hands_per_strength_sample === 70
    && typeof sample.strength_sample_count === "number"
    && Number.isSafeInteger(sample.strength_sample_count)
    && sample.strength_sample_count > 0
    && hex64(sample.replay_sha256)
    && [sample.bot0_wins, sample.bot1_wins, sample.draws].every((count) => (
      typeof count === "number" && Number.isSafeInteger(count) && count >= 0
    ))
    && Number(sample.bot0_wins) + Number(sample.bot1_wins) + Number(sample.draws)
      === sample.strength_sample_count
    && nullableString(sample.timestamp)
  ));
  const stagedValid = value.staged_pending.every((sample) => (
    isObject(sample)
    && typeof sample.filename === "string"
    && sample.filename.endsWith(".json")
    && !sample.filename.includes("/")
    && !sample.filename.includes("\\")
    && sample.id === sample.filename
    && typeof sample.bot0 === "string"
    && typeof sample.bot1 === "string"
    && sample.bot0 !== sample.bot1
    && active.has(sample.bot0)
    && active.has(sample.bot1)
    && sample.evaluation_identity_digest === identity
    && sample.strength_sample_unit === "70_hand_match"
    && sample.hands_per_strength_sample === 70
    && typeof sample.strength_sample_count === "number"
    && Number.isSafeInteger(sample.strength_sample_count)
    && sample.strength_sample_count > 0
    && sample.strength_admitted === true
    && sample.strength_complete === true
    && sample.strength_compliance_passed === true
    && nullableString(sample.timestamp)
  ));
  const diagnosticsValid = value.inadmissible_diagnostics.every((row) => (
    isObject(row)
    && nullableString(row.id)
    && (row.filename === undefined || nullableString(row.filename))
    && nullableString(row.timestamp)
    && nullableString(row.bot0)
    && nullableString(row.bot1)
    && nullableNumber(row.strength_sample_count)
    && nullableNumber(row.hands_per_strength_sample)
    && stringArray(row.rejection_reasons)
  ));
  if (!admittedValid || !stagedValid || !diagnosticsValid) {
    throw new Error("strength jobs projection nested evidence contract is invalid");
  }
  return value as unknown as StrengthJobsResponse;
}

export interface StrengthControlAuthority {
  active_bots: string[];
  reset_receipt_digest: string | null;
}

/** Cross-bind a jobs observation to the same control-status reset/pool tuple. */
export function strengthJobsBindingIssues(
  value: StrengthJobsResponse | null | undefined,
  control: StrengthControlAuthority | null | undefined,
): string[] {
  if (!value || !control) return ["authority_unavailable"];
  const issues: string[] = [];
  if (value.authority_binding.complete !== true) issues.push("authority_binding_incomplete");
  if (!sameStrings(value.active_bots, control.active_bots)) issues.push("active_bots");
  if (!sameStrings(value.authority_binding.active_bots, control.active_bots)) {
    issues.push("authority_binding_active_bots");
  }
  if (value.epoch_reset_receipt_digest !== control.reset_receipt_digest) {
    issues.push("epoch_reset_receipt_digest");
  }
  if (value.authority_binding.epoch_reset_receipt_digest !== control.reset_receipt_digest) {
    issues.push("authority_binding_epoch_reset_receipt_digest");
  }
  return issues;
}
