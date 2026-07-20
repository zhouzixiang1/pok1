"""对局回放快照生成(里程碑 7)。

把 ``MatchRunner`` 发出的**线性事件流**(append-only 写进
``match_replays.events_json``)重组成「逐手牌桌快照序列」,供前端回放器
按手推进 + 按事件步进。

**核心概念**:

- **事件流**(linear):``[hand_start, cards_dealt, action, stage, action,
  ..., settle, hand_start, ...]``。orchestrator 走到哪写到哪。
- **逐手快照**(per-hand):把事件按 ``hand`` 字段分组,每手封一个完整
  快照——开始筹码 / 双方手牌 / 公共牌(逐阶段累积)/ 动作时间线 /
  结算 + 终筹。前端「上一手 / 下一手」切换的是这个粒度。
- **逐步快照**(per-step):按事件索引(0..len)切片,返回切片末尾的
  累积状态(当前手 / 当前阶段 / 当前公共牌 / 当前动作 / 当前筹码)。
  前端「上一步 / 下一步」切换的是这个粒度。

**事件类型**(与 ``match_runner.py:_emit`` 对齐):

- ``hand_start``:``{hand, sb_idx, bb_idx, names, player_chips, pot}``
- ``cards_dealt``:``{hand, hole_cards:[[c,c],[c,c]]}``
- ``stage``:``{hand, stage, cards:[c,...]}``(flop/turn/river)
- ``action``:``{hand, player_idx, action, amount?, stage, pot, player_chips}``
- ``settle``:``{hand, is_showdown, winner_idx?, pot, earnings, player_chips}``
- ``match_end``:``{winner, earnings, hands_played}``(全局,不归属任何手)

**卡牌格式兼容**:

- Card 对象的 ``to_str()`` → ``"<0,12>"`` 字符串
- 纯整数(发牌器偶尔会用)→ 按 ``suit = n // 13, rank = n % 13`` 解析
- 直接整数对 ``[suit, rank]`` 列表 → 同上
- 字符串 ``"<s,r>"`` → 解析后重组

**输出格式**(``card_to_display``):

- 同时给出 ``"<suit,rank>"`` 协议格式 + ``"♠A"`` 可读格式,前端二选一。
- ``suit`` 0-3 → ``♠♥♦♣``;``rank`` 0-12 → ``2-A``。
"""
from __future__ import annotations

from typing import Any

# 花色 / 点数符号(与 engine/deck.py 对齐)
_SUIT_NAMES = {0: "♠", 1: "♥", 2: "♦", 3: "♣"}
_RANK_NAMES = {
    0: "2", 1: "3", 2: "4", 3: "5", 4: "6", 5: "7", 6: "8",
    7: "9", 8: "10", 9: "J", 10: "Q", 11: "K", 12: "A",
}


# ══════════════════════════════════════════════════════════
# 卡牌格式转换
# ══════════════════════════════════════════════════════════

