import { useEffect, useState, useCallback } from "react";
import { controlApi, type ControlStatus, type Decision, type ToolResult, type AppConfig } from "../api/control";
import { api } from "../api/client";
import type { OrchestratorSession, PipelineCheckpoint } from "../api/types";

const MODES = ["orchestrator", "classic", "manual"] as const;

// ── Tool schema for typed forms ────────────────────────────────────────────────

interface ToolParam {
  name: string;
  type: "int" | "str" | "bool" | "list" | "dict";
  placeholder?: string;
  optional?: boolean;
}

interface ToolDef {
  name: string;
  description: string;
  params: ToolParam[];
}

const TOOL_GROUPS: { label: string; tools: ToolDef[] }[] = [
  {
    label: "Status",
    tools: [
      { name: "get_status", description: "Get current evolution system status, ratings, daemon state.", params: [] },
      { name: "get_bot_info", description: "Detailed info about a bot: rating, parent, files, code size.", params: [{ name: "version", type: "int", placeholder: "22" }] },
      { name: "get_match_history", description: "Recent match results for a specific bot.", params: [{ name: "version", type: "int", placeholder: "22" }, { name: "n", type: "int", placeholder: "5", optional: true }] },
    ],
  },
  {
    label: "Analysis",
    tools: [
      { name: "run_match_analysis", description: "Analyze recent losses for a bot. Returns weaknesses and patterns.", params: [{ name: "source_v", type: "int", placeholder: "22" }] },
      { name: "run_performance_verification", description: "SATLUTION LLM performance analysis: trend, weaknesses, diversity.", params: [{ name: "source_v", type: "int", placeholder: "22" }] },
      { name: "analyze_stagnation", description: "Check if evolution is stagnating or just Glicko noise.", params: [{ name: "source_v", type: "int", placeholder: "22" }, { name: "active_bots", type: "list", placeholder: '["claude_v22","claude_v21"]' }] },
    ],
  },
  {
    label: "Pipeline",
    tools: [
      { name: "run_master", description: "Run Master Architect to plan next generation task assignments.", params: [{ name: "source_v", type: "int" }, { name: "next_v", type: "int" }, { name: "stagnation_info", type: "str", placeholder: "No stagnation", optional: true }, { name: "match_analysis", type: "str", placeholder: "", optional: true }] },
      { name: "execute_workers", description: "Execute worker tasks to modify bot code.", params: [{ name: "tasks", type: "list", placeholder: "[]" }, { name: "next_v", type: "int" }, { name: "source_v", type: "int" }, { name: "reviewer_feedback", type: "str", placeholder: "", optional: true }] },
      { name: "run_quality_gates", description: "Run compile, smoke test, decision tests, file size check.", params: [{ name: "version", type: "int" }] },
      { name: "run_review", description: "Run Lead Code Reviewer. Returns approved/rejected with score.", params: [{ name: "version", type: "int" }, { name: "source_v", type: "int" }, { name: "plan", type: "list", placeholder: "[]" }] },
      { name: "run_critic", description: "Run Poker Strategy Critic. Score 1-10; ≥6 = approved.", params: [{ name: "version", type: "int" }, { name: "source_v", type: "int" }, { name: "plan", type: "list", placeholder: "[]" }, { name: "reviewer_feedback", type: "str", placeholder: "", optional: true }] },
    ],
  },
  {
    label: "Commit",
    tools: [
      { name: "prepare_next_gen", description: "Copy source bot directory to prepare for next generation.", params: [{ name: "source_v", type: "int" }, { name: "next_v", type: "int" }] },
      { name: "run_crossover", description: "Combine two elite bots into a hybrid child bot.", params: [{ name: "parent_a", type: "int" }, { name: "parent_b", type: "int" }, { name: "target_v", type: "int" }] },
      { name: "commit_bot", description: "Git commit and tag the new bot. review_approved must be true.", params: [{ name: "version", type: "int" }, { name: "source_v", type: "int" }, { name: "strategy", type: "str", placeholder: "Improved folding range" }, { name: "review_approved", type: "bool" }] },
    ],
  },
  {
    label: "Daemon",
    tools: [
      { name: "start_daemon", description: "Start the background ELO daemon (mirror battles + ratings).", params: [{ name: "workers", type: "int", placeholder: "14", optional: true }, { name: "pairs", type: "int", placeholder: "5", optional: true }] },
      { name: "stop_daemon", description: "Stop the background ELO daemon.", params: [] },
      { name: "wait_for_eval", description: "Wait for daemon to evaluate a bot (enough matches + low RD).", params: [{ name: "version", type: "int" }, { name: "timeout", type: "int", placeholder: "600", optional: true }, { name: "min_matches", type: "int", placeholder: "20", optional: true }, { name: "max_rd", type: "int", placeholder: "40", optional: true }] },
      { name: "run_inline_eval", description: "Battle bot against all active opponents and update ratings.", params: [{ name: "version", type: "int" }, { name: "n_games", type: "int", placeholder: "5", optional: true }] },
    ],
  },
  {
    label: "Pool",
    tools: [
      { name: "reap_weakest", description: "Cull weakest bot if pool exceeds 30 bots.", params: [] },
      { name: "trim_experience", description: "Trim experience pool to keep only recent entries.", params: [] },
      { name: "consolidate_experience", description: "LLM-based consolidation and deduplication of experience pool.", params: [] },
    ],
  },
];

