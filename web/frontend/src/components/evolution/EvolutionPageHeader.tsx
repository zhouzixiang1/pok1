import { Link } from "react-router";
import type { ControlHealth, ControlStatus } from "../../api/control";
import { draftGenerations, primaryGenerationSlot } from "../../api/control";
import { EvolutionSurface, EvolutionStatusBadge, RefreshStatusBadge } from "./ui";
import { cn } from "../../lib/utils";
import { epochStateLabels } from "../../lib/epochStateLabels";

interface EvolutionPageHeaderProps {
  title: string;
  subtitle?: string;
  status: ControlStatus | null;
  health?: ControlHealth | null;
  loading?: boolean;
  error?: string | null;
  /** Epoch-ms of the last successful observation, for the refresh badge. */
  lastUpdated?: number | null;
  /** full = dual identity strip; compact = single-line for subpages */
  variant?: "full" | "compact";
  className?: string;
}

/**
 * Unified evolution page header. Replaces scattered EpochAuthority dual cards
 * on subpages when used with variant=compact.
 */
export function EvolutionPageHeader({
  title,
  subtitle,
  status,
  loading = false,
  error = null,
  lastUpdated = null,
  variant = "full",
  className,
}: EvolutionPageHeaderProps) {
  const primary = primaryGenerationSlot(status);
  const drafts = draftGenerations(status);
  const handoff = status?.post_publication_handoff;

  if (!status) {
    return (
      <EvolutionSurface className={cn("mb-4", className)} padding="sm">
        {loading ? (
          <div>
            <p className="text-sm text-gray-500">
              {error ? "正在刷新运行权威…" : "正在核对严格进化与版本身份…"}
            </p>
            <RefreshStatusBadge lastUpdated={lastUpdated} className="mt-1 block text-xs text-gray-400 dark:text-gray-500" />
          </div>
        ) : (
          <div>
            <p className="font-semibold text-red-600 dark:text-red-300">无法确认版本与运行权威</p>
            <p className="mt-1 text-xs text-gray-500">{error || "控制状态不可用"}</p>
          </div>
        )}
      </EvolutionSurface>
    );
  }

  const genLabel = primary
    ? `主槽 v${primary.next_v} · ${primary.stage}`
    : handoff && handoff.status !== "none"
      ? `交接 v${handoff.version ?? "?"} · ${handoff.status}`
      : "无活动代次";

  if (variant === "compact") {
    return (
      <EvolutionSurface className={cn("mb-4", className)} padding="sm">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h1 className="text-lg font-semibold text-gray-800 dark:text-white">{title}</h1>
            {subtitle && <p className="text-xs text-gray-500">{subtitle}</p>}
          </div>
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <EvolutionStatusBadge tone={status.running ? "ok" : "neutral"}>
              {status.running ? "运行中" : "已停止"}
            </EvolutionStatusBadge>
            <span className="font-mono text-gray-600 dark:text-gray-300">{genLabel}</span>
            {drafts.length === 0 ? (
              <span className="text-gray-400">草稿槽：无</span>
            ) : (
              drafts.map((d) => (
                <EvolutionStatusBadge key={`${d.next_v}-${d.workflow_run_id}`} tone="info">
                  草稿 v{d.next_v}
                </EvolutionStatusBadge>
              ))
            )}
          </div>
        </div>
      </EvolutionSurface>
    );
  }

  return (
    <EvolutionSurface className={cn("mb-4 space-y-3", className)}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-gray-800 dark:text-white">{title}</h1>
          {subtitle && (
            <p className="mt-0.5 text-sm font-medium text-gray-500">{subtitle}</p>
          )}
        </div>
        <Link to="/control" className="text-xs text-brand-600 hover:underline dark:text-brand-400">
          控制面板
        </Link>
      </div>
      <div className="flex flex-wrap gap-2 text-xs">
        <EvolutionStatusBadge tone={status.epoch_initialized ? "ok" : "warn"}>
          {epochStateLabels[status.epoch_state] ?? status.epoch_state}
        </EvolutionStatusBadge>
        <EvolutionStatusBadge tone={status.running ? "ok" : "neutral"}>
          {status.running ? "编排运行中" : "编排已停止"}
        </EvolutionStatusBadge>
        <span className="rounded-md border border-gray-200 px-2 py-0.5 font-mono dark:border-gray-700">
          {genLabel}
        </span>
        {drafts.length === 0 ? (
          <span className="text-gray-400">草稿槽：无（显式空）</span>
        ) : (
          drafts.map((d) => (
            <EvolutionStatusBadge key={`${d.next_v}-${d.stage}`} tone="park" pulse>
              草稿 · v{d.next_v} · {d.stage}
            </EvolutionStatusBadge>
          ))
        )}
      </div>
      {error && <p className="text-xs text-amber-600">{error}</p>}
    </EvolutionSurface>
  );
}
