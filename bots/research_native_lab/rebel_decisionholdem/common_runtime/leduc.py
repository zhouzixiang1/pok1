"""Independent exact two-player limit Leduc poker tree.

This is a clean-room small-game correctness oracle.  It uses a six-card deck
with two physical cards of each rank, one private card per player, one public
card, a one-chip ante, fixed bets of two/four chips, and at most two raises per
round.  Player 0 acts first on both rounds.  These rules are frozen here rather
than inferred from either DecisionHoldem code or route B.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache
from itertools import permutations
from math import isfinite
from typing import TypeAlias


RANK_NAMES = ("J", "Q", "K")
PHYSICAL_CARDS = tuple(range(6))
ANTE = 1
BET_SIZE_BY_STREET = (2, 4)
MAX_RAISES_PER_STREET = 2
ACTIONS = ("fold", "check", "call", "raise")

LeducDeal: TypeAlias = tuple[int, int, int]
LeducInfoSet: TypeAlias = tuple[int, int, int, str]
LeducStrategy: TypeAlias = dict[LeducInfoSet, dict[str, float]]


def card_rank(card: int) -> int:
    if type(card) is not int or not 0 <= card < 6:
        raise ValueError(f"Leduc card must be an integer in [0, 5], got {card!r}")
    return card // 2


@lru_cache(maxsize=1)
def ordered_deals() -> tuple[LeducDeal, ...]:
    """Return all 120 ordered private/private/public physical-card deals."""

    return tuple(permutations(PHYSICAL_CARDS, 3))


@dataclass(frozen=True, slots=True)
class LeducState:
    street: int = 0
    actor: int = 0
    contributions: tuple[int, int] = (ANTE, ANTE)
    street_bets: tuple[int, int] = (0, 0)
    raises: int = 0
    previous_action: str | None = None
    history: tuple[str, ...] = ()
    terminal: bool = False
    folded_winner: int | None = None

    @property
    def pot(self) -> int:
        return sum(self.contributions)

    @property
    def to_call(self) -> int:
        return max(0, self.street_bets[1 - self.actor] - self.street_bets[self.actor])

    @property
    def depth(self) -> int:
        return len(self.history)


def initial_state() -> LeducState:
    return LeducState()


def public_history(state: LeducState) -> str:
    return ",".join(state.history)


def information_set(state: LeducState, deal: LeducDeal) -> LeducInfoSet:
    if state.terminal:
        raise ValueError("terminal Leduc state has no information set")
    if len(set(deal)) != 3 or any(card not in PHYSICAL_CARDS for card in deal):
        raise ValueError(f"invalid Leduc deal: {deal}")
    public_rank = -1 if state.street == 0 else card_rank(deal[2])
    return (
        state.actor,
        card_rank(deal[state.actor]),
        public_rank,
        public_history(state),
    )


def legal_actions(state: LeducState) -> tuple[str, ...]:
    if state.terminal:
        return ()
    if state.to_call > 0:
        actions = ["fold", "call"]
    else:
        actions = ["check"]
    if state.raises < MAX_RAISES_PER_STREET:
        actions.append("raise")
    return tuple(actions)


def _close_street(state: LeducState) -> LeducState:
    if state.street == 1:
        return replace(
            state,
            terminal=True,
            previous_action=None,
        )
    return LeducState(
        street=1,
        actor=0,
        contributions=state.contributions,
        street_bets=(0, 0),
        raises=0,
        previous_action=None,
        history=state.history + ("/",),
    )


def apply_action(state: LeducState, action: str) -> LeducState:
    if action not in legal_actions(state):
        raise ValueError(
            f"illegal Leduc action {action!r} at {public_history(state)!r}"
        )
    actor = state.actor
    opponent = 1 - actor
    history = state.history + (action,)

    if action == "fold":
        return replace(
            state,
            history=history,
            terminal=True,
            folded_winner=opponent,
            previous_action=action,
        )

    if action == "check":
        checked = replace(
            state,
            actor=opponent,
            history=history,
            previous_action=action,
        )
        if state.previous_action == "check":
            return _close_street(checked)
        return checked

    contributions = list(state.contributions)
    street_bets = list(state.street_bets)
    if action == "call":
        payment = state.to_call
        contributions[actor] += payment
        street_bets[actor] += payment
        called = replace(
            state,
            actor=opponent,
            contributions=(contributions[0], contributions[1]),
            street_bets=(street_bets[0], street_bets[1]),
            history=history,
            previous_action=action,
        )
        return _close_street(called)

    target = state.street_bets[opponent] + BET_SIZE_BY_STREET[state.street]
    payment = target - state.street_bets[actor]
    contributions[actor] += payment
    street_bets[actor] = target
    return replace(
        state,
        actor=opponent,
        contributions=(contributions[0], contributions[1]),
        street_bets=(street_bets[0], street_bets[1]),
        raises=state.raises + 1,
        previous_action="raise",
        history=history,
    )


def _showdown_strength(private_card: int, public_card: int) -> tuple[int, int]:
    private_rank = card_rank(private_card)
    public_rank = card_rank(public_card)
    return (1 if private_rank == public_rank else 0, private_rank)


def terminal_utility(state: LeducState, deal: LeducDeal, player: int = 0) -> float:
    if not state.terminal:
        raise ValueError("Leduc utility requires a terminal state")
    if player not in (0, 1):
        raise ValueError(f"invalid player: {player}")
    if state.folded_winner is not None:
        winner = state.folded_winner
    else:
        first = _showdown_strength(deal[0], deal[2])
        second = _showdown_strength(deal[1], deal[2])
        winner = 0 if first > second else 1 if second > first else None

    if winner is None:
        utility0 = state.pot / 2.0 - state.contributions[0]
    elif winner == 0:
        utility0 = float(state.pot - state.contributions[0])
    else:
        utility0 = float(-state.contributions[0])
    return utility0 if player == 0 else -utility0


@lru_cache(maxsize=1)
def all_infosets() -> tuple[LeducInfoSet, ...]:
    found: dict[LeducInfoSet, tuple[str, ...]] = {}

    def visit(state: LeducState, deal: LeducDeal) -> None:
        if state.terminal:
            return
        key = information_set(state, deal)
        actions = legal_actions(state)
        previous = found.setdefault(key, actions)
        if previous != actions:
            raise RuntimeError(f"Leduc information set has inconsistent actions: {key}")
        for action in actions:
            visit(apply_action(state, action), deal)

    for deal in ordered_deals():
        visit(initial_state(), deal)
    return tuple(sorted(found))


@lru_cache(maxsize=1)
def actions_by_infoset() -> dict[LeducInfoSet, tuple[str, ...]]:
    result: dict[LeducInfoSet, tuple[str, ...]] = {}

    def visit(state: LeducState, deal: LeducDeal) -> None:
        if state.terminal:
            return
        key = information_set(state, deal)
        actions = legal_actions(state)
        previous = result.setdefault(key, actions)
        if previous != actions:
            raise RuntimeError(f"Leduc information set has inconsistent actions: {key}")
        for action in actions:
            visit(apply_action(state, action), deal)

    for deal in ordered_deals():
        visit(initial_state(), deal)
    if set(result) != set(all_infosets()):
        raise RuntimeError("Leduc infoset discovery mismatch")
    return result


def uniform_strategy() -> LeducStrategy:
    return {
        key: {action: 1.0 / len(actions) for action in actions}
        for key, actions in actions_by_infoset().items()
    }


def validate_strategy(profile: LeducStrategy, tolerance: float = 1e-12) -> None:
    expected = actions_by_infoset()
    if set(profile) != set(expected):
        raise ValueError("Leduc strategy infosets do not match the exact tree")
    for key, actions in expected.items():
        probabilities = profile[key]
        if set(probabilities) != set(actions):
            raise ValueError(f"Leduc strategy actions differ at {key}")
        if any(
            type(value) not in (int, float)
            or not isfinite(value)
            or value < 0.0
            for value in probabilities.values()
        ):
            raise ValueError(f"Leduc strategy has invalid probability at {key}")
        if abs(sum(probabilities.values()) - 1.0) > tolerance:
            raise ValueError(f"Leduc probabilities do not sum to one at {key}")
