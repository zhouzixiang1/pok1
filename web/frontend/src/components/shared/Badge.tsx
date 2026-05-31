import { type ReactNode } from "react";
import { cn } from "../../lib/utils";

type BadgeVariant = "success" | "warning" | "error" | "info" | "neutral";

interface BadgeProps {
  variant?: BadgeVariant;
  size?: "sm" | "md";
  pulse?: boolean;
  children: ReactNode;
  className?: string;
}

const variantStyles: Record<BadgeVariant, string> = {
  success:
    "bg-success-50 text-success-700 dark:bg-success-900/30 dark:text-success-400",
  warning:
    "bg-warning-50 text-warning-700 dark:bg-warning-900/30 dark:text-warning-400",
  error:
    "bg-error-50 text-error-700 dark:bg-error-900/30 dark:text-error-400",
  info:
    "bg-blue-light-50 text-blue-light-700 dark:bg-blue-light-900/30 dark:text-blue-light-400",
  neutral:
    "bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-400",
};

export function Badge({ variant = "neutral", size = "sm", pulse, children, className }: BadgeProps) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full font-medium",
        size === "sm" ? "px-2 py-0.5 text-[10px]" : "px-2.5 py-1 text-xs",
        variantStyles[variant],
        className,
      )}
    >
      {pulse && (
        <span className="relative flex h-1.5 w-1.5">
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full opacity-75 bg-current" />
          <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-current" />
        </span>
      )}
      {children}
    </span>
  );
}
