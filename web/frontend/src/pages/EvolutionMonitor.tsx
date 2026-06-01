import { useEffect, useRef, useState, useCallback, useMemo } from "react";
import { useEvolutionSSE, fetchEvolutionState } from "../api/evolution";
import type { IOLine } from "../api/evolution";
import { api } from "../api/client";
import type { BotRating, PipelineCheckpoint, WorkerFailure } from "../api/types";
import PageMeta from "../components/common/PageMeta";
import { Badge } from "../components/shared/Badge";
import { PipelineStatus } from "../components/evolution/PipelineStatus";
import { CostBreakdown } from "../components/evolution/CostBreakdown";
import type { RoleCost } from "../components/evolution/CostBreakdown";
import { WorkerProgress, parseWorkerStatus } from "../components/evolution/WorkerProgress";
import type { WorkerInfo } from "../components/evolution/WorkerProgress";
import { ToolCard, ThinkingBlock } from "../components/evolution/ToolCard";
import type { ConvMsg } from "../components/evolution/ToolCard";
import { CrossIcon, CopyIcon } from "../components/evolution/icons";
import { cn } from "../lib/utils";

type TabKey = "pipeline" | "metrics" | "history";

const ROLE_COLORS: Record<string, { bg: string; text: string; border: string; dot: string }> = {
  Orchestrator: { bg: "bg-indigo-500/15", text: "text-indigo-400", border: "border-l-indigo-500", dot: "bg-indigo-400" },
  Master:       { bg: "bg-amber-500/15", text: "text-amber-400", border: "border-l-amber-500", dot: "bg-amber-400" },
  Worker:       { bg: "bg-cyan-500/15", text: "text-cyan-400", border: "border-l-cyan-500", dot: "bg-cyan-400" },
  Reviewer:     { bg: "bg-violet-500/15", text: "text-violet-400", border: "border-l-violet-500", dot: "bg-violet-400" },
  Critic:       { bg: "bg-rose-500/15", text: "text-rose-400", border: "border-l-rose-500", dot: "bg-rose-400" },
  Analyst:      { bg: "bg-emerald-500/15", text: "text-emerald-400", border: "border-l-emerald-500", dot: "bg-emerald-400" },
  Consolidator: { bg: "bg-emerald-500/15", text: "text-emerald-400", border: "border-l-emerald-500", dot: "bg-emerald-400" },
  Archivist:    { bg: "bg-emerald-500/15", text: "text-emerald-400", border: "border-l-emerald-500", dot: "bg-emerald-400" },
  Crossover:    { bg: "bg-orange-500/15", text: "text-orange-400", border: "border-l-orange-500", dot: "bg-orange-400" },
  Diagnostician:{ bg: "bg-pink-500/15", text: "text-pink-400", border: "border-l-pink-500", dot: "bg-pink-400" },
};

function getRoleColor(role: string) {
  const key = Object.keys(ROLE_COLORS).find((k) => role.startsWith(k));
  return ROLE_COLORS[key || ""] ?? { bg: "bg-gray-500/15", text: "text-gray-400", border: "border-l-gray-500", dot: "bg-gray-400" };
}

