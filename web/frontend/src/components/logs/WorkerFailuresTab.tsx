import { useEffect, useState, useCallback, useRef } from "react";
import { api } from "../../api/client";
import type { WorkerFailure } from "../../api/types";
import { Badge } from "../shared/Badge";
import { Skeleton } from "../shared/Skeleton";

function FailureCard({ failure }: { failure: WorkerFailure }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div
      className="group flex gap-3 px-3 py-2 rounded-lg hover:bg-gray-50 dark:hover:bg-white/[0.03] cursor-pointer transition-colors"
      onClick={() => setExpanded(!expanded)}
    >
      <Badge variant="warning" className="shrink-0 h-fit">FAIL</Badge>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 text-xs">
          <span className="font-mono font-medium text-gray-700 dark:text-gray-200">
            v{failure.gen}
          </span>
          <span className="text-gray-400">|</span>
          <span className="text-gray-600 dark:text-gray-300">{failure.role}</span>
          {failure.worker_id && (
            <span className="text-[10px] text-gray-400 font-mono">({failure.worker_id})</span>
          )}
        </div>
        <p className="text-xs text-gray-500 dark:text-gray-400 truncate mt-0.5">
          {failure.error.slice(0, 150)}
        </p>
        {expanded && (
          <pre className="mt-2 text-[10px] font-mono text-gray-500 dark:text-gray-400 whitespace-pre-wrap bg-gray-100 dark:bg-surface-0 rounded p-2 max-h-48 overflow-y-auto">
            {failure.error}
          </pre>
        )}
      </div>
    </div>
  );
}

export default function WorkerFailuresTab() {
  const [failures, setFailures] = useState<WorkerFailure[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [genFilter, setGenFilter] = useState<number | "">("");
  const [roleFilter, setRoleFilter] = useState("");
  const abortRef = useRef<AbortController | null>(null);

  const fetchFailures = useCallback(async () => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setLoading(true);
    setError(null);
    try {
      const res = await api.workerFailures({
        gen: genFilter || undefined,
        role: roleFilter || undefined,
        limit: 200,
      });
      if (controller.signal.aborted) return;
      setFailures(res.failures);
      setTotal(res.total);
    } catch (e) {
      if (controller.signal.aborted) return;
      setFailures([]);
      setError(e instanceof Error ? e.message : "加载失败记录失败");
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, [genFilter, roleFilter]);

  useEffect(() => {
    fetchFailures();
    return () => { abortRef.current?.abort(); };
  }, [fetchFailures]);

  const uniqueGens = [...new Set(failures.map((f) => f.gen))].sort((a, b) => b - a);
  const uniqueRoles = [...new Set(failures.map((f) => f.role))].sort();

  return (
    <div className="rounded-2xl border border-gray-200 bg-white dark:border-border-subtle dark:bg-white/[0.03]">
      <div className="px-5 py-4 border-b border-gray-100 dark:border-border-subtle flex items-center justify-between gap-4 flex-wrap">
        <h3 className="text-lg font-semibold text-gray-800 dark:text-white">Worker 失败记录</h3>
        <div className="flex items-center gap-2">
          <select
            value={genFilter}
            onChange={(e) => setGenFilter(e.target.value ? Number(e.target.value) : "")}
            className="text-xs border border-gray-200 dark:border-border-subtle dark:bg-surface-1 rounded px-2 py-1"
          >
            <option value="">全部代数</option>
            {uniqueGens.map((g) => (
              <option key={g} value={g}>v{g}</option>
            ))}
          </select>
          <select
            value={roleFilter}
            onChange={(e) => setRoleFilter(e.target.value)}
            className="text-xs border border-gray-200 dark:border-border-subtle dark:bg-surface-1 rounded px-2 py-1"
          >
            <option value="">全部角色</option>
            {uniqueRoles.map((r) => (
              <option key={r} value={r}>{r}</option>
            ))}
          </select>
          <button
            onClick={fetchFailures}
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
            <button onClick={fetchFailures} className="mt-2 text-xs text-blue-500 hover:underline">重试</button>
          </div>
        ) : failures.length === 0 ? (
          <div className="text-sm text-gray-400 text-center py-8">暂无失败记录</div>
        ) : (
          <div className="space-y-0.5">
            {failures.map((f, i) => (
              <FailureCard key={`${f.gen}-${f.worker_id}-${i}`} failure={f} />
            ))}
          </div>
        )}
      </div>
      {total > 0 && (
        <div className="px-5 py-3 border-t border-gray-100 dark:border-border-subtle text-xs text-gray-400">
          共 {total} 条记录
        </div>
      )}
    </div>
  );
}
