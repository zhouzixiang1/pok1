"""Versioned public decision context missing from the legacy state vector."""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any


DECISION_CONTEXT_SCHEMA = "public_decision_context_v1"
INITIAL_CHIPS = 20_000.0
TOTAL_HANDS = 70
MAX_POT = 2.0 * INITIAL_CHIPS
MAX_MATCH_SCORE = TOTAL_HANDS * INITIAL_CHIPS

ACTION_LABELS = (
    "fold",
    "call",
    "raise_half",
    "raise_pot",
    "raise_2pot",
    "allin",
)
DECISION_CONTEXT_FIELDS = (
    "opponent_stack_fraction",
    "effective_stack_fraction",
    "min_raise_action_fraction",
    "allin_call_amount_fraction",
    "to_call_over_effective_stack",
    "pot_fraction",
    "remaining_hands_fraction",
    "match_score_fraction",
    "score_over_remaining_swing",
    *(f"legal_{label}" for label in ACTION_LABELS),
)
DECISION_CONTEXT_FEATURE_NAMES = DECISION_CONTEXT_FIELDS
DECISION_CONTEXT_DIM = len(DECISION_CONTEXT_FIELDS)
DECISION_CONTEXT_FEATURE_INDEX = {
    name: index for index, name in enumerate(DECISION_CONTEXT_FIELDS)
}
DECISION_CONTEXT_FEATURE_BOUNDS = tuple(
    (0.0, 1.0) for _ in DECISION_CONTEXT_FIELDS
)
LEGAL_MASK_SLICE = slice(
    DECISION_CONTEXT_DIM - len(ACTION_LABELS), DECISION_CONTEXT_DIM
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _field(mapping: Mapping[str, Any], key: str) -> float | None:
    return _number(mapping[key]) if key in mapping else None


def _first_number(*values: Any) -> float | None:
    for value in values:
        number = _number(value)
        if number is not None:
            return number
    return None


def _indexed_number(value: Any, index: int) -> float | None:
    if isinstance(value, Mapping):
        if index in value:
            return _number(value[index])
        return _number(value.get(str(index)))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if 0 <= index < len(value):
            return _number(value[index])
    return None


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return low if value < low else high if value > high else value


def _amount_fraction(value: float | None, scale: float) -> float:
    if value is None or scale <= 0.0:
        return 0.0
    return _clip(value / scale)


def _centered_fraction(value: float | None, scale: float) -> float:
    """Map a signed value in ``[-scale, scale]`` to ``[0, 1]``."""
    if value is None or scale <= 0.0:
        return 0.5
    return _clip(0.5 + 0.5 * value / scale)


def _seat(request: Mapping[str, Any]) -> int:
    raw = _field(request, "my_id")
    if raw is None:
        return 0
    seat = int(raw)
    return seat if seat in (0, 1) else 0


def _stack(
    request: Mapping[str, Any],
    state: Mapping[str, Any],
    *,
    seat: int,
    opponent: bool,
) -> float | None:
    target = 1 - seat if opponent else seat
    direct_key = "opponent_chips" if opponent else "my_chips"
    state_key = "opponent_stack" if opponent else "my_stack"
    return _first_number(
        request.get(direct_key),
        state.get(direct_key),
        state.get(state_key),
        _indexed_number(state.get("stacks"), target),
    )


def _remaining_hands(request: Mapping[str, Any]) -> float:
    for key in (
        "remaining_hands",
        "remain_hands",
        "hands_left",
        "left_hands",
    ):
        value = _field(request, key)
        if value is not None and value >= 0.0:
            return _clip(value, 0.0, float(TOTAL_HANDS))

    hand = _field(request, "hand")
    max_hand = _field(request, "max_hand")
    if hand is not None and max_hand is not None:
        return _clip(max_hand - hand, 0.0, float(TOTAL_HANDS))
    return float(TOTAL_HANDS)


def _match_score(
    request: Mapping[str, Any], state: Mapping[str, Any], seat: int
) -> float:
    score = _indexed_number(request.get("total_win_chips"), seat)
    if score is None:
        score = _indexed_number(state.get("total_win_chips"), seat)
    fallback = _first_number(
        request.get("match_score"),
        request.get("score"),
        state.get("match_score"),
        state.get("score"),
    )
    return score if score is not None else fallback if fallback is not None else 0.0


def _mask_values(
    legal_mask: Any,
    request: Mapping[str, Any],
    state: Mapping[str, Any],
) -> list[float]:
    raw = legal_mask
    if raw is None:
        raw = state.get("legal_mask", request.get("legal_mask"))

    if isinstance(raw, Mapping):
        values = [raw.get(label, 0.0) for label in ACTION_LABELS]
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        values = list(raw[: len(ACTION_LABELS)])
    else:
        values = []

    values.extend([0.0] * (len(ACTION_LABELS) - len(values)))
    encoded = []
    for value in values[: len(ACTION_LABELS)]:
        number = _number(value)
        encoded.append(1.0 if number is not None and number > 0.0 else 0.0)
    return encoded


def encode_decision_context(
    request: Mapping[str, Any] | None,
    state: Mapping[str, Any] | None = None,
    legal_mask: Any = None,
) -> list[float]:
    """Encode only public information available at the current decision.

    The score scale covers the full 70-hand match swing instead of the legacy
    +/-40k window. Missing numeric fields use zero-valued amount features and
    neutral centered score features.
    """
    request_map = _mapping(request)
    state_map = _mapping(state)
    seat = _seat(request_map)

    my_stack = _stack(request_map, state_map, seat=seat, opponent=False)
    opponent_stack = _stack(request_map, state_map, seat=seat, opponent=True)
    to_call = _first_number(state_map.get("to_call"), request_map.get("to_call"))
    min_raise_action = _first_number(
        state_map.get("min_raise_action"), request_map.get("min_raise_action")
    )
    allin_call_amount = _first_number(
        state_map.get("allin_call_amount"), request_map.get("allin_call_amount")
    )
    pot = _first_number(state_map.get("pot"), request_map.get("pot"))

    explicit_effective = _first_number(
        state_map.get("effective_stack"), request_map.get("effective_stack")
    )
    if explicit_effective is not None:
        effective_stack = max(0.0, explicit_effective)
    elif my_stack is not None and opponent_stack is not None:
        effective_stack = max(0.0, min(my_stack, opponent_stack))
    else:
        effective_stack = 0.0

    safe_to_call = max(0.0, to_call if to_call is not None else 0.0)
    if effective_stack > 0.0:
        to_call_over_effective = _clip(safe_to_call / effective_stack)
    else:
        to_call_over_effective = 1.0 if safe_to_call > 0.0 else 0.0

    remaining_hands = _remaining_hands(request_map)
    score = _match_score(request_map, state_map, seat)
    remaining_swing = remaining_hands * INITIAL_CHIPS
    if remaining_swing > 0.0:
        score_over_remaining = _centered_fraction(score, remaining_swing)
    else:
        score_over_remaining = 0.5 if score == 0.0 else float(score > 0.0)

    features = [
        _amount_fraction(opponent_stack, INITIAL_CHIPS),
        _amount_fraction(effective_stack, INITIAL_CHIPS),
        _amount_fraction(min_raise_action, INITIAL_CHIPS),
        _amount_fraction(allin_call_amount, INITIAL_CHIPS),
        to_call_over_effective,
        _amount_fraction(pot, MAX_POT),
        _amount_fraction(remaining_hands, float(TOTAL_HANDS)),
        _centered_fraction(score, MAX_MATCH_SCORE),
        score_over_remaining,
        *_mask_values(legal_mask, request_map, state_map),
    ]
    if len(features) != DECISION_CONTEXT_DIM:
        raise RuntimeError("unexpected decision-context feature dimension")
    return features


def decision_context_metadata() -> dict[str, Any]:
    return {
        "schema": DECISION_CONTEXT_SCHEMA,
        "dim": DECISION_CONTEXT_DIM,
        "dimension": DECISION_CONTEXT_DIM,
        "fields": list(DECISION_CONTEXT_FIELDS),
        "feature_names": list(DECISION_CONTEXT_FEATURE_NAMES),
        "feature_bounds": [list(bounds) for bounds in DECISION_CONTEXT_FEATURE_BOUNDS],
        "public_only": True,
        "private_feature_indices": [],
    }


schema_metadata = decision_context_metadata
encode_public_decision_context = encode_decision_context
