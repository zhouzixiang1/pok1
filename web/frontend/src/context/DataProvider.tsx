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

// Single shared /health poll.  Previously every evolution page mounted its own
// useControlStatus() instance (~13 independent polls plus this one), so a
// single read-only control surface hammered the backend with 15× /health
// traffic every 5s (3s on ControlPanel) and amplified the observer-cache load
// exactly during the ~76s projection build that the cache exists to absorb.
// The control projection is the same for every page, so one poll is correct;
// pages consume it via useControlStatusValue().
type ControlStatusValue = ReturnType<typeof useControlStatus>;
const ControlStatusContext = createContext<ControlStatusValue>({
  status: null,
  health: null,
  loading: true,
  error: null,
  refresh: () => Promise.resolve(),
});

export function DataProvider({ children }: { children: ReactNode }) {
  const [store, setStore] = useState<DataStore>(initialDataStore);
  // The one and only /health poll.  epochStatus feeds the stream-authority key
  // below; the full value (status/health/loading/error/refresh) is shared with
  // every page through ControlStatusContext.
  const controlStatusValue = useControlStatus(5_000);
  const { status: epochStatus } = controlStatusValue;
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
      <DataContext.Provider value={store}>
        <ControlStatusContext.Provider value={controlStatusValue}>
          {children}
        </ControlStatusContext.Provider>
      </DataContext.Provider>
    </SetDataContext.Provider>
  );
}

// Shared /health control projection.  Prefer this over useControlStatus() from
// a page: a direct hook call would re-instate an independent poll and defeat
// the single-poll convergence.
export const useControlStatusValue = () => useContext(ControlStatusContext);

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
