import { useState } from "react";
import { GearIcon } from "./icons";

export type MsgType = "claude" | "thinking" | "tool_call" | "error" | "raw";

export interface ConvMsg {
  id: number;
  type: MsgType;
  text: string;
  toolName?: string;
  toolArgs?: Record<string, unknown>;
  toolOutput: string[];
  toolDone: boolean;
}

export function ToolCard({ msg }: { msg: ConvMsg }) {
  const [expanded, setExpanded] = useState(false);
  const [argsExpanded, setArgsExpanded] = useState(false);

  return (
    <div className="my-1 rounded-lg border border-cyan-800/40 bg-cyan-950/20 overflow-hidden">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center justify-between px-3 py-2 text-left hover:bg-cyan-950/40 transition-colors"
      >
        <span className="flex items-center gap-2">
          <GearIcon className={`text-cyan-400 ${!msg.toolDone ? "animate-spin" : ""}`} />
          <span className="text-cyan-300 text-xs font-mono font-medium">{msg.toolName}</span>
        </span>
        <span className="flex items-center gap-1.5 text-xs text-gray-500">
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
          {msg.toolOutput.length > 0 && (
            <div>
              <div className="text-[10px] text-gray-500 mb-1">输出</div>
              <div className="text-[10px] font-mono text-gray-400 whitespace-pre-wrap max-h-48 overflow-y-auto">
                {msg.toolOutput.join("\n")}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function ThinkingBlock({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="my-1 border-l-2 border-amber-400/30 pl-2">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1.5 text-xs text-amber-400/70 hover:text-amber-300 transition-colors"
      >
        <span className="italic">💭 思考中...</span>
        <span className="text-[10px]">{expanded ? "▲" : "▼"}</span>
      </button>
      {expanded && (
        <div className="mt-1 text-xs text-amber-300/50 italic whitespace-pre-wrap font-mono leading-relaxed max-h-48 overflow-y-auto">
          {text}
        </div>
      )}
    </div>
  );
}
