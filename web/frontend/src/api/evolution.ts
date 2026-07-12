import { useCallback, useEffect, useRef } from "react";

export type StreamType = "prompt" | "claude" | "thinking" | "tool" | "tool_result" | "error" | "default";

export interface GenerationCostPolicyState {
  policy_id: string;
  enforcement_mode: "monitor_only" | "operator_hard_limit";
  warning_usd: number;
  hard_limit_usd: number | null;
  receipt_sha256: string;
  binding_sha256?: string;
  ledger_errors?: string[];
  configuration_from_llm_input: false;
  same_uid_llm_resistance: false;
  candidate_sandbox_mutable: false;
  workflow_guarded_paths: true;
}

export type EvolutionEventType =
  | "history"
  | "status"
  | "io"
  | "clear_io"
  | "eval_table"
  | "daemon_stats"
  | "header"
  | "cost"
  | "generation_cost_policy"
  | "metrics"
  | "tool_call"
  | "log_event"
  | "system_event"
  | "ping";

export interface EvolutionState {
  status: string;
  is_working: boolean;
  header: string;
  metrics: Record<string, number>;
  ratings: Array<{
    rank: number;
    name: string;
    rating: number;
    rd: number;
    conservative_rating: number;
    sigma?: number;
    confidence?: string;
    h2h_avg_wr?: number;
    h2h_coverage?: number;
    leaderboard_score?: number;
    rank_basis?: string;
    strength_confidence?: string;
  }>;
  pipeline_stage?: string | null;
  current_v?: number;
  next_v?: number;
  running?: boolean;
  active_bots: string[];
  grand_cost_total: number;
  gen_cost_total: number;
  generation_cost_identity?: string | null;
  generation_cost_policy?: GenerationCostPolicyState | null;
}

export interface IOLine {
  text: string;
  streamType: StreamType;
  ts: number;
  role?: string;
}

const BASE = "/api";

type EvolutionHandlers = {
  onHistory?: (msg: string, status: string) => void;
  onStatus?: (msg: string, isWorking: boolean) => void;
  onIO?: (line: IOLine) => void;
  onClearIO?: () => void;
  onEvalTable?: (rows: EvolutionState["ratings"]) => void;
  onDaemonStats?: (data: { total_matches: number; total_periods: number; total_games: number; n_bots: number }) => void;
  onHeader?: (msg: string) => void;
  onCost?: (data: {
    role: string;
    cost_usd: number;
    input_tokens: number;
    output_tokens: number;
    gen_total: number;
    grand_total: number;
  }) => void;
  onGenerationCostPolicy?: (data: {
    generation_id: string | null;
    spent_usd: number;
    policy: GenerationCostPolicyState | null;
  }) => void;
  onMetrics?: (metrics: Record<string, number>) => void;
  onToolCall?: (data: { tool_name: string; args: Record<string, unknown>; ts: number; role?: string }) => void;
  onLogEvent?: (data: { level: string; logger: string; msg: string; ts: number }) => void;
  onSystemEvent?: (data: { ts: number; type: string; severity: string; message: string; data?: Record<string, unknown> }) => void;
  onConnect?: () => void;
};

export function useEvolutionSSE(
  handlers: EvolutionHandlers,
  enabled = true
) {
  const handlersRef = useRef(handlers);

  useEffect(() => {
    handlersRef.current = handlers;
  }, [handlers]);

  const connect = useCallback(() => {
    if (!enabled) return () => {};

    let currentSource: EventSource | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

    const doConnect = () => {
      currentSource = new EventSource(`${BASE}/evolution/stream`);
      currentSource.onopen = () => {
        handlersRef.current.onConnect?.();
      };

      const eventTypes: EvolutionEventType[] = [
        "history", "status", "io", "clear_io",
        "eval_table", "daemon_stats", "header", "cost", "generation_cost_policy", "metrics", "tool_call",
        "log_event", "system_event",
      ];

      eventTypes.forEach((eventType) => {
        currentSource!.addEventListener(eventType, (e: MessageEvent) => {
          try {
            const data = JSON.parse(e.data);
            const activeHandlers = handlersRef.current;
            switch (eventType) {
              case "history":
                activeHandlers.onHistory?.(data.msg, data.status);
                break;
              case "status":
                activeHandlers.onStatus?.(data.msg, data.is_working);
                break;
              case "io":
                activeHandlers.onIO?.({ text: data.msg, streamType: data.stream_type, ts: data.ts, role: data.role });
                break;
              case "clear_io":
                activeHandlers.onClearIO?.();
                break;
              case "eval_table":
                activeHandlers.onEvalTable?.(data.rows);
                break;
              case "daemon_stats":
                activeHandlers.onDaemonStats?.(data);
                break;
              case "header":
                activeHandlers.onHeader?.(data.msg);
                break;
              case "cost":
                activeHandlers.onCost?.(data);
                break;
              case "generation_cost_policy":
                activeHandlers.onGenerationCostPolicy?.(data);
                break;
              case "metrics":
                activeHandlers.onMetrics?.(data);
                break;
              case "tool_call":
                activeHandlers.onToolCall?.(data);
                break;
              case "log_event":
                activeHandlers.onLogEvent?.(data);
                break;
              case "system_event":
                activeHandlers.onSystemEvent?.(data);
                break;
            }
          } catch { /* ignore parse errors */ }
        });
      });

      currentSource.onerror = () => {
        currentSource?.close();
        currentSource = null;
        reconnectTimer = setTimeout(doConnect, 5000);
      };
    };

    doConnect();

    return () => {
      if (reconnectTimer) clearTimeout(reconnectTimer);
      currentSource?.close();
      currentSource = null;
    };
  }, [enabled]);

  return connect;
}

export async function fetchEvolutionState(): Promise<EvolutionState> {
  const res = await fetch(`${BASE}/evolution/state`, { signal: AbortSignal.timeout(30_000) });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}
