/* 德州扑克对弈平台前端 */
"use strict";

// ── 常量 ──
const SUIT_SYMBOLS = { 0: "♠", 1: "♥", 2: "♦", 3: "♣" };
const RANK_NAMES = {
  0:"2", 1:"3", 2:"4", 3:"5", 4:"6", 5:"7", 6:"8",
  7:"9", 8:"10", 9:"J", 10:"Q", 11:"K", 12:"A"
};
const SUIT_COLORS = { 0: "black", 1: "red", 2: "red", 3: "black" };

// ── 状态 ──
let state = {
  status: "waiting",
  names: ["", ""],
  handNum: 0,
  totalEarnings: [0, 0],
  communityCards: [],
  pot: 0,
  stage: "",
  sbIdx: -1,
};

// ── DOM 元素 ──
const $ = (id) => document.getElementById(id);

// ── SSE 连接 ──
let eventSource = null;

function connectSSE() {
  if (eventSource) eventSource.close();
  eventSource = new EventSource("/api/events");

  eventSource.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      handleEvent(data);
    } catch (err) {
      console.error("SSE parse error:", err);
    }
  };

  eventSource.onerror = () => {
    console.log("SSE reconnecting...");
    setTimeout(connectSSE, 3000);
  };
}

// ── 事件处理 ──
function handleEvent(data) {
  switch (data.type) {
    case "connected":
      addLog(`客户端 ${data.client_idx + 1} 已连接 (${data.addr})`, "info");
      updateStatus("waiting", `已连接 ${data.client_idx + 1}/2`);
      break;

    case "names":
      state.names = data.names;
      $("p0-name").textContent = data.names[0] || "玩家 1";
      $("p1-name").textContent = data.names[1] || "玩家 2";
      $("earn-name-0").textContent = data.names[0] || "玩家 1";
      $("earn-name-1").textContent = data.names[1] || "玩家 2";
      addLog(`${data.names[0]} vs ${data.names[1]}`, "info");
      break;

    case "hand_start":
      state.handNum = data.hand;
      state.sbIdx = data.sb_idx;
      state.pot = 0;
      state.communityCards = [];
      state.stage = "preflop";
      $("hand-num").textContent = data.hand;
      $("current-stage").textContent = "Preflop";
      $("current-sb").textContent = state.names[data.sb_idx] || `玩家${data.sb_idx+1}`;
      updateRoles(data.sb_idx, data.bb_idx);
      updateCommunityCards([]);
      updatePot(0);
      // 清空玩家手牌展示（除了showdown）
      $("p0-cards").innerHTML = "";
      $("p1-cards").innerHTML = "";
      addLog(`── 第 ${data.hand} 局 ──`, "info");
      showTimer();
      break;

    case "stage":
      state.stage = data.stage;
      const stageNames = { flop: "Flop", turn: "Turn", river: "River" };
      $("current-stage").textContent = stageNames[data.stage] || data.stage;
      if (data.cards) {
        state.communityCards = data.cards;
        updateCommunityCards(data.cards);
      }
      addLog(`${stageNames[data.stage] || data.stage}: ${data.cards.map(formatCardStr).join(" ")}`, "info");
      break;

    case "action":
      handleActionEvent(data);
      break;

    case "settle":
      handleSettleEvent(data);
      break;

    case "match_end":
      updateStatus("ended", "比赛结束");
      hideTimer();
      addLog(`=== 比赛结束 ===`, "info");
      if (data.total_earnings) {
        addLog(`总输赢: ${state.names[0]} ${data.total_earnings[0]}, `
             + `${state.names[1]} ${data.total_earnings[1]}`, "settle");
      }
      break;

    case "error":
      addLog(`错误: ${data.message}`, "error");
      break;
  }

  // 更新累计输赢显示
  if (data.earnings && data.type === "settle") {
    state.totalEarnings[0] += data.earnings[0];
    state.totalEarnings[1] += data.earnings[1];
    updateEarnings();
  }
}

