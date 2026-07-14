"""Exact blocker-aware reduction from pair utilities to range CFVs."""

from __future__ import annotations

import math
from collections.abc import Callable

from .combo_index import COMBOS, COMBO_COUNT, board_legal_mask
from .semantics import RangeCFVQuery, RangeCFVResult


PairUtility = Callable[[int, int], float]


def _compatible(first_index: int, second_index: int) -> bool:
    first = COMBOS[first_index]
    second = COMBOS[second_index]
    return (
        first[0] != second[0]
        and first[0] != second[1]
        and first[1] != second[0]
        and first[1] != second[1]
    )


def exact_pairwise_cfv(
    query: RangeCFVQuery,
    utility0: PairUtility,
    *,
    provider_id: str,
) -> RangeCFVResult:
    """Compute both counterfactual vectors without multiplying own reach."""

    if type(query) is not RangeCFVQuery or not callable(utility0):
        raise TypeError("pairwise CFV requires exact query and callable utility")
    board_legal = board_legal_mask(query.public_state.board_card_ids)
    support0 = tuple(
        index for index, weight in enumerate(query.private_ranges[0]) if weight > 0.0
    )
    support1 = tuple(
        index for index, weight in enumerate(query.private_ranges[1]) if weight > 0.0
    )
    cache: dict[tuple[int, int], float] = {}

    def pair_value(first_index: int, second_index: int) -> float:
        key = (first_index, second_index)
        if key not in cache:
            value = utility0(first_index, second_index)
            if type(value) not in (int, float) or not math.isfinite(float(value)):
                raise ValueError("pair utility must return a finite JSON number")
            cache[key] = float(value)
        return cache[key]

    values0 = [0.0] * COMBO_COUNT
    values1 = [0.0] * COMBO_COUNT
    for first_index in range(COMBO_COUNT):
        if not board_legal[first_index]:
            continue
        terms = [
            query.private_ranges[1][second_index]
            * pair_value(first_index, second_index)
            for second_index in support1
            if _compatible(first_index, second_index)
        ]
        if terms:
            values0[first_index] = math.fsum(terms)
    for second_index in range(COMBO_COUNT):
        if not board_legal[second_index]:
            continue
        terms = [
            -query.private_ranges[0][first_index]
            * pair_value(first_index, second_index)
            for first_index in support0
            if _compatible(first_index, second_index)
        ]
        if terms:
            values1[second_index] = math.fsum(terms)
    return RangeCFVResult.create(
        query,
        provider_id=provider_id,
        raw_values=(tuple(values0), tuple(values1)),
        enforce_exact_zero_sum=True,
    )
