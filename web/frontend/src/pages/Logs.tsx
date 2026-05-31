import { useEffect, useState, useCallback } from "react";
import { api } from "../api/client";
import type { OrchestratorLogFile } from "../api/types";
import PageMeta from "../components/common/PageMeta";
import { useGenerations } from "../context/DataProvider";
import { Skeleton } from "../components/shared/Skeleton";

type Tab = "generation" | "orchestrator" | "conversation";

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatFileTime(mtime: number): string {
  return new Date(mtime * 1000).toLocaleDateString();
}

// ── Inline SVG helpers ─────────────────────────────────────────────────────────
const FlagIcon = ({ className }: { className?: string }) => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className}><path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><line x1="4" y1="22" x2="4" y2="15"/></svg>
);
const ThoughtIcon = ({ className }: { className?: string }) => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className}><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
);
const GearIcon = ({ className }: { className?: string }) => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className}><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.67 15 1.65 1.65 0 0 0 3 13.5V13a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V21a2 2 0 1 1 4 0v-.09a1.65 1.65 0 0 0 .33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H21a2 2 0 1 1 0-4h-.09a1.65 1.65 0 0 0-1.51-1z"/></svg>
);

// ── LLM Conversation Parser ────────────────────────────────────────────────────

type ConvPart =
  | { kind: "prompt"; text: string }
  | { kind: "claude"; text: string }
  | { kind: "thinking"; text: string }
  | { kind: "tool"; name: string; output: string }
  | { kind: "cycle_end"; cost: string }
  | { kind: "separator" };

function parseConversation(raw: string): ConvPart[] {
  const lines = raw.split("\n");
  const parts: ConvPart[] = [];
  let i = 0;
  let currentTool: { name: string; lines: string[] } | null = null;

  const flushTool = () => {
    if (currentTool) {
      parts.push({ kind: "tool", name: currentTool.name, output: currentTool.lines.join("\n") });
      currentTool = null;
    }
  };

  while (i < lines.length) {
    const line = lines[i];

    if (line.startsWith("====") || line.includes("[ORCHESTRATOR CYCLE]")) {
      flushTool();
      parts.push({ kind: "separator" });
      i++;
      continue;
    }

    if (line.startsWith("[PROMPT]")) {
      flushTool();
      const promptLines: string[] = [];
      i++;
      while (i < lines.length && !lines[i].startsWith("[OUTPUT]") && !lines[i].startsWith("====")) {
        promptLines.push(lines[i]);
        i++;
      }
      parts.push({ kind: "prompt", text: promptLines.join("\n") });
      continue;
    }

    if (line.startsWith("[OUTPUT]") || line.startsWith("[PROMPT]")) {
      i++;
      continue;
    }

    if (line.startsWith("[CYCLE DONE]")) {
      flushTool();
      const cost = line.match(/cost=\$?([\d.]+)/)?.[1] || "";
      parts.push({ kind: "cycle_end", cost });
      i++;
      continue;
    }

    if (line.startsWith("[INTERRUPTED]") || line.startsWith("[ERROR]")) {
      flushTool();
      parts.push({ kind: "cycle_end", cost: line });
      i++;
      continue;
    }

    if (line.startsWith("[tool:") || line.includes("[tool:")) {
      flushTool();
      const nameMatch = line.match(/\[tool:\s*([^\]]+)\]/);
      const toolName = nameMatch ? nameMatch[1].trim() : "unknown";
      currentTool = { name: toolName, lines: [] };
      i++;
      continue;
    }

    if (line.startsWith("▸ ") || line.startsWith("▸")) {
      flushTool();
      const text = line.replace(/^▸\s?/, "");
      const last = parts[parts.length - 1];
      if (last && last.kind === "claude") {
        last.text += "\n" + text;
      } else {
        parts.push({ kind: "claude", text });
      }
      i++;
      continue;
    }

    if (line.startsWith("… ") || line.startsWith("…")) {
      flushTool();
      const text = line.replace(/^…\s?/, "");
      const last = parts[parts.length - 1];
      if (last && last.kind === "thinking") {
        last.text += "\n" + text;
      } else {
        parts.push({ kind: "thinking", text });
      }
      i++;
      continue;
    }

    if (currentTool) {
      currentTool.lines.push(line);
    }
    i++;
  }

  flushTool();
  return parts;
}

