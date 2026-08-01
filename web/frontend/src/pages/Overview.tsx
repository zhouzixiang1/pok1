import { useState, useMemo, useRef, useEffect } from "react";
import { Link } from "react-router";
import {
  useRatings,
  useMatchStats,
  useDaemonStatus,
  useRateLimit,
  useRecentMatches,
  useH2H,
  useGenerations,
  useDataStreamStatus,
  useControlStatusValue,
} from "../context/DataProvider";
import { api } from "../api/client";
import { controlSchedulerOwnsPrepareBoundary } from "../api/control";
import type { LlmMetricsSummary } from "../api/types";
import { useBoundPolling } from "../hooks/useBoundPolling";
import { authorityNextVersion } from "../hooks/useControlStatus";
import PageMeta from "../components/common/PageMeta";
import { Badge } from "../components/shared/Badge";
import { EmptyState } from "../components/shared/EmptyState";
import { EvolutionPageScaffold } from "../components/evolution/EvolutionPageScaffold";
import { StabilityStatus } from "../components/evolution/StabilityStatus";
import {
  EvolutionSurface,
  EvolutionStatusBadge,
} from "../components/evolution/ui";
import { stabilityPresentation } from "../lib/stabilityView";
import { controlTaskActive, controlTaskStopping } from "../lib/controlRuntimeState";
import { cn, compactBotName } from "../lib/utils";
import { canonicalGenerationLabel } from "../lib/canonicalGenerationIdentity";

const strengthConfidenceLabel: Record<string, string> = {
  high: "强度高置信",
  medium: "强度中置信",
  low: "强度低置信",
};

type StrengthBadgeVariant = "success" | "warning" | "error";

const strengthConfidenceVariant = (value?: string): StrengthBadgeVariant => (
  value === "high" ? "success" : value === "medium" ? "warning" : "error"
);

function RecentActivityCard() {
  const matches = useRecentMatches();
  const h2h = useH2H();
  const generations = useGenerations();

  const topRivalry = useMemo(() => {
    const entries = Object.entries(h2h);
    if (entries.length === 0) return null;
    let best: { key: string; wr: number; games: number } | null = null;
    for (const [key, val] of entries) {
      const wr = Math.abs(val.win_rate - 0.5);
      if (!best || wr > Math.abs(best.wr - 0.5)) best = { key, wr: val.win_rate, games: val.games };
    }
    return best;
  }, [h2h]);

  const latestGen = generations.length > 0 ? generations[generations.length - 1] : null;
  const recentMatches = matches.slice(0, 5);

  return (
    <div className="space-y-3">
      <div className="rounded-2xl border border-gray-200 bg-white p-4 dark:border-border-subtle dark:bg-surface-1">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-3">最近对局</h4>
        {recentMatches.length === 0 ? (
          <p className="text-xs text-gray-400">暂无对局记录</p>
        ) : (
          <div className="space-y-2">
            {recentMatches.map((m) => (
              <div key={m.id} className="flex items-center gap-2 text-xs">
                <span className="text-gray-600 dark:text-gray-300 truncate">
                  {compactBotName(m.bot0)} vs {compactBotName(m.bot1)}
                </span>
                <span className="ml-auto flex gap-1.5 text-gray-500 font-mono shrink-0">
                  <span className={cn(m.bot0_wins > m.bot1_wins && "text-success-600 font-medium dark:text-success-400")}>{m.bot0_wins}</span>
                  <span>:</span>
                  <span className={cn(m.bot1_wins > m.bot0_wins && "text-success-600 font-medium dark:text-success-400")}>{m.bot1_wins}</span>
                </span>
              </div>
            ))}
          </div>
        )}
      </div>

      {topRivalry && (
        <div className="rounded-2xl border border-gray-200 bg-white p-4 dark:border-border-subtle dark:bg-surface-1">
          <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-2">最悬殊对战</h4>
          <p className="text-sm text-gray-700 dark:text-gray-300 font-medium">
            {topRivalry.key.split(" vs ").map(compactBotName).join(" vs ")}
          </p>
          <p className="text-xs text-gray-500 mt-1">胜率 {(topRivalry.wr * 100).toFixed(0)}% · {topRivalry.games} 个完整 70 手样本</p>
        </div>
      )}

      {latestGen && (
        <div className="rounded-2xl border border-gray-200 bg-white p-4 dark:border-border-subtle dark:bg-surface-1">
          <h4 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-2">最新代次</h4>
          <div className="flex items-center gap-2 text-sm">
            <span className="text-gray-800 dark:text-gray-200 font-semibold">{latestGen.version}</span>
            <span className="text-gray-400 text-xs">{latestGen.files.length} 个日志文件</span>
          </div>
        </div>
      )}
    </div>
  );
}

