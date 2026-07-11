import type { ArenaEvent, ArenaSession } from "../api/types";

export type ArenaStreet = "preflop" | "flop" | "turn" | "river" | "showdown";

export interface ArenaActionView {
  eventId: number;
  playerIdx: 0 | 1;
  action: string;
  stage: string;
  hand: number;
  timestamp: string;
  amount?: number;
  reason?: string;
}

export interface ArenaHandView {
  eventId: number;
  hand: number;
  winnerIdx: 0 | 1 | null;
  earnings: [number, number];
  pot: number;
  showdown: boolean;
  reason?: string;
}

export interface ArenaViewModel {
  hand: number;
  handsTotal: number;
  street: ArenaStreet;
  pot: number;
  board: string[];
  holeCards: [string[], string[]];
  chips: [number, number];
  bets: [number, number];
  actingPlayer: 0 | 1 | null;
  deadlineEpochMs: number | null;
  latestActions: [string, string];
  actions: ArenaActionView[];
  history: ArenaHandView[];
}

const pair = (value: unknown, fallback: [number, number]): [number, number] => {
  if (!Array.isArray(value) || value.length < 2) return fallback;
  return [Number(value[0]) || 0, Number(value[1]) || 0];
};

const stringCards = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];

export function buildArenaView(
  session: ArenaSession | null,
  events: ArenaEvent[],
): ArenaViewModel {
  const view: ArenaViewModel = {
    hand: session?.hands_completed ?? 0,
    handsTotal: session?.hands_total ?? 70,
    street: "preflop",
    pot: 0,
    board: [],
    holeCards: [[], []],
    chips: [20_000, 20_000],
    bets: [0, 0],
    actingPlayer: null,
    deadlineEpochMs: null,
    latestActions: ["", ""],
    actions: [],
    history: [],
  };

  for (const event of [...events].sort((a, b) => a.event_id - b.event_id)) {
    const payload = event.payload;
    if (event.type === "hand_started") {
      view.hand = Number(payload.hand) || event.hand_no;
      view.street = "preflop";
      view.pot = Number(payload.pot) || 0;
      view.board = [];
      view.holeCards = [[], []];
      view.chips = pair(payload.player_chips, [19_950, 19_900]);
      view.bets = [50, 100];
      view.actingPlayer = null;
      view.deadlineEpochMs = null;
      view.latestActions = ["", ""];
      continue;
    }
    if (event.type === "hole_cards_dealt") {
      const cards = payload.hole_cards;
      if (Array.isArray(cards) && cards.length >= 2) {
        view.holeCards = [stringCards(cards[0]), stringCards(cards[1])];
      }
      continue;
    }
    if (event.type === "street_started") {
      const street = String(payload.stage || "preflop") as ArenaStreet;
      view.street = street;
      view.board = [...view.board, ...stringCards(payload.cards)];
      view.chips = pair(payload.player_chips, view.chips);
      view.bets = [0, 0];
      view.actingPlayer = null;
      view.deadlineEpochMs = null;
      continue;
    }
    if (event.type === "action_requested") {
      const player = Number(payload.player_idx);
      view.actingPlayer = player === 0 || player === 1 ? player : null;
      view.street = String(payload.stage || view.street) as ArenaStreet;
      view.pot = Number(payload.pot) || view.pot;
      view.bets = pair(payload.player_bets, view.bets);
      view.chips = pair(payload.player_chips, view.chips);
      view.deadlineEpochMs = Number(payload.deadline_epoch_ms) || null;
      continue;
    }
    if (["player_action", "illegal_action", "timeout"].includes(event.type)) {
      const player = Number(payload.player_idx);
      if (player !== 0 && player !== 1) continue;
      const action = event.type === "timeout" ? "timeout" : String(payload.action || "");
      view.latestActions[player] = action;
      view.actions.push({
        eventId: event.event_id,
        playerIdx: player,
        action,
        stage: String(payload.stage || view.street),
        hand: Number(payload.hand) || event.hand_no,
        timestamp: event.timestamp,
        amount: payload.amount === undefined ? undefined : Number(payload.amount),
        reason: payload.reason === undefined ? undefined : String(payload.reason),
      });
      view.pot = Number(payload.pot) || view.pot;
      view.chips = pair(payload.player_chips, view.chips);
      view.actingPlayer = null;
      view.deadlineEpochMs = null;
      continue;
    }
    if (event.type === "hand_finished") {
      const winner = payload.winner_idx;
      const winnerIdx = winner === 0 || winner === 1 ? winner : null;
      const earnings = pair(payload.earnings, [0, 0]);
      const hand = Number(payload.hand) || event.hand_no;
      view.hand = Math.max(view.hand, hand);
      view.street = payload.is_showdown ? "showdown" : view.street;
      view.pot = Number(payload.pot) || view.pot;
      view.chips = pair(payload.player_chips, view.chips);
      view.actingPlayer = null;
      view.deadlineEpochMs = null;
      view.history.push({
        eventId: event.event_id,
        hand,
        winnerIdx,
        earnings,
        pot: Number(payload.pot) || 0,
        showdown: Boolean(payload.is_showdown),
        reason: payload.reason === undefined ? undefined : String(payload.reason),
      });
    }
  }
  return view;
}

export function actionLabel(action: string): string {
  if (action.startsWith("illegal:")) return "非法动作";
  if (action.startsWith("protocol_")) return "通信违规";
  if (action.startsWith("raise")) return action.replace("raise", "加注至");
  return {
    fold: "弃牌",
    call: "跟注",
    check: "过牌",
    allin: "全下",
    timeout: "超时",
  }[action] ?? action;
}
