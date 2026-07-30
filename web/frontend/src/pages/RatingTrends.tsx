import { useState, useMemo } from "react";
import Chart from "react-apexcharts";
import type { ApexOptions } from "apexcharts";
import { useHistory, useControlStatusValue } from "../context/DataProvider";
import PageMeta from "../components/common/PageMeta";
import { SegmentedControl } from "../components/shared/SegmentedControl";
import { EmptyState } from "../components/shared/EmptyState";
import { EpochAuthorityStatus } from "../components/evolution/EpochAuthorityStatus";
import { compactBotName } from "../lib/utils";

const COLORS = [
  "#465FFF", "#9CB9FF", "#F59E0B", "#10B981", "#EF4444",
  "#8B5CF6", "#EC4899", "#06B6D4", "#F97316", "#84CC16",
  "#6366F1", "#14B8A6", "#F43F5E", "#A855F7", "#22D3EE",
  "#FB923C", "#34D399",
];

type MetricMode = "glicko" | "h2h_wr";

export default function RatingTrends() {
  const history = useHistory();
  const { status, loading, error } = useControlStatusValue();
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
          name: compactBotName(name),
          type: "line" as const,
          data: history.map((e) => e.ratings[name]?.r ?? null),
          color: COLORS[i % COLORS.length],
        });
      });
    } else {
      names.forEach((name, i) => {
        series.push({
          name: compactBotName(name),
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

  const options: ApexOptions = useMemo(
    () => ({
      chart: {
        fontFamily: "Outfit, sans-serif",
        height: 500,
        type: "line",
        toolbar: { show: true },
        background: "transparent",
        animations: { enabled: false },
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
        borderColor: undefined,
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
          formatter: metric === "h2h_wr" ? (val: number) => `${(val * 100).toFixed(1)}%` : undefined,
        },
        title: { text: yTitle, style: { fontSize: "12px" } },
      },
      theme: { mode: "light" },
    }),
    [categories, showConfidence, metric, yTitle, names]
  );

  if (!status?.epoch_initialized || history.length === 0) {
    const emptyMessage = !status?.epoch_initialized
      ? "epoch 尚未初始化；旧评分趋势已从权威视图移除。"
      : status.active_bots.length === 0
        ? "当前严格发布池为空；尚无评分历史。"
        : "等待首个同发布池 evaluation cycle；不会显示旧 epoch 或默认评分趋势。";
    return (
      <>
        <PageMeta title="评分趋势 — Bot 自进化" description="严格 epoch 历史评分趋势" />
        <EpochAuthorityStatus status={status} loading={loading} error={error} compact className="mb-4" />
        <div className="rounded-2xl border border-gray-200 bg-white dark:border-border-subtle dark:bg-surface-1">
          <EmptyState message={emptyMessage} />
        </div>
      </>
    );
  }

  return (
    <>
      <PageMeta title="评分趋势 — Bot 自进化" description="历史评分趋势" />
      <EpochAuthorityStatus status={status} loading={loading} error={error} compact className="mb-4" />
      <div className="rounded-2xl border border-gray-200 bg-white dark:border-border-subtle dark:bg-surface-1">
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-100 dark:border-border-subtle">
          <h3 className="text-lg font-semibold text-gray-800 dark:text-white">评分趋势</h3>
          <div className="flex items-center gap-4">
            <SegmentedControl
              value={metric}
              onChange={(v) => setMetric(v as MetricMode)}
              options={[{ value: "h2h_wr", label: "H2H 胜率" }, { value: "glicko", label: "Glicko 评分" }]}
            />
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
              当前周期尚无 H2H 胜率历史；等待同发布池 evaluation cycle 写入权威数据。
            </div>
          ) : (
            <Chart options={options} series={series} type="line" height={500} />
          )}
        </div>
      </div>
    </>
  );
}
