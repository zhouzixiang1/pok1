import { useEffect, useState, useCallback } from "react";
import { api } from "../api/client";
import type { BotSummary, BotDetail } from "../api/types";
import PageMeta from "../components/common/PageMeta";
import { controlApi } from "../api/control";
import { useBots } from "../context/DataProvider";

// ── Inline SVG helpers ─────────────────────────────────────────────────────────
const TombIcon = ({ className }: { className?: string }) => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className}><path d="M6 22V10a6 6 0 0 1 12 0v12"/><path d="M4 22h16"/></svg>
);
const DocIcon = ({ className }: { className?: string }) => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className={className}><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
);
const CheckIcon = ({ className }: { className?: string }) => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" className={className}><polyline points="20 6 9 17 4 12"/></svg>
);

function RatingBadge({ r, rd, winRate, games }: { r: number; rd: number; winRate?: number; games?: number }) {
  const conf = rd < 50 ? "text-green-600" : rd < 100 ? "text-yellow-600" : "text-orange-500";
  return (
    <span className="text-sm">
      <span className="font-mono font-semibold">{r.toFixed(0)}</span>
      <span className={`ml-1 text-xs ${conf}`}>±{(2 * rd).toFixed(0)}</span>
      {winRate != null && <span className="ml-2 text-xs text-gray-500">WR {(winRate * 100).toFixed(0)}%{games ? ` (${games})` : ""}</span>}
    </span>
  );
}

function BotCard({ bot, onAction }: { bot: BotSummary; onAction: (msg: string) => void }) {
  const [expanded, setExpanded] = useState(false);
  const [detail, setDetail] = useState<BotDetail | null>(null);
  const [selectedFile, setSelectedFile] = useState("");
  const [code, setCode] = useState("");
  const [loading, setLoading] = useState(false);
  const [toolLoading, setToolLoading] = useState<string | null>(null);

  const loadDetail = useCallback(async () => {
    if (detail) return;
    try {
      const d = await api.botDetail(bot.version);
      setDetail(d);
      if (d.files.length > 0) setSelectedFile(d.files[0]);
    } catch {}
  }, [bot.version, detail]);

  const loadCode = useCallback(async (filename: string) => {
    setLoading(true);
    try {
      const text = await api.botCode(bot.version, filename);
      setCode(text);
    } catch {
      setCode("加载代码失败。");
    } finally {
      setLoading(false);
    }
  }, [bot.version]);

  useEffect(() => {
    if (expanded) {
      loadDetail();
    }
  }, [expanded, loadDetail]);

  useEffect(() => {
    if (selectedFile && expanded) {
      loadCode(selectedFile);
    }
  }, [selectedFile, expanded, loadCode]);

  const handleInlineEval = async () => {
    setToolLoading("eval");
    try {
      const r = await controlApi.callTool("run_inline_eval", { version: bot.version, n_games: 5 });
      onAction(r.result || r.error || "完成");
    } finally {
      setToolLoading(null);
    }
  };

  const displayName = bot.name.replace("claude_", "v");
  const conserv = bot.rating ? bot.rating.conservative.toFixed(0) : "—";

  return (
    <div className={`rounded-xl border ${bot.graveyard ? "border-gray-300 opacity-60" : "border-gray-200 dark:border-gray-700"} bg-white dark:bg-gray-800 overflow-hidden`}>
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center justify-between px-4 py-3 hover:bg-gray-50 dark:hover:bg-gray-700/50 text-left"
      >
        <div className="flex items-center gap-3">
          <span className={`text-base font-semibold ${bot.graveyard ? "text-gray-400" : "text-gray-800 dark:text-white"}`}>
            {displayName}
          </span>
          {bot.completed
            ? <span className="px-1.5 py-0.5 text-[10px] rounded bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400 flex items-center gap-0.5"><CheckIcon /> 完成</span>
            : <span className="px-1.5 py-0.5 text-[10px] rounded bg-yellow-100 text-yellow-700 dark:bg-yellow-900/30 dark:text-yellow-400">进行中</span>
          }
          {bot.graveyard && <span className="px-1.5 py-0.5 text-[10px] rounded bg-gray-100 text-gray-400">已归档</span>}
        </div>
        <div className="flex items-center gap-4 text-sm text-gray-500">
          {bot.rating && <RatingBadge r={bot.rating.r} rd={bot.rating.rd} winRate={bot.win_rate} games={bot.games} />}
          <span className="text-xs text-gray-400">{bot.total_lines} 行</span>
          <span className="text-xs text-gray-400">保守 {conserv}</span>
          <span className="text-gray-400">{expanded ? "▲" : "▼"}</span>
        </div>
      </button>

      {expanded && (
        <div className="border-t border-gray-100 dark:border-gray-700 p-4 space-y-4">
          {detail ? (
            <>
              {detail.parent && (
                <p className="text-xs text-gray-500">父代: <span className="font-mono">{detail.parent}</span></p>
              )}

              {/* File picker + code viewer */}
              <div>
                <div className="flex gap-1 mb-2 flex-wrap">
                  {detail.files.map((f) => (
                    <button
                      key={f}
                      onClick={() => setSelectedFile(f)}
                      className={`px-2 py-1 text-xs rounded ${selectedFile === f ? "bg-blue-600 text-white" : "bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600"}`}
                    >
                      {f}
                    </button>
                  ))}
                </div>
                {loading
                  ? <div className="text-xs text-gray-400 p-3">加载中...</div>
                  : (
                    <pre className="text-[11px] font-mono bg-gray-950 text-gray-200 rounded p-3 overflow-auto max-h-80 leading-relaxed whitespace-pre">
                      {code || "无内容"}
                    </pre>
                  )
                }
              </div>

              {/* Actions */}
              {!bot.graveyard && (
                <div className="flex gap-2 flex-wrap">
                  <button
                    onClick={handleInlineEval}
                    disabled={toolLoading === "eval"}
                    className="px-3 py-1.5 text-xs rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50 flex items-center gap-1"
                  >
                    <PlayIcon /> {toolLoading === "eval" ? "运行中..." : "快速评估 (5 局)"}
                  </button>
                </div>
              )}
            </>
          ) : (
            <div className="text-xs text-gray-400">加载详情中...</div>
          )}
        </div>
      )}
    </div>
  );
}

