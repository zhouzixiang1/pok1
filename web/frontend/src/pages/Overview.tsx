import { useEffect, useState } from "react";
import { Link } from "react-router";
import { useRatings, useMatchStats, useDaemonStatus } from "../context/DataProvider";
import { api } from "../api/client";
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

  return (
    <div className="rounded-2xl border border-gray-200 bg-white p-5 dark:border-gray-800 dark:bg-white/[0.03]">
      <div className="flex items-center justify-between">
        <p className="text-sm text-gray-500 dark:text-gray-400">评分引擎</p>
        <span className={`px-2 py-0.5 rounded text-xs font-medium flex items-center gap-1.5 ${colors[daemon.status] || colors.unknown}`}>
          <span className={`w-1.5 h-1.5 rounded-full ${dotColors[daemon.status] || dotColors.unknown}`} />
          {statusLabels[daemon.status] || daemon.status}
        </span>
      </div>
      <p className="mt-2 text-lg font-semibold text-gray-800 dark:text-white">
        {daemon.daemon_enabled ? "已启用" : "已禁用"}
      </p>
      <p className="mt-1 text-xs text-gray-400">最近更新: {ageStr}</p>
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
  const [summary, setSummary] = useState<Record<string, { peak_rating: number; current_rating: number; trend: number; periods: number }>>({});

  useEffect(() => {
    api.historySummary().then(setSummary).catch(() => {});
  }, []);

  if (ratings.length === 0) {
    return <div className="p-6 text-gray-500 dark:text-gray-400">加载中...</div>;
  }

  return (
    <>
      <PageMeta title="总览 — Bot 自进化" description="Bot 种群概览" />
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-5 md:gap-6">
        <StatCard title="活跃 Bot" value={ratings.length} />
        <StatCard title="总对局数" value={(stats?.total_games ?? 0).toLocaleString()} />
        <StatCard title="对局组合" value={stats?.total_pairs ?? 0} />
        <StatCard
          title="最活跃组合"
          value={stats?.most_active_pair ?? "—"}
          subtitle={`${stats?.most_active_count ?? 0} 场对局`}
        />
        <DaemonStatusWidget />
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
                <th className="px-5 py-3 font-medium">胜率</th>
                <th className="px-5 py-3 font-medium">场数</th>
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
                      {bot.win_rate != null ? `${(bot.win_rate * 100).toFixed(1)}%` : "—"}
                    </td>
                    <td className="px-5 py-3 text-gray-600 dark:text-gray-300">{bot.games ?? "—"}</td>
                    <td className="px-5 py-3 text-xs">
                      {s ? (
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
