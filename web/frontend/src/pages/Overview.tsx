import { useEffect, useState, useMemo } from "react";
import { Link } from "react-router";
import { useRatings, useMatchStats, useDaemonStatus, useBots, useRecentMatches, useH2H, useGenerations, useBotStats } from "../context/DataProvider";
import { api } from "../api/client";
import { controlApi, type ControlStatus } from "../api/control";
import type { PipelineCheckpoint } from "../api/types";
import PageMeta from "../components/common/PageMeta";

function ConfidenceBadge({ level }: { level: string }) {
  const colors: Record<string, string> = {
    very_confident: "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400",
    confident: "bg-yellow-100 text-yellow-700 dark:bg-yellow-900/30 dark:text-yellow-400",
    uncertain: "bg-orange-100 text-orange-700 dark:bg-orange-900/30 dark:text-orange-400",
    very_uncertain: "bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400",
  };
  const labels: Record<string, string> = {
    very_confident: "高置信",
    confident: "中置信",
    uncertain: "低置信",
    very_uncertain: "极低置信",
  };
  return (
    <span className={`px-2 py-0.5 rounded text-xs font-medium ${colors[level] || colors.uncertain}`}>
      {labels[level] || level}
    </span>
  );
}

function StatCard({ title, value, subtitle }: { title: string; value: string | number; subtitle?: string }) {
  return (
    <div className="rounded-2xl border border-gray-200 bg-white p-5 dark:border-gray-800 dark:bg-white/[0.03]">
      <p className="text-sm text-gray-500 dark:text-gray-400">{title}</p>
      <p className="mt-2 text-2xl font-semibold text-gray-800 dark:text-white">{value}</p>
      {subtitle && <p className="mt-1 text-xs text-gray-400">{subtitle}</p>}
    </div>
  );
}

function DaemonStatusWidget() {
  const daemon = useDaemonStatus();
  const [toggling, setToggling] = useState(false);

  if (!daemon) return null;

  const colors: Record<string, string> = {
    active: "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400",
    recent: "bg-yellow-100 text-yellow-700 dark:bg-yellow-900/30 dark:text-yellow-400",
    idle: "bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400",
    unknown: "bg-gray-100 text-gray-500",
  };
  const dotColors: Record<string, string> = {
    active: "bg-green-500 animate-pulse",
    recent: "bg-yellow-500",
    idle: "bg-red-500",
    unknown: "bg-gray-400",
  };
  const statusLabels: Record<string, string> = {
    active: "活跃",
    recent: "最近",
    idle: "空闲",
    unknown: "未知",
  };

  const age = daemon.last_update_age_seconds;
  const ageStr = age < 0 ? "从未" : age < 60 ? `${age}秒前` : `${Math.round(age / 60)}分钟前`;

  const handleToggle = async () => {
    setToggling(true);
    try {
      await controlApi.setConfig({ daemon_enabled: !daemon.daemon_enabled });
    } catch {}
    setToggling(false);
  };

  return (
    <div className="rounded-2xl border border-gray-200 bg-white p-5 dark:border-gray-800 dark:bg-white/[0.03]">
      <div className="flex items-center justify-between">
        <p className="text-sm text-gray-500 dark:text-gray-400">评分引擎</p>
        <span className={`px-2 py-0.5 rounded text-xs font-medium flex items-center gap-1.5 ${colors[daemon.status] || colors.unknown}`}>
          <span className={`w-1.5 h-1.5 rounded-full ${dotColors[daemon.status] || dotColors.unknown}`} />
          {statusLabels[daemon.status] || daemon.status}
        </span>
      </div>
      <div className="mt-2 flex items-center justify-between">
        <p className="text-lg font-semibold text-gray-800 dark:text-white">
          {daemon.daemon_enabled ? "已启用" : "已禁用"}
        </p>
        <button
          onClick={handleToggle}
          disabled={toggling}
          className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors focus:outline-none disabled:opacity-50 ${
            daemon.daemon_enabled
              ? "bg-green-500 dark:bg-green-600"
              : "bg-gray-300 dark:bg-gray-600"
          }`}
          title={daemon.daemon_enabled ? "关闭评分引擎" : "启动评分引擎"}
        >
          <span
            className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${
              daemon.daemon_enabled ? "translate-x-6" : "translate-x-1"
            }`}
          />
        </button>
      </div>
      <p className="mt-1 text-xs text-gray-400">最近更新: {ageStr}</p>
    </div>
  );
}

