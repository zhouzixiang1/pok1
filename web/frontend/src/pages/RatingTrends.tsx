import { useState, useMemo } from "react";
import Chart from "react-apexcharts";
import type { ApexOptions } from "apexcharts";
import { useHistory } from "../context/DataProvider";
import PageMeta from "../components/common/PageMeta";

const COLORS = [
  "#465FFF", "#9CB9FF", "#F59E0B", "#10B981", "#EF4444",
  "#8B5CF6", "#EC4899", "#06B6D4", "#F97316", "#84CC16",
  "#6366F1", "#14B8A6", "#F43F5E", "#A855F7", "#22D3EE",
  "#FB923C", "#34D399",
];

type MetricMode = "glicko" | "h2h_wr";

export default function RatingTrends() {
  const history = useHistory();
  const [showConfidence, setShowConfidence] = useState(false);
  const [metric, setMetric] = useState<MetricMode>("h2h_wr");

  const hasWrData = useMemo(
    () => history.some((e) => e.win_rates && Object.keys(e.win_rates).length > 0),
    [history]
  );

  const { series, categories, names } = useMemo(() => {
    if (!history.length) return { series: [] as ApexAxisChartSeries, categories: [] as string[], names: [] as string[] };

    const names = Object.keys(history[history.length - 1]?.ratings || {}).sort(
      (a, b) => {
        const na = parseInt(a.match(/\d+/)?.[0] || "0");
        const nb = parseInt(b.match(/\d+/)?.[0] || "0");
        return na - nb;
      }
    );

    const categories = history.map((e) => `周期${e.period}`);

    const series: ApexAxisChartSeries = [];

    if (metric === "glicko" && showConfidence) {
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

    if (metric === "glicko") {
      names.forEach((name, i) => {
        series.push({
          name: name.replace("claude_", ""),
          type: "line" as const,
          data: history.map((e) => e.ratings[name]?.r ?? null),
          color: COLORS[i % COLORS.length],
        });
      });
    } else {
      names.forEach((name, i) => {
        series.push({
          name: name.replace("claude_", ""),
          type: "line" as const,
          data: history.map((e) => {
            const wr = e.win_rates?.[name]?.h2h_avg_wr;
            return wr != null ? wr : null;
          }),
          color: COLORS[i % COLORS.length],
        });
      });
    }

    return { series, categories, names };
  }, [history, showConfidence, metric]);

  const yTitle = metric === "glicko" ? "Glicko-2 评分" : "H2H 平均胜率";
  const yFormatter = metric === "h2h_wr"
    ? (val: number) => `${(val * 100).toFixed(1)}%`
    : undefined;

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
        width: metric === "glicko" && showConfidence ? [...names.map(() => 0), ...names.map(() => 2)] : 2,
        curve: "smooth",
      },
      fill: {
        type: metric === "glicko" && showConfidence ? [...names.map(() => "solid"), ...names.map(() => "solid")] : "solid",
        opacity: metric === "glicko" && showConfidence ? [...names.map(() => 0.15), ...names.map(() => 1)] : 1,
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
          formatter: yFormatter as ((val: number) => string) | undefined,
        },
        title: { text: yTitle, style: { fontSize: "12px" } },
      },
      theme: { mode: "light" },
    }),
    [categories, showConfidence, metric, yTitle, yFormatter, names.length]
  );

  if (history.length === 0) {
    return <div className="p-6 text-gray-500 dark:text-gray-400">加载中...</div>;
  }

  return (
    <>
      <PageMeta title="评分趋势 — Bot 自进化" description="历史评分趋势" />
      <div className="rounded-2xl border border-gray-200 bg-white dark:border-gray-800 dark:bg-white/[0.03]">
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100 dark:border-gray-800">
          <h3 className="text-lg font-semibold text-gray-800 dark:text-white">评分趋势</h3>
          <div className="flex items-center gap-4">
            <div className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400">
              <button
                onClick={() => setMetric("h2h_wr")}
                className={`px-2 py-0.5 rounded ${metric === "h2h_wr" ? "bg-blue-100 dark:bg-blue-900 text-blue-700 dark:text-blue-300 font-medium" : ""}`}
              >
                H2H 胜率
              </button>
              <button
                onClick={() => setMetric("glicko")}
                className={`px-2 py-0.5 rounded ${metric === "glicko" ? "bg-blue-100 dark:bg-blue-900 text-blue-700 dark:text-blue-300 font-medium" : ""}`}
              >
                Glicko 评分
              </button>
            </div>
            {metric === "glicko" && (
              <label className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400 cursor-pointer">
                <input
                  type="checkbox"
                  checked={showConfidence}
                  onChange={(e) => setShowConfidence(e.target.checked)}
                  className="rounded"
                />
                置信带 (r ± 2×rd)
              </label>
            )}
          </div>
        </div>
        <div className="p-5">
          {metric === "h2h_wr" && !hasWrData ? (
            <div className="text-center py-20 text-gray-500 dark:text-gray-400">
              暂无 H2H 胜率历史数据，需等待 daemon 写入新数据周期
            </div>
          ) : (
            <Chart options={options} series={series} type="line" height={500} />
          )}
        </div>
      </div>
    </>
  );
}
