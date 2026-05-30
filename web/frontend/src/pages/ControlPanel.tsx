import { useEffect, useState, useCallback } from "react";
import { controlApi, type ControlStatus, type Decision, type ToolResult, type AppConfig } from "../api/control";
import { api } from "../api/client";
import type { OrchestratorSession, PipelineCheckpoint } from "../api/types";


// ── Inline SVG helpers ─────────────────────────────────────────────────────────
const RefreshIcon = ({ className }: { className?: string }) => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className}><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
);
const PlayIcon = ({ className }: { className?: string }) => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" className={className}><polygon points="5 3 19 12 5 21 5 3"/></svg>
);

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
    label: "状态",
    tools: [
      { name: "get_status", description: "获取当前进化系统状态、评分、评分引擎状态。", params: [] },
      { name: "get_bot_info", description: "查看 Bot 的详细信息：评分、父代、文件、代码大小。", params: [{ name: "version", type: "int", placeholder: "22" }] },
      { name: "get_match_history", description: "查看指定 Bot 的近期对局结果。", params: [{ name: "version", type: "int", placeholder: "22" }, { name: "n", type: "int", placeholder: "5", optional: true }] },
      { name: "get_h2h", description: "查看指定 Bot 的 Head-to-Head 胜率数据（按对手）。", params: [{ name: "bot_name", type: "str", placeholder: "claude_v22" }, { name: "opponent", type: "str", placeholder: "claude_v21", optional: true }] },
      { name: "get_bot_stats", description: "查看指定 Bot 的总战绩：胜/负/平/场数/胜率。", params: [{ name: "bot_name", type: "str", placeholder: "claude_v22" }] },
    ],
  },
  {
    label: "分析",
    tools: [
      { name: "run_match_analysis", description: "分析 Bot 的近期败局，返回弱点与模式。", params: [{ name: "source_v", type: "int", placeholder: "22" }] },
      { name: "run_performance_verification", description: "LLM 性能分析：趋势、弱点、多样性。", params: [{ name: "source_v", type: "int", placeholder: "22" }] },
      { name: "analyze_stagnation", description: "检查进化是否停滞或只是 Glicko 噪声。", params: [{ name: "source_v", type: "int", placeholder: "22" }, { name: "active_bots", type: "list", placeholder: '["claude_v22","claude_v21"]' }] },
    ],
  },
  {
    label: "进化流程",
    tools: [
      { name: "run_master", description: "运行主架构师，规划下一代任务分配。", params: [{ name: "source_v", type: "int" }, { name: "next_v", type: "int" }, { name: "stagnation_info", type: "str", placeholder: "无停滞", optional: true }, { name: "match_analysis", type: "str", placeholder: "", optional: true }, { name: "performance_verification", type: "str", placeholder: "", optional: true }] },
      { name: "execute_workers", description: "执行 Worker 任务以修改 Bot 代码。", params: [{ name: "tasks", type: "list", placeholder: "[]" }, { name: "next_v", type: "int" }, { name: "source_v", type: "int" }, { name: "reviewer_feedback", type: "str", placeholder: "", optional: true }] },
      { name: "run_quality_gates", description: "运行编译、冒烟测试、决策测试、文件大小检查。", params: [{ name: "version", type: "int" }] },
      { name: "run_review", description: "运行首席代码审核员。返回通过/拒绝及评分。", params: [{ name: "version", type: "int" }, { name: "source_v", type: "int" }, { name: "plan", type: "list", placeholder: "[]" }] },
      { name: "run_critic", description: "运行扑克策略评论家。评分 1-10；≥6 = 通过。", params: [{ name: "version", type: "int" }, { name: "source_v", type: "int" }, { name: "plan", type: "list", placeholder: "[]" }, { name: "reviewer_feedback", type: "str", placeholder: "", optional: true }, { name: "force_advance", type: "bool", optional: true }] },
      { name: "run_precommit_eval", description: "提交前最小镜像验证：父代、Top 对手、H2H 弱项。", params: [{ name: "version", type: "int" }, { name: "source_v", type: "int" }, { name: "n_games", type: "int", placeholder: "1", optional: true }] },
    ],
  },
  {
    label: "提交",
    tools: [
      { name: "prepare_next_gen", description: "复制源 Bot 目录以准备下一代。", params: [{ name: "source_v", type: "int" }, { name: "next_v", type: "int" }] },
      { name: "run_crossover", description: "将两个精英 Bot 组合为杂交子代。", params: [{ name: "parent_a", type: "int" }, { name: "parent_b", type: "int" }, { name: "target_v", type: "int" }] },
      { name: "commit_bot", description: "Git 提交并标记新 Bot。review_approved 必须为 true。", params: [{ name: "version", type: "int" }, { name: "source_v", type: "int" }, { name: "strategy", type: "str", placeholder: "改进弃牌范围" }, { name: "review_approved", type: "bool" }] },
    ],
  },
  {
    label: "评分引擎",
    tools: [
      { name: "start_daemon", description: "启动后台评分引擎（镜像对战 + 评分）。", params: [{ name: "workers", type: "int", placeholder: "14", optional: true }, { name: "pairs", type: "int", placeholder: "5", optional: true }] },
      { name: "stop_daemon", description: "停止后台评分引擎。", params: [] },
      { name: "wait_for_eval", description: "等待评分引擎评估 Bot（足够对局数）。", params: [{ name: "version", type: "int" }, { name: "timeout", type: "int", placeholder: "600", optional: true }, { name: "min_games", type: "int", placeholder: "100", optional: true }] },
      { name: "run_inline_eval", description: "让 Bot 与所有活跃对手对战并更新评分。", params: [{ name: "version", type: "int" }, { name: "n_games", type: "int", placeholder: "5", optional: true }] },
    ],
  },
  {
    label: "经验管理",
    tools: [
      { name: "reap_weakest", description: "当 Bot 池超过 30 个时，淘汰保守评分最低的。", params: [] },
      { name: "trim_experience", description: "裁剪经验池，仅保留近期条目。", params: [] },
      { name: "consolidate_experience", description: "基于 LLM 的经验池整合与去重。", params: [] },
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
        className="px-3 py-1.5 text-xs rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50 flex items-center gap-1"
      >
        <PlayIcon /> {loading ? "运行中..." : `运行 ${tool.name}`}
      </button>
    );
  }

  return (
    <div className="space-y-2">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        {tool.params.map((p) => (
          <div key={p.name}>
            <label className="text-xs text-gray-500 block mb-0.5">
              {p.name}{p.optional ? " (可选)" : ""}
            </label>
            {p.type === "bool" ? (
              <label className="flex items-center gap-2 text-xs">
                <input
                  type="checkbox"
                  checked={boolValues[p.name] ?? false}
                  onChange={(e) => setBoolValues((v) => ({ ...v, [p.name]: e.target.checked }))}
                />
                <span className="text-gray-600 dark:text-gray-300">{boolValues[p.name] ? "是" : "否"}</span>
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
        className="px-3 py-1.5 text-xs rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50 flex items-center gap-1"
      >
        <PlayIcon /> {loading ? "运行中..." : `运行 ${tool.name}`}
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
  const [openGroup, setOpenGroup] = useState<string | null>("状态");
  const [session, setSession] = useState<OrchestratorSession | null>(null);
  const [checkpoint, setCheckpoint] = useState<PipelineCheckpoint | null>(null);
  const [sessionLoading, setSessionLoading] = useState(false);
  const [resetLoading, setResetLoading] = useState(false);

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
    if (!confirm("重置编排器会话？下次重启将开始全新的 LLM 对话。")) return;
    setSessionLoading(true);
    try {
      await api.clearOrchestratorSession();
      await refreshSession();
    } finally {
      setSessionLoading(false);
    }
  };

  const handleResetEvolution = async () => {
    if (!confirm("⚠️ 重置演化系统？将删除 v7+ 所有演化数据（bot、评分、对局记录），仅保留 v1-v6 基线。系统将自动重启。")) return;
    if (!confirm("此操作不可逆！确认重置？")) return;
    setResetLoading(true);
    try {
      await api.resetEvolution();
      await refresh();
      await refreshSession();
    } catch (e) {
      alert(`重置失败: ${e}`);
    } finally {
      setResetLoading(false);
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
        <h1 className="text-2xl font-bold text-gray-800 dark:text-white">控制面板</h1>
        <button onClick={refresh} className="px-3 py-1 text-sm rounded bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 flex items-center gap-1">
          <RefreshIcon /> 刷新
        </button>
      </div>

      {/* Status Bar */}
      <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4">
        <div className="flex flex-wrap items-center gap-4">
          <div className="flex items-center gap-2">
            <span className="text-sm text-gray-500">模式:</span>
            <span className="text-sm font-medium text-gray-700 dark:text-gray-300">编排器</span>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-sm text-gray-500">状态:</span>
            <span className={`inline-flex items-center gap-1.5 text-sm font-medium ${status?.running ? "text-green-600" : "text-gray-400"}`}>
              <span className={`w-2 h-2 rounded-full ${status?.running ? "bg-green-500 animate-pulse" : "bg-gray-400"}`} />
              {status?.running ? "运行中" : "已停止"}
            </span>
          </div>
          {status && (
            <div className="text-sm text-gray-500">代 {status.generation_count} | v{status.current_v} → v{status.next_v}</div>
          )}
          <div className="flex gap-2 ml-auto">
            {!status?.running ? (
              <button onClick={handleStart} disabled={loading === "start"} className="px-4 py-1.5 text-sm rounded bg-green-600 text-white hover:bg-green-700 disabled:opacity-50">
                {loading === "start" ? "启动中..." : "启动"}
              </button>
            ) : (
              <button onClick={handleStop} disabled={loading === "stop"} className="px-4 py-1.5 text-sm rounded bg-red-600 text-white hover:bg-red-700 disabled:opacity-50">
                {loading === "stop" ? "停止中..." : "停止"}
              </button>
            )}
          </div>
        </div>
      </div>

      {/* LLM Session Control */}
      <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4">
        <h2 className="text-lg font-semibold text-gray-800 dark:text-white mb-3">LLM 会话</h2>
        <div className="flex flex-wrap items-center gap-4">
          <div>
            <span className="text-xs text-gray-500 block mb-1">编排器会话 ID</span>
            <span className="font-mono text-sm text-gray-800 dark:text-gray-200">
              {session?.session_id ? session.session_id.slice(0, 12) + "..." : <span className="text-gray-400 italic">无活跃会话</span>}
            </span>
          </div>
          {checkpoint && (
            <div>
              <span className="text-xs text-gray-500 block mb-1">流程阶段</span>
              <span className="px-2 py-0.5 rounded bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300 text-xs font-medium">
                v{checkpoint.next_v} ← v{checkpoint.source_v}: {checkpoint.stage}
              </span>
            </div>
          )}
          <div className="ml-auto">
            <button
              onClick={handleResetSession}
              disabled={sessionLoading || !session?.active}
              className="px-4 py-1.5 text-sm rounded bg-orange-600 text-white hover:bg-orange-700 disabled:opacity-40 flex items-center gap-1"
            >
              <RefreshIcon /> {sessionLoading ? "重置中..." : "重置会话"}
            </button>
            <p className="text-xs text-gray-400 mt-1">下次重启时强制开启全新 LLM 对话</p>
          </div>
        </div>
      </div>

      {/* Evolution Reset */}
      <div className="rounded-lg border border-red-300 dark:border-red-800 bg-red-50 dark:bg-red-900/20 p-4">
        <h2 className="text-lg font-semibold text-red-700 dark:text-red-400 mb-3">危险操作</h2>
        <div className="flex flex-wrap items-center gap-4">
          <div className="flex-1">
            <p className="text-sm text-red-600 dark:text-red-300">
              重置演化系统将删除 v7+ 所有数据（bot、评分、对局记录、经验池），仅保留 v1-v6 基线。
              系统将自动重启并从 v7 开始新一轮演化。
            </p>
          </div>
          <button
            onClick={handleResetEvolution}
            disabled={resetLoading}
            className="px-4 py-2 text-sm font-medium rounded bg-red-600 text-white hover:bg-red-700 disabled:opacity-50 whitespace-nowrap"
          >
            {resetLoading ? "重置中..." : "重置演化系统"}
          </button>
        </div>
      </div>

      {/* Settings */}
      <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4">
        <h2 className="text-lg font-semibold text-gray-800 dark:text-white mb-3">评分引擎设置</h2>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <div className="flex items-center gap-3">
            <label className="text-sm text-gray-600 dark:text-gray-300">评分引擎</label>
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
            <label className="text-sm text-gray-600 dark:text-gray-300 whitespace-nowrap">Worker</label>
            <input type="number" min={1} max={32} value={editWorkers} onChange={(e) => setEditWorkers(Math.max(1, Math.min(32, Number(e.target.value) || 1)))} disabled={!editDaemon} className="w-20 px-2 py-1 text-sm rounded border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white disabled:opacity-40" />
          </div>
          <div className="flex items-center gap-2">
            <label className="text-sm text-gray-600 dark:text-gray-300 whitespace-nowrap">每次配对数</label>
            <input type="number" min={1} max={20} value={editPairs} onChange={(e) => setEditPairs(Math.max(1, Math.min(20, Number(e.target.value) || 1)))} disabled={!editDaemon} className="w-20 px-2 py-1 text-sm rounded border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white disabled:opacity-40" />
          </div>
        </div>
        <div className="mt-3 flex justify-end">
          <button onClick={handleSaveConfig} disabled={!configDirty || loading === "config"} className="px-4 py-1.5 text-sm rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-40">
            {loading === "config" ? "保存中..." : "保存"}
          </button>
        </div>
      </div>

      {/* Decision Chain */}
      <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4">
        <h2 className="text-lg font-semibold text-gray-800 dark:text-white mb-3">调用记录</h2>
        {decisions.length === 0 ? (
          <p className="text-sm text-gray-400">暂无调用</p>
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
        <h2 className="text-lg font-semibold text-gray-800 dark:text-white mb-3">手动工具</h2>
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
            输出 — {toolResult.tool}
            <button onClick={() => setToolResult(null)} className="ml-3 text-xs text-gray-400 hover:text-gray-600 underline">清空</button>
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
