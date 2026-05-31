import { cn } from "../../lib/utils";

interface MetricCardProps {
  label: string;
  value: string | number;
  trend?: { value: number; label?: string };
  loading?: boolean;
  className?: string;
}

export function MetricCard({ label, value, trend, loading, className }: MetricCardProps) {
  if (loading) {
    return (
      <div className={cn("rounded-xl border border-gray-200 bg-white px-4 py-3 dark:border-gray-800 dark:bg-white/[0.04]", className)}>
        <div className="h-3 w-16 animate-shimmer rounded bg-gray-200 dark:bg-gray-700" />
        <div className="mt-2 h-6 w-20 animate-shimmer rounded bg-gray-200 dark:bg-gray-700" />
      </div>
    );
  }

  const trendColor = trend
    ? trend.value > 0
      ? "text-success-600 dark:text-success-400"
      : trend.value < 0
        ? "text-error-600 dark:text-error-400"
        : "text-gray-400"
    : "";

  const trendArrow = trend
    ? trend.value > 0
      ? "↑"
      : trend.value < 0
        ? "↓"
        : "→"
    : "";

  return (
    <div className={cn("rounded-xl border border-gray-200 bg-white px-4 py-3 dark:border-gray-800 dark:bg-white/[0.04]", className)}>
      <p className="text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">{label}</p>
      <div className="mt-1 flex items-baseline gap-2">
        <span className="text-2xl font-semibold text-gray-900 dark:text-white">{value}</span>
        {trend && (
          <span className={cn("text-xs font-medium", trendColor)}>
            {trendArrow} {Math.abs(trend.value).toFixed(trend.value % 1 === 0 ? 0 : 1)}
            {trend.label && <span className="ml-0.5 text-gray-400">{trend.label}</span>}
          </span>
        )}
      </div>
    </div>
  );
}
