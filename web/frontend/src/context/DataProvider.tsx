import { createContext, useContext, useState, useEffect, useRef, type ReactNode } from "react";
import type {
  BotRating, MatchStats, MatchMatrix, HistoryEntry, GenerationLog,
  MatchSummary, DaemonStatus, BotSummary, H2HEntry, BotStatsEntry,
} from "../api/types";

export type DataStore = {
  ratings: BotRating[];
  stats: MatchStats | null;
  daemon: DaemonStatus | null;
  bots: { active: BotSummary[]; graveyard: BotSummary[] };
  matches: MatchSummary[];
  matrix: MatchMatrix | null;
  history: HistoryEntry[];
  generations: GenerationLog[];
  h2h: Record<string, H2HEntry>;
  botStats: Record<string, BotStatsEntry>;
};

const initial: DataStore = {
  ratings: [],
  stats: null,
  daemon: null,
  bots: { active: [], graveyard: [] },
  matches: [],
  matrix: null,
  history: [],
  generations: [],
  h2h: {},
  botStats: {},
};

const DataContext = createContext<DataStore>(initial);

export function DataProvider({ children }: { children: ReactNode }) {
  const [store, setStore] = useState<DataStore>(initial);
  const reconnectRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    let currentSource: EventSource | null = null;

    const connect = () => {
      currentSource = new EventSource("/api/data/stream");

      const handlers: Record<string, (data: unknown) => void> = {
        ratings: (data) => setStore((s) => ({ ...s, ratings: data as BotRating[] })),
        daemon: (data) => setStore((s) => ({ ...s, daemon: data as DaemonStatus })),
        bots: (data) => setStore((s) => ({ ...s, bots: data as DataStore["bots"] })),
        stats: (data) => setStore((s) => ({ ...s, stats: data as MatchStats })),
        matches: (data) => setStore((s) => ({ ...s, matches: data as MatchSummary[] })),
        generations: (data) => setStore((s) => ({ ...s, generations: data as GenerationLog[] })),
        matrix: (data) => setStore((s) => ({ ...s, matrix: data as MatchMatrix })),
        history: (data) => setStore((s) => ({ ...s, history: data as HistoryEntry[] })),
        h2h: (data) => setStore((s) => ({ ...s, h2h: data as Record<string, H2HEntry> })),
        bot_stats: (data) => setStore((s) => ({ ...s, botStats: data as Record<string, BotStatsEntry> })),
      };

      Object.entries(handlers).forEach(([event, handler]) => {
        currentSource!.addEventListener(event, (e: MessageEvent) => {
          try { handler(JSON.parse(e.data)); } catch { /* ignore */ }
        });
      });

      currentSource.onerror = () => {
        currentSource?.close();
        currentSource = null;
        reconnectRef.current = setTimeout(connect, 5000);
      };
    };

    connect();

    return () => {
      if (reconnectRef.current) clearTimeout(reconnectRef.current);
      currentSource?.close();
    };
  }, []);

  return <DataContext.Provider value={store}>{children}</DataContext.Provider>;
}

export const useRatings = () => useContext(DataContext).ratings;
export const useMatchStats = () => useContext(DataContext).stats;
export const useDaemonStatus = () => useContext(DataContext).daemon;
export const useBots = () => useContext(DataContext).bots;
export const useRecentMatches = () => useContext(DataContext).matches;
export const useMatchMatrix = () => useContext(DataContext).matrix;
export const useHistory = () => useContext(DataContext).history;
export const useGenerations = () => useContext(DataContext).generations;
export const useH2H = () => useContext(DataContext).h2h;
export const useBotStats = () => useContext(DataContext).botStats;
