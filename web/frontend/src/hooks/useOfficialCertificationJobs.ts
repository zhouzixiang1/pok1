import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { OfficialCertificationJobsProjection } from "../api/types";

export function useOfficialCertificationJobs(enabled: boolean, pollMs = 3_000) {
  const [jobsProjection, setJobsProjection] = useState<OfficialCertificationJobsProjection | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [error, setError] = useState<string | null>(null);
  const inFlight = useRef<Promise<void> | null>(null);
  const enabledRef = useRef(enabled);
  enabledRef.current = enabled;

  const refresh = useCallback(async () => {
    if (!enabled) {
      setJobsProjection(null);
      setError(null);
      setLoading(false);
      return;
    }
    if (inFlight.current) return inFlight.current;
    const request = (async () => {
      try {
        const next = await api.certificationJobs();
        if (
          next.evaluation_epoch !== "national_tcp_policy_v1"
          || next.epoch_initialized !== true
          || next.formal_policy_id !== "official-full-v5"
          || next.formal_mode !== "full"
        ) {
          throw new Error("official durable jobs projection is not bound to the initialized strict epoch");
        }
        if (!enabledRef.current) return;
        setJobsProjection(next);
        setError(null);
      } catch (value) {
        if (!enabledRef.current) return;
        // Never retain a formerly visible job after its workflow identity moves.
        setJobsProjection(null);
        setError(value instanceof Error ? value.message : String(value));
      } finally {
        setLoading(false);
      }
    })();
    inFlight.current = request;
    try {
      await request;
    } finally {
      if (inFlight.current === request) inFlight.current = null;
    }
  }, [enabled]);

  useEffect(() => {
    let stopped = false;
    let timer: number | null = null;
    const tick = async () => {
      await refresh();
      if (!stopped && enabled && pollMs > 0) timer = window.setTimeout(tick, pollMs);
    };
    void tick();
    return () => {
      stopped = true;
      if (timer != null) window.clearTimeout(timer);
    };
  }, [enabled, pollMs, refresh]);

  return { jobsProjection, loading, error, refresh };
}
