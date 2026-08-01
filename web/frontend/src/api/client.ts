import type {
  BotRating, MatchStats, MatchMatrix, HistoryEntry, GenerationLog, LogContent,
  MatchSummary, MatchReplayData, DaemonStatus, BotSummary, BotDetail,
  WorkerFailure, PromptInfo, OrchestratorSession, OrchestratorLogFile,
  H2HEntry, BotStatsEntry, SystemEventsResponse, WorkerFailuresResponse, OfficialCertification,
  ArenaCreatePayload, ArenaEventHistoryResponse, ArenaSession, ArenaSessionUnavailable,
  ArenaSessionsResponse, ArenaBotsResponse, ArenaWireHistoryResponse,
  LlmCallMetric, LlmMetricsSummary,
} from "./types";
import { expectPipelineCheckpoint } from "./pipeline";
import { expectAgentActivity } from "./agentActivity";
import { expectOfficialCertificationJobs } from "./officialJobs";
import { expectStrengthJobs } from "./strengthJobs";
const BASE = "/api";
const FETCH_TIMEOUT = 30_000;

function abortSignal(): AbortSignal {
  return AbortSignal.timeout(FETCH_TIMEOUT);
}

async function extractError(res: Response): Promise<never> {
  let msg = `HTTP ${res.status}`;
  try {
    const b = await res.json();
    if (b.detail) {
      const detail = typeof b.detail === "string"
        ? b.detail
        : b.detail.message || b.detail.code || JSON.stringify(b.detail);
      msg += `: ${detail}`;
    }
  } catch {
    // Keep the status-only message when the error body is not JSON.
  }
  throw new Error(msg);
}

async function fetchJSON<T>(url: string, signal?: AbortSignal): Promise<T> {
  const combinedSignal = signal
    ? AbortSignal.any([signal, AbortSignal.timeout(FETCH_TIMEOUT)])
    : AbortSignal.timeout(FETCH_TIMEOUT);
  const res = await fetch(url, { signal: combinedSignal });
  if (!res.ok) return extractError(res);
  return res.json();
}

async function fetchText(url: string, signal?: AbortSignal): Promise<string> {
  const combinedSignal = signal
    ? AbortSignal.any([signal, AbortSignal.timeout(FETCH_TIMEOUT)])
    : AbortSignal.timeout(FETCH_TIMEOUT);
  const res = await fetch(url, { signal: combinedSignal });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.text();
}

async function arenaPostJSON<T>(url: string, body: unknown, controlToken = ""): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (controlToken) headers["X-Control-Token"] = controlToken;
  const res = await fetch(url, {
    method: "POST",
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
    signal: abortSignal(),
  });
  if (!res.ok) return extractError(res);
  return res.json();
}

function expectArenaSession(value: ArenaSession | ArenaSessionUnavailable): ArenaSession {
  if (value && typeof value === "object" && "session_id" in value && typeof value.session_id === "string") {
    return value;
  }
  const state = value && typeof value === "object" && "epoch_authority" in value
    ? value.epoch_authority.state
    : "unavailable";
  throw new Error(`Arena session is unavailable for the current strict epoch (${state})`);
}

