import type { BotRating, MatchStats, MatchMatrix, HistoryEntry, GenerationLog, LogContent, MatchSummary, MatchReplayData } from "./types";

const BASE = "/api";

async function fetchJSON<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export const api = {
  ratings: () => fetchJSON<BotRating[]>(`${BASE}/ratings`),
  ratingDetail: (bot: string) => fetchJSON<BotRating>(`${BASE}/ratings/${bot}`),
  history: (bots?: string[], resolution = "medium") => {
    const params = new URLSearchParams();
    if (bots?.length) params.set("bots", bots.join(","));
    params.set("resolution", resolution);
    return fetchJSON<HistoryEntry[]>(`${BASE}/history?${params}`);
  },
  matchMatrix: () => fetchJSON<MatchMatrix>(`${BASE}/matches/matrix`),
  matchStats: () => fetchJSON<MatchStats>(`${BASE}/matches/stats`),
  generations: () => fetchJSON<GenerationLog[]>(`${BASE}/logs/generations`),
  logContent: (version: string, filename: string, tail = 0) =>
    fetchJSON<LogContent>(`${BASE}/logs/generations/${version}/${filename}?tail=${tail}`),
  experience: () => fetch(`${BASE}/experience`).then((r) => r.text()),
  daemonStatus: () => fetchJSON<{ status: string; last_update_age_seconds: number }>(`${BASE}/daemon/status`),
  recentMatches: (limit = 100) => fetchJSON<MatchSummary[]>(`${BASE}/matches/recent?limit=${limit}`),
  matchReplay: (id: string) => fetchJSON<MatchReplayData>(`${BASE}/matches/replay/${id}`),
};

export function useRatingsSSE(onData: (ratings: { name: string; rating: number; rd: number }[]) => void) {
  const connect = () => {
    const source = new EventSource(`${BASE}/ratings/stream`);
    source.addEventListener("ratings", (e) => {
      try { onData(JSON.parse(e.data)); } catch {}
    });
    source.onerror = () => {
      source.close();
      setTimeout(connect, 5000);
    };
    return source;
  };
  return connect;
}
