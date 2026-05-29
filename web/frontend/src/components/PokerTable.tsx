import { useRef, useEffect, useCallback } from "react";
import type { DisplayFrame } from "../api/types";

const SUITS = ["♥", "♦", "♠", "♣"];
const SUIT_COLORS = ["#e53935", "#e53935", "#1a1a1a", "#1a1a1a"];
const POINTS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"];
const PHASES = ["翻牌前", "翻牌", "转牌", "河牌", "摊牌"];
const HAND_TYPES = [
  "", "高牌", "一对", "两对", "三条", "顺子", "同花", "葫芦", "四条", "同花顺",
];

function cardPoint(c: number): string {
  return c < 0 ? "" : POINTS[Math.floor(c / 4)];
}

function cardSuit(c: number): string {
  return c < 0 ? "" : SUITS[c % 4];
}

function cardColor(c: number): string {
  return c < 0 ? "#888" : SUIT_COLORS[c % 4];
}

interface PokerTableProps {
  frame: DisplayFrame | null;
  bot0Name: string;
  bot1Name: string;
}

const W = 800;
const H = 500;
const TABLE_RX = 280;
const TABLE_RY = 150;
const CARD_W = 44;
const CARD_H = 64;

export default function PokerTable({ frame, bot0Name, bot1Name }: PokerTableProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    ctx.clearRect(0, 0, W, H);

    // Background
    const bg = ctx.createRadialGradient(W / 2, H / 2, 50, W / 2, H / 2, 400);
    bg.addColorStop(0, "#2d5a1e");
    bg.addColorStop(1, "#1a3a10");
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, W, H);

    // Table
    ctx.beginPath();
    ctx.ellipse(W / 2, H / 2, TABLE_RX, TABLE_RY, 0, 0, Math.PI * 2);
    ctx.fillStyle = "#2e7d32";
    ctx.fill();
    ctx.strokeStyle = "#4a2c0a";
    ctx.lineWidth = 6;
    ctx.stroke();

    if (!frame || !frame.matchdata || !frame.player_chips || !frame.player_cards || !frame.public_cards) {
      ctx.fillStyle = "rgba(255,255,255,0.5)";
      ctx.font = "18px sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(frame ? "回放数据不完整" : "选择一场对局以查看回放", W / 2, H / 2);
      return;
    }

    const y0 = H / 2 - TABLE_RY - 20; // top player
    const y1 = H / 2 + TABLE_RY + 20; // bottom player
    const cx = W / 2;

    // Player names & chips
    drawPlayerInfo(ctx, cx, y0 - 10, bot0Name.replace("claude_", ""), frame.player_chips[0],
      frame.matchdata.total_win_chips[0], frame.round_idx === 0 && !frame.final_result);
    drawPlayerInfo(ctx, cx, y1 + 10, bot1Name.replace("claude_", ""), frame.player_chips[1],
      frame.matchdata.total_win_chips[1], frame.round_idx === 1 && !frame.final_result);

    // Player cards (top)
    const pc0 = frame.player_cards[0];
    drawCard(ctx, cx - CARD_W - 4, y0 + 18, pc0[0]);
    drawCard(ctx, cx + 4, y0 + 18, pc0[1]);

    // Player cards (bottom)
    const pc1 = frame.player_cards[1];
    drawCard(ctx, cx - CARD_W - 4, y1 - CARD_H - 18, pc1[0]);
    drawCard(ctx, cx + 4, y1 - CARD_H - 18, pc1[1]);

    // Community cards
    const pub = frame.public_cards;
    const pubX = cx - (5 * (CARD_W + 6)) / 2;
    for (let i = 0; i < 5; i++) {
      drawCard(ctx, pubX + i * (CARD_W + 6), H / 2 - CARD_H / 2, pub.length > i ? pub[i] : -1);
    }

    // Pot
    ctx.fillStyle = "#fff";
    ctx.font = "bold 16px sans-serif";
    ctx.textAlign = "center";
    ctx.fillText(`底池 ${frame.pot}`, cx, H / 2 + TABLE_RY / 2);

    // Phase & hand info
    ctx.font = "13px sans-serif";
    ctx.fillStyle = "rgba(255,255,255,0.7)";
    ctx.textAlign = "left";
    ctx.fillText(`手牌 ${frame.matchdata.hand + 1}/${frame.matchdata.max_hand}`, 20, 25);
    ctx.fillText(`阶段 ${PHASES[frame.round] || "摊牌"}`, 20, 45);

    // Last action
    if (frame.last_action) {
      const la = frame.last_action;
      const ay = la.player_id === 0 ? y0 + 16 : y1 - 16;
      const ax = cx + CARD_W + 30;
      drawActionLabel(ctx, ax, ay, la.action_type, la.action);
    }

    // Round bets
    drawRoundBet(ctx, cx + TABLE_RX + 10, y0 + 18, frame.round_player_bet[0]);
    drawRoundBet(ctx, cx + TABLE_RX + 10, y1 - CARD_H - 18, frame.round_player_bet[1]);

    // Results
    if (frame.final_result) {
      drawResult(ctx, cx, y0 + 20, frame.final_result[0]);
      drawResult(ctx, cx, y1 - 20, frame.final_result[1]);
    } else if (frame.temp_result && frame.temp_result.length >= 2) {
      drawTempResult(ctx, cx, y0 + 20, frame.temp_result[0]);
      drawTempResult(ctx, cx, y1 - 20, frame.temp_result[1]);
    }
  }, [frame, bot0Name, bot1Name]);

  useEffect(() => { draw(); }, [draw]);

  return (
    <canvas
      ref={canvasRef}
      width={W}
      height={H}
      className="mx-auto rounded-lg shadow-lg"
      style={{ maxWidth: "100%", height: "auto" }}
    />
  );
}

