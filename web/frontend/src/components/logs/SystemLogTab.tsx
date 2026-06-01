import { useEffect, useState, useCallback, useRef } from "react";
import { api } from "../../api/client";
import type { SystemEvent } from "../../api/types";
import { Badge } from "../shared/Badge";
import { Skeleton } from "../shared/Skeleton";

const SEVERITY_CONFIG: Record<string, { label: string; variant: "success" | "warning" | "error" | "info" | "neutral" }> = {
  info: { label: "INFO", variant: "info" },
  success: { label: "OK", variant: "success" },
  warn: { label: "WARN", variant: "warning" },
  error: { label: "ERR", variant: "error" },
};

const CATEGORY_MAP: Record<string, string> = {
  "": "全部",
  "pipeline.": "Pipeline",
  "orchestrator.": "编排器",
  "daemon.": "守护进程",
  "bot.": "Bot 生命周期",
};

function formatTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString("zh-CN", { hour12: false });
}

function EventCard({ event }: { event: SystemEvent }) {
  const [expanded, setExpanded] = useState(false);
  const cfg = SEVERITY_CONFIG[event.severity] ?? SEVERITY_CONFIG.info;
  const typeParts = event.type.split(".");

  return (
    <div
      className="group flex gap-3 px-3 py-2 rounded-lg hover:bg-gray-50 dark:hover:bg-white/[0.03] cursor-pointer transition-colors"
      onClick={() => setExpanded(!expanded)}
    >
      <span className="text-[10px] font-mono text-gray-400 shrink-0 pt-0.5 w-16">
        {formatTime(event.ts)}
      </span>
      <Badge variant={cfg.variant} className="shrink-0">{cfg.label}</Badge>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="text-[10px] font-mono text-gray-500 dark:text-gray-400">
            {typeParts[0]}
          </span>
          <span className="text-xs font-medium text-gray-700 dark:text-gray-200 truncate">
            {typeParts.slice(1).join(".")}
          </span>
        </div>
        <p className="text-xs text-gray-600 dark:text-gray-300 truncate mt-0.5">
          {event.message}
        </p>
        {expanded && event.data && Object.keys(event.data).length > 0 && (
          <pre className="mt-2 text-[10px] font-mono text-gray-500 dark:text-gray-400 whitespace-pre-wrap bg-gray-100 dark:bg-surface-0 rounded p-2 max-h-48 overflow-y-auto">
            {JSON.stringify(event.data, null, 2)}
          </pre>
        )}
      </div>
    </div>
  );
}

export default function SystemLogTab() {
  const [events, setEvents] = useState<SystemEvent[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [severity, setSeverity] = useState("");
  const [category, setCategory] = useState("");
  const [offset, setOffset] = useState(0);
  const LIMIT = 100;
  const abortRef = useRef<AbortController | null>(null);

  const fetchEvents = useCallback(async () => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setLoading(true);
    setError(null);
    try {
      const res = await api.systemEvents({
        type: category,
        severity: severity || undefined,
        limit: LIMIT,
        offset,
      });
      if (controller.signal.aborted) return;
      setEvents(res.events);
      setTotal(res.total);
    } catch (e) {
      if (controller.signal.aborted) return;
      setEvents([]);
      setError(e instanceof Error ? e.message : "加载系统日志失败");
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, [category, severity, offset]);

  useEffect(() => {
    fetchEvents();
    return () => { abortRef.current?.abort(); };
  }, [fetchEvents]);

  const hasMore = offset + events.length < total;

  return (
    <div className="rounded-2xl border border-gray-200 bg-white dark:border-border-subtle dark:bg-white/[0.03]">
      <div className="px-5 py-4 border-b border-gray-100 dark:border-border-subtle flex items-center justify-between gap-4 flex-wrap">
        <h3 className="text-lg font-semibold text-gray-800 dark:text-white">系统日志</h3>
        <div className="flex items-center gap-2">
          <select
            value={severity}
            onChange={(e) => { setSeverity(e.target.value); setOffset(0); }}
            className="text-xs border border-gray-200 dark:border-border-subtle dark:bg-surface-1 rounded px-2 py-1"
          >
            <option value="">全部级别</option>
            <option value="info">Info</option>
            <option value="success">Success</option>
            <option value="warn">Warn</option>
            <option value="error">Error</option>
          </select>
          <select
            value={category}
            onChange={(e) => { setCategory(e.target.value); setOffset(0); }}
            className="text-xs border border-gray-200 dark:border-border-subtle dark:bg-surface-1 rounded px-2 py-1"
          >
            {Object.entries(CATEGORY_MAP).map(([k, v]) => (
              <option key={k} value={k}>{v}</option>
            ))}
          </select>
          <button
            onClick={fetchEvents}
            className="text-xs px-3 py-1 rounded bg-gray-100 dark:bg-surface-1 hover:bg-gray-200 dark:hover:bg-gray-700 transition-colors"
          >
            刷新
          </button>
        </div>
      </div>
      <div className="p-4 max-h-[600px] overflow-y-auto">
        {loading ? (
          <div className="space-y-2"><Skeleton.Card count={3} /></div>
        ) : error ? (
          <div className="flex flex-col items-center justify-center py-8 text-center">
            <p className="text-sm text-red-500">{error}</p>
            <button onClick={fetchEvents} className="mt-2 text-xs text-blue-500 hover:underline">重试</button>
          </div>
        ) : events.length === 0 ? (
          <div className="text-sm text-gray-400 text-center py-8">暂无系统事件</div>
        ) : (
          <div className="space-y-0.5">
            {events.map((ev, i) => (
              <EventCard key={`${ev.ts}-${i}`} event={ev} />
            ))}
          </div>
        )}
      </div>
      {total > LIMIT && (
        <div className="px-5 py-3 border-t border-gray-100 dark:border-border-subtle flex items-center justify-between text-xs text-gray-400">
          <span>显示 {offset + 1}-{offset + events.length} / {total}</span>
          <div className="flex gap-2">
            <button
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - LIMIT))}
              className="px-2 py-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-30"
            >
              上一页
            </button>
            {hasMore && (
              <button
                onClick={() => setOffset(offset + LIMIT)}
                className="px-2 py-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700"
              >
                下一页
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
