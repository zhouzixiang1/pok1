import { useEffect, useState, useMemo, useRef } from "react";
import { Link } from "react-router";
import { useRatings, useMatchStats, useDaemonStatus, useRateLimit, useRecentMatches, useH2H, useGenerations, useDataStreamStatus } from "../context/DataProvider";
import { api } from "../api/client";
import {
  controlPipelineBlocked,
  controlPipelineIssues,
  controlSchedulerOwnsPrepareBoundary,
} from "../api/control";
import type { PipelineCheckpoint } from "../api/types";
import PageMeta from "../components/common/PageMeta";
import { Badge } from "../components/shared/Badge";
import { EmptyState } from "../components/shared/EmptyState";
import { PipelineStatus } from "../components/evolution/PipelineStatus";
import { EpochAuthorityStatus } from "../components/evolution/EpochAuthorityStatus";
import { StabilityStatus } from "../components/evolution/StabilityStatus";
import { stabilityPresentation } from "../lib/stabilityView";
import { controlTaskActive, controlTaskStopping } from "../lib/controlRuntimeState";
import { authorityNextVersion, useControlStatus } from "../hooks/useControlStatus";
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

function Sparkline({ data, color }: { data: number[]; color: string }) {
  if (data.length < 2) return null;
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  const w = 40;
  const h = 14;
  const points = data.map((v, i) => `${(i / (data.length - 1)) * w},${h - ((v - min) / range) * h}`).join(" ");
  return (
    <svg width={w} height={h} className="inline-block">
      <polyline fill="none" stroke={color} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" points={points} />
    </svg>
  );
}

