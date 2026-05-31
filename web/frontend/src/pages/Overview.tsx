import { useEffect, useState, useMemo } from "react";
import { Link } from "react-router";
import { useRatings, useMatchStats, useDaemonStatus, useBots, useRecentMatches, useH2H, useGenerations, useBotStats } from "../context/DataProvider";
import { api } from "../api/client";
import { controlApi, type ControlStatus } from "../api/control";
import type { PipelineCheckpoint } from "../api/types";
import PageMeta from "../components/common/PageMeta";
import { MetricCard } from "../components/shared/MetricCard";
import { Badge } from "../components/shared/Badge";
import { Skeleton } from "../components/shared/Skeleton";
import { PipelineStepper } from "../components/evolution/PipelineStatus";
import { cn } from "../lib/utils";

function DaemonStatusWidget() {
  const daemon = useDaemonStatus();
  const [toggling, setToggling] = useState(false);

  if (!daemon) return <MetricCard label="评分引擎" value="—" loading />;

  const statusVariant = daemon.status === "active" ? "success" as const : daemon.status === "idle" ? "error" as const : "warning" as const;
  const statusLabels: Record<string, string> = { active: "活跃", recent: "最近", idle: "空闲", unknown: "未知" };
  const age = daemon.last_update_age_seconds;
  const ageStr = age < 0 ? "从未" : age < 60 ? `${age}秒前` : `${Math.round(age / 60)}分钟前`;

  const handleToggle = async () => {
    setToggling(true);
    try { await controlApi.setConfig({ daemon_enabled: !daemon.daemon_enabled }); } catch {}
    setToggling(false);
  };

  return (
    <div className="rounded-xl border border-gray-200 bg-white px-4 py-3 dark:border-gray-800 dark:bg-white/[0.04]">
      <div className="flex items-center justify-between">
        <p className="text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">评分引擎</p>
        <Badge variant={statusVariant} size="sm" pulse={daemon.status === "active"}>
          {statusLabels[daemon.status] || daemon.status}
        </Badge>
      </div>
      <div className="mt-1 flex items-center justify-between">
        <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
          {daemon.daemon_enabled ? "已启用" : "已禁用"}
        </span>
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
      </div>
      <p className="mt-0.5 text-[10px] text-gray-400">{ageStr}</p>
    </div>
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
      <div className="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-white/[0.04]">
        <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-3">最近对局</h4>
        {recentMatches.length === 0 ? (
          <p className="text-xs text-gray-400">暂无对局记录</p>
        ) : (
          <div className="space-y-2">
            {recentMatches.map((m) => (
              <div key={m.id} className="flex items-center gap-2 text-xs">
                <span className="text-gray-600 dark:text-gray-300 truncate">
                  {m.bot0.replace("claude_", "")} vs {m.bot1.replace("claude_", "")}
                </span>
                <span className="ml-auto flex gap-2 text-gray-500 font-mono shrink-0">
                  <span className={m.bot0_wins > m.bot1_wins ? "text-success-600 font-medium" : ""}>{m.bot0_wins}</span>
                  <span>:</span>
                  <span className={m.bot1_wins > m.bot0_wins ? "text-success-600 font-medium" : ""}>{m.bot1_wins}</span>
                </span>
              </div>
            ))}
          </div>
        )}
      </div>

      {topRivalry && (
        <div className="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-white/[0.04]">
          <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-2">最悬殊对战</h4>
          <div className="text-xs">
            <p className="text-gray-600 dark:text-gray-300 font-medium">
              {topRivalry.key.split(" vs ").map((n) => n.replace("claude_", "")).join(" vs ")}
            </p>
            <p className="text-gray-500 mt-1">胜率 {(topRivalry.wr * 100).toFixed(0)}% · {topRivalry.games} 场</p>
          </div>
        </div>
      )}

      {latestGen && (
        <div className="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-white/[0.04]">
          <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-2">最新代次</h4>
          <div className="flex items-center gap-2 text-xs">
            <span className="text-gray-800 dark:text-gray-200 font-medium">{latestGen.version}</span>
            <span className="text-gray-400">{latestGen.files.length} 个日志文件</span>
          </div>
        </div>
      )}
    </div>
  );
}

