import { useEffect, useRef, useState, useCallback } from "react";
import { useEvolutionSSE, fetchEvolutionState } from "../api/evolution";
import type { IOLine } from "../api/evolution";
import { api } from "../api/client";
import type { BotRating, PipelineCheckpoint, WorkerFailure } from "../api/types";
import PageMeta from "../components/common/PageMeta";

// ── Inline SVG helpers (replacing emoji) ──────────────────────────────────────

const StatusDot = ({ className }: { className?: string }) => (
  <svg width="10" height="10" viewBox="0 0 10 10" className={className}><circle cx="5" cy="5" r="4" fill="currentColor"/></svg>
);
const CheckIcon = ({ className }: { className?: string }) => (
  <svg width="12" height="12" viewBox="0 0 12 12" className={className}><path d="M2 6.5l2.5 2.5L10 3.5" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round" strokeLinejoin="round"/></svg>
);
const CrossIcon = ({ className }: { className?: string }) => (
  <svg width="12" height="12" viewBox="0 0 12 12" className={className}><path d="M3 3l6 6M9 3l-6 6" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round"/></svg>
);
const GearIcon = ({ className }: { className?: string }) => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className}><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.67 15 1.65 1.65 0 0 0 3 13.5V13a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V21a2 2 0 1 1 4 0v-.09a1.65 1.65 0 0 0 .33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H21a2 2 0 1 1 0-4h-.09a1.65 1.65 0 0 0-1.51-1z"/></svg>
);
const ThoughtIcon = ({ className }: { className?: string }) => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className}><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
);

// ── Pipeline ──────────────────────────────────────────────────────────────────

const PIPELINE_STAGES = ["prepared", "workers_done", "quality_passed", "reviewed", "critic_checked", "verified"];
const STAGE_LABELS: Record<string, string> = {
  prepared: "环境就绪",
  workers_done: "Worker 完成",
  quality_passed: "质量检查通过",
  reviewed: "代码审核通过",
  critic_checked: "策略审核通过",
  verified: "提交前验证",
};

