"""Versioned, bounded features for current-hand action-history sequences.

The actor-aware schema is intentionally independent from the existing trainer
and runtime.  It can be adopted by both sides in a later, explicit model-format
change without silently changing legacy 15-dimensional inputs.
"""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any


STARTING_STACK_CHIPS = 20_000.0
TABLE_CHIP_CAP = 2.0 * STARTING_STACK_CHIPS

LEGACY_HISTORY_SCHEMA = "legacy15_v1"
ACTOR_AWARE_HISTORY_SCHEMA = "current_hand_actor_event_v2"
CURRENT_HAND_HISTORY_SCHEMA = ACTOR_AWARE_HISTORY_SCHEMA
HISTORY_FEATURE_SCHEMAS = (
    LEGACY_HISTORY_SCHEMA,
    ACTOR_AWARE_HISTORY_SCHEMA,
)

LEGACY_HISTORY_FEATURE_NAMES = (
    "street_preflop",
    "street_flop",
    "street_turn",
    "street_river",
    "action_fold",
    "action_call",
    "action_check",
    "action_raise",
    "action_allin",
    "stage_bet_norm",
    "action_amount_norm",
    "committed_norm",
    "decision_pot_norm",
    "is_raise",
    "is_allin",
)
LEGACY_HISTORY_FEATURE_DIM = len(LEGACY_HISTORY_FEATURE_NAMES)

ACTOR_CATEGORIES = ("hero", "opponent", "unknown")
STREET_CATEGORIES = ("preflop", "flop", "turn", "river", "unknown")
ACTION_CATEGORIES = ("fold", "call", "check", "raise", "allin", "unknown")
NUMERIC_FEATURE_NAMES = (
    "action_amount_norm",
    "stage_bet_norm",
    "committed_norm",
    "pot_after_norm",
    "chips_after_norm",
    "effective_stack_norm",
    "wager_to_pot_after",
    "wager_to_effective_stack_before",
    "pot_after_known",
    "stack_after_known",
)

ACTOR_AWARE_HISTORY_FEATURE_NAMES = (
    tuple(f"actor_{name}" for name in ACTOR_CATEGORIES)
    + tuple(f"street_{name}" for name in STREET_CATEGORIES)
    + tuple(f"action_{name}" for name in ACTION_CATEGORIES)
    + NUMERIC_FEATURE_NAMES
)
ACTOR_AWARE_HISTORY_FEATURE_DIM = len(ACTOR_AWARE_HISTORY_FEATURE_NAMES)

# Default aliases are the schema a new model should declare and consume.
HISTORY_FEATURE_SCHEMA = ACTOR_AWARE_HISTORY_SCHEMA
HISTORY_FEATURE_NAMES = ACTOR_AWARE_HISTORY_FEATURE_NAMES
HISTORY_FEATURE_DIM = ACTOR_AWARE_HISTORY_FEATURE_DIM
HISTORY_FEATURE_BOUNDS = tuple((0.0, 1.0) for _ in HISTORY_FEATURE_NAMES)
HISTORY_FEATURE_INDEX = {
    name: index for index, name in enumerate(HISTORY_FEATURE_NAMES)
}

ACTOR_FEATURE_SLICE = slice(0, len(ACTOR_CATEGORIES))
STREET_FEATURE_SLICE = slice(
    ACTOR_FEATURE_SLICE.stop,
    ACTOR_FEATURE_SLICE.stop + len(STREET_CATEGORIES),
)
ACTION_FEATURE_SLICE = slice(
    STREET_FEATURE_SLICE.stop,
    STREET_FEATURE_SLICE.stop + len(ACTION_CATEGORIES),
)


def _clip01(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


def _number(
    mapping: Mapping[str, Any], *keys: str
) -> tuple[float, bool]:
    """Return a finite, non-negative number and whether a value was usable."""
    for key in keys:
        if key not in mapping:
            continue
        raw = mapping.get(key)
        if isinstance(raw, bool):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(value):
            return max(0.0, value), True
    return 0.0, False


def _normalize(value: float, scale: float) -> float:
    return _clip01(value / scale) if scale > 0.0 else 0.0


def _player_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)
        player_id = int(numeric)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(numeric) or numeric != player_id:
        return None
    return player_id if player_id in (0, 1) else None


