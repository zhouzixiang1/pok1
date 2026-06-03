import type {
  BotRating, MatchStats, MatchMatrix, HistoryEntry, GenerationLog, LogContent,
  MatchSummary, MatchReplayData, DaemonStatus, BotSummary, BotDetail,
  PipelineCheckpoint, WorkerFailure, PromptInfo, OrchestratorSession, OrchestratorLogFile,
  H2HEntry, BotStatsEntry, SystemEventsResponse, WorkerFailuresResponse,
} from "./types";

const BASE = "/api";
const FETCH_TIMEOUT = 30_000;

function abortSignal(): AbortSignal {
  return AbortSignal.timeout(FETCH_TIMEOUT);
}

async function extractError(res: Response): Promise<never> {
  let msg = `HTTP ${res.status}`;
  try {
    const b = await res.json();
    if (b.detail) msg += `: ${b.detail}`;
  } catch {}
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

async function putJSON<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: abortSignal(),
  });
  if (!res.ok) return extractError(res);
  return res.json();
}

async function postJSON<T>(url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
    signal: abortSignal(),
  });
  if (!res.ok) return extractError(res);
  return res.json();
}

async function deleteReq<T>(url: string): Promise<T> {
  const res = await fetch(url, { method: "DELETE", signal: abortSignal() });
  if (!res.ok) return extractError(res);
  return res.json();
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
  matchCommentary: (id: string) => fetchJSON<Record<string, string>>(`${BASE}/matches/commentary/${id}`),

  // H2H & Bot Stats
  h2h: (botName?: string) => fetchJSON<Record<string, H2HEntry>>(
    `${BASE}/h2h${botName ? `?bot_name=${encodeURIComponent(botName)}` : ""}`
  ),
  botStats: () => fetchJSON<Record<string, BotStatsEntry>>(`${BASE}/bot-stats`),

  // Logs - generation
  generations: () => fetchJSON<GenerationLog[]>(`${BASE}/logs/generations`),
  logContent: (version: string, filename: string, tail = 0) =>
    fetchJSON<LogContent>(`${BASE}/logs/generations/${version}/${filename}?tail=${tail}`),

  // Logs - orchestrator
  orchestratorLogs: () => fetchJSON<OrchestratorLogFile[]>(`${BASE}/logs/orchestrator`),
  orchestratorLogContent: (filename: string, tail = 0) =>
    fetchText(`${BASE}/logs/orchestrator/${encodeURIComponent(filename)}${tail ? `?tail=${tail}` : ""}`),

  // Logs - system events
  systemEvents: (params?: { type?: string; severity?: string; since?: number; limit?: number; offset?: number }, signal?: AbortSignal) => {
    const p = new URLSearchParams();
    if (params?.type) p.set("type", params.type);
    if (params?.severity) p.set("severity", params.severity);
    if (params?.since !== undefined) p.set("since", String(params.since));
    if (params?.limit !== undefined) p.set("limit", String(params.limit));
    if (params?.offset !== undefined) p.set("offset", String(params.offset));
    return fetchJSON<SystemEventsResponse>(`${BASE}/logs/system-events?${p}`, signal);
  },

  // Logs - worker failures
  workerFailures: (params?: { gen?: number; role?: string; limit?: number; offset?: number }, signal?: AbortSignal) => {
    const p = new URLSearchParams();
    if (params?.gen !== undefined && params.gen !== null) p.set("gen", String(params.gen));
    if (params?.role) p.set("role", params.role);
    if (params?.limit !== undefined) p.set("limit", String(params.limit));
    if (params?.offset !== undefined) p.set("offset", String(params.offset));
    return fetchJSON<WorkerFailuresResponse>(`${BASE}/logs/worker-failures?${p}`, signal);
  },

  // Experience pool
  experience: () => fetchText(`${BASE}/experience`),
  updateExperience: (content: string) => putJSON<{ saved: boolean; lines: number; chars: number }>(`${BASE}/experience`, { content }),
  appendExperience: (lesson: string) => postJSON<{ appended: boolean; lesson: string; total_chars: number }>(`${BASE}/experience/append`, { lesson }),

  // Daemon
  daemonStatus: () => fetchJSON<DaemonStatus>(`${BASE}/daemon/status`),

  // Bots
  listBots: (includeGraveyard = false) =>
    fetchJSON<{ active: BotSummary[]; graveyard: BotSummary[] }>(
      `${BASE}/bots${includeGraveyard ? "?include_graveyard=true" : ""}`
    ),
  botDetail: (version: number) => fetchJSON<BotDetail>(`${BASE}/bots/${version}`),
  botCode: (version: number, filename: string) =>
    fetchText(`${BASE}/bots/${version}/code/${encodeURIComponent(filename)}`),

  // Pipeline
  pipelineCheckpoint: () => fetchJSON<PipelineCheckpoint | null>(`${BASE}/pipeline/checkpoint`),
  pipelineFailures: (limit = 10) => fetchJSON<WorkerFailure[]>(`${BASE}/pipeline/failures?limit=${limit}`),

  // Prompts
  listPrompts: () => fetchJSON<PromptInfo[]>(`${BASE}/prompts`),
  getPrompt: (name: string) => fetchText(`${BASE}/prompts/${name}`),
  updatePrompt: (name: string, content: string) =>
    putJSON<{ saved: boolean; name: string; lines: number }>(`${BASE}/prompts/${name}`, { content }),
  resetPrompt: (name: string) =>
    postJSON<{ reset: boolean; name: string }>(`${BASE}/prompts/${name}/reset`),

  // Orchestrator session
  orchestratorSession: () => fetchJSON<OrchestratorSession>(`${BASE}/control/orchestrator/session`),
  clearOrchestratorSession: () => deleteReq<{ cleared: boolean; message: string }>(`${BASE}/control/orchestrator/session`),

  // Evolution reset
  resetEvolution: () => postJSON<{ status: string; details: Record<string, unknown> }>(`${BASE}/control/reset`),
};

