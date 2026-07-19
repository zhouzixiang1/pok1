import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { OfficialCertificationJobsProjection } from "../api/types";
import type { ActiveGeneration } from "../api/control";
import {
  isOfficialCertificationStage,
  officialJobsBindingIssues,
} from "../api/officialJobs";

export function useOfficialCertificationJobs(
  enabled: boolean,
  generation: ActiveGeneration | null | undefined,
  pollMs = 3_000,
) {
  // A caller cannot accidentally turn this into an all-stage poll. Durable
  // official jobs are relevant only at the exact certification/publication
  // boundary of the active strict generation.
  const effectiveEnabled = enabled && isOfficialCertificationStage(generation?.stage);
  const [jobsProjection, setJobsProjection] = useState<OfficialCertificationJobsProjection | null>(null);
  const [loading, setLoading] = useState(effectiveEnabled);
  const [error, setError] = useState<string | null>(null);
  const inFlight = useRef<Promise<void> | null>(null);
  const enabledRef = useRef(effectiveEnabled);
  const generationRef = useRef(generation);
  enabledRef.current = effectiveEnabled;
  generationRef.current = generation;
  const identityKey = generation ? [
    generation.next_v,
    generation.source_v ?? "none",
    generation.parent2_v ?? "none",
    generation.stage,
    generation.run_id,
    generation.workflow_run_id ?? "none",
    generation.checkpoint_revision,
  ].join(":") : "none";

  useLayoutEffect(() => {
    // Same-stage revision movement is still a new authority.  Clear before
    // paint; a formerly current request may finish, but it is rechecked against
    // generationRef below before it can become visible.
    setJobsProjection(null);
    setError(null);
    setLoading(effectiveEnabled);
  }, [effectiveEnabled, identityKey]);

  const refresh = useCallback(async () => {
    if (!effectiveEnabled) {
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
          || officialJobsBindingIssues(next, generationRef.current).length > 0
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
  }, [effectiveEnabled]);

  useEffect(() => {
    let stopped = false;
    let timer: number | null = null;
    const tick = async () => {
      await refresh();
      if (!stopped && effectiveEnabled && pollMs > 0) timer = window.setTimeout(tick, pollMs);
    };
    void tick();
    return () => {
      stopped = true;
      if (timer != null) window.clearTimeout(timer);
    };
  }, [effectiveEnabled, identityKey, pollMs, refresh]);

  return { jobsProjection, loading, error, refresh };
}
