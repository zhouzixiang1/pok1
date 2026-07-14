"""Independent exact physical-card HUNL micro-oracles for M5 CFV gates."""

from __future__ import annotations

import math

from bots.research_native_lab.common_contracts.cards import compare_hands
from bots.research_native_lab.common_contracts.constants import BIG_BLIND, INITIAL_CHIPS

from .combo_index import COMBOS, COMBO_COUNT
from .pairwise import exact_pairwise_cfv
from .public_state import PublicHUNLState
from .semantics import RangeCFVQuery, RangeCFVResult


FOLD_PROVIDER_ID = "route-b-m5-hunl-fold-oracle-v1"
SHOWDOWN_PROVIDER_ID = "route-b-m5-hunl-showdown-oracle-v1"
RIVER_DECISION_PROVIDER_ID = "route-b-m5-hunl-river-one-decision-oracle-v1"
TURN_ALLIN_PROVIDER_ID = "route-b-m5-hunl-turn-allin-runout-oracle-v1"


def _chips(value_bb: float, context: str) -> int:
    chips = int(round(value_bb * BIG_BLIND))
    if not math.isclose(value_bb, chips / BIG_BLIND, abs_tol=1e-12, rel_tol=0.0):
        raise ValueError(f"{context} is not representable in national chip units")
    return chips


def _public_chips(state: PublicHUNLState) -> tuple[tuple[int, int], int]:
    stacks = (_chips(state.stacks_bb[0], "stack"), _chips(state.stacks_bb[1], "stack"))
    pot = _chips(state.pot_bb, "pot")
    if sum(stacks) + pot != 2 * INITIAL_CHIPS:
        raise ValueError("public stacks and pot do not conserve national chips")
    return stacks, pot


def _fold_utility0(state: PublicHUNLState, folder: int) -> float:
    if folder not in (0, 1):
        raise ValueError("folder must be player 0/1")
    stacks, pot = _public_chips(state)
    final = [stacks[0], stacks[1]]
    final[1 - folder] += pot
    return (final[0] - INITIAL_CHIPS) / BIG_BLIND


def _require_no_fold(state: PublicHUNLState, context: str) -> None:
    if any(record.kind == "fold" for record in state.public_action_history):
        raise ValueError(f"{context} cannot follow a public fold")


def _require_called_allin_runout(state: PublicHUNLState) -> None:
    _require_no_fold(state, "all-in runout")
    history = state.public_action_history
    if (
        len(history) < 2
        or history[-2].kind != "allin"
        or history[-1].kind != "call"
        or history[-2].street != history[-1].street
        or history[-2].actor == history[-1].actor
    ):
        raise ValueError("all-in runout requires an immediately called public all-in")


def _showdown_utility0(
    state: PublicHUNLState,
    first_index: int,
    second_index: int,
    board: tuple[int, ...],
    *,
    stacks: tuple[int, int] | None = None,
    pot: int | None = None,
) -> float:
    current_stacks, current_pot = _public_chips(state)
    final = list(current_stacks if stacks is None else stacks)
    award = current_pot if pot is None else pot
    first = COMBOS[first_index]
    second = COMBOS[second_index]
    comparison = compare_hands(first + board, second + board)
    if comparison > 0:
        final[0] += award
    elif comparison < 0:
        final[1] += award
    else:
        half = award // 2
        final[state.small_blind_player] += half
        final[1 - state.small_blind_player] += award - half
    if sum(final) != 2 * INITIAL_CHIPS:
        raise ValueError("showdown payoff does not conserve national chips")
    return (final[0] - INITIAL_CHIPS) / BIG_BLIND


def exact_fold_cfv(query: RangeCFVQuery) -> RangeCFVResult:
    state = query.public_state
    if state.actor is not None or not state.public_action_history:
        raise ValueError("fold oracle requires a terminal public fold state")
    replayed = state.replay_common_public_state()
    if not replayed.is_terminal or replayed.terminal_reason != "fold":
        raise ValueError("fold oracle requires a Common fold terminal")
    last = state.public_action_history[-1]
    if last.kind != "fold" or sum(
        record.kind == "fold" for record in state.public_action_history
    ) != 1:
        raise ValueError("fold oracle terminal history does not end in fold")
    utility = _fold_utility0(state, last.actor)
    return exact_pairwise_cfv(
        query,
        lambda first, second: utility,
        provider_id=FOLD_PROVIDER_ID,
    )


