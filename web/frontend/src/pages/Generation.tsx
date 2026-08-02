import { useEffect, useLayoutEffect, useRef, useState, useCallback } from "react";
import { useEvolutionSSE } from "../api/evolution";
import type { IOLine } from "../api/evolution";
import { useControlStatusValue } from "../context/DataProvider";
import { useBoundAgentActivity } from "../hooks/useBoundAgentActivity";
import { usePipelineCheckpoint } from "../hooks/usePipelineCheckpoint";
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
import {
  controlPipelineBlocked,
  controlPipelineIssues,
  draftGenerations,
} from "../api/control";
import {
  PIPELINE_TIMEOUT_LEASES,
  isPipelineTimeoutLeaseStage,
} from "../constants/pipeline";
import PageMeta from "../components/common/PageMeta";
import { EmptyState } from "../components/shared/EmptyState";
import { EvolutionPageScaffold } from "../components/evolution/EvolutionPageScaffold";
import { HandoffEightStep } from "../components/evolution/HandoffEightStep";
import { PipelineDiagnostics } from "../components/evolution/PipelineDiagnostics";
import { PipelineStatus } from "../components/evolution/PipelineStatus";
import { OfficialCertificationProgress } from "../components/evolution/OfficialCertificationProgress";
import { EvolutionStreamPanel } from "../components/evolution/EvolutionStreamPanel";
import { ToolCard, ThinkingBlock } from "../components/evolution/ToolCard";
import type { ConvMsg } from "../components/evolution/ToolCard";
import { CopyIcon, CrossIcon } from "../components/evolution/icons";
import { agentActivityView, type AgentRoleSummary } from "../domain/agentActivityView";
import { pipelineRecoveryRows } from "../domain/failureRecoveryView";
import { cn } from "../lib/utils";
import {
  EvolutionSection,
  EvolutionStatusBadge,
  EvolutionSurface,
} from "../components/evolution/ui";
import type { EvolutionStatusTone } from "../components/evolution/ui";

let _msgId = 0;
const nextId = () => ++_msgId + Date.now();