function EvolutionStatusWidget({ status }: { status: ControlStatus | null }) {
  return (
    <div className="rounded-2xl border border-gray-200 bg-white p-5 dark:border-gray-800 dark:bg-white/[0.03]">
      <div className="flex items-center justify-between">
        <p className="text-sm text-gray-500 dark:text-gray-400">进化状态</p>
        <span className={`px-2 py-0.5 rounded text-xs font-medium flex items-center gap-1.5 ${
          status?.running
            ? "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400"
            : "bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400"
        }`}>
          <span className={`w-1.5 h-1.5 rounded-full ${status?.running ? "bg-green-500 animate-pulse" : "bg-gray-400"}`} />
          {status?.running ? "运行中" : "已停止"}
        </span>
      </div>
      <p className="mt-2 text-lg font-semibold text-gray-800 dark:text-white">
        第 {status?.generation_count ?? 0} 代
      </p>
      <p className="mt-1 text-xs text-gray-400">
        v{status?.current_v ?? 0} → v{status?.next_v ?? 0}
      </p>
    </div>
  );
}

function PipelineStageBadge({ checkpoint }: { checkpoint: PipelineCheckpoint | null }) {
  if (!checkpoint) return null;
  const stageLabels: Record<string, string> = {
    prepared: "环境就绪",
    workers_done: "Worker 完成",
    quality_passed: "质量通过",
    reviewed: "审核通过",
    critic_checked: "策略通过",
    verified: "验证完成",
  };
  return (
    <div className="flex items-center gap-2 text-xs">
      <span className="text-gray-500">流水线:</span>
      <span className="px-2 py-0.5 rounded bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300 font-medium">
        v{checkpoint.next_v} ← v{checkpoint.source_v}
      </span>
      <span className="text-gray-600 dark:text-gray-300">{stageLabels[checkpoint.stage] || checkpoint.stage}</span>
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
      if (!best || wr > Math.abs(best.wr - 0.5)) {
        best = { key, wr: val.win_rate, games: val.games };
      }
    }
    return best;
  }, [h2h]);

  const latestGen = generations.length > 0 ? generations[generations.length - 1] : null;
  const recentMatches = matches.slice(0, 5);

  return (
    <div className="space-y-3">
      {/* Recent matches */}
      <div className="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-white/[0.03]">
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
                  <span className={m.bot0_wins > m.bot1_wins ? "text-green-600 font-medium" : ""}>{m.bot0_wins}</span>
                  <span>:</span>
                  <span className={m.bot1_wins > m.bot0_wins ? "text-green-600 font-medium" : ""}>{m.bot1_wins}</span>
                </span>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Top rivalry */}
      {topRivalry && (
        <div className="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-white/[0.03]">
          <h4 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-2">最悬殊对战</h4>
          <div className="text-xs">
            <p className="text-gray-600 dark:text-gray-300 font-medium">
              {topRivalry.key.split(" vs ").map((n) => n.replace("claude_", "")).join(" vs ")}
            </p>
            <p className="text-gray-500 mt-1">
              胜率 {(topRivalry.wr * 100).toFixed(0)}% · {topRivalry.games} 场
            </p>
          </div>
        </div>
      )}

      {/* Latest generation */}
      {latestGen && (
        <div className="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-800 dark:bg-white/[0.03]">
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

export default function Overview() {
  const ratings = useRatings();
  const stats = useMatchStats();
  const bots = useBots();
  const botStats = useBotStats();
  const [summary, setSummary] = useState<Record<string, { peak_rating: number; current_rating: number; trend: number; periods: number; peak_h2h_avg_wr?: number; current_h2h_avg_wr?: number; wr_trend?: number }>>({});
  const [controlStatus, setControlStatus] = useState<ControlStatus | null>(null);
  const [checkpoint, setCheckpoint] = useState<PipelineCheckpoint | null>(null);

  useEffect(() => {
    api.historySummary().then(setSummary).catch(() => {});
    const id = setInterval(() => {
      api.historySummary().then(setSummary).catch(() => {});
    }, 15000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    const refresh = () => {
      controlApi.status().then(setControlStatus).catch(() => {});
      api.pipelineCheckpoint().then(setCheckpoint).catch(() => {});
    };
    refresh();
    const id = setInterval(refresh, 5000);
    return () => clearInterval(id);
  }, []);

  if (ratings.length === 0) {
    return <div className="p-6 text-gray-500 dark:text-gray-400">加载中...</div>;
  }

  return (
    <>
      <PageMeta title="总览 — Bot 自进化" description="Bot 种群概览" />
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-5 md:gap-6">
        <StatCard title="活跃 Bot" value={bots.active.length} />
        <StatCard title="总对局数" value={(stats?.total_games ?? 0).toLocaleString()} />
        <StatCard title="对局组合" value={stats?.total_pairs ?? 0} />
        <StatCard
          title="最活跃组合"
          value={stats?.most_active_pair ?? "—"}
          subtitle={`${stats?.most_active_count ?? 0} 场对局`}
        />
        <DaemonStatusWidget />
      </div>

      {/* Evolution status + Recent activity row */}
      <div className="mt-6 grid grid-cols-1 gap-4 md:grid-cols-2 md:gap-6">
        <div className="space-y-4">
          <EvolutionStatusWidget status={controlStatus} />
          <PipelineStageBadge checkpoint={checkpoint} />
        </div>
        <RecentActivityCard />
      </div>

      <div className="mt-6 rounded-2xl border border-gray-200 bg-white dark:border-gray-800 dark:bg-white/[0.03]">
        <div className="px-5 py-4 border-b border-gray-100 dark:border-gray-800">
          <h3 className="text-lg font-semibold text-gray-800 dark:text-white">排行榜</h3>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-100 dark:border-gray-800 text-left text-gray-500 dark:text-gray-400">
                <th className="px-5 py-3 font-medium">排名</th>
                <th className="px-5 py-3 font-medium">Bot</th>
                <th className="px-5 py-3 font-medium">评分</th>
                <th className="px-5 py-3 font-medium">RD</th>
                <th className="px-5 py-3 font-medium">波动率</th>
                <th className="px-5 py-3 font-medium">保守评分</th>
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
                return (
                  <tr key={bot.name} className="border-b border-gray-50 dark:border-gray-800/50 hover:bg-gray-50 dark:hover:bg-gray-800/30">
                    <td className="px-5 py-3 font-medium text-gray-800 dark:text-gray-200">#{bot.rank}</td>
                    <td className="px-5 py-3 font-medium">
                      <Link to="/bots" className="text-gray-800 dark:text-white hover:text-blue-600 dark:hover:text-blue-400 transition-colors">
                        {bot.name.replace("claude_", "v")}
                      </Link>
                    </td>
                    <td className="px-5 py-3 text-gray-600 dark:text-gray-300" title={s ? `峰值: ${s.peak_rating.toFixed(1)}` : undefined}>{bot.rating.toFixed(1)}</td>
                    <td className="px-5 py-3 text-gray-600 dark:text-gray-300">{bot.rd.toFixed(1)}</td>
                    <td className="px-5 py-3 text-gray-400 text-xs">{bot.sigma.toFixed(4)}</td>
                    <td className="px-5 py-3 text-gray-600 dark:text-gray-300">{bot.conservative_rating.toFixed(1)}</td>
                    <td className="px-5 py-3 text-gray-600 dark:text-gray-300">
                      {bot.h2h_avg_wr != null ? `${(bot.h2h_avg_wr * 100).toFixed(1)}%` : "—"}
                    </td>
                    <td className="px-5 py-3 text-gray-600 dark:text-gray-300">{bot.games ?? "—"}</td>
                    <td className="px-5 py-3 text-xs font-mono text-gray-500">
                      {(() => { const bs = botStats[bot.name]; return bs ? `${bs.wins}/${bs.losses}/${bs.draws}` : "—"; })()}
                    </td>
                    <td className="px-5 py-3 text-xs">
                      {s && s.wr_trend != null ? (
                        <span className={s.wr_trend > 0 ? "text-green-600" : s.wr_trend < 0 ? "text-red-600" : "text-gray-400"}>
                          {s.wr_trend > 0 ? "↑" : s.wr_trend < 0 ? "↓" : "→"} {(s.wr_trend * 100).toFixed(1)}pp
                        </span>
                      ) : s ? (
                        <span className={s.trend > 0 ? "text-green-600" : s.trend < 0 ? "text-red-600" : "text-gray-400"}>
                          {s.trend > 0 ? "↑" : s.trend < 0 ? "↓" : "→"} {s.trend > 0 ? "+" : ""}{s.trend.toFixed(1)}
                        </span>
                      ) : "—"}
                    </td>
                    <td className="px-5 py-3"><ConfidenceBadge level={bot.confidence} /></td>
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