def _actor_name(event: Mapping[str, Any], my_id: Any) -> str:
    role = str(event.get("actor_role", "")).strip().lower()
    if role in ACTOR_CATEGORIES:
        return role

    hero_id = _player_id(my_id)
    actor_id = None
    for key in ("player_id", "player_idx", "actor_id", "actor"):
        if key in event:
            actor_id = _player_id(event.get(key))
            if actor_id is not None:
                break
    if hero_id is None or actor_id is None:
        return "unknown"
    return "hero" if actor_id == hero_id else "opponent"


def _street_name(event: Mapping[str, Any]) -> str:
    raw_stage = event.get("stage", event.get("street"))
    if isinstance(raw_stage, str):
        stage = raw_stage.strip().lower().replace("-", "").replace("_", "")
        aliases = {
            "preflop": "preflop",
            "flop": "flop",
            "turn": "turn",
            "river": "river",
        }
        if stage in aliases:
            return aliases[stage]

    if "round" in event:
        try:
            round_index = int(event.get("round"))
        except (TypeError, ValueError, OverflowError):
            round_index = -1
        if round_index in range(4):
            return STREET_CATEGORIES[round_index]

    if "public_cards" in event:
        cards = event.get("public_cards")
        if isinstance(cards, Sequence) and not isinstance(cards, (str, bytes)):
            card_count = len(cards)
            if card_count == 0:
                return "preflop"
            if card_count == 3:
                return "flop"
            if card_count == 4:
                return "turn"
            if card_count >= 5:
                return "river"
    return "unknown"


def _action_name(event: Mapping[str, Any]) -> str:
    raw_action = event.get("action_type", event.get("action_name"))
    if not isinstance(raw_action, str) and isinstance(event.get("action"), str):
        raw_action = event.get("action")
    if isinstance(raw_action, str):
        action = raw_action.strip().lower().replace("-", "").replace("_", "")
        if action in {"fold", "call", "check", "raise", "allin"}:
            return action

    raw_numeric = event.get("action")
    if isinstance(raw_numeric, bool):
        return "unknown"
    try:
        numeric = float(raw_numeric)
    except (TypeError, ValueError, OverflowError):
        return "unknown"
    if not math.isfinite(numeric):
        return "unknown"
    if numeric == -1.0:
        return "fold"
    if numeric == -2.0:
        return "allin"
    if numeric > 0.0:
        return "raise"
    return "unknown"


def _one_hot(value: str, categories: tuple[str, ...]) -> list[float]:
    return [1.0 if value == category else 0.0 for category in categories]


def _effective_stack(event: Mapping[str, Any]) -> tuple[float, bool]:
    effective, known = _number(
        event,
        "effective_stack_after",
        "effective_stack",
        "effective_chips_after",
    )
    if known:
        return effective, True

    raw_stacks = event.get("stacks_after")
    if isinstance(raw_stacks, Mapping):
        candidates = raw_stacks.values()
    elif isinstance(raw_stacks, Sequence) and not isinstance(
        raw_stacks, (str, bytes)
    ):
        candidates = raw_stacks
    else:
        candidates = ()
    stacks = []
    for raw in candidates:
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(value) and value >= 0.0:
            stacks.append(value)
    return (min(stacks), True) if stacks else (0.0, False)


def encode_history_event(
    event: Mapping[str, Any] | None,
    my_id: Any,
) -> list[float]:
    """Encode one event with actor-relative and event-local state features."""
    if not isinstance(event, Mapping):
        event = {}
    action_name = _action_name(event)

    action_amount, action_amount_known = _number(event, "amount")
    if not action_amount_known and action_name in {"raise", "allin"}:
        action_amount, action_amount_known = _number(event, "action")

    stage_bet, stage_bet_known = _number(event, "stage_bet")
    if not stage_bet_known and action_name in {"raise", "allin"}:
        stage_bet = action_amount

    committed, committed_known = _number(event, "committed")
    if not committed_known and action_amount_known:
        committed = action_amount

    # Only event-local pot fields are consulted.  The request's current pot is
    # deliberately unavailable here, so earlier events cannot inherit it.
    pot_after, pot_after_known = _number(event, "pot_after", "pot")
    chips_after, chips_after_known = _number(
        event, "chips_after", "stack_after", "actor_chips_after"
    )
    effective_stack, effective_stack_known = _effective_stack(event)
    if not effective_stack_known and chips_after_known:
        effective_stack = chips_after
        effective_stack_known = True

    wager = committed if committed_known else action_amount
    wager_to_pot = (
        _clip01(wager / pot_after)
        if pot_after_known and pot_after > 0.0
        else 0.0
    )
    wager_to_stack = (
        _clip01(wager / (wager + effective_stack))
        if effective_stack_known and wager + effective_stack > 0.0
        else 0.0
    )
    numeric = [
        _normalize(action_amount, STARTING_STACK_CHIPS),
        _normalize(stage_bet, STARTING_STACK_CHIPS),
        _normalize(committed, STARTING_STACK_CHIPS),
        _normalize(pot_after, TABLE_CHIP_CAP),
        _normalize(chips_after, STARTING_STACK_CHIPS),
        _normalize(effective_stack, STARTING_STACK_CHIPS),
        wager_to_pot,
        wager_to_stack,
        float(pot_after_known),
        float(chips_after_known or effective_stack_known),
    ]
    features = (
        _one_hot(_actor_name(event, my_id), ACTOR_CATEGORIES)
        + _one_hot(_street_name(event), STREET_CATEGORIES)
        + _one_hot(action_name, ACTION_CATEGORIES)
        + numeric
    )
    if len(features) != ACTOR_AWARE_HISTORY_FEATURE_DIM:
        raise RuntimeError("unexpected actor-aware history feature dimension")
    return [_clip01(value) for value in features]


