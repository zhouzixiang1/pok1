import type { StabilityObservation } from "../../api/control";
import { stabilityPresentation } from "../../lib/stabilityView";
import { Badge } from "../shared/Badge";

export function StabilityStatus({
  observation,
  compact = false,
}: {
  observation: StabilityObservation | null | undefined;
  compact?: boolean;
}) {
  const presentation = stabilityPresentation(observation);
  const countVerified = observation?.verification?.state === "fresh"
    && presentation.variant !== "error";
  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      {!compact && observation && countVerified && (
        <span className="font-mono tabular-nums">{observation.count}/{observation.target}</span>
      )}
      <Badge variant={presentation.variant} size="sm">{presentation.label}</Badge>
      {!compact && <span className="text-xs text-gray-500 dark:text-gray-400">{presentation.detail}</span>}
    </span>
  );
}