// Inline PlayIcon for BotCard
const PlayIcon = ({ className }: { className?: string }) => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" className={className}><polygon points="5 3 19 12 5 21 5 3"/></svg>
);

export default function BotManager() {
  const { active: rawBots, graveyard: rawGraveyard } = useBots();
  const bots = rawBots.sort((a, b) => b.version - a.version);
  const graveyard = rawGraveyard.sort((a, b) => b.version - a.version);
  const [showGraveyard, setShowGraveyard] = useState(false);
  const [message, setMessage] = useState("");
  const [prepForm, setPrepForm] = useState({ source_v: "", next_v: "" });
  const [reapLoading, setReapLoading] = useState(false);
  const [prepLoading, setPrepLoading] = useState(false);

  const refresh = useCallback(async () => {
    try {
      await api.listBots(true);
    } catch {}
  }, []);

  const handleReapWeakest = async () => {
    setReapLoading(true);
    try {
      const r = await controlApi.callTool("reap_weakest", {});
      setMessage(r.result || r.error || "完成");
      await refresh();
    } finally {
      setReapLoading(false);
    }
  };

  const handlePrepare = async () => {
    const sv = parseInt(prepForm.source_v);
    const nv = parseInt(prepForm.next_v);
    if (!sv || !nv) return;
    setPrepLoading(true);
    try {
      const r = await controlApi.callTool("prepare_next_gen", { source_v: sv, next_v: nv });
      setMessage(r.result || r.error || "完成");
      await refresh();
    } finally {
      setPrepLoading(false);
    }
  };

  if (bots.length === 0 && graveyard.length === 0) return <div className="p-6 text-gray-500">加载中...</div>;

  return (
    <>
      <PageMeta title="Bot 管理 — Bot 自进化" description="管理所有 Bot 版本" />

      <div className="flex items-center justify-between mb-4">
        <h1 className="text-2xl font-bold text-gray-800 dark:text-white">Bot 管理</h1>
        <div className="flex gap-2">
          <button onClick={refresh} className="px-3 py-1.5 text-sm rounded bg-gray-200 dark:bg-gray-700 hover:bg-gray-300 dark:hover:bg-gray-600">
            刷新
          </button>
        </div>
      </div>

      {message && (
        <div className="mb-4 px-4 py-3 rounded-lg bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 text-sm text-blue-700 dark:text-blue-300 font-mono whitespace-pre-wrap max-h-40 overflow-y-auto">
          {message}
          <button onClick={() => setMessage("")} className="ml-2 text-xs underline">清空</button>
        </div>
      )}

      {/* Global actions */}
      <div className="mb-4 rounded-xl border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 p-4 flex flex-wrap gap-4 items-end">
        <div>
          <button
            onClick={handleReapWeakest}
            disabled={reapLoading}
            className="px-4 py-2 text-sm rounded bg-red-600 text-white hover:bg-red-700 disabled:opacity-50 flex items-center gap-1"
          >
            <TombIcon /> {reapLoading ? "淘汰中..." : "淘汰最弱 Bot"}
          </button>
          <p className="text-xs text-gray-400 mt-1">当 Bot 池超过 30 个时，淘汰保守评分最低的</p>
        </div>

        <div className="flex items-end gap-2">
          <div>
            <label className="text-xs text-gray-500 block mb-1">源版本</label>
            <input
              type="number"
              value={prepForm.source_v}
              onChange={(e) => setPrepForm((p) => ({ ...p, source_v: e.target.value }))}
              className="w-20 px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 dark:bg-gray-700 rounded"
              placeholder="22"
            />
          </div>
          <div>
            <label className="text-xs text-gray-500 block mb-1">下一版本</label>
            <input
              type="number"
              value={prepForm.next_v}
              onChange={(e) => setPrepForm((p) => ({ ...p, next_v: e.target.value }))}
              className="w-20 px-2 py-1.5 text-sm border border-gray-300 dark:border-gray-600 dark:bg-gray-700 rounded"
              placeholder="23"
            />
          </div>
          <button
            onClick={handlePrepare}
            disabled={prepLoading || !prepForm.source_v || !prepForm.next_v}
            className="px-4 py-2 text-sm rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50 flex items-center gap-1"
          >
            <DocIcon /> {prepLoading ? "准备中..." : "准备下一代"}
          </button>
        </div>
      </div>

      {/* Active bots */}
      <div className="space-y-2 mb-6">
        <h2 className="text-sm font-semibold text-gray-600 dark:text-gray-400 uppercase tracking-wide">
          活跃 Bot ({bots.length})
        </h2>
        {bots.map((bot) => (
          <BotCard key={bot.name} bot={bot} onAction={setMessage} />
        ))}
        {bots.length === 0 && <p className="text-sm text-gray-400">暂无活跃 Bot。</p>}
      </div>

      {/* Graveyard */}
      <div>
        <button
          onClick={() => setShowGraveyard(!showGraveyard)}
          className="text-sm font-semibold text-gray-600 dark:text-gray-400 uppercase tracking-wide flex items-center gap-1 mb-2"
        >
          <span>{showGraveyard ? "▼" : "▶"}</span>
          已归档 ({graveyard.length})
        </button>
        {showGraveyard && (
          <div className="space-y-2">
            {graveyard.map((bot) => (
              <BotCard key={bot.name} bot={bot} onAction={setMessage} />
            ))}
            {graveyard.length === 0 && <p className="text-sm text-gray-400">无已归档 Bot。</p>}
          </div>
        )}
      </div>
    </>
  );
}