def _legacy_history_event(
    event: Mapping[str, Any], decision_pot: float
) -> list[float]:
    """Reproduce the actor-blind legacy shape for explicit compatibility."""
    street = _street_name(event)
    action = _action_name(event)
    stage_bet, _ = _number(event, "stage_bet")
    action_amount, _ = _number(event, "action")
    committed, _ = _number(event, "committed")
    return (
        _one_hot(street, STREET_CATEGORIES[:4])
        + _one_hot(action, ACTION_CATEGORIES[:5])
        + [
            _normalize(stage_bet, STARTING_STACK_CHIPS),
            _normalize(action_amount, STARTING_STACK_CHIPS),
            _normalize(committed, STARTING_STACK_CHIPS),
            _normalize(decision_pot, STARTING_STACK_CHIPS),
            float(action == "raise"),
            float(action == "allin"),
        ]
    )


def _history_source(
    request_or_history: Mapping[str, Any] | Sequence[Any] | None,
) -> tuple[list[Any], float]:
    if isinstance(request_or_history, Mapping):
        raw_history = request_or_history.get("history")
        decision_pot, _ = _number(request_or_history, "pot")
    else:
        raw_history = request_or_history
        decision_pot = 0.0
    if not isinstance(raw_history, Sequence) or isinstance(
        raw_history, (str, bytes)
    ):
        return [], decision_pot
    return list(raw_history), decision_pot


def encode_history_sequence(
    request_or_history: Mapping[str, Any] | Sequence[Any] | None,
    my_id: Any,
    *,
    schema: str = CURRENT_HAND_HISTORY_SCHEMA,
) -> list[list[float]]:
    """Encode a request's history or a raw history list without mutating it."""
    history, decision_pot = _history_source(request_or_history)
    if schema == ACTOR_AWARE_HISTORY_SCHEMA:
        return [encode_history_event(event, my_id) for event in history]
    if schema == LEGACY_HISTORY_SCHEMA:
        return [
            _legacy_history_event(
                event if isinstance(event, Mapping) else {}, decision_pot
            )
            for event in history
        ]
    raise ValueError(f"unsupported history feature schema: {schema}")


def history_feature_metadata(
    *, schema: str = CURRENT_HAND_HISTORY_SCHEMA
) -> dict[str, Any]:
    """Return a serializable declaration suitable for model metadata."""
    if schema == LEGACY_HISTORY_SCHEMA:
        names = LEGACY_HISTORY_FEATURE_NAMES
    elif schema == ACTOR_AWARE_HISTORY_SCHEMA:
        names = ACTOR_AWARE_HISTORY_FEATURE_NAMES
    else:
        raise ValueError(f"unsupported history feature schema: {schema}")
    return {
        "schema": schema,
        "history_feature_dim": len(names),
        "feature_names": list(names),
        "feature_bounds": [[0.0, 1.0] for _ in names],
        "actor_relative_to_my_id": schema == ACTOR_AWARE_HISTORY_SCHEMA,
        "uses_event_pot_after": schema == ACTOR_AWARE_HISTORY_SCHEMA,
    }


encode_current_hand_history = encode_history_sequence
