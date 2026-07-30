import type { ReactNode } from "react";
import { EvolutionStreamShell, EvolutionStatusBadge } from "./ui";

interface EvolutionStreamPanelProps {
  title?: string;
  subtitle?: string;
  connected: boolean;
  statusText: string;
  isWorking?: boolean;
  slotLabel?: string | null;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}

/**
 * Shared evolution SSE stream panel. Agents is the sole live route; legacy
 * /evolution redirects here. Visual language goes through EvolutionStreamShell.
 */
export function EvolutionStreamPanel({
  title = "研发执行流",
  subtitle = "唯一实时 SSE；slot 标注区分 primary / draft",
  connected,
  statusText,
  isWorking = false,
  slotLabel = null,
  actions,
  children,
  className,
  bodyClassName,
}: EvolutionStreamPanelProps) {
  return (
    <EvolutionStreamShell
      title={title}
      subtitle={subtitle}
      className={className}
      bodyClassName={bodyClassName}
      status={
        <>
          <EvolutionStatusBadge tone={connected ? "ok" : "warn"} pulse={isWorking}>
            {connected ? (isWorking ? "工作中" : "已连接") : "未连接"}
          </EvolutionStatusBadge>
          {slotLabel && (
            <EvolutionStatusBadge tone="info">{slotLabel}</EvolutionStatusBadge>
          )}
          <span className="text-[10px] text-gray-500 dark:text-gray-400">{statusText}</span>
        </>
      }
      actions={actions}
    >
      {children}
    </EvolutionStreamShell>
  );
}
