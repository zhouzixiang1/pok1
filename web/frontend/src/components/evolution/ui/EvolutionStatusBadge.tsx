import type { ReactNode } from "react";
import { cn } from "../../../lib/utils";
import { EVOLUTION_RADIUS, STATUS_TONE_CLASSES, type EvolutionStatusTone } from "./tokens";

interface EvolutionStatusBadgeProps {
  tone?: EvolutionStatusTone;
  children: ReactNode;
  pulse?: boolean;
  className?: string;
  size?: "sm" | "md";
}

/** Semantic badge: ok / warn / error / info / neutral / park. */
export function EvolutionStatusBadge({
  tone = "neutral",
  children,
  pulse,
  className,
  size = "sm",
}: EvolutionStatusBadgeProps) {
  const styles = STATUS_TONE_CLASSES[tone];
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 font-medium",
        EVOLUTION_RADIUS.badge,
        size === "sm" ? "px-2 py-0.5 text-[10px]" : "px-2.5 py-1 text-xs",
        styles.badge,
        className,
      )}
    >
      {pulse && (
        <span className="relative flex h-1.5 w-1.5">
          <span className={cn("absolute inline-flex h-full w-full animate-ping rounded-full opacity-75", styles.dot)} />
          <span className={cn("relative inline-flex h-1.5 w-1.5 rounded-full", styles.dot)} />
        </span>
      )}
      {children}
    </span>
  );
}
