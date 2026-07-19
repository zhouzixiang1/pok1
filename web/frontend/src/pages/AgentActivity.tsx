import { useEffect, useRef, useState, useCallback } from "react";
import { api } from "../api/client";
import type { AgentActivityResponse } from "../api/types";
import { useEvolutionSSE, fetchEvolutionState } from "../api/evolution";
import type { IOLine } from "../api/evolution";
import { useControlStatus } from "../hooks/useControlStatus";
import { epochStreamAuthorityKey } from "../lib/epochStreamAuthority";
import PageMeta from "../components/common/PageMeta";
import { Badge } from "../components/shared/Badge";
import { Card, CardHeader, EmptyState } from "../components/shared";
import { EpochAuthorityStatus } from "../components/evolution/EpochAuthorityStatus";
import { ToolCard, ThinkingBlock } from "../components/evolution/ToolCard";
import type { ConvMsg } from "../components/evolution/ToolCard";
import { CopyIcon, CrossIcon } from "../components/evolution/icons";
import { agentActivityView, type AgentRoleSummary } from "../domain/agentActivityView";
import { cn } from "../lib/utils";

let _msgId = 0;
const nextId = () => ++_msgId + Date.now();

const ROLE_TONE: Record<AgentRoleSummary["state"], { variant: "success" | "warning" | "neutral" | "info"; dot: string }> = {
  running: { variant: "info", dot: "bg-brand-400" },
  terminal: { variant: "success", dot: "bg-success-400" },
  not_reached: { variant: "neutral", dot: "bg-gray-400" },
  unknown: { variant: "warning", dot: "bg-warning-400" },
};

/**
 * Agent Activity — structured Master/Scouts/Workers/Reviewer/Critic/
 * Orchestrator projection paired with the live LLM conversation stream.
 *
 * The left panel consumes /api/pipeline/agents (checkpoint-derived).  The
 * right panel reuses the evolution SSE stream; it never reveals model tokens,
 * full sensitive prompts, or private reasoning — only the same role/tool/text
 * projections the existing EvolutionMonitor exposes.
 */
