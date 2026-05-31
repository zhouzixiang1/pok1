import { type ReactNode } from "react";
import { cn } from "../../lib/utils";

interface CardProps {
  children: ReactNode;
  variant?: "solid" | "glass" | "danger";
  className?: string;
  padding?: string;
}

export function Card({ children, variant = "solid", className, padding = "p-4" }: CardProps) {
  return (
    <div
      className={cn(
        "rounded-2xl border transition-colors",
        variant === "solid" && "border-gray-200 bg-white dark:border-gray-800 dark:bg-white/[0.04]",
        variant === "glass" && "border-white/[0.08] bg-white/[0.04] backdrop-blur-xl dark:shadow-[inset_0_1px_0_rgba(255,255,255,0.05)]",
        variant === "danger" && "border-error-200 bg-error-50 dark:border-error-900/30 dark:bg-error-950/20",
        padding,
        className,
      )}
    >
      {children}
    </div>
  );
}
