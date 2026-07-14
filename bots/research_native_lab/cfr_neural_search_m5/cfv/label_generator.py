"""Generate CFV training labels for the RangeCFVNet.

Produces (PublicHUNLState, range_p0, range_p1, target_cfv_p0, target_cfv_p1)
tuples using exact oracle evaluators for terminal states and blueprint lookup
for non-terminal states.
"""

from __future__ import annotations

import itertools
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from bots.research_native_lab.common_contracts import NationalGameState
from bots.research_native_lab.common_contracts.actions import Action, ActionKind

from .combo_index import COMBO_COUNT, COMBO_TO_INDEX, board_legal_mask
from .hunl_micro_oracle import (
    exact_fold_cfv,
    exact_showdown_cfv,
    exact_river_call_or_fold_cfv,
    exact_turn_allin_runout_cfv,
)
from .public_state import PublicHUNLState
from .ranges import uniform_reach_range
from .semantics import RangeCFVQuery

_BOARDS = [
    (0, 4, 8, 12, 16),
    (1, 5, 9, 13, 17),
    (2, 6, 10, 14, 18),
    (3, 7, 11, 15, 19),
    (20, 24, 28, 32, 36),
    (0, 5, 10, 15, 20),
    (1, 6, 11, 16, 21),
    (2, 7, 12, 17, 22),
    (3, 8, 13, 18, 23),
    (4, 9, 14, 19, 24),
    (0, 1, 2, 3, 4),
    (5, 6, 7, 8, 9),
    (10, 11, 12, 13, 14),
    (15, 16, 17, 18, 19),
    (20, 21, 22, 23, 24),
]

def _safe_hands(board):
    """Pick two non-conflicting hole card pairs (4 distinct cards not on board)."""
    used = set(board)
    available = [c for c in range(52) if c not in used]
    # Take pairs from non-overlapping segments
    pairs = []
    i = 0
    while i + 1 < len(available) and len(pairs) < 2:
        pairs.append((available[i], available[i + 1]))
        i += 2
    return pairs


@dataclass(frozen=True, slots=True)
class CFVLabel:
    public_state: PublicHUNLState
    range_p0: tuple[float, ...]
    range_p1: tuple[float, ...]
    target_cfv_p0: tuple[float, ...]
    target_cfv_p1: tuple[float, ...]
    label_source: str


def _make_fold_state(
    board: tuple[int, ...],
    hands: tuple[tuple[int, int], tuple[int, int]],
    sb: int,
) -> NationalGameState:
    state = NationalGameState.new_hand(1, small_blind=sb, hole_cards=hands)
    return state.apply_action(Action(ActionKind.FOLD))


def _make_showdown_state(
    board: tuple[int, ...],
    hands: tuple[tuple[int, int], tuple[int, int]],
    sb: int,
) -> NationalGameState:
    state = NationalGameState.new_hand(1, small_blind=sb, hole_cards=hands)
    state = state.apply_action(Action(ActionKind.CALL))
    state = state.apply_action(Action(ActionKind.CHECK))
    state = state.apply_chance(board[:3])
    state = state.apply_action(Action(ActionKind.CHECK))
    state = state.apply_action(Action(ActionKind.CALL))
    state = state.apply_chance(board[3:4])
    state = state.apply_action(Action(ActionKind.CHECK))
    state = state.apply_action(Action(ActionKind.CALL))
    state = state.apply_chance(board[4:5])
    state = state.apply_action(Action(ActionKind.CHECK))
    state = state.apply_action(Action(ActionKind.CALL))
    return state