export default function AgentActivity() {
  const { status, health, loading, error } = useControlStatus(5_000);
  const streamAuthorityKey = epochStreamAuthorityKey(status);
  const epochReady = streamAuthorityKey !== null;
  const [agents, setAgents] = useState<AgentActivityResponse | null>(null);
  const [messages, setMessages] = useState<ConvMsg[]>([]);
  const [status2, setStatus2] = useState("连接中...");
  const [isWorking, setIsWorking] = useState(false);
  const [autoScroll, setAutoScroll] = useState(true);
  const ioRef = useRef<HTMLDivElement>(null);
  const openToolId = useRef<number | null>(null);
  const thinkingId = useRef<number | null>(null);

  const clearEpochProjection = useCallback(() => {
    setAgents(null);
    setMessages([]);
    setStatus2("等待 epoch 权威");
    setIsWorking(false);
    openToolId.current = null;
    thinkingId.current = null;
  }, []);

  // Structured agent activity poll.
  useEffect(() => {
    if (!epochReady) { setAgents(null); return; }
    let cancelled = false;
    const refresh = () => api.pipelineAgents().then((v) => { if (!cancelled) setAgents(v); }).catch((e) => {
      if (!cancelled) setAgents(null);
      console.error("[AgentActivity] agents error:", e);
    });
    refresh();
    const id = setInterval(refresh, 5000);
    return () => { cancelled = true; clearInterval(id); };
  }, [epochReady]);

  const addMsg = useCallback((msg: ConvMsg) => {
    setMessages((prev) => {
      const next = [...prev, msg];
      return next.length > 500 ? next.slice(-500) : next;
    });
  }, []);

  const updateLastTool = useCallback((text: string) => {
    if (openToolId.current == null) return;
    const id = openToolId.current;
    setMessages((prev) => prev.map((m) => m.id === id ? { ...m, toolOutput: [...m.toolOutput, text] } : m));
  }, []);

  const closeTool = useCallback(() => {
    if (openToolId.current == null) return;
    const id = openToolId.current;
    setMessages((prev) => prev.map((m) => m.id === id ? { ...m, toolDone: true } : m));
    openToolId.current = null;
  }, []);

  const closeThinking = useCallback(() => {
    if (thinkingId.current == null) return;
    const id = thinkingId.current;
    setMessages((prev) => prev.map((m) => m.id === id ? { ...m, toolDone: true } : m));
    thinkingId.current = null;
  }, []);

  const connect = useEvolutionSSE({
    onStatus: (evt) => {
      if (!epochReady) return;
      setStatus2(evt.msg);
      setIsWorking(Boolean(evt.is_working));
    },
    onIO: (line: IOLine) => {
      if (!epochReady) return;
      const role = line.role || "";
      if (line.streamType === "tool") {
        closeThinking();
        if (line.text.trim() && !line.text.startsWith("\n[tool:")) updateLastTool(line.text.trim());
      } else if (line.streamType === "claude") {
        closeTool(); closeThinking();
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
        closeTool(); closeThinking();
        addMsg({ id: nextId(), type: "error", text: line.text, role: role || undefined, toolOutput: [], toolDone: false });
      } else if (line.streamType === "tool_result") {
        if (line.text.trim()) updateLastTool(line.text.trim());
      } else if (line.streamType === "prompt") {
        const cleanText = line.text.replace(/\n/g, " ").trim();
        if (cleanText) {
          closeTool(); closeThinking();
          addMsg({ id: nextId(), type: "raw", text: cleanText, role: role || undefined, toolOutput: [], toolDone: false });
        }
      } else if (line.text.trim()) {
        closeTool(); closeThinking();
        addMsg({ id: nextId(), type: "raw", text: line.text, role: role || undefined, toolOutput: [], toolDone: false });
      }
    },
    onToolCall: (data) => {
      if (!epochReady) return;
      closeTool();
      const id = nextId();
      openToolId.current = id;
      const role = data.role || undefined;
      addMsg({ id, type: "tool_call", text: data.tool_name, role, toolName: data.tool_name, toolArgs: data.args, toolOutput: [], toolDone: false });
    },
    onEpochBlocked: () => clearEpochProjection(),
    onConnect: () => { if (!epochReady) return; setMessages([]); openToolId.current = null; thinkingId.current = null; },
    onHistory: () => {}, onHeader: () => {}, onCost: () => {}, onGenerationCostPolicy: () => {},
    onEvalTable: () => {}, onMetrics: () => {}, onDaemonStats: () => {}, onClearIO: () => {},
  }, streamAuthorityKey);

  useEffect(() => {
    if (!epochReady) { clearEpochProjection(); return; }
    let cancelled = false;
    fetchEvolutionState().then((state) => {
      if (cancelled) return;
      if (state.epoch_initialized && state.evaluation_epoch === "national_tcp_policy_v1") {
        setStatus2(state.status); setIsWorking(state.is_working);
      } else { clearEpochProjection(); }
    }).catch((e) => console.error("[AgentActivity] state error:", e));
    const disconnect = connect();
    return () => { cancelled = true; disconnect(); };
  }, [clearEpochProjection, connect, epochReady]);

  useEffect(() => {
    if (autoScroll && ioRef.current) ioRef.current.scrollTop = ioRef.current.scrollHeight;
  }, [messages, autoScroll]);

  const view = agents ? agentActivityView(agents) : null;
  const authoritativeWorking = Boolean(epochReady && status?.running && isWorking);
  const taskActive = health?.task?.present === true && health.task.done !== true;

  return (
    <>
      <PageMeta title="Agent 活动 — Bot 自进化" description="结构化 Agent 活动投影" />
      <EpochAuthorityStatus status={status} loading={loading} error={error} className="mb-4" />

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* Structured agent roles */}
        <div className="lg:col-span-1 space-y-4">
          <Card>
            <CardHeader title="角色活动" subtitle="/api/pipeline/agents · checkpoint 投影" />
            <div className="p-3 space-y-2">
              {!epochReady && <p className="text-xs text-gray-400">epoch 未初始化；agent 投影不可用。</p>}
              {epochReady && !view && <p className="text-xs text-gray-400">当前无 strict workflow；无 agent 活动。</p>}
              {view && !view.available && (
                <p className="text-xs text-gray-400">agent 投影不可用：{view.reason}</p>
              )}
              {view && view.available && (
                <>
                  <div className="text-xs text-gray-500 mb-1">
                    stage：<span className="font-mono text-gray-800 dark:text-gray-200">{view.stage ?? "(无)"}</span>
                    {view.stageIsTimeoutLease && <Badge variant="error" size="sm" className="ml-2">超时租约</Badge>}
                  </div>
                  {view.roles.map((role) => {
                    const tone = ROLE_TONE[role.state];
                    return (
                      <div key={role.role} className="rounded border border-gray-100 dark:border-gray-800 p-2">
                        <div className="flex items-center gap-2">
                          <span className={cn("inline-block w-1.5 h-1.5 rounded-full", tone.dot, role.state === "running" && authoritativeWorking && "animate-pulse")} />
                          <span className="text-xs font-semibold text-gray-700 dark:text-gray-200">{role.label}</span>
                          <Badge variant={tone.variant} size="sm" className="ml-auto">
                            {role.state === "running" ? "运行中" : role.state === "terminal" ? "已完成" : role.state === "not_reached" ? "未到达" : "未知"}
                          </Badge>
                        </div>
                        <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">{role.detail}</p>
                      </div>
                    );
                  })}
                  {view.master.tasks.length > 0 && (
                    <div className="mt-2 pt-2 border-t border-gray-100 dark:border-gray-800">
                      <p className="text-xs font-semibold text-gray-600 dark:text-gray-300 mb-1">Worker 任务</p>
                      {view.master.tasks.map((t, i) => (
                        <div key={i} className="text-xs text-gray-600 dark:text-gray-400 pl-2 border-l-2 border-brand-300 mb-1">
                          <span className="font-medium">#{t.worker_id} {t.role}</span>
                          {t.skill_layer && <span className="ml-1 text-gray-400">· {t.skill_layer}</span>}
                          {t.difficulty && <span className="ml-1 px-1 rounded bg-gray-100 dark:bg-gray-800 text-gray-500">{t.difficulty}</span>}
                        </div>
                      ))}
                    </div>
                  )}
                  {view.workerFailures.length > 0 && (
                    <div className="mt-2 pt-2 border-t border-gray-100 dark:border-gray-800">
                      <p className="text-xs font-semibold text-error-600 dark:text-error-400 mb-1">最近 Worker 失败</p>
                      {view.workerFailures.slice(0, 5).map((f, i) => (
                        <div key={i} className="text-xs text-error-700 dark:text-error-300">
                          #{f.worker_id} ({f.role})：{f.error}
                        </div>
                      ))}
                    </div>
                  )}
                </>
              )}
            </div>
          </Card>
        </div>

        {/* Live LLM conversation stream */}
        <div className="lg:col-span-2 rounded-2xl border border-gray-800 bg-[#0d1117] overflow-hidden flex flex-col">
          <div className="flex items-center justify-between border-b border-gray-800 bg-[#161b22] px-4 py-2">
            <div className="flex items-center gap-3">
              <span className="text-xs font-medium text-gray-400">LLM 对话流</span>
              <Badge variant={authoritativeWorking ? "success" : "neutral"} size="sm" pulse={authoritativeWorking}>
                {!epochReady ? "等待 epoch" : authoritativeWorking ? "LIVE" : status2 === "连接中..." ? "连接中" : "空闲"}
              </Badge>
              {!authoritativeWorking && epochReady && status?.running && taskActive && (
                <span className="text-xs text-warning-500">运行标志存在但任务未活动</span>
              )}
            </div>
            <div className="flex items-center gap-2">
              <button onClick={() => { navigator.clipboard.writeText(messages.map((m) => m.type === "tool_call" ? `[tool: ${m.toolName}]` : m.type === "thinking" ? `[thinking] ${m.text}` : m.text).join("\n")).catch(() => {}); }} title="复制" className="p-1 rounded hover:bg-gray-800 text-gray-500 hover:text-gray-300">
                <CopyIcon />
              </button>
              <button onClick={() => setAutoScroll(!autoScroll)} className={cn("rounded px-2 py-1 text-[10px]", autoScroll ? "bg-brand-500/20 text-brand-400" : "text-gray-500 hover:text-gray-300")}>
                {autoScroll ? "自动滚动:开" : "自动滚动:关"}
              </button>
            </div>
          </div>
          <div ref={ioRef} className="h-[500px] overflow-y-auto p-4 font-mono text-[13px] leading-relaxed custom-scrollbar">
            {!epochReady && (
              <EmptyState message="严格国赛 epoch 尚未初始化；旧对话流不会显示为当前进化。" />
            )}
            {epochReady && messages.length === 0 && (
              <EmptyState message="等待进化输出..." />
            )}
            {messages.map((msg) => (
              <div key={msg.id}>
                {msg.type === "tool_call" ? <ToolCard msg={msg} />
                  : msg.type === "thinking" ? <ThinkingBlock text={msg.text} done={msg.toolDone} />
                  : msg.type === "error" ? (
                    <div className="my-0.5 border-l-2 border-red-500 rounded px-2 py-0.5 font-medium text-red-400 bg-red-950/40">
                      <CrossIcon className="inline mr-1 w-3 h-3" /> {msg.text}
                    </div>
                  ) : (
                    msg.text.split("\n").map((textLine, j) => (
                      <div key={`${msg.id}-${j}`} className={cn("animate-fade-in-up", msg.type === "claude" ? "text-gray-200" : "text-gray-500")}>
                        {msg.type === "claude" ? <span className="text-emerald-500 opacity-50">▸ </span> : "  "}{textLine}
                      </div>
                    ))
                  )}
              </div>
            ))}
            {authoritativeWorking && <span className="inline-block w-2 h-4 bg-indigo-400 animate-cursor-blink ml-1" />}
          </div>
        </div>
      </div>
    </>
  );
}
