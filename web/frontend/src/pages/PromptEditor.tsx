import { useEffect, useState, useCallback } from "react";
import { api } from "../api/client";
import type { PromptInfo } from "../api/types";
import PageMeta from "../components/common/PageMeta";

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
      // Sort in preferred order
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
        setMessage({ text: `Saved ${r.name} — ${r.lines} lines`, type: "success" });
        await loadPromptList();
      } else {
        setMessage({ text: "Save failed", type: "error" });
      }
    } catch (e) {
      setMessage({ text: String(e), type: "error" });
    } finally {
      setSaving(false);
    }
  };

  const handleReset = async () => {
    if (!confirm(`Reset ${selected} to the last git-committed version? Unsaved changes will be lost.`)) return;
    setResetting(true);
    try {
      const r = await api.resetPrompt(selected);
      if (r.reset) {
        setMessage({ text: `Reset ${selected} to git version`, type: "success" });
        await loadPromptContent(selected);
        await loadPromptList();
      } else {
        setMessage({ text: "Reset failed", type: "error" });
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
    if (isDirty && !confirm("You have unsaved changes. Discard and switch?")) return;
    setSelected(name);
    setMessage(null);
  };

  const selectedInfo = prompts.find((p) => p.name === selected);
  const lines = editContent.split("\n").length;

  return (
    <>
      <PageMeta title="Prompt Editor — Evolution Dashboard" description="Edit LLM prompt files" />

      <div className="flex h-[calc(100vh-8rem)] gap-4 overflow-hidden">
        {/* Left sidebar — prompt list */}
        <div className="w-52 flex-shrink-0 rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 overflow-y-auto">
          <div className="px-4 py-3 border-b border-gray-100 dark:border-gray-700">
            <h2 className="text-sm font-semibold text-gray-700 dark:text-gray-300">Prompts</h2>
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
              <div className="text-xs text-gray-400 mt-0.5">{p.lines} lines</div>
            </button>
          ))}
        </div>

        {/* Right — editor */}
        <div className="flex-1 flex flex-col overflow-hidden">
          {/* Unsaved warning */}
          {isDirty && (
            <div className="mb-2 px-4 py-2 rounded-lg bg-orange-50 dark:bg-orange-900/20 border border-orange-200 dark:border-orange-700 text-sm text-orange-700 dark:text-orange-300 flex items-center justify-between">
              <span>⚠ Unsaved changes — these will affect the next LLM call</span>
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
              <button onClick={() => setMessage(null)} className="text-xs underline ml-2">×</button>
            </div>
          )}

          {/* Header bar */}
          <div className="mb-2 flex items-center justify-between px-4 py-2 rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800">
            <div>
              <span className="font-semibold text-gray-800 dark:text-white">{selected}</span>
              {selectedInfo?.filename && (
                <span className="ml-2 text-xs text-gray-400">{selectedInfo.filename}</span>
              )}
              <span className="ml-3 text-xs text-gray-500">{lines} lines</span>
              {selectedInfo?.mtime_str && (
                <span className="ml-2 text-xs text-gray-400">Modified: {selectedInfo.mtime_str}</span>
              )}
            </div>
            <div className="flex gap-2">
              {isDirty && (
                <button
                  onClick={handleDiscard}
                  className="px-3 py-1 text-xs rounded bg-gray-200 dark:bg-gray-700 hover:bg-gray-300"
                >
                  Discard
                </button>
              )}
              <button
                onClick={handleReset}
                disabled={resetting}
                className="px-3 py-1 text-xs rounded bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 disabled:opacity-50"
              >
                {resetting ? "Resetting..." : "↩ Reset to Git"}
              </button>
              <button
                onClick={handleSave}
                disabled={saving || !isDirty}
                className="px-3 py-1 text-xs rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-40"
              >
                {saving ? "Saving..." : "💾 Save"}
              </button>
            </div>
          </div>

          {/* Role description */}
          {selectedInfo?.role && (
            <p className="mb-2 text-xs text-gray-500 dark:text-gray-400 px-1">
              <span className="font-medium">Role:</span> {selectedInfo.role}
            </p>
          )}

          {/* Editor */}
          <div className="flex-1 rounded-xl border border-gray-200 dark:border-gray-700 bg-gray-950 overflow-hidden">
            {loading ? (
              <div className="p-4 text-gray-500 text-sm">Loading...</div>
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
