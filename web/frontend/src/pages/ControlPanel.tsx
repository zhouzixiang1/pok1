import { useEffect, useState, useCallback } from "react";
import { controlApi, type ControlStatus, type Decision, type ToolResult, type AppConfig } from "../api/control";

const MODES = ["orchestrator", "classic", "manual"] as const;

export default function ControlPanel() {
  const [status, setStatus] = useState<ControlStatus | null>(null);
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [tools, setTools] = useState<string[]>([]);
  const [toolResult, setToolResult] = useState<ToolResult | null>(null);
  const [loading, setLoading] = useState<string | null>(null);
  const [toolArgs, setToolArgs] = useState("{}");

  // Local editing state for config
  const [editWorkers, setEditWorkers] = useState(14);
  const [editPairs, setEditPairs] = useState(5);
  const [editDaemon, setEditDaemon] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const [s, d, t, c] = await Promise.all([
        controlApi.status(),
        controlApi.decisions(),
        controlApi.listTools(),
        controlApi.getConfig(),
      ]);
      setStatus(s);
      setDecisions(d);
      setTools(t.tools);
      setConfig(c);
      setEditWorkers(c.daemon_workers);
      setEditPairs(c.daemon_pairs);
      setEditDaemon(c.daemon_enabled);
    } catch (e) {
      console.error("Failed to fetch control status:", e);
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 3000);
    return () => clearInterval(id);
  }, [refresh]);

  const handleSetMode = async (mode: string) => {
    await controlApi.setConfig({ mode });
    await refresh();
  };

  const handleSaveConfig = async () => {
    setLoading("config");
    try {
      await controlApi.setConfig({
        daemon_enabled: editDaemon,
        daemon_workers: editWorkers,
        daemon_pairs: editPairs,
      });
    } finally {
      setLoading(null);
    }
    await refresh();
  };

  const handleStart = async () => {
    setLoading("start");
    try { await controlApi.start(); } finally { setLoading(null); }
    await refresh();
  };

  const handleStop = async () => {
    setLoading("stop");
    try { await controlApi.stop(); } finally { setLoading(null); }
    await refresh();
  };

  const handleToolCall = async (toolName: string) => {
    setLoading(toolName);
    setToolResult(null);
    try {
      let args = {};
      try { args = JSON.parse(toolArgs); } catch { /* use empty args */ }
      const result = await controlApi.callTool(toolName, args);
      setToolResult(result);
    } catch (e: unknown) {
      setToolResult({ tool: toolName, error: String(e) });
    } finally {
      setLoading(null);
      await refresh();
    }
  };

  const formatTime = (ts: number) => {
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString();
  };

  const configDirty =
    config &&
    (editWorkers !== config.daemon_workers ||
      editPairs !== config.daemon_pairs ||
      editDaemon !== config.daemon_enabled);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-gray-800 dark:text-white">
          Control Panel
        </h1>
        <button
          onClick={refresh}
          className="px-3 py-1 text-sm rounded bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600"
        >
          Refresh
        </button>
      </div>

      {/* Status Bar */}
      <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4">
        <div className="flex flex-wrap items-center gap-4">
          <div className="flex items-center gap-2">
            <span className="text-sm text-gray-500">Mode:</span>
            <select
              value={status?.mode || "orchestrator"}
              onChange={(e) => handleSetMode(e.target.value)}
              className="px-2 py-1 rounded border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white text-sm"
            >
              {MODES.map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
          </div>

          <div className="flex items-center gap-2">
            <span className="text-sm text-gray-500">Status:</span>
            <span className={`inline-flex items-center gap-1.5 text-sm font-medium ${
              status?.running ? "text-green-600" : "text-gray-400"
            }`}>
              <span className={`w-2 h-2 rounded-full ${
                status?.running ? "bg-green-500 animate-pulse" : "bg-gray-400"
              }`} />
              {status?.running ? "Running" : "Stopped"}
            </span>
          </div>

          {status && (
            <div className="text-sm text-gray-500">
              Gen {status.generation_count} | v{status.current_v} → v{status.next_v}
            </div>
          )}

          <div className="flex gap-2 ml-auto">
            {!status?.running ? (
              <button
                onClick={handleStart}
                disabled={loading === "start"}
                className="px-4 py-1.5 text-sm rounded bg-green-600 text-white hover:bg-green-700 disabled:opacity-50"
              >
                {loading === "start" ? "Starting..." : "Start"}
              </button>
            ) : (
              <button
                onClick={handleStop}
                disabled={loading === "stop"}
                className="px-4 py-1.5 text-sm rounded bg-red-600 text-white hover:bg-red-700 disabled:opacity-50"
              >
                {loading === "stop" ? "Stopping..." : "Stop"}
              </button>
            )}
          </div>
        </div>
      </div>

      {/* Settings */}
      <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4">
        <h2 className="text-lg font-semibold text-gray-800 dark:text-white mb-3">
          Settings
        </h2>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          {/* Daemon Toggle */}
          <div className="flex items-center gap-3">
            <label className="text-sm text-gray-600 dark:text-gray-300">Daemon</label>
            <button
              type="button"
              role="switch"
              aria-checked={editDaemon}
              onClick={() => setEditDaemon(!editDaemon)}
              className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors ${
                editDaemon ? "bg-blue-600" : "bg-gray-300 dark:bg-gray-600"
              }`}
            >
              <span
                className={`pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition-transform ${
                  editDaemon ? "translate-x-5" : "translate-x-0"
                }`}
              />
            </button>
            <span className="text-xs text-gray-400">
              {editDaemon ? "ON" : "OFF"}
            </span>
          </div>

          {/* Workers */}
          <div className="flex items-center gap-2">
            <label className="text-sm text-gray-600 dark:text-gray-300 whitespace-nowrap">
              Workers
            </label>
            <input
              type="number"
              min={1}
              max={32}
              value={editWorkers}
              onChange={(e) => setEditWorkers(Math.max(1, Math.min(32, Number(e.target.value) || 1)))}
              disabled={!editDaemon}
              className="w-20 px-2 py-1 text-sm rounded border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white disabled:opacity-40"
            />
          </div>

          {/* Pairs */}
          <div className="flex items-center gap-2">
            <label className="text-sm text-gray-600 dark:text-gray-300 whitespace-nowrap">
              Pairs
            </label>
            <input
              type="number"
              min={1}
              max={20}
              value={editPairs}
              onChange={(e) => setEditPairs(Math.max(1, Math.min(20, Number(e.target.value) || 1)))}
              disabled={!editDaemon}
              className="w-20 px-2 py-1 text-sm rounded border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white disabled:opacity-40"
            />
          </div>
        </div>

        <div className="mt-3 flex justify-end">
          <button
            onClick={handleSaveConfig}
            disabled={!configDirty || loading === "config"}
            className="px-4 py-1.5 text-sm rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-40"
          >
            {loading === "config" ? "Saving..." : "Save"}
          </button>
        </div>
      </div>

      {/* Decision Chain */}
      <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4">
        <h2 className="text-lg font-semibold text-gray-800 dark:text-white mb-3">
          Decision Chain
        </h2>
        {decisions.length === 0 ? (
          <p className="text-sm text-gray-400">No decisions yet</p>
        ) : (
          <div className="space-y-1.5 max-h-64 overflow-y-auto">
            {[...decisions].reverse().map((d, i) => (
              <div key={i} className="flex items-start gap-3 text-sm font-mono">
                <span className="text-gray-400 shrink-0">{formatTime(d.ts)}</span>
                <span className="text-blue-600 dark:text-blue-400 shrink-0">
                  {d.tool}()
                </span>
                <span className="text-gray-600 dark:text-gray-300 truncate">
                  {d.summary}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Manual Tools */}
      <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4">
        <h2 className="text-lg font-semibold text-gray-800 dark:text-white mb-3">
          Manual Tools
        </h2>

        <div className="mb-3">
          <label className="text-xs text-gray-500 mb-1 block">Tool Arguments (JSON)</label>
          <input
            type="text"
            value={toolArgs}
            onChange={(e) => setToolArgs(e.target.value)}
            className="w-full px-3 py-1.5 text-sm font-mono rounded border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white"
            placeholder='{"version": 10}'
          />
        </div>

        <div className="flex flex-wrap gap-2">
          {tools.map((t) => (
            <button
              key={t}
              onClick={() => handleToolCall(t)}
              disabled={loading === t}
              className="px-3 py-1.5 text-xs font-mono rounded bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 text-gray-700 dark:text-gray-200 disabled:opacity-50"
            >
              {loading === t ? "..." : t}
            </button>
          ))}
        </div>
      </div>

      {/* Tool Output */}
      {toolResult && (
        <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4">
          <h2 className="text-lg font-semibold text-gray-800 dark:text-white mb-3">
            Tool Output — {toolResult.tool}
          </h2>
          {toolResult.error ? (
            <p className="text-sm text-red-600">{toolResult.error}</p>
          ) : (
            <pre className="text-xs font-mono text-gray-600 dark:text-gray-300 whitespace-pre-wrap max-h-96 overflow-y-auto">
              {(() => {
                try { return JSON.stringify(JSON.parse(toolResult.result || "{}"), null, 2); }
                catch { return toolResult.result || ""; }
              })()}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}
