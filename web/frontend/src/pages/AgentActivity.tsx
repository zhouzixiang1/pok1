import { useEffect, useLayoutEffect, useRef, useState, useCallback } from "react";
import { useEvolutionSSE } from "../api/evolution";
import type { IOLine } from "../api/evolution";
import { useControlStatus } from "../hooks/useControlStatus";
import { useBoundAgentActivity } from "../hooks/useBoundAgentActivity";
import { agentWorkflowIdentityKey } from "../api/agentActivity";
import { epochStreamAuthorityKey } from "../lib/epochStreamAuthority";
import {
  acceptedEvolutionStatusAllowsIO,
  createTransientStatusTaskAuthorityState,
  evolutionStatusExpiryAt,
  isFreshEvolutionStatusEvent,
  loseTransientStatusTaskAuthority,
  observeTransientStatusTaskProjection,
  shouldAcceptEvolutionStatus,
  type EvolutionStatusEvent,
  type TransientStatusTask,
} from "../lib/evolutionStreamController";
import PageMeta from "../components/common/PageMeta";
import { Badge } from "../components/shared/Badge";
import { Card, CardHeader, EmptyState } from "../components/shared";
import { EpochAuthorityStatus } from "../components/evolution/EpochAuthorityStatus";
import { OperatorSituation } from "../components/evolution/OperatorSituation";
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

