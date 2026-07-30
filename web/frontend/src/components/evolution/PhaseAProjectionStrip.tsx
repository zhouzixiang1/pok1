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
    const tip = notStuckLabel(evalWait.degraded ? "eval_wait_degraded" : "eval_wait");
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
            slice2b · in_flight={mode.in_flight_count}
            {mode.consumer_parked ? " · parked" : ""}
          </EvolutionStatusBadge>
        ) : (
          <EvolutionStatusBadge tone="neutral">slice2b 关</EvolutionStatusBadge>
        )}
        {evalWait?.waiting ? (
          <EvolutionStatusBadge tone="park" pulse>
            eval_wait · {evalWait.bot ?? "?"} · {evalWait.games ?? 0}/{evalWait.min_games}
          </EvolutionStatusBadge>
        ) : (
          <EvolutionStatusBadge tone="neutral">eval_wait 无</EvolutionStatusBadge>
        )}
        {va && (
          <EvolutionStatusBadge tone="info">
            权威 hw={va.high_water} · paired={va.paired_versions?.length ?? 0}
          </EvolutionStatusBadge>
        )}
        {flags?.staging_as_parent && (
          <EvolutionStatusBadge tone="warn">staging parent 允许</EvolutionStatusBadge>
        )}
        {manualRequired && (
          <Link
            to="/control#abandon"
            className="rounded-md bg-error-50 px-2 py-0.5 text-[10px] font-medium text-error-700 hover:underline dark:bg-error-900/30 dark:text-error-400"
          >
            需人工 → Control abandon
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
