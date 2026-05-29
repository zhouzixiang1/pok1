import { useEffect, useState, useMemo } from "react";
import Chart from "react-apexcharts";
import type { ApexOptions } from "apexcharts";
import { api } from "../api/client";
import type { HistoryEntry } from "../api/types";
import PageMeta from "../components/common/PageMeta";

const COLORS = [
  "#465FFF", "#9CB9FF", "#F59E0B", "#10B981", "#EF4444",
  "#8B5CF6", "#EC4899", "#06B6D4", "#F97316", "#84CC16",
  "#6366F1", "#14B8A6", "#F43F5E", "#A855F7", "#22D3EE",
  "#FB923C", "#34D399",
];

export default function RatingTrends() {
  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [showConfidence, setShowConfidence] = useState(false);

  useEffect(() => {
    api.history([], "full")
      .then(setHistory)
      .catch(console.error)
      .finally(() => setLoading(false));
  }, []);

  const { series, categories } = useMemo(() => {
    if (!history.length) return { series: [] as ApexAxisChartSeries, categories: [] as string[] };

    const names = Object.keys(history[history.length - 1]?.ratings || {}).sort(
      (a, b) => {
        const na = parseInt(a.match(/\d+/)?.[0] || "0");
        const nb = parseInt(b.match(/\d+/)?.[0] || "0");
        return na - nb;
      }
    );

    const categories = history.map((e) => `周期${e.period}`);

    const series: ApexAxisChartSeries = [];
    if (showConfidence) {
      names.forEach((name, i) => {
        series.push({
          name: `${name} 区间`,
          type: "rangeArea" as const,
          data: history.map((e) => {
            const r = e.ratings[name];
            return r ? [r.r - 2 * r.rd, r.r + 2 * r.rd] : [0, 0];
          }),
          color: COLORS[i % COLORS.length],
        });
      });
    }
    names.forEach((name, i) => {
      series.push({
        name: name.replace("claude_", "v"),
        type: "line" as const,
        data: history.map((e) => e.ratings[name]?.r ?? null),
        color: COLORS[i % COLORS.length],
      });
    });

    return { series, categories };
  }, [history, showConfidence]);

  const options: ApexOptions = useMemo(
    () => ({
      chart: {
        fontFamily: "Outfit, sans-serif",
        height: 500,
        type: "line",
        toolbar: { show: true },
        background: "transparent",
      },
      stroke: {
        width: showConfidence ? [0, 2] : 2,
        curve: "smooth",
      },
      fill: {
        type: showConfidence ? ["solid", "solid"] : "solid",
        opacity: showConfidence ? [0.15, 1] : 1,
      },
      markers: { size: 0, hover: { size: 4 } },
      dataLabels: { enabled: false },
      legend: {
        show: true,
        position: "bottom",
        horizontalAlign: "left",
        fontSize: "11px",
      },
      grid: {
        borderColor: "#e5e7eb",
        strokeDashArray: 3,
        xaxis: { lines: { show: false } },
        yaxis: { lines: { show: true } },
      },
      tooltip: {
        theme: "dark",
        x: { show: true },
      },
      xaxis: {
        categories,
        tickAmount: 20,
        labels: { style: { fontSize: "10px" } },
        axisBorder: { show: false },
        axisTicks: { show: false },
      },
      yaxis: {
        labels: {
          style: { fontSize: "12px", colors: ["#6B7280"] },
        },
        title: { text: "Glicko-2 评分", style: { fontSize: "12px" } },
      },
      theme: { mode: "light" },
    }),
    [categories, showConfidence]
  );

  if (loading) {
    return <div className="p-6 text-gray-500 dark:text-gray-400">加载中...</div>;
  }

  return (
    <>
      <PageMeta title="评分趋势 — 进化仪表盘" description="历史评分趋势" />
      <div className="rounded-2xl border border-gray-200 bg-white dark:border-gray-800 dark:bg-white/[0.03]">
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100 dark:border-gray-800">
          <h3 className="text-lg font-semibold text-gray-800 dark:text-white">评分趋势</h3>
          <label className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400 cursor-pointer">
            <input
              type="checkbox"
              checked={showConfidence}
              onChange={(e) => setShowConfidence(e.target.checked)}
              className="rounded"
            />
            置信带 (r ± 2×rd)
          </label>
        </div>
        <div className="p-5">
          <Chart options={options} series={series} type="line" height={500} />
        </div>
      </div>
    </>
  );
}