/** LLM 今日用量摘要卡：用 useBoundPolling 统一拉取 /api/llm/metrics/summary。 */
function LlmUsageCard({ epochReady }: { epochReady: boolean }) {
  const { data: summary, loading, error } = useBoundPolling<LlmMetricsSummary | null>(
    async () => (epochReady ? api.llmMetricsSummary() : Promise.resolve(null)),
    { enabled: epochReady, pollMs: 30_000 },
  );

  if (!epochReady) {
    return (
      <EvolutionSurface padding="sm">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-1">LLM 今日用量</h3>
        <p className="text-xs text-gray-400">严格进化尚未初始化；不会展示旧 LLM 用量。</p>
      </EvolutionSurface>
    );
  }
  if (loading && !summary) {
    return (
      <EvolutionSurface padding="sm">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-1">LLM 今日用量</h3>
        <p className="text-xs text-gray-400">正在读取用量摘要…</p>
      </EvolutionSurface>
    );
  }
  if (error && !summary) {
    return (
      <EvolutionSurface padding="sm">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-1">LLM 今日用量</h3>
        <p className="text-xs text-error-600 dark:text-error-400">用量摘要不可用：{error.message}</p>
      </EvolutionSurface>
    );
  }

  // total_count/overall_success_rate are optional backend totals; when absent,
  // derive from by_role so the strip still reflects current usage (non-authoritative
  // aggregation only — full figures live on /llm).
  const byRole = summary?.by_role ?? [];
  const total = summary?.total_count ?? byRole.reduce((a, r) => a + r.count, 0);
  const totalCost = summary?.total_cost_usd ?? byRole.reduce((a, r) => a + (r.total_cost_usd ?? 0), 0);
  const successCount = summary?.total_success_count
    ?? byRole.reduce((a, r) => a + r.success_count, 0);
  const successRate = summary?.overall_success_rate ?? (total > 0 && successCount != null ? successCount / total : null);
  const avgElapsed = summary?.avg_total_elapsed_sec ?? null;

  return (
    <EvolutionSurface padding="sm">
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">LLM 今日用量</h3>
        <Link to="/llm" className="text-xs text-brand-600 hover:underline dark:text-brand-400">详细分析 →</Link>
      </div>
      {total === 0 ? (
        <p className="mt-2 text-xs text-gray-400">暂无 LLM 调用记录。</p>
      ) : (
        <div className="mt-2 flex flex-wrap items-baseline gap-x-5 gap-y-1.5">
          <div className="flex items-baseline gap-1.5">
            <span className="text-xl font-bold text-gray-900 dark:text-white tabular-nums">{total}</span>
            <span className="text-[11px] text-gray-500">总调用</span>
          </div>
          <div className="flex items-baseline gap-1.5">
            <span className="text-xl font-bold text-gray-900 dark:text-white tabular-nums">
              {totalCost != null ? `$${totalCost.toFixed(2)}` : "—"}
            </span>
            <span className="text-[11px] text-gray-500">总成本</span>
          </div>
          <div className="flex items-baseline gap-1.5">
            <span className={cn(
              "text-xl font-bold tabular-nums",
              successRate != null && successRate >= 0.95
                ? "text-success-600 dark:text-success-400"
                : successRate != null && successRate < 0.8
                  ? "text-error-600 dark:text-error-400"
                  : "text-gray-900 dark:text-white",
            )}>
              {successRate != null ? `${(successRate * 100).toFixed(1)}%` : "—"}
            </span>
            <span className="text-[11px] text-gray-500">成功率</span>
          </div>
          <div className="flex items-baseline gap-1.5">
            <span className="text-xl font-bold text-gray-900 dark:text-white tabular-nums">
              {avgElapsed != null ? `${avgElapsed.toFixed(1)}s` : "—"}
            </span>
            <span className="text-[11px] text-gray-500">平均耗时</span>
          </div>
        </div>
      )}
    </EvolutionSurface>
  );
}

