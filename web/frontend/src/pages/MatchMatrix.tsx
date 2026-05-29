import { useMemo } from "react";
import Chart from "react-apexcharts";
import type { ApexOptions } from "apexcharts";
import { useMatchMatrix } from "../context/DataProvider";
import PageMeta from "../components/common/PageMeta";

export default function MatchMatrix() {
  const data = useMatchMatrix();

  const { series, options } = useMemo(() => {
    if (!data || !data.bots.length) return { series: [], options: {} };

    const bots = data.bots.map((b) => b.replace("claude_", "v"));
    const isH2H = data.source === "h2h";

    const series = data.bots.map((botName, i) => ({
      name: botName.replace("claude_", "v"),
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
        theme: "dark",
        y: {
          formatter: (val: number | null) => {
            if (val === null) return "无数据";
            if (isH2H) return `${(val * 100).toFixed(0)}% 胜率`;
            return `${val} 场对局`;
          },
        },
      },
      stroke: { width: 1, colors: ["#fff"] },
    };

    return { series, options };
  }, [data]);

  if (!data || !data.bots.length) {
    return <div className="p-6 text-gray-500 dark:text-gray-400">加载中...</div>;
  }

  return (
    <>
      <PageMeta title="对局矩阵 — Bot 自进化" description="Bot 间的 Head-to-Head 胜率" />
      <div className="rounded-2xl border border-gray-200 bg-white dark:border-gray-800 dark:bg-white/[0.03]">
        <div className="px-5 py-4 border-b border-gray-100 dark:border-gray-800">
          <h3 className="text-lg font-semibold text-gray-800 dark:text-white">Head-to-Head 胜率矩阵</h3>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
            {data.source === "h2h"
              ? "每格表示行 Bot 对列 Bot 的胜率（蓝=强，红=弱）"
              : "所有 Bot 之间的一对一对局次数"}
          </p>
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
