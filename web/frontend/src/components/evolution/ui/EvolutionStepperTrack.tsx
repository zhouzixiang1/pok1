import { cn } from "../../../lib/utils";
import { EVOLUTION_RADIUS } from "./tokens";

export type StepperStepStatus = "pending" | "running" | "completed" | "error" | "blocked";

export interface StepperStep {
  id: string;
  label: string;
  status: StepperStepStatus;
  detail?: string;
}

interface EvolutionStepperTrackProps {
  steps: StepperStep[];
  className?: string;
  compact?: boolean;
}

/**
 * Shared track for generation + handoff steppers.
 * Only the current (running) node pulses — no decorative glow.
 */
export function EvolutionStepperTrack({
  steps,
  className,
  compact = false,
}: EvolutionStepperTrackProps) {
  return (
    <div className={cn("flex items-center gap-0 overflow-x-auto py-2", className)}>
      {steps.map((step, i) => {
        const done = step.status === "completed";
        const active = step.status === "running";
        const failed = step.status === "error" || step.status === "blocked";
        return (
          <div key={step.id} className="flex items-center shrink-0">
            <div className="flex flex-col items-center">
              <div
                className={cn(
                  "relative flex items-center justify-center border-2 font-bold transition-all duration-300",
                  EVOLUTION_RADIUS.trackNode,
                  compact ? "h-6 w-6 text-[9px]" : "h-7 w-7 text-[10px]",
                  done && "border-success-500 bg-success-500 text-white",
                  active && "border-brand-500 bg-brand-50 text-brand-700 dark:bg-brand-950/40 dark:text-brand-300",
                  failed && "border-error-500 bg-error-50 text-error-700 dark:bg-error-950/40 dark:text-error-300",
                  !done && !active && !failed && "border-gray-300 bg-white text-gray-400 dark:border-gray-600 dark:bg-surface-0",
                )}
                title={step.detail ?? step.label}
              >
                {done ? "✓" : step.status === "error" ? "!" : i + 1}
                {active && (
                  <span className="absolute -inset-1 animate-pulse rounded-full border border-brand-400/60" />
                )}
              </div>
              <span
                className={cn(
                  "mt-1 max-w-[4.5rem] truncate text-center text-[10px]",
                  active ? "font-semibold text-brand-700 dark:text-brand-300" : "text-gray-500",
                )}
              >
                {step.label}
              </span>
            </div>
            {i < steps.length - 1 && (
              <div
                className={cn(
                  "mx-1 mb-4 h-0.5 w-6 shrink-0",
                  done ? "bg-success-400" : "bg-gray-200 dark:bg-gray-700",
                )}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}