export default function Overview() {
  const ratings = useRatings();
  const stats = useMatchStats();
  const daemon = useDaemonStatus();
  const dataStream = useDataStreamStatus();
  const { status: controlStatus, health: controlHealth } = useControlStatusValue();
  const [localElapsed, setLocalElapsed] = useState(0);
  const lastDaemonAgeRef = useRef<number | undefined>(undefined);
  const rateLimit = useRateLimit();
  const epochReady = Boolean(controlStatus?.epoch_initialized);

  // Local 1s tick so "X秒前" increments between SSE pushes (cheap timer, not a poll).
  useEffect(() => {
    const timer = setInterval(() => setLocalElapsed((e) => e + 1), 1000);
    return () => clearInterval(timer);
  }, []);

  // Reset the local timer when SSE pushes a new daemon heartbeat age. Strength
  // cycle age is a separate evidence field and must not masquerade as liveness.
  useEffect(() => {
    if (daemon?.heartbeat_age_seconds !== lastDaemonAgeRef.current) {
      lastDaemonAgeRef.current = daemon?.heartbeat_age_seconds ?? undefined;
      setLocalElapsed(0);
    }
  }, [daemon?.heartbeat_age_seconds]);

  const visibleRatings = epochReady
    ? ratings.filter((bot) => Number.isFinite(bot.selection_score ?? bot.leaderboard_score))
    : [];

  const scoreOf = (b: (typeof visibleRatings)[number]) => (
    (b.selection_score ?? b.leaderboard_score) as number
  );
  const maxScore = visibleRatings.length > 0 ? Math.max(...visibleRatings.map(scoreOf)) : 0;
  const minScore = visibleRatings.length > 0 ? Math.min(...visibleRatings.map(scoreOf)) : 0;
  const scoreRange = maxScore - minScore || 1;
  const top5 = visibleRatings.slice(0, 5);
  const rest = visibleRatings.slice(5);
  const nextAuthorityVersion = authorityNextVersion(controlStatus);
  const activeIdentityLabel = controlStatus?.active_generation
    ? canonicalGenerationLabel(
      controlStatus.active_generation,
      controlStatus.active_generation.next_v,
    )
    : null;
  const strengthEmptyMessage = epochReady
    ? controlStatus!.active_bots.length === 0
      ? "当前严格发布池为空；尚无可进入评分周期的 Bot。"
      : "严格发布池正在等待首个绑定当前发布池的完整 70 手评分周期；不会用默认分伪造强度。"
    : nextAuthorityVersion != null
      ? `严格进化尚未初始化；v${controlStatus?.version_authority_high_water ?? 0} 只用于防止版本号倒退，初始化后首目标为 v${nextAuthorityVersion}。`
      : "当前无法验证严格进化身份；恢复前不声明下一版本或强度结果。";
  const strengthSampleDisplay = epochReady && visibleRatings.length > 0
    ? (stats?.total_strength_samples ?? stats?.total_games ?? 0).toLocaleString()
    : "—";
  const daemonAge = daemon?.heartbeat_age_seconds;
  const effectiveAge = daemonAge != null ? daemonAge + localElapsed : null;
  const daemonAgeStr = effectiveAge != null
    ? effectiveAge < 0 ? "从未" : effectiveAge < 60 ? `${Math.round(effectiveAge)}秒前` : `${Math.round(effectiveAge / 60)}分钟前`
    : "—";
  const stability = controlStatus?.stability_observation;
  const stabilityView = stabilityPresentation(stability);
  const stabilityCountVerified = stability?.verification?.state === "fresh"
    && stabilityView.variant !== "error";
  const dataStreamAge = dataStream.last_event_at == null
    ? null
    : Math.max(0, (Date.now() - dataStream.last_event_at) / 1000);
  const dataStreamFresh = dataStream.state === "connected"
    && dataStreamAge != null
    && dataStreamAge <= 10;
  const daemonConfigured = controlHealth?.daemon.configured;
  const daemonConfigConsistent = daemonConfigured != null
    && daemonConfigured === controlStatus?.daemon_enabled;
  const daemonHeartbeatFresh = controlHealth?.daemon.heartbeat_status === "fresh";
  const daemonActuallyHealthy = Boolean(
    dataStreamFresh
    && daemonConfigConsistent
    && daemonConfigured === true
    && controlHealth?.daemon.alive === true
    && daemonHeartbeatFresh
    && !controlHealth.daemon.health_error
    && daemon?.process_alive === true
    && daemon.heartbeat_stale === false
    && (daemon.status === "active" || daemon.status === "idle"),
  );
  const taskActive = controlTaskActive(controlHealth?.task);
  const taskStopping = controlTaskStopping(controlHealth?.task);
  const orchestratorHealthy = Boolean(
    controlStatus?.running
    && controlHealth?.overall === "healthy"
    && taskActive
    && !taskStopping,
  );
  const orchestratorOrphan = Boolean(taskActive && !taskStopping && !controlStatus?.running);
  const schedulerOwnsPrepare = controlSchedulerOwnsPrepareBoundary(
    controlStatus,
    controlHealth,
  );
  const daemonStatusLabel = !epochReady
    ? "未初始化"
    : dataStream.state === "disconnected" ? "评分投影流已断开"
    : dataStream.state === "connecting" ? "评分投影流连接中"
    : !dataStreamFresh ? "评分投影流已过期"
    : daemonConfigured == null ? "评分配置权威不可用"
    : !daemonConfigConsistent ? "评分配置投影不一致"
    : daemonConfigured === false ? "评分进程未配置"
    : controlHealth?.daemon.health_error ? "评分健康投影失败"
    : controlHealth?.daemon.alive !== true ? "评分进程未运行"
    : !daemonHeartbeatFresh ? `评分心跳${controlHealth?.daemon.heartbeat_status ?? "不可用"}`
    : daemon?.status === "active" ? "评分进程活跃"
    : daemon?.status === "idle" ? "评分进程健康等待"
    : "评分实际状态不可用";

  return (
    <EvolutionPageScaffold
      title="运行总览"
      subtitle="系统健康 · 最新代次进度 · LLM 今日用量 · 最新发布 Bot 强度"
    >
      <PageMeta title="运行总览 — Bot 自进化" description="现在发生什么、已发布什么、真实强度如何" />

      {/* 429 rate-limit warning banner */}
      {epochReady && rateLimit?.blocked && (
        <div className="bg-amber-500/10 border border-amber-500/30 rounded-lg px-4 py-2.5 mb-4 flex items-center gap-3">
          <span className="text-amber-400 text-lg">⏳</span>
          <div>
            <p className="text-amber-300 text-sm font-medium">API 配额已耗尽，进化暂停</p>
            <p className="text-amber-400/70 text-xs">
              将在 {rateLimit.reset_time} 自动恢复
              {rateLimit.wait_seconds != null && `（剩余 ${Math.ceil(rateLimit.wait_seconds / 60)} 分钟）`}
            </p>
          </div>
          <span className="text-amber-400/50 text-xs ml-auto">
            {daemonActuallyHealthy
              ? daemon?.status === "active" ? "评分进程实际运行" : "评分进程健康等待"
              : "评分进程当前不可确认运行"}
          </span>
        </div>
      )}

      {/* Compact metric strip: system health cards (epoch 状态/服务/daemon/版本权威) */}
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
        <div className="flex items-baseline gap-2">
          <span className="text-2xl font-bold text-gray-900 dark:text-white tabular-nums">{controlStatus?.active_bots.length ?? 0}</span>
          <span className="text-xs text-gray-500 dark:text-gray-400">真正完成发布的 Bot</span>
        </div>
        <div className="w-px h-5 bg-gray-200 dark:bg-gray-700" />
        <div className="flex items-baseline gap-2">
          <span className="text-2xl font-bold text-gray-900 dark:text-white tabular-nums">{strengthSampleDisplay}</span>
          <span className="text-xs text-gray-500 dark:text-gray-400">已采纳完整 70 手样本</span>
        </div>
        <div className="w-px h-5 bg-gray-200 dark:bg-gray-700" />
        <div className="flex items-baseline gap-2">
          <span className="text-2xl font-bold text-gray-900 dark:text-white tabular-nums">{controlStatus?.strict_generation_count ?? 0}</span>
          <span className="text-xs text-gray-500 dark:text-gray-400">已发布严格代次</span>
        </div>
        <div className="w-px h-5 bg-gray-200 dark:bg-gray-700" />
        <div className="flex items-center gap-2">
          <span className="text-2xl font-bold text-gray-900 dark:text-white tabular-nums">
            {stability && stabilityCountVerified ? `${stability.count}/${stability.target}` : "—"}
          </span>
          <span className="text-xs text-gray-500 dark:text-gray-400">连续稳定代次</span>
          <StabilityStatus observation={stability} compact />
        </div>
        <div className="w-px h-5 bg-gray-200 dark:bg-gray-700" />
        <div className="flex items-center gap-2">
          <Badge
            variant={daemonActuallyHealthy ? "success" : daemonConfigured === false && daemonConfigConsistent ? "neutral" : "error"}
            size="sm"
            pulse={Boolean(daemonActuallyHealthy && daemon?.status === "active")}
          >
            {daemonStatusLabel}
          </Badge>
          {epochReady && (
            <span className="text-[10px] text-gray-400">控制操作请前往控制面板</span>
          )}
          <span className="text-[10px] text-gray-400">心跳 {daemonAgeStr}</span>
          <span className="text-[10px] text-gray-400">
            配置意图：{daemonConfigured == null ? "不可用" : daemonConfigured ? "启用" : "禁用"}
            {" · "}实际进程：{controlHealth?.daemon.alive == null ? "不可用" : controlHealth.daemon.alive ? "运行" : "停止"}
          </span>
        </div>
      </div>

      {/* Top 5 featured bots (最新发布 bot 强度卡片) + Activity + LLM 用量 */}
      <div className="mt-6 grid grid-cols-1 gap-4 lg:grid-cols-3 lg:gap-6">
        {/* Top 5 podium */}
        <div className="lg:col-span-2 space-y-4">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {top5.length === 0 && (
              <div className="sm:col-span-2 lg:col-span-3 rounded-2xl border border-gray-200 bg-white dark:border-border-subtle dark:bg-surface-1">
                <EmptyState
                  message={strengthEmptyMessage}
                />
              </div>
            )}
            {/* #1 — large featured card */}
            {top5[0] && (() => {
              const bot = top5[0];
              return (
                <div className={cn(
                  "sm:col-span-2 lg:col-span-1 rounded-2xl border p-5 relative overflow-hidden",
                  "bg-gradient-to-br from-amber-50 to-white dark:from-amber-950/20 dark:to-surface-1",
                  "border-amber-200 dark:border-amber-900/30",
                )}>
                  <div className="absolute top-3 right-3">
                    <span className="inline-flex items-center justify-center w-8 h-8 rounded-full bg-amber-400 text-amber-900 text-sm font-bold">1</span>
                  </div>
                  <div className="pr-10">
                    <Link to="/bots" className="text-lg font-bold text-gray-900 dark:text-white hover:text-brand-600 dark:hover:text-brand-400">
                      {compactBotName(bot.name)}
                    </Link>
                  </div>
                  <div className="mt-2 flex items-baseline gap-2">
                    <span className="text-3xl font-bold text-gray-900 dark:text-white tabular-nums">{scoreOf(bot).toFixed(4)}</span>
                    <span className="text-xs text-gray-500">进化选择分</span>
                  </div>
                  <div className="mt-2 flex items-center gap-3 text-xs text-gray-500 dark:text-gray-400">
                    <span>H2H {bot.h2h_avg_wr != null ? `${(bot.h2h_avg_wr * 100).toFixed(1)}%` : "—"}</span>
                    <span>覆盖 {bot.h2h_coverage != null ? `${(bot.h2h_coverage * 100).toFixed(0)}%` : "—"}</span>
                    <Badge variant={strengthConfidenceVariant(bot.strength_confidence)} size="sm">
                      {strengthConfidenceLabel[bot.strength_confidence ?? ""] ?? "强度低置信"}
                    </Badge>
                  </div>
                  <div className="mt-3">
                    <Link
                      to="/bots"
                      className="text-xs font-medium text-brand-600 hover:underline dark:text-brand-400"
                    >
                      查看 Bot 强度与回放 →
                    </Link>
                  </div>
                </div>
              );
            })()}

            {/* #2-5 — compact cards */}
            {top5.slice(1).map((bot) => {
              const scorePct = ((scoreOf(bot) - minScore) / scoreRange) * 100;
              return (
                <div key={bot.name} className="rounded-2xl border border-gray-200 bg-white p-4 dark:border-border-subtle dark:bg-surface-1">
                  <div className="flex items-center justify-between">
                    <Link to="/bots" className="text-sm font-semibold text-gray-800 dark:text-white hover:text-brand-600 dark:hover:text-brand-400">
                      {compactBotName(bot.name)}
                    </Link>
                    <span className={cn(
                      "inline-flex items-center justify-center w-5 h-5 rounded-full text-[10px] font-bold",
                      bot.rank === 2 && "bg-gray-300 text-gray-700",
                      bot.rank === 3 && "bg-orange-400 text-orange-900",
                      (bot.rank ?? 0) >= 4 && "bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400",
                    )}>
                      {bot.rank}
                    </span>
                  </div>
                  <div className="mt-2 flex items-baseline gap-2">
                    <span className="text-xl font-bold text-gray-900 dark:text-white tabular-nums">{scoreOf(bot).toFixed(4)}</span>
                    <div className="flex-1 h-1.5 bg-gray-100 dark:bg-gray-800 rounded-full overflow-hidden">
                      <div className="h-full bg-brand-500 rounded-full transition-all" style={{ width: `${scorePct}%` }} />
                    </div>
                  </div>
                  <div className="mt-1.5 flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
                    <span>H2H {bot.h2h_avg_wr != null ? `${(bot.h2h_avg_wr * 100).toFixed(1)}%` : "—"}</span>
                    <span>覆盖 {bot.h2h_coverage != null ? `${(bot.h2h_coverage * 100).toFixed(0)}%` : "—"}</span>
                    <Badge variant={strengthConfidenceVariant(bot.strength_confidence)} size="sm">
                      {bot.strength_confidence === "high" ? "高置信" : bot.strength_confidence === "medium" ? "中置信" : "低置信"}
                    </Badge>
                  </div>
                </div>
              );
            })}
          </div>

          {/* Latest generation progress (slim) */}
          {controlStatus && (
            controlStatus.active_generation
            || controlStatus.post_publication_handoff.status !== "none"
            || schedulerOwnsPrepare
          ) && (
            <EvolutionSurface className="space-y-3" padding="sm">
              <div className="flex items-center justify-between gap-3">
                <div className="flex items-center gap-3">
                  <EvolutionStatusBadge
                    tone={orchestratorHealthy ? "ok" : taskStopping ? "warn" : controlStatus?.running || orchestratorOrphan ? "error" : "neutral"}
                    pulse={orchestratorHealthy}
                  >
                    {orchestratorHealthy ? "任务健康运行" : taskStopping ? "正在安全停止，等待任务退出" : orchestratorOrphan ? "孤立任务仍活动" : controlStatus?.running ? "运行标志异常" : "已停止"}
                  </EvolutionStatusBadge>
                  <span className="text-sm text-gray-500 dark:text-gray-400">
                    {controlStatus.active_generation
                      ? `${activeIdentityLabel ?? "代次与真实标签无法配对"} · 主父本 ${controlStatus.active_generation.source_v == null ? "无" : `v${controlStatus.active_generation.source_v}`}`
                      : controlStatus.post_publication_handoff.status !== "none"
                        ? `post-publication v${controlStatus.post_publication_handoff.version ?? "?"}`
                        : `scheduler target ${nextAuthorityVersion == null ? "待恢复" : `v${nextAuthorityVersion}`}`}
                  </span>
                </div>
                <Link
                  to="/generation"
                  className="shrink-0 text-xs font-medium text-brand-600 hover:underline dark:text-brand-400"
                >
                  打开本代进度 →
                </Link>
              </div>
            </EvolutionSurface>
          )}

          {/* LLM 今日用量摘要 */}
          <LlmUsageCard epochReady={epochReady} />
        </div>

        {/* Right: Activity */}
        {epochReady ? (
          <RecentActivityCard />
        ) : (
          <div className="rounded-2xl border border-gray-200 bg-white p-4 text-xs text-gray-500 dark:border-border-subtle dark:bg-surface-1 dark:text-gray-400">
            旧对局、旧 H2H 和旧代次日志不会混入当前视图；完成一次性初始化后才展示新周期证据。
          </div>
        )}
      </div>

      {/* Full leaderboard table */}
      {rest.length > 0 && (
        <div className="mt-6 rounded-2xl border border-gray-200 bg-white dark:border-border-subtle dark:bg-surface-1 overflow-hidden">
          <div className="px-5 py-3 border-b border-gray-100 dark:border-border-subtle">
            <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300">排行榜 · #{top5.length + 1}–#{visibleRatings.length}</h3>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-100 dark:border-border-subtle text-left text-xs text-gray-400 dark:text-gray-500">
                  <th className="px-5 py-2 font-medium w-12">#</th>
                  <th className="px-5 py-2 font-medium">Bot</th>
                  <th className="px-5 py-2 font-medium">选择分</th>
                  <th className="px-5 py-2 font-medium">H2H</th>
                  <th className="px-5 py-2 font-medium">净筹码/70手</th>
                  <th className="px-5 py-2 font-medium">覆盖</th>
                  <th className="px-5 py-2 font-medium">场数</th>
                  <th className="px-5 py-2 font-medium">强度置信</th>
                </tr>
              </thead>
              <tbody>
                {rest.map((bot) => {
                  const scorePct = ((scoreOf(bot) - minScore) / scoreRange) * 100;
                  return (
                    <tr key={bot.name} className={cn(
                      "border-b border-gray-50 dark:border-border-subtle/50 hover:bg-gray-50 dark:hover:bg-white/[0.02] transition-colors",
                    )}>
                      <td className="px-5 py-2.5 text-gray-400 font-medium text-xs">{bot.rank}</td>
                      <td className="px-5 py-2.5">
                        <Link to="/bots" className="text-sm font-medium text-gray-800 dark:text-gray-200 hover:text-brand-600 dark:hover:text-brand-400">
                          {compactBotName(bot.name)}
                        </Link>
                      </td>
                      <td className="px-5 py-2.5">
                        <div className="flex items-center gap-2">
                          <span className="font-mono font-semibold text-gray-700 dark:text-gray-200 tabular-nums">{scoreOf(bot).toFixed(4)}</span>
                          <div className="w-12 h-1 bg-gray-100 dark:bg-gray-800 rounded-full overflow-hidden">
                            <div className="h-full bg-brand-500/60 rounded-full" style={{ width: `${scorePct}%` }} />
                          </div>
                        </div>
                      </td>
                      <td className="px-5 py-2.5 text-gray-600 dark:text-gray-300 text-xs tabular-nums">
                        {bot.h2h_avg_wr != null ? `${(bot.h2h_avg_wr * 100).toFixed(1)}%` : "—"}
                      </td>
                      <td className="px-5 py-2.5 text-gray-600 dark:text-gray-300 text-xs tabular-nums">
                        {bot.secondary_net_chips_mean != null
                          ? `${bot.secondary_net_chips_mean >= 0 ? "+" : ""}${bot.secondary_net_chips_mean.toFixed(0)}`
                          : "—"}
                      </td>
                      <td className="px-5 py-2.5 text-gray-600 dark:text-gray-300 text-xs tabular-nums">
                        {bot.h2h_coverage != null ? `${(bot.h2h_coverage * 100).toFixed(0)}%` : "—"}
                      </td>
                      <td className="px-5 py-2.5 text-gray-500 text-xs tabular-nums">{bot.games ?? "—"}</td>
                      <td className="px-5 py-2.5">
                        <Badge
                          variant={strengthConfidenceVariant(bot.strength_confidence)}
                          size="sm"
                        >
                          {{
                            high: "高",
                            medium: "中",
                            low: "低",
                          }[bot.strength_confidence ?? ""] || "低"}
                        </Badge>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </EvolutionPageScaffold>
  );
}
