import type { ReactNode } from "react";
import { cn } from "../../../lib/utils";

interface EvolutionSectionProps {
  title: string;
  subtitle?: string;
  children?: ReactNode;
  className?: string;
  actions?: ReactNode;
}

/** Page-section title ladder: text-lg title + text-sm muted subtitle. */
export function EvolutionSection({
  title,
  subtitle,
  children,
  className,
  actions,
}: EvolutionSectionProps) {
  return (
    <section className={cn("space-y-3", className)}>
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-gray-800 dark:text-white">{title}</h2>
          {subtitle && (
            <p className="mt-0.5 text-sm font-medium text-gray-500 dark:text-gray-400">{subtitle}</p>
          )}
        </div>
        {actions}
      </div>
      {children}
    </section>
  );
}
