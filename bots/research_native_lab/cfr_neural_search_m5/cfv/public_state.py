"""Pure public HUNL state consumed by every M5 label and leaf provider."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from bots.research_native_lab.cfr_neural_search.core.identity import payload_sha256
from bots.research_native_lab.common_contracts import Action, ActionKind, NationalGameState
from bots.research_native_lab.common_contracts.constants import BIG_BLIND, INITIAL_CHIPS

from .combo_index import canonical_board


ACTION_SLOTS = (
    "fold",
    "check",
    "call",
    "min_raise",
    "half_pot",
    "pot",
    "one_and_half_pot",
    "all_in",
)
STREETS = ("preflop", "flop", "turn", "river")
BOARD_LENGTH_BY_STREET = {"preflop": 0, "flop": 3, "turn": 4, "river": 5}
INITIAL_STACK_BB = INITIAL_CHIPS / BIG_BLIND
TOTAL_CHIPS_BB = 2.0 * INITIAL_STACK_BB


def _number(value: object, context: str, *, nonnegative: bool = True) -> float:
    if type(value) not in (int, float):
        raise TypeError(f"{context} must be an exact JSON number")
    result = float(value)
    if not math.isfinite(result) or (nonnegative and result < 0.0):
        raise ValueError(f"{context} must be finite and nonnegative")
    return result


def _pair(values: object, context: str) -> tuple[float, float]:
    if type(values) is not tuple or len(values) != 2:
        raise TypeError(f"{context} must be an immutable pair")
    return (_number(values[0], context), _number(values[1], context))


def _bb_to_chips(value: float, context: str) -> int:
    chips = int(round(value * BIG_BLIND))
    if not math.isclose(value, chips / BIG_BLIND, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError(f"{context} is not representable in national chip units")
    return chips


@dataclass(frozen=True, slots=True)
class PublicActionRecord:
    actor: int
    street: str
    kind: str
    raise_to_bb: float | None = None

    def __post_init__(self) -> None:
        if type(self.actor) is not int or self.actor not in (0, 1):
            raise ValueError("public action actor must be exact player 0/1")
        if type(self.street) is not str or self.street not in STREETS:
            raise ValueError("public action street is invalid")
        if type(self.kind) is not str or self.kind not in {
            "fold",
            "check",
            "call",
            "raise",
            "allin",
        }:
            raise ValueError("public action kind is invalid")
        if self.kind == "raise":
            amount = _number(self.raise_to_bb, "public raise-to")
            if amount <= 0.0:
                raise ValueError("public raise-to must be positive")
            object.__setattr__(self, "raise_to_bb", amount)
        elif self.raise_to_bb is not None:
            raise ValueError("only raise actions carry a raise-to amount")

    def to_payload(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "street": self.street,
            "kind": self.kind,
            "raise_to_bb": self.raise_to_bb,
        }

def _slot_mask_from_common(state: NationalGameState) -> tuple[bool, ...]:
    if state.actor is None or state.is_terminal or state.chance_pending:
        return (False,) * len(ACTION_SLOTS)
    legal = state.legal_actions()
    candidates: list[Action | None] = [None] * len(ACTION_SLOTS)
    if legal.fold:
        candidates[0] = Action(ActionKind.FOLD)
    if legal.check:
        candidates[1] = Action(ActionKind.CHECK)
    if legal.call:
        candidates[2] = Action(ActionKind.CALL)
    if legal.min_raise_to is not None:
        assert legal.max_raise_to is not None
        candidates[3] = Action(ActionKind.RAISE, legal.min_raise_to)
        actor = state.actor
        other = 1 - actor
        to_call = max(0, state.street_bets[other] - state.street_bets[actor])
        pot_after_call = state.pot + to_call
        for index, fraction in ((4, 0.5), (5, 1.0), (6, 1.5)):
            increment = max(1, int(math.floor(fraction * pot_after_call + 0.5)))
            target = state.street_bets[actor] + to_call + increment
            target = min(legal.max_raise_to, max(legal.min_raise_to, target))
            candidates[index] = Action(ActionKind.RAISE, target)
    if legal.allin:
        candidates[7] = Action(ActionKind.ALLIN)

    result = [False] * len(ACTION_SLOTS)
    seen: set[Action] = set()
    for index, action in enumerate(candidates):
        if action is None or action in seen:
            continue
        if not legal.contains(action) or not state.validate_action(action)[0]:
            raise ValueError("Common state rejected a generated public action slot")
        seen.add(action)
        result[index] = True
    if not any(result):
        raise ValueError("pending Common decision has no public action slot")
    return tuple(result)


@dataclass(frozen=True, slots=True)
class PublicHUNLState:
    """A private-card-free, BB-normalized projection of Common state."""

    street: str
    board_card_ids: tuple[int, ...]
    small_blind_player: int
    actor: int | None
    pot_bb: float
    stacks_bb: tuple[float, float]
    street_commitments_bb: tuple[float, float]
    to_call_bb: float
    min_raise_to_bb: float | None
    public_action_history: tuple[PublicActionRecord, ...]
    legal_action_mask: tuple[bool, ...]

    def __post_init__(self) -> None:
        if type(self.street) is not str or self.street not in STREETS:
            raise ValueError("public HUNL street is invalid")
        board = canonical_board(self.board_card_ids)
        if len(board) != BOARD_LENGTH_BY_STREET[self.street]:
            raise ValueError("public board length disagrees with street")
        if type(self.small_blind_player) is not int or self.small_blind_player not in (0, 1):
            raise ValueError("small_blind_player must be exact player 0/1")
        if self.actor is not None and (type(self.actor) is not int or self.actor not in (0, 1)):
            raise ValueError("public actor must be exact player 0/1 or None")
        pot = _number(self.pot_bb, "public pot")
        stacks = _pair(self.stacks_bb, "public stacks")
        commitments = _pair(self.street_commitments_bb, "street commitments")
        to_call = _number(self.to_call_bb, "public to-call")
        if any(stack > INITIAL_STACK_BB for stack in stacks):
            raise ValueError("public stack exceeds national initial stack")
        contributions = tuple(INITIAL_STACK_BB - stack for stack in stacks)
        if not math.isclose(pot, math.fsum(contributions), abs_tol=1e-9, rel_tol=0.0):
            raise ValueError("public pot differs from past contributions")
        if any(commitments[player] > contributions[player] + 1e-9 for player in (0, 1)):
            raise ValueError("street commitment exceeds total contribution")
        if type(self.public_action_history) is not tuple or any(
            type(record) is not PublicActionRecord for record in self.public_action_history
        ):
            raise TypeError("public action history must contain exact immutable records")
        if type(self.legal_action_mask) is not tuple or len(self.legal_action_mask) != len(
            ACTION_SLOTS
        ) or any(type(value) is not bool for value in self.legal_action_mask):
            raise TypeError("legal action mask must be eight exact bools")
        if self.actor is None:
            if any(self.legal_action_mask) or to_call != 0.0 or self.min_raise_to_bb is not None:
                raise ValueError("non-decision public state cannot expose action values")
        else:
            expected_to_call = max(
                0.0,
                commitments[1 - self.actor] - commitments[self.actor],
            )
            if not math.isclose(to_call, expected_to_call, abs_tol=1e-9, rel_tol=0.0):
                raise ValueError("public to-call disagrees with commitments")
            if not any(self.legal_action_mask):
                raise ValueError("decision public state requires a legal action")
            if self.min_raise_to_bb is None:
                if any(self.legal_action_mask[index] for index in (3, 4, 5, 6)):
                    raise ValueError("raise slots require min_raise_to_bb")
            else:
                minimum = _number(self.min_raise_to_bb, "minimum raise-to")
                if minimum <= 0.0 or not self.legal_action_mask[3]:
                    raise ValueError("minimum raise-to requires the min-raise slot")
                object.__setattr__(self, "min_raise_to_bb", minimum)
        object.__setattr__(self, "board_card_ids", board)
        object.__setattr__(self, "pot_bb", pot)
        object.__setattr__(self, "stacks_bb", stacks)
        object.__setattr__(self, "street_commitments_bb", commitments)
        object.__setattr__(self, "to_call_bb", to_call)

    @classmethod
    def from_common_state(cls, state: NationalGameState) -> "PublicHUNLState":
        if type(state) is not NationalGameState:
            raise TypeError("public projection requires exact Common NationalGameState")
        state.assert_invariants()
        actor = state.actor
        legal = state.legal_actions()
        to_call = (
            0
            if actor is None
            else max(0, state.street_bets[1 - actor] - state.street_bets[actor])
        )
        history = tuple(
            PublicActionRecord(
                actor=record.actor,
                street=record.street.value,
                kind=record.action.kind.value,
                raise_to_bb=(
                    None
                    if record.action.amount is None
                    else record.action.amount / BIG_BLIND
                ),
            )
            for record in state.hand_history
        )
        return cls(
            street=state.street.value,
            board_card_ids=state.board,
            small_blind_player=state.small_blind,
            actor=actor,
            pot_bb=state.pot / BIG_BLIND,
            stacks_bb=(state.stacks[0] / BIG_BLIND, state.stacks[1] / BIG_BLIND),
            street_commitments_bb=(
                state.street_bets[0] / BIG_BLIND,
                state.street_bets[1] / BIG_BLIND,
            ),
            to_call_bb=to_call / BIG_BLIND,
            min_raise_to_bb=(
                None if legal.min_raise_to is None else legal.min_raise_to / BIG_BLIND
            ),
            public_action_history=history,
            legal_action_mask=_slot_mask_from_common(state),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "street": self.street,
            "board_card_ids": list(self.board_card_ids),
            "small_blind_player": self.small_blind_player,
            "actor": self.actor,
            "pot_bb": self.pot_bb,
            "stacks_bb": list(self.stacks_bb),
            "street_commitments_bb": list(self.street_commitments_bb),
            "to_call_bb": self.to_call_bb,
            "min_raise_to_bb": self.min_raise_to_bb,
            "public_action_history": [
                record.to_payload() for record in self.public_action_history
            ],
            "legal_action_mask": list(self.legal_action_mask),
        }

    def replay_common_public_state(self) -> NationalGameState:
        """Replay the complete public history through the Common transition law."""

        state = NationalGameState.new_hand(
            1,
            small_blind=self.small_blind_player,
            hole_cards=((), ()),
        )

        def deal_next(current: NationalGameState) -> NationalGameState:
            if not current.chance_pending:
                raise ValueError("public history crosses a street without a Common close")
            if current.street.value == "preflop":
                cards = self.board_card_ids[:3]
            elif current.street.value == "flop":
                cards = self.board_card_ids[3:4]
            elif current.street.value == "turn":
                cards = self.board_card_ids[4:5]
            else:
                raise ValueError("public replay attempts to deal beyond river")
            return current.apply_chance(cards)

        street_order = {street: index for index, street in enumerate(STREETS)}
        for record in self.public_action_history:
            if street_order[record.street] < street_order[state.street.value]:
                raise ValueError("public action history moves backward across streets")
            while state.street.value != record.street:
                state = deal_next(state)
            if state.is_terminal or state.chance_pending or state.actor != record.actor:
                raise ValueError("public action cannot be replayed at the claimed Common node")
            kind = ActionKind(record.kind)
            amount = (
                None
                if record.raise_to_bb is None
                else _bb_to_chips(record.raise_to_bb, "public replay raise-to")
            )
            state = state.apply_action(Action(kind, amount))
        if street_order[self.street] < street_order[state.street.value]:
            raise ValueError("public state street precedes replayed Common state")
        while state.street.value != self.street:
            state = deal_next(state)
        replayed = PublicHUNLState.from_common_state(state)
        if payload_sha256(replayed.to_payload()) != payload_sha256(self.to_payload()):
            raise ValueError("public fields differ from complete Common replay")
        return state

    @property
    def digest(self) -> str:
        return payload_sha256(self.to_payload())
