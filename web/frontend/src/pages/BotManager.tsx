import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import type { BotDetail, BotSummary, H2HEntry, OfficialCertification } from "../api/types";
import PageMeta from "../components/common/PageMeta";
import { EpochAuthorityStatus } from "../components/evolution/EpochAuthorityStatus";
import { EmptyState } from "../components/shared/EmptyState";
import { Skeleton } from "../components/shared/Skeleton";
import { useBots, useH2H, useUpdateData } from "../context/DataProvider";
import { useControlStatus } from "../hooks/useControlStatus";

const CheckIcon = () => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><polyline points="20 6 9 17 4 12" /></svg>
);

const DownloadIcon = () => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" /></svg>
);

const compactBotName = (name: string) => name.replace(/^national_/, "");

interface CertificationView {
  formal: boolean;
  label: string;
  detail: string;
  tone: string;
}

const isDigest = (value: unknown): value is string => (
  typeof value === "string" && /^[0-9a-f]{64}$/.test(value)
);

function certificationView(certification?: OfficialCertification): CertificationView {
  if (!certification) {
    return {
      formal: false,
      label: "未认证",
      detail: "没有 signed official-full-v5 证书。",
      tone: "border-gray-200 bg-gray-50 text-gray-600 dark:border-gray-700 dark:bg-gray-800/40 dark:text-gray-300",
    };
  }

  // The signed certificate, deterministic receipt, candidate content, and
  // verdict ledger are validated server-side.  Browser code must not create a
  // second, inevitably weaker certification oracle from raw JSON fields.
  const ledgerEntry = certification.official_verdict_ledger_entry;
  const formal = certification.formal_certified === true
    && certification.formal_authority === "signed_full_v5"
    && certification.mode === "full"
    && certification.policy_id === "official-full-v5"
    && isDigest(certification.certificate_digest)
    && isDigest(certification.certificate_signature_sha256)
    && isDigest(certification.published_attestation_digest)
    && isDigest(ledgerEntry?.entry_digest)
    && ledgerEntry?.certificate_digest === certification.certificate_digest
    && ledgerEntry?.policy_id === certification.policy_id
    && ledgerEntry?.outcome === "official-certified";

  if (formal) {
    const firstStrictControl = certification.certification_profile === "first_strict_control_v1"
      && certification.opponent_authority === "system_control"
      && certification.strength_evidence_weight === 0
      && certification.strategy_evidence_weight === 0;
    const normalFull = certification.certification_profile === "official-full-v5"
      && certification.opponent_authority === "strict_published_pool"
      && certification.formal_summary?.self_play_rounds === 5
      && certification.formal_summary.opponent_rounds === 3
      && certification.formal_summary.target_hands === 70
      && certification.strength_evidence_weight === 0
      && certification.strategy_evidence_weight === 0;
    if (firstStrictControl || normalFull) {
      return {
        formal: true,
        label: firstStrictControl ? "首代系统控制证书通过" : "正式认证通过",
        detail: firstStrictControl
          ? "first_strict_control_v1：system-control 仅证明官方协议合规；强度与策略证据权重均为 0。"
          : "signed official-full-v5：5 轮自对弈 + 3 轮合格 strict 对手，每轮 70 手。",
        tone: "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-800 dark:bg-emerald-900/20 dark:text-emerald-300",
      };
    }
    return {
      formal: false,
      label: "正式证书身份投影不完整",
      detail: "formal_certified 存在，但 profile、对手权威、5/3/70 或零权重字段不匹配；不显示为正式通过，也不猜测为普通 5+3。",
      tone: "border-red-200 bg-red-50 text-red-700 dark:border-red-800 dark:bg-red-900/20 dark:text-red-300",
    };
  }

  if (certification.status === "official-smoke-pass") {
    return {
      formal: false,
      label: "Smoke 诊断通过（非认证）",
      detail: "Smoke 只验证短程官方平台诊断，不能发布 Bot。",
      tone: "border-cyan-200 bg-cyan-50 text-cyan-700 dark:border-cyan-800 dark:bg-cyan-900/20 dark:text-cyan-300",
    };
  }
  if (certification.status === "official-compliance-pass") {
    return {
      formal: false,
      label: "Compliance 诊断通过（非认证）",
      detail: "短程 compliance 不是 signed 5+3×70 正式证书。",
      tone: "border-cyan-200 bg-cyan-50 text-cyan-700 dark:border-cyan-800 dark:bg-cyan-900/20 dark:text-cyan-300",
    };
  }
  if (certification.status === "local-pass") {
    return {
      formal: false,
      label: "本地诊断通过（非认证）",
      detail: "本地 raw TCP 强度/合规门不能替代官方 Windows EXE。",
      tone: "border-gray-200 bg-gray-50 text-gray-600 dark:border-gray-700 dark:bg-gray-800/40 dark:text-gray-300",
    };
  }
  if (certification.status === "official-pending") {
    const full = certification.mode === "full";
    return {
      formal: false,
      label: full ? "Full 正式认证进行中" : "诊断任务进行中（非认证）",
      detail: full ? "尚未形成签名证书，当前不能显示为通过。" : "该任务不会形成正式发布资格。",
      tone: "border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-800 dark:bg-amber-900/20 dark:text-amber-300",
    };
  }
  if (certification.status === "official-certified") {
    return {
      formal: false,
      label: "正式权威未验证",
      detail: "记录声称 certified，但后端未验证为当前发布物的 signed full-v5；按非认证处理。",
      tone: "border-red-200 bg-red-50 text-red-700 dark:border-red-800 dark:bg-red-900/20 dark:text-red-300",
    };
  }

  const labels: Partial<Record<OfficialCertification["status"], string>> = {
    "official-failed": "正式认证失败",
    "official-inconclusive": "正式认证无结论",
    "official-unavailable": "认证状态不可用",
    "official-uncertified": "未认证",
  };
  return {
    formal: false,
    label: labels[certification.status] ?? "未认证",
    detail: certification.reason || "没有可验证的 signed official-full-v5 证书。",
    tone: certification.status === "official-failed"
      ? "border-red-200 bg-red-50 text-red-700 dark:border-red-800 dark:bg-red-900/20 dark:text-red-300"
      : "border-orange-200 bg-orange-50 text-orange-700 dark:border-orange-800 dark:bg-orange-900/20 dark:text-orange-300",
  };
}

