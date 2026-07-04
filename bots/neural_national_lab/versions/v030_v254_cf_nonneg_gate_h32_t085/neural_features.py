from __future__ import annotations

from collections import Counter
from typing import Any


LABELS = ("fold", "call", "raise_half", "raise_pot", "raise_2pot", "allin")
INITIAL_CHIPS = 20000.0


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def _rank(card: int) -> int:
    return int(card) // 4 + 2


def _suit(card: int) -> int:
    return int(card) % 4


def _preflop(cards: list[int]) -> float:
    if len(cards) < 2:
        return 0.5
    hi, lo = sorted((_rank(cards[0]), _rank(cards[1])), reverse=True)
    suited = _suit(cards[0]) == _suit(cards[1])
    if hi == lo:
        return _clip(0.56 + 0.035 * (hi - 2))
    score = 0.18 + 0.42 * ((hi - 2) / 12.0) + 0.18 * ((lo - 2) / 12.0)
    score += 0.12 if suited else 0.0
    score -= 0.025 * min(5, max(0, hi - lo - 1))
    if hi == 14 and lo >= 10:
        score += 0.08
    return _clip(score)


def _board(public_cards: list[int]) -> list[float]:
    ranks = [_rank(c) for c in public_cards]
    suits = [_suit(c) for c in public_cards]
    rc = Counter(ranks)
    sc = Counter(suits)
    max_rank = max(rc.values(), default=0)
    max_suit = max(sc.values(), default=0)
    unique = sorted(set(ranks))
    straight = 0
    for start in range(2, 11):
        if sum(1 for r in unique if start <= r <= start + 4) >= 3:
            straight += 1
    if 14 in unique and sum(1 for r in unique if r in {14, 2, 3, 4, 5}) >= 3:
        straight += 1
    return [
        len(public_cards) / 5.0,
        _clip((max(ranks, default=2) - 2) / 12.0),
        1.0 if max_rank >= 2 else 0.0,
        1.0 if max_rank >= 3 else 0.0,
        _clip(max_suit / 5.0),
        1.0 if max_suit >= 4 else 0.0,
        _clip(straight / 4.0),
        _clip(sum(1 for r in ranks if r >= 11) / 5.0),
    ]


def _history(history: list[dict[str, Any]], my_id: int) -> list[float]:
    opp_id = 1 - my_id
    out: list[float] = []
    for pid in (my_id, opp_id):
        rows = [h for h in history if h.get("player_id") == pid]
        total = max(1, len(rows))
        c = Counter(h.get("action_type") for h in rows)
        out.extend([
            c.get("fold", 0) / total,
            c.get("call", 0) / total,
            c.get("check", 0) / total,
            c.get("raise", 0) / total,
            c.get("allin", 0) / total,
            _clip(len(rows) / 80.0),
        ])
    last = history[-1] if history else {}
    out.extend([
        1.0 if last.get("player_id") == opp_id else 0.0,
        1.0 if last.get("action_type") in {"raise", "allin"} else 0.0,
    ])
    street = max((int(h.get("round", 0)) for h in history), default=0)
    street_rows = [h for h in history if int(h.get("round", 0)) == street]
    out.extend([
        _clip(len(street_rows) / 8.0),
        _clip(sum(1 for h in street_rows if h.get("action_type") in {"raise", "allin"}) / 4.0),
    ])
    return out


def _showdowns(req: dict[str, Any]) -> list[float]:
    rows = req.get("opponent_showdowns") or []
    if not rows:
        return [0.0, 0.0, 0.0]
    strong = paired = suited = 0
    for row in rows[-20:]:
        cards = row.get("cards") or row.get("hand") or []
        if len(cards) < 2:
            continue
        r1, r2 = sorted((_rank(int(cards[0])), _rank(int(cards[1]))), reverse=True)
        strong += 1 if r1 >= 13 or (r1 >= 11 and r2 >= 10) else 0
        paired += 1 if r1 == r2 else 0
        suited += 1 if _suit(int(cards[0])) == _suit(int(cards[1])) else 0
    denom = max(1, min(len(rows), 20))
    return [_clip(len(rows) / 20.0), strong / denom, (paired + suited) / (2.0 * denom)]


