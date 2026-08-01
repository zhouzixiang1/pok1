/**
 * Shared runtime type guards used by both stream controllers
 * (dataStreamController.ts / evolutionStreamController.ts).
 *
 * These were previously duplicated verbatim in each controller.  They are
 * fail-closed structural validators: a value that does not structurally match
 * the declared SSE contract is rejected before any UI state is mutated, so a
 * malformed or replayed envelope can never write partial fields into the
 * browser store.
 *
 * The shape of each guard is part of the contract: do not relax a predicate
 * here without verifying both consumers remain fail-closed.
 */

import type { BotRating } from "../api/types.js";

/** A plain JSON object (not null, not an array). */
export type JsonObject = Record<string, unknown>;

export const isObject = (value: unknown): value is JsonObject => (
  typeof value === "object" && value !== null && !Array.isArray(value)
);

export const isNumber = (value: unknown): value is number => (
  typeof value === "number" && Number.isFinite(value)
);

export const isInteger = (value: unknown): value is number => (
  isNumber(value) && Number.isSafeInteger(value)
);

/** Accepts `undefined` (absent) or anything the predicate accepts. */
export const isOptional = (
  value: unknown,
  predicate: (candidate: unknown) => boolean,
): boolean => value === undefined || predicate(value);

/** Accepts `null` or a finite number. */
export const isNullableNumber = (value: unknown): boolean => (
  value === null || isNumber(value)
);

/** Accepts `null` or a string. */
export const isNullableString = (value: unknown): boolean => (
  value === null || typeof value === "string"
);

export const isStringArray = (value: unknown): value is string[] => (
  Array.isArray(value) && value.every((item) => typeof item === "string")
);

/** Matches the SHA-256 hex digest format used throughout the authority layer. */
export const isHexDigest = (value: unknown): value is string => (
  typeof value === "string" && /^[0-9a-f]{64}$/.test(value)
);

/**
 * epoch_blocked SSE envelope.  The two digests are optional and, when present,
 * must be 64-char hex.  This is the stricter unification of the two prior
 * in-module copies; every value either copy accepted is still accepted.
 */
export const isEpochBlocked = (value: unknown): boolean => (
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

/** BotRating contract shared by both the data and evolution streams. */
export const isBotRating = (value: unknown): value is BotRating => (
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