function formatLastPeriod(val: string): string {
  if (!val) return "—";
  const parsed = new Date(val);
  if (isNaN(parsed.getTime())) return val;
  return parsed.toLocaleString();
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
  const botStats = useBotStats();
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
  const topQuartile = ratings.length > 4 ? ratings[Math.floor(ratings.length / 4) - 1] : ratings[0];
  const bottomQuartile = ratings.length > 4 ? ratings[Math.ceil(ratings.length * 3 / 4) - 1] : ratings[ratings.length - 1];

  return (
    <>
      <PageMeta title="总览 — Bot 自进化" description="Bot 种群概览" />

      {/* Metric strip */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <MetricCard label="活跃 Bot" value={bots.active.length} />
        <MetricCard label="总对局数" value={(stats?.total_games ?? 0).toLocaleString()} />
        <MetricCard
          label="进化代次"
          value={`第 ${controlStatus?.generation_count ?? 0} 代`}
          loading={!controlStatus}
        />
        <DaemonStatusWidget />
      </div>

      {/* Evolution status + Pipeline + Recent activity */}
      <div className="mt-6 grid grid-cols-1 gap-4 md:grid-cols-2 md:gap-6">
        <div className="space-y-4">
          {/* Evolution status */}
          <div className="rounded-2xl border border-gray-200 bg-white p-5 dark:border-gray-800 dark:bg-white/[0.04]">
            <div className="flex items-center justify-between mb-2">
              <p className="text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">进化状态</p>
              <Badge variant={controlStatus?.running ? "success" : "neutral"} size="sm" pulse={controlStatus?.running}>
                {controlStatus?.running ? "运行中" : "已停止"}
              </Badge>
            </div>
            <p className="text-2xl font-semibold text-gray-900 dark:text-white">
              第 {controlStatus?.generation_count ?? 0} 代
            </p>
            <p className="mt-1 text-xs text-gray-400">
              v{controlStatus?.current_v ?? 0} → v{controlStatus?.next_v ?? 0}
            </p>
            {/* Pipeline stepper */}
            {checkpoint && (
              <div className="mt-3 pt-3 border-t border-gray-100 dark:border-gray-800">
                <PipelineStepper checkpoint={checkpoint} />
              </div>
            )}
          </div>
        </div>
        <RecentActivityCard />
      </div>

      {/* Leaderboard */}
      <div className="mt-6 rounded-2xl border border-gray-200 bg-white dark:border-gray-800 dark:bg-white/[0.04] overflow-hidden">
        <div className="px-5 py-4 border-b border-gray-100 dark:border-gray-800">
          <h3 className="text-lg font-semibold text-gray-800 dark:text-white">排行榜</h3>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-100 dark:border-gray-800 text-left text-gray-500 dark:text-gray-400">
                <th className="px-5 py-3 font-medium w-16">排名</th>
                <th className="px-5 py-3 font-medium">Bot</th>
                <th className="px-5 py-3 font-medium">评分</th>
                <th className="px-5 py-3 font-medium">RD</th>
                <th className="px-5 py-3 font-medium">H2H 胜率</th>
                <th className="px-5 py-3 font-medium">场数</th>
                <th className="px-5 py-3 font-medium">胜/负/平</th>
                <th className="px-5 py-3 font-medium">趋势</th>
                <th className="px-5 py-3 font-medium">置信度</th>
                <th className="px-5 py-3 font-medium">最后更新</th>
              </tr>
            </thead>
            <tbody>
              {ratings.map((bot) => {
                const s = summary[bot.name];
                const ratingPct = ((bot.rating - minRating) / ratingRange) * 100;
                const tierBorder = bot.rating >= (topQuartile?.rating ?? 0) ? "border-l-2 border-l-success-500"
                  : bot.rating <= (bottomQuartile?.rating ?? 0) ? "border-l-2 border-l-error-400"
                  : "border-l-2 border-l-warning-400";

                // Build sparkline from trend data
                const sparkData = s ? [s.peak_rating, (s.peak_rating + s.current_rating) / 2, s.current_rating] : [];
                const sparkColor = s && s.trend > 0 ? "#12b76a" : s && s.trend < 0 ? "#f04438" : "#98a2b3";

                return (
                  <tr key={bot.name} className={cn(
                    "border-b border-gray-50 dark:border-gray-800/50 hover:bg-gray-50 dark:hover:bg-gray-800/30 transition-colors",
                    tierBorder,
                  )}>
                    <td className="px-5 py-3">
                      {bot.rank <= 3 ? (
                        <span className={cn(
                          "inline-flex items-center justify-center w-6 h-6 rounded-full text-[10px] font-bold",
                          bot.rank === 1 && "bg-amber-400 text-amber-900",
                          bot.rank === 2 && "bg-gray-300 text-gray-700",
                          bot.rank === 3 && "bg-orange-400 text-orange-900",
                        )}>
                          {bot.rank}
                        </span>
                      ) : (
                        <span className="text-gray-500 font-medium">#{bot.rank}</span>
                      )}
                    </td>
                    <td className="px-5 py-3 font-medium">
                      <Link to="/bots" className="text-gray-800 dark:text-white hover:text-brand-600 dark:hover:text-brand-400 transition-colors">
                        {bot.name.replace("claude_", "")}
                      </Link>
                    </td>
                    <td className="px-5 py-3">
                      <div className="flex items-center gap-2">
                        <span className="font-mono font-semibold text-gray-700 dark:text-gray-200" title={s ? `峰值: ${s.peak_rating.toFixed(1)}` : undefined}>
                          {bot.rating.toFixed(1)}
                        </span>
                        <div className="w-16 h-1.5 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
                          <div className="h-full bg-brand-500 rounded-full transition-all" style={{ width: `${ratingPct}%` }} />
                        </div>
                        {sparkData.length >= 2 && <Sparkline data={sparkData} color={sparkColor} />}
                      </div>
                    </td>
                    <td className="px-5 py-3 text-gray-600 dark:text-gray-300 text-xs">{bot.rd.toFixed(1)}</td>
                    <td className="px-5 py-3 text-gray-600 dark:text-gray-300">
                      {bot.h2h_avg_wr != null ? `${(bot.h2h_avg_wr * 100).toFixed(1)}%` : "—"}
                    </td>
                    <td className="px-5 py-3 text-gray-600 dark:text-gray-300">{bot.games ?? "—"}</td>
                    <td className="px-5 py-3 text-xs font-mono text-gray-500">
                      {(() => { const bs = botStats[bot.name]; return bs ? `${bs.wins}/${bs.losses}/${bs.draws}` : "—"; })()}
                    </td>
                    <td className="px-5 py-3">
                      {s && s.wr_trend != null ? (
                        <Badge variant={s.wr_trend > 0 ? "success" : s.wr_trend < 0 ? "error" : "neutral"} size="sm">
                          {s.wr_trend > 0 ? "↑" : s.wr_trend < 0 ? "↓" : "→"} {(Math.abs(s.wr_trend) * 100).toFixed(1)}pp
                        </Badge>
                      ) : s ? (
                        <Badge variant={s.trend > 0 ? "success" : s.trend < 0 ? "error" : "neutral"} size="sm">
                          {s.trend > 0 ? "↑" : s.trend < 0 ? "↓" : "→"} {Math.abs(s.trend).toFixed(1)}
                        </Badge>
                      ) : "—"}
                    </td>
                    <td className="px-5 py-3">
                      <Badge
                        variant={
                          bot.confidence === "very_confident" ? "success" :
                          bot.confidence === "confident" ? "warning" :
                          bot.confidence === "uncertain" ? "warning" : "error"
                        }
                        size="sm"
                      >
                        {{
                          very_confident: "高置信",
                          confident: "中置信",
                          uncertain: "低置信",
                          very_uncertain: "极低置信",
                        }[bot.confidence] || bot.confidence}
                      </Badge>
                    </td>
                    <td className="px-5 py-3 text-gray-400 text-xs">{formatLastPeriod(bot.last_period)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}
