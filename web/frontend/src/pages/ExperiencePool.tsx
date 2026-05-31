import { useEffect, useState, useCallback, useRef } from "react";
import { api } from "../api/client";
import { controlApi } from "../api/control";
import PageMeta from "../components/common/PageMeta";
import { Skeleton } from "../components/shared/Skeleton";
import { Badge } from "../components/shared/Badge";

// ── Inline SVG helpers ─────────────────────────────────────────────────────────
const ScissorsIcon = ({ className }: { className?: string }) => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className}><circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><line x1="20" y1="4" x2="8.12" y2="15.88"/><line x1="14.47" y1="14.48" x2="20" y2="20"/><line x1="8.12" y1="8.12" x2="12" y2="12"/></svg>
);
const CrystalIcon = ({ className }: { className?: string }) => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className}><circle cx="12" cy="12" r="10"/><polygon points="16.24 7.76 14.12 14.12 7.76 16.24 9.88 9.88 16.24 7.76"/></svg>
);
const SaveIcon = ({ className }: { className?: string }) => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className}><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
);
const PencilIcon = ({ className }: { className?: string }) => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className}><path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/></svg>
);
const RefreshIcon = ({ className }: { className?: string }) => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className}><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
);

export default function ExperiencePool() {
  const [content, setContent] = useState("");
  const [editContent, setEditContent] = useState("");
  const [isEditing, setIsEditing] = useState(false);
  const isEditingRef = useRef(false);
  isEditingRef.current = isEditing;
  const [appendLesson, setAppendLesson] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [toolLoading, setToolLoading] = useState<string | null>(null);
  const [message, setMessage] = useState<{ text: string; type: "success" | "error" } | null>(null);

  const refresh = useCallback(async () => {
    try {
      const text = await api.experience();
      setContent(text);
      if (!isEditingRef.current) setEditContent(text);
    } catch {}
  }, []);

  useEffect(() => {
    refresh().finally(() => setLoading(false));
    const id = setInterval(refresh, 30000);
    return () => clearInterval(id);
  }, [refresh]);

  const handleSave = async () => {
    setSaving(true);
    try {
      const r = await api.updateExperience(editContent);
      if (r.saved) {
        setContent(editContent);
        setIsEditing(false);
        setMessage({ text: `已保存 — ${r.lines} 行, ${r.chars} 字符`, type: "success" });
      } else {
        setMessage({ text: "保存失败", type: "error" });
      }
    } catch (e) {
      setMessage({ text: String(e), type: "error" });
    } finally {
      setSaving(false);
    }
  };

  const handleAppend = async () => {
    const lesson = appendLesson.trim();
    if (!lesson) return;
    try {
      const r = await api.appendExperience(lesson);
      if (r.appended) {
        setAppendLesson("");
        setMessage({ text: `经验已追加`, type: "success" });
        await refresh();
      }
    } catch (e) {
      setMessage({ text: String(e), type: "error" });
    }
  };

  const handleTool = async (toolName: string) => {
    setToolLoading(toolName);
    try {
      const r = await controlApi.callTool(toolName, {});
      setMessage({ text: r.result || r.error || "完成", type: r.error ? "error" : "success" });
      await refresh();
    } finally {
      setToolLoading(null);
    }
  };

  const entryCount = content.split("\n").filter((l) => l.trim().startsWith("-") || l.trim().startsWith("##")).length;
  const charCount = content.length;

  if (loading) return <div className="p-6 space-y-4"><Skeleton className="h-8 w-48" /><Skeleton.Card count={1} /></div>;

  return (
    <>
      <PageMeta title="经验池 — Bot 自进化" description="查看和编辑策略知识库" />

      <div className="flex items-center justify-between mb-4">
        <div>
          <h1 className="text-2xl font-bold text-gray-800 dark:text-white">经验池</h1>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-0.5">
            策略知识库 — 每次迭代 Master 都会读取
          </p>
        </div>
        <div className="flex items-center gap-2 text-xs text-gray-400">
          <span>约 {entryCount} 条经验</span>
          <span>·</span>
          <span>{charCount.toLocaleString()} 字符</span>
          <button onClick={refresh} className="ml-2 px-2 py-1 rounded bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 flex items-center gap-1">
            <RefreshIcon />
          </button>
        </div>
      </div>

      {message && (
        <div className="mb-4">
          <Badge variant={message.type === "success" ? "success" : "error"} size="md">
            {message.text}
            <button onClick={() => setMessage(null)} className="ml-2 text-xs underline opacity-70">关闭</button>
          </Badge>
        </div>
      )}

      {/* Actions toolbar */}
      <div className="mb-4 rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4 flex flex-wrap gap-3 items-start">
        {/* Append lesson */}
        <div className="flex flex-1 min-w-64 gap-2">
          <input
            type="text"
            value={appendLesson}
            onChange={(e) => setAppendLesson(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleAppend()}
            placeholder="添加新的策略经验..."
            className="flex-1 px-3 py-1.5 text-sm border border-gray-300 dark:border-gray-600 dark:bg-gray-700 rounded"
          />
          <button
            onClick={handleAppend}
            disabled={!appendLesson.trim()}
            className="px-3 py-1.5 text-sm rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-40"
          >
            + 追加
          </button>
        </div>

        <div className="flex gap-2">
          <button
            onClick={() => handleTool("trim_experience")}
            disabled={toolLoading === "trim_experience"}
            className="px-3 py-1.5 text-sm rounded bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600 flex items-center gap-1"
          >
            <ScissorsIcon /> {toolLoading === "trim_experience" ? "裁剪中..." : "裁剪"}
          </button>
          <button
            onClick={() => handleTool("consolidate_experience")}
            disabled={toolLoading === "consolidate_experience"}
            className="px-3 py-1.5 text-sm rounded bg-purple-600 text-white hover:bg-purple-700 disabled:opacity-50 flex items-center gap-1"
          >
            <CrystalIcon /> {toolLoading === "consolidate_experience" ? "整合中 (LLM)..." : "整合 (LLM)"}
          </button>

          {isEditing ? (
            <>
              <button
                onClick={handleSave}
                disabled={saving}
                className="px-3 py-1.5 text-sm rounded bg-green-600 text-white hover:bg-green-700 disabled:opacity-50 flex items-center gap-1"
              >
                <SaveIcon /> {saving ? "保存中..." : "保存"}
              </button>
              <button
                onClick={() => { setEditContent(content); setIsEditing(false); }}
                className="px-3 py-1.5 text-sm rounded bg-gray-300 dark:bg-gray-600 hover:bg-gray-400"
              >
                放弃
              </button>
            </>
          ) : (
            <button
              onClick={() => { setEditContent(content); setIsEditing(true); }}
              className="px-3 py-1.5 text-sm rounded bg-yellow-500 text-white hover:bg-yellow-600 flex items-center gap-1"
            >
              <PencilIcon /> 编辑
            </button>
          )}
        </div>
      </div>

      {/* Content */}
      <div className="rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 overflow-hidden">
        {isEditing ? (
          <textarea
            value={editContent}
            onChange={(e) => setEditContent(e.target.value)}
            className="w-full h-[600px] p-4 text-sm font-mono text-gray-800 dark:text-gray-200 bg-transparent resize-none outline-none leading-relaxed"
            spellCheck={false}
          />
        ) : (
          <pre className="p-4 text-sm font-mono text-gray-700 dark:text-gray-300 whitespace-pre-wrap leading-relaxed overflow-auto max-h-[600px]">
            {content || <span className="text-gray-400 italic">经验池为空。</span>}
          </pre>
        )}
      </div>
    </>
  );
}
