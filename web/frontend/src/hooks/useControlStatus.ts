import { useCallback, useEffect, useState } from "react";
import { controlApi, type ControlStatus } from "../api/control";

export function authorityNextVersion(status: ControlStatus | null): number | null {
  if (!status) return null;
  // Only the explicit pre-reset state owns the high-water projection.  During
  // recovery or an unavailable authority read, claiming a target version
  // would turn an error fallback into version authority.
  if (!status.epoch_initialized) {
    return status.epoch_state === "reset_required"
      ? status.version_authority_high_water + 1
      : null;
  }
  return status.active_generation?.next_v ?? status.next_v;
}

export function useControlStatus(pollMs = 5_000) {
  const [status, setStatus] = useState<ControlStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const next = await controlApi.status();
      setStatus(next);
      setError(null);
    } catch (err) {
      // Fail closed: pages must not keep mutation controls, Arena sessions, or
      // epoch-bound evidence enabled from a previously successful poll.
      setStatus(null);
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    if (pollMs <= 0) return;
    const timer = window.setInterval(() => void refresh(), pollMs);
    return () => window.clearInterval(timer);
  }, [pollMs, refresh]);

  return { status, loading, error, refresh };
}