// ── Tool form component ────────────────────────────────────────────────────────

function ToolForm({
  tool,
  onCall,
  loading,
}: {
  tool: ToolDef;
  onCall: (name: string, args: Record<string, unknown>) => void;
  loading: boolean;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [boolValues, setBoolValues] = useState<Record<string, boolean>>({});

  const buildArgs = () => {
    const args: Record<string, unknown> = {};
    for (const p of tool.params) {
      const raw = values[p.name]?.trim();
      if (!raw && p.optional) continue;
      if (p.type === "int") {
        const n = parseInt(raw || "");
        if (!isNaN(n)) args[p.name] = n;
      } else if (p.type === "bool") {
        args[p.name] = boolValues[p.name] ?? false;
      } else if (p.type === "list" || p.type === "dict") {
        try { args[p.name] = JSON.parse(raw || (p.type === "list" ? "[]" : "{}")); } catch {}
      } else {
        if (raw !== undefined) args[p.name] = raw;
      }
    }
    return args;
  };

  if (tool.params.length === 0) {
    return (
      <button
        onClick={() => onCall(tool.name, {})}
        disabled={loading}
        className="px-3 py-1.5 text-xs rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
      >
        {loading ? "Running..." : `▶ Run ${tool.name}`}
      </button>
    );
  }

  return (
    <div className="space-y-2">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        {tool.params.map((p) => (
          <div key={p.name}>
            <label className="text-xs text-gray-500 block mb-0.5">
              {p.name}{p.optional ? " (opt)" : ""}
            </label>
            {p.type === "bool" ? (
              <label className="flex items-center gap-2 text-xs">
                <input
                  type="checkbox"
                  checked={boolValues[p.name] ?? false}
                  onChange={(e) => setBoolValues((v) => ({ ...v, [p.name]: e.target.checked }))}
                />
                <span className="text-gray-600 dark:text-gray-300">{boolValues[p.name] ? "true" : "false"}</span>
              </label>
            ) : (p.type === "list" || p.type === "dict") ? (
              <textarea
                value={values[p.name] ?? ""}
                onChange={(e) => setValues((v) => ({ ...v, [p.name]: e.target.value }))}
                placeholder={p.placeholder || (p.type === "list" ? "[]" : "{}")}
                rows={2}
                className="w-full px-2 py-1 text-xs font-mono border border-gray-300 dark:border-gray-600 dark:bg-gray-700 rounded resize-none"
              />
            ) : (
              <input
                type={p.type === "int" ? "number" : "text"}
                value={values[p.name] ?? ""}
                onChange={(e) => setValues((v) => ({ ...v, [p.name]: e.target.value }))}
                placeholder={p.placeholder || ""}
                className="w-full px-2 py-1 text-xs border border-gray-300 dark:border-gray-600 dark:bg-gray-700 rounded"
              />
            )}
          </div>
        ))}
      </div>
      <button
        onClick={() => onCall(tool.name, buildArgs())}
        disabled={loading}
        className="px-3 py-1.5 text-xs rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
      >
        {loading ? "Running..." : `▶ Run ${tool.name}`}
      </button>
    </div>
  );
}

// ── Main ───────────────────────────────────────────────────────────────────────

export default function ControlPanel() {
  const [status, setStatus] = useState<ControlStatus | null>(null);
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [toolResult, setToolResult] = useState<ToolResult | null>(null);
  const [loading, setLoading] = useState<string | null>(null);
  const [editWorkers, setEditWorkers] = useState(14);
  const [editPairs, setEditPairs] = useState(5);
  const [editDaemon, setEditDaemon] = useState(true);
  const [openGroup, setOpenGroup] = useState<string | null>("Status");
  const [session, setSession] = useState<OrchestratorSession | null>(null);
  const [checkpoint, setCheckpoint] = useState<PipelineCheckpoint | null>(null);
  const [sessionLoading, setSessionLoading] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [s, d, c] = await Promise.all([
        controlApi.status(),
        controlApi.decisions(),
        controlApi.getConfig(),
      ]);
      setStatus(s);
      setDecisions(d);
      setConfig(c);
      setEditWorkers(c.daemon_workers);
      setEditPairs(c.daemon_pairs);
      setEditDaemon(c.daemon_enabled);
    } catch {}
  }, []);

  const refreshSession = useCallback(async () => {
    try {
      const [sess, ckpt] = await Promise.all([
        api.orchestratorSession(),
        api.pipelineCheckpoint(),
      ]);
      setSession(sess);
      setCheckpoint(ckpt);
    } catch {}
  }, []);

  useEffect(() => {
    refresh();
    refreshSession();
    const id = setInterval(refresh, 3000);
    const sessId = setInterval(refreshSession, 5000);
    return () => { clearInterval(id); clearInterval(sessId); };
  }, [refresh, refreshSession]);

  const handleSetMode = async (mode: string) => {
    await controlApi.setConfig({ mode });
    await refresh();
  };

  const handleSaveConfig = async () => {
    setLoading("config");
    try {
      await controlApi.setConfig({ daemon_enabled: editDaemon, daemon_workers: editWorkers, daemon_pairs: editPairs });
    } finally { setLoading(null); }
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

  const handleToolCall = async (toolName: string, args: Record<string, unknown>) => {
    setLoading(toolName);
    setToolResult(null);
    try {
      const result = await controlApi.callTool(toolName, args);
      setToolResult(result);
    } catch (e: unknown) {
      setToolResult({ tool: toolName, error: String(e) });
    } finally {
      setLoading(null);
      await refresh();
    }
  };

  const handleResetSession = async () => {
    if (!confirm("Reset Orchestrator session? Next restart will begin a fresh LLM conversation.")) return;
    setSessionLoading(true);
    try {
      await api.clearOrchestratorSession();
      await refreshSession();
    } finally {
      setSessionLoading(false);
    }
  };

  const formatTime = (ts: number) => new Date(ts * 1000).toLocaleTimeString();

  const configDirty = config && (
    editWorkers !== config.daemon_workers ||
    editPairs !== config.daemon_pairs ||
    editDaemon !== config.daemon_enabled
  );

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-gray-800 dark:text-white">Control Panel</h1>
        <button onClick={refresh} className="px-3 py-1 text-sm rounded bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600">
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
              {MODES.map((m) => <option key={m} value={m}>{m}</option>)}
            </select>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-sm text-gray-500">Status:</span>
            <span className={`inline-flex items-center gap-1.5 text-sm font-medium ${status?.running ? "text-green-600" : "text-gray-400"}`}>
              <span className={`w-2 h-2 rounded-full ${status?.running ? "bg-green-500 animate-pulse" : "bg-gray-400"}`} />
              {status?.running ? "Running" : "Stopped"}
            </span>
          </div>
          {status && (
            <div className="text-sm text-gray-500">Gen {status.generation_count} | v{status.current_v} → v{status.next_v}</div>
          )}
          <div className="flex gap-2 ml-auto">
            {!status?.running ? (
              <button onClick={handleStart} disabled={loading === "start"} className="px-4 py-1.5 text-sm rounded bg-green-600 text-white hover:bg-green-700 disabled:opacity-50">
                {loading === "start" ? "Starting..." : "Start"}
              </button>
            ) : (
              <button onClick={handleStop} disabled={loading === "stop"} className="px-4 py-1.5 text-sm rounded bg-red-600 text-white hover:bg-red-700 disabled:opacity-50">
                {loading === "stop" ? "Stopping..." : "Stop"}
              </button>
            )}
          </div>
        </div>
      </div>

      {/* LLM Session Control */}
      <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4">
        <h2 className="text-lg font-semibold text-gray-800 dark:text-white mb-3">LLM Session</h2>
        <div className="flex flex-wrap items-center gap-4">
          <div>
            <span className="text-xs text-gray-500 block mb-1">Orchestrator Session ID</span>
            <span className="font-mono text-sm text-gray-800 dark:text-gray-200">
              {session?.session_id ? session.session_id.slice(0, 12) + "..." : <span className="text-gray-400 italic">No active session</span>}
            </span>
          </div>
          {checkpoint && (
            <div>
              <span className="text-xs text-gray-500 block mb-1">Pipeline Stage</span>
              <span className="px-2 py-0.5 rounded bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300 text-xs font-medium">
                v{checkpoint.next_v} ← v{checkpoint.source_v}: {checkpoint.stage}
              </span>
            </div>
          )}
          <div className="ml-auto">
            <button
              onClick={handleResetSession}
              disabled={sessionLoading || !session?.active}
              className="px-4 py-1.5 text-sm rounded bg-orange-600 text-white hover:bg-orange-700 disabled:opacity-40"
            >
              {sessionLoading ? "Resetting..." : "↺ Reset Session"}
            </button>
            <p className="text-xs text-gray-400 mt-1">Forces fresh LLM conversation on next restart</p>
          </div>
        </div>
      </div>

      {/* Settings */}
      <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4">
        <h2 className="text-lg font-semibold text-gray-800 dark:text-white mb-3">Daemon Settings</h2>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <div className="flex items-center gap-3">
            <label className="text-sm text-gray-600 dark:text-gray-300">Daemon</label>
            <button
              role="switch"
              aria-checked={editDaemon}
              onClick={() => setEditDaemon(!editDaemon)}
              className={`relative inline-flex h-6 w-11 rounded-full border-2 border-transparent transition-colors ${editDaemon ? "bg-blue-600" : "bg-gray-300 dark:bg-gray-600"}`}
            >
              <span className={`inline-block h-5 w-5 transform rounded-full bg-white shadow transition-transform ${editDaemon ? "translate-x-5" : "translate-x-0"}`} />
            </button>
          </div>
          <div className="flex items-center gap-2">
            <label className="text-sm text-gray-600 dark:text-gray-300 whitespace-nowrap">Workers</label>
            <input type="number" min={1} max={32} value={editWorkers} onChange={(e) => setEditWorkers(Math.max(1, Math.min(32, Number(e.target.value) || 1)))} disabled={!editDaemon} className="w-20 px-2 py-1 text-sm rounded border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white disabled:opacity-40" />
          </div>
          <div className="flex items-center gap-2">
            <label className="text-sm text-gray-600 dark:text-gray-300 whitespace-nowrap">Pairs</label>
            <input type="number" min={1} max={20} value={editPairs} onChange={(e) => setEditPairs(Math.max(1, Math.min(20, Number(e.target.value) || 1)))} disabled={!editDaemon} className="w-20 px-2 py-1 text-sm rounded border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white disabled:opacity-40" />
          </div>
        </div>
        <div className="mt-3 flex justify-end">
          <button onClick={handleSaveConfig} disabled={!configDirty || loading === "config"} className="px-4 py-1.5 text-sm rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-40">
            {loading === "config" ? "Saving..." : "Save"}
          </button>
        </div>
      </div>

      {/* Decision Chain */}
      <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4">
        <h2 className="text-lg font-semibold text-gray-800 dark:text-white mb-3">Decision Chain</h2>
        {decisions.length === 0 ? (
          <p className="text-sm text-gray-400">No decisions yet</p>
        ) : (
          <div className="space-y-1.5 max-h-40 overflow-y-auto">
            {[...decisions].reverse().map((d, i) => (
              <div key={i} className="flex items-start gap-3 text-sm font-mono">
                <span className="text-gray-400 shrink-0">{formatTime(d.ts)}</span>
                <span className="text-blue-600 dark:text-blue-400 shrink-0">{d.tool}()</span>
                <span className="text-gray-600 dark:text-gray-300 truncate">{d.summary}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Manual Tools — accordion groups */}
      <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4">
        <h2 className="text-lg font-semibold text-gray-800 dark:text-white mb-3">Manual Tools</h2>
        <div className="space-y-2">
          {TOOL_GROUPS.map((group) => (
            <div key={group.label} className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
              <button
                onClick={() => setOpenGroup(openGroup === group.label ? null : group.label)}
                className="w-full flex items-center justify-between px-4 py-2.5 text-sm font-medium bg-gray-50 dark:bg-gray-700/50 hover:bg-gray-100 dark:hover:bg-gray-700"
              >
                <span className="text-gray-700 dark:text-gray-300">{group.label}</span>
                <span className="text-gray-400">{openGroup === group.label ? "▲" : "▼"}</span>
              </button>
              {openGroup === group.label && (
                <div className="divide-y divide-gray-100 dark:divide-gray-700">
                  {group.tools.map((tool) => (
                    <div key={tool.name} className="px-4 py-3">
                      <div className="flex items-start justify-between mb-2">
                        <div>
                          <span className="text-xs font-mono font-semibold text-blue-600 dark:text-blue-400">{tool.name}</span>
                          <p className="text-xs text-gray-500 mt-0.5">{tool.description}</p>
                        </div>
                      </div>
                      <ToolForm tool={tool} onCall={handleToolCall} loading={loading === tool.name} />
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      </div>

      {/* Tool Output */}
      {toolResult && (
        <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4">
          <h2 className="text-lg font-semibold text-gray-800 dark:text-white mb-3">
            Output — {toolResult.tool}
            <button onClick={() => setToolResult(null)} className="ml-3 text-xs text-gray-400 hover:text-gray-600 underline">clear</button>
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
