import { useCallback, useEffect, useRef } from "react";
import type { BotRating } from "./types";
import type { PostPublicationHandoffStatus } from "./control";
import { createEvolutionStreamController } from "../lib/evolutionStreamController";
import type {
  EvolutionHandlers,
  GenerationCostPolicyState,
} from "../lib/evolutionStreamController";

export type {
  EvolutionEventType,
  GenerationCostPolicyState,
  IOLine,
  StreamType,
} from "../lib/evolutionStreamController";

export interface EvolutionState {
  evaluation_epoch: "national_tcp_policy_v1";
  epoch_state: string;
  epoch_initialized: boolean;
  epoch_reset_receipt_digest: string | null;
  stream_authority_digest: string | null;
  status: string;
  is_working: boolean;
  header: string;
  metrics: Record<string, number>;
  ratings: BotRating[];
  pipeline_stage?: string | null;
  post_publication_handoff: PostPublicationHandoffStatus;
  current_v?: number;
  next_v?: number;
  running?: boolean;
  active_bots: string[];
  grand_cost_total: number;
  gen_cost_total: number;
  generation_cost_identity?: string | null;
  generation_cost_policy?: GenerationCostPolicyState | null;
}

const BASE = "/api";

export function useEvolutionSSE(
  handlers: EvolutionHandlers,
  authorityKey: string | null,
) {
  const handlersRef = useRef(handlers);

  useEffect(() => {
    handlersRef.current = handlers;
  }, [handlers]);

  const connect = useCallback(() => {
    if (!authorityKey) return () => {};
    // The production controller owns `if (authorityBlocked) return`, the
    // stale-source generation fence, and onDisconnect delivery; the hook
    // supplies only live handler lookup.
    const controller = createEvolutionStreamController(
      () => handlersRef.current,
      authorityKey,
    );
    return controller.start();
  }, [authorityKey]);

  return connect;
}

export async function fetchEvolutionState(): Promise<EvolutionState> {
  const res = await fetch(`${BASE}/evolution/state`, { signal: AbortSignal.timeout(30_000) });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}
