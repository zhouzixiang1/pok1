import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Chart from "react-apexcharts";
import type { ApexOptions } from "apexcharts";
import { useSearchParams } from "react-router";
import { api } from "../api/client";
import type {
  MatchReplayData,
  NativeActionName,
  NativeHandRecord,
  NativeReplayAction,
  NativeStreet,
} from "../api/types";
import {
  useRatings,
  useMatchMatrix,
  useH2H,
  useBots,
  useRecentMatches,
  useUpdateData,
  useControlStatusValue,
} from "../context/DataProvider";
import { useBoundPolling } from "../hooks/useBoundPolling";
import PageMeta from "../components/common/PageMeta";
import { EmptyState } from "../components/shared/EmptyState";
import { Skeleton } from "../components/shared/Skeleton";
import { EvolutionPageScaffold } from "../components/evolution/EvolutionPageScaffold";
import { EvolutionSection, EvolutionStatusBadge, EvolutionSurface } from "../components/evolution/ui";
import type { CanonicalGenerationIdentity } from "../api/control";
import {
  canonicalGenerationIdentityIssues,
  sameCanonicalGenerationIdentity,
} from "../lib/canonicalGenerationIdentity";
import { cn, compactBotName } from "../lib/utils";

const STREET_LABELS: Record<NativeStreet, string> = {
  preflop: "翻牌前",
  flop: "翻牌",
  turn: "转牌",
  river: "河牌",
};

const ACTION_LABELS: Record<NativeActionName, string> = {
  fold: "弃牌",
  call: "跟注",
  check: "过牌",
  raise: "加注",
  allin: "全押",
};

const SUITS = ["♠", "♥", "♦", "♣"];
const RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"];

type FlatHand = {
  gameIndex: number;
  record: NativeHandRecord;
};

function formatTime(ts: string): string {
  if (!ts || ts.length < 14) return ts;
  return `${ts.slice(0, 4)}-${ts.slice(4, 6)}-${ts.slice(6, 8)} ${ts.slice(9, 11)}:${ts.slice(11, 13)}:${ts.slice(13, 15)}`;
}

function formatSigned(value: number): string {
  return `${value > 0 ? "+" : ""}${value}`;
}

function parseOfficialCard(card: string): { rank: string; suit: string; red: boolean } | null {
  const match = /^<([0-3]),([0-9]|1[0-2])>$/.exec(card);
  if (!match) return null;
  const suit = Number(match[1]);
  const rank = Number(match[2]);
  return { rank: RANKS[rank], suit: SUITS[suit], red: suit === 1 || suit === 2 };
}

function PlayingCard({ value }: { value: string }) {
  const parsed = parseOfficialCard(value);
  return (
    <span
      className={`inline-flex min-h-14 min-w-10 flex-col items-center justify-center rounded-md border border-gray-300 bg-white px-2 py-1 font-semibold shadow-sm ${parsed?.red ? "text-red-600" : "text-gray-900"}`}
      title={value}
    >
      {parsed ? <><span>{parsed.rank}</span><span>{parsed.suit}</span></> : <span>?</span>}
    </span>
  );
}

function actionText(action: NativeReplayAction): string {
  const amount = action.amount !== null && action.action === "raise" ? ` ${action.amount}` : "";
  return `${STREET_LABELS[action.stage]} · ${ACTION_LABELS[action.action]}${amount}`;
}

const PlayIcon = () => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3" /></svg>
);
const PauseIcon = () => (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16" /><rect x="14" y="4" width="4" height="16" /></svg>
);

/**
 * Bot 强度 + 回放页：合并原 BotManager（Glicko-2 强度排行）+ MatchReplay
 * （点击 bot → 比赛回放）+ MatchMatrix（H2H 胜率矩阵）。
 *
 * - Glicko-2 强度排行表
 * - H2H 胜率矩阵
 * - 点击 bot → 比赛回放面板
 */
