import { type ReactNode } from "react";
import { cn } from "../../lib/utils";

interface CardHeaderProps {
  title: string;
  subtitle?: string;
  actions?: ReactNode;
  className?: string;
}

export function CardHeader({ title, subtitle, actions, className }: CardHeaderProps) {
  return (
    <div className={cn("flex items-center justify-between border-b border-gray-100 px-5 py-4 dark:border-border-subtle", className)}>
      <div>
        <h3 className="text-lg font-semibold text-gray-800 dark:text-white">{title}</h3>
        {subtitle && <p className="mt-0.5 text-xs text-gray-400">{subtitle}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}
