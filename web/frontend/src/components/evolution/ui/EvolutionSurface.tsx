import type { ReactNode } from "react";
import { cn } from "../../../lib/utils";
import { EVOLUTION_RADIUS } from "./tokens";

interface EvolutionSurfaceProps {
  children: ReactNode;
  className?: string;
  padding?: "sm" | "md" | "lg";
}

const PADDING = {
  sm: "p-4",
  md: "p-5",
  lg: "p-6",
} as const;

/** Unified evolution card: rounded-2xl, border, tokenized padding. */
export function EvolutionSurface({
  children,
  className,
  padding = "md",
}: EvolutionSurfaceProps) {
  return (
    <div
      className={cn(
        EVOLUTION_RADIUS.surface,
        "border border-gray-200 bg-white dark:border-border-subtle dark:bg-surface-1",
        PADDING[padding],
        className,
      )}
    >
      {children}
    </div>
  );
}