function drawCard(ctx: CanvasRenderingContext2D, x: number, y: number, card: number) {
  // Card background
  ctx.fillStyle = card >= 0 ? "#fff" : "#3a5a8c";
  ctx.strokeStyle = "#333";
  ctx.lineWidth = 1;
  roundRect(ctx, x, y, CARD_W, CARD_H, 5);
  ctx.fill();
  ctx.stroke();

  if (card >= 0) {
    const point = cardPoint(card);
    const suit = cardSuit(card);
    ctx.fillStyle = cardColor(card);
    ctx.font = "bold 16px sans-serif";
    ctx.textAlign = "left";
    ctx.fillText(point, x + 5, y + 20);
    ctx.font = "14px sans-serif";
    ctx.fillText(suit, x + 5, y + 36);
    // Bottom right (inverted)
    ctx.textAlign = "right";
    ctx.font = "bold 16px sans-serif";
    ctx.fillText(point, x + CARD_W - 5, y + CARD_H - 8);
  } else {
    // Card back pattern
    ctx.fillStyle = "rgba(255,255,255,0.1)";
    roundRect(ctx, x + 4, y + 4, CARD_W - 8, CARD_H - 8, 3);
    ctx.fill();
    // Diamond pattern instead of Chinese text (font rendering issues on canvas)
    const cx2 = x + CARD_W / 2;
    const cy2 = y + CARD_H / 2;
    ctx.fillStyle = "rgba(255,255,255,0.25)";
    ctx.beginPath();
    ctx.moveTo(cx2, cy2 - 10);
    ctx.lineTo(cx2 + 7, cy2);
    ctx.lineTo(cx2, cy2 + 10);
    ctx.lineTo(cx2 - 7, cy2);
    ctx.closePath();
    ctx.fill();
  }
}

