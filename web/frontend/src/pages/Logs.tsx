import { useEffect, useState, useCallback } from "react";
import { api } from "../api/client";
import type { GenerationLog, OrchestratorLogFile } from "../api/types";
import PageMeta from "../components/common/PageMeta";

type Tab = "generation" | "orchestrator" | "conversation";

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
      // Merge adjacent claude lines
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

    // Append to current tool output or skip
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
      <div className="my-2 px-3 py-1.5 rounded bg-gray-800 text-xs text-gray-400 font-mono">
        🏁 {part.cost}
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
          <span>[PROMPT — click to expand]</span>
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
          <span>💭 Thinking...</span>
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
          <span className="text-xs text-cyan-400 font-mono">⚙ {part.name}</span>
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

  // Generation logs
  const [generations, setGenerations] = useState<GenerationLog[]>([]);
  const [selectedVersion, setSelectedVersion] = useState<string>("");
  const [selectedFile, setSelectedFile] = useState<string>("");
  const [logContent, setLogContent] = useState<string>("");
  const [genLoading, setGenLoading] = useState(true);

  // Orchestrator logs
  const [orchFiles, setOrchFiles] = useState<OrchestratorLogFile[]>([]);
  const [selectedOrch, setSelectedOrch] = useState<string>("");
  const [orchContent, setOrchContent] = useState<string>("");
  const [orchLoading, setOrchLoading] = useState(false);

  // Conversation
  const [convFile, setConvFile] = useState<string>("");
  const [convParts, setConvParts] = useState<ConvPart[]>([]);
  const [convLoading, setConvLoading] = useState(false);

  // ── Generation logs ──
  useEffect(() => {
    api.generations()
      .then((gens) => {
        setGenerations(gens);
        if (gens.length > 0) setSelectedVersion(gens[gens.length - 1].version);
      })
      .finally(() => setGenLoading(false));
  }, []);

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
        .catch(() => setLogContent("Error loading log"));
    }
  }, [selectedVersion, selectedFile]);

  // ── Orchestrator logs ──
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

  // ── Conversation tab ──
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
      <PageMeta title="Logs — Evolution Dashboard" description="Generation and orchestrator logs" />

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
            {t === "generation" ? "Generation Logs" : t === "orchestrator" ? "Orchestrator Logs" : "LLM Conversations"}
          </button>
        ))}
      </div>

      {/* Generation Logs */}
      {tab === "generation" && (
        <div className="rounded-2xl border border-gray-200 bg-white dark:border-gray-800 dark:bg-white/[0.03]">
          <div className="px-5 py-4 border-b border-gray-100 dark:border-gray-800">
            <h3 className="text-lg font-semibold text-gray-800 dark:text-white">Generation Logs</h3>
          </div>
          {genLoading ? (
            <div className="p-6 text-gray-500">Loading...</div>
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
                    <span className="block text-xs text-gray-400">{gen.files.length} files</span>
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
                    {logContent || "Select a file to view"}
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
            <h3 className="text-lg font-semibold text-gray-800 dark:text-white">Orchestrator Logs</h3>
            <select
              value={selectedOrch}
              onChange={(e) => setSelectedOrch(e.target.value)}
              className="text-sm border border-gray-200 dark:border-gray-700 dark:bg-gray-800 rounded px-2 py-1"
            >
              {orchFiles.map((f) => (
                <option key={f.filename} value={f.filename}>{f.filename}</option>
              ))}
            </select>
          </div>
          <div className="p-4">
            {orchLoading ? (
              <div className="text-gray-400 text-sm">Loading...</div>
            ) : (
              <pre className="text-xs text-gray-700 dark:text-gray-300 overflow-auto max-h-[600px] whitespace-pre-wrap font-mono leading-relaxed bg-gray-50 dark:bg-gray-900 rounded-lg p-4">
                {orchContent || "No log content"}
              </pre>
            )}
          </div>
        </div>
      )}

      {/* LLM Conversations */}
      {tab === "conversation" && (
        <div className="rounded-2xl border border-gray-200 bg-white dark:border-gray-800 dark:bg-white/[0.03] overflow-hidden">
          <div className="px-5 py-4 border-b border-gray-100 dark:border-gray-800 flex items-center justify-between">
            <h3 className="text-lg font-semibold text-gray-800 dark:text-white">LLM Conversations</h3>
            <select
              value={convFile}
              onChange={(e) => setConvFile(e.target.value)}
              className="text-sm border border-gray-200 dark:border-gray-700 dark:bg-gray-800 rounded px-2 py-1"
            >
              {orchFiles.map((f) => (
                <option key={f.filename} value={f.filename}>{f.filename}</option>
              ))}
            </select>
          </div>
          <div className="p-4 bg-gray-950 overflow-y-auto max-h-[600px]">
            {convLoading ? (
              <div className="text-gray-400 text-sm">Parsing conversation...</div>
            ) : convParts.length === 0 ? (
              <div className="text-gray-500 text-sm">Select a log file above</div>
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