function handleActionEvent(data) {
  const name = state.names[data.player_idx] || `玩家${data.player_idx+1}`;
  const pIdx = data.player_idx;
  const action = data.action;

  if (action === "fold" || action.startsWith("timeout") || action.startsWith("illegal")) {
    addLog(`${name}: ${action}`, "fold");
  } else if (action === "raise") {
    addLog(`${name}: raise ${data.amount} (投入 ${data.needed})`, "raise");
    if (data.pot) updatePot(data.pot);
  } else if (action === "call") {
    addLog(`${name}: call`, "call");
    if (data.pot) updatePot(data.pot);
  } else if (action === "check") {
    addLog(`${name}: check`, "check");
  } else if (action === "allin") {
    addLog(`${name}: ALL-IN ${data.amount}!`, "allin");
    if (data.pot) updatePot(data.pot);
  }
}

function handleSettleEvent(data) {
  hideTimer();
  if (data.is_showdown) {
    // 显示双方手牌
    if (data.sb_cards) {
      const sbZone = data.sb_idx === 0 ? $("p0-cards") : $("p1-cards");
      sbZone.innerHTML = data.sb_cards.map(cardHtml).join("");
    }
    if (data.bb_cards) {
      const bbZone = data.bb_idx === 0 ? $("p0-cards") : $("p1-cards");
      bbZone.innerHTML = data.bb_cards.map(cardHtml).join("");
    }
    // 更新公共牌
    if (data.community) {
      state.communityCards = data.community;
      updateCommunityCards(data.community);
    }

    const winName = data.winner_idx !== null
      ? state.names[data.winner_idx]
      : "平局";
    const detail = `${data.sb_hand} vs ${data.bb_hand}`;
    addLog(`比牌: ${detail} → ${winName} 赢得 ${data.pot}`, "settle");
  } else {
    const winName = state.names[data.winner_idx] || `玩家${data.winner_idx+1}`;
    addLog(`${winName} 赢得底池 ${data.pot} (${data.reason})`, "settle");
  }

  // 添加到历史
  addHistoryItem(data);
}

// ── UI 更新 ──

function updateStatus(status, text) {
  const badge = $("status-badge");
  badge.className = `badge ${status}`;
  badge.textContent = text;

  $("btn-start").disabled = status !== "waiting" || text.includes("2/2") === false;
  if (text.includes("2/2")) $("btn-start").disabled = false;
}

function updateRoles(sbIdx, bbIdx) {
  const p0Role = sbIdx === 0 ? "SB" : "BB";
  const p1Role = sbIdx === 1 ? "SB" : "BB";
  $("p0-role").textContent = p0Role;
  $("p0-role").className = `player-role ${sbIdx === 0 ? "sb" : "bb"}`;
  $("p1-role").textContent = p1Role;
  $("p1-role").className = `player-role ${sbIdx === 1 ? "sb" : "bb"}`;
}

function updateCommunityCards(cards) {
  const container = $("community-cards");
  const slots = [];
  for (let i = 0; i < 5; i++) {
    if (i < cards.length) {
      slots.push(cardHtml(cards[i]));
    } else {
      slots.push('<div class="card-slot empty"></div>');
    }
  }
  container.innerHTML = slots.join("");
}

function updatePot(amount) {
  state.pot = amount;
  $("pot").textContent = amount;
}

function updateEarnings() {
  for (let i = 0; i < 2; i++) {
    const el = $("earn-val-" + i);
    const val = state.totalEarnings[i];
    el.textContent = (val >= 0 ? "+" : "") + val;
    el.className = "earning-value " + (val > 0 ? "positive" : val < 0 ? "negative" : "");
  }
}

