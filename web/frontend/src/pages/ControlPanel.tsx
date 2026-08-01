import { useState, useCallback } from "react";
import {
  controlApi,
  controlAbandonAvailable,
  controlPipelineBlocked,
  controlPipelineIssues,
  controlPipelineRouteAllowed,
  controlSchedulerOwnsPrepareBoundary,
  controlStartBlocked,
  controlStartBlockedReason,
  type Decision,
  type AppConfig,
} from "../api/control";
import { api } from "../api/client";
import type { PipelineCheckpoint } from "../api/types";
import { EpochAuthorityStatus } from "../components/evolution/EpochAuthorityStatus";
import { StabilityStatus } from "../components/evolution/StabilityStatus";
import { AsyncCertificationQueue } from "../components/evolution/AsyncCertificationQueue";
import { EvolutionPageScaffold } from "../components/evolution/EvolutionPageScaffold";
import { authorityNextVersion } from "../hooks/useControlStatus";
import { useBoundPolling } from "../hooks/useBoundPolling";
import { useControlStatusValue } from "../context/DataProvider";
import { getOperatorControlToken, setOperatorControlToken } from "../api/operatorControl";
import { controlTaskActive, controlTaskStopping } from "../lib/controlRuntimeState";
import { canonicalGenerationLabel } from "../lib/canonicalGenerationIdentity";


// ── Inline SVG helpers ─────────────────────────────────────────────────────────
const RefreshIcon = ({ className }: { className?: string }) => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className}><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
);

interface ControlConfigProjection {
  decisions: Decision[];
  config: AppConfig | null;
}

// ── Main ───────────────────────────────────────────────────────────────────────

