import { useEffect, useRef, useState, useCallback } from "react";
import { useEvolutionSSE, fetchEvolutionState } from "../api/evolution";
import type { IOLine, EvolutionState } from "../api/evolution";
import PageMeta from "../components/common/PageMeta";

const STREAM_COLORS: Record<string, string> = {
  prompt: "text-gray-400",
  claude: "text-green-400 font-medium",
  thinking: "text-yellow-300 italic",
  tool: "text-cyan-400 text-[11px]",
  error: "text-red-400 font-bold",
  default: "text-gray-200",
};

const STREAM_PREFIX: Record<string, string> = {
  prompt: "│ ",
  claude: "▸ ",
  thinking: "… ",
  tool: "⚙ ",
  error: "✖ ",
  default: "  ",
};

const MAX_LINES = 2000;

export default function EvolutionMonitor() {
  const [ioLines, setIoLines] = useState<IOLine[]>([]);
  const [historyLines, setHistoryLines] = useState<Array<{ msg: string; status: string }>>([]);
  const [status, setStatus] = useState("Connecting...");
  const [isWorking, setIsWorking] = useState(false);
  const [header, setHeader] = useState("Evolution Framework");
  const [cost, setCost] = useState({ grand: 0, gen: 0 });
  const [metrics, setMetrics] = useState<Record<string, number>>({});
  const [leaderboard, setLeaderboard] = useState<EvolutionState["ratings"]>([]);
  const [autoScroll, setAutoScroll] = useState(true);

  const ioRef = useRef<HTMLDivElement>(null);

  const connect = useEvolutionSSE({
    onHistory: (msg, s) => setHistoryLines((prev) => [...prev.slice(-200), { msg, status: s }]),
    onStatus: (msg, w) => { setStatus(msg); setIsWorking(w); },
    onIO: (line) =>
      setIoLines((prev) => {
        const next = [...prev, line];
        return next.length > MAX_LINES ? next.slice(-MAX_LINES) : next;
      }),
    onClearIO: () => setIoLines([]),
    onEvalTable: (rows) => setLeaderboard(rows),
    onHeader: (msg) => setHeader(msg),
    onCost: (data) => setCost({ grand: data.grand_total, gen: data.gen_total }),
    onMetrics: (m) => setMetrics(m),
  });

  useEffect(() => {
    fetchEvolutionState().catch(() => {});
    const disconnect = connect();
    return disconnect;
  }, []);

  useEffect(() => {
    if (autoScroll && ioRef.current) {
      ioRef.current.scrollTop = ioRef.current.scrollHeight;
    }
  }, [ioLines, autoScroll]);

  const handleScroll = useCallback((e: React.UIEvent<HTMLDivElement>) => {
    const el = e.currentTarget;
    setAutoScroll(el.scrollHeight - el.scrollTop - el.clientHeight < 50);
  }, []);

  return (
    <>
      <PageMeta title="Evolution Monitor" description="Real-time evolution view" />

      {/* Header */}
      <div className="mb-4 rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-white/[0.03]">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold text-gray-800 dark:text-white">{header}</h2>
          <div className="flex items-center gap-4 text-sm">
            <span className="flex items-center gap-1.5 text-gray-500">
              <span
                className={`inline-block size-2 rounded-full ${
                  isWorking ? "animate-pulse bg-green-400" : "bg-gray-400"
                }`}
              />
              {status}
            </span>
            <span className="text-gray-400">Cost: ${cost.grand.toFixed(3)}</span>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        {/* IO Stream - 2/3 */}
        <div className="overflow-hidden rounded-2xl border border-gray-800 bg-gray-950 lg:col-span-2">
          <div className="flex items-center justify-between border-b border-gray-800 px-4 py-2">
            <span className="text-xs font-medium text-gray-400">LLM OUTPUT STREAM</span>
            <div className="flex gap-2">
              <button
                onClick={() => setAutoScroll(!autoScroll)}
                className={`rounded px-2 py-1 text-xs ${
                  autoScroll ? "bg-blue-900/30 text-blue-400" : "text-gray-500"
                }`}
              >
                {autoScroll ? "Auto-scroll ON" : "Auto-scroll OFF"}
              </button>
              <button
                onClick={() => setIoLines([])}
                className="text-xs text-gray-500 hover:text-gray-300"
              >
                Clear
              </button>
            </div>
          </div>
          <div
            ref={ioRef}
            onScroll={handleScroll}
            className="h-[500px] overflow-y-auto p-3 font-mono text-xs leading-relaxed"
          >
            {ioLines.length === 0 && (
              <div className="text-gray-600">Waiting for evolution output...</div>
            )}
            {ioLines.map((line, i) =>
              line.text.split("\n").map((textLine, j) => (
                <div
                  key={`${i}-${j}`}
                  className={STREAM_COLORS[line.streamType] || STREAM_COLORS.default}
                >
                  {STREAM_PREFIX[line.streamType]}{textLine}
                </div>
              ))
            )}
          </div>
        </div>

        {/* Right panel - 1/3 */}
        <div className="space-y-4">
          {/* Metrics */}
          {Object.keys(metrics).length > 0 && (
            <div className="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-white/[0.03]">
              <h3 className="mb-2 text-sm font-semibold text-gray-700 dark:text-gray-300">
                Metrics
              </h3>
              <div className="grid grid-cols-2 gap-2 text-xs">
                <div className="text-gray-500">Generation</div>
                <div className="text-gray-800 dark:text-gray-200">
                  v{metrics.current_v} → v{metrics.next_v}
                </div>
                <div className="text-gray-500">Success Rate</div>
                <div className="text-gray-800 dark:text-gray-200">
                  {((metrics.success_rate ?? 0) * 100).toFixed(0)}%
                </div>
                <div className="text-gray-500">Rating Trend</div>
                <div
                  className={
                    metrics.rating_trend > 0
                      ? "text-green-500"
                      : metrics.rating_trend < 0
                        ? "text-red-500"
                        : "text-gray-400"
                  }
                >
                  {metrics.rating_trend > 0 ? "+" : ""}
                  {metrics.rating_trend?.toFixed(0)}
                </div>
                <div className="text-gray-500">Total Time</div>
                <div className="text-gray-800 dark:text-gray-200">
                  {Math.floor((metrics.total_time_s ?? 0) / 60)}m{" "}
                  {Math.floor((metrics.total_time_s ?? 0) % 60)}s
                </div>
                <div className="text-gray-500">Gen Cost</div>
                <div className="text-gray-800 dark:text-gray-200">${cost.gen.toFixed(3)}</div>
              </div>
            </div>
          )}

          {/* Leaderboard */}
          {leaderboard.length > 0 && (
            <div className="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-white/[0.03]">
              <h3 className="mb-2 text-sm font-semibold text-gray-700 dark:text-gray-300">
                Leaderboard
              </h3>
              <div className="space-y-1">
                {leaderboard.slice(0, 10).map((bot) => (
                  <div key={bot.name} className="flex justify-between text-xs">
                    <span className="text-gray-600 dark:text-gray-400">
                      #{bot.rank} {bot.name.replace("claude_", "v")}
                    </span>
                    <span className="font-mono text-gray-800 dark:text-gray-200">
                      {bot.rating}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* History */}
          <div className="max-h-[200px] overflow-y-auto rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-white/[0.03]">
            <h3 className="mb-2 text-sm font-semibold text-gray-700 dark:text-gray-300">
              History
            </h3>
            <div className="space-y-1">
              {historyLines.length === 0 && (
                <div className="text-xs text-gray-500">No events yet</div>
              )}
              {historyLines.slice(-50).reverse().map((line, i) => (
                <div
                  key={i}
                  className={`text-xs ${
                    line.status === "error"
                      ? "text-red-400"
                      : line.status === "warn"
                        ? "text-yellow-400"
                        : line.status === "success"
                          ? "text-green-400"
                          : "text-gray-400"
                  }`}
                >
                  {line.msg}
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