def card_to_display(card: Any) -> dict[str, Any]:
    """把任意卡牌输入转成统一展示格式。

    支持的输入:

    - 字符串 ``"<suit,rank>"``(Card.to_str() 格式)
    - 字符串可读格式(如 ``"♠A"``,``"♥10"``)— 反向解析
    - 整数(单值)→ ``suit = n // 13, rank = n % 13``
    - 长度 2 的 list/tuple → ``[suit, rank]``
    - 已是 dict 输出 → 透传

    返回::

        {"card": "<0,12>", "text": "♠A", "suit": 0, "rank": 12}

    解析失败返回 ``{"card": str(card), "text": "?", "suit": None,
    "rank": None}``(不抛,避免污染整个快照)。
    """
    if isinstance(card, dict):
        # 已是 display dict(幂等)
        if "suit" in card and "rank" in card and ("card" in card or "text" in card):
            return card
        suit = card.get("suit")
        rank = card.get("rank")
        return _format(suit, rank, fallback=str(card))

    if isinstance(card, str):
        s = card.strip()
        # 协议格式 <suit,rank>
        if s.startswith("<") and s.endswith(">") and "," in s:
            inner = s[1:-1].strip()
            try:
                suit_s, rank_s = inner.split(",")
                return _format(int(suit_s.strip()), int(rank_s.strip()),
                               fallback=card)
            except (ValueError, IndexError):
                return _format(None, None, fallback=card)
        # 反向解析可读格式(如 "♠A","♥10","♦K")
        if s:
            for suit_id, sym in _SUIT_NAMES.items():
                if s.startswith(sym):
                    rest = s[len(sym):]
                    if rest in _RANK_NAMES.values():
                        for rank_id, rname in _RANK_NAMES.items():
                            if rname == rest:
                                return _format(suit_id, rank_id, fallback=card)
        return _format(None, None, fallback=card)

    if isinstance(card, int):
        # 单整数:suit = n // 13, rank = n % 13(52 张标准牌编号)
        n = int(card)
        if 0 <= n < 52:
            return _format(n // 13, n % 13, fallback=card)
        return _format(None, None, fallback=card)

    if isinstance(card, (list, tuple)) and len(card) == 2:
        try:
            return _format(int(card[0]), int(card[1]), fallback=card)
        except (ValueError, TypeError):
            return _format(None, None, fallback=str(card))

    return _format(None, None, fallback=str(card))


def _format(suit: int | None, rank: int | None, *, fallback: Any) -> dict[str, Any]:
    """根据 suit/rank 拼最终展示 dict。无效值退回 fallback 字符串。"""
    if (suit is None or rank is None
            or suit not in _SUIT_NAMES or rank not in _RANK_NAMES):
        return {"card": str(fallback), "text": "?", "suit": None, "rank": None}
    return {
        "card": f"<{suit},{rank}>",
        "text": f"{_SUIT_NAMES[suit]}{_RANK_NAMES[rank]}",
        "suit": suit,
        "rank": rank,
    }


def _cards_to_display(cards: Any) -> list[dict[str, Any]]:
    """把卡牌列表转成展示列表。``None`` / 缺失返回空列表。"""
    if not cards:
        return []
    if isinstance(cards, (list, tuple)):
        return [card_to_display(c) for c in cards]
    return [card_to_display(cards)]


# ══════════════════════════════════════════════════════════
# 逐手快照生成
# ══════════════════════════════════════════════════════════

def build_hand_snapshots(events: list[dict]) -> list[dict]:
    """把线性事件流重组成「逐手快照序列」。

    遍历事件,按 ``hand`` 字段分组,每手累积 actions / community,
    ``settle`` 时封盘(填 settle 字段 + final_chips)。

    返回每手一个快照(顺序与 ``hand`` 出现顺序一致)。结构::

        {
          "hand": 1,
          "sb_idx": 0, "bb_idx": 1,
          "names": ["BotA", "BotB"],
          "initial_chips": [20000, 20000],
          "hole_cards": [[...], [...]],   # 双方手牌展示
          "community": [...],             # 公共牌(完整,所有阶段累积)
          "actions": [...],               # 本手动作时间线
          "settle": {...},                # 结算(可能缺失,若对局中断)
          "final_chips": [...]            # 本手结束筹码(=initial+earnings)
        }

    动作记录字段::

        {"player_idx": 0, "action": "call", "amount": null,
         "stage": "preflop", "pot": 150, "chips_after": [...]}

    **容错**:未出现 ``settle`` 的手(对局异常中断)也会输出,``settle``
    为 ``None``、``final_chips`` 取最后已知的 ``player_chips``。
    """
    snapshots: list[dict] = []
    by_hand: dict[int, dict[str, Any]] = {}
    order: list[int] = []  # 保留首次出现顺序

    for ev in events:
        etype = ev.get("type")
        hand = ev.get("hand")

        # 全局事件(match_start/match_end)不归属任何手,跳过
        if hand is None:
            continue

        snap = by_hand.get(hand)
        if snap is None:
            snap = _new_snapshot(hand)
            by_hand[hand] = snap
            order.append(hand)

        if etype == "hand_start":
            snap["sb_idx"] = ev.get("sb_idx")
            snap["bb_idx"] = ev.get("bb_idx")
            if ev.get("names") is not None:
                snap["names"] = list(ev["names"])
            chips = ev.get("player_chips")
            if chips is not None:
                snap["initial_chips"] = list(chips)
                # initial_chips 也是「当前筹码」的初值,后续 action 会更新
                snap["_current_chips"] = list(chips)

        elif etype == "cards_dealt":
            hole = ev.get("hole_cards")
            if hole is not None:
                snap["hole_cards"] = [_cards_to_display(h) for h in hole]

        elif etype == "stage":
            cards = ev.get("cards")
            for c in _cards_to_display(cards):
                snap["community"].append(c)

        elif etype == "action":
            chips_after = ev.get("player_chips")
            if chips_after is not None:
                snap["_current_chips"] = list(chips_after)
            snap["actions"].append({
                "player_idx": ev.get("player_idx"),
                "action": ev.get("action"),
                "amount": ev.get("amount"),
                "stage": ev.get("stage"),
                "pot": ev.get("pot"),
                "chips_after": (list(chips_after) if chips_after is not None
                                else None),
            })

        elif etype == "settle":
            earnings = ev.get("earnings")
            snap["settle"] = {
                "winner_idx": ev.get("winner_idx"),
                "earnings": (list(earnings) if earnings is not None else None),
                "is_showdown": bool(ev.get("is_showdown", False)),
                "pot": ev.get("pot"),
            }
            # final_chips = initial_chips + earnings(settle 事件本身也有
            # player_chips,优先用它;否则用 initial + earnings 推)
            if ev.get("player_chips") is not None:
                snap["final_chips"] = list(ev["player_chips"])
            elif (snap["initial_chips"] is not None
                  and earnings is not None):
                snap["final_chips"] = [
                    int(snap["initial_chips"][i]) + int(earnings[i])
                    for i in range(min(len(snap["initial_chips"]),
                                       len(earnings)))
                ]
            # settle 也会刷新当前筹码
            if ev.get("player_chips") is not None:
                snap["_current_chips"] = list(ev["player_chips"])

    for hand in order:
        snap = by_hand[hand]
        # 未封盘的手:final_chips 用最后已知筹码兜底
        if snap["final_chips"] is None:
            snap["final_chips"] = snap.get("_current_chips")
        # 去掉内部字段
        snap.pop("_current_chips", None)
        snapshots.append(snap)

    return snapshots


def _new_snapshot(hand: int) -> dict[str, Any]:
    """新建一手的空白快照(累积用)。"""
    return {
        "hand": hand,
        "sb_idx": None,
        "bb_idx": None,
        "names": [],
        "initial_chips": None,
        "hole_cards": [[], []],
        "community": [],
        "actions": [],
        "settle": None,
        "final_chips": None,
    }


# ══════════════════════════════════════════════════════════
# 逐步快照(按事件索引切片)
# ══════════════════════════════════════════════════════════

def snapshot_at_step(events: list[dict], step: int) -> dict[str, Any]:
    """返回「前 step 个事件」累积的中间状态快照。

    ``step`` 是事件索引(0..len),即「已处理多少个事件」。step=0 表示
    尚未开始,step=len 表示全部回放完毕。

    返回::

        {
          "step": step,
          "total_events": len,
          "hand": 1,                      # 当前手(None 若尚未 hand_start)
          "stage": "flop",                # 当前阶段(最近一次 stage 事件)
          "community_so_far": [...],      # 当前已发的公共牌
          "actions_so_far": [...],        # 当前手已发生的动作(累积)
          "chips_so_far": [...],          # 当前筹码(最近一次 player_chips)
          "names": [...],                 # 当前手玩家名
          "event": {...}                  # 本步对应的事件(step>0 时;None for step=0)
        }

    用于前端回放器逐步推进时定位到某个中间状态:拖动进度条 / 点上一步
    下一步时,前端发 step,后端返回该步的累积快照。
    """
    total = len(events)
    if step < 0:
        step = 0
    if step > total:
        step = total

    snap: dict[str, Any] = {
        "step": step,
        "total_events": total,
        "hand": None,
        "stage": None,
        "community_so_far": [],
        "actions_so_far": [],
        "chips_so_far": None,
        "names": [],
        "event": None,
    }

    # 累积处理前 step 个事件
    current_actions: list[dict] = []
    current_community: list[dict] = []
    last_event = None

    for ev in events[:step]:
        etype = ev.get("type")
        last_event = ev

        if etype == "hand_start":
            # 新手开始:重置本手累积
            current_actions = []
            current_community = []
            snap["hand"] = ev.get("hand")
            snap["names"] = list(ev.get("names") or [])
            snap["stage"] = "preflop"
            if ev.get("player_chips") is not None:
                snap["chips_so_far"] = list(ev["player_chips"])

        elif etype == "cards_dealt":
            pass  # 手牌不进逐步快照(展示用,放在逐手快照里)

        elif etype == "stage":
            snap["stage"] = ev.get("stage")
            for c in _cards_to_display(ev.get("cards")):
                current_community.append(c)

        elif etype == "action":
            current_actions.append({
                "player_idx": ev.get("player_idx"),
                "action": ev.get("action"),
                "amount": ev.get("amount"),
                "stage": ev.get("stage"),
                "pot": ev.get("pot"),
                "chips_after": (list(ev["player_chips"])
                                if ev.get("player_chips") is not None
                                else None),
            })
            if ev.get("player_chips") is not None:
                snap["chips_so_far"] = list(ev["player_chips"])

        elif etype == "settle":
            if ev.get("player_chips") is not None:
                snap["chips_so_far"] = list(ev["player_chips"])

        # match_end / match_start / 其他:不更新中间状态

    snap["community_so_far"] = current_community
    snap["actions_so_far"] = current_actions
    snap["event"] = last_event
    return snap
