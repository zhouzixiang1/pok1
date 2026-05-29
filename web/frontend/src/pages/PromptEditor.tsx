import { useEffect, useState, useCallback } from "react";
import { api } from "../api/client";
import type { PromptInfo } from "../api/types";
import PageMeta from "../components/common/PageMeta";

// ── Inline SVG helpers ─────────────────────────────────────────────────────────
const WarnIcon = ({ className }: { className?: string }) => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className}><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
);
const SaveIcon = ({ className }: { className?: string }) => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className}><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>
);
const GitResetIcon = ({ className }: { className?: string }) => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className}><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg>
);

const PROMPT_ORDER = ["orchestrator", "master", "worker", "reviewer", "critic", "crossover", "initial"];

export default function PromptEditor() {
  const [prompts, setPrompts] = useState<PromptInfo[]>([]);
  const [selected, setSelected] = useState<string>("orchestrator");
  const [originalContent, setOriginalContent] = useState("");
  const [editContent, setEditContent] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [message, setMessage] = useState<{ text: string; type: "success" | "error" } | null>(null);

  const isDirty = editContent !== originalContent;

  const loadPromptList = useCallback(async () => {
    try {
      const data = await api.listPrompts();
      data.sort((a, b) => PROMPT_ORDER.indexOf(a.name) - PROMPT_ORDER.indexOf(b.name));
      setPrompts(data);
    } catch {}
  }, []);

  const loadPromptContent = useCallback(async (name: string) => {
    setLoading(true);
    try {
      const text = await api.getPrompt(name);
      setOriginalContent(text);
      setEditContent(text);
    } catch (e) {
      setMessage({ text: String(e), type: "error" });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadPromptList();
  }, [loadPromptList]);

  useEffect(() => {
    loadPromptContent(selected);
  }, [selected, loadPromptContent]);

  const handleSave = async () => {
    setSaving(true);
    try {
      const r = await api.updatePrompt(selected, editContent);
      if (r.saved) {
        setOriginalContent(editContent);
        setMessage({ text: `已保存 ${r.name} — ${r.lines} 行`, type: "success" });
        await loadPromptList();
      } else {
        setMessage({ text: "保存失败", type: "error" });
      }
    } catch (e) {
      setMessage({ text: String(e), type: "error" });
    } finally {
      setSaving(false);
    }
  };

  const handleReset = async () => {
    if (!confirm(`将 ${selected} 重置为最后一次 git 提交的版本？未保存的更改将丢失。`)) return;
    setResetting(true);
    try {
      const r = await api.resetPrompt(selected);
      if (r.reset) {
        setMessage({ text: `已将 ${selected} 重置为 git 版本`, type: "success" });
        await loadPromptContent(selected);
        await loadPromptList();
      } else {
        setMessage({ text: "重置失败", type: "error" });
      }
    } catch (e) {
      setMessage({ text: String(e), type: "error" });
    } finally {
      setResetting(false);
    }
  };

  const handleDiscard = () => {
    setEditContent(originalContent);
    setMessage(null);
  };

  const handleSelect = (name: string) => {
    if (isDirty && !confirm("有未保存的更改。放弃并切换？")) return;
    setSelected(name);
    setMessage(null);
  };

  const selectedInfo = prompts.find((p) => p.name === selected);
  const lines = editContent.split("\n").length;

  return (
    <>
      <PageMeta title="提示词编辑器 — 进化仪表盘" description="编辑 LLM 提示词文件" />

      <div className="flex h-[calc(100vh-8rem)] gap-4 overflow-hidden">
        {/* Left sidebar — prompt list */}
        <div className="w-52 flex-shrink-0 rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 overflow-y-auto">
          <div className="px-4 py-3 border-b border-gray-100 dark:border-gray-700">
            <h2 className="text-sm font-semibold text-gray-700 dark:text-gray-300">提示词</h2>
          </div>
          {prompts.map((p) => (
            <button
              key={p.name}
              onClick={() => handleSelect(p.name)}
              className={`w-full text-left px-4 py-3 text-sm transition-colors ${
                selected === p.name
                  ? "bg-blue-50 text-blue-700 border-r-2 border-blue-500 dark:bg-blue-900/20 dark:text-blue-300"
                  : "text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700/50"
              }`}
            >
              <div className="font-medium">{p.name}</div>
              <div className="text-xs text-gray-400 mt-0.5">{p.lines} 行</div>
            </button>
          ))}
        </div>

        {/* Right — editor */}
        <div className="flex-1 flex flex-col overflow-hidden">
          {/* Unsaved warning */}
          {isDirty && (
            <div className="mb-2 px-4 py-2 rounded-lg bg-orange-50 dark:bg-orange-900/20 border border-orange-200 dark:border-orange-700 text-sm text-orange-700 dark:text-orange-300 flex items-center justify-between">
              <span className="flex items-center gap-1"><WarnIcon /> 有未保存的更改 — 这些将影响下一次 LLM 调用</span>
            </div>
          )}

          {/* Message */}
          {message && (
            <div className={`mb-2 px-4 py-2 rounded-lg text-sm flex items-center justify-between ${
              message.type === "success"
                ? "bg-green-50 text-green-700 dark:bg-green-900/20 dark:text-green-300"
                : "bg-red-50 text-red-700 dark:bg-red-900/20 dark:text-red-300"
            }`}>
              <span>{message.text}</span>
              <button onClick={() => setMessage(null)} className="text-xs underline ml-2">关闭</button>
            </div>
          )}

          {/* Header bar */}
          <div className="mb-2 flex items-center justify-between px-4 py-2 rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800">
            <div>
              <span className="font-semibold text-gray-800 dark:text-white">{selected}</span>
              {selectedInfo?.filename && (
                <span className="ml-2 text-xs text-gray-400">{selectedInfo.filename}</span>
              )}
              <span className="ml-3 text-xs text-gray-500">{lines} 行</span>
              {selectedInfo?.mtime_str && (
                <span className="ml-2 text-xs text-gray-400">修改于: {selectedInfo.mtime_str}</span>
              )}
            </div>
            <div className="flex gap-2">
              {isDirty && (
                <button
                  onClick={handleDiscard}
                  className="px-3 py-1 text-xs rounded bg-gray-200 dark:bg-gray-700 hover:bg-gray-300"
                >
                  放弃
                </button>
              )}
              <button
                onClick={handleReset}
                disabled={resetting}
                className="px-3 py-1 text-xs rounded bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 disabled:opacity-50 flex items-center gap-1"
              >
                <GitResetIcon /> {resetting ? "重置中..." : "重置为 Git"}
              </button>
              <button
                onClick={handleSave}
                disabled={saving || !isDirty}
                className="px-3 py-1 text-xs rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-40 flex items-center gap-1"
              >
                <SaveIcon /> {saving ? "保存中..." : "保存"}
              </button>
            </div>
          </div>

          {/* Role description */}
          {selectedInfo?.role && (
            <p className="mb-2 text-xs text-gray-500 dark:text-gray-400 px-1">
              <span className="font-medium">角色:</span> {selectedInfo.role}
            </p>
          )}

          {/* Editor */}
          <div className="flex-1 rounded-xl border border-gray-200 dark:border-gray-700 bg-gray-950 overflow-hidden">
            {loading ? (
              <div className="p-4 text-gray-500 text-sm">加载中...</div>
            ) : (
              <textarea
                value={editContent}
                onChange={(e) => setEditContent(e.target.value)}
                className="w-full h-full p-4 text-sm font-mono text-gray-200 bg-transparent resize-none outline-none leading-relaxed"
                spellCheck={false}
              />
            )}
          </div>
        </div>
      </div>
    </>
  );
}