export default function ControlPanel() {
  const { status, health, loading: statusLoading, error: statusError, refresh: refreshStatus, lastUpdated } = useControlStatusValue();
  const [loading, setLoading] = useState<string | null>(null);
  const [editWorkers, setEditWorkers] = useState(12);
  const [editPairs, setEditPairs] = useState(5);
  const [editDaemon, setEditDaemon] = useState(true);
  const [operatorToken, setOperatorToken] = useState(getOperatorControlToken);
  const [mutationError, setMutationError] = useState("");
  const [configSynced, setConfigSynced] = useState(false);

  // config + decisions：用 useBoundPolling 统一轮询（替换原 setInterval(3s)）。
  const {
    data: configProjection,
    error: configError,
    refresh: refreshConfig,
  } = useBoundPolling<ControlConfigProjection>(
    async () => {
      const [decisions, config] = await Promise.all([
        controlApi.decisions(),
        controlApi.getConfig(),
      ]);
      return { decisions, config };
    },
    { pollMs: 3_000 },
  );
  // checkpoint：独立轮询（替换原 setInterval(5s)）。
  const {
    data: checkpoint,
    refresh: refreshCheckpoint,
  } = useBoundPolling<PipelineCheckpoint | null>(
    async () => api.pipelineCheckpoint(),
    { pollMs: 5_000 },
  );

  const decisions = configProjection?.decisions ?? [];
  const config = configProjection?.config ?? null;
  const connError = configError != null;

  // 将后端 config 同步进编辑态（首次拿到 config 后对齐一次）。
  const configReady = config != null;
  if (configReady && !configSynced) {
    setEditWorkers(config.daemon_workers);
    setEditPairs(config.daemon_pairs);
    setEditDaemon(config.daemon_enabled);
    setConfigSynced(true);
  }

  const updateOperatorToken = (value: string) => {
    setOperatorToken(value);
    setOperatorControlToken(value);
    setMutationError("");
  };

  const refresh = useCallback(() => {
    void refreshConfig();
  }, [refreshConfig]);

  const handleSaveConfig = async () => {
    if (status?.running || (health?.task.present && health.task.done === false)) return;
    setLoading("config");
    setMutationError("");
    try {
      await controlApi.setConfig({ daemon_enabled: editDaemon, daemon_workers: editWorkers, daemon_pairs: editPairs });
    } catch (error) {
      setMutationError(error instanceof Error ? error.message : String(error));
    } finally { setLoading(null); }
    try { await refreshConfig(); } catch (e) { console.error("[ControlPanel] refresh after save failed:", e); }
  };

  const handleStart = async () => {
    if (!config || controlStartBlocked(status, health)) return;
    setLoading("start");
    setMutationError("");
    try { await controlApi.start(); }
    catch (error) { setMutationError(error instanceof Error ? error.message : String(error)); }
    finally { setLoading(null); }
    await Promise.all([refreshConfig(), refreshStatus()]);
  };

  const handleStop = async () => {
    setLoading("stop");
    setMutationError("");
    try { await controlApi.stop(); }
    catch (error) { setMutationError(error instanceof Error ? error.message : String(error)); }
    finally { setLoading(null); }
    await Promise.all([refreshConfig(), refreshStatus()]);
  };

  const handleAbandon = async () => {
    if (!controlAbandonAvailable(status)) return;
    const identity = status?.active_generation
      ? `v${status.active_generation.next_v} @ ${status.active_generation.stage}`
      : "当前活跃代次";
    const confirmed = window.confirm(
      `确认受控放弃 ${identity}？\n将先停止编排器，再执行权威 abandon；完成后服务保持停止，需手动重新启动。`,
    );
    if (!confirmed) return;
    setLoading("abandon");
    setMutationError("");
    try {
      await controlApi.abandon({ reason: "operator_control_panel_abandon" });
    } catch (error) {
      setMutationError(error instanceof Error ? error.message : String(error));
    } finally {
      setLoading(null);
    }
    await Promise.all([refreshConfig(), refreshStatus(), refreshCheckpoint()]);
  };

  const formatTime = (ts: number) => new Date(ts * 1000).toLocaleTimeString();

  const configDirty = config && configSynced && (
    editWorkers !== config.daemon_workers ||
    editPairs !== config.daemon_pairs ||
    editDaemon !== config.daemon_enabled
  );

  const taskActive = controlTaskActive(health?.task);
  const taskStopping = controlTaskStopping(health?.task);
  const daemonConfigAligned = health?.daemon.configured != null
    && health.daemon.configured === status?.daemon_enabled;
  const authoritativeRunning = Boolean(
    status?.epoch_initialized
    && status.mode === "orchestrator"
    && status.running
    && health?.overall === "healthy"
    && health.running === true
    && taskActive
    && !taskStopping
    && daemonConfigAligned,
  );
  const degradedRunning = Boolean(status?.running && !authoritativeRunning);
  const orphanTask = Boolean(taskActive && !status?.running);
  const runtimeMutationLocked = Boolean(status?.running || taskActive);
  const authorityTarget = authorityNextVersion(status);
  const activeIdentityLabel = status?.active_generation
    ? canonicalGenerationLabel(status.active_generation, status.active_generation.next_v)
    : null;
  const pipeline = health?.pipeline;
  const route = pipeline?.route ?? null;
  const handoff = status?.post_publication_handoff;
  const pipelineBlocked = controlPipelineBlocked(pipeline);
  const pipelineIssues = controlPipelineIssues(pipeline);
  const schedulerOwnsPrepare = controlSchedulerOwnsPrepareBoundary(status, health);
  const startBlockedReason = !config
    ? "运行配置不可用，暂不能启动"
    : controlStartBlockedReason(status, health);
  const startBlocked = startBlockedReason !== null;
  const routeMatchesGeneration = Boolean(
    route
    && status?.active_generation
    && controlPipelineRouteAllowed(pipeline)
    && pipeline?.authority === "strict_epoch_projection"
    && pipeline.next_v === status.active_generation.next_v
    && pipeline.source_v === status.active_generation.source_v
    && pipeline.stage === status.active_generation.stage
    && pipeline.run_id === status.active_generation.run_id
    && pipeline.workflow_run_id === status.active_generation.workflow_run_id
    && pipeline.checkpoint_revision === status.active_generation.checkpoint_revision
    && route.stage === status.active_generation.stage
    && route.next_v === status.active_generation.next_v
    && route.source_v === status.active_generation.source_v
    && route.parent2_v === status.active_generation.parent2_v
    && (route.next_tool == null || typeof route.next_tool === "string")
    && Array.isArray(route.allowed_tools)
    && route.allowed_tools.every((tool) => typeof tool === "string")
    && (route.next_tool == null || route.allowed_tools.includes(route.next_tool))
    && typeof route.intent === "string"
    && route.intent.trim().length > 0
    && typeof route.directive === "string"
    && route.directive.trim().length > 0,
  );
  const routeMatchesHandoff = Boolean(
    route
    && handoff
    && handoff.status !== "none"
    && controlPipelineRouteAllowed(pipeline)
    && pipeline?.authority === "post_publication_handoff_journal"
    && pipeline.handoff_projection_digest
      === handoff.projection_digest
    && pipeline.handoff_identity_digest
      === handoff.identity_digest
    && pipeline.handoff_owner_scope
      === handoff.owner_scope
    && route.stage === "post_publication_handoff"
    && route.next_v === handoff.version
    && route.source_v === handoff.source_v
    && route.parent2_v == null
    && route.next_tool === "run_archivist"
    && route.allowed_tools.length === 1
    && route.allowed_tools[0] === "run_archivist"
    && route.intent === "post_publication_handoff"
    && typeof route.directive === "string"
    && route.directive.trim().length > 0,
  );
  const abandonAvailable = controlAbandonAvailable(status);
  const asyncCert = status?.async_certification;
  const daemonEffective = health?.daemon;

  return (
    <EvolutionPageScaffold
      title="控制面板"
      subtitle="启停 / 受控放弃 / daemon 配置 / epoch 权威 / 异步认证队列"
    >
      <div className="space-y-6">
      <div className="flex items-center justify-end gap-3 -mb-2">
        <button onClick={() => void Promise.all([refresh(), refreshStatus(), refreshCheckpoint()])} className="px-3 py-1 text-sm rounded bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 flex items-center gap-1 shrink-0">
          <RefreshIcon /> 刷新
        </button>
      </div>

      <EpochAuthorityStatus status={status} loading={statusLoading} error={statusError} lastUpdated={lastUpdated} />

      <div className="rounded-lg border border-gray-200 dark:border-border-subtle bg-white dark:bg-surface-1 p-4">
        <h2 className="text-sm font-semibold text-gray-800 dark:text-white">操作员授权</h2>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <input
            aria-label="操作员控制令牌"
            type="password"
            autoComplete="off"
            value={operatorToken}
            onChange={(event) => updateOperatorToken(event.target.value)}
            placeholder="远程访问时输入 POK_CONTROL_TOKEN"
            className="h-9 min-w-72 flex-1 rounded border border-gray-300 px-3 text-sm dark:border-gray-600 dark:bg-gray-800 dark:text-white"
          />
          <button
            type="button"
            onClick={() => updateOperatorToken("")}
            disabled={!operatorToken}
            className="h-9 rounded bg-gray-200 px-3 text-sm text-gray-700 hover:bg-gray-300 disabled:opacity-40 dark:bg-gray-700 dark:text-gray-200"
          >
            清除
          </button>
        </div>
        <p className="mt-2 text-xs text-gray-500">令牌只保存在当前页面进程内存中，刷新页面即清除；本机同源访问无需令牌。</p>
        {mutationError && <p className="mt-2 text-xs text-red-600">操作失败：{mutationError}</p>}
      </div>

      {/* Status Bar */}
      <div className="rounded-lg border border-gray-200 dark:border-border-subtle bg-white dark:bg-surface-1 p-4">
        <div className="flex flex-wrap items-center gap-4">
          <div className="flex items-center gap-2">
            <span className="text-sm text-gray-500">模式:</span>
            <span className="text-sm font-medium text-gray-700 dark:text-gray-300">{status?.mode ?? "权威投影不可用"}</span>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-sm text-gray-500">状态:</span>
            <span className={`inline-flex items-center gap-1.5 text-sm font-medium ${authoritativeRunning ? "text-green-600" : degradedRunning || orphanTask || statusError ? "text-red-600" : "text-gray-400"}`}>
              <span className={`w-2 h-2 rounded-full ${authoritativeRunning ? "bg-green-500 animate-pulse" : degradedRunning || orphanTask || statusError ? "bg-red-500" : "bg-gray-400"}`} />
              {authoritativeRunning
                ? "运行中（任务与健康均已确认）"
                : taskStopping
                  ? "停止中（任务仍持有运行权威）"
                : orphanTask
                  ? "异常：running=false 但编排器任务仍活动"
                : degradedRunning
                  ? `运行标志异常（${health?.issues.join("、") || "健康投影不可用"}）`
                  : statusError ? "控制状态不可用" : "已停止"}
            </span>
            {connError && <span className="text-xs text-red-500">连接失败</span>}
          </div>
          {status && (
            <div className="text-sm text-gray-500">
              严格代次 {status.strict_generation_count} | 数字权威 v{status.version_authority_high_water} → 目标 {authorityTarget != null ? `v${authorityTarget}` : "待恢复"}
            </div>
          )}
          <div className="flex gap-2 ml-auto">
            {!status?.running && !taskActive ? (
              <button
                onClick={handleStart}
                disabled={loading === "start" || startBlocked}
                title={startBlockedReason ?? undefined}
                className="px-4 py-1.5 text-sm rounded bg-green-600 text-white hover:bg-green-700 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {loading === "start" ? "启动中..." : "启动"}
              </button>
            ) : (
              <button onClick={handleStop} disabled={loading === "stop" || taskStopping} className="px-4 py-1.5 text-sm rounded bg-red-600 text-white hover:bg-red-700 disabled:opacity-50">
                {loading === "stop" || taskStopping ? "停止中..." : "停止"}
              </button>
            )}
            {abandonAvailable && (
              <button
                id="abandon"
                onClick={handleAbandon}
                disabled={loading === "abandon" || loading === "stop" || taskStopping}
                title="停止编排器并受控放弃当前活跃代次；完成后保持停止"
                className="px-4 py-1.5 text-sm rounded border border-amber-500 bg-amber-50 text-amber-800 hover:bg-amber-100 disabled:cursor-not-allowed disabled:opacity-40 dark:border-amber-700 dark:bg-amber-950/30 dark:text-amber-200"
              >
                {loading === "abandon" ? "放弃中..." : "受控放弃"}
              </button>
            )}
          </div>
        </div>
      </div>

      {status && (
        <div className="rounded-lg border border-gray-200 bg-white p-4 dark:border-border-subtle dark:bg-surface-1">
          <div className="flex flex-wrap items-start gap-x-6 gap-y-3">
            <div>
              <p className="mb-1 text-xs font-semibold uppercase text-gray-500">连续稳定性权威</p>
              <StabilityStatus observation={status.stability_observation} />
            </div>
            <div className="min-w-0 flex-1">
              <p className="mb-1 text-xs font-semibold uppercase text-gray-500">确定性下一步</p>
              {(routeMatchesGeneration || routeMatchesHandoff) && route ? (
                <div className="space-y-1 text-xs text-gray-600 dark:text-gray-300">
                  <p>
                    <span className="font-mono">{route.stage}</span> → 工具 <b className="font-mono">{route.next_tool || "无自动工具"}</b>
                    {route.parent2_v != null ? ` · crossover parent2_v=v${route.parent2_v}` : ""}
                    {route.failure_class ? ` · failure=${route.failure_class}` : ""}
                  </p>
                  <p>意图：{route.intent}</p>
                  <p>指令：{route.directive}</p>
                  <p className="font-mono text-[10px] text-gray-400">允许工具：{route.allowed_tools.length > 0 ? route.allowed_tools.join(", ") : "[]"}</p>
                </div>
              ) : status.post_publication_handoff.status === "blocked" ? (
                <p className="text-xs text-red-600 dark:text-red-300">
                  发布后交接已阻断；不会从旧 checkpoint 或阶段名称猜测下一工具。
                </p>
              ) : pipelineBlocked ? (
                <p className="text-xs text-red-600 dark:text-red-300">
                  流水线恢复已阻断；不会显示或执行 checkpoint route。
                  {pipelineIssues.length > 0 ? ` ${pipelineIssues.join("、")}` : ""}
                </p>
              ) : schedulerOwnsPrepare ? (
                <p className="text-xs text-amber-700 dark:text-amber-300">
                  外层 generation scheduler 持有无 checkpoint 边界；下一动作是系统非 MCP
                  <span className="font-mono"> prepare_generation</span>，不是可由页面调用的流水线工具。
                </p>
              ) : !status.active_generation && status.post_publication_handoff.status === "none" ? (
                <p className="text-xs text-gray-500">当前没有活跃代次或发布后交接；运行已停止或调度权威不可用。</p>
              ) : (
                <p className="text-xs text-red-600 dark:text-red-300">
                  权威 route 不可用或与 active_generation 不一致；页面不从 stage 猜测下一工具。
                </p>
              )}
            </div>
          </div>
        </div>
      )}

      <AsyncCertificationQueue projection={asyncCert} />

      {/* Provider-history recovery boundary */}
      <div className="rounded-lg border border-gray-200 dark:border-border-subtle bg-white dark:bg-surface-1 p-4">
        <h2 className="text-lg font-semibold text-gray-800 dark:text-white mb-3">LLM 恢复边界</h2>
        <div className="flex flex-wrap items-center gap-4">
          <div>
            <span className="text-xs text-gray-500 block mb-1">Provider history</span>
            <span className="text-sm text-gray-800 dark:text-gray-200">禁止持久化与恢复</span>
            <p className="mt-1 max-w-xl text-xs text-gray-500">
              重启只从已验证 checkpoint 重建确定性上下文，并创建全新的 provider stream；opaque session ID 不构成恢复权威。
            </p>
          </div>
          {status?.active_generation && (
            <div>
              <span className="text-xs text-gray-500 block mb-1">流程阶段</span>
              <span className="px-2 py-0.5 rounded bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300 text-xs font-medium">
                {activeIdentityLabel ?? "双身份投影不可用"}
                {status.active_generation.source_v != null ? ` · source_v=v${status.active_generation.source_v}` : ""}: {status.active_generation.stage}
              </span>
              {!checkpoint && <p className="mt-1 text-[10px] text-amber-600">详细 checkpoint 暂不可用</p>}
            </div>
          )}
        </div>
      </div>

      {/* Settings */}
      <div className="rounded-lg border border-gray-200 dark:border-border-subtle bg-white dark:bg-surface-1 p-4">
        <h2 className="text-lg font-semibold text-gray-800 dark:text-white mb-3">评分引擎设置</h2>
        <p className="mb-3 text-xs text-gray-500">
          配置意图：{config ? (config.daemon_enabled ? "启用" : "禁用") : "不可用"}
          {" · "}实际进程：{health?.daemon.alive == null ? "不可用" : health.daemon.alive ? "运行" : "停止"}
          {health?.daemon.process_identity ? ` · 进程身份：${health.daemon.process_identity}` : ""}
          {" · "}心跳：{health?.daemon.heartbeat_status ?? "不可用"}
          {health?.daemon.health_error ? ` · health_error=${health.daemon.health_error}` : ""}
        </p>
        {(daemonEffective?.effective_pairs != null
          || daemonEffective?.configured_pairs != null
          || daemonEffective?.pairs_drift != null) && (
          <p className={`mb-3 text-xs ${daemonEffective.pairs_drift ? "text-amber-700 dark:text-amber-300" : "text-gray-500"}`}>
            配对投影：配置 {daemonEffective.configured_pairs ?? "—"}
            {daemonEffective.env_pairs != null ? ` · env ${daemonEffective.env_pairs}` : ""}
            {daemonEffective.effective_pairs != null ? ` · 进程生效 ${daemonEffective.effective_pairs}` : " · 进程生效不可用"}
            {daemonEffective.effective_workers != null ? ` · 生效 workers ${daemonEffective.effective_workers}` : ""}
            {daemonEffective.pairs_drift ? " · 检测到 pairs_drift" : ""}
          </p>
        )}
        <p className="mb-3 text-xs text-gray-500">
          每次配对数是写入 evaluation identity 的完整 70 手样本预算（1–8），只影响评分周期的采样量与吞吐；它本身不是 Bot 强度证明。
        </p>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <div className="flex items-center gap-3">
            <label className="text-sm text-gray-600 dark:text-gray-300">评分引擎</label>
            <button
              role="switch"
              aria-checked={editDaemon}
              disabled={!config || !status?.epoch_initialized || runtimeMutationLocked}
              onClick={() => setEditDaemon(!editDaemon)}
              className={`relative inline-flex h-6 w-11 rounded-full border-2 border-transparent transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${editDaemon ? "bg-blue-600" : "bg-gray-300 dark:bg-gray-600"}`}
            >
              <span className={`inline-block h-5 w-5 transform rounded-full bg-white shadow transition-transform ${editDaemon ? "translate-x-5" : "translate-x-0"}`} />
            </button>
          </div>
          <div className="flex items-center gap-2">
            <label className="text-sm text-gray-600 dark:text-gray-300 whitespace-nowrap">Worker</label>
            <input type="number" min={1} max={12} value={editWorkers} onChange={(e) => setEditWorkers(Math.max(1, Math.min(12, Number(e.target.value) || 1)))} disabled={!config || !editDaemon || !status?.epoch_initialized || runtimeMutationLocked} className="w-20 px-2 py-1 text-sm rounded border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white disabled:opacity-40" />
          </div>
          <div className="flex items-center gap-2">
            <label className="text-sm text-gray-600 dark:text-gray-300 whitespace-nowrap">每次配对数</label>
            <input type="number" min={1} max={8} value={editPairs} onChange={(e) => setEditPairs(Math.max(1, Math.min(8, Number(e.target.value) || 1)))} disabled={!config || !editDaemon || !status?.epoch_initialized || runtimeMutationLocked} className="w-20 px-2 py-1 text-sm rounded border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white disabled:opacity-40" />
          </div>
        </div>
        <div className="mt-3 flex justify-end">
          <button onClick={handleSaveConfig} disabled={!configDirty || loading === "config" || !status?.epoch_initialized || runtimeMutationLocked} className="px-4 py-1.5 text-sm rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-40">
            {loading === "config" ? "保存中..." : "保存"}
          </button>
        </div>
      </div>

      {/* Decision Chain */}
      <div className="rounded-lg border border-gray-200 dark:border-border-subtle bg-white dark:bg-surface-1 p-4">
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

      <div className="rounded-lg border border-blue-200 dark:border-blue-800 bg-blue-50 dark:bg-blue-950/30 p-4 text-sm text-blue-800 dark:text-blue-200">
        生成、质量门、预提交评估和正式认证只能由编排器按 checkpoint 顺序推进；网页不提供可绕过流程的通用工具执行入口。
      </div>
      </div>
    </EvolutionPageScaffold>
  );
}
