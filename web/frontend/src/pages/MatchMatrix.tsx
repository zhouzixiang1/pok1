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
    const series = data.bots.map((botName, i) => ({
      name: botName.replace("claude_", "v"),
      data: data.bots.map((_, j) => ({
        x: bots[j],
        y: data.matrix[i][j],
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
      colors: ["#465FFF"],
      plotOptions: {
        heatmap: {
          radius: 2,
          shadeIntensity: 0.8,
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
          formatter: (val: number) => `${val} 场对局`,
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
      <PageMeta title="对局矩阵 — Bot 自进化" description="Bot 间的对局次数" />
      <div className="rounded-2xl border border-gray-200 bg-white dark:border-gray-800 dark:bg-white/[0.03]">
        <div className="px-5 py-4 border-b border-gray-100 dark:border-gray-800">
          <h3 className="text-lg font-semibold text-gray-800 dark:text-white">对局矩阵</h3>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
            所有 Bot 之间的一对一对局次数
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
