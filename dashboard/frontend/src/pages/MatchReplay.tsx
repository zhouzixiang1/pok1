import { useEffect, useState, useRef, useCallback } from "react";
import type { MatchSummary, MatchReplayData, DisplayFrame, GameReplay } from "../api/types";
import PokerTable from "../components/PokerTable";
import PageMeta from "../components/common/PageMeta";

function extractFrames(game: GameReplay): DisplayFrame[] {
  const frames: DisplayFrame[] = [];
  for (const entry of game.logs) {
    const output = entry["output"] as Record<string, unknown> | undefined;
    if (output && output["display"]) {
      frames.push(output["display"] as DisplayFrame);
    }
  }
  return frames;
}

function formatTime(ts: string): string {
  if (!ts || ts.length < 14) return ts;
  return `${ts.slice(0, 4)}-${ts.slice(4, 6)}-${ts.slice(6, 8)} ${ts.slice(9, 11)}:${ts.slice(11, 13)}:${ts.slice(13, 15)}`;
}

export default function MatchReplay() {
  const [matches, setMatches] = useState<MatchSummary[]>([]);
  const [selectedMatch, setSelectedMatch] = useState<MatchReplayData | null>(null);
  const [currentHand, setCurrentHand] = useState(0);
  const [currentStep, setCurrentStep] = useState(0);
  const [frames, setFrames] = useState<DisplayFrame[]>([]);
  const [isPlaying, setIsPlaying] = useState(false);
  const [speed, setSpeed] = useState(800);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    fetch("/api/matches/recent?limit=100")
      .then((r) => r.json())
      .then(setMatches)
      .catch(() => {});
  }, []);

  const loadMatch = useCallback(async (id: string) => {
    const res = await fetch(`/api/matches/replay/${id}`);
    const data: MatchReplayData = await res.json();
    setSelectedMatch(data);
    setCurrentHand(0);
    setCurrentStep(0);
    setIsPlaying(false);
    if (data.games && data.games.length > 0) {
      setFrames(extractFrames(data.games[0]));
    } else {
      setFrames([]);
    }
  }, []);

  const changeHand = useCallback((idx: number) => {
    if (!selectedMatch || idx < 0 || idx >= selectedMatch.games.length) return;
    setCurrentHand(idx);
    setCurrentStep(0);
    setIsPlaying(false);
    setFrames(extractFrames(selectedMatch.games[idx]));
  }, [selectedMatch]);

  // Auto-play
  useEffect(() => {
    if (isPlaying) {
      timerRef.current = setInterval(() => {
        setCurrentStep((prev) => {
          if (prev >= frames.length - 1) {
            // Auto advance to next hand
            if (selectedMatch && currentHand < selectedMatch.games.length - 1) {
              const nextH = currentHand + 1;
              setCurrentHand(nextH);
              setFrames(extractFrames(selectedMatch.games[nextH]));
              return 0;
            }
            setIsPlaying(false);
            return prev;
          }
          return prev + 1;
        });
      }, speed);
    }
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [isPlaying, speed, frames.length, selectedMatch, currentHand]);

  const currentFrame = frames[currentStep] || null;

  return (
    <>
      <PageMeta title="Match Replay" description="Poker match replay viewer" />
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-4">
        {/* Match list */}
        <div className="xl:col-span-1">
          <div className="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-white/[0.03]">
            <h2 className="mb-3 text-sm font-semibold text-gray-700 dark:text-gray-300">
              Recent Matches ({matches.length})
            </h2>
            <div className="max-h-[600px] space-y-2 overflow-y-auto">
              {matches.length === 0 && (
                <div className="text-xs text-gray-500">No matches recorded yet. Run the daemon to generate replays.</div>
              )}
              {matches.map((m) => (
                <button
                  key={m.id}
                  onClick={() => loadMatch(m.id)}
                  className={`w-full rounded-lg border p-2 text-left text-xs transition-colors ${
                    selectedMatch?.id === m.id
                      ? "border-brand-500 bg-brand-50 dark:bg-brand-500/10"
                      : "border-gray-200 hover:border-gray-300 dark:border-gray-700"
                  }`}
                >
                  <div className="font-medium text-gray-800 dark:text-gray-200">
                    {m.bot0.replace("claude_", "")} vs {m.bot1.replace("claude_", "")}
                  </div>
                  <div className="mt-1 flex items-center justify-between text-gray-500">
                    <span className={m.bot0_wins > m.bot1_wins ? "text-green-500 font-medium" : ""}>
                      {m.bot0_wins}W
                    </span>
                    <span>{m.draws}D</span>
                    <span className={m.bot1_wins > m.bot0_wins ? "text-green-500 font-medium" : ""}>
                      {m.bot1_wins}W
                    </span>
                  </div>
                  <div className="mt-1 text-[10px] text-gray-400">{formatTime(m.timestamp)}</div>
                </button>
              ))}
            </div>
          </div>
        </div>

        {/* Replay area */}
        <div className="xl:col-span-3">
          {/* Poker table */}
          <div className="mb-4 rounded-2xl border border-gray-200 bg-gray-900 p-4 dark:border-gray-800">
            <PokerTable
              frame={currentFrame}
              bot0Name={selectedMatch?.bot0 || "Bot 0"}
              bot1Name={selectedMatch?.bot1 || "Bot 1"}
            />
          </div>

          {/* Controls */}
          {selectedMatch && (
            <div className="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-white/[0.03]">
              {/* Hand selector */}
              <div className="mb-3 flex items-center gap-2">
                <span className="text-xs text-gray-500">Hand:</span>
                <select
                  value={currentHand}
                  onChange={(e) => changeHand(Number(e.target.value))}
                  className="rounded border border-gray-200 px-2 py-1 text-xs dark:border-gray-700 dark:bg-gray-800"
                >
                  {selectedMatch.games.map((g, i) => (
                    <option key={i} value={i}>
                      Hand {i + 1} — {g.winner === 0 ? selectedMatch.bot0.replace("claude_", "") : g.winner === 1 ? selectedMatch.bot1.replace("claude_", "") : "Draw"} ({g.bot0_chips > 0 ? "+" : ""}{g.bot0_chips})
                    </option>
                  ))}
                </select>
                <span className="text-xs text-gray-400">
                  {currentStep + 1} / {frames.length} steps
                </span>
              </div>

              {/* Step controls */}
              <div className="flex items-center gap-2">
                <button
                  onClick={() => changeHand(Math.max(0, currentHand - 1))}
                  className="rounded bg-gray-100 px-3 py-1.5 text-xs font-medium hover:bg-gray-200 dark:bg-gray-800 dark:hover:bg-gray-700"
                >
                  ◀◀ Prev Hand
                </button>
                <button
                  onClick={() => setCurrentStep(Math.max(0, currentStep - 1))}
                  className="rounded bg-gray-100 px-3 py-1.5 text-xs font-medium hover:bg-gray-200 dark:bg-gray-800 dark:hover:bg-gray-700"
                >
                  ◀ Step
                </button>
                <button
                  onClick={() => setIsPlaying(!isPlaying)}
                  className={`rounded px-4 py-1.5 text-xs font-medium ${
                    isPlaying
                      ? "bg-red-500 text-white hover:bg-red-600"
                      : "bg-brand-500 text-white hover:bg-brand-600"
                  }`}
                >
                  {isPlaying ? "⏸ Pause" : "▶ Play"}
                </button>
                <button
                  onClick={() => setCurrentStep(Math.min(frames.length - 1, currentStep + 1))}
                  className="rounded bg-gray-100 px-3 py-1.5 text-xs font-medium hover:bg-gray-200 dark:bg-gray-800 dark:hover:bg-gray-700"
                >
                  Step ▶
                </button>
                <button
                  onClick={() => changeHand(Math.min(selectedMatch.games.length - 1, currentHand + 1))}
                  className="rounded bg-gray-100 px-3 py-1.5 text-xs font-medium hover:bg-gray-200 dark:bg-gray-800 dark:hover:bg-gray-700"
                >
                  Next Hand ▶▶
                </button>

                <div className="ml-auto flex items-center gap-2">
                  <span className="text-xs text-gray-400">Speed:</span>
                  <select
                    value={speed}
                    onChange={(e) => setSpeed(Number(e.target.value))}
                    className="rounded border border-gray-200 px-2 py-1 text-xs dark:border-gray-700 dark:bg-gray-800"
                  >
                    <option value={1500}>0.5x</option>
                    <option value={800}>1x</option>
                    <option value={400}>2x</option>
                    <option value={200}>4x</option>
                  </select>
                </div>
              </div>

              {/* Progress bar */}
              <div className="mt-3 h-1.5 w-full rounded-full bg-gray-200 dark:bg-gray-700">
                <div
                  className="h-1.5 rounded-full bg-brand-500 transition-all"
                  style={{ width: `${frames.length > 1 ? (currentStep / (frames.length - 1)) * 100 : 0}%` }}
                />
              </div>
            </div>
          )}
        </div>
      </div>
    </>
  );
}