function addLog(text, cls) {
  const log = $("action-log");
  const div = document.createElement("div");
  div.className = "log-entry " + (cls || "");
  div.textContent = text;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

function addHistoryItem(data) {
  const list = $("history-list");
  if (list.querySelector(".empty-history")) list.innerHTML = "";

  const handNum = data.hand;
  const winnerIdx = data.winner_idx;
  const pot = data.pot;
  const isShowdown = data.is_showdown;

  let resultText, resultCls;
  if (winnerIdx === null) {
    resultText = "平局";
    resultCls = "draw";
  } else {
    resultText = `${state.names[winnerIdx]} +${pot}`;
    resultCls = "win";
  }

  const detail = isShowdown ? `${data.sb_hand} vs ${data.bb_hand}` : "fold";

  const item = document.createElement("div");
  item.className = "history-item";
  item.innerHTML = `
    <span class="hand-num">#${handNum}</span>
    <span class="hand-result ${resultCls}">${resultText}</span>
    <span class="hand-detail">${detail}</span>
  `;
  list.appendChild(item);
  list.scrollTop = list.scrollHeight;
}

// ── 扑克牌渲染 ──

function parseCardStr(s) {
  s = s.replace(/[<>]/g, "");
  const [suit, rank] = s.split(",").map(Number);
  return { suit, rank };
}

function formatCardStr(s) {
  const c = parseCardStr(s);
  return SUIT_SYMBOLS[c.suit] + RANK_NAMES[c.rank];
}

function cardHtml(cardStr) {
  const c = typeof cardStr === "string" ? parseCardStr(cardStr) : cardStr;
  const color = SUIT_COLORS[c.suit];
  const suit = SUIT_SYMBOLS[c.suit];
  const rank = RANK_NAMES[c.rank];
  return `<div class="card-slot card-face ${color}">
    <div class="card-content"><span class="card-rank">${rank}</span><span class="card-suit">${suit}</span></div>
  </div>`;
}

// ── 计时器 ──
let timerInterval = null;

function showTimer() {
  const el = $("timer");
  el.style.display = "inline";
  let t = 60;
  el.textContent = t;
  if (timerInterval) clearInterval(timerInterval);
  timerInterval = setInterval(() => {
    t--;
    el.textContent = t;
    if (t <= 0) { clearInterval(timerInterval); el.textContent = "⏰"; }
  }, 1000);
}

function hideTimer() {
  if (timerInterval) clearInterval(timerInterval);
  $("timer").style.display = "none";
}

// ── API 调用 ──

async function startMatch() {
  const resp = await fetch("/api/start", { method: "POST" });
  const data = await resp.json();
  if (data.error) { addLog(data.error, "error"); return; }
  addLog("比赛开始！", "info");
  updateStatus("playing", "比赛中");
  $("btn-start").disabled = true;
}

async function resetMatch() {
  const resp = await fetch("/api/reset", { method: "POST" });
  const data = await resp.json();
  addLog("已重置", "info");
  state = { status: "waiting", names: ["", ""], handNum: 0,
            totalEarnings: [0, 0], communityCards: [], pot: 0, stage: "", sbIdx: -1 };
  updateStatus("waiting", "等待连接");
  updateCommunityCards([]);
  updatePot(0);
  updateEarnings();
  $("p0-cards").innerHTML = "";
  $("p1-cards").innerHTML = "";
  $("action-log").innerHTML = '<div class="log-entry info">等待客户端连接...</div>';
  $("history-list").innerHTML = '<div class="empty-history">暂无历史记录</div>';
  $("hand-num").textContent = "0";
  $("current-stage").textContent = "--";
  $("current-sb").textContent = "--";
  $("p0-role").textContent = "";
  $("p1-role").textContent = "";
  $("p0-name").textContent = "玩家 1";
  $("p1-name").textContent = "玩家 2";
  $("earn-name-0").textContent = "玩家 1";
  $("earn-name-1").textContent = "玩家 2";
}

// ── 初始化 ──
connectSSE();
// 定期刷新状态
setInterval(async () => {
  try {
    const resp = await fetch("/api/state");
    const data = await resp.json();
    if (data.status === "playing" && state.status !== "playing") {
      updateStatus("playing", "比赛中");
      $("btn-start").disabled = true;
    }
  } catch (e) {}
}, 3000);
