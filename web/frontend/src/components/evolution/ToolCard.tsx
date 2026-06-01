import { useEffect, useState } from "react";
import { GearIcon } from "./icons";

export type MsgType = "claude" | "thinking" | "tool_call" | "error" | "raw";

export interface ConvMsg {
  id: number;
  type: MsgType;
  text: string;
  role?: string;
  toolName?: string;
  toolArgs?: Record<string, unknown>;
  toolOutput: string[];
  toolDone: boolean;
}

function formatToolSummary(toolName: string, args?: Record<string, unknown>): string {
  if (!args) return toolName;
  switch (toolName) {
    case "Bash":
      return `$ ${String(args.command ?? "").slice(0, 120)}`;
    case "Read":
      return String(args.file_path ?? toolName);
    case "Edit":
    case "MultiEdit":
      return `✏ ${args.file_path ?? toolName}`;
    case "Write":
      return `📝 ${args.file_path ?? toolName}`;
    case "Glob":
      return `🔍 ${args.pattern ?? toolName}`;
    case "Grep":
      return `🔍 ${args.pattern ?? ""}${args.path ? ` in ${args.path}` : ""}`;
    default:
      return toolName;
  }
}

export function ToolCard({ msg }: { msg: ConvMsg }) {
  const [expanded, setExpanded] = useState(false);
  const [argsExpanded, setArgsExpanded] = useState(false);

  const summary = formatToolSummary(msg.toolName ?? "", msg.toolArgs);

  return (
    <div className="my-1 rounded-lg border border-cyan-800/40 bg-cyan-950/20 overflow-hidden">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center justify-between px-3 py-2 text-left hover:bg-cyan-950/40 transition-colors"
      >
        <span className="flex items-center gap-2 min-w-0">
          <GearIcon className={`text-cyan-400 shrink-0 ${!msg.toolDone ? "animate-spin" : ""}`} />
          <span className="text-cyan-300 text-xs font-mono font-medium truncate">{summary}</span>
        </span>
        <span className="flex items-center gap-1.5 text-xs text-gray-500 shrink-0 ml-2">
          {!msg.toolDone && (
            <span className="flex gap-0.5">
              <span className="w-1 h-1 rounded-full bg-cyan-400 animate-bounce" style={{ animationDelay: "0ms" }} />
              <span className="w-1 h-1 rounded-full bg-cyan-400 animate-bounce" style={{ animationDelay: "150ms" }} />
              <span className="w-1 h-1 rounded-full bg-cyan-400 animate-bounce" style={{ animationDelay: "300ms" }} />
            </span>
          )}
          {msg.toolDone ? "完成" : "运行中"}
          {expanded ? " ▲" : " ▼"}
        </span>
      </button>

      {expanded && (
        <div className="border-t border-cyan-800/30 px-3 py-2 space-y-2">
          {msg.toolArgs && Object.keys(msg.toolArgs).length > 0 && (
            <div>
              {msg.toolName === "Bash" ? (
                <div>
                  <div className="text-[10px] text-gray-400 mb-1">命令</div>
                  <pre className="text-[10px] font-mono text-green-300 whitespace-pre-wrap bg-black/30 rounded p-2">
                    $ {msg.toolArgs.command as string}
                  </pre>
                </div>
              ) : (
                <div>
                  <button
                    onClick={() => setArgsExpanded(!argsExpanded)}
                    className="text-[10px] text-gray-400 hover:text-gray-300 flex items-center gap-1"
                  >
                    <span>{argsExpanded ? "▼" : "▶"}</span> 参数
                  </button>
                  {argsExpanded && (
                    <pre className="mt-1 text-[10px] font-mono text-gray-300 whitespace-pre-wrap">
                      {JSON.stringify(msg.toolArgs, null, 2)}
                    </pre>
                  )}
                </div>
              )}
            </div>
          )}
          {msg.toolOutput.length > 0 && (
            <div>
              <div className="text-[10px] text-gray-500 mb-1">输出</div>
              <div className="text-[10px] font-mono text-gray-400 whitespace-pre-wrap max-h-[400px] overflow-y-auto">
                {msg.toolOutput.join("\n")}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function ThinkingBlock({ text, done }: { text: string; done: boolean }) {
  const [expanded, setExpanded] = useState(true);

  useEffect(() => {
    if (done) setExpanded(false);
  }, [done]);

  const charCount = text.length;
  const label = done
    ? `💭 思考完成 (${charCount > 999 ? `${(charCount / 1000).toFixed(1)}k` : charCount} 字)`
    : "💭 思考中...";

  return (
    <div className="my-1 border-l-2 border-amber-400/30 pl-2">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1.5 text-xs text-amber-400/70 hover:text-amber-300 transition-colors"
      >
        <span className={done ? "" : "italic"}>{label}</span>
        <span className="text-[10px]">{expanded ? "▲" : "▼"}</span>
      </button>
      {expanded && text && (
        <div className="mt-1 text-xs text-amber-300/50 italic whitespace-pre-wrap font-mono leading-relaxed max-h-[400px] overflow-y-auto">
          {text}
        </div>
      )}
    </div>
  );
}
