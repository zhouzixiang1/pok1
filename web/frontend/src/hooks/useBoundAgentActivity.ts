import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import type { AgentActivityResponse } from "../api/types";
import type { ActiveGeneration } from "../api/control";
import { agentActivityBindingIssues, agentWorkflowIdentityKey } from "../api/agentActivity";

/**
 * Poll /agents only as a projection of the paired control active generation.
 * A successor or any checkpoint revision movement invalidates the retained
 * response synchronously; no page can briefly render R data under R+1.
 */
export function useBoundAgentActivity(
  active: ActiveGeneration | null | undefined,
  enabled: boolean,
  pollMs = 5_000,
) {
  const [observed, setObserved] = useState<AgentActivityResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const identityKey = agentWorkflowIdentityKey(active);

  useEffect(() => {
    if (!enabled || !active) {
      setObserved(null);
      setError(null);
      return;
    }
    let cancelled = false;
    const refresh = async () => {
      try {
        const value = await api.pipelineAgents();
        if (cancelled) return;
        if (value.available && agentActivityBindingIssues(value, active).length > 0) {
          throw new Error("agent activity belongs to another checkpoint revision");
        }
        setObserved(value);
        setError(null);
      } catch (value) {
        if (cancelled) return;
        setObserved(null);
        setError(value instanceof Error ? value.message : String(value));
      }
    };
    void refresh();
    const timer = window.setInterval(() => { void refresh(); }, pollMs);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [active, enabled, identityKey, pollMs]);

  const agents = useMemo(() => {
    if (!observed) return null;
    if (!observed.available) return observed;
    return agentActivityBindingIssues(observed, active).length === 0
      ? observed
      : null;
  }, [active, observed]);

  return { agents, error };
}
