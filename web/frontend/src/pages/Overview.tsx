import { useEffect, useState, useMemo } from "react";
import { Link } from "react-router";
import { useRatings, useMatchStats, useDaemonStatus, useBots, useRecentMatches, useH2H, useGenerations } from "../context/DataProvider";
import { api } from "../api/client";
import { controlApi, type ControlStatus } from "../api/control";
import type { PipelineCheckpoint } from "../api/types";
import PageMeta from "../components/common/PageMeta";
import { Badge } from "../components/shared/Badge";
import { Skeleton } from "../components/shared/Skeleton";
import { PipelineStepper } from "../components/evolution/PipelineStatus";
import { cn } from "../lib/utils";

function DaemonToggle() {
  const daemon = useDaemonStatus();
  const [toggling, setToggling] = useState(false);

  if (!daemon) return null;

  const handleToggle = async () => {
    setToggling(true);
    try { await controlApi.setConfig({ daemon_enabled: !daemon.daemon_enabled }); } catch {}
    setToggling(false);
  };

  return (
    <button
      onClick={handleToggle}
      disabled={toggling}
      className={cn(
        "relative inline-flex h-5 w-9 items-center rounded-full transition-colors focus:outline-none disabled:opacity-50",
        daemon.daemon_enabled ? "bg-success-500" : "bg-gray-300 dark:bg-gray-600",
      )}
    >
      <span className={cn(
        "inline-block h-3.5 w-3.5 transform rounded-full bg-white shadow transition-transform",
        daemon.daemon_enabled ? "translate-x-[18px]" : "translate-x-[3px]",
      )} />
    </button>
  );
}

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
                  {m.bot0.replace("claude_", "")} vs {m.bot1.replace("claude_", "")}
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
            {topRivalry.key.split(" vs ").map((n) => n.replace("claude_", "")).join(" vs ")}
          </p>
          <p className="text-xs text-gray-500 mt-1">胜率 {(topRivalry.wr * 100).toFixed(0)}% · {topRivalry.games} 场</p>
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
  const bots = useBots();
  const daemon = useDaemonStatus();
  const [summary, setSummary] = useState<Record<string, { peak_rating: number; current_rating: number; trend: number; periods: number; peak_h2h_avg_wr?: number; current_h2h_avg_wr?: number; wr_trend?: number }>>({});
  const [controlStatus, setControlStatus] = useState<ControlStatus | null>(null);
  const [checkpoint, setCheckpoint] = useState<PipelineCheckpoint | null>(null);

  useEffect(() => {
    api.historySummary().then(setSummary).catch((e) => console.error("[Overview] API error:", e));
    const id = setInterval(() => {
      api.historySummary().then(setSummary).catch((e) => console.error("[Overview] API error:", e));
    }, 15000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    const refresh = () => {
      controlApi.status().then(setControlStatus).catch((e) => console.error("[Overview] API error:", e));
      api.pipelineCheckpoint().then(setCheckpoint).catch((e) => console.error("[Overview] API error:", e));
    };
    refresh();
    const id = setInterval(refresh, 5000);
    return () => clearInterval(id);
  }, []);

  if (ratings.length === 0) {
    return (
      <div className="space-y-4">
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <Skeleton className="h-20 rounded-xl" />
          <Skeleton className="h-20 rounded-xl" />
          <Skeleton className="h-20 rounded-xl" />
          <Skeleton className="h-20 rounded-xl" />
        </div>
        <Skeleton className="h-64 rounded-2xl" />
      </div>
    );
  }

  const maxRating = Math.max(...ratings.map((b) => b.rating));
  const minRating = Math.min(...ratings.map((b) => b.rating));
  const ratingRange = maxRating - minRating || 1;
  const top5 = ratings.slice(0, 5);
  const rest = ratings.slice(5);
  const daemonAge = daemon?.last_update_age_seconds;
  const daemonAgeStr = daemonAge != null
    ? daemonAge < 0 ? "从未" : daemonAge < 60 ? `${daemonAge}秒前` : `${Math.round(daemonAge / 60)}分钟前`
    : "—";

  return (
    <>
      <PageMeta title="总览 — Bot 自进化" description="Bot 种群概览" />

      {/* Compact metric strip */}
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
        <div className="flex items-baseline gap-2">
          <span className="text-2xl font-bold text-gray-900 dark:text-white tabular-nums">{bots.active.length}</span>
          <span className="text-xs text-gray-500 dark:text-gray-400">活跃 Bot</span>
        </div>
        <div className="w-px h-5 bg-gray-200 dark:bg-gray-700" />
        <div className="flex items-baseline gap-2">
          <span className="text-2xl font-bold text-gray-900 dark:text-white tabular-nums">{(stats?.total_games ?? 0).toLocaleString()}</span>
          <span className="text-xs text-gray-500 dark:text-gray-400">总对局</span>
        </div>
        <div className="w-px h-5 bg-gray-200 dark:bg-gray-700" />
        <div className="flex items-baseline gap-2">
          <span className="text-2xl font-bold text-gray-900 dark:text-white tabular-nums">第 {controlStatus?.generation_count ?? 0} 代</span>
          <span className="text-xs text-gray-500 dark:text-gray-400">进化</span>
        </div>
        <div className="w-px h-5 bg-gray-200 dark:bg-gray-700" />
        <div className="flex items-center gap-2">
          <Badge variant={daemon?.status === "active" ? "success" : "error"} size="sm" pulse={daemon?.status === "active"}>
            {daemon?.status === "active" ? "活跃" : daemon?.status === "idle" ? "空闲" : "未知"}
          </Badge>
          <DaemonToggle />
          <span className="text-[10px] text-gray-400">{daemonAgeStr}</span>
        </div>
      </div>

      {/* Top 5 featured + Activity + Pipeline */}
      <div className="mt-6 grid grid-cols-1 gap-4 lg:grid-cols-3 lg:gap-6">
        {/* Top 5 podium */}
        <div className="lg:col-span-2">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
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
                      {bot.name.replace("claude_", "")}
                    </Link>
                  </div>
                  <div className="mt-2 flex items-baseline gap-2">
                    <span className="text-3xl font-bold text-gray-900 dark:text-white tabular-nums">{bot.rating.toFixed(0)}</span>
                    <span className="text-xs text-gray-500">评分</span>
                  </div>
                  <div className="mt-2 flex items-center gap-3 text-xs text-gray-500 dark:text-gray-400">
                    <span>H2H {bot.h2h_avg_wr != null ? `${(bot.h2h_avg_wr * 100).toFixed(1)}%` : "—"}</span>
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
              const ratingPct = ((bot.rating - minRating) / ratingRange) * 100;
              return (
                <div key={bot.name} className="rounded-2xl border border-gray-200 bg-white p-4 dark:border-border-subtle dark:bg-surface-1">
                  <div className="flex items-center justify-between">
                    <Link to="/bots" className="text-sm font-semibold text-gray-800 dark:text-white hover:text-brand-600 dark:hover:text-brand-400">
                      {bot.name.replace("claude_", "")}
                    </Link>
                    <span className={cn(
                      "inline-flex items-center justify-center w-5 h-5 rounded-full text-[10px] font-bold",
                      bot.rank === 2 && "bg-gray-300 text-gray-700",
                      bot.rank === 3 && "bg-orange-400 text-orange-900",
                      bot.rank >= 4 && "bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400",
                    )}>
                      {bot.rank}
                    </span>
                  </div>
                  <div className="mt-2 flex items-baseline gap-2">
                    <span className="text-xl font-bold text-gray-900 dark:text-white tabular-nums">{bot.rating.toFixed(0)}</span>
                    <div className="flex-1 h-1.5 bg-gray-100 dark:bg-gray-800 rounded-full overflow-hidden">
                      <div className="h-full bg-brand-500 rounded-full transition-all" style={{ width: `${ratingPct}%` }} />
                    </div>
                  </div>
                  <div className="mt-1.5 flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
                    <span>H2H {bot.h2h_avg_wr != null ? `${(bot.h2h_avg_wr * 100).toFixed(1)}%` : "—"}</span>
                    {s && (s.wr_trend != null ? (
                      <Badge variant={s.wr_trend > 0 ? "success" : s.wr_trend < 0 ? "error" : "neutral"} size="sm">
                        {s.wr_trend > 0 ? "↑" : s.wr_trend < 0 ? "↓" : "→"}{(Math.abs(s.wr_trend) * 100).toFixed(0)}pp
                      </Badge>
                    ) : s.trend !== 0 ? (
                      <span className={s.trend > 0 ? "text-success-600 dark:text-success-400" : "text-error-600 dark:text-error-400"}>
                        {s.trend > 0 ? "↑" : "↓"}{Math.abs(s.trend).toFixed(0)}
                      </span>
                    ) : null)}
                  </div>
                </div>
              );
            })}
          </div>

          {/* Pipeline status bar */}
          {checkpoint && (
            <div className="mt-4 rounded-2xl border border-gray-200 bg-white p-4 dark:border-border-subtle dark:bg-surface-1">
              <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-3">
                  <Badge variant={controlStatus?.running ? "success" : "neutral"} size="sm" pulse={controlStatus?.running}>
                    {controlStatus?.running ? "运行中" : "已停止"}
                  </Badge>
                  <span className="text-sm text-gray-500 dark:text-gray-400">
                    v{controlStatus?.current_v ?? 0} → v{controlStatus?.next_v ?? 0}
                  </span>
                </div>
              </div>
              <PipelineStepper checkpoint={checkpoint} />
            </div>
          )}
        </div>

        {/* Right: Activity */}
        <RecentActivityCard />
      </div>

      {/* Full leaderboard table */}
      {rest.length > 0 && (
        <div className="mt-6 rounded-2xl border border-gray-200 bg-white dark:border-border-subtle dark:bg-surface-1 overflow-hidden">
          <div className="px-5 py-3 border-b border-gray-100 dark:border-border-subtle">
            <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300">排行榜 · #{top5.length + 1}–#{ratings.length}</h3>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-100 dark:border-border-subtle text-left text-xs text-gray-400 dark:text-gray-500">
                  <th className="px-5 py-2 font-medium w-12">#</th>
                  <th className="px-5 py-2 font-medium">Bot</th>
                  <th className="px-5 py-2 font-medium">评分</th>
                  <th className="px-5 py-2 font-medium">H2H</th>
                  <th className="px-5 py-2 font-medium">场数</th>
                  <th className="px-5 py-2 font-medium">趋势</th>
                  <th className="px-5 py-2 font-medium">置信</th>
                </tr>
              </thead>
              <tbody>
                {rest.map((bot) => {
                  const s = summary[bot.name];
                  const ratingPct = ((bot.rating - minRating) / ratingRange) * 100;
                  return (
                    <tr key={bot.name} className={cn(
                      "border-b border-gray-50 dark:border-border-subtle/50 hover:bg-gray-50 dark:hover:bg-white/[0.02] transition-colors",
                    )}>
                      <td className="px-5 py-2.5 text-gray-400 font-medium text-xs">{bot.rank}</td>
                      <td className="px-5 py-2.5">
                        <Link to="/bots" className="text-sm font-medium text-gray-800 dark:text-gray-200 hover:text-brand-600 dark:hover:text-brand-400">
                          {bot.name.replace("claude_", "")}
                        </Link>
                      </td>
                      <td className="px-5 py-2.5">
                        <div className="flex items-center gap-2">
                          <span className="font-mono font-semibold text-gray-700 dark:text-gray-200 tabular-nums">{bot.rating.toFixed(1)}</span>
                          <div className="w-12 h-1 bg-gray-100 dark:bg-gray-800 rounded-full overflow-hidden">
                            <div className="h-full bg-brand-500/60 rounded-full" style={{ width: `${ratingPct}%` }} />
                          </div>
                        </div>
                      </td>
                      <td className="px-5 py-2.5 text-gray-600 dark:text-gray-300 text-xs tabular-nums">
                        {bot.h2h_avg_wr != null ? `${(bot.h2h_avg_wr * 100).toFixed(1)}%` : "—"}
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
                          variant={
                            bot.confidence === "very_confident" ? "success" :
                            bot.confidence === "confident" ? "warning" :
                            bot.confidence === "uncertain" ? "warning" : "error"
                          }
                          size="sm"
                        >
                          {{
                            very_confident: "高",
                            confident: "中",
                            uncertain: "低",
                            very_uncertain: "极低",
                          }[bot.confidence] || bot.confidence}
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
