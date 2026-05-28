import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { BotRating, MatchStats } from "../api/types";
import PageMeta from "../components/common/PageMeta";

function ConfidenceBadge({ level }: { level: string }) {
  const colors: Record<string, string> = {
    very_confident: "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400",
    confident: "bg-yellow-100 text-yellow-700 dark:bg-yellow-900/30 dark:text-yellow-400",
    uncertain: "bg-orange-100 text-orange-700 dark:bg-orange-900/30 dark:text-orange-400",
    very_uncertain: "bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400",
  };
  return (
    <span className={`px-2 py-0.5 rounded text-xs font-medium ${colors[level] || colors.uncertain}`}>
      {level.replace("_", " ")}
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

export default function Overview() {
  const [ratings, setRatings] = useState<BotRating[]>([]);
  const [stats, setStats] = useState<MatchStats | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([api.ratings(), api.matchStats()])
      .then(([r, s]) => {
        setRatings(r);
        setStats(s);
      })
      .catch(console.error)
      .finally(() => setLoading(false));
  }, []);

  if (loading) {
    return <div className="p-6 text-gray-500 dark:text-gray-400">Loading...</div>;
  }

  return (
    <>
      <PageMeta title="Overview — Evolution Dashboard" description="Bot population overview" />
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4 md:gap-6">
        <StatCard title="Active Bots" value={ratings.length} />
        <StatCard title="Total Games" value={(stats?.total_games ?? 0).toLocaleString()} />
        <StatCard title="Rating Periods" value={stats?.total_periods ?? 0} />
        <StatCard
          title="Most Active Pair"
          value={stats?.most_active_pair ?? "—"}
          subtitle={`${stats?.most_active_count ?? 0} matches`}
        />
      </div>

      <div className="mt-6 rounded-2xl border border-gray-200 bg-white dark:border-gray-800 dark:bg-white/[0.03]">
        <div className="px-5 py-4 border-b border-gray-100 dark:border-gray-800">
          <h3 className="text-lg font-semibold text-gray-800 dark:text-white">Leaderboard</h3>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-100 dark:border-gray-800 text-left text-gray-500 dark:text-gray-400">
                <th className="px-5 py-3 font-medium">Rank</th>
                <th className="px-5 py-3 font-medium">Bot</th>
                <th className="px-5 py-3 font-medium">Rating</th>
                <th className="px-5 py-3 font-medium">RD</th>
                <th className="px-5 py-3 font-medium">Conservative</th>
                <th className="px-5 py-3 font-medium">Confidence</th>
                <th className="px-5 py-3 font-medium">Last Updated</th>
              </tr>
            </thead>
            <tbody>
              {ratings.map((bot) => (
                <tr key={bot.name} className="border-b border-gray-50 dark:border-gray-800/50 hover:bg-gray-50 dark:hover:bg-gray-800/30">
                  <td className="px-5 py-3 font-medium text-gray-800 dark:text-gray-200">#{bot.rank}</td>
                  <td className="px-5 py-3 font-medium text-gray-800 dark:text-white">{bot.name.replace("claude_", "v")}</td>
                  <td className="px-5 py-3 text-gray-600 dark:text-gray-300">{bot.rating.toFixed(1)}</td>
                  <td className="px-5 py-3 text-gray-600 dark:text-gray-300">{bot.rd.toFixed(1)}</td>
                  <td className="px-5 py-3 text-gray-600 dark:text-gray-300">{bot.conservative_rating.toFixed(1)}</td>
                  <td className="px-5 py-3"><ConfidenceBadge level={bot.confidence} /></td>
                  <td className="px-5 py-3 text-gray-400 text-xs">{bot.last_period ? new Date(bot.last_period).toLocaleString() : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}
