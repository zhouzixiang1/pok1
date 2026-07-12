"""Exact two-player Kuhn poker rules used by both route-A toy validators.

The model uses net utilities after a one-chip ante.  A showdown without a bet
is worth +/-1; a called bet is worth +/-2; a fold is worth +/-1.  The public
history is represented with descriptive action names to keep checkpoints and
failure reports readable.
"""

from __future__ import annotations

from itertools import permutations
from typing import TypeAlias

CARDS: tuple[int, ...] = (0, 1, 2)  # J, Q, K
CARD_NAMES: tuple[str, ...] = ("J", "Q", "K")

Deal: TypeAlias = tuple[int, int]
InfoSet: TypeAlias = tuple[int, int, str]
ActionProbabilities: TypeAlias = dict[str, float]
StrategyProfile: TypeAlias = dict[InfoSet, ActionProbabilities]

_ACTOR_BY_HISTORY: dict[str, int] = {
    "": 0,
    "check": 1,
    "bet": 1,
    "check-bet": 0,
}
_ACTIONS_BY_HISTORY: dict[str, tuple[str, ...]] = {
    "": ("check", "bet"),
    "check": ("check", "bet"),
    "bet": ("fold", "call"),
    "check-bet": ("fold", "call"),
}
_NEXT_HISTORY: dict[tuple[str, str], str] = {
    ("", "check"): "check",
    ("", "bet"): "bet",
    ("check", "check"): "check-check",
    ("check", "bet"): "check-bet",
    ("bet", "fold"): "bet-fold",
    ("bet", "call"): "bet-call",
    ("check-bet", "fold"): "check-bet-fold",
    ("check-bet", "call"): "check-bet-call",
}
_TERMINAL_HISTORIES = frozenset(
    {"check-check", "bet-fold", "bet-call", "check-bet-fold", "check-bet-call"}
)


def ordered_deals() -> tuple[Deal, ...]:
    """Return the six equally likely ordered private-card deals."""

    return tuple(permutations(CARDS, 2))


def is_terminal(history: str) -> bool:
    return history in _TERMINAL_HISTORIES


def current_player(history: str) -> int:
    try:
        return _ACTOR_BY_HISTORY[history]
    except KeyError as exc:
        if is_terminal(history):
            raise ValueError(f"terminal history has no acting player: {history}") from exc
        raise ValueError(f"unknown Kuhn history: {history}") from exc


def legal_actions(history: str) -> tuple[str, ...]:
    try:
        return _ACTIONS_BY_HISTORY[history]
    except KeyError as exc:
        if is_terminal(history):
            return ()
        raise ValueError(f"unknown Kuhn history: {history}") from exc


def next_history(history: str, action: str) -> str:
    try:
        return _NEXT_HISTORY[(history, action)]
    except KeyError as exc:
        raise ValueError(f"illegal Kuhn action {action!r} at {history!r}") from exc


def terminal_utility(deal: Deal, history: str, player: int = 0) -> float:
    """Return the requested player's net utility at a terminal history."""

    if deal[0] == deal[1] or any(card not in CARDS for card in deal):
        raise ValueError(f"invalid Kuhn deal: {deal}")
    if not is_terminal(history):
        raise ValueError(f"history is not terminal: {history}")

    if history == "bet-fold":
        utility0 = 1.0
    elif history == "check-bet-fold":
        utility0 = -1.0
    else:
        stake = 2.0 if history in {"bet-call", "check-bet-call"} else 1.0
        utility0 = stake if deal[0] > deal[1] else -stake
    if player == 0:
        return utility0
    if player == 1:
        return -utility0
    raise ValueError(f"invalid player: {player}")


def infosets_for_player(player: int) -> tuple[InfoSet, ...]:
    if player == 0:
        histories = ("", "check-bet")
    elif player == 1:
        histories = ("check", "bet")
    else:
        raise ValueError(f"invalid player: {player}")
    return tuple((player, card, history) for history in histories for card in CARDS)


def all_infosets() -> tuple[InfoSet, ...]:
    return infosets_for_player(0) + infosets_for_player(1)


def uniform_strategy() -> StrategyProfile:
    profile: StrategyProfile = {}
    for player, card, history in all_infosets():
        actions = legal_actions(history)
        probability = 1.0 / len(actions)
        profile[(player, card, history)] = {
            action: probability for action in actions
        }
    return profile


def validate_strategy(profile: StrategyProfile, tolerance: float = 1e-12) -> None:
    for key in all_infosets():
        if key not in profile:
            raise ValueError(f"strategy is missing infoset {key}")
        actions = legal_actions(key[2])
        probabilities = profile[key]
        if set(probabilities) != set(actions):
            raise ValueError(
                f"strategy actions for {key} are {sorted(probabilities)}, expected {actions}"
            )
        if any(probability < -tolerance for probability in probabilities.values()):
            raise ValueError(f"negative action probability at {key}: {probabilities}")
        if abs(sum(probabilities.values()) - 1.0) > tolerance:
            raise ValueError(f"action probabilities do not sum to one at {key}")
