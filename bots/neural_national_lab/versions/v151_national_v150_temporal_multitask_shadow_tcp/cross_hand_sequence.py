"""Leakage-free public opponent summaries for cross-hand sequence models."""
from __future__ import annotations

import math
from typing import Any


CROSS_HAND_SEQUENCE_SCHEMA = "public_opponent_hand_v1"
CROSS_HAND_SEQUENCE_DIM = 16
MAX_CROSS_HANDS = 32
FEATURE_NAMES = (
    "opponent_action_count_norm",
    "opponent_fold_rate",
    "opponent_call_rate",
    "opponent_check_rate",
    "opponent_raise_rate",
    "opponent_allin_rate",
    "opponent_preflop_aggression",
    "opponent_postflop_aggression",
    "opponent_total_aggression",
    "opponent_mean_aggressive_pot_ratio",
    "opponent_max_aggressive_pot_ratio",
    "reached_flop",
    "reached_river",
    "showdown",
    "opponent_won",
    "opponent_earnings_norm",
)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _unit(value: Any) -> float:
    return max(0.0, min(1.0, _number(value)))


def normalize_cross_hand_sequence(
    raw: Any, *, max_hands: int = MAX_CROSS_HANDS
) -> list[list[float]]:
    """Validate a serialized sequence without padding or inventing observations."""
    if not isinstance(raw, list):
        return []
    rows: list[list[float]] = []
    for value in raw[-max(0, int(max_hands)):]:
        if not isinstance(value, (list, tuple)) or len(value) != CROSS_HAND_SEQUENCE_DIM:
            continue
        row = [_number(item) for item in value]
        row[:15] = [_unit(item) for item in row[:15]]
        row[15] = max(-1.0, min(1.0, row[15]))
        rows.append(row)
    return rows


def _summarize(
    actions: list[dict[str, Any]],
    *,
    reached_flop: bool,
    reached_river: bool,
    showdown: bool,
    opponent_earnings: float,
) -> list[float]:
    valid_actions = [
        row for row in actions
        if str(row.get("action", "")) in {"fold", "call", "check", "raise", "allin"}
    ]
    total = len(valid_actions)
    denominator = max(1, total)
    counts = {
        label: sum(str(row.get("action")) == label for row in valid_actions)
        for label in ("fold", "call", "check", "raise", "allin")
    }
    preflop = [row for row in valid_actions if str(row.get("stage")) == "preflop"]
    postflop = [row for row in valid_actions if str(row.get("stage")) != "preflop"]

    def aggression(rows: list[dict[str, Any]]) -> float:
        if not rows:
            return 0.0
        aggressive = sum(
            str(row.get("action")) in {"raise", "allin"} for row in rows
        )
        return aggressive / len(rows)

    aggressive_ratios = []
    for row in valid_actions:
        if str(row.get("action")) not in {"raise", "allin"}:
            continue
        amount = max(0.0, _number(row.get("amount")))
        pot = max(1.0, _number(row.get("pot"), 1.0))
        aggressive_ratios.append(min(4.0, amount / pot) / 4.0)
    opponent_earnings_norm = max(
        -1.0, min(1.0, _number(opponent_earnings) / 20000.0)
    )
    return [
        min(1.0, total / 12.0),
        counts["fold"] / denominator,
        counts["call"] / denominator,
        counts["check"] / denominator,
        counts["raise"] / denominator,
        counts["allin"] / denominator,
        aggression(preflop),
        aggression(postflop),
        (counts["raise"] + counts["allin"]) / denominator,
        sum(aggressive_ratios) / len(aggressive_ratios)
        if aggressive_ratios else 0.0,
        max(aggressive_ratios) if aggressive_ratios else 0.0,
        float(bool(reached_flop)),
        float(bool(reached_river)),
        float(bool(showdown)),
        float(opponent_earnings > 0.0),
        opponent_earnings_norm,
    ]


def summarize_server_hand(
    events: list[dict[str, Any]],
    settlement: dict[str, Any],
    *,
    opponent_player_idx: int,
) -> list[float]:
    """Summarize one completed hand from authoritative local-server events."""
    hand = int(settlement.get("hand", 0) or 0)
    actions = []
    stages = set()
    for event in events:
        if int(event.get("hand", 0) or 0) != hand:
            continue
        if event.get("type") == "stage":
            stages.add(str(event.get("stage", "")))
        if event.get("type") != "action":
            continue
        if int(event.get("player_idx", -1)) != int(opponent_player_idx):
            continue
        action = str(event.get("action", ""))
        if action not in {"fold", "call", "check", "raise", "allin"}:
            continue
        actions.append({
            "action": action,
            "stage": str(event.get("stage", "")),
            "amount": event.get("amount", 0),
            "pot": event.get("pot", settlement.get("pot", 1)),
        })
    earnings = settlement.get("earnings") or []
    opponent_earnings = (
        _number(earnings[opponent_player_idx])
        if isinstance(earnings, list) and len(earnings) > opponent_player_idx
        else 0.0
    )
    return _summarize(
        actions,
        reached_flop=bool({"flop", "turn", "river"} & stages),
        reached_river="river" in stages,
        showdown=bool(settlement.get("is_showdown", False)),
        opponent_earnings=opponent_earnings,
    )


def server_sequences_by_hand(
    events: list[dict[str, Any]],
    *,
    opponent_player_idx: int,
    max_hands: int = MAX_CROSS_HANDS,
) -> dict[int, list[list[float]]]:
    """Map each current hand to summaries from strictly earlier hands."""
    settlements = sorted(
        (
            event for event in events
            if event.get("type") == "settle" and int(event.get("hand", 0) or 0) > 0
        ),
        key=lambda event: int(event.get("hand", 0) or 0),
    )
    result: dict[int, list[list[float]]] = {}
    prior: list[list[float]] = []
    for settlement in settlements:
        hand = int(settlement["hand"])
        result[hand] = [list(row) for row in prior[-max_hands:]]
        prior.append(
            summarize_server_hand(
                events, settlement, opponent_player_idx=opponent_player_idx
            )
        )
    if settlements:
        result[int(settlements[-1]["hand"]) + 1] = [
            list(row) for row in prior[-max_hands:]
        ]
    return result


def summarize_native_hand(
    history: list[dict[str, Any]],
    public_cards: list[Any],
    *,
    opponent_id: int,
    hero_earned: float,
    final_pot: float,
    showdown: bool,
) -> list[float]:
    """Build the same summary from information available inside a native bot."""
    actions = []
    for entry in history:
        if int(entry.get("player_id", -1)) != int(opponent_id):
            continue
        action = str(entry.get("action_type", ""))
        if action not in {"fold", "call", "check", "raise", "allin"}:
            continue
        if action == "raise":
            amount = entry.get("stage_bet", entry.get("action", 0))
        elif action == "allin":
            amount = entry.get("committed", 0)
        else:
            amount = entry.get("committed", 0)
        actions.append({
            "action": action,
            "stage": str(entry.get("stage") or {
                0: "preflop", 1: "flop", 2: "turn", 3: "river"
            }.get(int(entry.get("round", 0) or 0), "preflop")),
            "amount": amount,
            "pot": entry.get("pot_after", final_pot),
        })
    return _summarize(
        actions,
        reached_flop=len(public_cards) >= 3,
        reached_river=len(public_cards) >= 5,
        showdown=showdown,
        opponent_earnings=-_number(hero_earned),
    )
