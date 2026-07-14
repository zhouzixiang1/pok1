"""Strict board-legal reach ranges and blocker-conditioned masks."""

from __future__ import annotations

import math
from collections.abc import Iterable

from .combo_index import COMBOS, COMBO_COUNT, board_legal_mask


def validate_reach_range(
    values: tuple[float, ...],
    board_card_ids: Iterable[int],
) -> tuple[float, ...]:
    if type(values) is not tuple or len(values) != COMBO_COUNT:
        raise TypeError("reach range must be an immutable 1,326-vector")
    result: list[float] = []
    for value in values:
        if type(value) not in (int, float):
            raise TypeError("reach weights must be exact JSON numbers")
        numeric = float(value)
        if not math.isfinite(numeric) or numeric < 0.0:
            raise ValueError("reach weights must be finite and nonnegative")
        if numeric == 0.0:
            numeric = 0.0
        result.append(numeric)
    legal = board_legal_mask(board_card_ids)
    if any(not legal[index] and result[index] != 0.0 for index in range(COMBO_COUNT)):
        raise ValueError("board-blocked reach entries must be canonical zero")
    total = math.fsum(result)
    if not math.isclose(total, 1.0, rel_tol=1e-12, abs_tol=1e-12):
        raise ValueError(f"board-legal reach range sums to {total!r}, expected one")
    return tuple(result)


def uniform_reach_range(
    board_card_ids: Iterable[int],
    *,
    support_indices: Iterable[int] | None = None,
) -> tuple[float, ...]:
    legal = board_legal_mask(board_card_ids)
    if support_indices is None:
        support = tuple(index for index, enabled in enumerate(legal) if enabled)
    else:
        raw = tuple(support_indices)
        if any(type(index) is not int or not 0 <= index < COMBO_COUNT for index in raw):
            raise ValueError("reach support index is outside [0, 1325]")
        support = tuple(sorted(set(raw)))
        if len(support) != len(raw):
            raise ValueError("reach support indices must be unique")
        if any(not legal[index] for index in support):
            raise ValueError("reach support contains a board-blocked combo")
    if not support:
        raise ValueError("reach support cannot be empty")
    probability = 1.0 / len(support)
    values = [0.0] * COMBO_COUNT
    for index in support:
        values[index] = probability
    return validate_reach_range(tuple(values), board_card_ids)


def compatible_opponent_mass(
    opponent_range: tuple[float, ...],
    hero_index: int,
) -> float:
    if type(hero_index) is not int or not 0 <= hero_index < COMBO_COUNT:
        raise ValueError("hero combo index is outside [0, 1325]")
    first, second = COMBOS[hero_index]
    mass = math.fsum(
        weight
        for opponent_index, weight in enumerate(opponent_range)
        if weight > 0.0
        and first not in COMBOS[opponent_index]
        and second not in COMBOS[opponent_index]
    )
    if mass < 0.0 or mass > 1.0 + 1e-12:
        raise ValueError("compatible opponent mass is outside probability bounds")
    return min(1.0, mass)


def cfv_valid_mask(
    board_card_ids: Iterable[int],
    opponent_range: tuple[float, ...],
) -> tuple[bool, ...]:
    legal = board_legal_mask(board_card_ids)
    support = tuple(
        COMBOS[index] for index, weight in enumerate(opponent_range) if weight > 0.0
    )
    return tuple(
        enabled
        and any(
            COMBOS[index][0] not in opponent
            and COMBOS[index][1] not in opponent
            for opponent in support
        )
        for index, enabled in enumerate(legal)
    )