export default function Overview() {
  const ratings = useRatings();
  const stats = useMatchStats();
  const daemon = useDaemonStatus();
  const dataStream = useDataStreamStatus();
  const [summary, setSummary] = useState<Record<string, { peak_rating: number; current_rating: number; trend: number; periods: number; peak_h2h_avg_wr?: number; current_h2h_avg_wr?: number; wr_trend?: number }>>({});
  const { status: controlStatus, health: controlHealth, loading: controlLoading, error: controlError } = useControlStatus(5_000);
  const [checkpoint, setCheckpoint] = useState<PipelineCheckpoint | null>(null);
  const [localElapsed, setLocalElapsed] = useState(0);
  const lastDaemonAgeRef = useRef<number | undefined>(undefined);
  const checkpointRequestSequence = useRef(0);
  const rateLimit = useRateLimit();

  useEffect(() => {
    if (!controlStatus?.epoch_initialized) {
      setSummary({});
      return;
    }
    api.historySummary().then(setSummary).catch((e) => console.error("[Overview] API error:", e));
    const id = setInterval(() => {
      api.historySummary().then(setSummary).catch((e) => console.error("[Overview] API error:", e));
    }, 15000);
    return () => clearInterval(id);
  }, [controlStatus?.epoch_initialized]);

  useEffect(() => {
    const requestSequenceRef = checkpointRequestSequence;
    if (!controlStatus?.epoch_initialized) {
      ++requestSequenceRef.current;
      setCheckpoint(null);
      return;
    }
    const refresh = () => {
      const requestSequence = ++checkpointRequestSequence.current;
      api.pipelineCheckpoint().then((value) => {
        if (requestSequence === checkpointRequestSequence.current) {
          setCheckpoint(value);
        }
      }).catch((e) => {
        if (requestSequence !== checkpointRequestSequence.current) return;
        setCheckpoint(null);
        console.error("[Overview] API error:", e);
      });
    };
    refresh();
    const id = setInterval(refresh, 5000);
    return () => {
      ++requestSequenceRef.current;
      clearInterval(id);
    };
  }, [controlStatus?.epoch_initialized]);

  // Reset the local timer when SSE pushes a new daemon heartbeat age. Strength
  // cycle age is a separate evidence field and must not masquerade as liveness.
  useEffect(() => {
    if (daemon?.heartbeat_age_seconds !== lastDaemonAgeRef.current) {
      lastDaemonAgeRef.current = daemon?.heartbeat_age_seconds ?? undefined;
      setLocalElapsed(0);
    }
  }, [daemon?.heartbeat_age_seconds]);

  // Local 1s tick so "X秒前" increments between SSE pushes
  useEffect(() => {
    const timer = setInterval(() => setLocalElapsed((e) => e + 1), 1000);
    return () => clearInterval(timer);
  }, []);

  const visibleRatings = controlStatus?.epoch_initialized
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
  const strengthEmptyMessage = controlStatus?.epoch_initialized
    ? controlStatus.active_bots.length === 0
      ? "当前严格发布池为空；尚无可进入评分周期的 Bot。"
      : "严格发布池正在等待首个同发布池 evaluation cycle；不会用 Glicko 默认值伪造选择分。"
    : nextAuthorityVersion != null
      ? `严格国赛 epoch 尚未初始化；v${controlStatus?.version_authority_high_water ?? 142} 仅为数字高水位，reset 后首目标为 v${nextAuthorityVersion}。`
      : "epoch 权威当前不可用或需要恢复；在权威恢复前不声明下一版本或强度结果。";
  const strengthSampleDisplay = controlStatus?.epoch_initialized && visibleRatings.length > 0
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
  const pipelineBlocked = controlPipelineBlocked(controlHealth?.pipeline);
  const pipelineIssues = controlPipelineIssues(controlHealth?.pipeline);
  const schedulerOwnsPrepare = controlSchedulerOwnsPrepareBoundary(
    controlStatus,
    controlHealth,
  );
  const daemonStatusLabel = !controlStatus?.epoch_initialized
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
    <>
      <PageMeta title="总览 — Bot 自进化" description="Bot 种群概览" />

      <EpochAuthorityStatus
        status={controlStatus}
        loading={controlLoading}
        error={controlError}
        className="mb-4"
      />

      {/* 429 rate-limit warning banner */}
      {controlStatus?.epoch_initialized && rateLimit?.blocked && (
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

      {/* Compact metric strip */}
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
        <div className="flex items-baseline gap-2">
          <span className="text-2xl font-bold text-gray-900 dark:text-white tabular-nums">{controlStatus?.active_bots.length ?? 0}</span>
          <span className="text-xs text-gray-500 dark:text-gray-400">严格发布 Bot</span>
        </div>
        <div className="w-px h-5 bg-gray-200 dark:bg-gray-700" />
        <div className="flex items-baseline gap-2">
          <span className="text-2xl font-bold text-gray-900 dark:text-white tabular-nums">{strengthSampleDisplay}</span>
          <span className="text-xs text-gray-500 dark:text-gray-400">权威强度样本</span>
        </div>
        <div className="w-px h-5 bg-gray-200 dark:bg-gray-700" />
        <div className="flex items-baseline gap-2">
          <span className="text-2xl font-bold text-gray-900 dark:text-white tabular-nums">{controlStatus?.strict_generation_count ?? 0}</span>
          <span className="text-xs text-gray-500 dark:text-gray-400">严格代次</span>
        </div>
        <div className="w-px h-5 bg-gray-200 dark:bg-gray-700" />
        <div className="flex items-center gap-2">
          <span className="text-2xl font-bold text-gray-900 dark:text-white tabular-nums">
            {stability && stabilityCountVerified ? `${stability.count}/${stability.target}` : "—"}
          </span>
          <span className="text-xs text-gray-500 dark:text-gray-400">连续进化验收</span>
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
          {controlStatus?.epoch_initialized && (
            <span className="text-[10px] text-gray-400">控制操作请前往控制面板</span>
          )}
          <span className="text-[10px] text-gray-400">心跳 {daemonAgeStr}</span>
          <span className="text-[10px] text-gray-400">
            配置意图：{daemonConfigured == null ? "不可用" : daemonConfigured ? "启用" : "禁用"}
            {" · "}实际进程：{controlHealth?.daemon.alive == null ? "不可用" : controlHealth.daemon.alive ? "运行" : "停止"}
          </span>
          {controlStatus?.epoch_initialized && (
            <span className="text-[10px] text-gray-400">
              {daemon?.strength_evidence_available
                ? "强度周期已发布"
                : daemon?.strength_evidence_status === "active_pool_empty"
                  ? "发布池为空，无强度证据"
                  : daemon?.strength_evidence_status === "active_pool_singleton"
                    ? "单 Bot 无法形成强度样本"
                    : "等待首个完整 70 手强度样本"}
            </span>
          )}
        </div>
      </div>

      {/* Top 5 featured + Activity + Pipeline */}
      <div className="mt-6 grid grid-cols-1 gap-4 lg:grid-cols-3 lg:gap-6">
        {/* Top 5 podium */}
        <div className="lg:col-span-2">
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
              const s = summary[bot.name];
              const sparkData = s ? [s.peak_rating, (s.peak_rating + s.current_rating) / 2, s.current_rating] : [];
              const sparkColor = s && s.trend > 0 ? "#12b76a" : s && s.trend < 0 ? "#f04438" : "#98a2b3";
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
                    {sparkData.length >= 2 && <Sparkline data={sparkData} color={sparkColor} />}
                    {s && (s.wr_trend != null ? (
                      <Badge variant={s.wr_trend > 0 ? "success" : s.wr_trend < 0 ? "error" : "neutral"} size="sm">
                        {s.wr_trend > 0 ? "↑" : s.wr_trend < 0 ? "↓" : "→"} {(Math.abs(s.wr_trend) * 100).toFixed(1)}pp
                      </Badge>
                    ) : (
                      <Badge variant={s.trend > 0 ? "success" : s.trend < 0 ? "error" : "neutral"} size="sm">
                        {s.trend > 0 ? "↑" : s.trend < 0 ? "↓" : "→"} {Math.abs(s.trend).toFixed(1)}
                      </Badge>
                    ))}
                  </div>
                </div>
              );
            })()}

            {/* #2-5 — compact cards */}
            {top5.slice(1).map((bot) => {
              const s = summary[bot.name];
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
                    {s && (s.wr_trend != null ? (
                      <Badge variant={s.wr_trend > 0 ? "success" : s.wr_trend < 0 ? "error" : "neutral"} size="sm">
                        {s.wr_trend > 0 ? "↑" : s.wr_trend < 0 ? "↓" : "→"} {(Math.abs(s.wr_trend) * 100).toFixed(1)}pp
                      </Badge>
                    ) : s.trend !== 0 ? (
                      <span className={s.trend > 0 ? "text-success-600 dark:text-success-400" : "text-error-600 dark:text-error-400"}>
                        {s.trend > 0 ? "↑" : "↓"} {Math.abs(s.trend).toFixed(1)}
                      </span>
                    ) : null)}
                  </div>
                </div>
              );
            })}
          </div>

          {/* Pipeline status bar */}
          {controlStatus && (
            controlStatus.active_generation
            || controlStatus.post_publication_handoff.status !== "none"
            || schedulerOwnsPrepare
          ) && (
            <div className="mt-4 rounded-2xl border border-gray-200 bg-white p-4 dark:border-border-subtle dark:bg-surface-1">
              <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-3">
                  <Badge variant={orchestratorHealthy ? "success" : taskStopping ? "warning" : controlStatus?.running || orchestratorOrphan ? "error" : "neutral"} size="sm" pulse={orchestratorHealthy}>
                    {orchestratorHealthy ? "任务健康运行" : taskStopping ? "正在安全停止，等待任务退出" : orchestratorOrphan ? "孤立任务仍活动" : controlStatus?.running ? "运行标志异常" : "已停止"}
                  </Badge>
                  <span className="text-sm text-gray-500 dark:text-gray-400">
                    {controlStatus.active_generation
                      ? `${activeIdentityLabel ?? "双身份投影不可用"} · source_v ${controlStatus.active_generation.source_v == null ? "—" : `v${controlStatus.active_generation.source_v}`}`
                      : controlStatus.post_publication_handoff.status !== "none"
                        ? `post-publication v${controlStatus.post_publication_handoff.version ?? "?"}`
                        : `scheduler target ${nextAuthorityVersion == null ? "待恢复" : `v${nextAuthorityVersion}`}`}
                  </span>
                </div>
              </div>
              <PipelineStatus
                checkpoint={checkpoint}
                activeGeneration={controlStatus.active_generation}
                handoff={controlStatus.post_publication_handoff}
                handoffBlocked={controlStatus.post_publication_handoff.status !== "none" && pipelineBlocked}
                activeBlocked={Boolean(controlStatus.active_generation && pipelineBlocked)}
                activeIssues={pipelineIssues}
                schedulerActive={schedulerOwnsPrepare}
              />
            </div>
          )}
        </div>

        {/* Right: Activity */}
        {controlStatus?.epoch_initialized ? (
          <RecentActivityCard />
        ) : (
          <div className="rounded-2xl border border-gray-200 bg-white p-4 text-xs text-gray-500 dark:border-border-subtle dark:bg-surface-1 dark:text-gray-400">
            旧对局、旧 H2H 和旧代次日志已退出当前权威视图；完成一次性 reset 后才会展示新 epoch 证据。
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
                  <th className="px-5 py-2 font-medium">趋势</th>
                  <th className="px-5 py-2 font-medium">强度置信</th>
                </tr>
              </thead>
              <tbody>
                {rest.map((bot) => {
                  const s = summary[bot.name];
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
                        {s && (s.wr_trend != null ? (
                          <Badge variant={s.wr_trend > 0 ? "success" : s.wr_trend < 0 ? "error" : "neutral"} size="sm">
                            {s.wr_trend > 0 ? "↑" : s.wr_trend < 0 ? "↓" : "→"} {(Math.abs(s.wr_trend) * 100).toFixed(1)}pp
                          </Badge>
                        ) : s ? (
                          <Badge variant={s.trend > 0 ? "success" : s.trend < 0 ? "error" : "neutral"} size="sm">
                            {s.trend > 0 ? "↑" : s.trend < 0 ? "↓" : "→"} {Math.abs(s.trend).toFixed(1)}
                          </Badge>
                        ) : "—")}
                      </td>
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
    </>
  );
}
