import { Link } from "react-router";
import type { AsyncCertificationProjection } from "../../api/control";
import { EvolutionSection, EvolutionStatusBadge, EvolutionSurface } from "./ui";
import { cn } from "../../lib/utils";

interface AsyncCertificationQueueProps {
  projection: AsyncCertificationProjection | null | undefined;
  className?: string;
}

function itemTone(state: string): "ok" | "warn" | "info" | "neutral" {
  if (state === "passed") return "ok";
  if (state === "running") return "info";
  if (state === "pending") return "warn";
  return "neutral";
}

/**
 * Async official certification queue (staging → certified). Control owns abandon;
 * this panel is read-only with a deep link.
 */
export function AsyncCertificationQueue({
  projection,
  className,
}: AsyncCertificationQueueProps) {
  const items = projection?.items ?? [];
  if (items.length === 0) {
    return (
      <EvolutionSurface className={cn(className)} padding="sm">
        <EvolutionSection
          title="异步正式认证"
          subtitle="暂无排队或进行中的 staging→certified 任务"
        />
      </EvolutionSurface>
    );
  }

  return (
    <EvolutionSurface className={cn("space-y-3", className)} padding="sm">
      <EvolutionSection
        title="异步正式认证队列"
        subtitle={projection?.any_pending ? "仍有进行中任务" : "队列已空闲"}
        actions={
          <Link to="/control" className="text-xs text-brand-600 hover:underline">
            在 Control 查看
          </Link>
        }
      />
      <ul className="space-y-2">
        {items.map((item) => (
          <li
            key={`${item.version}-${item.staging_tag}`}
            className="flex flex-wrap items-center gap-2 text-xs"
          >
            <EvolutionStatusBadge tone={itemTone(item.state)} pulse={item.state === "running"}>
              {item.state}
            </EvolutionStatusBadge>
            <span className="font-mono text-gray-800 dark:text-gray-200">
              v{item.version} · {item.bot_name}
            </span>
            <span className="text-gray-500">{item.staging_tag}</span>
            {item.certified_tag && (
              <span className="text-success-600 dark:text-success-400">{item.certified_tag}</span>
            )}
            <span className="text-gray-400">{item.formal_authority}</span>
          </li>
        ))}
      </ul>
    </EvolutionSurface>
  );
}
