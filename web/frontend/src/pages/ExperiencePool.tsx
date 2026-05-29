import { useEffect, useState, useCallback } from "react";
import { api } from "../api/client";
import { controlApi } from "../api/control";
import PageMeta from "../components/common/PageMeta";

export default function ExperiencePool() {
  const [content, setContent] = useState("");
  const [editContent, setEditContent] = useState("");
  const [isEditing, setIsEditing] = useState(false);
  const [appendLesson, setAppendLesson] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [toolLoading, setToolLoading] = useState<string | null>(null);
  const [message, setMessage] = useState<{ text: string; type: "success" | "error" } | null>(null);

  const refresh = useCallback(async () => {
    try {
      const text = await api.experience();
      setContent(text);
      if (!isEditing) setEditContent(text);
    } catch {}
  }, [isEditing]);

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
        setMessage({ text: `Saved — ${r.lines} lines, ${r.chars} chars`, type: "success" });
      } else {
        setMessage({ text: "Save failed", type: "error" });
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
        setMessage({ text: `Lesson appended`, type: "success" });
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
      setMessage({ text: r.result || r.error || "Done", type: r.error ? "error" : "success" });
      await refresh();
    } finally {
      setToolLoading(null);
    }
  };

  // Count experience entries (lines starting with "-" or "##")
  const entryCount = content.split("\n").filter((l) => l.trim().startsWith("-") || l.trim().startsWith("##")).length;
  const charCount = content.length;

  if (loading) return <div className="p-6 text-gray-500">Loading...</div>;

  return (
    <>
      <PageMeta title="Experience Pool — Evolution Dashboard" description="View and edit the strategic experience pool" />

      <div className="flex items-center justify-between mb-4">
        <div>
          <h1 className="text-2xl font-bold text-gray-800 dark:text-white">Experience Pool</h1>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-0.5">
            Shared strategic memory — Master reads this every generation
          </p>
        </div>
        <div className="flex items-center gap-2 text-xs text-gray-400">
          <span>~{entryCount} entries</span>
          <span>·</span>
          <span>{charCount.toLocaleString()} chars</span>
          <button onClick={refresh} className="ml-2 px-2 py-1 rounded bg-gray-200 dark:bg-gray-700 hover:bg-gray-300">↺</button>
        </div>
      </div>

      {message && (
        <div className={`mb-4 px-4 py-2 rounded-lg text-sm ${message.type === "success" ? "bg-green-50 text-green-700 dark:bg-green-900/20 dark:text-green-300" : "bg-red-50 text-red-700 dark:bg-red-900/20 dark:text-red-300"}`}>
          {message.text}
          <button onClick={() => setMessage(null)} className="ml-2 text-xs underline">×</button>
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
            placeholder="Add a new strategic lesson..."
            className="flex-1 px-3 py-1.5 text-sm border border-gray-300 dark:border-gray-600 dark:bg-gray-700 rounded"
          />
          <button
            onClick={handleAppend}
            disabled={!appendLesson.trim()}
            className="px-3 py-1.5 text-sm rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-40"
          >
            + Append
          </button>
        </div>

        <div className="flex gap-2">
          <button
            onClick={() => handleTool("trim_experience")}
            disabled={toolLoading === "trim_experience"}
            className="px-3 py-1.5 text-sm rounded bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600"
          >
            {toolLoading === "trim_experience" ? "Trimming..." : "✂ Trim"}
          </button>
          <button
            onClick={() => handleTool("consolidate_experience")}
            disabled={toolLoading === "consolidate_experience"}
            className="px-3 py-1.5 text-sm rounded bg-purple-600 text-white hover:bg-purple-700 disabled:opacity-50"
          >
            {toolLoading === "consolidate_experience" ? "Consolidating (LLM)..." : "🔮 Consolidate (LLM)"}
          </button>

          {isEditing ? (
            <>
              <button
                onClick={handleSave}
                disabled={saving}
                className="px-3 py-1.5 text-sm rounded bg-green-600 text-white hover:bg-green-700 disabled:opacity-50"
              >
                {saving ? "Saving..." : "💾 Save"}
              </button>
              <button
                onClick={() => { setEditContent(content); setIsEditing(false); }}
                className="px-3 py-1.5 text-sm rounded bg-gray-300 dark:bg-gray-600 hover:bg-gray-400"
              >
                Discard
              </button>
            </>
          ) : (
            <button
              onClick={() => { setEditContent(content); setIsEditing(true); }}
              className="px-3 py-1.5 text-sm rounded bg-yellow-500 text-white hover:bg-yellow-600"
            >
              ✏ Edit
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
            {content || <span className="text-gray-400 italic">Experience pool is empty.</span>}
          </pre>
        )}
      </div>
    </>
  );
}
