/**
 * A compact "last updated + auto-refresh countdown" badge.
 *
 * The backend's read-only observer projection (`/api/control/health`) can take
 * ~76s to rebuild during an active generation, during which the dashboard shows
 * a neutral "正在核对…" / "正在刷新…" state that can look frozen. This badge
 * turns that ambiguity into explicit, live feedback so an operator always knows
 * the data is not stale and when the next refresh will attempt:
 *
 *   上次更新 10:15:03 · 4s 后自动刷新
 *
 * It is pure presentational chrome: it takes the last successful observation
 * timestamp and the configured poll interval and ticks a 1s countdown. When the
 * countdown reaches zero it just keeps showing 0s (the next poll fires on the
 * hook's own recursive setTimeout, not here) — the value is informational, not a
 * trigger. Mirrors the last-updated pattern in LlmMetrics.tsx and the live-tick
 * pattern in national-arena/DecisionClock.tsx.
 */
import { useEffect, useState } from "react";

interface RefreshStatusBadgeProps {
  /** Epoch-ms of the last successful /health observation, or null if never. */
  lastUpdated: number | null;
  /** Poll interval in ms (the countdown target). Defaults to 5000. */
  pollMs?: number;
  className?: string;
}

function formatClock(ms: number): string {
  return new Date(ms).toLocaleTimeString("zh-CN", { hour12: false });
}

export function RefreshStatusBadge({
  lastUpdated,
  pollMs = 5_000,
  className,
}: RefreshStatusBadgeProps) {
  // Tick once per second so the countdown stays live without re-rendering the
  // whole status tree. State is just "now"; derived values recompute each tick.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  const updatedLabel = lastUpdated ? formatClock(lastUpdated) : "—";
  // Seconds remaining until the next scheduled poll. lastUpdated is the anchor;
  // a poll that just fired shows ~pollMs/1000 remaining and counts down. If we
  // have never observed (lastUpdated null) or the interval has somehow elapsed
  // (a slow poll), clamp at 0 — the next attempt is effectively imminent.
  const remaining = lastUpdated
    ? Math.max(0, Math.ceil((lastUpdated + pollMs - now) / 1000))
    : 0;

  return (
    <span className={className}>
      上次更新 {updatedLabel} · {remaining}s 后自动刷新
    </span>
  );
}
