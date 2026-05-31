import { useMemo, useState } from "react";
import Chart from "react-apexcharts";
import type { ApexOptions } from "apexcharts";
import { useMatchMatrix, useH2H } from "../context/DataProvider";
import PageMeta from "../components/common/PageMeta";
import { SegmentedControl } from "../components/shared/SegmentedControl";
import { Skeleton } from "../components/shared/Skeleton";

type ViewMode = "winrate" | "count";

export default function MatchMatrix() {
  const data = useMatchMatrix();
  const h2hRaw = useH2H();
  const [viewMode, setViewMode] = useState<ViewMode>("winrate");

  const { series, options } = useMemo(() => {
    if (!data || !data.bots.length) return { series: [], options: {} };

    const bots = data.bots.map((b) => b.replace("claude_", ""));
    const isH2H = data.source === "h2h";

    if (viewMode === "count" && Object.keys(h2hRaw).length > 0) {
      const gamesMatrix: (number | null)[][] = data.bots.map((_, i) =>
        data.bots.map((__, j) => {
          if (i === j) return null;
          const key1 = `${data.bots[i]} vs ${data.bots[j]}`;
          const key2 = `${data.bots[j]} vs ${data.bots[i]}`;
          const entry = h2hRaw[key1] || h2hRaw[key2];
          return entry ? entry.games : null;
        })
      );

      const series = data.bots.map((botName, i) => ({
        name: botName.replace("claude_", ""),
        data: data.bots.map((_, j) => ({ x: bots[j], y: gamesMatrix[i][j] })),
      }));

      const options: ApexOptions = {
        chart: { fontFamily: "Outfit, sans-serif", height: Math.max(400, bots.length * 32), type: "heatmap", background: "transparent", toolbar: { show: true } },
        dataLabels: { enabled: false },
        plotOptions: {
          heatmap: {
            radius: 2, shadeIntensity: 0.8,
            colorScale: {
              ranges: [
                { from: 0, to: 0, color: "#f3f4f6", name: "无" },
                { from: 1, to: 100, color: "#dbeafe", name: "低" },
                { from: 101, to: 500, color: "#93c5fd", name: "中" },
                { from: 501, to: 1500, color: "#3b82f6", name: "高" },
                { from: 1501, to: 10000, color: "#1d4ed8", name: "极高" },
              ],
            },
          },
        },
        xaxis: { labels: { style: { fontSize: "10px" } }, axisBorder: { show: false }, axisTicks: { show: false } },
        yaxis: { labels: { style: { fontSize: "10px" } } },
        tooltip: {
          custom: ({ seriesIndex, dataPointIndex }: { seriesIndex: number; dataPointIndex: number }) => {
            const rowBot = data!.bots[seriesIndex];
            const colBot = data!.bots[dataPointIndex];
            const key1 = `${rowBot} vs ${colBot}`;
            const key2 = `${colBot} vs ${rowBot}`;
            const entry = h2hRaw[key1] || h2hRaw[key2];
            if (!entry) return '<div style="padding:4px 8px;font-size:12px">无数据</div>';
            const isA = !!h2hRaw[key1];
            const w = isA ? entry.a_wins : entry.b_wins;
            const l = isA ? entry.b_wins : entry.a_wins;
            return `<div style="padding:6px 10px;font-size:12px">
              <div style="font-weight:600">${bots[seriesIndex]} vs ${bots[dataPointIndex]}</div>
              <div style="margin-top:2px">${entry.games} 场 · ${w}胜 ${entry.draws}平 ${l}负</div>
            </div>`;
          },
        },
        stroke: { width: 1, colors: ["#fff"] },
      };
      return { series, options };
    }

    // Default: win rate view
    const series = data.bots.map((botName, i) => ({
      name: botName.replace("claude_", ""),
      data: data.bots.map((_, j) => ({
        x: bots[j],
        y: i === j ? null : data.matrix[i]?.[j] ?? null,
      })),
    }));

    const options: ApexOptions = {
      chart: {
        fontFamily: "Outfit, sans-serif",
        height: Math.max(400, bots.length * 32),
        type: "heatmap",
        background: "transparent",
        toolbar: { show: true },
      },
      dataLabels: { enabled: false },
      plotOptions: {
        heatmap: {
          radius: 2,
          shadeIntensity: 0.8,
          colorScale: {
            ranges: isH2H
              ? [
                  { from: -0.01, to: 0.01, color: "#9ca3af", name: "无数据" },
                  { from: 0.01, to: 0.30, color: "#ef4444", name: "很弱" },
                  { from: 0.30, to: 0.45, color: "#f87171", name: "弱" },
                  { from: 0.45, to: 0.55, color: "#e5e7eb", name: "均势" },
                  { from: 0.55, to: 0.70, color: "#c7d2fe", name: "强" },
                  { from: 0.70, to: 0.85, color: "#818cf8", name: "很强" },
                  { from: 0.85, to: 1.01, color: "#4f46e5", name: "极强" },
                ]
              : [
                  { from: 0, to: 0, color: "#f3f4f6", name: "无" },
                  { from: 1, to: 100, color: "#dbeafe", name: "低" },
                  { from: 101, to: 500, color: "#93c5fd", name: "中" },
                  { from: 501, to: 1500, color: "#3b82f6", name: "高" },
                  { from: 1501, to: 10000, color: "#1d4ed8", name: "极高" },
                ],
          },
        },
      },
      xaxis: {
        labels: { style: { fontSize: "10px" } },
        axisBorder: { show: false },
        axisTicks: { show: false },
      },
      yaxis: {
        labels: { style: { fontSize: "10px" } },
      },
      tooltip: {
        custom: ({ seriesIndex, dataPointIndex }: { seriesIndex: number; dataPointIndex: number }) => {
          const val = data!.matrix[seriesIndex]?.[dataPointIndex];
          if (val == null) return '<div style="padding:4px 8px;font-size:12px">无数据</div>';
          const rowBot = data!.bots[seriesIndex];
          const colBot = data!.bots[dataPointIndex];
          if (!isH2H) return `<div style="padding:6px 10px;font-size:12px">${bots[seriesIndex]} vs ${bots[dataPointIndex]}<br/>${val} 场对局</div>`;
          const key1 = `${rowBot} vs ${colBot}`;
          const key2 = `${colBot} vs ${rowBot}`;
          const entry = h2hRaw[key1] || h2hRaw[key2];
          let extra = "";
          if (entry) {
            const isA = !!h2hRaw[key1];
            const w = isA ? entry.a_wins : entry.b_wins;
            const l = isA ? entry.b_wins : entry.a_wins;
            extra = `<div style="margin-top:2px">${entry.games} 场 · ${w}胜 ${entry.draws}平 ${l}负</div>`;
          }
          return `<div style="padding:6px 10px;font-size:12px">
            <div style="font-weight:600">${bots[seriesIndex]} vs ${bots[dataPointIndex]}</div>
            <div>${(val * 100).toFixed(0)}% 胜率</div>
            ${extra}
          </div>`;
        },
      },
      stroke: { width: 1, colors: ["#fff"] },
    };

    return { series, options };
  }, [data, h2hRaw, viewMode]);

  if (!data || !data.bots.length) {
    return <div className="p-6 space-y-4"><Skeleton className="h-8 w-64" /><Skeleton className="h-[400px] rounded-2xl" /></div>;
  }

  return (
    <>
      <PageMeta title="对局矩阵 — Bot 自进化" description="Bot 间的 Head-to-Head 胜率" />
      <div className="rounded-2xl border border-gray-200 bg-white dark:border-gray-800 dark:bg-white/[0.03]">
        <div className="px-5 py-4 border-b border-gray-100 dark:border-gray-800">
          <div className="flex items-center justify-between">
            <div>
              <h3 className="text-lg font-semibold text-gray-800 dark:text-white">Head-to-Head 胜率矩阵</h3>
              <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
                {viewMode === "winrate"
                  ? "每格表示行 Bot 对列 Bot 的胜率（蓝=强，红=弱）"
                  : "每格表示行 Bot 与列 Bot 之间的对局总数"}
              </p>
            </div>
            <SegmentedControl
              value={viewMode}
              onChange={(v) => setViewMode(v as ViewMode)}
              options={[{ value: "winrate", label: "胜率" }, { value: "count", label: "对局数" }]}
            />
          </div>
        </div>
        <div className="p-5">
          <Chart
            options={options}
            series={series}
            type="heatmap"
            height={Math.max(400, (data?.bots.length ?? 10) * 32)}
          />
        </div>
      </div>
    </>
  );
}