function ConvPartView({ part }: { part: ConvPart }) {
  const [expanded, setExpanded] = useState(false);

  if (part.kind === "separator") {
    return <div className="my-3 border-t-2 border-dashed border-gray-700 opacity-50" />;
  }

  if (part.kind === "cycle_end") {
    return (
      <div className="my-2 px-3 py-1.5 rounded bg-gray-800 text-xs text-gray-400 font-mono flex items-center gap-1">
        <FlagIcon /> {part.cost}
      </div>
    );
  }

  if (part.kind === "prompt") {
    return (
      <div className="my-1">
        <button
          onClick={() => setExpanded(!expanded)}
          className="text-xs text-gray-500 hover:text-gray-400 flex items-center gap-1"
        >
          <span>{expanded ? "▼" : "▶"}</span>
          <span>[提示词 — 点击展开]</span>
        </button>
        {expanded && (
          <pre className="mt-1 ml-4 text-[10px] text-gray-500 whitespace-pre-wrap font-mono max-h-64 overflow-y-auto">
            {part.text}
          </pre>
        )}
      </div>
    );
  }

  if (part.kind === "thinking") {
    return (
      <div className="my-1">
        <button
          onClick={() => setExpanded(!expanded)}
          className="text-xs text-yellow-400/60 hover:text-yellow-300 flex items-center gap-1 italic"
        >
          <ThoughtIcon className="text-yellow-400/60" />
          <span>思考中...</span>
          <span className="text-[10px]">{expanded ? "▲" : "▼"}</span>
        </button>
        {expanded && (
          <div className="mt-1 ml-4 text-[10px] text-yellow-300/50 italic font-mono whitespace-pre-wrap max-h-48 overflow-y-auto">
            {part.text}
          </div>
        )}
      </div>
    );
  }

  if (part.kind === "tool") {
    return (
      <div className="my-1 rounded border border-blue-900/40 bg-blue-950/20">
        <button
          onClick={() => setExpanded(!expanded)}
          className="w-full flex items-center justify-between px-3 py-1.5 text-left"
        >
          <span className="text-xs text-cyan-400 font-mono flex items-center gap-1"><GearIcon /> {part.name}</span>
          <span className="text-[10px] text-gray-500">{expanded ? "▲" : "▼"}</span>
        </button>
        {expanded && part.output && (
          <div className="border-t border-blue-900/30 px-3 py-2">
            <pre className="text-[10px] font-mono text-gray-400 whitespace-pre-wrap max-h-64 overflow-y-auto">
              {part.output}
            </pre>
          </div>
        )}
      </div>
    );
  }

  if (part.kind === "claude") {
    return (
      <div className="my-0.5 text-xs text-green-400 font-mono">
        {part.text.split("\n").map((line, i) => (
          <div key={i}>▸ {line}</div>
        ))}
      </div>
    );
  }

  return null;
}

// ── Main component ─────────────────────────────────────────────────────────────

