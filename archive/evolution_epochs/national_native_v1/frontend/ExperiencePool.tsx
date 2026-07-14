// Archived national_native_v1 surface; the active UI exposes no /experience route.
import { useCallback, useEffect, useState } from "react";
import type { NativeExperienceView } from "../api/types";
import { api } from "../api/client";
import PageMeta from "../components/common/PageMeta";
import { Skeleton } from "../components/shared/Skeleton";

const RefreshIcon = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><polyline points="23 4 23 10 17 10" /><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" /></svg>
);

export default function ExperiencePool() {
  const [view, setView] = useState<NativeExperienceView | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      setError("");
      setView(await api.experience());
    } catch {
      setView(null);
      setError("当前 national_tcp_policy_v1 证据身份不可用；为避免旧经验混入，页面已保持空白。");
    }
  }, []);

  useEffect(() => {
    refresh().finally(() => setLoading(false));
    const timer = setInterval(refresh, 30000);
    return () => clearInterval(timer);
  }, [refresh]);

  if (loading) {
    return <div className="space-y-4 p-6"><Skeleton className="h-8 w-56" /><Skeleton.Card count={2} /></div>;
  }

  return (
    <>
      <PageMeta title="原生回放经验 — Bot 自进化" description="只读展示当前证据身份支持的国赛原生回放经验" />
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-gray-800 dark:text-white">原生回放经验</h1>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">只读 · battle_lessons.jsonl · 不读取或兼容旧 Markdown 经验池</p>
        </div>
        <button onClick={refresh} className="flex items-center gap-1 rounded bg-gray-200 px-3 py-1.5 text-xs hover:bg-gray-300 dark:bg-gray-700 dark:hover:bg-gray-600"><RefreshIcon />刷新</button>
      </div>

      {error && <div className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700 dark:border-red-900/50 dark:bg-red-950/30 dark:text-red-300">{error}</div>}

      {view && (
        <>
          <div className="mb-4 grid gap-3 sm:grid-cols-3">
            <div className="rounded-xl border border-gray-200 bg-white p-3 dark:border-border-subtle dark:bg-white/[0.03]"><div className="text-xs text-gray-400">执行协议</div><div className="mt-1 font-mono text-sm">{view.execution_mode}</div></div>
            <div className="rounded-xl border border-gray-200 bg-white p-3 dark:border-border-subtle dark:bg-white/[0.03]"><div className="text-xs text-gray-400">评估纪元</div><div className="mt-1 font-mono text-sm">{view.epoch}</div></div>
            <div className="rounded-xl border border-gray-200 bg-white p-3 dark:border-border-subtle dark:bg-white/[0.03]"><div className="text-xs text-gray-400">证据身份</div><div className="mt-1 truncate font-mono text-sm" title={view.evaluation_identity_digest}>{view.evaluation_identity_digest}</div></div>
          </div>

          <div className="mb-4 rounded-xl border border-blue-200 bg-blue-50 p-3 text-sm text-blue-800 dark:border-blue-900/40 dark:bg-blue-950/20 dark:text-blue-300">{view.message}</div>

          <div className="space-y-3">
            {view.lessons.map((lesson) => (
              <article key={lesson.lesson_id} className="rounded-xl border border-gray-200 bg-white p-4 dark:border-border-subtle dark:bg-white/[0.03]">
                <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
                  <span className="rounded bg-emerald-100 px-2 py-0.5 text-emerald-800 dark:bg-emerald-900/30 dark:text-emerald-300">{lesson.scope}</span>
                  <span className="text-gray-500">{lesson.section}</span>
                  <span className="ml-auto font-mono text-gray-400">{lesson.lesson_id}</span>
                </div>
                <p className="text-sm leading-relaxed text-gray-700 dark:text-gray-200">{lesson.text}</p>
                <div className="mt-3 flex flex-wrap gap-1 text-[11px] text-gray-400">
                  <span>证据：</span>
                  {lesson.evidence_ids.map((evidenceId) => <code key={evidenceId} className="rounded bg-gray-100 px-1 dark:bg-gray-800">{evidenceId}</code>)}
                </div>
              </article>
            ))}
            {view.lessons.length === 0 && (
              <div className="rounded-xl border border-dashed border-gray-300 bg-white p-10 text-center text-sm text-gray-500 dark:border-border-subtle dark:bg-white/[0.02]">当前身份还没有通过证据引用校验的经验；旧产物不会在此显示。</div>
            )}
          </div>
        </>
      )}
    </>
  );
}