export const api = {
  // Ratings & history
  ratings: () => fetchJSON<BotRating[]>(`${BASE}/ratings`),
  ratingDetail: (bot: string) => fetchJSON<BotRating>(`${BASE}/ratings/${bot}`),
  history: (bots?: string[], resolution = "medium") => {
    const params = new URLSearchParams();
    if (bots?.length) params.set("bots", bots.join(","));
    params.set("resolution", resolution);
    return fetchJSON<HistoryEntry[]>(`${BASE}/history?${params}`);
  },
  historySummary: () => fetchJSON<Record<string, { peak_rating: number; current_rating: number; trend: number; periods: number; peak_h2h_avg_wr?: number; current_h2h_avg_wr?: number; wr_trend?: number }>>(`${BASE}/history/summary`),

  // Matches
  matchMatrix: () => fetchJSON<MatchMatrix>(`${BASE}/matches/matrix`),
  matchStats: () => fetchJSON<MatchStats>(`${BASE}/matches/stats`),
  recentMatches: (limit = 100) => fetchJSON<MatchSummary[]>(`${BASE}/matches/recent?limit=${limit}`),
  matchReplay: (id: string) => fetchJSON<MatchReplayData>(`${BASE}/matches/replay/${id}`),

  // H2H & Bot Stats
  h2h: (botName?: string) => fetchJSON<Record<string, H2HEntry>>(
    `${BASE}/h2h${botName ? `?bot_name=${encodeURIComponent(botName)}` : ""}`
  ),
  botStats: () => fetchJSON<Record<string, BotStatsEntry>>(`${BASE}/bot-stats`),

  // Logs - generation
  generations: () => fetchJSON<GenerationLog[]>(`${BASE}/logs/generations`),
  logContent: (version: string, filename: string, tail = 0) =>
    fetchJSON<LogContent>(
      `${BASE}/logs/generations/${encodeURIComponent(version)}/${encodeURIComponent(filename)}?tail=${tail}`
    ),

  // Logs - orchestrator
  orchestratorLogs: () => fetchJSON<OrchestratorLogFile[]>(`${BASE}/logs/orchestrator`),
  orchestratorLogContent: (filename: string, tail = 0) =>
    fetchText(`${BASE}/logs/orchestrator/${encodeURIComponent(filename)}${tail ? `?tail=${tail}` : ""}`),

  // Logs - system events
  systemEvents: (params?: {
    type?: string;
    category?: string;
    severity?: string;
    source?: "structured";
    run_id?: string;
    stage?: string;
    since?: number;
    limit?: number;
    offset?: number;
  }, signal?: AbortSignal) => {
    const p = new URLSearchParams();
    p.set("source", params?.source ?? "structured");
    if (params?.type) p.set("type", params.type);
    if (params?.category) p.set("category", params.category);
    if (params?.severity) p.set("severity", params.severity);
    if (params?.run_id) p.set("run_id", params.run_id);
    if (params?.stage) p.set("stage", params.stage);
    if (params?.since !== undefined) p.set("since", String(params.since));
    if (params?.limit !== undefined) p.set("limit", String(params.limit));
    if (params?.offset !== undefined) p.set("offset", String(params.offset));
    return fetchJSON<SystemEventsResponse>(`${BASE}/logs/system-events?${p}`, signal);
  },

  // Logs - worker failures
  workerFailures: (params?: { gen?: number; role?: string; category?: "worker" | "gate"; limit?: number; offset?: number }, signal?: AbortSignal) => {
    const p = new URLSearchParams();
    if (params?.gen !== undefined && params.gen !== null) p.set("gen", String(params.gen));
    if (params?.role) p.set("role", params.role);
    if (params?.category) p.set("category", params.category);
    if (params?.limit !== undefined) p.set("limit", String(params.limit));
    if (params?.offset !== undefined) p.set("offset", String(params.offset));
    return fetchJSON<WorkerFailuresResponse>(`${BASE}/logs/worker-failures?${p}`, signal);
  },

  // Daemon
  daemonStatus: () => fetchJSON<DaemonStatus>(`${BASE}/daemon/status`),

  // Bots
  listBots: () => fetchJSON<{ active: BotSummary[] }>(`${BASE}/bots`),
  botDetail: (version: number) => fetchJSON<BotDetail>(`${BASE}/bots/${version}`),
  botCode: (version: number, filename: string) =>
    fetchText(`${BASE}/bots/${version}/code/${encodeURIComponent(filename)}`),
  downloadBot: async (version: number) => {
    // 120s timeout: a zip can be a few MB and downloads may be slow — the
    // global 30s timeout (FETCH_TIMEOUT) is tuned for JSON endpoints, not blobs.
    const res = await fetch(`${BASE}/bots/${version}/download`, { signal: AbortSignal.timeout(120_000) });
    if (!res.ok) {
      let msg = `HTTP ${res.status}`;
      try {
        const b = await res.json();
        if (b.detail) msg += `: ${b.detail}`;
      } catch {
        // Keep the status-only message when the error body is not JSON.
      }
      throw new Error(msg);
    }
    const blob = await res.blob();
    const disposition = res.headers.get("Content-Disposition") || res.headers.get("content-disposition") || "";
    const filenameMatch = disposition.match(/filename="?([^";]+)"?/i);
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filenameMatch?.[1] || `bot_v${version}.zip`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  },
  certificationStatus: (version: number) => fetchJSON<OfficialCertification>(`${BASE}/certification/${version}`),
  certificationJobs: async () => expectOfficialCertificationJobs(
    await fetchJSON<unknown>(`${BASE}/certification/jobs`),
  ),

  // National Web Arena. These matches are local diagnostics and never certify a bot.
  arenaBots: () => fetchJSON<ArenaBotsResponse>(`${BASE}/national-arena/bots`),
  arenaSessions: () => fetchJSON<ArenaSessionsResponse>(`${BASE}/national-arena/sessions`),
  arenaSession: async (sessionId: string) => expectArenaSession(
    await fetchJSON<ArenaSession | ArenaSessionUnavailable>(`${BASE}/national-arena/sessions/${encodeURIComponent(sessionId)}`),
  ),
  createArenaSession: (payload: ArenaCreatePayload, controlToken = "") =>
    arenaPostJSON<ArenaSession>(`${BASE}/national-arena/sessions`, payload, controlToken),
  startArenaSession: (sessionId: string, controlToken = "") =>
    arenaPostJSON<ArenaSession>(`${BASE}/national-arena/sessions/${encodeURIComponent(sessionId)}/start`, undefined, controlToken),
  stopArenaSession: (sessionId: string, controlToken = "") =>
    arenaPostJSON<ArenaSession>(`${BASE}/national-arena/sessions/${encodeURIComponent(sessionId)}/stop`, undefined, controlToken),
  arenaEventHistory: (sessionId: string, afterEventId = 0, limit = 5000) =>
    fetchJSON<ArenaEventHistoryResponse>(
      `${BASE}/national-arena/sessions/${encodeURIComponent(sessionId)}/events/history?after_event_id=${afterEventId}&limit=${limit}`,
    ),
  arenaWireHistory: (sessionId: string, afterSequence = 0, limit = 1000) =>
    fetchJSON<ArenaWireHistoryResponse>(
      `${BASE}/national-arena/sessions/${encodeURIComponent(sessionId)}/wire/history?after_sequence=${afterSequence}&limit=${limit}`,
    ),

  // Pipeline
  pipelineCheckpoint: async () => expectPipelineCheckpoint(
    await fetchJSON<unknown>(`${BASE}/pipeline/checkpoint`),
  ),
  pipelineFailures: (limit = 10) => fetchJSON<WorkerFailure[]>(`${BASE}/pipeline/failures?limit=${limit}`),
  pipelineAgents: async () => expectAgentActivity(
    await fetchJSON<unknown>(`${BASE}/pipeline/agents`),
  ),
  pipelineStrengthJobs: async (offset = 0, limit = 50) => expectStrengthJobs(
    await fetchJSON<unknown>(`${BASE}/pipeline/strength-jobs?offset=${offset}&limit=${limit}`),
  ),

  // Prompts
  listPrompts: () => fetchJSON<PromptInfo[]>(`${BASE}/prompts`),
  getPrompt: (name: string) => fetchText(`${BASE}/prompts/${name}`),

  // Orchestrator session
  orchestratorSession: () => fetchJSON<OrchestratorSession>(`${BASE}/control/orchestrator/session`),

  // LLM call metrics
  llmMetrics: (limit = 50) => fetchJSON<LlmCallMetric[]>(`${BASE}/llm/metrics?limit=${limit}`),
  llmMetricsSummary: () => fetchJSON<LlmMetricsSummary>(`${BASE}/llm/metrics/summary`),

};
