import { useMemo, useState, useCallback } from "react";
import Chart from "react-apexcharts";
import type { ApexOptions } from "apexcharts";
import { api } from "../api/client";
import type {
  LlmCallMetric,
  LlmMetricsSummary,
  LlmRoleSummary,
  LlmLiveMetrics,
  LlmGenerationSummary,
} from "../api/types";
import { useBoundPolling } from "../hooks/useBoundPolling";
import PageMeta from "../components/common/PageMeta";
import { Badge } from "../components/shared/Badge";
import { Card, CardHeader, EmptyState } from "../components/shared";
import { EvolutionPageScaffold } from "../components/evolution/EvolutionPageScaffold";
import { cn } from "../lib/utils";

/**
 * LLM 使用分析页。
 *
 * 重构要点：
 * - 用 useBoundPolling 统一拉取 /api/llm/metrics 与 /api/llm/metrics/summary，
 *   替换原散落的 useEffect+setInterval；fail-closed（错误时清空，不展示陈旧数据）。
 * - 用 EvolutionPageScaffold 作为统一页头三件套。
 * - 增加按 role / token / cost 的快速筛选。
 * - 顶部实时利用率仪表盘（active / capacity），每 5s 轮询 /api/llm/metrics/live。
 * - 概览卡片提供输入/输出/思考/缓存 token 四项细分。
 * - 按代次成本表，数据来自 /api/llm/metrics/by-generation。
 * - 并发时间线图（最近 60 分钟），由明细 [ts, ts+elapsed] 区间按分钟桶重叠计数。
 * - 明细表 token 列拆为 输入|输出|思考|缓存 四列；展开行附"查看请求体"链接。
 *
 * 所有数值字段由后端权威写入；前端只读展示，不推断、不回填 null。
 * null 一律表示"未记录"，UI 渲染为 "—"，绝不当成 0。
 */

const REFRESH_INTERVAL_MS = 15_000;
const LIVE_INTERVAL_MS = 10_000;
const CONCURRENCY_WINDOW_MIN = 60;
const CHART_COLORS = {
  elapsed: "#465FFF",
  input: "#10B981",
  output: "#F59E0B",
  thinking: "#8B5CF6",
  cost: "#EF4444",
  ttft: "#06B6D4",
};

// LLM 调用角色 → 中文标签（后端 role 原始码作为 fallback 保留，便于对照日志）。
const ROLE_LABELS: Record<string, string> = {
  COMBINED_ANALYST: "综合分析师",
  DIRECTION_AUDITOR: "方向审计",
  DEGENERATION_DIAGNOSIS: "退化诊断",
  MASTER_PLAN_AUDIT: "方案审计",
  WORKER_COT_CHECK_1: "Worker 推理检查",
  WORKER_COT_CHECK_2: "Worker 推理检查 2",
  FINAL_ANALYSIS: "Master 最终分析",
};
const MASTER_PROPOSAL_LABELS: Record<string, string> = {
  mechanism: "机制方向",
  counterfactual: "反事实方向",
  compute_memory: "计算/记忆方向",
};
const MASTER_CRITIC_LABELS: Record<string, string> = {
  falsification: "证伪评审",
  scope: "范围评审",
};

/** 把后端 role 原始码映射为中文标签，未命中时原样返回。 */
function roleLabel(role: string): string {
  if (ROLE_LABELS[role]) return ROLE_LABELS[role];
  // MASTER PROPOSAL <direction> / MASTER PROPOSAL CRITIC <scope> / MASTER (Try N)
  const proposalMatch = role.match(/^MASTER PROPOSAL (.+)$/);
  if (proposalMatch) {
    const sub = proposalMatch[1];
    if (sub.startsWith("CRITIC ")) {
      const criticSub = sub.slice(7);
      return `方案提议 · ${MASTER_CRITIC_LABELS[criticSub] ?? criticSub}评审`;
    }
    return `方案提议 · ${MASTER_PROPOSAL_LABELS[sub] ?? sub}`;
  }
  const tryMatch = role.match(/^MASTER \(Try (\d+)\)$/);
  if (tryMatch) return `Master 最终分析（第 ${tryMatch[1]} 次）`;
  if (role === "MASTER PROPOSAL CRITIC") return "方案提议 · 评审";
  if (role.startsWith("WORKER")) return `Worker 实现${role.slice(6) ? " · " + role.slice(6) : ""}`;
  return role;
}

// ── 小工具 ────────────────────────────────────────────────────────────────

