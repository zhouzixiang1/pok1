export type StreamType = "prompt" | "claude" | "thinking" | "tool" | "error" | "default";

export type EvolutionEventType =
  | "history"
  | "status"
  | "io"
  | "clear_io"
  | "eval_table"
  | "daemon"
  | "header"
  | "cost"
  | "metrics"
  | "tool_call"
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
    h2h_avg_wr?: number;
  }>;
  active_bots: string[];
  grand_cost_total: number;
  gen_cost_total: number;
}

export interface IOLine {
  text: string;
  streamType: StreamType;
  ts: number;
}

const BASE = "/api";

export function useEvolutionSSE(
  handlers: {
    onHistory?: (msg: string, status: string) => void;
    onStatus?: (msg: string, isWorking: boolean) => void;
    onIO?: (line: IOLine) => void;
    onClearIO?: () => void;
    onEvalTable?: (rows: EvolutionState["ratings"]) => void;
    onDaemon?: (data: { total_matches: number; total_periods: number; total_games: number; n_bots: number }) => void;
    onHeader?: (msg: string) => void;
    onCost?: (data: {
      role: string;
      cost_usd: number;
      input_tokens: number;
      output_tokens: number;
      gen_total: number;
      grand_total: number;
    }) => void;
    onMetrics?: (metrics: Record<string, number>) => void;
    onToolCall?: (data: { tool_name: string; args: Record<string, unknown>; ts: number }) => void;
    onConnect?: () => void;
  },
  enabled = true
) {
  const connect = () => {
    if (!enabled) return () => {};

    let currentSource: EventSource | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

    const doConnect = () => {
      currentSource = new EventSource(`${BASE}/evolution/stream`);
      currentSource.onopen = () => {
        handlers.onConnect?.();
      };

      const eventTypes: EvolutionEventType[] = [
        "history", "status", "io", "clear_io",
        "eval_table", "daemon", "header", "cost", "metrics", "tool_call",
      ];

      eventTypes.forEach((eventType) => {
        currentSource!.addEventListener(eventType, (e: MessageEvent) => {
          try {
            const data = JSON.parse(e.data);
            switch (eventType) {
              case "history":
                handlers.onHistory?.(data.msg, data.status);
                break;
              case "status":
                handlers.onStatus?.(data.msg, data.is_working);
                break;
              case "io":
                handlers.onIO?.({ text: data.msg, streamType: data.stream_type, ts: data.ts });
                break;
              case "clear_io":
                handlers.onClearIO?.();
                break;
              case "eval_table":
                handlers.onEvalTable?.(data.rows);
                break;
              case "daemon":
                handlers.onDaemon?.(data);
                break;
              case "header":
                handlers.onHeader?.(data.msg);
                break;
              case "cost":
                handlers.onCost?.(data);
                break;
              case "metrics":
                handlers.onMetrics?.(data);
                break;
              case "tool_call":
                handlers.onToolCall?.(data);
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
  };

  return connect;
}

export async function fetchEvolutionState(): Promise<EvolutionState> {
  const res = await fetch(`${BASE}/evolution/state`, { signal: AbortSignal.timeout(30_000) });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}