def exact_showdown_cfv(query: RangeCFVQuery) -> RangeCFVResult:
    state = query.public_state
    if state.street != "river" or len(state.board_card_ids) != 5 or state.actor is not None:
        raise ValueError("showdown oracle requires a terminal five-card public board")
    replayed = state.replay_common_public_state()
    if not replayed.is_terminal or replayed.terminal_reason != "showdown":
        raise ValueError("showdown oracle requires a Common showdown terminal, not a fold")
    _require_no_fold(state, "showdown")
    if not state.public_action_history or state.public_action_history[-1].kind != "call":
        raise ValueError("showdown oracle requires a street-closing public call")
    board = state.board_card_ids
    return exact_pairwise_cfv(
        query,
        lambda first, second: _showdown_utility0(state, first, second, board),
        provider_id=SHOWDOWN_PROVIDER_ID,
    )


def validate_river_call_probabilities(
    values: tuple[float, ...],
) -> tuple[float, ...]:
    if type(values) is not tuple or len(values) != COMBO_COUNT:
        raise TypeError("river call policy must be an immutable 1,326-vector")
    result: list[float] = []
    for value in values:
        if type(value) not in (int, float):
            raise TypeError("river call probabilities must be exact JSON numbers")
        probability = float(value)
        if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
            raise ValueError("river call probabilities must lie in [0,1]")
        result.append(probability)
    return tuple(result)


def exact_river_call_or_fold_cfv(
    query: RangeCFVQuery,
    call_probabilities: tuple[float, ...],
) -> RangeCFVResult:
    state = query.public_state
    probabilities = validate_river_call_probabilities(call_probabilities)
    if (
        state.street != "river"
        or len(state.board_card_ids) != 5
        or state.actor not in (0, 1)
        or state.to_call_bb <= 0.0
        or not state.legal_action_mask[0]
        or not state.legal_action_mask[2]
    ):
        raise ValueError("river one-decision oracle requires a facing-bet fold/call node")
    replayed = state.replay_common_public_state()
    if replayed.is_terminal or replayed.chance_pending or replayed.actor != state.actor:
        raise ValueError("river one-decision oracle requires a Common decision node")
    _require_no_fold(state, "river decision")
    actor = state.actor
    board_set = set(state.board_card_ids)
    if any(
        probability != 0.0
        and (COMBOS[index][0] in board_set or COMBOS[index][1] in board_set)
        for index, probability in enumerate(probabilities)
    ):
        raise ValueError("board-blocked river policy entries must be canonical zero")
    folder_value = _fold_utility0(state, actor)
    stacks, pot = _public_chips(state)
    payment = _chips(state.to_call_bb, "river call")
    if payment > stacks[actor]:
        raise ValueError("river call exceeds actor stack")
    called_stacks = list(stacks)
    called_stacks[actor] -= payment
    called_pot = pot + payment
    board = state.board_card_ids

    def utility(first_index: int, second_index: int) -> float:
        call_probability = probabilities[first_index if actor == 0 else second_index]
        showdown = _showdown_utility0(
            state,
            first_index,
            second_index,
            board,
            stacks=(called_stacks[0], called_stacks[1]),
            pot=called_pot,
        )
        return math.fsum(
            ((1.0 - call_probability) * folder_value, call_probability * showdown)
        )

    return exact_pairwise_cfv(
        query,
        utility,
        provider_id=RIVER_DECISION_PROVIDER_ID,
    )


def exact_turn_allin_runout_cfv(query: RangeCFVQuery) -> RangeCFVResult:
    state = query.public_state
    if (
        state.street != "turn"
        or len(state.board_card_ids) != 4
        or state.actor is not None
    ):
        raise ValueError("turn all-in oracle requires a closed all-in turn state")
    replayed = state.replay_common_public_state()
    if (
        replayed.is_terminal
        or not replayed.chance_pending
        or not replayed.runout_pending
    ):
        raise ValueError("turn all-in oracle requires a Common runout chance node")
    _require_called_allin_runout(state)
    board = state.board_card_ids

    def utility(first_index: int, second_index: int) -> float:
        used = set(board + COMBOS[first_index] + COMBOS[second_index])
        rivers = tuple(card for card in range(52) if card not in used)
        if len(rivers) != 44:
            raise ValueError("turn all-in runout must have exactly 44 conditioned rivers")
        return math.fsum(
            _showdown_utility0(
                state,
                first_index,
                second_index,
                board + (river,),
            )
            for river in rivers
        ) / len(rivers)

    return exact_pairwise_cfv(
        query,
        utility,
        provider_id=TURN_ALLIN_PROVIDER_ID,
    )
