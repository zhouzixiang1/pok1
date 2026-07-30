import type { ReactNode } from "react";
import { cn } from "../../../lib/utils";
import { STREAM_SHELL } from "./tokens";

interface EvolutionStreamShellProps {
  title: string;
  subtitle?: string;
  status?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}

/**
 * Terminal-stream shell: outer Evolution Surface language; inner monospace
 * gray-950 pane — eliminates the "separate site" feel of raw #0d1117 blocks.
 */
export function EvolutionStreamShell({
  title,
  subtitle,
  status,
  actions,
  children,
  className,
  bodyClassName,
}: EvolutionStreamShellProps) {
  return (
    <div className={cn(STREAM_SHELL.outer, "overflow-hidden", className)}>
      <div className={cn(STREAM_SHELL.titleBar, "flex items-start justify-between gap-3")}>
        <div>
          <h3 className="text-sm font-semibold text-gray-800 dark:text-white">{title}</h3>
          {subtitle && (
            <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">{subtitle}</p>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {status}
          {actions}
        </div>
      </div>
      <div className={cn(STREAM_SHELL.body, "min-h-[12rem] p-3", bodyClassName)}>
        {children}
      </div>
    </div>
  );
}
