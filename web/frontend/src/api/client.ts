import type {
  BotRating, MatchStats, MatchMatrix, HistoryEntry, GenerationLog, LogContent,
  MatchSummary, MatchReplayData, DaemonStatus, BotSummary, BotDetail,
  PipelineCheckpoint, WorkerFailure, PromptInfo, OrchestratorSession, OrchestratorLogFile,
  H2HEntry, BotStatsEntry,
} from "./types";

const BASE = "/api";

async function fetchJSON<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function fetchText(url: string): Promise<string> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.text();
}

async function putJSON<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function postJSON<T>(url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function deleteReq<T>(url: string): Promise<T> {
  const res = await fetch(url, { method: "DELETE" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
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

  // Experience pool
  experience: () => fetchText(`${BASE}/experience`),
  updateExperience: (content: string) => putJSON<{ saved: boolean; lines: number; chars: number }>(`${BASE}/experience`, { content }),
  appendExperience: (lesson: string) => postJSON<{ appended: boolean; lesson: string }>(`${BASE}/experience/append`, { lesson }),

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