const transientStatusFallback = (task: TransientStatusTask | null): string => (
  task?.present === true
  && task.done === false
  && !task.shutdown_requested
  && task.status_eligible === true
    ? "等待当前活动任务状态"
    : "无可验证的当前活动任务状态"
);

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
  const { agents } = useBoundAgentActivity(
    status?.active_generation,
    epochReady,
  );
  const [messages, setMessages] = useState<ConvMsg[]>([]);
  const [status2, setStatus2] = useState("连接中...");
  const [isWorking, setIsWorking] = useState(false);
  const [autoScroll, setAutoScroll] = useState(true);
  const ioRef = useRef<HTMLDivElement>(null);
  const openToolId = useRef<number | null>(null);
  const thinkingId = useRef<number | null>(null);
  const activeGenerationRef = useRef(status?.active_generation ?? null);
  const controlTaskRef = useRef<TransientStatusTask | null>(null);
  const acceptedStatusRef = useRef<EvolutionStatusEvent | null>(null);
  const acceptedStatusAcceptedAtRef = useRef<number | null>(null);
  const acceptedStatusExpiryAtRef = useRef<number | null>(null);
  const [acceptedStatusExpiryAt, setAcceptedStatusExpiryAt] = useState<number | null>(null);
  const taskAuthorityRef = useRef(createTransientStatusTaskAuthorityState());
  const [streamTaskOwner, setStreamTaskOwner] = useState<TransientStatusTask | null>(null);
  activeGenerationRef.current = status?.active_generation ?? null;
  controlTaskRef.current = streamTaskOwner;
  const workflowIdentityKey = agentWorkflowIdentityKey(status?.active_generation);

  const clearEpochProjection = useCallback(() => {
    setMessages([]);
    setStatus2("等待严格进化身份");
    setIsWorking(false);
    openToolId.current = null;
    thinkingId.current = null;
    acceptedStatusRef.current = null;
    acceptedStatusAcceptedAtRef.current = null;
    acceptedStatusExpiryAtRef.current = null;
    setAcceptedStatusExpiryAt(null);
    controlTaskRef.current = null;
    taskAuthorityRef.current = createTransientStatusTaskAuthorityState();
    setStreamTaskOwner(null);
  }, []);

  const clearWorkflowProjection = useCallback(() => {
    setMessages([]);
    setStatus2("等待当前工作流活动任务权威");
    setIsWorking(false);
    openToolId.current = null;
    thinkingId.current = null;
    acceptedStatusRef.current = null;
    acceptedStatusAcceptedAtRef.current = null;
    acceptedStatusExpiryAtRef.current = null;
    setAcceptedStatusExpiryAt(null);
    controlTaskRef.current = null;
    taskAuthorityRef.current = loseTransientStatusTaskAuthority(
      taskAuthorityRef.current,
    );
    setStreamTaskOwner(null);
  }, []);

  useLayoutEffect(() => {
    clearEpochProjection();
  }, [clearEpochProjection, streamAuthorityKey]);

  useLayoutEffect(() => {
    // stream_authority_digest intentionally spans same-version successors.
    // The complete checkpoint identity is therefore an independent hard
    // fence: no v67 phrase/output may survive into v68 or revision R+1. Keep
    // the task lifecycle high-water so an old owner replay remains rejected.
    clearWorkflowProjection();
  }, [clearWorkflowProjection, workflowIdentityKey]);

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

  const loseTaskAuthority = useCallback(() => {
    taskAuthorityRef.current = loseTransientStatusTaskAuthority(taskAuthorityRef.current);
    controlTaskRef.current = null;
    acceptedStatusRef.current = null;
    acceptedStatusAcceptedAtRef.current = null;
    acceptedStatusExpiryAtRef.current = null;
    setAcceptedStatusExpiryAt(null);
    setStreamTaskOwner(null);
    setMessages([]);
    setStatus2("活动任务权威已失效");
    setIsWorking(false);
    openToolId.current = null;
    thinkingId.current = null;
  }, []);

  const invalidateAcceptedStatus = useCallback((task: TransientStatusTask | null) => {
    if (acceptedEvolutionStatusAllowsIO(
      acceptedStatusRef.current,
      acceptedStatusAcceptedAtRef.current,
      activeGenerationRef.current,
      task,
    )) return;
    acceptedStatusRef.current = null;
    acceptedStatusAcceptedAtRef.current = null;
    acceptedStatusExpiryAtRef.current = null;
    setAcceptedStatusExpiryAt(null);
    setIsWorking(false);
    setStatus2(transientStatusFallback(task));
  }, []);

  const acceptTransientStatus = useCallback((candidate: EvolutionStatusEvent): boolean => {
    const acceptedAt = Date.now() / 1000;
    if (
      !isFreshEvolutionStatusEvent(candidate, acceptedAt)
      || !shouldAcceptEvolutionStatus(
        candidate,
        activeGenerationRef.current,
        controlTaskRef.current,
        acceptedStatusRef.current,
      )
    ) return false;
    const expiryAt = evolutionStatusExpiryAt(candidate, acceptedAt);
    acceptedStatusRef.current = candidate;
    acceptedStatusAcceptedAtRef.current = acceptedAt;
    acceptedStatusExpiryAtRef.current = expiryAt;
    setAcceptedStatusExpiryAt(expiryAt);
    setStatus2(candidate.msg);
    setIsWorking(Boolean(candidate.is_working));
    return true;
  }, []);

  const expireAcceptedStatus = useCallback((expectedExpiryAt: number) => {
    if (acceptedStatusExpiryAtRef.current !== expectedExpiryAt) return;
    acceptedStatusRef.current = null;
    acceptedStatusAcceptedAtRef.current = null;
    acceptedStatusExpiryAtRef.current = null;
    setAcceptedStatusExpiryAt(null);
    setIsWorking(false);
    setStatus2(transientStatusFallback(controlTaskRef.current));
  }, []);

  const observeTaskProjection = useCallback((candidate: unknown): boolean => {
    const observed = observeTransientStatusTaskProjection(taskAuthorityRef.current, candidate);
    taskAuthorityRef.current = observed.state;
    if (!observed.accepted) {
      if (observed.reason === "invalid" || observed.reason === "conflict") loseTaskAuthority();
      return false;
    }
    const task = observed.state.current;
    if (!task) {
      loseTaskAuthority();
      return false;
    }
    const previous = controlTaskRef.current;
    if (
      previous
      && (
        previous.owner_id !== task.owner_id
        || previous.lifecycle_revision !== task.lifecycle_revision
      )
    ) {
      setMessages([]);
      openToolId.current = null;
      thinkingId.current = null;
      acceptedStatusRef.current = null;
      acceptedStatusAcceptedAtRef.current = null;
      acceptedStatusExpiryAtRef.current = null;
      setAcceptedStatusExpiryAt(null);
      setIsWorking(false);
    }
    controlTaskRef.current = task;
    setStreamTaskOwner(task);
    invalidateAcceptedStatus(task);
    if (
      task.present !== true
      || task.done !== false
      || task.shutdown_requested
      || task.status_eligible !== true
    ) {
      acceptedStatusRef.current = null;
      setIsWorking(false);
      setStatus2(task.shutdown_requested ? "正在安全停止" : "当前无活动模型任务");
    }
    return true;
  }, [invalidateAcceptedStatus, loseTaskAuthority]);

  useEffect(() => {
    if (acceptedStatusExpiryAt === null) return undefined;
    const waitMs = Math.max(
      0,
      Math.ceil((acceptedStatusExpiryAt - Date.now() / 1000) * 1000),
    );
    const timer = window.setTimeout(
      () => expireAcceptedStatus(acceptedStatusExpiryAt),
      waitMs,
    );
    return () => window.clearTimeout(timer);
  }, [acceptedStatusExpiryAt, expireAcceptedStatus]);

  useEffect(() => {
    if (!epochReady) return;
    observeTaskProjection(health?.task);
  }, [epochReady, health?.task, observeTaskProjection]);

  const connect = useEvolutionSSE({
    onStatus: (evt) => {
      if (!epochReady) return;
      acceptTransientStatus(evt);
    },
    onIO: (line: IOLine) => {
      const task = controlTaskRef.current;
      if (
        !epochReady
        || task?.present !== true
        || task.done !== false
        || task.shutdown_requested
        || task.status_eligible !== true
        || !acceptedEvolutionStatusAllowsIO(
          acceptedStatusRef.current,
          acceptedStatusAcceptedAtRef.current,
          activeGenerationRef.current,
          task,
        )
      ) return;
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
      const task = controlTaskRef.current;
      if (
        !epochReady
        || task?.present !== true
        || task.done !== false
        || task.shutdown_requested
        || task.status_eligible !== true
        || !acceptedEvolutionStatusAllowsIO(
          acceptedStatusRef.current,
          acceptedStatusAcceptedAtRef.current,
          activeGenerationRef.current,
          task,
        )
      ) return;
      closeTool();
      const id = nextId();
      openToolId.current = id;
      const role = data.role || undefined;
      addMsg({ id, type: "tool_call", text: data.tool_name, role, toolName: data.tool_name, toolArgs: data.args, toolOutput: [], toolDone: false });
    },
    onTaskOwner: (task) => { if (epochReady) observeTaskProjection(task); },
    onTaskAuthorityLost: () => { if (epochReady) loseTaskAuthority(); },
    onClearIO: () => {
      if (!epochReady) return;
      setMessages([]);
      openToolId.current = null;
      thinkingId.current = null;
    },
    onEpochBlocked: () => clearEpochProjection(),
    onConnect: () => {
      if (!epochReady) return;
      setMessages([]);
      acceptedStatusRef.current = null;
      acceptedStatusAcceptedAtRef.current = null;
      acceptedStatusExpiryAtRef.current = null;
      setAcceptedStatusExpiryAt(null);
      setStatus2("等待当前活动任务状态");
      setIsWorking(false);
      openToolId.current = null;
      thinkingId.current = null;
    },
    onDisconnect: () => loseTaskAuthority(),
    onHistory: () => {}, onHeader: () => {}, onCost: () => {}, onGenerationCostPolicy: () => {},
    onEvalTable: () => {}, onMetrics: () => {}, onDaemonStats: () => {},
  }, streamAuthorityKey);

  useEffect(() => {
    if (!epochReady) { clearEpochProjection(); return; }
    const disconnect = connect();
    return () => { disconnect(); };
  }, [clearEpochProjection, connect, epochReady]);

  useEffect(() => {
    if (autoScroll && ioRef.current) ioRef.current.scrollTop = ioRef.current.scrollHeight;
  }, [messages, autoScroll]);

  const view = agents ? agentActivityView(agents, health?.pipeline?.route) : null;
  const taskActive = streamTaskOwner?.present === true
    && streamTaskOwner.done === false
    && streamTaskOwner.shutdown_requested === false
    && streamTaskOwner.status_eligible === true;
  const authoritativeWorking = Boolean(epochReady && status?.running && taskActive && isWorking);

  return (
    <>
      <PageMeta title="研发协作 — Bot 自进化" description="本代各研发角色正在做什么" />
      <EpochAuthorityStatus status={status} loading={loading} error={error} className="mb-4" />
      <OperatorSituation status={status} health={health} className="mb-4" />

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* Structured agent roles */}
        <div className="lg:col-span-1 space-y-4">
          <Card>
            <CardHeader title="谁正在做什么" subtitle="只显示当前这次研发任务绑定的角色" />
            <div className="p-3 space-y-2">
              {!epochReady && <p className="text-xs text-gray-400">严格进化尚未初始化，当前没有研发角色。</p>}
              {epochReady && !view && <p className="text-xs text-gray-400">当前没有可验证的研发工作流。</p>}
              {view && !view.available && (
                <p className="text-xs text-gray-400">研发活动暂不可用：{view.reason}</p>
              )}
              {view && view.available && (
                <>
                  <div className="text-xs text-gray-500 mb-1">
                    当前内部阶段：<span className="font-mono text-gray-800 dark:text-gray-200">{view.stage ?? "(无)"}</span>
                    {view.stageIsTimeoutLease && <Badge variant="error" size="sm" className="ml-2">超时恢复</Badge>}
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
                      <p className="text-xs font-semibold text-gray-600 dark:text-gray-300 mb-1">实现任务</p>
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
                      <p className="text-xs font-semibold text-error-600 dark:text-error-400 mb-1">本工作流历史失败记录（不代表当前仍失败）</p>
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
              <span className="text-xs font-medium text-gray-400">模型执行输出</span>
              <Badge variant={authoritativeWorking ? "success" : "neutral"} size="sm" pulse={authoritativeWorking}>
                {!epochReady ? "等待初始化" : authoritativeWorking ? "模型正在输出" : status2 === "连接中..." ? "连接中" : taskActive ? "等待下一次输出" : "当前无任务"}
              </Badge>
              {!authoritativeWorking && epochReady && status?.running && taskActive && (
                <span className="text-xs text-warning-500">编排器任务仍在；可能处于局部重试、阶段切换或等待模型</span>
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
              <EmptyState message="严格进化尚未初始化；不会把旧模型输出混入当前代次。" />
            )}
            {epochReady && messages.length === 0 && (
              <EmptyState message={taskActive ? "当前没有新的模型输出；请以上方状态机的下一步为准。" : "当前没有活跃模型任务。"} />
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