const ROLE_TONE: Record<AgentRoleSummary["state"], { tone: EvolutionStatusTone; dot: string }> = {
  running: { tone: "info", dot: "bg-brand-400" },
  terminal: { tone: "ok", dot: "bg-success-400" },
  not_reached: { tone: "neutral", dot: "bg-gray-400" },
  unknown: { tone: "warn", dot: "bg-warning-400" },
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
 * 当代进度页：合并原 PipelineMap（完整 stepper + 交接八步 + 诊断）与
 * AgentActivity（研发角色 + 实时 LLM 输入输出流），并加入国赛 EXE 认证进度。
 *
 * - 完整 stepper：prepare→master→workers→quality→review→critic→precommit→commit→publish
 * - 国赛 EXE 认证进度（轮次/手数/通过率）
 * - LLM 实时输入输出流
 * - handoff 八步
 */
export default function Generation() {
  const { status, health } = useControlStatusValue();
  const streamAuthorityKey = epochStreamAuthorityKey(status);
  const epochReady = streamAuthorityKey !== null;
  const { checkpoint } = usePipelineCheckpoint(5_000);
  const { agents } = useBoundAgentActivity(
    status?.active_generation,
    epochReady,
  );

  // ── LLM 实时输入输出流（来自原 AgentActivity 的 SSE 流逻辑）──
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

  // ── Stepper / 诊断派生（来自原 PipelineMap）──
  const pipeline = health?.pipeline ?? null;
  const blocked = controlPipelineBlocked(pipeline);
  const issues = controlPipelineIssues(pipeline);
  const activeStage = status?.active_generation?.stage ?? null;
  const isTimeout = activeStage != null && isPipelineTimeoutLeaseStage(activeStage);
  const agentView = agents ? agentActivityView(agents, pipeline?.route) : null;
  const recoveryRows = pipelineRecoveryRows(
    pipeline,
    agentView && agentView.available ? agentView.infraFailure : null,
  );
  const route = pipeline?.route ?? null;
  const handoff = status?.post_publication_handoff ?? null;

  const view = agents ? agentActivityView(agents, health?.pipeline?.route) : null;
  const taskActive = streamTaskOwner?.present === true
    && streamTaskOwner.done === false
    && streamTaskOwner.shutdown_requested === false
    && streamTaskOwner.status_eligible === true;
  const authoritativeWorking = Boolean(epochReady && status?.running && taskActive && isWorking);
  const runFlagWithoutTask = Boolean(status?.running && !taskActive);
  const streamInterrupted = epochReady && !authoritativeWorking && status2 === "连接中..." && messages.length === 0;

  return (
    <EvolutionPageScaffold title="当代进度">
      <PageMeta title="当代进度 — Bot 自进化" description="当前 Bot 从研发到发布的真实进度" />

      <EvolutionSurface>
        <EvolutionSection title="本代从研发到发布的进度" />
        <div className="mt-3">
          <PipelineStatus
            checkpoint={checkpoint}
            activeGeneration={status?.active_generation ?? null}
            drafts={draftGenerations(status)}
            pipelineMode={status?.pipeline_mode ?? null}
            route={route}
            handoff={handoff}
          />
        </div>
      </EvolutionSurface>

      {/* 国赛 EXE 认证进度（轮次/手数/通过率）— 组件自带唯一轮询 */}
      <OfficialCertificationProgress status={status} className="mt-4" />

      <HandoffEightStep handoff={handoff} className="mt-4" />

      <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-3">
        {/* 研发角色 + 异常处理（左） */}
        <div className="space-y-4 lg:col-span-1">
          <EvolutionSurface padding="sm">
            <EvolutionSection title="谁正在做什么" />
            <div className="mt-3 space-y-2">
              {!epochReady && <p className="text-xs text-gray-400">严格进化尚未初始化，当前没有研发角色。</p>}
              {epochReady && !view && <p className="text-xs text-gray-400">当前没有可验证的研发工作流。</p>}
              {view && !view.available && (
                <p className="text-xs text-gray-400">研发活动暂不可用：{view.reason}</p>
              )}
              {view && view.available && (
                <>
                  <div className="mb-1 text-xs text-gray-500">
                    当前内部阶段：<span className="font-mono text-gray-800 dark:text-gray-200">{view.stage ?? "(无)"}</span>
                    {view.stageIsTimeoutLease && (
                      <EvolutionStatusBadge tone="error" className="ml-2">超时恢复</EvolutionStatusBadge>
                    )}
                  </div>
                  {view.roles.map((role) => {
                    const tone = ROLE_TONE[role.state];
                    return (
                      <div key={role.role} className="rounded-md border border-gray-100 p-2 dark:border-gray-800">
                        <div className="flex items-center gap-2">
                          <span className={cn("inline-block h-1.5 w-1.5 rounded-full", tone.dot, role.state === "running" && authoritativeWorking && "animate-pulse")} />
                          <span className="text-xs font-semibold text-gray-700 dark:text-gray-200">{role.label}</span>
                          <EvolutionStatusBadge tone={tone.tone} className="ml-auto">
                            {role.state === "running" ? "运行中" : role.state === "terminal" ? "已完成" : role.state === "not_reached" ? "未到达" : "未知"}
                          </EvolutionStatusBadge>
                        </div>
                        <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">{role.detail}</p>
                      </div>
                    );
                  })}
                  {view.master.tasks.length > 0 && (
                    <div className="mt-2 border-t border-gray-100 pt-2 dark:border-gray-800">
                      <p className="mb-1 text-xs font-semibold text-gray-600 dark:text-gray-300">实现任务</p>
                      {view.master.tasks.map((t, i) => (
                        <div key={i} className="mb-1 border-l-2 border-brand-300 pl-2 text-xs text-gray-600 dark:text-gray-400">
                          <span className="font-medium">#{t.worker_id} {t.role}</span>
                          {t.skill_layer && <span className="ml-1 text-gray-400">· {t.skill_layer}</span>}
                          {t.difficulty && <span className="ml-1 rounded bg-gray-100 px-1 text-gray-500 dark:bg-gray-800">{t.difficulty}</span>}
                        </div>
                      ))}
                    </div>
                  )}
                  {view.workerFailures.length > 0 && (
                    <div className="mt-2 border-t border-gray-100 pt-2 dark:border-gray-800">
                      <p className="mb-1 text-xs font-semibold text-error-600 dark:text-error-400">本工作流历史失败记录（不代表当前仍失败）</p>
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
          </EvolutionSurface>

          <PipelineDiagnostics status={status} health={health} />

          <EvolutionSurface padding="sm">
            <EvolutionSection title="当前异常如何处理" />
            <div className="mt-3 space-y-2">
              {isTimeout && activeStage && (
                <TimeoutLeaseCard stage={activeStage} />
              )}
              {issues.length === 0 && recoveryRows.length === 0 ? (
                <p className="text-xs text-success-600 dark:text-success-400">当前没有阻断流程的异常。</p>
              ) : (
                <>
                  {issues.length > 0 && (
                    <ul className="ml-4 list-disc text-xs text-gray-600 dark:text-gray-300">
                      {issues.map((issue) => (
                        <li key={issue} className="font-mono">{issue}</li>
                      ))}
                    </ul>
                  )}
                  {recoveryRows.map((row) => (
                    <div
                      key={row.key}
                      className="border-l-2 border-error-300 pl-2 text-xs dark:border-error-700"
                    >
                      <div className="flex items-center gap-2">
                        <EvolutionStatusBadge
                          tone={
                            row.disposition === "terminal"
                              ? "error"
                              : row.disposition === "auto_retry"
                                ? "warn"
                                : "neutral"
                          }
                        >
                          {row.failureClass}
                        </EvolutionStatusBadge>
                        <span className="truncate text-gray-600 dark:text-gray-300">{row.detail}</span>
                      </div>
                      <p className="mt-0.5 text-gray-500">{row.dispositionLabel}</p>
                    </div>
                  ))}
                </>
              )}
              {blocked && (
                <EvolutionStatusBadge tone="error">下一动作被安全阻断</EvolutionStatusBadge>
              )}
            </div>
          </EvolutionSurface>
        </div>

        {/* LLM 实时输入输出流（右） */}
        <div className="lg:col-span-2">
          <EvolutionStreamPanel
            connected={epochReady && status2 !== "连接中..."}
            statusText={
              !epochReady
                ? "等待初始化"
                : authoritativeWorking
                  ? "模型正在输出"
                  : runFlagWithoutTask
                    ? "运行标志存在但任务未活动"
                    : streamInterrupted
                      ? "状态未知（流中断）"
                      : status2 === "连接中..."
                        ? "连接中"
                        : taskActive
                          ? "等待下一次输出"
                          : "当前无任务"
            }
            isWorking={authoritativeWorking}
            actions={
              <>
                <button
                  onClick={() => {
                    navigator.clipboard.writeText(messages.map((m) => m.type === "tool_call" ? `[tool: ${m.toolName}]` : m.type === "thinking" ? `[thinking] ${m.text}` : m.text).join("\n")).catch(() => {});
                  }}
                  title="复制"
                  className="rounded p-1 text-gray-500 hover:bg-gray-800 hover:text-gray-300"
                >
                  <CopyIcon />
                </button>
                <button
                  onClick={() => setAutoScroll(!autoScroll)}
                  className={cn("rounded px-2 py-1 text-[10px]", autoScroll ? "bg-brand-500/20 text-brand-400" : "text-gray-500 hover:text-gray-300")}
                >
                  {autoScroll ? "自动滚动:开" : "自动滚动:关"}
                </button>
              </>
            }
            bodyClassName="h-[500px] overflow-y-auto custom-scrollbar"
          >
            <div ref={ioRef}>
              {!epochReady && (
                <EmptyState message="未初始化" />
              )}
              {epochReady && messages.length === 0 && (
                <EmptyState message={taskActive ? "无活跃任务" : "无活跃任务"} />
              )}
              {messages.map((msg) => (
                <div key={msg.id}>
                  {msg.slot && (
                    <span className={cn(
                      "mb-0.5 inline-block rounded-md px-1.5 py-0.5 text-[9px] font-medium",
                      msg.slot === "primary"
                        ? "bg-brand-900/40 text-brand-300"
                        : "bg-violet-900/40 text-violet-300",
                    )}>
                      {msg.slot}
                    </span>
                  )}
                  {msg.type === "tool_call" ? <ToolCard msg={msg} />
                    : msg.type === "thinking" ? <ThinkingBlock text={msg.text} done={msg.toolDone} />
                    : msg.type === "error" ? (
                      <div className="my-0.5 rounded border-l-2 border-red-500 bg-red-950/40 px-2 py-0.5 font-medium text-red-400">
                        <CrossIcon className="mr-1 inline h-3 w-3" /> {msg.text}
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
              {authoritativeWorking && <span className="ml-1 inline-block h-4 w-2 animate-cursor-blink bg-indigo-400" />}
            </div>
          </EvolutionStreamPanel>
        </div>
      </div>
    </EvolutionPageScaffold>
  );
}

function TimeoutLeaseCard({ stage }: { stage: "timed_out" | "infra_timed_out" }) {
  const lease = PIPELINE_TIMEOUT_LEASES[stage];
  return (
    <div className="rounded-md border border-error-300 bg-error-50 p-3 dark:border-error-800 dark:bg-error-950/30">
      <div className="mb-1 flex items-center gap-2">
        <EvolutionStatusBadge tone="error">超时恢复</EvolutionStatusBadge>
        <span className="text-sm font-semibold text-error-700 dark:text-error-300">{lease.label}</span>
        <span className="font-mono text-xs text-gray-500">{stage}</span>
      </div>
      <p className="mb-2 text-xs text-error-700 dark:text-error-300">{lease.description}</p>
      <div className="mt-1 font-mono text-xs text-gray-500">
        stage: {stage} · next_tool: {lease.nextTool}
      </div>
    </div>
  );
}
