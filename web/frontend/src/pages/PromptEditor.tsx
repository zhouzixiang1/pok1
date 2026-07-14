import { useEffect, useState, useCallback } from "react";
import { api } from "../api/client";
import type { PromptInfo } from "../api/types";
import PageMeta from "../components/common/PageMeta";
import { Skeleton } from "../components/shared/Skeleton";

const PROMPT_ORDER = [
  "orchestrator",
  "master",
  "master_plan_audit",
  "worker",
  "worker_profile_national_native",
  "worker_cot_check",
  "debug_worker",
  "reviewer",
  "critic",
  "crossover",
  "crossover_compatibility",
  "direction_auditor",
  "literature_probe",
  "combined_analyst",
  "degeneration_diagnosis",
  "cycle_archivist",
  "official_platform_analysis",
];

export default function PromptEditor() {
  const [prompts, setPrompts] = useState<PromptInfo[]>([]);
  const [selected, setSelected] = useState<string>("orchestrator");
  const [content, setContent] = useState("");
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const loadPromptList = useCallback(async () => {
    try {
      const data = await api.listPrompts();
      data.sort((a, b) => {
        const ai = PROMPT_ORDER.indexOf(a.name);
        const bi = PROMPT_ORDER.indexOf(b.name);
        return (ai < 0 ? PROMPT_ORDER.length : ai) - (bi < 0 ? PROMPT_ORDER.length : bi);
      });
      setPrompts(data);
    } catch (e) {
      setMessage(String(e));
    }
  }, []);

  const loadPromptContent = useCallback(async (name: string) => {
    setLoading(true);
    try {
      const text = await api.getPrompt(name);
      setContent(text);
    } catch (e) {
      setMessage(String(e));
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

  const handleSelect = (name: string) => {
    setSelected(name);
    setMessage(null);
  };

  const selectedInfo = prompts.find((p) => p.name === selected);
  const lines = content.split("\n").length;

  return (
    <>
      <PageMeta title="提示词契约 — Bot 自进化" description="只读查看 source-controlled LLM 提示词" />

      <div className="flex h-[calc(100vh-8rem)] gap-4 overflow-hidden">
        {/* Left sidebar — prompt list */}
        <div className="w-52 flex-shrink-0 rounded-xl border border-gray-200 dark:border-border-subtle bg-white dark:bg-surface-1 overflow-y-auto">
          <div className="px-4 py-3 border-b border-gray-100 dark:border-gray-700">
            <h2 className="text-sm font-semibold text-gray-700 dark:text-gray-300">提示词契约</h2>
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
          <div className="mb-2 rounded-lg border border-blue-200 bg-blue-50 px-4 py-2 text-xs text-blue-800 dark:border-blue-800 dark:bg-blue-950/25 dark:text-blue-300">
            只读权威视图。提示词是 evaluation contract 输入，修改必须在 operator checkout 通过 Git 审核、推送并按双 checkout 同步流程发布；网页不会热改运行中的提示词。
          </div>

          {/* Message */}
          {message && (
            <div className="mb-2 flex items-center justify-between rounded-lg bg-red-50 px-4 py-2 text-sm text-red-700 dark:bg-red-900/20 dark:text-red-300">
              <span>{message}</span>
              <button onClick={() => setMessage(null)} className="text-xs underline ml-2">关闭</button>
            </div>
          )}

          {/* Header bar */}
          <div className="mb-2 flex items-center justify-between px-4 py-2 rounded-xl border border-gray-200 dark:border-border-subtle bg-white dark:bg-surface-1">
            <div>
              <span className="font-semibold text-gray-800 dark:text-white">{selected}</span>
              {selectedInfo?.filename && (
                <span className="ml-2 text-xs text-gray-400">{selectedInfo.filename}</span>
              )}
              <span className="ml-3 text-xs text-gray-500">{lines} 行</span>
              {selectedInfo?.mtime_str && (
                <span className="ml-2 text-xs text-gray-400">最后修改: {selectedInfo.mtime_str}</span>
              )}
            </div>
            <span className="rounded bg-gray-100 px-2 py-1 text-[11px] font-medium text-gray-600 dark:bg-gray-800 dark:text-gray-300">source_control_only</span>
          </div>

          {/* Role description */}
          {selectedInfo?.role && (
            <p className="mb-2 text-xs text-gray-500 dark:text-gray-400 px-1">
              <span className="font-medium">角色:</span> {selectedInfo.role}
            </p>
          )}

          {/* Read-only source viewer */}
          <div className="flex-1 rounded-xl border border-gray-200 dark:border-gray-700 bg-gray-950 overflow-hidden">
            {loading ? (
              <div className="p-4 space-y-3">
                <Skeleton className="h-4 w-1/3" />
                <Skeleton className="h-4 w-2/3" />
                <Skeleton className="h-4 w-1/2" />
                <Skeleton className="h-4 w-3/4" />
              </div>
            ) : (
              <textarea
                value={content}
                readOnly
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
