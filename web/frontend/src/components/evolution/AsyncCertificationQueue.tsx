import { Link } from "react-router";
import type { AsyncCertificationItem, AsyncCertificationProjection } from "../../api/control";
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

/** Operator-facing Chinese label for the async-cert job state.
 *
 * `passed` means the staging bot has obtained its signed full certificate and
 * reached the certified tier. `pending`/`running` mean the staging bot is
 * still awaiting its async official EXE cert — that is the EXPECTED state of a
 * staging publication, not a failure. */
function itemStateLabel(item: AsyncCertificationItem): string {
  if (item.state === "passed") return "已认证";
  if (item.state === "running") return "认证中";
  if (item.state === "pending") return "待认证";
  return item.state;
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

  const passedCount = items.filter((it) => it.state === "passed").length;
  const pendingCount = items.length - passedCount;

  return (
    <EvolutionSurface className={cn("space-y-3", className)} padding="sm">
      <EvolutionSection
        title="异步正式认证队列"
        subtitle={`staging→certified：已认证 ${passedCount} · 待认证 ${pendingCount}${projection?.any_pending ? "（进行中）" : "（队列空闲）"}`}
        actions={
          <Link to="/control" className="text-xs text-brand-600 hover:underline">
            在控制面板查看
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
              {itemStateLabel(item)}
            </EvolutionStatusBadge>
            <span className="font-mono text-gray-800 dark:text-gray-200">
              v{item.version} · {item.bot_name}
            </span>
            <span className="text-gray-500">{item.staging_tag}</span>
            {item.certified_tag && (
              <span className="text-success-600 dark:text-success-400">{item.certified_tag}</span>
            )}
          </li>
        ))}
      </ul>
      <p className="text-[11px] text-gray-500 dark:text-gray-400">
        待认证 / 认证中：staging 已发布，正在补跑官方 EXE 证书（属预期状态，非失败）；已认证：取得正式证书，可进入评分池。
      </p>
    </EvolutionSurface>
  );
}
