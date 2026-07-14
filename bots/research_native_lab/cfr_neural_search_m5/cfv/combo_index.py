"""The versioned physical 1,326-combination index used by every M5 tensor."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from bots.research_native_lab.common_contracts.cards import all_hole_combinations


CARD_COUNT = 52
COMBOS = tuple(
    (first, second)
    for first in range(CARD_COUNT)
    for second in range(first + 1, CARD_COUNT)
)
COMBO_COUNT = 1326
COMBO_TO_INDEX = {combo: index for index, combo in enumerate(COMBOS)}
COMBO_REGISTRY_SHA256 = hashlib.sha256(
    json.dumps(
        {"combos": COMBOS},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
).hexdigest()
EXPECTED_COMBO_REGISTRY_SHA256 = (
    "4534e13c4bd7a32ebb621433f5b08344b2bb81a04f3c78c2840cd9362bddf89a"
)

if len(COMBOS) != COMBO_COUNT or len(COMBO_TO_INDEX) != COMBO_COUNT:
    raise AssertionError("physical combo index must contain exactly 1,326 pairs")
if COMBOS != all_hole_combinations():
    raise AssertionError("M5 physical combo order differs from the Common registry")
if COMBO_REGISTRY_SHA256 != EXPECTED_COMBO_REGISTRY_SHA256:
    raise AssertionError("M5/Common physical combo registry digest changed")


def canonical_board(board_card_ids: Iterable[int]) -> tuple[int, ...]:
    raw = tuple(board_card_ids)
    if any(type(card) is not int or not 0 <= card < CARD_COUNT for card in raw):
        raise ValueError("board cards must be exact integers in [0, 51]")
    if len(raw) not in (0, 3, 4, 5) or len(set(raw)) != len(raw):
        raise ValueError("board must contain 0, 3, 4, or 5 unique cards")
    return raw


def board_legal_mask(board_card_ids: Iterable[int]) -> tuple[bool, ...]:
    board = frozenset(canonical_board(board_card_ids))
    return tuple(first not in board and second not in board for first, second in COMBOS)


def compatible(first_index: int, second_index: int) -> bool:
    if (
        type(first_index) is not int
        or type(second_index) is not int
        or not 0 <= first_index < COMBO_COUNT
        or not 0 <= second_index < COMBO_COUNT
    ):
        raise ValueError("combo indices must be exact integers in [0, 1325]")
    first = COMBOS[first_index]
    second = COMBOS[second_index]
    return not ({first[0], first[1]} & {second[0], second[1]})
