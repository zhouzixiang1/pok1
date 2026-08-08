import { Link } from "react-router";
import type { ControlStatus } from "../../api/control";
import { EvolutionSurface, EvolutionStatusBadge } from "./ui";
import { notStuckLabel } from "../../lib/notStuckReasons";
import { cn } from "../../lib/utils";

interface PhaseAProjectionStripProps {
  status: ControlStatus | null;
  className?: string;
  /** When true, manualRequired deep-links to Control abandon. */
  manualRequired?: boolean;
}

/**
 * Compact Phase A contract summary: multi-slot / pipeline_mode / eval_wait /
 * version_authority / feature flags. Does not own start/stop/abandon.
 */
export function PhaseAProjectionStrip({
  status,
  className,
  manualRequired = false,
}: PhaseAProjectionStripProps) {
  if (!status) return null;

  const mode = status.pipeline_mode;
  const evalWait = status.eval_wait;
  const va = status.version_authority;
  const flags = status.feature_flags;
  const slots = status.active_generations ?? [];
  const tips: string[] = [];

  if (mode?.enabled && mode.consumer_parked) {
    const tip = notStuckLabel("consumer_parked");
    if (tip) tips.push(tip);
  }
  if (evalWait?.waiting) {
    const tip = notStuckLabel("eval_wait");
    if (tip) tips.push(tip);
  }

  return (
    <EvolutionSurface className={cn("mb-4 space-y-2", className)} padding="sm">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="font-medium text-gray-600 dark:text-gray-300">契约摘要</span>
        <EvolutionStatusBadge tone="neutral">
          槽位 {slots.length || (status.active_generation ? 1 : 0)}
        </EvolutionStatusBadge>
        {mode?.enabled ? (
          <EvolutionStatusBadge tone={mode.consumer_parked ? "park" : "ok"} pulse={mode.consumer_parked}>
            并行车道 · 在飞数 {mode.in_flight_count}
            {mode.consumer_parked ? " · 停泊" : ""}
          </EvolutionStatusBadge>
        ) : (
          <EvolutionStatusBadge tone="neutral">并行车道关</EvolutionStatusBadge>
        )}
        {evalWait?.waiting ? (
          <EvolutionStatusBadge tone="park" pulse>
            评测等待 · {evalWait.bot ?? "?"} · {evalWait.games ?? 0}/{evalWait.min_games}
          </EvolutionStatusBadge>
        ) : (
          <EvolutionStatusBadge tone="neutral">无评测等待</EvolutionStatusBadge>
        )}
        {va && (
          <EvolutionStatusBadge tone="info">
            版本高水位 v{va.high_water} · 已配对 {va.paired_versions?.length ?? 0}
          </EvolutionStatusBadge>
        )}
        {flags?.staging_as_parent && (
          <EvolutionStatusBadge tone="warn">允许暂存父本</EvolutionStatusBadge>
        )}
        {manualRequired && (
          <Link
            to="/control#abandon"
            className="rounded-md bg-error-50 px-2 py-0.5 text-[10px] font-medium text-error-700 hover:underline dark:bg-error-900/30 dark:text-error-400"
          >
            需人工 → 控制面板放弃
          </Link>
        )}
      </div>
      {tips.length > 0 && (
        <ul className="space-y-0.5 text-[11px] text-amber-800 dark:text-amber-300">
          {tips.map((tip) => (
            <li key={tip}>· {tip}</li>
          ))}
        </ul>
      )}
    </EvolutionSurface>
  );
}