function PipelineStatus({ checkpoint }: { checkpoint: PipelineCheckpoint | null }) {
  const [expanded, setExpanded] = useState(false);
  if (!checkpoint) {
    return (
      <div className="rounded-2xl border border-gray-200 bg-white p-3 dark:border-gray-800 dark:bg-white/[0.03]">
        <h3 className="mb-2 text-xs font-semibold uppercase text-gray-500">流水线</h3>
        <p className="text-xs text-gray-400">无活跃代次</p>
      </div>
    );
  }
  const currentIdx = PIPELINE_STAGES.indexOf(checkpoint.stage);
  const plan = Array.isArray(checkpoint.master_plan) ? checkpoint.master_plan : [];

  return (
    <div className="rounded-2xl border border-gray-200 bg-white p-3 dark:border-gray-800 dark:bg-white/[0.03]">
      <button onClick={() => setExpanded(!expanded)} className="w-full text-left flex items-center justify-between">
        <h3 className="text-xs font-semibold uppercase text-gray-500">流水线</h3>
        <span className="text-[10px] text-gray-400">{expanded ? "▲" : "▼"}</span>
      </button>
      <p className="text-xs text-gray-400 my-1">
        v{checkpoint.next_v} ← v{checkpoint.source_v}
        {checkpoint.generation_attempt ? ` (尝试 ${checkpoint.generation_attempt})` : ""}
      </p>
      <div className="flex gap-1 flex-wrap">
        {PIPELINE_STAGES.map((stage, i) => (
          <span
            key={stage}
            className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${
              i < currentIdx
                ? "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400"
                : i === currentIdx
                ? "bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400 ring-1 ring-blue-400"
                : "bg-gray-100 text-gray-400 dark:bg-gray-800 dark:text-gray-500"
            }`}
          >
            {STAGE_LABELS[stage]}
          </span>
        ))}
      </div>
      {expanded && (
        <div className="mt-2 pt-2 border-t border-gray-100 dark:border-gray-700 space-y-2">
          {plan.length > 0 && (
            <div>
              <p className="text-[10px] text-gray-500 mb-1">Master Plan</p>
              {plan.map((task: Record<string, unknown>, i: number) => (
                <div key={i} className="text-[10px] text-gray-600 dark:text-gray-400 pl-2 border-l-2 border-blue-300 mb-1">
                  <span className="font-medium">{String(task.role || `Task ${i + 1}`)}</span>
                  {task.target_files ? <span className="text-gray-400 ml-1">→ {Array.isArray(task.target_files) ? (task.target_files as string[]).join(", ") : String(task.target_files)}</span> : null}
                  {task.difficulty ? <span className="ml-1 px-1 rounded bg-gray-100 dark:bg-gray-800 text-gray-500">{String(task.difficulty)}</span> : null}
                </div>
              ))}
            </div>
          )}
          {checkpoint.reviewer_feedback && (
            <div>
              <p className="text-[10px] text-gray-500 mb-1">Reviewer 反馈</p>
              <p className="text-[10px] text-gray-600 dark:text-gray-400 whitespace-pre-wrap max-h-24 overflow-y-auto">{checkpoint.reviewer_feedback}</p>
            </div>
          )}
          {(() => {
            const gates = checkpoint.gate_results as Record<string, Record<string, unknown>> | undefined;
            if (!gates || Object.keys(gates).length === 0) return null;
            const gateLabels: Record<string, string> = {
              quality: "质量检查",
              review: "代码审核",
              critic: "策略审核",
              precommit_eval: "提交前验证",
            };
            return (
              <div>
                <p className="text-[10px] text-gray-500 mb-1">质量门</p>
                <div className="space-y-1">
                  {Object.entries(gates).map(([key, g]) => {
                    const passed = g.passed ?? g.all_passed ?? g.approved;
                    return (
                      <div key={key} className="flex items-start gap-1.5 text-[10px] pl-2 border-l-2 border-blue-300">
                        <span className="shrink-0 mt-px">{passed ? <CheckIcon className="text-green-600" /> : <CrossIcon className="text-red-500" />}</span>
                        <div>
                          <span className="font-medium text-gray-700 dark:text-gray-300">{gateLabels[key] || key}</span>
                          {g.quality_score != null && <span className="ml-1 text-gray-400">分数 {String(g.quality_score)}</span>}
                          {g.score != null && <span className="ml-1 text-gray-400">分数 {String(g.score)}</span>}
                          {g.decision_pass_rate != null && <span className="ml-1 text-gray-400">决策 {String(Math.round((g.decision_pass_rate as number) * 100))}%</span>}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            );
          })()}
        </div>
      )}
    </div>
  );
}

// ── Cost breakdown ─────────────────────────────────────────────────────────────

interface RoleCost {
  role: string;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
}

function CostBreakdown({
  costs,
  grand,
  gen,
  onReset,
}: {
  costs: RoleCost[];
  grand: number;
  gen: number;
  onReset: () => void;
}) {
  if (costs.length === 0 && grand === 0) return null;
  return (
    <div className="rounded-2xl border border-gray-200 bg-white p-3 dark:border-gray-800 dark:bg-white/[0.03]">
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-xs font-semibold uppercase text-gray-500">LLM 成本</h3>
        <button onClick={onReset} className="text-[10px] text-gray-400 hover:text-gray-600 underline">重置本代</button>
      </div>
      <div className="space-y-1">
        {costs.map((c) => (
          <div key={c.role} className="flex justify-between text-xs">
            <span className="text-gray-500 truncate max-w-[90px]">{c.role}</span>
            <span className="text-gray-400 font-mono">{(c.input_tokens + c.output_tokens).toLocaleString()} tokens</span>
            <span className="text-gray-800 dark:text-gray-200 font-mono">${c.cost_usd.toFixed(4)}</span>
          </div>
        ))}
      </div>
      <div className="mt-2 pt-2 border-t border-gray-100 dark:border-gray-700 flex justify-between text-xs font-medium">
        <span className="text-gray-500">本代 / 总计</span>
        <span className="text-gray-800 dark:text-gray-200 font-mono">${gen.toFixed(3)} / ${grand.toFixed(3)}</span>
      </div>
    </div>
  );
}

// ── Worker progress ────────────────────────────────────────────────────────────

type WorkerStatus = "running" | "done" | "failed";

interface WorkerInfo {
  id: number;
  role?: string;
  status: WorkerStatus;
}

function parseWorkerStatus(msg: string): { id: number; role?: string; status: WorkerStatus } | null {
  const startMatch = msg.match(/Worker[s]?\s+(\d+)(?:\s*\(([^)]+)\))?\s*(start|begin|running|launch)/i);
  if (startMatch) return { id: parseInt(startMatch[1]), role: startMatch[2], status: "running" };
  const doneMatch = msg.match(/Worker[s]?\s+(\d+)(?:\s*\(([^)]+)\))?\s*(done|finish|success|complete)/i);
  if (doneMatch) return { id: parseInt(doneMatch[1]), role: doneMatch[2], status: "done" };
  const failMatch = msg.match(/Worker[s]?\s+(\d+)(?:\s*\(([^)]+)\))?\s*(fail|error|timeout)/i);
  if (failMatch) return { id: parseInt(failMatch[1]), role: failMatch[2], status: "failed" };
  return null;
}

function WorkerProgress({ workers }: { workers: WorkerInfo[] }) {
  if (workers.length === 0) return null;
  return (
    <div className="rounded-2xl border border-gray-200 bg-white p-3 dark:border-gray-800 dark:bg-white/[0.03]">
      <h3 className="mb-2 text-xs font-semibold uppercase text-gray-500">Worker</h3>
      <div className="space-y-1">
        {workers.map((w) => (
          <div key={w.id} className="flex items-center gap-2 text-xs">
            <span className={
              w.status === "running" ? "text-blue-500 animate-pulse" :
              w.status === "done" ? "text-green-500" : "text-red-500"
            }>
              {w.status === "running" ? <StatusDot className="inline" /> : w.status === "done" ? <CheckIcon className="inline" /> : <CrossIcon className="inline" />}
            </span>
            <span className="text-gray-600 dark:text-gray-300">
              Worker {w.id}{w.role ? ` (${w.role})` : ""}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Structured conversation ────────────────────────────────────────────────────

type MsgType = "claude" | "thinking" | "tool_call" | "error" | "raw";

interface ConvMsg {
  id: number;
  type: MsgType;
  text: string;
  toolName?: string;
  toolArgs?: Record<string, unknown>;
  toolOutput: string[];
  toolDone: boolean;
}

function ToolCard({ msg }: { msg: ConvMsg }) {
  const [expanded, setExpanded] = useState(false);
  const [argsExpanded, setArgsExpanded] = useState(false);

  return (
    <div className="my-1 rounded-lg border border-blue-800/40 bg-blue-950/30 overflow-hidden">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center justify-between px-3 py-2 text-left hover:bg-blue-950/50"
      >
        <span className="flex items-center gap-2">
          <GearIcon className="text-cyan-400" />
          <span className="text-cyan-300 text-xs font-mono font-medium">{msg.toolName}</span>
        </span>
        <span className="text-xs text-gray-500">
          {msg.toolDone ? "完成" : "运行中…"}
          {" "}
          {expanded ? "▲" : "▼"}
        </span>
      </button>

      {expanded && (
        <div className="border-t border-blue-800/30 px-3 py-2 space-y-2">
          {/* Args */}
          {msg.toolArgs && Object.keys(msg.toolArgs).length > 0 && (
            <div>
              <button
                onClick={() => setArgsExpanded(!argsExpanded)}
                className="text-[10px] text-gray-400 hover:text-gray-300 flex items-center gap-1"
              >
                <span>{argsExpanded ? "▼" : "▶"}</span> 参数
              </button>
              {argsExpanded && (
                <pre className="mt-1 text-[10px] font-mono text-gray-300 whitespace-pre-wrap">
                  {JSON.stringify(msg.toolArgs, null, 2)}
                </pre>
              )}
            </div>
          )}
          {/* Output */}
          {msg.toolOutput.length > 0 && (
            <div>
              <div className="text-[10px] text-gray-500 mb-1">输出</div>
              <div className="text-[10px] font-mono text-gray-400 whitespace-pre-wrap max-h-48 overflow-y-auto">
                {msg.toolOutput.join("\n")}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ThinkingBlock({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="my-1">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1.5 text-xs text-yellow-400/70 hover:text-yellow-300"
      >
        <ThoughtIcon className="text-yellow-400/70" />
        <span className="italic">思考中...</span>
        <span className="text-[10px]">{expanded ? "▲" : "▼"}</span>
      </button>
      {expanded && (
        <div className="mt-1 pl-4 text-xs text-yellow-300/60 italic whitespace-pre-wrap font-mono leading-relaxed max-h-48 overflow-y-auto">
          {text}
        </div>
      )}
    </div>
  );
}

// ── Main component ─────────────────────────────────────────────────────────────

let _msgId = 0;
const nextId = () => ++_msgId;

export default function EvolutionMonitor() {
  const [messages, setMessages] = useState<ConvMsg[]>([]);
  const [historyLines, setHistoryLines] = useState<Array<{ msg: string; status: string }>>([]);
  const [status, setStatus] = useState("连接中...");
  const [isWorking, setIsWorking] = useState(false);
  const [header, setHeader] = useState("进化框架");
  const [grand, setGrand] = useState(0);
  const [gen, setGen] = useState(0);
  const [leaderboard, setLeaderboard] = useState<BotRating[]>([]);
  const [autoScroll, setAutoScroll] = useState(true);
  const [checkpoint, setCheckpoint] = useState<PipelineCheckpoint | null>(null);
  const [failures, setFailures] = useState<WorkerFailure[]>([]);
  const [workers, setWorkers] = useState<WorkerInfo[]>([]);
  const [roleCosts, setRoleCosts] = useState<RoleCost[]>([]);
  const [metrics, setMetrics] = useState<Record<string, number>>({});
  const [daemonInfo, setDaemonInfo] = useState<{ total_matches: number; total_periods: number; total_games: number; n_bots: number } | null>(null);

  const ioRef = useRef<HTMLDivElement>(null);
  const openToolId = useRef<number | null>(null);

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
    onStatus: (msg, w) => {
      setStatus(msg);
      setIsWorking(w);
    },
    onIO: (line: IOLine) => {
      if (line.streamType === "tool") {
        if (line.text.trim() && !line.text.startsWith("\n[tool:")) {
          updateLastTool(line.text.trim());
        }
      } else if (line.streamType === "claude") {
        closeTool();
        setMessages((prev) => {
          if (prev.length > 0 && prev[prev.length - 1].type === "claude") {
            const last = prev[prev.length - 1];
            return [...prev.slice(0, -1), { ...last, text: last.text + line.text }];
          }
          return [...prev, { id: nextId(), type: "claude", text: line.text, toolOutput: [], toolDone: false }];
        });
      } else if (line.streamType === "thinking") {
        closeTool();
        setMessages((prev) => {
          if (prev.length > 0 && prev[prev.length - 1].type === "thinking") {
            const last = prev[prev.length - 1];
            return [...prev.slice(0, -1), { ...last, text: last.text + line.text }];
          }
          return [...prev, { id: nextId(), type: "thinking", text: line.text, toolOutput: [], toolDone: false }];
        });
      } else if (line.streamType === "error") {
        closeTool();
        addMsg({ id: nextId(), type: "error", text: line.text, toolOutput: [], toolDone: false });
      } else {
        if (line.text.trim()) {
          closeTool();
          addMsg({ id: nextId(), type: "raw", text: line.text, toolOutput: [], toolDone: false });
        }
      }
    },
    onClearIO: () => {
      setMessages([]);
      openToolId.current = null;
      setWorkers([]);
    },
    onHeader: (msg) => setHeader(msg),
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
        if (idx >= 0) {
          const next = [...prev];
          next[idx] = updated;
          return next;
        }
        return [...prev, updated];
      });
    },
    onToolCall: (data) => {
      closeTool();
      const id = nextId();
      openToolId.current = id;
      const msg: ConvMsg = {
        id,
        type: "tool_call",
        text: data.tool_name,
        toolName: data.tool_name,
        toolArgs: data.args,
        toolOutput: [],
        toolDone: false,
      };
      addMsg(msg);
    },
    onEvalTable: (rows) => {
      setLeaderboard((prev) => {
        const prevMap = new Map(prev.map((b) => [b.name, b]));
        return rows.map((r: { rank: number; name: string; rating: number; rd: number; conservative: number; h2h_avg_wr?: number }) => {
          const existing = prevMap.get(r.name);
          return {
            name: r.name,
            rank: r.rank,
            rating: r.rating,
            rd: r.rd,
            sigma: existing?.sigma ?? 0,
            conservative_rating: r.conservative,
            confidence: existing?.confidence ?? (r.rd < 50 ? "very_confident" : r.rd < 100 ? "confident" : r.rd < 200 ? "uncertain" : "very_uncertain"),
            last_period: existing?.last_period ?? "",
            win_rate: existing?.win_rate,
            games: existing?.games,
            h2h_avg_wr: r.h2h_avg_wr,
          };
        });
      });
    },
    onMetrics: (m) => setMetrics(m),
    onDaemon: (data) => setDaemonInfo(data),
  });

  useEffect(() => {
    fetchEvolutionState().then((state) => {
      if (state) {
        setStatus(state.status);
        setIsWorking(state.is_working);
        if (state.header) setHeader(state.header);
      }
    }).catch(() => {});
    const refreshLeaderboard = () => api.ratings().then(setLeaderboard).catch(() => {});
    refreshLeaderboard();

    const refreshPipeline = () => api.pipelineCheckpoint().then(setCheckpoint).catch(() => {});
    refreshPipeline();
    const pipeInterval = setInterval(refreshPipeline, 5000);

    const refreshFailures = () => api.pipelineFailures(3).then(setFailures).catch(() => {});
    refreshFailures();
    const failInterval = setInterval(refreshFailures, 30000);

    const disconnect = connect();
    return () => {
      clearInterval(pipeInterval);
      clearInterval(failInterval);
      disconnect();
    };
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

  return (
    <>
      <PageMeta title="进化监控 — Bot 自进化" description="实时进化视图" />

      {/* Header */}
      <div className="mb-4 rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-white/[0.03]">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold text-gray-800 dark:text-white">{header}</h2>
          <div className="flex items-center gap-4 text-sm">
            <span className="flex items-center gap-1.5 text-gray-500">
              <span className={`inline-block size-2 rounded-full ${isWorking ? "animate-pulse bg-green-400" : "bg-gray-400"}`} />
              {status}
            </span>
            <span className="text-gray-400">成本: ${grand.toFixed(3)}</span>
            {daemonInfo && (
              <span className="text-gray-400 text-xs">{daemonInfo.total_games} 场 / {daemonInfo.n_bots} Bot</span>
            )}
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        {/* Conversation stream - 2/3 */}
        <div className="overflow-hidden rounded-2xl border border-gray-800 bg-gray-950 lg:col-span-2">
          <div className="flex items-center justify-between border-b border-gray-800 px-4 py-2">
            <span className="text-xs font-medium text-gray-400">LLM 对话流</span>
            <div className="flex gap-2">
              <button
                onClick={() => setAutoScroll(!autoScroll)}
                className={`rounded px-2 py-1 text-xs ${autoScroll ? "bg-blue-900/30 text-blue-400" : "text-gray-500"}`}
              >
                {autoScroll ? "自动滚动: 开" : "自动滚动: 关"}
              </button>
              <button
                onClick={() => { setMessages([]); openToolId.current = null; }}
                className="text-xs text-gray-500 hover:text-gray-300"
              >
                清空
              </button>
            </div>
          </div>
          <div
            ref={ioRef}
            onScroll={handleScroll}
            className="h-[500px] overflow-y-auto p-3 font-mono text-xs leading-relaxed"
          >
            {messages.length === 0 && (
              <div className="text-gray-600">等待进化输出...</div>
            )}
            {messages.map((msg) => {
              if (msg.type === "tool_call") {
                return <ToolCard key={msg.id} msg={msg} />;
              }
              if (msg.type === "thinking") {
                return <ThinkingBlock key={msg.id} text={msg.text} />;
              }
              if (msg.type === "error") {
                return (
                  <div key={msg.id} className="text-red-400 font-bold">
                    <CrossIcon className="inline mr-1" /> {msg.text}
                  </div>
                );
              }
              // claude / raw
              return msg.text.split("\n").map((textLine, j) => (
                <div key={`${msg.id}-${j}`} className={msg.type === "claude" ? "text-green-400 font-medium" : "text-gray-200"}>
                  {msg.type === "claude" ? "▸ " : "  "}{textLine}
                </div>
              ));
            })}
          </div>
        </div>

        {/* Right panel - 1/3 */}
        <div className="space-y-3">
          {/* Pipeline status */}
          <PipelineStatus checkpoint={checkpoint} />

          {/* Cost breakdown */}
          <CostBreakdown
            costs={roleCosts}
            grand={grand}
            gen={gen}
            onReset={() => { setRoleCosts([]); setGen(0); }}
          />

          {/* Worker progress */}
          <WorkerProgress workers={workers} />

          {/* Evolution metrics */}
          {Object.keys(metrics).length > 0 && (
            <div className="rounded-2xl border border-gray-200 bg-white p-3 dark:border-gray-800 dark:bg-white/[0.03]">
              <h3 className="mb-2 text-xs font-semibold uppercase text-gray-500">进化指标</h3>
              <div className="space-y-1 text-xs">
                {metrics.success_rate != null && (
                  <div className="flex justify-between">
                    <span className="text-gray-500">成功率</span>
                    <span className={metrics.success_rate >= 0.8 ? "text-green-600" : metrics.success_rate >= 0.5 ? "text-yellow-600" : "text-red-600"}>
                      {(metrics.success_rate * 100).toFixed(0)}%
                    </span>
                  </div>
                )}
                {metrics.rating_trend != null && (
                  <div className="flex justify-between">
                    <span className="text-gray-500">评分趋势</span>
                    <span className={metrics.rating_trend > 0 ? "text-green-600" : metrics.rating_trend < 0 ? "text-red-600" : "text-gray-500"}>
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
                  <span>代次 {metrics.total_gens ?? "—"} / 失败 {metrics.fail_count ?? "—"}</span>
                </div>
              </div>
            </div>
          )}

          {/* Leaderboard */}
          {leaderboard.length > 0 && (
            <div className="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-white/[0.03]">
              <h3 className="mb-2 text-sm font-semibold text-gray-700 dark:text-gray-300">排行榜</h3>
              <div className="space-y-1">
                {leaderboard.slice(0, 8).map((bot) => (
                  <div key={bot.name} className="flex justify-between text-xs">
                    <span className="text-gray-600 dark:text-gray-400">#{bot.rank} {bot.name.replace("claude_", "")}</span>
                    <span className="font-mono text-gray-800 dark:text-gray-200">
                      {bot.h2h_avg_wr != null ? `${(bot.h2h_avg_wr * 100).toFixed(1)}%` : "—"}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Worker failures */}
          {failures.length > 0 && (
            <div className="rounded-2xl border border-red-200 bg-red-50 p-3 dark:border-red-900/40 dark:bg-red-900/10">
              <h3 className="mb-2 text-xs font-semibold text-red-600 dark:text-red-400 uppercase">最近失败</h3>
              <div className="space-y-2">
                {failures.map((f, i) => (
                  <div key={i} className="text-xs">
                    <div className="font-medium text-red-700 dark:text-red-300">第 {f.gen} 代 Worker {f.worker_id} ({f.role})</div>
                    <div className="text-red-500 dark:text-red-400 truncate">{f.error}</div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* History */}
          <div className="max-h-[200px] overflow-y-auto rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-white/[0.03]">
            <h3 className="mb-2 text-sm font-semibold text-gray-700 dark:text-gray-300">历史</h3>
            <div className="space-y-1">
              {historyLines.length === 0 && <div className="text-xs text-gray-500">暂无事件</div>}
              {historyLines.slice(-50).reverse().map((line, i) => (
                <div key={i} className={`text-xs ${
                  line.status === "error" ? "text-red-400" :
                  line.status === "warn" ? "text-yellow-400" :
                  line.status === "success" ? "text-green-400" : "text-gray-400"
                }`}>{line.msg}</div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
