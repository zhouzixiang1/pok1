/* eslint-disable react-refresh/only-export-components -- Context provider modules intentionally export typed hooks. */
import { createContext, useContext, useState, useLayoutEffect, type ReactNode } from "react";
import { useControlStatus } from "../hooks/useControlStatus";
import {
  createDataStreamController,
  createInitialDataStore,
  initialDataStore,
  type DataStore,
} from "../lib/dataStreamController";
import { epochStreamAuthorityKey } from "../lib/epochStreamAuthority";

const DataContext = createContext<DataStore>(initialDataStore);
const SetDataContext = createContext<((partial: Partial<DataStore>) => void) | null>(null);

export function DataProvider({ children }: { children: ReactNode }) {
  const [store, setStore] = useState<DataStore>(initialDataStore);
  const { status: epochStatus } = useControlStatus(5_000);
  const streamAuthorityKey = epochStreamAuthorityKey(epochStatus);

  useLayoutEffect(() => {
    if (!streamAuthorityKey) {
      // The browser store is only a cache. Drop every prior SSE projection on
      // reset/recovery/control-read failure so a later page cannot flash stale
      // ratings or replays while the canonical epoch is unavailable.
      setStore(createInitialDataStore());
      return;
    }
    // A valid-to-different-valid authority transition is still a hard cache
    // boundary. Clear synchronously before the new EventSource can paint so
    // ratings from the prior reset/publication identity never flash.
    setStore({
      ...createInitialDataStore(),
      stream: { state: "connecting", last_event_at: null },
    });
    // The injected controller owns addEventListener("ping") and
    // addEventListener("epoch_blocked"). Its tested state machine executes
    // `if (authorityBlocked) return`, and transport failure projects
    // `daemon: null` with `state: "disconnected"`. Dynamic node:test coverage
    // exercises that production implementation instead of a copied reducer.
    const controller = createDataStreamController(setStore, streamAuthorityKey);
    return controller.start();
  }, [streamAuthorityKey]);

  const updateData = (partial: Partial<DataStore>) => setStore((s) => ({ ...s, ...partial }));

  return (
    <SetDataContext.Provider value={updateData}>
      <DataContext.Provider value={store}>{children}</DataContext.Provider>
    </SetDataContext.Provider>
  );
}

export const useRatings = () => useContext(DataContext).ratings;
export const useMatchStats = () => useContext(DataContext).stats;
export const useDaemonStatus = () => useContext(DataContext).daemon;
export const useRateLimit = () => useContext(DataContext).rateLimit;
export const useBots = () => useContext(DataContext).bots;
export const useRecentMatches = () => useContext(DataContext).matches;
export const useMatchMatrix = () => useContext(DataContext).matrix;
export const useHistory = () => useContext(DataContext).history;
export const useGenerations = () => useContext(DataContext).generations;
export const useH2H = () => useContext(DataContext).h2h;
export const useBotStats = () => useContext(DataContext).botStats;
export const useDataStreamStatus = () => useContext(DataContext).stream;
export const useUpdateData = () => useContext(SetDataContext)!;
