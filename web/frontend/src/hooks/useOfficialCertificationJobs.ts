import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import type { OfficialCertificationJobsProjection } from "../api/types";

export function useOfficialCertificationJobs(enabled: boolean, pollMs = 3_000) {
  const [jobsProjection, setJobsProjection] = useState<OfficialCertificationJobsProjection | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!enabled) {
      setJobsProjection(null);
      setError(null);
      setLoading(false);
      return;
    }
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
      setJobsProjection(next);
      setError(null);
    } catch (value) {
      // Never retain a formerly visible job after its workflow identity moves.
      setJobsProjection(null);
      setError(value instanceof Error ? value.message : String(value));
    } finally {
      setLoading(false);
    }
  }, [enabled]);

  useEffect(() => {
    void refresh();
    if (!enabled || pollMs <= 0) return;
    const timer = window.setInterval(() => void refresh(), pollMs);
    return () => window.clearInterval(timer);
  }, [enabled, pollMs, refresh]);

  return { jobsProjection, loading, error, refresh };
}
