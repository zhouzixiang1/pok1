import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  MatchReplayData,
  NativeActionName,
  NativeHandRecord,
  NativeReplayAction,
  NativeStreet,
} from "../api/types";
import { api } from "../api/client";
import PageMeta from "../components/common/PageMeta";
import { EpochAuthorityStatus } from "../components/evolution/EpochAuthorityStatus";
import { useRecentMatches } from "../context/DataProvider";
import { useControlStatus } from "../hooks/useControlStatus";
import { compactBotName } from "../lib/utils";

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

function Card({ value }: { value: string }) {
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
        {cards.map((card) => <Card key={card} value={card} />)}
      </div>
      <div className={`mt-2 text-xs ${earnings === null ? "text-white/50" : earnings >= 0 ? "text-emerald-300" : "text-red-300"}`}>
        {earnings === null ? "尚未结算" : `本手 ${formatSigned(earnings)}`}
      </div>
    </div>
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

export default function MatchReplay() {
  const matches = useRecentMatches();
  const { status, loading: statusLoading, error: statusError } = useControlStatus(5_000);
  const [selectedMatch, setSelectedMatch] = useState<MatchReplayData | null>(null);
  const [loadError, setLoadError] = useState("");
  const [currentHand, setCurrentHand] = useState(0);
  const [currentStep, setCurrentStep] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [speed, setSpeed] = useState(800);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const visibleMatches = useMemo(
    () => status?.epoch_initialized ? matches : [],
    [matches, status?.epoch_initialized],
  );

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
    if (!status?.epoch_initialized || !summary) {
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
  }, [status?.epoch_initialized, visibleMatches]);

  useEffect(() => {
    if (!status?.epoch_initialized) {
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
  }, [selectedMatch, status?.epoch_initialized, visibleMatches]);

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
  const street: NativeStreet = settled
    ? "river"
    : currentAction?.stage ?? "preflop";
  const boardCount = settled ? 5 : ({ preflop: 0, flop: 3, turn: 4, river: 5 } as const)[street];
  const pot = hand
    ? settled
      ? hand.settlement.pot ?? hand.actions[hand.actions.length - 1]?.pot_after ?? hand.starting_pot
      : currentAction?.pot_after ?? currentAction?.pot_before ?? hand.starting_pot
    : 0;

  return (
    <>
      <PageMeta title="国赛原生对局回放 — Bot 自进化" description="national_tcp_policy_v1 hand_records 回放" />
      <EpochAuthorityStatus status={status} loading={statusLoading} error={statusError} compact className="mb-4" />
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-4">
        <div className="xl:col-span-1">
          <div className="rounded-2xl border border-gray-200 bg-white p-4 dark:border-border-subtle dark:bg-white/[0.03]">
            <h2 className="mb-3 text-sm font-semibold text-gray-700 dark:text-gray-300">当前身份对局 ({visibleMatches.length})</h2>
            <div className="max-h-[650px] space-y-2 overflow-y-auto">
              {visibleMatches.length === 0 && (
                <div className="text-xs text-gray-500">
                  {!status?.epoch_initialized
                    ? "epoch 尚未初始化；旧回放已从权威视图移除。"
                    : status.active_bots.length === 0
                      ? "当前严格发布池为空，尚无回放。"
                      : "等待首个同发布池、同 evaluation identity 的完整 70 手回放。"}
                </div>
              )}
              {visibleMatches.map((match) => (
                <button
                  key={match.id}
                  onClick={() => loadMatch(match.id)}
                  className={`w-full rounded-lg border p-2 text-left text-xs transition-colors ${selectedMatch?.id === match.id ? "border-brand-500 bg-brand-50 dark:bg-brand-500/10" : "border-gray-200 hover:border-gray-300 dark:border-border-subtle"}`}
                >
                  <div className="font-medium text-gray-800 dark:text-gray-200">{compactBotName(match.bot0)} vs {compactBotName(match.bot1)}</div>
                  <div className="mt-1 flex items-center justify-between text-gray-500"><span>{match.bot0_wins}胜</span><span>{match.draws}平</span><span>{match.bot1_wins}胜</span></div>
                  <div className="mt-1 text-[10px] text-gray-400">{formatTime(match.timestamp)}</div>
                </button>
              ))}
            </div>
          </div>
        </div>

        <div className="space-y-4 xl:col-span-3">
          {loadError && <div className="rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700 dark:border-red-900/50 dark:bg-red-950/30 dark:text-red-300">{loadError}</div>}
          {!selectedMatch && !loadError && (
            <div className="rounded-2xl border border-gray-200 bg-gray-900 p-12 text-center text-sm text-white/60 dark:border-border-subtle">
              {status?.epoch_initialized && visibleMatches.length > 0
                ? "选择一场当前身份的原生 TCP 对局。"
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
                      {hand.board.slice(0, boardCount).map((card) => <Card key={card} value={card} />)}
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

              <div className="rounded-2xl border border-gray-200 bg-white p-4 dark:border-border-subtle dark:bg-white/[0.03]">
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
                  <button onClick={() => setIsPlaying((playing) => !playing)} className={`flex items-center gap-1 rounded px-4 py-1.5 text-xs font-medium text-white ${isPlaying ? "bg-red-500" : "bg-brand-500"}`}>{isPlaying ? <><PauseIcon />暂停</> : <><PlayIcon />播放</>}</button>
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
    </>
  );
}