export default function Logs() {
  const [tab, setTab] = useState<Tab>("generation");

  const generations = useGenerations();
  const [selectedVersion, setSelectedVersion] = useState<string>("");
  const [selectedFile, setSelectedFile] = useState<string>("");
  const [logContent, setLogContent] = useState<string>("");

  const [orchFiles, setOrchFiles] = useState<OrchestratorLogFile[]>([]);
  const [selectedOrch, setSelectedOrch] = useState<string>("");
  const [orchContent, setOrchContent] = useState<string>("");
  const [orchLoading, setOrchLoading] = useState(false);

  const [convFile, setConvFile] = useState<string>("");
  const [convParts, setConvParts] = useState<ConvPart[]>([]);
  const [convLoading, setConvLoading] = useState(false);

  useEffect(() => {
    if (generations.length > 0 && !selectedVersion) {
      setSelectedVersion(generations[generations.length - 1].version);
    }
  }, [generations, selectedVersion]);

  useEffect(() => {
    if (selectedVersion) {
      const gen = generations.find((g) => g.version === selectedVersion);
      if (gen && gen.files.length > 0 && !gen.files.includes(selectedFile)) {
        setSelectedFile(gen.files[0]);
      }
    }
  }, [selectedVersion, generations]);

  useEffect(() => {
    if (selectedVersion && selectedFile) {
      api.logContent(selectedVersion, selectedFile, 500)
        .then((res) => setLogContent(res.content))
        .catch(() => setLogContent("加载日志出错"));
    }
  }, [selectedVersion, selectedFile]);

  const loadOrchList = useCallback(async () => {
    try {
      const files = await api.orchestratorLogs();
      setOrchFiles(files);
      if (files.length > 0 && !selectedOrch) setSelectedOrch(files[0].filename);
    } catch {}
  }, [selectedOrch]);

  useEffect(() => {
    if (tab === "orchestrator" || tab === "conversation") {
      loadOrchList();
    }
  }, [tab, loadOrchList]);

  useEffect(() => {
    if (selectedOrch && tab === "orchestrator") {
      setOrchLoading(true);
      api.orchestratorLogContent(selectedOrch, 500)
        .then(setOrchContent)
        .finally(() => setOrchLoading(false));
    }
  }, [selectedOrch, tab]);

  useEffect(() => {
    if (!convFile && orchFiles.length > 0) setConvFile(orchFiles[0].filename);
  }, [orchFiles, convFile]);

  const loadConversation = useCallback(async (filename: string) => {
    setConvLoading(true);
    try {
      const raw = await api.orchestratorLogContent(filename);
      setConvParts(parseConversation(raw));
    } finally {
      setConvLoading(false);
    }
  }, []);

  useEffect(() => {
    if (convFile && tab === "conversation") {
      loadConversation(convFile);
    }
  }, [convFile, tab, loadConversation]);

  const currentGen = generations.find((g) => g.version === selectedVersion);

  return (
    <>
      <PageMeta title="日志 — Bot 自进化" description="迭代日志与编排器日志" />

      {/* Tab bar */}
      <div className="mb-4 flex gap-1">
        {(["generation", "orchestrator", "conversation"] as Tab[]).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-4 py-2 text-sm rounded-lg font-medium transition-colors ${
              tab === t
                ? "bg-blue-600 text-white"
                : "bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400 hover:bg-gray-200 dark:hover:bg-gray-700"
            }`}
          >
            {t === "generation" ? "迭代日志" : t === "orchestrator" ? "编排器日志" : "LLM 对话"}
          </button>
        ))}
      </div>

      {/* Generation Logs */}
      {tab === "generation" && (
        <div className="rounded-2xl border border-gray-200 bg-white dark:border-gray-800 dark:bg-white/[0.03]">
          <div className="px-5 py-4 border-b border-gray-100 dark:border-gray-800">
            <h3 className="text-lg font-semibold text-gray-800 dark:text-white">迭代日志</h3>
          </div>
          {generations.length === 0 ? (
            <div className="p-6"><Skeleton.Card count={2} /></div>
          ) : (
            <div className="flex">
              <div className="w-48 border-r border-gray-100 dark:border-gray-800 overflow-y-auto max-h-[600px]">
                {generations.map((gen) => (
                  <button
                    key={gen.version}
                    onClick={() => setSelectedVersion(gen.version)}
                    className={`w-full px-4 py-3 text-left text-sm hover:bg-gray-50 dark:hover:bg-gray-800/50 ${
                      selectedVersion === gen.version
                        ? "bg-blue-50 text-blue-700 font-medium dark:bg-blue-900/20 dark:text-blue-400"
                        : "text-gray-700 dark:text-gray-300"
                    }`}
                  >
                    {gen.version}
                    <span className="block text-xs text-gray-400">{gen.files.length} 个文件</span>
                  </button>
                ))}
              </div>
              <div className="flex-1">
                {currentGen && (
                  <div className="border-b border-gray-100 dark:border-gray-800 flex gap-1 px-3 py-2 overflow-x-auto">
                    {currentGen.files.map((file) => (
                      <button
                        key={file}
                        onClick={() => setSelectedFile(file)}
                        className={`px-3 py-1.5 rounded-lg text-xs whitespace-nowrap ${
                          selectedFile === file
                            ? "bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400"
                            : "text-gray-600 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-800/50"
                        }`}
                      >
                        {file}
                      </button>
                    ))}
                  </div>
                )}
                <div className="p-4">
                  <pre className="text-xs text-gray-700 dark:text-gray-300 overflow-auto max-h-[500px] whitespace-pre-wrap font-mono leading-relaxed bg-gray-50 dark:bg-gray-900 rounded-lg p-4">
                    {logContent || "选择一个文件以查看"}
                  </pre>
                </div>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Orchestrator Logs */}
      {tab === "orchestrator" && (
        <div className="rounded-2xl border border-gray-200 bg-white dark:border-gray-800 dark:bg-white/[0.03]">
          <div className="px-5 py-4 border-b border-gray-100 dark:border-gray-800 flex items-center justify-between">
            <h3 className="text-lg font-semibold text-gray-800 dark:text-white">编排器日志</h3>
            <select
              value={selectedOrch}
              onChange={(e) => setSelectedOrch(e.target.value)}
              className="text-sm border border-gray-200 dark:border-gray-700 dark:bg-gray-800 rounded px-2 py-1"
            >
              {orchFiles.map((f) => (
                <option key={f.filename} value={f.filename}>{f.filename} ({formatSize(f.size_bytes)}, {formatFileTime(f.mtime)})</option>
              ))}
            </select>
          </div>
          <div className="p-4">
            {orchLoading ? (
              <div className="space-y-2"><Skeleton.Line /><Skeleton.Line className="w-2/3" /><Skeleton.Line className="w-1/2" /></div>
            ) : (
              <pre className="text-xs text-gray-700 dark:text-gray-300 overflow-auto max-h-[600px] whitespace-pre-wrap font-mono leading-relaxed bg-gray-50 dark:bg-gray-900 rounded-lg p-4">
                {orchContent || "无日志内容"}
              </pre>
            )}
          </div>
        </div>
      )}

      {/* LLM Conversations */}
      {tab === "conversation" && (
        <div className="rounded-2xl border border-gray-200 bg-white dark:border-gray-800 dark:bg-white/[0.03] overflow-hidden">
          <div className="px-5 py-4 border-b border-gray-100 dark:border-gray-800 flex items-center justify-between">
            <h3 className="text-lg font-semibold text-gray-800 dark:text-white">LLM 对话</h3>
            <select
              value={convFile}
              onChange={(e) => setConvFile(e.target.value)}
              className="text-sm border border-gray-200 dark:border-gray-700 dark:bg-gray-800 rounded px-2 py-1"
            >
              {orchFiles.map((f) => (
                <option key={f.filename} value={f.filename}>{f.filename} ({formatSize(f.size_bytes)}, {formatFileTime(f.mtime)})</option>
              ))}
            </select>
          </div>
          <div className="p-4 bg-gray-950 overflow-y-auto max-h-[600px]">
            {convLoading ? (
              <div className="text-gray-400 text-sm">解析对话中...</div>
            ) : convParts.length === 0 ? (
              <div className="text-gray-500 text-sm">在上面选择一个日志文件</div>
            ) : (
              <div className="space-y-0.5">
                {convParts.map((part, i) => (
                  <ConvPartView key={i} part={part} />
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </>
  );
}