function fmtNum(value: number | null | undefined, digits = 2, suffix = ""): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${value.toFixed(digits)}${suffix}`;
}

function fmtInt(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return Math.round(value).toLocaleString();
}

function fmtPct(numerator: number, denominator: number): string {
  if (!denominator) return "—";
  return `${((numerator / denominator) * 100).toFixed(1)}%`;
}

function fmtCost(value: number | null | undefined, digits = 4): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return `$${value.toFixed(digits)}`;
}

function fmtTime(ts: string): string {
  try {
    const d = new Date(ts);
    if (Number.isNaN(d.getTime())) return ts;
    return d.toLocaleString();
  } catch {
    return ts;
  }
}

function shortTs(ts: string): string {
  try {
    const d = new Date(ts);
    if (Number.isNaN(d.getTime())) return ts;
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch {
    return ts;
  }
}

function sumValid(values: Array<number | null | undefined>): number {
  let total = 0;
  for (const v of values) if (v != null && Number.isFinite(v)) total += v;
  return total;
}

function avgValid(values: Array<number | null | undefined>): number | null {
  const present = values.filter((v): v is number => v != null && Number.isFinite(v));
  if (present.length === 0) return null;
  return present.reduce((a, b) => a + b, 0) / present.length;
}

// ── 类型 ──────────────────────────────────────────────────────────────────

type ChartKind = "elapsed" | "tokens" | "cost" | "ttft";
type SortKey = "ts" | "total_elapsed_sec" | "cost_usd" | "total_tokens";
type RoleFilter = string; // "" = 全部

// ── 页面 ──────────────────────────────────────────────────────────────────

export default function LlmMetrics() {
  // 调用日志：fail-closed 轮询。默认只拉最近 50 条以保持轮询轻量；
  // 点 "加载更多" 提升到 200（按需加载，不在常规轮询里固定拉 200）。
  const [metricsLimit, setMetricsLimit] = useState(50);
  const {
    data: rawMetrics,
    loading: metricsLoading,
    error: metricsError,
    refresh: refreshMetrics,
    lastUpdated,
  } = useBoundPolling<LlmCallMetric[]>(
    async () => api.llmMetrics(metricsLimit),
    { pollMs: REFRESH_INTERVAL_MS, identityKey: String(metricsLimit) },
  );
  // 按 role 聚合 summary：独立轮询；后端不可用时回退 null。
  const {
    data: summary,
    error: summaryError,
  } = useBoundPolling<LlmMetricsSummary | null>(
    async () => api.llmMetricsSummary().catch(() => null),
    { pollMs: REFRESH_INTERVAL_MS },
  );
  // 实时利用率：每 5s 轮询 /api/llm/metrics/live；fail-closed（错误时清空）。
  const {
    data: live,
    error: liveError,
  } = useBoundPolling<LlmLiveMetrics | null>(
    async () => api.llmMetricsLive().catch(() => null),
    { pollMs: LIVE_INTERVAL_MS },
  );
  // 按代次成本：与 summary 同节奏轮询；后端不可用时回退 null。
  const {
    data: byGeneration,
    error: byGenerationError,
  } = useBoundPolling<LlmGenerationSummary[] | null>(
    async () => api.llmMetricsByGeneration().catch(() => null),
    { pollMs: REFRESH_INTERVAL_MS },
  );

  const [chartKind, setChartKind] = useState<ChartKind>("elapsed");
  const [sortKey, setSortKey] = useState<SortKey>("ts");
  const [sortDesc, setSortDesc] = useState(true);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [roleFilter, setRoleFilter] = useState<RoleFilter>("");

  // 后端可能按任意顺序返回；为时间序列稳定性按 epoch_ts 升序排序。
  const metrics = useMemo(() => {
    if (!rawMetrics) return [];
    return [...rawMetrics].sort((a, b) => a.epoch_ts - b.epoch_ts);
  }, [rawMetrics]);

  const loading = metricsLoading && metrics.length === 0;
  const error = metricsError && metrics.length === 0 ? metricsError.message : null;
  const allRoles = useMemo(() => {
    const set = new Set<string>();
    for (const m of metrics) if (m.role) set.add(m.role);
    return Array.from(set).sort();
  }, [metrics]);

  // ── 概览聚合（前端从明细二次派生，仅用于速览，不覆盖后端 summary 权威字段） ──
  const overview = useMemo(() => {
    const total = metrics.length;
    const successCount = metrics.filter((m) => m.success).length;
    const totalCost = sumValid(metrics.map((m) => m.cost_usd));
    const totalTokens = sumValid(metrics.map((m) => m.total_tokens));
    const totalInput = sumValid(metrics.map((m) => m.input_tokens));
    const totalOutput = sumValid(metrics.map((m) => m.output_tokens));
    const totalThinking = sumValid(metrics.map((m) => m.thinking_tokens_estimated));
    const totalCacheRead = sumValid(metrics.map((m) => m.cache_read_input_tokens));
    const elapsedValues = metrics.map((m) => m.total_elapsed_sec).filter((v): v is number => v != null && Number.isFinite(v));
    const avgElapsed = elapsedValues.length ? elapsedValues.reduce((a, b) => a + b, 0) / elapsedValues.length : null;
    const ttftValues = metrics.map((m) => m.first_token_latency_sec).filter((v): v is number => v != null && Number.isFinite(v));
    const avgTtft = ttftValues.length ? ttftValues.reduce((a, b) => a + b, 0) / ttftValues.length : null;
    return {
      total,
      successCount,
      successRate: total ? successCount / total : null,
      totalCost,
      totalTokens,
      totalInput,
      totalOutput,
      totalThinking,
      totalCacheRead,
      avgElapsed,
      avgTtft,
      failCount: total - successCount,
    };
  }, [metrics]);

  // ── 错误类型分布（仅失败调用） ──
  const errorDistribution = useMemo(() => {
    const map = new Map<string, number>();
    for (const m of metrics) {
      if (m.success) continue;
      const key = m.error_type || "(unknown)";
      map.set(key, (map.get(key) ?? 0) + 1);
    }
    return Array.from(map.entries())
      .map(([type, count]) => ({ type, count }))
      .sort((a, b) => b.count - a.count);
  }, [metrics]);

  // ── 图表数据 ──
  const chartMetrics = roleFilter ? metrics.filter((m) => m.role === roleFilter) : metrics;
  const chartData = useMemo(() => {
    const categories = chartMetrics.map((m) => shortTs(m.ts));
    switch (chartKind) {
      case "elapsed":
        return {
          categories,
          series: [{
            name: "总耗时 (s)",
            type: "line" as const,
            data: chartMetrics.map((m) => m.total_elapsed_sec),
            color: CHART_COLORS.elapsed,
          }],
          yTitle: "秒 (s)",
        };
      case "tokens":
        return {
          categories,
          series: [
            { name: "输入 tokens", type: "line" as const, data: chartMetrics.map((m) => m.input_tokens), color: CHART_COLORS.input },
            { name: "输出 tokens", type: "line" as const, data: chartMetrics.map((m) => m.output_tokens), color: CHART_COLORS.output },
            { name: "思考 tokens", type: "line" as const, data: chartMetrics.map((m) => m.thinking_tokens_estimated), color: CHART_COLORS.thinking },
          ],
          yTitle: "tokens",
        };
      case "cost":
        return {
          categories,
          series: [{
            name: "累积成本 ($)",
            type: "area" as const,
            data: (() => {
              let acc = 0;
              return chartMetrics.map((m) => {
                if (m.cost_usd != null && Number.isFinite(m.cost_usd)) acc += m.cost_usd;
                return Number(acc.toFixed(6));
              });
            })(),
            color: CHART_COLORS.cost,
          }],
          yTitle: "美元 ($)",
        };
      case "ttft":
        return {
          categories,
          series: [{
            name: "首 token 延迟 (s)",
            type: "line" as const,
            data: chartMetrics.map((m) => m.first_token_latency_sec),
            color: CHART_COLORS.ttft,
          }],
          yTitle: "秒 (s)",
        };
      default:
        return { categories, series: [], yTitle: "" };
    }
  }, [chartMetrics, chartKind]);

  // ── 并发时间线（最近 60 分钟）──
  // 对每个调用按区间 [epoch_ts, epoch_ts + total_elapsed_sec] 占用；
  // 把区间落在每个分钟桶内的部分计入该桶的并发数（边界为左闭右开）。
  const concurrencySeries = useMemo(() => {
    if (metrics.length === 0) return { categories: [] as string[], data: [] as number[] };
    const nowSec = Date.now() / 1000;
    const bucketSec = 60;
    const bucketCount = CONCURRENCY_WINDOW_MIN;
    const startSec = Math.floor((nowSec - bucketCount * 60) / bucketSec) * bucketSec;
    const counts = new Array(bucketCount).fill(0);
    for (const m of metrics) {
      const start = m.epoch_ts;
      if (!Number.isFinite(start)) continue;
      const elapsed = Number.isFinite(m.total_elapsed_sec) ? m.total_elapsed_sec : 0;
      const end = start + Math.max(0, elapsed);
      let bStart = Math.floor(start / bucketSec) * bucketSec;
      // Only count the portion overlapping the window.
      if (bStart < startSec) bStart = startSec;
      const bEnd = Math.min(end, startSec + bucketCount * bucketSec);
      if (bEnd <= startSec || bStart >= startSec + bucketCount * bucketSec) continue;
      const firstIdx = Math.floor((bStart - startSec) / bucketSec);
      const lastIdx = Math.floor((bEnd - startSec - 1) / bucketSec);
      for (let i = Math.max(0, firstIdx); i <= Math.min(bucketCount - 1, lastIdx); i++) {
        counts[i] += 1;
      }
    }
    const categories: string[] = [];
    for (let i = 0; i < bucketCount; i++) {
      const d = new Date((startSec + i * bucketSec) * 1000);
      categories.push(d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
    }
    return { categories, data: counts };
  }, [metrics]);

  const concurrencyOptions: ApexOptions = useMemo(() => ({
    chart: {
      fontFamily: "Outfit, sans-serif",
      height: 240,
      type: "area",
      toolbar: { show: false },
      background: "transparent",
      animations: { enabled: false },
    },
    stroke: { width: 2, curve: "stepline" },
    fill: { type: "gradient", gradient: { shadeIntensity: 1, opacityFrom: 0.4, opacityTo: 0.05 } },
    markers: { size: 0 },
    dataLabels: { enabled: false },
    grid: {
      borderColor: undefined,
      strokeDashArray: 3,
      xaxis: { lines: { show: false } },
      yaxis: { lines: { show: true } },
    },
    tooltip: { theme: "dark", x: { show: true } },
    xaxis: {
      categories: concurrencySeries.categories,
      tickAmount: 12,
      labels: { style: { fontSize: "10px" } },
      axisBorder: { show: false },
      axisTicks: { show: false },
    },
    yaxis: {
      min: 0,
      labels: { style: { fontSize: "12px", colors: ["#6B7280"] } },
      title: { text: "并发调用数", style: { fontSize: "12px" } },
    },
    theme: { mode: "light" },
  }), [concurrencySeries]);

  const chartOptions: ApexOptions = useMemo(() => {
    const isArea = chartKind === "cost";
    return {
      chart: {
        fontFamily: "Outfit, sans-serif",
        height: 360,
        type: isArea ? "area" : "line",
        toolbar: { show: true },
        background: "transparent",
        animations: { enabled: false },
      },
      stroke: { width: 2, curve: "smooth" },
      fill: isArea ? { type: "gradient", gradient: { shadeIntensity: 1, opacityFrom: 0.4, opacityTo: 0.05 } } : undefined,
      markers: { size: 0, hover: { size: 4 } },
      dataLabels: { enabled: false },
      legend: {
        show: chartKind === "tokens",
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
      tooltip: { theme: "dark", x: { show: true } },
      xaxis: {
        categories: chartData.categories,
        tickAmount: 12,
        labels: { style: { fontSize: "10px" } },
        axisBorder: { show: false },
        axisTicks: { show: false },
      },
      yaxis: {
        labels: { style: { fontSize: "12px", colors: ["#6B7280"] } },
        title: { text: chartData.yTitle, style: { fontSize: "12px" } },
      },
      theme: { mode: "light" },
    };
  }, [chartData, chartKind]);

  // ── 明细排序 + 筛选 ──
  const sortedMetrics = useMemo(() => {
    const arr = roleFilter ? metrics.filter((m) => m.role === roleFilter) : [...metrics];
    arr.sort((a, b) => {
      let va: number | string;
      let vb: number | string;
      switch (sortKey) {
        case "total_elapsed_sec": va = a.total_elapsed_sec; vb = b.total_elapsed_sec; break;
        case "cost_usd": va = a.cost_usd ?? -1; vb = b.cost_usd ?? -1; break;
        case "total_tokens": va = a.total_tokens ?? -1; vb = b.total_tokens ?? -1; break;
        case "ts":
        default: va = a.epoch_ts; vb = b.epoch_ts; break;
      }
      if (va < vb) return sortDesc ? 1 : -1;
      if (va > vb) return sortDesc ? -1 : 1;
      return 0;
    });
    return arr.slice(0, 100);
  }, [metrics, sortKey, sortDesc, roleFilter]);

  const toggleSort = useCallback((key: SortKey) => {
    setSortKey((prev) => {
      if (prev === key) { setSortDesc((d) => !d); return prev; }
      setSortDesc(true);
      return key;
    });
  }, []);

  const toggleExpand = useCallback((id: string) => {
    setExpandedId((prev) => (prev === id ? null : id));
  }, []);

  // ── 渲染 ────────────────────────────────────────────────────────────────

  if (loading) {
    return (
      <EvolutionPageScaffold title="LLM 使用分析">
        <PageMeta title="LLM 使用分析 — Bot 自进化" description="LLM 调用记录与指标" />
        <div className="rounded-2xl border border-gray-200 bg-white dark:border-border-subtle dark:bg-surface-1">
          <EmptyState message="正在加载 LLM 调用记录…" />
        </div>
      </EvolutionPageScaffold>
    );
  }

  if (error) {
    return (
      <EvolutionPageScaffold title="LLM 使用分析">
        <PageMeta title="LLM 使用分析 — Bot 自进化" description="LLM 调用记录与指标" />
        <div className="rounded-2xl border border-error-200 bg-error-50 px-4 py-3 text-sm text-error-700 dark:border-error-900/30 dark:bg-error-950/20 dark:text-error-300">
          加载失败：{error}
          <button onClick={() => refreshMetrics()} className="ml-3 underline">重试</button>
        </div>
      </EvolutionPageScaffold>
    );
  }

  if (metrics.length === 0) {
    return (
      <EvolutionPageScaffold title="LLM 使用分析">
        <PageMeta title="LLM 使用分析 — Bot 自进化" description="LLM 调用记录与指标" />
        <div className="rounded-2xl border border-gray-200 bg-white dark:border-border-subtle dark:bg-surface-1">
          <EmptyState message="暂无调用记录" />
        </div>
      </EvolutionPageScaffold>
    );
  }

  return (
    <EvolutionPageScaffold title="LLM 使用分析" subtitle="调用日志、输入输出详情与按角色聚合统计">
      <PageMeta title="LLM 使用分析 — Bot 自进化" description="LLM 调用记录与指标" />

      {/* 顶部状态条 */}
      <div className="mb-4 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
        <Badge variant="info" size="sm">共 {overview.total} 条记录</Badge>
        <span className="text-gray-500 dark:text-gray-400">
          最近更新：<span className="font-medium text-gray-700 dark:text-gray-200">
            {lastUpdated ? new Date(lastUpdated).toLocaleTimeString() : "—"}
          </span>
        </span>
        <span className="text-gray-400 dark:text-gray-500 text-xs">
          每 {REFRESH_INTERVAL_MS / 1000}s 自动刷新
        </span>
        <button
          onClick={() => refreshMetrics()}
          className="text-xs text-brand-600 hover:text-brand-700 dark:text-brand-400 underline"
        >
          立即刷新
        </button>
        {metricsError && (
          <span className="text-xs text-error-600 dark:text-error-400">（调用日志刷新失败：{metricsError.message}）</span>
        )}
        {summaryError && (
          <span className="text-xs text-amber-600 dark:text-amber-400">（角色聚合暂不可用：{summaryError.message}）</span>
        )}
        {liveError && (
          <span className="text-xs text-amber-600 dark:text-amber-400">（实时指标暂不可用：{liveError.message}）</span>
        )}
        {byGenerationError && (
          <span className="text-xs text-amber-600 dark:text-amber-400">（按代次聚合暂不可用：{byGenerationError.message}）</span>
        )}
      </div>

      {/* 实时利用率仪表盘 */}
      <UtilizationGauge live={live} />

      {/* 概览卡片 */}
      <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
        <OverviewCard label="总调用数" value={fmtInt(overview.total)} />
        <OverviewCard
          label="成功率"
          value={overview.successRate != null ? fmtPct(overview.successCount, overview.total) : "—"}
          valueClass={overview.successRate != null && overview.successRate >= 0.95
            ? "text-success-600 dark:text-success-400"
            : overview.successRate != null && overview.successRate < 0.8
              ? "text-error-600 dark:text-error-400"
              : undefined}
        />
        <OverviewCard label="总成本" value={fmtCost(overview.totalCost, 2)} />
        <OverviewCard label="平均耗时" value={fmtNum(overview.avgElapsed, 1, "s")} />
        <OverviewCard label="平均 TTFT" value={fmtNum(overview.avgTtft, 2, "s")} />
      </div>

      {/* Token 细分卡片 */}
      <div className="mb-4">
        <TokenBreakdownCard
          input={overview.totalInput}
          output={overview.totalOutput}
          thinking={overview.totalThinking}
          cache={overview.totalCacheRead}
        />
      </div>

      {/* 时间序列图 */}
      <Card className="mb-4" padding="p-0">
        <CardHeader
          title="调用趋势"
          actions={
            <div className="flex flex-wrap gap-1">
              {([
                { key: "elapsed" as const, label: "耗时" },
                { key: "tokens" as const, label: "Token" },
                { key: "cost" as const, label: "累积成本" },
                { key: "ttft" as const, label: "TTFT" },
              ]).map((opt) => (
                <button
                  key={opt.key}
                  onClick={() => setChartKind(opt.key)}
                  className={cn(
                    "rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
                    chartKind === opt.key
                      ? "bg-brand-500 text-white"
                      : "text-gray-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-white/5",
                  )}
                >
                  {opt.label}
                </button>
              ))}
            </div>
          }
        />
        <div className="p-5">
          {chartData.series.length === 0 ? (
            <div className="py-16 text-center text-sm text-gray-400">当前指标无可绘制数据</div>
          ) : (
            <Chart
              options={chartOptions}
              series={chartData.series}
              type={chartKind === "cost" ? "area" : "line"}
              height={360}
            />
          )}
        </div>
      </Card>

      {/* 并发时间线 */}
      <Card className="mb-4" padding="p-0">
        <CardHeader
          title={`并发时间线（最近 ${CONCURRENCY_WINDOW_MIN} 分钟）`}
          subtitle="按调用区间 [ts, ts + 耗时] 在分钟桶上的重叠计数"
        />
        <div className="p-5">
          {concurrencySeries.data.length === 0 ? (
            <div className="py-10 text-center text-sm text-gray-400">暂无可绘制数据</div>
          ) : (
            <Chart
              options={concurrencyOptions}
              series={[{ name: "并发调用", data: concurrencySeries.data }]}
              type="area"
              height={240}
            />
          )}
        </div>
      </Card>

      {/* 按角色分组统计表 */}
      <Card className="mb-4" padding="p-0">
        <CardHeader title="按角色分组统计" />
        <RoleSummaryTable
          rows={summary?.by_role ?? null}
          fallbackRows={metrics}
        />
      </Card>

      {/* 按代次成本表 */}
      <Card className="mb-4" padding="p-0">
        <CardHeader title="按代次成本" subtitle="按 generation_id 聚合的调用数、成本与 token" />
        <GenerationSummaryTable rows={byGeneration ?? null} />
      </Card>

      {/* 错误分析 */}
      {errorDistribution.length > 0 && (
        <Card className="mb-4" padding="p-0">
          <CardHeader
            title="错误类型分布"
            subtitle={`失败调用共 ${overview.failCount} 次`}
          />
          <div className="p-5">
            <div className="space-y-2">
              {errorDistribution.map((e) => {
                const pct = overview.failCount ? (e.count / overview.failCount) * 100 : 0;
                return (
                  <div key={e.type} className="flex items-center gap-3">
                    <div className="w-40 shrink-0 truncate text-xs font-medium text-error-700 dark:text-error-300" title={e.type}>
                      {e.type}
                    </div>
                    <div className="relative h-5 flex-1 overflow-hidden rounded bg-gray-100 dark:bg-gray-800">
                      <div
                        className="absolute inset-y-0 left-0 rounded bg-error-400/70 dark:bg-error-500/60"
                        style={{ width: `${Math.max(pct, 2)}%` }}
                      />
                    </div>
                    <div className="w-20 shrink-0 text-right text-xs font-mono text-gray-700 dark:text-gray-200">
                      {e.count} <span className="text-gray-400">({pct.toFixed(0)}%)</span>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        </Card>
      )}

      {/* 调用明细表（含 role / token / cost 筛选） */}
      <Card padding="p-0">
        <CardHeader
          title="调用明细"
          subtitle={`最近 ${sortedMetrics.length} 条`}
          actions={
            <RoleFilterControl
              roles={allRoles}
              value={roleFilter}
              onChange={setRoleFilter}
            />
          }
        />
        <div className="overflow-x-auto">
          <table className="w-full min-w-[1020px] text-left text-xs">
            <thead className="border-b border-gray-100 bg-gray-50 text-[10px] uppercase tracking-wide text-gray-500 dark:border-gray-800 dark:bg-white/[0.02] dark:text-gray-400">
              <tr>
                <th className="px-3 py-2 font-medium">
                  <SortHeader label="时间" active={sortKey === "ts"} desc={sortDesc} onClick={() => toggleSort("ts")} />
                </th>
                <th className="px-3 py-2 font-medium">角色</th>
                <th className="px-3 py-2 font-medium">模型</th>
                <th className="px-3 py-2 text-right font-medium">
                  <SortHeader label="耗时(s)" active={sortKey === "total_elapsed_sec"} desc={sortDesc} onClick={() => toggleSort("total_elapsed_sec")} />
                </th>
                <th className="px-3 py-2 text-right font-medium">TTFT(s)</th>
                <th className="px-3 py-2 text-right font-medium">输入</th>
                <th className="px-3 py-2 text-right font-medium">输出</th>
                <th className="px-3 py-2 text-right font-medium">思考</th>
                <th className="px-3 py-2 text-right font-medium">缓存</th>
                <th className="px-3 py-2 text-right font-medium">
                  <SortHeader label="成本($)" active={sortKey === "cost_usd"} desc={sortDesc} onClick={() => toggleSort("cost_usd")} />
                </th>
                <th className="px-3 py-2 font-medium">状态</th>
                <th className="px-3 py-2" />
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
              {sortedMetrics.map((m) => {
                const expanded = expandedId === m.call_id;
                return (
                  <DetailRow
                    key={m.call_id}
                    metric={m}
                    expanded={expanded}
                    onToggle={() => toggleExpand(m.call_id)}
                  />
                );
              })}
            </tbody>
          </table>
        </div>
        {metricsLimit < 200 && (
          <div className="flex items-center justify-center border-t border-gray-100 p-3 dark:border-gray-800">
            <button
              type="button"
              onClick={() => setMetricsLimit(200)}
              className="rounded-lg border border-gray-200 bg-white px-4 py-1.5 text-xs font-medium text-gray-700 transition hover:bg-gray-50 dark:border-border-subtle dark:bg-surface-1 dark:text-gray-300 dark:hover:bg-white/[0.04]"
            >
              加载更多（至 200 条）
            </button>
          </div>
        )}
      </Card>
    </EvolutionPageScaffold>
  );
}

// ── 子组件 ─────────────────────────────────────────────────────────────────

function OverviewCard({ label, value, valueClass }: { label: string; value: string; valueClass?: string }) {
  return (
    <div className="rounded-xl border border-gray-200 bg-white px-4 py-3 dark:border-border-subtle dark:bg-surface-1">
      <p className="text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">{label}</p>
      <p className={cn("mt-1 text-2xl font-semibold text-gray-900 dark:text-white", valueClass)}>{value}</p>
    </div>
  );
}

/** 实时利用率仪表盘：大字 active/capacity + 着色条 + 近 5 分钟调用数。 */
function UtilizationGauge({ live }: { live: LlmLiveMetrics | null }) {
  const active = live?.active_streams ?? 0;
  const capacity = live?.capacity ?? 0;
  const pct = live?.utilization_pct ?? 0;
  const recent = live?.recent_calls_5min ?? 0;

  // 颜色：>50% 绿，20–50% 琥珀，<20% 红（按规范）。
  const barColor =
    pct > 50 ? "bg-success-500"
      : pct >= 20 ? "bg-amber-500"
        : "bg-error-500";
  const textColor =
    pct > 50 ? "text-success-600 dark:text-success-400"
      : pct >= 20 ? "text-amber-600 dark:text-amber-400"
        : "text-error-600 dark:text-error-400";

  return (
    <div className="mb-4 rounded-2xl border border-gray-200 bg-white p-5 dark:border-border-subtle dark:bg-surface-1">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-baseline gap-3">
          <div>
            <p className="text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">实时并发</p>
            <p className={cn("mt-1 text-4xl font-bold", textColor)}>
              {active}
              <span className="mx-1 text-2xl text-gray-400 dark:text-gray-500">/</span>
              <span className="text-2xl font-semibold text-gray-400 dark:text-gray-500">{capacity}</span>
            </p>
          </div>
          <div className="ml-4">
            <p className="text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">利用率</p>
            <p className={cn("mt-1 text-2xl font-semibold", textColor)}>{pct.toFixed(1)}%</p>
          </div>
        </div>
        <div className="text-sm text-gray-500 dark:text-gray-400">
          近 5 分钟调用：<span className="font-semibold text-gray-800 dark:text-gray-100">{recent}</span>
          <span className="ml-2 text-xs text-gray-400 dark:text-gray-500">每 {LIVE_INTERVAL_MS / 1000}s 刷新</span>
        </div>
      </div>
      <div className="mt-3 h-3 w-full overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
        <div
          className={cn("h-full rounded-full transition-all duration-500", barColor)}
          style={{ width: `${Math.min(100, Math.max(2, pct))}%` }}
        />
      </div>
      {!live && (
        <p className="mt-2 text-xs text-amber-600 dark:text-amber-400">实时指标暂不可用，显示 0 / 0</p>
      )}
    </div>
  );
}

/** Token 细分：输入 / 输出 / 思考 / 缓存命中。 */
function TokenBreakdownCard({
  input,
  output,
  thinking,
  cache,
}: {
  input: number;
  output: number;
  thinking: number;
  cache: number;
}) {
  const cells: Array<{ label: string; value: number; color: string }> = [
    { label: "输入", value: input, color: "text-success-600 dark:text-success-400" },
    { label: "输出", value: output, color: "text-amber-600 dark:text-amber-400" },
    { label: "思考", value: thinking, color: "text-purple-600 dark:text-purple-400" },
    { label: "缓存命中", value: cache, color: "text-brand-600 dark:text-brand-400" },
  ];
  return (
    <div className="rounded-2xl border border-gray-200 bg-white p-4 dark:border-border-subtle dark:bg-surface-1">
      <p className="mb-3 text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">Token 细分</p>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {cells.map((c) => (
          <div key={c.label} className="rounded-lg bg-gray-50 px-3 py-2 dark:bg-white/[0.03]">
            <p className="text-[10px] font-medium uppercase tracking-wide text-gray-400 dark:text-gray-500">{c.label}</p>
            <p className={cn("mt-0.5 text-lg font-semibold", c.color)}>{fmtInt(c.value)}</p>
          </div>
        ))}
      </div>
    </div>
  );
}

/** 按代次成本表。后端不可用时显示空态。 */
function GenerationSummaryTable({ rows }: { rows: LlmGenerationSummary[] | null }) {
  if (!rows || rows.length === 0) {
    return <div className="p-5 text-sm text-gray-400">暂无按代次聚合数据</div>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[820px] text-left text-xs">
        <thead className="border-b border-gray-100 bg-gray-50 text-[10px] uppercase tracking-wide text-gray-500 dark:border-gray-800 dark:bg-white/[0.02] dark:text-gray-400">
          <tr>
            <th className="px-3 py-2 font-medium">代次</th>
            <th className="px-3 py-2 text-right font-medium">调用数</th>
            <th className="px-3 py-2 text-right font-medium">总成本</th>
            <th className="px-3 py-2 text-right font-medium">平均耗时</th>
            <th className="px-3 py-2 text-right font-medium">输入 tokens</th>
            <th className="px-3 py-2 text-right font-medium">输出 tokens</th>
            <th className="px-3 py-2 text-right font-medium">思考 tokens</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
          {rows.map((r) => (
            <tr key={r.generation_id} className="hover:bg-gray-50 dark:hover:bg-white/[0.02]">
              <td className="px-3 py-2 font-mono text-gray-800 dark:text-gray-200">{r.generation_id}</td>
              <td className="px-3 py-2 text-right font-mono text-gray-700 dark:text-gray-300">{fmtInt(r.calls)}</td>
              <td className="px-3 py-2 text-right font-mono text-gray-800 dark:text-gray-100">{fmtCost(r.total_cost, 4)}</td>
              <td className="px-3 py-2 text-right font-mono text-gray-700 dark:text-gray-300">{fmtNum(r.avg_elapsed, 2, "s")}</td>
              <td className="px-3 py-2 text-right font-mono text-success-600 dark:text-success-400">{fmtInt(r.input_tokens)}</td>
              <td className="px-3 py-2 text-right font-mono text-amber-600 dark:text-amber-400">{fmtInt(r.output_tokens)}</td>
              <td className="px-3 py-2 text-right font-mono text-purple-600 dark:text-purple-400">{fmtInt(r.thinking_tokens)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SortHeader({ label, active, desc, onClick }: { label: string; active: boolean; desc: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-1 transition-colors hover:text-gray-700 dark:hover:text-gray-200",
        active ? "text-brand-600 dark:text-brand-400" : "",
      )}
    >
      {label}
      <span className="text-[8px]">{active ? (desc ? "▼" : "▲") : "↕"}</span>
    </button>
  );
}

function RoleFilterControl({
  roles,
  value,
  onChange,
}: {
  roles: string[];
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="flex items-center gap-2 text-xs text-gray-500">
      <span>按角色筛选</span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="rounded border border-gray-200 px-2 py-1 text-xs dark:border-border-subtle dark:bg-surface-1"
      >
        <option value="">全部 ({roles.length})</option>
        {roles.map((role) => (
          <option key={role} value={role}>{roleLabel(role)}</option>
        ))}
      </select>
    </label>
  );
}

function RoleSummaryTable({
  rows,
  fallbackRows,
}: {
  rows: LlmRoleSummary[] | null;
  fallbackRows: LlmCallMetric[];
}) {
  const displayRows: LlmRoleSummary[] = useMemo(() => {
    if (rows && rows.length > 0) return rows;
    // 后端 summary 不可用时的前端兜底聚合（仅展示用，非权威）。
    const byRole = new Map<string, LlmCallMetric[]>();
    for (const m of fallbackRows) {
      const arr = byRole.get(m.role) ?? [];
      arr.push(m);
      byRole.set(m.role, arr);
    }
    const derived: LlmRoleSummary[] = [];
    for (const [role, calls] of byRole) {
      const successCount = calls.filter((c) => c.success).length;
      const elapsedValues = calls.map((c) => c.total_elapsed_sec).filter((v): v is number => v != null && Number.isFinite(v));
      derived.push({
        role,
        count: calls.length,
        success_count: successCount,
        success_rate: calls.length ? successCount / calls.length : 0,
        avg_total_elapsed_sec: avgValid(elapsedValues) ?? 0,
        max_total_elapsed_sec: elapsedValues.length ? Math.max(...elapsedValues) : 0,
        avg_total_tokens: avgValid(calls.map((c) => c.total_tokens)),
        total_cost_usd: sumValid(calls.map((c) => c.cost_usd)) || null,
        avg_first_token_latency_sec: avgValid(calls.map((c) => c.first_token_latency_sec)),
      });
    }
    return derived.sort((a, b) => b.count - a.count);
  }, [rows, fallbackRows]);

  if (displayRows.length === 0) {
    return <div className="p-5 text-sm text-gray-400">暂无 Role 维度数据</div>;
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[760px] text-left text-xs">
        <thead className="border-b border-gray-100 bg-gray-50 text-[10px] uppercase tracking-wide text-gray-500 dark:border-gray-800 dark:bg-white/[0.02] dark:text-gray-400">
          <tr>
            <th className="px-3 py-2 font-medium">角色</th>
            <th className="px-3 py-2 text-right font-medium">调用数</th>
            <th className="px-3 py-2 text-right font-medium">成功率</th>
            <th className="px-3 py-2 text-right font-medium">平均耗时</th>
            <th className="px-3 py-2 text-right font-medium">最大耗时</th>
            <th className="px-3 py-2 text-right font-medium">平均 tokens</th>
            <th className="px-3 py-2 text-right font-medium">平均 TTFT</th>
            <th className="px-3 py-2 text-right font-medium">总成本</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
          {displayRows.map((r) => (
            <tr key={r.role} className="hover:bg-gray-50 dark:hover:bg-white/[0.02]">
              <td className="px-3 py-2 font-medium text-gray-800 dark:text-gray-200" title={r.role}>{roleLabel(r.role)}</td>
              <td className="px-3 py-2 text-right font-mono text-gray-700 dark:text-gray-300">{r.count}</td>
              <td className={cn(
                "px-3 py-2 text-right font-mono",
                r.success_rate >= 0.95 ? "text-success-600 dark:text-success-400"
                  : r.success_rate < 0.8 ? "text-error-600 dark:text-error-400"
                  : "text-gray-700 dark:text-gray-300",
              )}>{(r.success_rate * 100).toFixed(1)}%</td>
              <td className="px-3 py-2 text-right font-mono text-gray-700 dark:text-gray-300">{fmtNum(r.avg_total_elapsed_sec, 1, "s")}</td>
              <td className="px-3 py-2 text-right font-mono text-gray-700 dark:text-gray-300">{fmtNum(r.max_total_elapsed_sec, 1, "s")}</td>
              <td className="px-3 py-2 text-right font-mono text-gray-700 dark:text-gray-300">{fmtInt(r.avg_total_tokens)}</td>
              <td className="px-3 py-2 text-right font-mono text-gray-700 dark:text-gray-300">{fmtNum(r.avg_first_token_latency_sec, 2, "s")}</td>
              <td className="px-3 py-2 text-right font-mono text-gray-800 dark:text-gray-100">{fmtCost(r.total_cost_usd, 4)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function DetailRow({
  metric: m,
  expanded,
  onToggle,
}: {
  metric: LlmCallMetric;
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <>
      <tr
        onClick={onToggle}
        className={cn(
          "cursor-pointer transition-colors hover:bg-gray-50 dark:hover:bg-white/[0.02]",
          !m.success && "bg-error-50/40 dark:bg-error-950/10",
        )}
      >
        <td className="whitespace-nowrap px-3 py-2 text-gray-600 dark:text-gray-400">{shortTs(m.ts)}</td>
        <td className="px-3 py-2">
          <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] font-medium text-gray-700 dark:bg-white/10 dark:text-gray-300" title={m.role}>
            {roleLabel(m.role)}
          </span>
        </td>
        <td className="px-3 py-2 text-gray-500 dark:text-gray-400">{m.model}</td>
        <td className="px-3 py-2 text-right font-mono text-gray-700 dark:text-gray-300">{fmtNum(m.total_elapsed_sec, 2)}</td>
        <td className="px-3 py-2 text-right font-mono text-gray-500 dark:text-gray-400">{fmtNum(m.first_token_latency_sec, 2)}</td>
        <td className="px-3 py-2 text-right font-mono text-gray-700 dark:text-gray-300">{fmtInt(m.input_tokens)}</td>
        <td className="px-3 py-2 text-right font-mono text-gray-700 dark:text-gray-300">{fmtInt(m.output_tokens)}</td>
        <td className="px-3 py-2 text-right font-mono text-purple-600 dark:text-purple-400">{fmtInt(m.thinking_tokens_estimated)}</td>
        <td className="px-3 py-2 text-right font-mono text-gray-700 dark:text-gray-300">{fmtInt(m.cache_read_input_tokens)}</td>
        <td className="px-3 py-2 text-right font-mono text-gray-700 dark:text-gray-300">{fmtCost(m.cost_usd, 4)}</td>
        <td className="px-3 py-2">
          {m.success ? (
            <Badge variant="success" size="sm">成功</Badge>
          ) : (
            <span title={m.error_message ?? undefined}>
              <Badge variant="error" size="sm">
                {m.error_type || "失败"}
              </Badge>
            </span>
          )}
        </td>
        <td className="px-3 py-2 text-gray-400">{expanded ? "▲" : "▼"}</td>
      </tr>
      {expanded && (
        <tr className="bg-gray-50/60 dark:bg-white/[0.02]">
          <td colSpan={12} className="px-3 py-3">
            <ExpandedFields metric={m} />
          </td>
        </tr>
      )}
    </>
  );
}

/** 完整字段网格（展开行），null 一律显示 "—"。 */
function ExpandedFields({ metric: m }: { metric: LlmCallMetric }) {
  // log_file looks like "v1/logs/strict_invocations/<invocation_id>/<role>_io.txt".
  // Build a deep link into the existing generation-log viewer (/api/logs/generations/{version}/{filename}).
  const logLink = useMemo(() => {
    if (!m.log_file) return null;
    const parts = m.log_file.split("/");
    const version = parts[0]; // e.g. "v1"
    const filename = parts[parts.length - 1];
    if (!version || !filename) return null;
    return `/api/logs/generations/${encodeURIComponent(version)}/${encodeURIComponent(filename)}?tail=0`;
  }, [m.log_file]);

  const fields: Array<[string, string]> = [
    ["完整时间", fmtTime(m.ts)],
    ["call_id", m.call_id],
    ["attempt / max", `${m.attempt}${m.max_attempts != null ? ` / ${m.max_attempts}` : ""}`],
    ["模型", m.model],
    ["总耗时", fmtNum(m.total_elapsed_sec, 3, "s")],
    ["首 token 延迟", fmtNum(m.first_token_latency_sec, 3, "s")],
    ["首文本延迟", fmtNum(m.first_text_latency_sec, 3, "s")],
    ["信号量等待", fmtNum(m.semaphore_wait_sec, 3, "s")],
    ["输入 tokens", fmtInt(m.input_tokens)],
    ["输出 tokens", fmtInt(m.output_tokens)],
    ["缓存创建 tokens", fmtInt(m.cache_creation_input_tokens)],
    ["缓存读 tokens", fmtInt(m.cache_read_input_tokens)],
    ["缓存命中率", fmtNum(m.cache_hit_rate, 4)],
    ["思考 tokens（估）", fmtInt(m.thinking_tokens_estimated)],
    ["思考 tokens 增量", fmtInt(m.thinking_tokens_delta_total)],
    ["总 tokens", fmtInt(m.total_tokens)],
    ["输出 tokens/s", fmtNum(m.output_tokens_per_sec, 2)],
    ["总 tokens/s", fmtNum(m.total_tokens_per_sec, 2)],
    ["成本", fmtCost(m.cost_usd, 6)],
    ["success", m.success ? "true" : "false"],
    ["error_type", m.error_type ?? "—"],
    ["error_message", m.error_message ?? "—"],
    ["api_error_status", m.api_error_status != null ? String(m.api_error_status) : "—"],
    ["stop_reason", m.stop_reason ?? "—"],
    ["num_turns", fmtInt(m.num_turns)],
    ["terminal_reason", m.terminal_reason ?? "—"],
    ["effort", m.effort ?? "—"],
    ["thinking_budget", fmtInt(m.thinking_budget)],
    ["thinking_mode", m.thinking_mode ?? "—"],
    ["全局并发", fmtInt(m.global_concurrency)],
    ["prompt_chars", fmtInt(m.prompt_chars)],
    ["output_chars", fmtInt(m.output_chars)],
    ["invocation_id", m.invocation_id ?? "—"],
    ["generation_id", m.generation_id ?? "—"],
    ["log_file", m.log_file ?? "—"],
    ["schema_version", String(m.schema_version)],
  ];

  return (
    <div>
      {logLink && (
        <div className="mb-3">
          <a
            href={logLink}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 rounded-md border border-brand-200 bg-brand-50 px-2.5 py-1 text-xs font-medium text-brand-700 transition hover:bg-brand-100 dark:border-brand-800/50 dark:bg-brand-950/30 dark:text-brand-300 dark:hover:bg-brand-950/50"
          >
            查看请求体（{m.log_file}）
          </a>
        </div>
      )}
      <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-xs sm:grid-cols-3 lg:grid-cols-4">
        {fields.map(([k, v]) => (
          <div key={k} className="flex flex-col">
            <span className="text-[10px] uppercase tracking-wide text-gray-400 dark:text-gray-500">{k}</span>
            <span className={cn(
              "break-all font-mono",
              k === "error_message" || k === "error_type"
                ? "text-error-600 dark:text-error-400"
                : "text-gray-700 dark:text-gray-200",
            )}>{v}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
