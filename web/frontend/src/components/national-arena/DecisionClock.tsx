import { useEffect, useMemo, useState } from "react";
import { TimeIcon } from "../../icons";
import { cn } from "../../lib/utils";

export function DecisionClock({ deadlineEpochMs, budgetSeconds }: {
  deadlineEpochMs: number | null;
  budgetSeconds: number;
}) {
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    if (!deadlineEpochMs) return;
    const timer = window.setInterval(() => setNow(Date.now()), 100);
    return () => window.clearInterval(timer);
  }, [deadlineEpochMs]);

  const remaining = deadlineEpochMs ? Math.max(0, deadlineEpochMs - now) : 0;
  const seconds = Math.ceil(remaining / 1000);
  const ratio = useMemo(
    () => Math.max(0, Math.min(1, remaining / Math.max(1, budgetSeconds * 1000))),
    [remaining, budgetSeconds],
  );
  const danger = Boolean(deadlineEpochMs) && seconds <= 10;

  return (
    <div className="w-24" aria-label={deadlineEpochMs ? `剩余 ${seconds} 秒` : "未等待动作"}>
      <div className={cn(
        "flex h-7 items-center justify-center gap-1 rounded-md border px-2 text-xs font-semibold tabular-nums",
        danger
          ? "border-error-400 bg-error-50 text-error-700"
          : "border-white/20 bg-black/20 text-white",
      )}>
        <TimeIcon className="size-3.5" />
        {deadlineEpochMs ? `${seconds}s` : "--"}
      </div>
      <div className="mt-1 h-1 overflow-hidden rounded-full bg-black/25">
        <div
          className={cn("h-full transition-[width] duration-100", danger ? "bg-error-400" : "bg-warning-400")}
          style={{ width: `${ratio * 100}%` }}
        />
      </div>
    </div>
  );
}