def _make_river_bet_state(
    board: tuple[int, ...],
    hands: tuple[tuple[int, int], tuple[int, int]],
    sb: int,
) -> NationalGameState:
    state = NationalGameState.new_hand(1, small_blind=sb, hole_cards=hands)
    state = state.apply_action(Action(ActionKind.CALL))
    state = state.apply_action(Action(ActionKind.CHECK))
    state = state.apply_chance(board[:3])
    state = state.apply_action(Action(ActionKind.CHECK))
    state = state.apply_action(Action(ActionKind.CALL))
    state = state.apply_chance(board[3:4])
    state = state.apply_action(Action(ActionKind.CHECK))
    state = state.apply_action(Action(ActionKind.CALL))
    state = state.apply_chance(board[4:5])
    state = state.apply_action(Action(ActionKind.RAISE, amount=200))
    return state


def _make_turn_allin_state(
    board: tuple[int, ...],
    hands: tuple[tuple[int, int], tuple[int, int]],
    sb: int,
) -> NationalGameState:
    state = NationalGameState.new_hand(1, small_blind=sb, hole_cards=hands)
    state = state.apply_action(Action(ActionKind.CALL))
    state = state.apply_action(Action(ActionKind.CHECK))
    state = state.apply_chance(board[:3])
    state = state.apply_action(Action(ActionKind.CHECK))
    state = state.apply_action(Action(ActionKind.CALL))
    state = state.apply_chance(board[3:4])
    state = state.apply_action(Action(ActionKind.ALLIN))
    state = state.apply_action(Action(ActionKind.CALL))
    return state


def _build_query(state: NationalGameState) -> RangeCFVQuery:
    public = PublicHUNLState.from_common_state(state)
    r0 = uniform_reach_range(public.board_card_ids)
    r1 = uniform_reach_range(public.board_card_ids)
    return RangeCFVQuery(public_state=public, private_ranges=(r0, r1))


def _result_to_label(
    query: RangeCFVQuery,
    result,
    source: str,
) -> CFVLabel:
    return CFVLabel(
        public_state=query.public_state,
        range_p0=query.private_ranges[0],
        range_p1=query.private_ranges[1],
        target_cfv_p0=tuple(result.values[0]),
        target_cfv_p1=tuple(result.values[1]),
        label_source=source,
    )


def generate_labels(
    n_samples: int,
    seed: int = 42,
    max_showdown: int = 5,
) -> list[CFVLabel]:
    """Generate CFV training labels from exact oracle evaluators.

    Fold labels are instant. Showdown/turn labels enumerate all combo
    pairs and are capped at max_showdown each for speed.
    """
    rng = random.Random(seed)
    labels: list[CFVLabel] = []

    # Phase 1: Fast fold labels (bulk of dataset)
    n_fold = max(n_samples - 2 * max_showdown, n_samples // 2)
    for i in range(n_fold):
        board = _BOARDS[i % len(_BOARDS)] if i < len(_BOARDS) * 3 else ()
        hands_pair = _safe_hands(board)
        if len(hands_pair) < 2:
            continue
        hands = (hands_pair[0], hands_pair[1])
        sb = i % 2
        try:
            state = _make_fold_state(board, hands, sb)
            query = _build_query(state)
            result = exact_fold_cfv(query)
            labels.append(_result_to_label(query, result, "oracle_fold"))
        except (ValueError, TypeError):
            continue

    # Phase 2: Slow oracle labels (capped)
    slow_specs = [
        ("oracle_showdown", _make_showdown_state, exact_showdown_cfv),
        ("oracle_turn_allin", _make_turn_allin_state, exact_turn_allin_runout_cfv),
    ]
    remaining = n_samples - len(labels)
    per_slow = min(max_showdown, max(0, remaining // len(slow_specs)))

    for source, state_fn, oracle_fn in slow_specs:
        for i in range(per_slow):
            if len(labels) >= n_samples:
                break
            board = _BOARDS[(i + 7) % len(_BOARDS)]
            hands_pair = _safe_hands(board)
            if len(hands_pair) < 2:
                continue
            hands = (hands_pair[0], hands_pair[1])
            sb = i % 2
            try:
                state = state_fn(board, hands, sb)
                query = _build_query(state)
                result = oracle_fn(query)
                labels.append(_result_to_label(query, result, source))
            except (ValueError, TypeError):
                continue

    return labels
