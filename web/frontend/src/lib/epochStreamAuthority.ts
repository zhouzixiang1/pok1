const SHA256_PATTERN = /^[0-9a-f]{64}$/;

export interface EpochStreamAuthority {
  evaluation_epoch?: unknown;
  epoch_state?: unknown;
  epoch_initialized?: unknown;
  version_authority_high_water?: unknown;
  active_bots?: unknown;
  reset_receipt_valid?: unknown;
  reset_receipt_digest?: unknown;
  stream_authority_digest?: unknown;
}

/**
 * Return the control-plane identity that is allowed to arm an epoch SSE stream.
 *
 * A boolean `epoch_initialized` flag is not an identity. The backend-issued
 * digest binds the reset receipt, published high-water, and active strict pool,
 * and is also the exact replay-ring identity. Returning null fails closed for
 * partial or unverified control snapshots.
 */
export function epochStreamAuthorityKey(
  status: EpochStreamAuthority | null | undefined,
): string | null {
  if (
    !status
    || status.evaluation_epoch !== "national_tcp_policy_v1"
    || status.epoch_initialized !== true
    || !["fresh_bootstrap_ready", "strict_published"].includes(
      String(status.epoch_state ?? ""),
    )
    || status.reset_receipt_valid !== true
    || typeof status.reset_receipt_digest !== "string"
    || !SHA256_PATTERN.test(status.reset_receipt_digest)
    || typeof status.stream_authority_digest !== "string"
    || !SHA256_PATTERN.test(status.stream_authority_digest)
    || !Number.isSafeInteger(status.version_authority_high_water)
    || Number(status.version_authority_high_water) < 0
    || !Array.isArray(status.active_bots)
    || !status.active_bots.every((name) => (
      typeof name === "string" && /^national_v[1-9][0-9]*$/.test(name)
    ))
    || new Set(status.active_bots).size !== status.active_bots.length
  ) {
    return null;
  }
  return status.stream_authority_digest;
}
