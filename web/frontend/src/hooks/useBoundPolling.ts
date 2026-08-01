import { useCallback, useEffect, useRef, useState } from "react";

interface UseBoundPollingOptions {
  /** When this key changes, data is cleared and re-fetched. */
  identityKey?: string;
  /** When false, polling pauses. Default: true. */
  enabled?: boolean;
  /** Poll interval in ms. Default: 5000. */
  pollMs?: number;
}

interface UseBoundPollingResult<T> {
  data: T | null;
  loading: boolean;
  error: Error | null;
  refresh: () => void;
  lastUpdated: number | null;
}

/**
 * Unified polling hook with identity binding, sequence fencing, and
 * fail-closed semantics. Replaces the scattered useEffect+setInterval
 * patterns across Overview/ControlPanel/BackgroundStrength/LlmMetrics.
 *
 * - identityKey change → clear data, reset loading, re-fetch
 * - sequence fence → stale async resolves are discarded
 * - mounted guard → no setState after unmount
 * - fail-closed → on error, data is cleared (never shows stale)
 */
export function useBoundPolling<T>(
  fetcher: () => Promise<T>,
  options: UseBoundPollingOptions = {},
): UseBoundPollingResult<T> {
  const { identityKey, enabled = true, pollMs = 5000 } = options;
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [error, setError] = useState<Error | null>(null);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);

  const seqRef = useRef(0);
  const mountedRef = useRef(true);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const doFetch = useCallback(async () => {
    const seq = ++seqRef.current;
    try {
      const result = await fetcherRef.current();
      if (!mountedRef.current || seq !== seqRef.current) return;
      setData(result);
      setError(null);
      setLastUpdated(Date.now());
    } catch (err) {
      if (!mountedRef.current || seq !== seqRef.current) return;
      setError(err instanceof Error ? err : new Error(String(err)));
      setData(null); // fail-closed
    } finally {
      if (mountedRef.current && seq === seqRef.current) {
        setLoading(false);
      }
    }
  }, []);

  // Identity change → clear + re-fetch
  useEffect(() => {
    if (identityKey === undefined) return;
    setData(null);
    setError(null);
    setLoading(enabled);
    seqRef.current++;
  }, [identityKey, enabled]);

  // Initial fetch + polling
  useEffect(() => {
    mountedRef.current = true;
    if (!enabled) {
      setLoading(false);
      return;
    }
    doFetch();
    const interval = setInterval(doFetch, pollMs);
    return () => {
      mountedRef.current = false;
      clearInterval(interval);
      seqRef.current++;
    };
  }, [doFetch, enabled, pollMs, identityKey]);

  return { data, loading, error, refresh: doFetch, lastUpdated };
}