function shortRoleName(role: string): string {
  if (role === "Orchestrator") return "Orchestrator";
  const m = role.match(/^(\w+(?: \w+)?) \(/);
  if (m) return m[1];
  return role.split(" ")[0];
}

let _msgId = 0;
const nextId = () => ++_msgId;

export default function EvolutionMonitor() {
  const [messages, setMessages] = useState<ConvMsg[]>([]);
  const [historyLines, setHistoryLines] = useState<Array<{ msg: string; status: string }>>([]);
  const [status, setStatus] = useState("连接中...");
  const [isWorking, setIsWorking] = useState(false);
  const [grand, setGrand] = useState(0);
  const [gen, setGen] = useState(0);
  const [leaderboard, setLeaderboard] = useState<BotRating[]>([]);
  const [autoScroll, setAutoScroll] = useState(true);
  const [checkpoint, setCheckpoint] = useState<PipelineCheckpoint | null>(null);
  const [failures, setFailures] = useState<WorkerFailure[]>([]);
  const [workers, setWorkers] = useState<WorkerInfo[]>([]);
  const [roleCosts, setRoleCosts] = useState<RoleCost[]>([]);
  const [metrics, setMetrics] = useState<Record<string, number>>({});
  const [activeTab, setActiveTab] = useState<TabKey>("pipeline");
  const [filterRole, setFilterRole] = useState<string>("");
  const [activeRole, setActiveRole] = useState<string>("");
  const [knownRoles, setKnownRoles] = useState<string[]>([]);

  const ioRef = useRef<HTMLDivElement>(null);
  const openToolId = useRef<number | null>(null);
  const thinkingId = useRef<number | null>(null);
  const activeRoleRef = useRef<string>(activeRole);
  activeRoleRef.current = activeRole;

  // ── Message management (unchanged from original) ──

  const addMsg = useCallback((msg: ConvMsg) => {
    setMessages((prev) => {
      const next = [...prev, msg];
      return next.length > 500 ? next.slice(-500) : next;
    });
  }, []);

  const updateLastTool = useCallback((text: string) => {
    if (openToolId.current == null) return;
    const id = openToolId.current;
    setMessages((prev) =>
      prev.map((m) =>
        m.id === id ? { ...m, toolOutput: [...m.toolOutput, text] } : m
      )
    );
  }, []);

  const closeTool = useCallback(() => {
    if (openToolId.current == null) return;
    const id = openToolId.current;
    setMessages((prev) =>
      prev.map((m) => (m.id === id ? { ...m, toolDone: true } : m))
    );
    openToolId.current = null;
  }, []);

  const closeThinking = useCallback(() => {
    if (thinkingId.current == null) return;
    const id = thinkingId.current;
    setMessages((prev) =>
      prev.map((m) => (m.id === id ? { ...m, toolDone: true } : m))
    );
    thinkingId.current = null;
  }, []);

  // ── SSE connection (unchanged from original) ──

  const connect = useEvolutionSSE({
    onHistory: (msg, s) => {
      setHistoryLines((prev) => [...prev.slice(-200), { msg, status: s }]);
      const w = parseWorkerStatus(msg);
      if (w) {
        setWorkers((prev) => {
          const idx = prev.findIndex((x) => x.id === w.id);
          if (idx >= 0) {
            const next = [...prev];
            next[idx] = { ...next[idx], ...w };
            return next;
          }
          return [...prev, w];
        });
      }
    },
    onStatus: (msg, w) => { setStatus(msg); setIsWorking(w); },
    onIO: (line: IOLine) => {
      const role = line.role || "";
      if (role) {
        setActiveRole(role);
        setKnownRoles((prev) => prev.includes(role) ? prev : [...prev, role]);
      }
      if (line.streamType === "tool") {
        closeThinking();
        if (line.text.trim() && !line.text.startsWith("\n[tool:")) {
          updateLastTool(line.text.trim());
        }
      } else if (line.streamType === "claude") {
        closeTool();
        closeThinking();
        setMessages((prev) => {
          if (prev.length > 0 && prev[prev.length - 1].type === "claude" && prev[prev.length - 1].role === role) {
            const last = prev[prev.length - 1];
            return [...prev.slice(0, -1), { ...last, text: last.text + line.text }];
          }
          return [...prev, { id: nextId(), type: "claude", text: line.text, role: role || undefined, toolOutput: [], toolDone: false }];
        });
      } else if (line.streamType === "thinking") {
        closeTool();
        setMessages((prev) => {
          if (prev.length > 0 && prev[prev.length - 1].type === "thinking" && prev[prev.length - 1].role === role) {
            const last = prev[prev.length - 1];
            return [...prev.slice(0, -1), { ...last, text: last.text + line.text }];
          }
          const newId = nextId();
          thinkingId.current = newId;
          return [...prev, { id: newId, type: "thinking", text: line.text, role: role || undefined, toolOutput: [], toolDone: false }];
        });
      } else if (line.streamType === "error") {
        closeTool();
        closeThinking();
        addMsg({ id: nextId(), type: "error", text: line.text, role: role || undefined, toolOutput: [], toolDone: false });
      } else if (line.streamType === "tool_result") {
        if (line.text.trim()) {
          updateLastTool(line.text.trim());
        }
      } else if (line.streamType === "prompt") {
        // System/meta messages — show as subtle info
        const cleanText = line.text.replace(/\n/g, " ").trim();
        if (cleanText) {
          closeTool();
          closeThinking();
          addMsg({ id: nextId(), type: "raw", text: cleanText, role: role || undefined, toolOutput: [], toolDone: false });
        }
      } else {
        if (line.text.trim()) {
          closeTool();
          closeThinking();
          addMsg({ id: nextId(), type: "raw", text: line.text, role: role || undefined, toolOutput: [], toolDone: false });
        }
      }
    },
    onClearIO: () => { setMessages([]); openToolId.current = null; thinkingId.current = null; setWorkers([]); setFilterRole(""); },
    onHeader: () => {},
    onCost: (data) => {
      setGrand(data.grand_total);
      setGen(data.gen_total);
      setRoleCosts((prev) => {
        const idx = prev.findIndex((c) => c.role === data.role);
        const updated = {
          role: data.role,
          input_tokens: (idx >= 0 ? prev[idx].input_tokens : 0) + (data.input_tokens ?? 0),
          output_tokens: (idx >= 0 ? prev[idx].output_tokens : 0) + (data.output_tokens ?? 0),
          cost_usd: (idx >= 0 ? prev[idx].cost_usd : 0) + (data.cost_usd ?? 0),
        };
        if (idx >= 0) { const next = [...prev]; next[idx] = updated; return next; }
        return [...prev, updated];
      });
    },
    onToolCall: (data) => {
      closeTool();
      const id = nextId();
      openToolId.current = id;
      const role = data.role || activeRoleRef.current || undefined;
      if (role && !knownRoles.includes(role)) setKnownRoles((prev) => [...prev, role]);
      addMsg({ id, type: "tool_call", text: data.tool_name, role, toolName: data.tool_name, toolArgs: data.args, toolOutput: [], toolDone: false });
    },
    onEvalTable: (rows) => {
      setLeaderboard((prev) => {
        const prevMap = new Map(prev.map((b) => [b.name, b]));
        return rows.map((r: { rank: number; name: string; rating: number; rd: number; conservative_rating: number; h2h_avg_wr?: number }) => {
          const existing = prevMap.get(r.name);
          return {
            name: r.name, rank: r.rank, rating: r.rating, rd: r.rd,
            sigma: existing?.sigma ?? 0, conservative_rating: r.conservative_rating,
            confidence: existing?.confidence ?? (r.rd < 50 ? "very_confident" : r.rd < 100 ? "confident" : r.rd < 200 ? "uncertain" : "very_uncertain"),
            last_period: existing?.last_period ?? "", win_rate: existing?.win_rate, games: existing?.games, h2h_avg_wr: r.h2h_avg_wr,
          };
        });
      });
    },
    onMetrics: (m) => setMetrics(m),
    onDaemon: () => {},
    onConnect: () => {
      setRoleCosts([]); setMessages([]); setHistoryLines([]); setWorkers([]);
      setFilterRole(""); setActiveRole(""); setKnownRoles([]);
      openToolId.current = null; thinkingId.current = null;
      fetchEvolutionState().then((state) => {
        if (state) { setGrand(state.grand_cost_total ?? 0); setGen(state.gen_cost_total ?? 0); }
      }).catch((e) => console.error("[EvolutionMonitor] fetchEvolutionState error:", e));
    },
  });

  useEffect(() => {
    fetchEvolutionState().then((state) => {
      if (state) { setStatus(state.status); setIsWorking(state.is_working); }
    }).catch((e) => console.error("[EvolutionMonitor] API error:", e));
    const refreshLeaderboard = () => api.ratings().then(setLeaderboard).catch((e) => console.error("[EvolutionMonitor] API error:", e));
    refreshLeaderboard();
    const refreshPipeline = () => api.pipelineCheckpoint().then(setCheckpoint).catch((e) => console.error("[EvolutionMonitor] API error:", e));
    refreshPipeline();
    const pipeInterval = setInterval(refreshPipeline, 5000);
    const refreshFailures = () => api.pipelineFailures(3).then(setFailures).catch((e) => console.error("[EvolutionMonitor] API error:", e));
    refreshFailures();
    const failInterval = setInterval(refreshFailures, 30000);
    const disconnect = connect();
    return () => { clearInterval(pipeInterval); clearInterval(failInterval); disconnect(); };
  }, []);

  useEffect(() => {
    if (autoScroll && ioRef.current) {
      ioRef.current.scrollTop = ioRef.current.scrollHeight;
    }
  }, [messages, autoScroll]);

  const handleScroll = useCallback((e: React.UIEvent<HTMLDivElement>) => {
    const el = e.currentTarget;
    setAutoScroll(el.scrollHeight - el.scrollTop - el.clientHeight < 50);
  }, []);

  const handleCopy = useCallback(() => {
    const text = messages.map((m) => {
      if (m.type === "tool_call") return `[tool: ${m.toolName}]`;
      if (m.type === "thinking") return `[thinking] ${m.text}`;
      return m.text;
    }).join("\n");
    navigator.clipboard.writeText(text).catch(() => {});
  }, [messages]);

  const filteredMessages = useMemo(() => {
    if (!filterRole) return messages;
    return messages.filter((m) => m.role === filterRole);
  }, [messages, filterRole]);

  const tabs: { key: TabKey; label: string }[] = [
    { key: "pipeline", label: "流水线" },
    { key: "metrics", label: "指标" },
    { key: "history", label: "历史" },
  ];

  return (
    <>
      <PageMeta title="进化监控 — Bot 自进化" description="实时进化视图" />
      {/* Compact status bar */}
      <div className="mb-4 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
        <div className="flex items-center gap-2">
          <Badge variant={isWorking ? "success" : status === "连接中..." ? "neutral" : "warning"} size="sm" pulse={isWorking}>
            {isWorking ? "运行中" : status === "连接中..." ? "连接中" : "空闲"}
          </Badge>
        </div>
        <div className="w-px h-4 bg-gray-200 dark:bg-gray-700" />
        <span className="text-gray-600 dark:text-gray-400">
          总成本 <span className="font-semibold text-gray-900 dark:text-white">${grand.toFixed(2)}</span>
        </span>
        <div className="w-px h-4 bg-gray-200 dark:bg-gray-700" />
        <span className="text-gray-600 dark:text-gray-400">
          成功率 <span className={cn(
            "font-semibold",
            metrics.success_rate != null && metrics.success_rate >= 0.8 ? "text-success-600 dark:text-success-400"
            : metrics.success_rate != null && metrics.success_rate >= 0.5 ? "text-warning-600 dark:text-warning-400"
            : metrics.success_rate != null ? "text-error-600 dark:text-error-400"
            : "text-gray-900 dark:text-white",
          )}>{metrics.success_rate != null ? `${(metrics.success_rate * 100).toFixed(0)}%` : "—"}</span>
        </span>
        <div className="w-px h-4 bg-gray-200 dark:bg-gray-700" />
        <span className="text-gray-600 dark:text-gray-400">
          代次 <span className="font-semibold text-gray-900 dark:text-white">{metrics.total_gens ?? "—"}</span>
        </span>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        {/* Terminal stream */}
        <div className="relative overflow-hidden rounded-2xl border border-gray-800 bg-[#0d1117] lg:col-span-2">
          {/* Title bar - macOS style */}
          <div className="flex items-center justify-between border-b border-gray-800 bg-[#161b22] px-4 py-2">
            <div className="flex items-center gap-3">
              <div className="flex gap-1.5">
                <span className="w-3 h-3 rounded-full bg-[#ff5f57]" />
                <span className="w-3 h-3 rounded-full bg-[#febc2e]" />
                <span className="w-3 h-3 rounded-full bg-[#28c840]" />
              </div>
              <span className="text-xs font-medium text-gray-400">LLM 对话流</span>
              {isWorking && <Badge variant="success" size="sm" pulse>LIVE</Badge>}
            </div>
            <div className="flex items-center gap-2">
              <button onClick={handleCopy} title="复制" className="p-1 rounded hover:bg-gray-800 text-gray-500 hover:text-gray-300 transition-colors">
                <CopyIcon />
              </button>
              <button
                onClick={() => setAutoScroll(!autoScroll)}
                className={cn(
                  "rounded px-2 py-1 text-[10px] font-medium transition-colors",
                  autoScroll ? "bg-brand-500/20 text-brand-400" : "text-gray-500 hover:text-gray-300",
                )}
              >
                {autoScroll ? "自动滚动: 开" : "自动滚动: 关"}
              </button>
              <button
                onClick={() => { setMessages([]); openToolId.current = null; thinkingId.current = null; }}
                className="text-[10px] text-gray-500 hover:text-gray-300 transition-colors"
              >
                清空
              </button>
            </div>
          </div>

          {/* Role pills */}
          {knownRoles.length > 0 && (
            <div className="flex items-center gap-1.5 px-3 py-1.5 border-b border-gray-800/60 overflow-x-auto">
              <button
                onClick={() => setFilterRole("")}
                className={cn(
                  "shrink-0 rounded-full px-2.5 py-0.5 text-[10px] font-medium transition-colors",
                  !filterRole
                    ? "bg-white/10 text-white"
                    : "text-gray-500 hover:text-gray-300 hover:bg-white/5",
                )}
              >
                全部
              </button>
              {knownRoles.map((role) => {
                const color = getRoleColor(role);
                const isActive = activeRole === role;
                const isFiltered = filterRole === role;
                return (
                  <button
                    key={role}
                    onClick={() => setFilterRole(isFiltered ? "" : role)}
                    className={cn(
                      "shrink-0 rounded-full px-2.5 py-0.5 text-[10px] font-medium transition-all flex items-center gap-1",
                      color.bg, color.text,
                      isFiltered ? "ring-1 ring-current" : "opacity-70 hover:opacity-100",
                      isActive && "opacity-100",
                    )}
                  >
                    <span className={cn("inline-block w-1.5 h-1.5 rounded-full", color.dot, isActive && isWorking && "animate-pulse")} />
                    {shortRoleName(role)}
                  </button>
                );
              })}
            </div>
          )}

          {/* Message area */}
          <div
            ref={ioRef}
            onScroll={handleScroll}
            className="h-[500px] overflow-y-auto p-4 font-mono text-[13px] leading-relaxed custom-scrollbar"
          >
            {filteredMessages.length === 0 && messages.length === 0 && (
              <div className="flex items-center justify-center h-full text-gray-600 text-sm">
                等待进化输出...
              </div>
            )}
            {filteredMessages.length === 0 && messages.length > 0 && (
              <div className="flex items-center justify-center h-full text-gray-600 text-sm">
                该角色暂无输出
              </div>
            )}
            {filteredMessages.map((msg, idx) => {
              const prevMsg = idx > 0 ? filteredMessages[idx - 1] : null;
              const showRoleLabel = msg.role && msg.role !== prevMsg?.role && !filterRole;
              const roleColor = msg.role ? getRoleColor(msg.role) : null;
              return (
                <div key={msg.id}>
                  {showRoleLabel && roleColor && (
                    <div className={cn("mt-2 mb-1 flex items-center gap-1.5", roleColor.text)}>
                      <span className={cn("inline-block w-1.5 h-1.5 rounded-full", roleColor.dot)} />
                      <span className="text-[10px] font-semibold uppercase tracking-wide">{shortRoleName(msg.role!)}</span>
                    </div>
                  )}
                  {msg.type === "tool_call" ? (
                    <ToolCard msg={msg} />
                  ) : msg.type === "thinking" ? (
                    <ThinkingBlock text={msg.text} done={msg.toolDone} />
                  ) : msg.type === "error" ? (
                    <div className={cn("my-0.5 border-l-2 rounded px-2 py-0.5 font-medium", roleColor ? `${roleColor.border} bg-red-950/30 ${roleColor.text}` : "border-red-500 bg-red-950/40 text-red-400")}>
                      <CrossIcon className="inline mr-1 w-3 h-3" /> {msg.text}
                    </div>
                  ) : (
                    // claude / raw
                    msg.text.split("\n").map((textLine, j) => (
                      <div
                        key={`${msg.id}-${j}`}
                        className={cn(
                          "animate-fade-in-up",
                          msg.type === "claude" ? (roleColor ? roleColor.text : "text-gray-200") : "text-gray-500",
                        )}
                      >
                        {msg.type === "claude" ? <span className={roleColor ? `${roleColor.text} opacity-50` : "text-emerald-500"}>▸ </span> : "  "}{textLine}
                      </div>
                    ))
                  )}
                </div>
              );
            })}
            {/* Streaming cursor */}
            {isWorking && (
              <span className="inline-block w-2 h-4 bg-indigo-400 animate-cursor-blink ml-1" />
            )}
          </div>

          {/* Scroll to bottom button */}
          {!autoScroll && filteredMessages.length > 0 && (
            <button
              onClick={() => { setAutoScroll(true); if (ioRef.current) ioRef.current.scrollTop = ioRef.current.scrollHeight; }}
              className="absolute bottom-4 left-1/2 -translate-x-1/2 px-3 py-1.5 rounded-full bg-brand-500 text-white text-xs shadow-lg hover:bg-brand-600 transition-all"
            >
              ↓ 跳到底部
            </button>
          )}
        </div>

        {/* Right panel with tabs */}
        <div className="rounded-2xl border border-gray-200 bg-white dark:border-border-subtle dark:bg-surface-1 overflow-hidden flex flex-col">
          {/* Tab bar */}
          <div className="flex border-b border-gray-200 dark:border-border-subtle px-1 pt-2">
            {tabs.map((tab) => (
              <button
                key={tab.key}
                onClick={() => setActiveTab(tab.key)}
                className={cn(
                  "flex-1 px-3 py-2 text-xs font-medium border-b-2 transition-colors",
                  activeTab === tab.key
                    ? "border-brand-500 text-brand-600 dark:text-brand-400"
                    : "border-transparent text-gray-500 hover:text-gray-700 dark:hover:text-gray-300",
                )}
              >
                {tab.label}
              </button>
            ))}
          </div>

          {/* Tab content */}
          <div className="flex-1 overflow-y-auto">
            {activeTab === "pipeline" && (
              <div className="divide-y divide-gray-100 dark:divide-gray-800">
                <PipelineStatus checkpoint={checkpoint} />
                <WorkerProgress workers={workers} />
                <CostBreakdown costs={roleCosts} grand={grand} gen={gen} onReset={() => { setRoleCosts([]); setGen(0); }} />
              </div>
            )}

            {activeTab === "metrics" && (
              <div className="divide-y divide-gray-100 dark:divide-gray-800">
                {/* Evolution metrics */}
                {Object.keys(metrics).length > 0 && (
                  <div className="p-3">
                    <h3 className="mb-2 text-xs font-semibold uppercase text-gray-500">进化指标</h3>
                    <div className="space-y-1.5 text-xs">
                      {metrics.success_rate != null && (
                        <div className="flex justify-between">
                          <span className="text-gray-500">成功率</span>
                          <span className={metrics.success_rate >= 0.8 ? "text-success-600 font-medium" : metrics.success_rate >= 0.5 ? "text-warning-600 font-medium" : "text-error-600 font-medium"}>
                            {(metrics.success_rate * 100).toFixed(0)}%
                          </span>
                        </div>
                      )}
                      {metrics.rating_trend != null && (
                        <div className="flex justify-between">
                          <span className="text-gray-500">评分趋势</span>
                          <span className={cn("font-medium", metrics.rating_trend > 0 ? "text-success-600" : metrics.rating_trend < 0 ? "text-error-600" : "text-gray-500")}>
                            {metrics.rating_trend > 0 ? "↑" : metrics.rating_trend < 0 ? "↓" : "→"} {metrics.rating_trend > 0 ? "+" : ""}{Math.round(metrics.rating_trend)}
                          </span>
                        </div>
                      )}
                      {metrics.avg_gen_time_s != null && (
                        <div className="flex justify-between">
                          <span className="text-gray-500">平均耗时</span>
                          <span className="text-gray-700 dark:text-gray-300">{Math.round(metrics.avg_gen_time_s)}s</span>
                        </div>
                      )}
                      <div className="flex justify-between text-gray-400">
                        <span>代次 {metrics.total_gens ?? "—"}</span>
                        <span>失败 {metrics.fail_count ?? "—"}</span>
                      </div>
                    </div>
                  </div>
                )}

                {/* Leaderboard */}
                {leaderboard.length > 0 && (
                  <div className="p-3">
                    <h3 className="mb-2 text-xs font-semibold uppercase text-gray-500">排行榜</h3>
                    <div className="space-y-1">
                      {leaderboard.slice(0, 10).map((bot) => (
                        <div key={bot.name} className="flex justify-between text-xs items-center">
                          <span className="text-gray-600 dark:text-gray-400 truncate">
                            {bot.rank <= 3 ? (
                              <span className={cn(
                                "inline-flex items-center justify-center w-4 h-4 rounded-full text-[9px] font-bold mr-1",
                                bot.rank === 1 && "bg-amber-400 text-amber-900",
                                bot.rank === 2 && "bg-gray-300 text-gray-700",
                                bot.rank === 3 && "bg-orange-400 text-orange-900",
                              )}>{bot.rank}</span>
                            ) : (
                              <span className="text-gray-400 mr-1">#{bot.rank}</span>
                            )}
                            {bot.name.replace("claude_", "")}
                          </span>
                          <span className="font-mono text-gray-800 dark:text-gray-200 shrink-0 ml-2">
                            {bot.h2h_avg_wr != null ? `${(bot.h2h_avg_wr * 100).toFixed(1)}%` : "—"}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {/* Worker failures */}
                {failures.length > 0 && (
                  <div className="p-3">
                    <h3 className="mb-2 text-xs font-semibold uppercase text-error-600 dark:text-error-400">最近失败</h3>
                    <div className="space-y-2">
                      {failures.map((f, i) => (
                        <div key={i} className="text-xs">
                          <div className="font-medium text-error-700 dark:text-error-300">第 {f.gen} 代 Worker {f.worker_id} ({f.role})</div>
                          <div className="text-error-500 dark:text-error-400 truncate">{f.error}</div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}

            {activeTab === "history" && (
              <div className="p-3">
                <h3 className="mb-2 text-xs font-semibold uppercase text-gray-500">历史</h3>
                <div className="space-y-1">
                  {historyLines.length === 0 && <div className="text-xs text-gray-500">暂无事件</div>}
                  {historyLines.slice(-100).reverse().map((line, i) => (
                    <div key={i} className={cn(
                      "text-xs py-0.5",
                      line.status === "error" ? "text-red-400" :
                      line.status === "warn" ? "text-amber-400" :
                      line.status === "success" ? "text-emerald-400" : "text-gray-400",
                    )}>{line.msg}</div>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </>
  );
}