def encode_features(req: dict[str, Any], display: dict[str, Any] | None = None) -> list[float]:
    display = display or {}
    cards = list(req.get("my_cards") or [])
    public_cards = list(req.get("public_cards") or [])
    my_id = int(req.get("my_id", 0) or 0)
    dealer_id = int(req.get("dealer_id", 0) or 0)
    ranks = sorted([_rank(c) for c in cards], reverse=True) + [2, 2]
    round_player_bet = display.get("round_player_bet") or []
    if len(round_player_bet) >= 2:
        my_bet = float(round_player_bet[my_id])
        opp_bet = float(round_player_bet[1 - my_id])
    else:
        my_bet = float(req.get("my_stage_bet", 0) or 0)
        opp_bet = float(req.get("opponent_stage_bet", 0) or 0)
    pot = float(req.get("pot", display.get("pot", 150)) or 0)
    to_call = float(req.get("to_call", max(0.0, opp_bet - my_bet)) or 0)
    my_chips = float(req.get("my_chips", INITIAL_CHIPS) or 0)
    total_win = req.get("total_win_chips") or [0, 0]
    score = float(total_win[my_id]) if len(total_win) > my_id else 0.0
    remaining = req.get("remaining_hands")
    if remaining is None:
        remaining = max(0, int(req.get("max_hand", 70)) - int(req.get("hand", 0)))
    stage = 3 if len(public_cards) >= 5 else 2 if len(public_cards) == 4 else 1 if len(public_cards) >= 3 else 0
    feats = [
        1.0 if stage == 0 else 0.0,
        1.0 if stage == 1 else 0.0,
        1.0 if stage == 2 else 0.0,
        1.0 if stage == 3 else 0.0,
        1.0 if my_id == dealer_id else 0.0,
        _clip((ranks[0] - 2) / 12.0),
        _clip((ranks[1] - 2) / 12.0),
        1.0 if len(cards) >= 2 and _suit(cards[0]) == _suit(cards[1]) else 0.0,
        1.0 if len(cards) >= 2 and ranks[0] == ranks[1] else 0.0,
        _preflop(cards),
        _clip(pot / INITIAL_CHIPS),
        _clip(my_chips / INITIAL_CHIPS),
        _clip(to_call / INITIAL_CHIPS),
        _clip(my_bet / INITIAL_CHIPS),
        _clip(opp_bet / INITIAL_CHIPS),
        _clip(to_call / max(1.0, pot + to_call)),
        _clip(my_chips / max(100.0, pot) / 50.0),
        _clip(float(remaining) / 70.0),
        _clip((score + 40000.0) / 80000.0),
        1.0 if req.get("opponent_allin") else 0.0,
    ]
    feats.extend(_board(public_cards))
    feats.extend(_history(list(req.get("history") or []), my_id))
    feats.extend(_showdowns(req))
    feats.append(1.0)
    return feats


def feature_dim() -> int:
    return len(encode_features({"my_cards": [0, 4], "public_cards": []}, {}))


def label_action(action: int, req: dict[str, Any], display: dict[str, Any] | None = None) -> int:
    if action == -1:
        return 0
    if action == -2:
        return 5
    if action == 0:
        return 1
    display = display or {}
    my_id = int(req.get("my_id", 0) or 0)
    rpb = display.get("round_player_bet") or []
    my_bet = float(rpb[my_id]) if len(rpb) >= 2 else float(req.get("my_stage_bet", 0) or 0)
    opp_bet = float(rpb[1 - my_id]) if len(rpb) >= 2 else float(req.get("opponent_stage_bet", 0) or 0)
    pot = float(req.get("pot", display.get("pot", 150)) or 1)
    to_call = float(req.get("to_call", max(0.0, opp_bet - my_bet)) or 0)
    ratio = max(0.0, float(action) - my_bet) / max(1.0, pot + to_call)
    return 2 if ratio <= 0.75 else 3 if ratio <= 1.5 else 4