export default function Bots() {
  const ratings = useRatings();
  const matrix = useMatchMatrix();
  const h2hRaw = useH2H();
  const { active: streamedBots } = useBots();
  const updateData = useUpdateData();
  const { status } = useControlStatusValue();
  const epochReady = Boolean(status?.epoch_initialized);
  const [searchParams, setSearchParams] = useSearchParams();
  const expandVersion = (() => {
    const raw = searchParams.get("v");
    if (!raw) return null;
    const n = Number(raw);
    return Number.isSafeInteger(n) && n > 0 ? n : null;
  })();

  // 发布池清单：用 useBoundPolling 统一拉取 /api/bots，写入 SSE store 供消费。
  const { loading: botsLoading, error: botsError } = useBoundPolling(
    async () => {
      const bots = await api.listBots();
      updateData({ bots });
      return bots;
    },
    { enabled: epochReady, pollMs: 15_000 },
  );

  // ── 强度排行（Glicko-2 选择分）──
  const visibleRatings = useMemo(() => epochReady
    ? ratings.filter((bot) => Number.isFinite(bot.selection_score ?? bot.leaderboard_score))
    : [], [ratings, epochReady]);

  const scoreOf = (b: (typeof ratings)[number]) => (
    (b.selection_score ?? b.leaderboard_score) as number
  );

  const rankedBots = useMemo(() => {
    if (!epochReady) return [];
    return [...visibleRatings].sort((a, b) => scoreOf(b) - scoreOf(a));
  }, [visibleRatings, epochReady]);

  // ── 发布 Bot 身份（来自原 BotManager 的 validatedPublishedIdentity）──
  const publishedBots = useMemo(() => {
    if (!epochReady) return [];
    const allowed = new Set(status!.active_bots);
    return streamedBots.filter((bot) => allowed.has(bot.name));
  }, [status, streamedBots, epochReady]);

  const identityByName = useMemo(() => {
    const authorityByName = new Map<string, CanonicalGenerationIdentity | null>();
    for (const identity of status?.strict_published_bot_identities ?? []) {
      const name = identity.canonical_bot_name;
      if (authorityByName.has(name) || canonicalGenerationIdentityIssues(identity).length > 0) {
        authorityByName.set(name, null);
      } else {
        authorityByName.set(name, identity);
      }
    }
    return authorityByName;
  }, [status?.strict_published_bot_identities]);

  // ── H2H 胜率矩阵热力图 ──
  const matrixChart = useMemo(() => {
    if (!matrix || !matrix.bots.length || matrix.evidence_available !== true) {
      return { series: [], options: {} as ApexOptions };
    }
    const bots = matrix.bots.map(compactBotName);
    const series = matrix.bots.map((botName, i) => ({
      name: compactBotName(botName),
      data: matrix.bots.map((_, j) => ({
        x: bots[j],
        y: i === j ? null : matrix.matrix[i]?.[j] ?? null,
      })),
    }));
    const options: ApexOptions = {
      chart: {
        fontFamily: "Outfit, sans-serif",
        height: Math.max(360, bots.length * 32),
        type: "heatmap",
        background: "transparent",
        toolbar: { show: true },
      },
      dataLabels: { enabled: false },
      plotOptions: {
        heatmap: {
          radius: 2,
          shadeIntensity: 0.8,
          colorScale: {
            ranges: [
              { from: -0.01, to: 0.01, color: "#374151", name: "无数据" },
              { from: 0.01, to: 0.35, color: "#dc2626", name: "很弱 <35%" },
              { from: 0.35, to: 0.45, color: "#f87171", name: "弱 35-45%" },
              { from: 0.45, to: 0.55, color: "#1f2937", name: "均势 45-55%" },
              { from: 0.55, to: 0.65, color: "#93c5fd", name: "强 55-65%" },
              { from: 0.65, to: 0.75, color: "#3b82f6", name: "很强 65-75%" },
              { from: 0.75, to: 1.01, color: "#1d4ed8", name: "极强 >75%" },
            ],
          },
        },
      },
      xaxis: { labels: { style: { fontSize: "10px" } }, axisBorder: { show: false }, axisTicks: { show: false } },
      yaxis: { labels: { style: { fontSize: "10px" } } },
      tooltip: {
        custom: ({ seriesIndex, dataPointIndex }: { seriesIndex: number; dataPointIndex: number }) => {
          const val = matrix!.matrix[seriesIndex]?.[dataPointIndex];
          if (val == null) return '<div style="padding:4px 8px;font-size:12px">无数据</div>';
          const rowBot = matrix!.bots[seriesIndex];
          const colBot = matrix!.bots[dataPointIndex];
          const key1 = `${rowBot} vs ${colBot}`;
          const key2 = `${colBot} vs ${rowBot}`;
          const entry = h2hRaw[key1] || h2hRaw[key2];
          let extra = "";
          if (entry) {
            const isA = !!h2hRaw[key1];
            const w = isA ? entry.a_wins : entry.b_wins;
            const l = isA ? entry.b_wins : entry.a_wins;
            extra = `<div style="margin-top:2px">${entry.games} 个70手样本 · ${w}胜 ${entry.draws}平 ${l}负</div>`;
          }
          return `<div style="padding:6px 10px;font-size:12px">
            <div style="font-weight:600">${bots[seriesIndex]} vs ${bots[dataPointIndex]}</div>
            <div>${(val * 100).toFixed(0)}% 胜率</div>
            ${extra}
          </div>`;
        },
      },
      stroke: { width: 1, colors: ["#fff"] },
    };
    return { series, options };
  }, [matrix, h2hRaw]);

  // ── 回放（来自原 MatchReplay）──
  const matches = useRecentMatches();
  const visibleMatches = useMemo(() => epochReady ? matches : [], [matches, epochReady]);
  const [selectedMatch, setSelectedMatch] = useState<MatchReplayData | null>(null);
  const [loadError, setLoadError] = useState("");
  const [currentHand, setCurrentHand] = useState(0);
  const [currentStep, setCurrentStep] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [speed, setSpeed] = useState(800);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const hands = useMemo<FlatHand[]>(() => (
    selectedMatch?.games.flatMap((game, gameIndex) =>
      game.hand_records.map((record) => ({ gameIndex, record })),
    ) ?? []
  ), [selectedMatch]);
  const selected = hands[currentHand] ?? null;
  const maxStep = selected ? selected.record.actions.length + 1 : 0;

  const loadMatch = useCallback(async (id: string) => {
    setLoadError("");
    setIsPlaying(false);
    const summary = visibleMatches.find((match) => match.id === id);
    if (!epochReady || !summary) {
      setSelectedMatch(null);
      setLoadError("该回放不在当前严格 epoch 的权威列表中，已拒绝加载。");
      return;
    }
    try {
      const data = await api.matchReplay(id);
      if (
        data.id !== summary.id
        || data.evaluation_epoch !== "national_tcp_policy_v1"
        || data.evaluation_identity_digest !== summary.evaluation_identity_digest
      ) {
        throw new Error("replay identity changed during load");
      }
      setSelectedMatch(data);
      setCurrentHand(0);
      setCurrentStep(0);
    } catch (error) {
      setSelectedMatch(null);
      setCurrentHand(0);
      setCurrentStep(0);
      const reason = error instanceof Error ? error.message : String(error);
      setLoadError(`该记录不属于当前 national_tcp_policy_v1 / native_tcp 证据身份，已拒绝加载。${reason ? `（${reason}）` : ""}`);
    }
  }, [epochReady, visibleMatches]);

  // 点击强度排行中的 bot → 跳到该 bot 的最近一场对局回放
  const focusBot = useCallback((botName: string) => {
    const match = visibleMatches.find((m) => m.bot0 === botName || m.bot1 === botName);
    if (match) {
      void loadMatch(match.id);
    } else {
      setLoadError(`Bot ${compactBotName(botName)} 暂无可回放的对局。`);
      setSelectedMatch(null);
    }
  }, [visibleMatches, loadMatch]);

  useEffect(() => {
    if (!epochReady) {
      setSelectedMatch(null);
      setLoadError("");
      setCurrentHand(0);
      setCurrentStep(0);
      setIsPlaying(false);
      return;
    }
    if (
      selectedMatch
      && !visibleMatches.some((match) => (
        match.id === selectedMatch.id
        && match.evaluation_identity_digest === selectedMatch.evaluation_identity_digest
      ))
    ) {
      setSelectedMatch(null);
      setLoadError("发布池或 evaluation identity 已切换，旧回放已从视图移除。");
      setCurrentHand(0);
      setCurrentStep(0);
      setIsPlaying(false);
    }
  }, [selectedMatch, epochReady, visibleMatches]);

  const changeHand = useCallback((index: number) => {
    if (index < 0 || index >= hands.length) return;
    setCurrentHand(index);
    setCurrentStep(0);
    setIsPlaying(false);
  }, [hands.length]);

  useEffect(() => {
    if (!isPlaying || !selected) return;
    timerRef.current = setInterval(() => {
      setCurrentStep((step) => {
        if (step < selected.record.actions.length + 1) return step + 1;
        if (currentHand < hands.length - 1) {
          setCurrentHand((hand) => hand + 1);
          return 0;
        }
        setIsPlaying(false);
        return step;
      });
    }, speed);
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [currentHand, hands.length, isPlaying, selected, speed]);

  const hand = selected?.record ?? null;
  const visibleActions = hand ? hand.actions.slice(0, Math.min(currentStep, hand.actions.length)) : [];
  const currentAction = hand && currentStep > 0 && currentStep <= hand.actions.length
    ? hand.actions[currentStep - 1]
    : null;
  const settled = Boolean(hand && currentStep > hand.actions.length);
  const street: NativeStreet = settled ? "river" : currentAction?.stage ?? "preflop";
  const boardCount = settled ? 5 : ({ preflop: 0, flop: 3, turn: 4, river: 5 } as const)[street];
  const pot = hand
    ? settled
      ? hand.settlement.pot ?? hand.actions[hand.actions.length - 1]?.pot_after ?? hand.starting_pot
      : currentAction?.pot_after ?? currentAction?.pot_before ?? hand.starting_pot
    : 0;

  const emptyStrengthMessage = !epochReady
    ? "未初始化"
    : status!.active_bots.length === 0
      ? "发布池为空"
      : "等待首个评分周期";

  return (
    <EvolutionPageScaffold title="Bot 强度与回放">
      <PageMeta title="Bot 强度与回放 — Bot 自进化" description="严格发布池的强度排行、H2H 矩阵与对局回放" />

      {/* Glicko-2 强度排行表 */}
      <EvolutionSurface padding="sm" className="space-y-3">
        <EvolutionSection title="Glicko-2 强度排行" />
        {botsLoading && rankedBots.length === 0 ? (
          <Skeleton.Card count={2} />
        ) : rankedBots.length === 0 ? (
          <EmptyState message={emptyStrengthMessage} />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-100 dark:border-border-subtle text-left text-xs text-gray-400 dark:text-gray-500">
                  <th className="px-3 py-2 font-medium w-12">#</th>
                  <th className="px-3 py-2 font-medium">Bot</th>
                  <th className="px-3 py-2 font-medium">选择分</th>
                  <th className="px-3 py-2 font-medium">H2H</th>
                  <th className="px-3 py-2 font-medium">净筹码/70手</th>
                  <th className="px-3 py-2 font-medium">覆盖</th>
                  <th className="px-3 py-2 font-medium">场数</th>
                  <th className="px-3 py-2 font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {rankedBots.map((bot, idx) => {
                  const score = scoreOf(bot);
                  return (
                    <tr key={bot.name} className="border-b border-gray-50 dark:border-border-subtle/50 hover:bg-gray-50 dark:hover:bg-white/[0.02] transition-colors">
                      <td className="px-3 py-2.5 text-gray-400 font-medium text-xs">{idx + 1}</td>
                      <td className="px-3 py-2.5">
                        <button
                          onClick={() => focusBot(bot.name)}
                          className="text-sm font-medium text-gray-800 dark:text-gray-200 hover:text-brand-600 dark:hover:text-brand-400 text-left"
                        >
                          {compactBotName(bot.name)}
                        </button>
                      </td>
                      <td className="px-3 py-2.5 font-mono font-semibold text-gray-700 dark:text-gray-200 tabular-nums">
                        {Number.isFinite(score) ? score.toFixed(4) : "—"}
                      </td>
                      <td className="px-3 py-2.5 text-gray-600 dark:text-gray-300 text-xs tabular-nums">
                        {bot.h2h_avg_wr != null ? `${(bot.h2h_avg_wr * 100).toFixed(1)}%` : "—"}
                      </td>
                      <td className="px-3 py-2.5 text-gray-600 dark:text-gray-300 text-xs tabular-nums">
                        {bot.secondary_net_chips_mean != null
                          ? `${bot.secondary_net_chips_mean >= 0 ? "+" : ""}${bot.secondary_net_chips_mean.toFixed(0)}`
                          : "—"}
                      </td>
                      <td className="px-3 py-2.5 text-gray-600 dark:text-gray-300 text-xs tabular-nums">
                        {bot.h2h_coverage != null ? `${(bot.h2h_coverage * 100).toFixed(0)}%` : "—"}
                      </td>
                      <td className="px-3 py-2.5 text-gray-500 text-xs tabular-nums">{bot.games ?? "—"}</td>
                      <td className="px-3 py-2.5">
                        <button
                          onClick={() => focusBot(bot.name)}
                          className="rounded bg-brand-50 px-2 py-1 text-[11px] font-medium text-brand-600 hover:bg-brand-100 dark:bg-brand-500/10 dark:text-brand-400"
                        >
                          回放
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {botsError && (
          <p className="text-xs text-error-600 dark:text-error-400">发布池刷新失败：{botsError.message}</p>
        )}
      </EvolutionSurface>

      {/* H2H 胜率矩阵 */}
      <EvolutionSurface className="mt-4" padding="sm">
        <EvolutionSection
          title="H2H 胜率矩阵"
          actions={
            <span className="flex items-center gap-2 text-xs text-gray-400">
              <span className="flex items-center gap-1">
                <span className="inline-block h-2.5 w-2.5 rounded-sm bg-brand-600" />强
              </span>
              <span className="flex items-center gap-1">
                <span className="inline-block h-2.5 w-2.5 rounded-sm bg-red-600" />弱
              </span>
            </span>
          }
        />
        <div className="mt-3">
          {!epochReady || !matrix || matrix.evidence_available !== true || !matrix.bots.length ? (
            <EmptyState message={
              !epochReady
                ? "未初始化"
                : status!.active_bots.length === 0
                  ? "发布池为空"
                  : "等待首个评分周期"
            } />
          ) : matrixChart.series.length === 0 ? (
            <div className="py-12 text-center text-sm text-gray-400">当前矩阵无可绘制数据</div>
          ) : (
            <Chart
              options={matrixChart.options}
              series={matrixChart.series}
              type="heatmap"
              height={Math.max(360, (matrix?.bots.length ?? 10) * 32)}
            />
          )}
        </div>
      </EvolutionSurface>

      {/* 对局回放面板 */}
      <EvolutionSurface className="mt-4" padding="sm">
        <EvolutionSection
          title="对局回放"
          actions={
            visibleMatches.length > 0 ? (
              <span className="text-xs text-gray-400">可回放对局 {visibleMatches.length} 场</span>
            ) : undefined
          }
        />
        <div className="mt-3 grid grid-cols-1 gap-4 xl:grid-cols-4">
          {/* 对局列表 */}
          <div className="xl:col-span-1">
            <div className="rounded-xl border border-gray-100 p-3 dark:border-border-subtle">
              <h4 className="mb-2 text-xs font-semibold text-gray-600 dark:text-gray-300">当前身份对局</h4>
              <div className="max-h-[420px] space-y-2 overflow-y-auto">
                {visibleMatches.length === 0 && (
                  <div className="text-xs text-gray-500">
                    {!epochReady
                      ? "未初始化"
                      : status!.active_bots.length === 0
                        ? "发布池为空"
                        : "等待首个评分周期"}
                  </div>
                )}
                {visibleMatches.map((match) => (
                  <button
                    key={match.id}
                    onClick={() => loadMatch(match.id)}
                    className={cn(
                      "w-full rounded-lg border p-2 text-left text-xs transition-colors",
                      selectedMatch?.id === match.id
                        ? "border-brand-500 bg-brand-50 dark:bg-brand-500/10"
                        : "border-gray-200 hover:border-gray-300 dark:border-border-subtle",
                    )}
                  >
                    <div className="font-medium text-gray-800 dark:text-gray-200">{compactBotName(match.bot0)} vs {compactBotName(match.bot1)}</div>
                    <div className="mt-1 flex items-center justify-between text-gray-500"><span>{match.bot0_wins}胜</span><span>{match.draws}平</span><span>{match.bot1_wins}胜</span></div>
                    <div className="mt-1 text-[10px] text-gray-400">{formatTime(match.timestamp)}</div>
                  </button>
                ))}
              </div>
            </div>
          </div>

          {/* 回放主区 */}
          <div className="space-y-4 xl:col-span-3">
            {loadError && <div className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700 dark:border-red-900/50 dark:bg-red-950/30 dark:text-red-300">{loadError}</div>}
            {!selectedMatch && !loadError && (
              <div className="rounded-2xl border border-gray-200 bg-gray-900 p-12 text-center text-sm text-white/60 dark:border-border-subtle">
                {epochReady && visibleMatches.length > 0
                  ? "点击上方强度排行中的 Bot，或选择左侧一场对局开始回放。"
                  : "当前没有可加载的权威回放。"}
              </div>
            )}

            {selectedMatch && hand && selected && (
              <>
                <div className="rounded-2xl border border-gray-200 bg-gradient-to-br from-emerald-950 to-gray-950 p-5 dark:border-border-subtle">
                  <div className="mb-4 flex flex-wrap items-center justify-between gap-2 text-xs text-white/60">
                    <span>样本 {selected.gameIndex + 1}/{selectedMatch.games.length} · 手牌 {hand.hand}/70</span>
                    <span>{STREET_LABELS[street]} · 底池 {pot}</span>
                    <span className="rounded bg-emerald-500/15 px-2 py-1 text-emerald-300">native_tcp · national_tcp_policy_v1</span>
                  </div>

                  <div className="grid gap-4 md:grid-cols-[1fr_1.4fr_1fr]">
                    <PlayerCards
                      label={selectedMatch.bot0}
                      position={hand.sb_idx === 0 ? "SB" : "BB"}
                      cards={hand.hole_cards[0]}
                      earnings={settled ? hand.settlement.earnings[0] : null}
                    />
                    <div className="flex min-h-36 flex-col items-center justify-center rounded-xl border border-white/10 bg-emerald-900/30 p-3">
                      <div className="mb-3 text-xs uppercase tracking-wide text-white/50">公共牌</div>
                      <div className="flex flex-wrap justify-center gap-2">
                        {hand.board.slice(0, boardCount).map((card) => <PlayingCard key={card} value={card} />)}
                        {boardCount === 0 && <span className="text-sm text-white/40">翻牌前</span>}
                      </div>
                      {currentAction && (
                        <div className="mt-4 rounded-full bg-black/25 px-3 py-1 text-sm text-white">
                          {compactBotName(selectedMatch[currentAction.player_idx === 0 ? "bot0" : "bot1"])} · {actionText(currentAction)}
                        </div>
                      )}
                      {settled && <div className="mt-4 text-sm font-semibold text-amber-300">结算 · {hand.settlement.reason}</div>}
                    </div>
                    <PlayerCards
                      label={selectedMatch.bot1}
                      position={hand.sb_idx === 1 ? "SB" : "BB"}
                      cards={hand.hole_cards[1]}
                      earnings={settled ? hand.settlement.earnings[1] : null}
                    />
                  </div>
                </div>

                <div className="rounded-xl border border-gray-100 p-3 dark:border-border-subtle">
                  <div className="mb-3 flex flex-wrap items-center gap-2">
                    <span className="text-xs text-gray-500">手牌:</span>
                    <select value={currentHand} onChange={(event) => changeHand(Number(event.target.value))} className="max-w-full rounded border border-gray-200 px-2 py-1 text-xs dark:border-border-subtle dark:bg-surface-1">
                      {hands.map(({ gameIndex, record }, index) => (
                        <option key={`${gameIndex}-${record.hand}`} value={index}>
                          样本 {gameIndex + 1} / 手牌 {record.hand} · {formatSigned(record.settlement.earnings[0])}
                        </option>
                      ))}
                    </select>
                    <span className="text-xs text-gray-400">步骤 {currentStep + 1}/{maxStep + 1}</span>
                    <div className="ml-auto flex items-center gap-2 text-xs text-gray-400">
                      <span>速度</span>
                      <select value={speed} onChange={(event) => setSpeed(Number(event.target.value))} className="rounded border border-gray-200 px-2 py-1 dark:border-border-subtle dark:bg-surface-1">
                        <option value={1500}>0.5x</option><option value={800}>1x</option><option value={400}>2x</option><option value={200}>4x</option>
                      </select>
                    </div>
                  </div>

                  <div className="mb-4 flex flex-wrap gap-2">
                    <button onClick={() => changeHand(Math.max(0, currentHand - 1))} disabled={currentHand === 0} className="rounded bg-gray-100 px-3 py-1.5 text-xs disabled:opacity-40 dark:bg-surface-1">上一手</button>
                    <button onClick={() => setCurrentStep((step) => Math.max(0, step - 1))} disabled={currentStep === 0} className="rounded bg-gray-100 px-3 py-1.5 text-xs disabled:opacity-40 dark:bg-surface-1">上一步</button>
                    <button onClick={() => setIsPlaying((playing) => !playing)} className={cn("flex items-center gap-1 rounded px-4 py-1.5 text-xs font-medium text-white", isPlaying ? "bg-red-500" : "bg-brand-500")}>{isPlaying ? <><PauseIcon />暂停</> : <><PlayIcon />播放</>}</button>
                    <button onClick={() => setCurrentStep((step) => Math.min(maxStep, step + 1))} disabled={currentStep >= maxStep} className="rounded bg-gray-100 px-3 py-1.5 text-xs disabled:opacity-40 dark:bg-surface-1">下一步</button>
                    <button onClick={() => changeHand(Math.min(hands.length - 1, currentHand + 1))} disabled={currentHand >= hands.length - 1} className="rounded bg-gray-100 px-3 py-1.5 text-xs disabled:opacity-40 dark:bg-surface-1">下一手</button>
                  </div>

                  <div className="space-y-2">
                    {visibleActions.map((action, index) => (
                      <div key={`${action.stage}-${index}`} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-gray-100 px-3 py-2 text-xs dark:border-border-subtle">
                        <span className="font-medium text-gray-700 dark:text-gray-200">{index + 1}. {compactBotName(selectedMatch[action.player_idx === 0 ? "bot0" : "bot1"])} · {actionText(action)}</span>
                        <span className="text-gray-400">底池 {action.pot_before ?? "?"} → {action.pot_after ?? "?"}</span>
                      </div>
                    ))}
                    {settled && (
                      <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:border-amber-900/40 dark:bg-amber-950/20 dark:text-amber-300">
                        结算：{compactBotName(selectedMatch.bot0)} {formatSigned(hand.settlement.earnings[0])}，{compactBotName(selectedMatch.bot1)} {formatSigned(hand.settlement.earnings[1])}；{hand.settlement.is_showdown ? "摊牌" : "未摊牌"}
                      </div>
                    )}
                    {visibleActions.length === 0 && !settled && <div className="py-4 text-center text-xs text-gray-400">本手起始状态</div>}
                  </div>
                </div>
              </>
            )}
          </div>
        </div>
      </EvolutionSurface>

      {/* 已发布 Bot 清单（来自原 BotManager，可展开） */}
      {publishedBots.length > 0 && (
        <EvolutionSurface className="mt-4" padding="sm">
          <EvolutionSection
            title="已发布 Bot 清单"
            actions={
              expandVersion != null ? (
                <EvolutionStatusBadge tone="info">展开 v{expandVersion}</EvolutionStatusBadge>
              ) : undefined
            }
          />
          <div className="mt-3 space-y-2">
            {publishedBots.map((bot) => {
              const identity = identityByName.get(bot.name) ?? null;
              const validIdentity = identity && bot.name === bot.canonical_bot_name
                && bot.version === bot.canonical_version
                && canonicalGenerationIdentityIssues(bot, bot.version).length === 0
                && canonicalGenerationIdentityIssues(identity, bot.version).length === 0
                && sameCanonicalGenerationIdentity(bot, identity)
                  ? identity
                  : null;
              return (
                <PublishedBotRow
                  key={bot.name}
                  bot={bot}
                  identity={validIdentity}
                  defaultExpanded={expandVersion === bot.version}
                  onToggleExpand={(expanded) => {
                    const next = new URLSearchParams(searchParams);
                    if (expanded) next.set("v", String(bot.version));
                    else next.delete("v");
                    setSearchParams(next, { replace: true });
                  }}
                />
              );
            })}
          </div>
        </EvolutionSurface>
      )}
    </EvolutionPageScaffold>
  );
}

function PlayerCards({
  label,
  position,
  cards,
  earnings,
}: {
  label: string;
  position: "SB" | "BB";
  cards: [string, string];
  earnings: number | null;
}) {
  return (
    <div className="rounded-xl border border-white/15 bg-black/20 p-3 text-center">
      <div className="mb-2 text-sm font-semibold text-white">
        {compactBotName(label)} <span className="text-xs font-normal text-white/60">{position}</span>
      </div>
      <div className="flex justify-center gap-2">
        {cards.map((card) => <PlayingCard key={card} value={card} />)}
      </div>
      <div className={`mt-2 text-xs ${earnings === null ? "text-white/50" : earnings >= 0 ? "text-emerald-300" : "text-red-300"}`}>
        {earnings === null ? "尚未结算" : `本手 ${formatSigned(earnings)}`}
      </div>
    </div>
  );
}

type PublishedBotRowProps = {
  bot: import("../api/types").BotSummary;
  identity: CanonicalGenerationIdentity | null;
  defaultExpanded: boolean;
  onToggleExpand: (expanded: boolean) => void;
};

function PublishedBotRow({
  bot,
  identity,
  defaultExpanded,
  onToggleExpand,
}: PublishedBotRowProps) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const [detail, setDetail] = useState<import("../api/types").BotDetail | null>(null);
  const [selectedFile, setSelectedFile] = useState("");
  const [code, setCode] = useState("");
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [loadingCode, setLoadingCode] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [message, setMessage] = useState("");

  useEffect(() => {
    if (!expanded || detail || loadingDetail) return;
    let cancelled = false;
    setLoadingDetail(true);
    api.botDetail(bot.version)
      .then((next) => {
        if (cancelled) return;
        setDetail(next);
        if (next.files.length > 0) setSelectedFile(next.files[0]);
      })
      .catch((error) => setMessage(`详情加载失败：${error instanceof Error ? error.message : String(error)}`))
      .finally(() => { if (!cancelled) setLoadingDetail(false); });
    return () => { cancelled = true; };
  }, [expanded, detail, loadingDetail, bot.version]);

  useEffect(() => {
    if (!expanded || !selectedFile) return;
    let cancelled = false;
    setLoadingCode(true);
    api.botCode(bot.version, selectedFile)
      .then((c) => { if (!cancelled) setCode(c); })
      .catch((error) => { if (!cancelled) setCode(`加载代码失败：${error instanceof Error ? error.message : String(error)}`); })
      .finally(() => { if (!cancelled) setLoadingCode(false); });
    return () => { cancelled = true; };
  }, [bot.version, expanded, selectedFile]);

  const toggle = () => {
    const next = !expanded;
    setExpanded(next);
    onToggleExpand(next);
  };

  const handleDownload = async () => {
    setDownloading(true);
    try {
      await api.downloadBot(bot.version);
      setMessage(`已下载 ${bot.name}.zip`);
    } catch (error) {
      setMessage(`下载失败：${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setDownloading(false);
    }
  };

  return (
    <article className="overflow-hidden rounded-xl border border-gray-200 bg-white dark:border-border-subtle dark:bg-surface-1">
      <button
        onClick={toggle}
        className="flex w-full items-center justify-between gap-4 px-4 py-3 text-left hover:bg-gray-50 dark:hover:bg-gray-800/40"
      >
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            {identity ? (
              <span className="font-semibold text-gray-900 dark:text-white">第{identity.generation_ordinal}代</span>
            ) : (
              <span className="font-semibold text-red-700 dark:text-red-300">Bot 双身份不可用</span>
            )}
            <span className="font-mono text-[10px] text-gray-400">{identity?.canonical_bot_name ?? bot.name}</span>
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-500 dark:text-gray-400">
            <span>选择分 <span className="font-mono font-semibold text-gray-800 dark:text-gray-200">{(bot.selection_score ?? bot.leaderboard_score) != null ? (bot.selection_score ?? bot.leaderboard_score)!.toFixed(4) : "—"}</span></span>
            <span>H2H {bot.h2h_avg_wr != null ? `${(bot.h2h_avg_wr * 100).toFixed(1)}%` : "—"}</span>
            <span>70 手样本 {bot.strength_sample_count ?? bot.games ?? 0}</span>
          </div>
        </div>
        <span className="shrink-0 text-xs text-gray-400">{expanded ? "▲" : "▼"}</span>
      </button>

      {message && <p className="px-4 pb-2 text-xs text-amber-600">{message}</p>}

      {expanded && (
        <div className="space-y-3 border-t border-gray-100 p-4 dark:border-gray-800">
          {loadingDetail && !detail ? (
            <div className="space-y-2"><Skeleton.Line /><Skeleton.Line className="w-1/2" /></div>
          ) : detail ? (
            <>
              {detail.parent && <p className="text-xs text-gray-500">发布父代：<span className="font-mono">{detail.parent}</span></p>}
              <div>
                <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                  <div className="flex flex-wrap gap-1">
                    {detail.files.map((filename) => (
                      <button
                        key={filename}
                        onClick={() => setSelectedFile(filename)}
                        className={cn("rounded px-2 py-1 text-xs", selectedFile === filename ? "bg-blue-600 text-white" : "bg-gray-100 text-gray-700 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700")}
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
                    {downloading ? "打包中…" : "下载发布包"}
                  </button>
                </div>
                {loadingCode ? (
                  <div className="space-y-2 p-3"><Skeleton.Line /><Skeleton.Line className="w-1/2" /></div>
                ) : (
                  <pre className="max-h-80 overflow-auto whitespace-pre rounded bg-gray-950 p-3 font-mono text-[11px] leading-relaxed text-gray-200">{code || "选择文件查看代码"}</pre>
                )}
              </div>
            </>
          ) : (
            <p className="text-xs text-red-500">无法读取发布 Bot 详情。</p>
          )}
        </div>
      )}
    </article>
  );
}
