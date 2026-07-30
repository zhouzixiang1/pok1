import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { PipelineCheckpoint } from "../api/types";

/**
 * Independent pipeline checkpoint poll (5s). Fail-closed: a structurally
 * incomplete response clears the retained checkpoint rather than merging.
 */
export function usePipelineCheckpoint(pollMs = 5_000) {
  const [checkpoint, setCheckpoint] = useState<PipelineCheckpoint | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const sequenceRef = useRef(0);

  const refresh = useCallback(async () => {
    const seq = ++sequenceRef.current;
    try {
      const next = await api.pipelineCheckpoint();
      if (seq !== sequenceRef.current) return;
      setCheckpoint(next);
      setError(null);
    } catch (err) {
      if (seq !== sequenceRef.current) return;
      setCheckpoint(null);
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (seq === sequenceRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    const tick = () => {
      if (cancelled) return;
      void refresh().finally(() => {
        if (!cancelled) {
          window.setTimeout(tick, pollMs);
        }
      });
    };
    tick();
    return () => {
      cancelled = true;
      sequenceRef.current += 1;
    };
  }, [pollMs, refresh]);

  return { checkpoint, error, loading, refresh };
}