function drawPlayerInfo(
  ctx: CanvasRenderingContext2D, x: number, y: number,
  name: string, chips: number, totalWin: number, isTurn: boolean
) {
  ctx.textAlign = "center";
  if (isTurn) {
    ctx.fillStyle = "rgba(255,238,88,0.3)";
    roundRect(ctx, x - 70, y - 16, 140, 32, 8);
    ctx.fill();
  }
  ctx.fillStyle = isTurn ? "#ffee58" : "#fff";
  ctx.font = "bold 14px sans-serif";
  ctx.fillText(`${name}  筹码 ${chips}`, x, y);
  ctx.font = "11px sans-serif";
  ctx.fillStyle = "rgba(255,255,255,0.6)";
  ctx.fillText(`累计 ${totalWin > 0 ? "+" : ""}${Math.floor(totalWin)}`, x, y + 15);
}

function drawActionLabel(
  ctx: CanvasRenderingContext2D, x: number, y: number,
  actionType: string, amount: number
) {
  const labels: Record<string, string> = {
    fold: "弃牌", check: "过牌", call: "跟注", raise: `加注 ${amount}`, allin: "全押",
  };
  const colors: Record<string, string> = {
    fold: "#bdbdbd", check: "#81d4fa", call: "#81d4fa", raise: "#ffee58", allin: "#ef5350",
  };
  const label = labels[actionType] || actionType;
  ctx.font = "bold 14px sans-serif";
  ctx.textAlign = "left";
  ctx.fillStyle = colors[actionType] || "#fff";
  ctx.fillText(label, x, y);
}

function drawRoundBet(ctx: CanvasRenderingContext2D, x: number, y: number, bet: number) {
  if (bet === -1) {
    ctx.fillStyle = "#bdbdbd";
    ctx.font = "12px sans-serif";
    ctx.textAlign = "left";
    ctx.fillText("弃牌", x, y + 10);
  } else if (bet > 0) {
    ctx.fillStyle = "#fff";
    ctx.font = "12px sans-serif";
    ctx.textAlign = "left";
    ctx.fillText(`下注 ${bet}`, x, y + 10);
  }
}

function drawResult(
  ctx: CanvasRenderingContext2D, x: number, y: number,
  result: { win_chips: number; win_games: number }
) {
  ctx.fillStyle = "rgba(0,0,0,0.6)";
  roundRect(ctx, x - 90, y - 12, 180, 30, 6);
  ctx.fill();
  ctx.textAlign = "center";
  ctx.font = "bold 14px sans-serif";
  const chips = Math.floor(result.win_chips);
  if (chips > 0) ctx.fillStyle = "#ffee58";
  else if (chips < 0) ctx.fillStyle = "#ef5350";
  else ctx.fillStyle = "#fff";
  ctx.fillText(`${chips > 0 ? "+" : ""}${chips}  (胜${result.win_games}局)`, x, y + 8);
}

function drawTempResult(
  ctx: CanvasRenderingContext2D, x: number, y: number,
  result: { win_chips: number; max_hand_type?: number }
) {
  const chips = Math.floor(result.win_chips);
  const ht = result.max_hand_type ? HAND_TYPES[result.max_hand_type] : "";
  ctx.fillStyle = "rgba(0,0,0,0.5)";
  roundRect(ctx, x - 70, y - 12, 140, 26, 6);
  ctx.fill();
  ctx.textAlign = "center";
  ctx.font = "12px sans-serif";
  ctx.fillStyle = chips > 0 ? "#ffee58" : chips < 0 ? "#ef5350" : "#fff";
  ctx.fillText(`${chips > 0 ? "+" : ""}${chips} ${ht}`, x, y + 6);
}

function roundRect(
  ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number
) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.lineTo(x + w - r, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + r);
  ctx.lineTo(x + w, y + h - r);
  ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
  ctx.lineTo(x + r, y + h);
  ctx.quadraticCurveTo(x, y + h, x, y + h - r);
  ctx.lineTo(x, y + r);
  ctx.quadraticCurveTo(x, y, x + r, y);
  ctx.closePath();
}
