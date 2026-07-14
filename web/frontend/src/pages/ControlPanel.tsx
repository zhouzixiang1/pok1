import { useEffect, useState, useCallback } from "react";
import { controlApi, type Decision, type AppConfig } from "../api/control";
import { api } from "../api/client";
import type { OrchestratorSession, PipelineCheckpoint } from "../api/types";
import { EpochAuthorityStatus } from "../components/evolution/EpochAuthorityStatus";
import { OfficialCertificationProgress } from "../components/evolution/OfficialCertificationProgress";
import { authorityNextVersion, useControlStatus } from "../hooks/useControlStatus";
import { getOperatorControlToken, setOperatorControlToken } from "../api/operatorControl";


// ── Inline SVG helpers ─────────────────────────────────────────────────────────
const RefreshIcon = ({ className }: { className?: string }) => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className}><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
);
// ── Main ───────────────────────────────────────────────────────────────────────

export default function ControlPanel() {
  const { status, loading: statusLoading, error: statusError, refresh: refreshStatus } = useControlStatus(3_000);
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [loading, setLoading] = useState<string | null>(null);
  const [editWorkers, setEditWorkers] = useState(12);
  const [editPairs, setEditPairs] = useState(5);
  const [editDaemon, setEditDaemon] = useState(true);
  const [session, setSession] = useState<OrchestratorSession | null>(null);
  const [checkpoint, setCheckpoint] = useState<PipelineCheckpoint | null>(null);
  const [sessionLoading, setSessionLoading] = useState(false);
  const [connError, setConnError] = useState(false);
  const [operatorToken, setOperatorToken] = useState(getOperatorControlToken);
  const [mutationError, setMutationError] = useState("");

  const updateOperatorToken = (value: string) => {
    setOperatorToken(value);
    setOperatorControlToken(value);
    setMutationError("");
  };

  const refresh = useCallback(async () => {
    try {
      const [d, c] = await Promise.all([
        controlApi.decisions(),
        controlApi.getConfig(),
      ]);
      setDecisions(d);
      setConfig(c);
      setEditWorkers(c.daemon_workers);
      setEditPairs(c.daemon_pairs);
      setEditDaemon(c.daemon_enabled);
      setConnError(false);
    } catch { setConnError(true); }
  }, []);

  const refreshSession = useCallback(async () => {
    try {
      const [sess, ckpt] = await Promise.all([
        api.orchestratorSession(),
        api.pipelineCheckpoint(),
      ]);
      setSession(sess);
      setCheckpoint(ckpt);
      setConnError(false);
    } catch { setConnError(true); }
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
    setMutationError("");
    try {
      await controlApi.setConfig({ daemon_enabled: editDaemon, daemon_workers: editWorkers, daemon_pairs: editPairs });
    } catch (error) {
      setMutationError(error instanceof Error ? error.message : String(error));
    } finally { setLoading(null); }
    try { await refresh(); } catch (e) { console.error("[ControlPanel] refresh after save failed:", e); }
  };

  const handleStart = async () => {
    if (!status?.epoch_initialized) return;
    setLoading("start");
    setMutationError("");
    try { await controlApi.start(); }
    catch (error) { setMutationError(error instanceof Error ? error.message : String(error)); }
    finally { setLoading(null); }
    await Promise.all([refresh(), refreshStatus()]);
  };

  const handleStop = async () => {
    setLoading("stop");
    setMutationError("");
    try { await controlApi.stop(); }
    catch (error) { setMutationError(error instanceof Error ? error.message : String(error)); }
    finally { setLoading(null); }
    await Promise.all([refresh(), refreshStatus()]);
  };

  const handleResetSession = async () => {
    if (!status?.epoch_initialized) return;
    if (!confirm("重置编排器会话？下次重启将开始全新的 LLM 对话。")) return;
    setSessionLoading(true);
    setMutationError("");
    try {
      await api.clearOrchestratorSession();
      await refreshSession();
    } catch (error) {
      setMutationError(error instanceof Error ? error.message : String(error));
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

  const authoritativeRunning = Boolean(status?.epoch_initialized && status.running);
  const unsafeRunning = Boolean(status?.running && !status.epoch_initialized);
  const authorityTarget = authorityNextVersion(status);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-gray-800 dark:text-white">控制面板</h1>
        <button onClick={() => void Promise.all([refresh(), refreshStatus(), refreshSession()])} className="px-3 py-1 text-sm rounded bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 flex items-center gap-1">
          <RefreshIcon /> 刷新
        </button>
      </div>

      <EpochAuthorityStatus status={status} loading={statusLoading} error={statusError} />

      <OfficialCertificationProgress status={status} />

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
            <span className="text-sm font-medium text-gray-700 dark:text-gray-300">编排器</span>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-sm text-gray-500">状态:</span>
            <span className={`inline-flex items-center gap-1.5 text-sm font-medium ${authoritativeRunning ? "text-green-600" : unsafeRunning ? "text-red-600" : "text-gray-400"}`}>
              <span className={`w-2 h-2 rounded-full ${authoritativeRunning ? "bg-green-500 animate-pulse" : unsafeRunning ? "bg-red-500" : "bg-gray-400"}`} />
              {authoritativeRunning ? "运行中" : unsafeRunning ? "异常运行（epoch 未初始化）" : "已停止"}
            </span>
            {connError && <span className="text-xs text-red-500">连接失败</span>}
          </div>
          {status && (
            <div className="text-sm text-gray-500">
              严格代次 {status.strict_generation_count} | 数字权威 v{status.version_authority_high_water} → 目标 {authorityTarget != null ? `v${authorityTarget}` : "待恢复"}
            </div>
          )}
          <div className="flex gap-2 ml-auto">
            {!status?.running ? (
              <button
                onClick={handleStart}
                disabled={loading === "start" || !status?.epoch_initialized}
                title={!status?.epoch_initialized ? "完成操作员一次性 epoch reset 后才能启动" : undefined}
                className="px-4 py-1.5 text-sm rounded bg-green-600 text-white hover:bg-green-700 disabled:cursor-not-allowed disabled:opacity-40"
              >
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
      <div className="rounded-lg border border-gray-200 dark:border-border-subtle bg-white dark:bg-surface-1 p-4">
        <h2 className="text-lg font-semibold text-gray-800 dark:text-white mb-3">LLM 会话</h2>
        <div className="flex flex-wrap items-center gap-4">
          <div>
            <span className="text-xs text-gray-500 block mb-1">编排器会话 ID</span>
            <span className="font-mono text-sm text-gray-800 dark:text-gray-200">
              {session?.session_id ? session.session_id.slice(0, 12) + "..." : <span className="text-gray-400 italic">无活跃会话</span>}
            </span>
          </div>
          {status?.active_generation && (
            <div>
              <span className="text-xs text-gray-500 block mb-1">流程阶段</span>
              <span className="px-2 py-0.5 rounded bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300 text-xs font-medium">
                v{status.active_generation.next_v}
                {status.active_generation.source_v != null ? ` ← v${status.active_generation.source_v}` : ""}: {status.active_generation.stage}
              </span>
              {!checkpoint && <p className="mt-1 text-[10px] text-amber-600">详细 checkpoint 暂不可用</p>}
            </div>
          )}
          <div className="ml-auto">
            <button
              onClick={handleResetSession}
              disabled={sessionLoading || !session?.active || !status?.epoch_initialized}
              className="px-4 py-1.5 text-sm rounded bg-orange-600 text-white hover:bg-orange-700 disabled:opacity-40 flex items-center gap-1"
            >
              <RefreshIcon /> {sessionLoading ? "重置中..." : "重置会话"}
            </button>
            <p className="text-xs text-gray-400 mt-1">下次重启时强制开启全新 LLM 对话</p>
          </div>
        </div>
      </div>

      {/* Settings */}
      <div className="rounded-lg border border-gray-200 dark:border-border-subtle bg-white dark:bg-surface-1 p-4">
        <h2 className="text-lg font-semibold text-gray-800 dark:text-white mb-3">评分引擎设置</h2>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <div className="flex items-center gap-3">
            <label className="text-sm text-gray-600 dark:text-gray-300">评分引擎</label>
            <button
              role="switch"
              aria-checked={editDaemon}
              disabled={!status?.epoch_initialized}
              onClick={() => setEditDaemon(!editDaemon)}
              className={`relative inline-flex h-6 w-11 rounded-full border-2 border-transparent transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${editDaemon ? "bg-blue-600" : "bg-gray-300 dark:bg-gray-600"}`}
            >
              <span className={`inline-block h-5 w-5 transform rounded-full bg-white shadow transition-transform ${editDaemon ? "translate-x-5" : "translate-x-0"}`} />
            </button>
          </div>
          <div className="flex items-center gap-2">
            <label className="text-sm text-gray-600 dark:text-gray-300 whitespace-nowrap">Worker</label>
            <input type="number" min={1} max={12} value={editWorkers} onChange={(e) => setEditWorkers(Math.max(1, Math.min(12, Number(e.target.value) || 1)))} disabled={!editDaemon || !status?.epoch_initialized} className="w-20 px-2 py-1 text-sm rounded border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white disabled:opacity-40" />
          </div>
          <div className="flex items-center gap-2">
            <label className="text-sm text-gray-600 dark:text-gray-300 whitespace-nowrap">每次配对数</label>
            <input type="number" min={1} max={20} value={editPairs} onChange={(e) => setEditPairs(Math.max(1, Math.min(20, Number(e.target.value) || 1)))} disabled={!editDaemon || !status?.epoch_initialized} className="w-20 px-2 py-1 text-sm rounded border border-gray-300 dark:border-gray-600 dark:bg-gray-700 dark:text-white disabled:opacity-40" />
          </div>
        </div>
        <div className="mt-3 flex justify-end">
          <button onClick={handleSaveConfig} disabled={!configDirty || loading === "config" || !status?.epoch_initialized} className="px-4 py-1.5 text-sm rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-40">
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
  );
}