function RatingLine({ bot }: { bot: BotSummary }) {
  const score = bot.selection_score ?? bot.leaderboard_score;
  if (bot.strength_evidence_available === false) {
    return (
      <div className="text-xs text-amber-600 dark:text-amber-400">
        已严格发布；等待评分守护进程生成首个同发布池 evaluation cycle。
      </div>
    );
  }
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-500 dark:text-gray-400">
      <span>选择分 <span className="font-mono font-semibold text-gray-800 dark:text-gray-200">{score != null ? score.toFixed(4) : "—"}</span></span>
      <span>H2H {bot.h2h_avg_wr != null ? `${(bot.h2h_avg_wr * 100).toFixed(1)}%` : "—"}</span>
      <span>70 手样本 {bot.strength_sample_count ?? bot.games ?? 0}</span>
      {bot.secondary_net_chips_mean != null && <span>净筹码/70手 {bot.secondary_net_chips_mean >= 0 ? "+" : ""}{bot.secondary_net_chips_mean.toFixed(0)}</span>}
      <span>{bot.total_lines} 行</span>
    </div>
  );
}
function BotCard({
  bot,
  displayOrdinal,
  h2hData,
  onMessage,
}: {
  bot: BotSummary;
  displayOrdinal: number | null;
  h2hData: Record<string, H2HEntry>;
  onMessage: (message: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [detail, setDetail] = useState<BotDetail | null>(null);
  const [selectedFile, setSelectedFile] = useState("");
  const [code, setCode] = useState("");
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [loadingCode, setLoadingCode] = useState(false);
  const [downloading, setDownloading] = useState(false);

  const loadDetail = useCallback(async () => {
    if (detail || loadingDetail) return;
    setLoadingDetail(true);
    try {
      const next = await api.botDetail(bot.version);
      setDetail(next);
      if (next.files.length > 0) setSelectedFile(next.files[0]);
    } catch (error) {
      onMessage(`详情加载失败：${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setLoadingDetail(false);
    }
  }, [bot.version, detail, loadingDetail, onMessage]);

  useEffect(() => {
    if (expanded) void loadDetail();
  }, [expanded, loadDetail]);

  useEffect(() => {
    if (!expanded || !selectedFile) return;
    setLoadingCode(true);
    api.botCode(bot.version, selectedFile)
      .then(setCode)
      .catch((error) => setCode(`加载代码失败：${error instanceof Error ? error.message : String(error)}`))
      .finally(() => setLoadingCode(false));
  }, [bot.version, expanded, selectedFile]);

  const certification = detail?.official_certification ?? bot.official_certification;
  // This is presentation metadata only.  The backend has already admitted
  // this row through the strict published inventory; the real completion tag
  // remains the durable identity and the display ordinal never enters rating,
  // evidence, checkpoint, or publication state.
  const completionTag = bot.active === true
    && bot.tagged === true
    && Number.isSafeInteger(bot.version)
    && bot.version > 0
    ? `national-bot-v${bot.version}`
    : null;
  const certView = certificationView(certification);
  const formalSummary = certification?.formal_summary;
  const ledgerEntry = certification?.official_verdict_ledger_entry;
  const ledgerIdentity = ledgerEntry && (
    ledgerEntry.entry_digest
    ?? ledgerEntry.ledger_entry_digest
    ?? ledgerEntry.digest
    ?? ledgerEntry.sequence
  );
  const opponents = useMemo(() => {
    const rows: Array<{ name: string; wins: number; losses: number; draws: number; games: number; wr: number }> = [];
    for (const [key, value] of Object.entries(h2hData)) {
      const parts = key.split(" vs ");
      if (parts.length !== 2 || !parts.includes(bot.name) || value.games <= 0) continue;
      const isA = parts[0] === bot.name;
      const wins = isA ? value.a_wins : value.b_wins;
      const losses = isA ? value.b_wins : value.a_wins;
      rows.push({
        name: isA ? parts[1] : parts[0],
        wins,
        losses,
        draws: value.draws,
        games: value.games,
        wr: (wins + 0.5 * value.draws) / value.games,
      });
    }
    return rows.sort((a, b) => b.wr - a.wr);
  }, [bot.name, h2hData]);

  const handleDownload = async () => {
    setDownloading(true);
    try {
      await api.downloadBot(bot.version);
      onMessage(`已下载 ${bot.name}.zip`);
    } catch (error) {
      onMessage(`下载失败：${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setDownloading(false);
    }
  };

  return (
    <article className="overflow-hidden rounded-xl border border-gray-200 bg-white dark:border-border-subtle dark:bg-surface-1">
      <button
        onClick={() => setExpanded((value) => !value)}
        className="flex w-full items-center justify-between gap-4 px-4 py-3 text-left hover:bg-gray-50 dark:hover:bg-gray-800/40"
      >
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            {displayOrdinal != null ? (
              <span className="font-semibold text-gray-900 dark:text-white">Bot {displayOrdinal}</span>
            ) : (
              <span className="font-semibold text-red-700 dark:text-red-300">Bot 序号不可用</span>
            )}
            {completionTag ? (
              <span
                className="rounded border border-gray-200 bg-gray-50 px-1.5 py-0.5 font-mono text-[10px] text-gray-600 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300"
                title={`真实发布目录：${bot.name}`}
              >
                tag: {completionTag}
              </span>
            ) : (
              <span className="rounded border border-red-200 bg-red-50 px-1.5 py-0.5 text-[10px] text-red-700 dark:border-red-800 dark:bg-red-900/20 dark:text-red-300">
                tag 身份不可用
              </span>
            )}
            <span className="font-mono text-[10px] text-gray-400">{compactBotName(bot.name)}</span>
            <span className="inline-flex items-center gap-1 rounded bg-emerald-100 px-1.5 py-0.5 text-[10px] text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300"><CheckIcon /> 严格发布</span>
            <span className={`rounded border px-1.5 py-0.5 text-[10px] ${certView.tone}`}>{certView.label}</span>
          </div>
          <div className="mt-1"><RatingLine bot={bot} /></div>
        </div>
        <span className="shrink-0 text-xs text-gray-400">{expanded ? "▲" : "▼"}</span>
      </button>

      {expanded && (
        <div className="space-y-4 border-t border-gray-100 p-4 dark:border-gray-800">
          {loadingDetail && !detail ? (
            <div className="space-y-2"><Skeleton.Line /><Skeleton.Line className="w-1/2" /></div>
          ) : detail ? (
            <>
              <div className={`rounded-lg border p-3 text-xs ${certView.tone}`}>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-semibold">{certView.label}</span>
                  <span>{certView.detail}</span>
                </div>
                {certification && (
                  <div className="mt-2 grid gap-1 font-mono text-[11px] sm:grid-cols-2">
                    <span>mode: {certification.mode ?? "—"}</span>
                    <span>policy: {certification.policy_id ?? "—"}</span>
                    <span>schema: {certification.certificate_schema_version ?? "—"}</span>
                    <span>rounds: {formalSummary?.self_play_rounds ?? "—"}+{formalSummary?.opponent_rounds ?? "—"} × {formalSummary?.target_hands ?? "—"} hands</span>
                    <span>profile: {certification.certification_profile ?? "权威投影不可用"}</span>
                    <span>opponent authority: {certification.opponent_authority ?? "权威投影不可用"}</span>
                    <span>strength weight: {certification.strength_evidence_weight ?? "不可用"}</span>
                    <span>strategy weight: {certification.strategy_evidence_weight ?? "不可用"}</span>
                    <span className="break-all sm:col-span-2">certificate: {certification.certificate_digest ?? "—"}</span>
                    <span className="break-all sm:col-span-2">signature sha256: {certification.certificate_signature_sha256 ?? "—"}</span>
                    <span className="break-all sm:col-span-2">published attestation: {certification.published_attestation_digest ?? "权威投影不可用"}</span>
                    <span className="break-all sm:col-span-2">verdict-ledger identity: {ledgerIdentity != null ? String(ledgerIdentity) : "权威投影不可用"}</span>
                  </div>
                )}
                {(certification?.issues?.length ?? 0) > 0 && (
                  <ul className="mt-2 space-y-1 font-mono text-[11px]">
                    {certification!.issues!.slice(0, 6).map((issue) => <li key={issue}>{issue}</li>)}
                  </ul>
                )}
              </div>

              {detail.parent && <p className="text-xs text-gray-500">发布父代：<span className="font-mono">{detail.parent}</span></p>}

              <div>
                <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                  <div className="flex flex-wrap gap-1">
                    {detail.files.map((filename) => (
                      <button
                        key={filename}
                        onClick={() => setSelectedFile(filename)}
                        className={`rounded px-2 py-1 text-xs ${selectedFile === filename ? "bg-blue-600 text-white" : "bg-gray-100 text-gray-700 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700"}`}
                      >
                        {filename}
                      </button>
                    ))}
                  </div>
                  <button
                    onClick={handleDownload}
                    disabled={downloading}
                    className="flex shrink-0 items-center gap-1 rounded bg-gray-700 px-3 py-1.5 text-xs text-white hover:bg-gray-800 disabled:opacity-50 dark:bg-gray-600"
                  >
                    <DownloadIcon /> {downloading ? "打包中…" : "下载发布包"}
                  </button>
                </div>
                {loadingCode ? (
                  <div className="space-y-2 p-3"><Skeleton.Line /><Skeleton.Line className="w-1/2" /></div>
                ) : (
                  <pre className="max-h-80 overflow-auto whitespace-pre rounded bg-gray-950 p-3 font-mono text-[11px] leading-relaxed text-gray-200">{code || "选择文件查看代码"}</pre>
                )}
              </div>

              {opponents.length > 0 && (
                <div>
                  <h3 className="mb-2 text-xs font-semibold text-gray-600 dark:text-gray-400">权威本地 70 手 H2H 样本</h3>
                  <div className="space-y-1">
                    {opponents.map((opponent) => (
                      <div key={opponent.name} className="flex items-center gap-3 text-xs text-gray-500">
                        <span className="w-24 truncate">{compactBotName(opponent.name)}</span>
                        <span className="font-mono">{(opponent.wr * 100).toFixed(1)}%</span>
                        <span>{opponent.games} 个70手样本</span>
                        <span className="font-mono text-[10px]">{opponent.wins}-{opponent.draws}-{opponent.losses}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </>
          ) : (
            <p className="text-xs text-red-500">无法读取发布 Bot 详情。</p>
          )}
        </div>
      )}
    </article>
  );
}

type BotSortMode = "selection" | "h2h" | "version";

export default function BotManager() {
  const { active: streamedBots } = useBots();
  const h2hData = useH2H();
  const updateData = useUpdateData();
  const { status, loading: statusLoading, error: statusError } = useControlStatus(5_000);
  const [loaded, setLoaded] = useState(false);
  const [sortMode, setSortMode] = useState<BotSortMode>("selection");
  const [message, setMessage] = useState("");

  const refresh = useCallback(async () => {
    try {
      const bots = await api.listBots();
      updateData({ bots });
    } catch (error) {
      setMessage(`发布池读取失败：${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setLoaded(true);
    }
  }, [updateData]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const publishedBots = useMemo(() => {
    if (!status?.epoch_initialized) return [];
    const allowed = new Set(status.active_bots);
    return streamedBots.filter((bot) => allowed.has(bot.name));
  }, [status, streamedBots]);

  const displayOrdinalByName = useMemo(() => new Map(
    [...publishedBots]
      .sort((a, b) => a.version - b.version)
      .map((bot, index) => [bot.name, index + 1] as const),
  ), [publishedBots]);

  const bots = useMemo(() => {
    return [...publishedBots].sort((a, b) => {
      if (sortMode === "version") return b.version - a.version;
      if (sortMode === "h2h") return (b.h2h_avg_wr ?? -1) - (a.h2h_avg_wr ?? -1);
      return (b.selection_score ?? b.leaderboard_score ?? -1) - (a.selection_score ?? a.leaderboard_score ?? -1);
    });
  }, [publishedBots, sortMode]);

  return (
    <>
      <PageMeta title="严格发布 Bot — Bot 自进化" description="查看严格国赛发布 Bot、签名证书和源码" />

      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-gray-800 dark:text-white">严格发布 Bot</h1>
          <p className="mt-1 text-xs text-gray-500">只读页面：Bot 序号仅表示当前发布池的版本顺序，真实身份以旁列 annotated completion tag 为准。</p>
        </div>
        <div className="flex items-center gap-2 text-xs text-gray-500">
          <span>排序</span>
          {(["selection", "h2h", "version"] as BotSortMode[]).map((mode) => (
            <button
              key={mode}
              onClick={() => setSortMode(mode)}
              className={`rounded px-2 py-1 ${sortMode === mode ? "bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300" : "hover:bg-gray-100 dark:hover:bg-gray-800"}`}
            >
              {mode === "selection" ? "选择分" : mode === "h2h" ? "H2H" : "版本"}
            </button>
          ))}
          <button onClick={refresh} className="rounded bg-gray-200 px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-300 dark:bg-gray-700 dark:text-gray-200 dark:hover:bg-gray-600">刷新</button>
        </div>
      </div>

      <EpochAuthorityStatus status={status} loading={statusLoading} error={statusError} className="mb-4" />

      {message && (
        <div className="mb-4 rounded-lg border border-blue-200 bg-blue-50 px-4 py-3 text-sm text-blue-700 dark:border-blue-800 dark:bg-blue-900/20 dark:text-blue-300">
          {message}
          <button onClick={() => setMessage("")} className="ml-2 text-xs underline">清除</button>
        </div>
      )}

      {!loaded || statusLoading ? (
        <Skeleton.Card count={2} />
      ) : bots.length === 0 ? (
        <div className="rounded-xl border border-gray-200 bg-white dark:border-border-subtle dark:bg-surface-1">
          <EmptyState
            message={status?.epoch_initialized
              ? "当前严格发布池为空；未发布候选和历史目录不会出现在这里。"
              : "epoch 尚未初始化；v155 等未发布目录是残骸，不是可管理候选。"}
          />
        </div>
      ) : (
        <div className="space-y-2">
          {bots.map((bot) => (
            <BotCard
              key={bot.name}
              bot={bot}
              displayOrdinal={displayOrdinalByName.get(bot.name) ?? null}
              h2hData={h2hData}
              onMessage={setMessage}
            />
          ))}
        </div>
      )}
    </>
  );
}
