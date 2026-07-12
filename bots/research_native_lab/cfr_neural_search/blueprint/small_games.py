"""Clean-room Kuhn and Leduc poker games for solver verification.

The games use physical cards at chance nodes and rank-only private
observations, so suit-equivalent deals share information sets.  Utilities are
net of each player's contributions and are exactly zero sum.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import ClassVar

from ..core.game import Action, CHANCE_PLAYER, TERMINAL_PLAYER

CHECK = "check"
BET = "bet"
CALL = "call"
FOLD = "fold"
RAISE = "raise"

_KUHN_TERMINALS = {
    (CHECK, CHECK),
    (BET, CALL),
    (BET, FOLD),
    (CHECK, BET, CALL),
    (CHECK, BET, FOLD),
}


@dataclass(frozen=True, slots=True)
class KuhnState:
    """Three-card Kuhn poker with one-chip antes and one-chip bets."""

    cards: tuple[int, ...] = ()
    history: tuple[str, ...] = ()

    @property
    def depth(self) -> int:
        return len(self.cards) + len(self.history)

    @property
    def current_player(self) -> int:
        if self.history in _KUHN_TERMINALS:
            return TERMINAL_PLAYER
        if len(self.cards) < 2:
            return CHANCE_PLAYER
        if self.history in ((), (CHECK, BET)):
            return 0
        if self.history in ((CHECK,), (BET,)):
            return 1
        raise RuntimeError(f"invalid Kuhn history: {self.history!r}")

    def chance_outcomes(self) -> tuple[tuple[Action, float], ...]:
        if self.current_player != CHANCE_PLAYER:
            raise ValueError("not a chance node")
        available = tuple(card for card in range(3) if card not in self.cards)
        probability = 1.0 / len(available)
        return tuple((card, probability) for card in available)

    def legal_actions(self) -> tuple[Action, ...]:
        player = self.current_player
        if player < 0:
            return ()
        if self.history in ((), (CHECK,)):
            return (CHECK, BET)
        if self.history in ((BET,), (CHECK, BET)):
            return (CALL, FOLD)
        raise RuntimeError(f"invalid Kuhn decision history: {self.history!r}")

    def child(self, action: Action) -> "KuhnState":
        if self.current_player == CHANCE_PLAYER:
            legal = tuple(card for card, _ in self.chance_outcomes())
            if action not in legal:
                raise ValueError(f"illegal Kuhn chance action: {action!r}")
            return replace(self, cards=self.cards + (int(action),))
        if action not in self.legal_actions():
            raise ValueError(f"illegal Kuhn action {action!r} at {self.history!r}")
        return replace(self, history=self.history + (str(action),))

    def information_state_key(self, player: int) -> str:
        if player not in (0, 1) or len(self.cards) < 2:
            raise ValueError("Kuhn information state requires a dealt player")
        private_rank = self.cards[player]
        history = ",".join(self.history) or "root"
        return f"kuhn:p{player}:r{private_rank}:h={history}"

    def returns(self) -> tuple[float, float]:
        if self.current_player != TERMINAL_PLAYER:
            raise ValueError("returns requested from non-terminal Kuhn state")
        if self.history[-1] == FOLD:
            folder = 1 if self.history == (BET, FOLD) else 0
            utility0 = -1.0 if folder == 0 else 1.0
        else:
            stake = 2.0 if BET in self.history else 1.0
            utility0 = stake if self.cards[0] > self.cards[1] else -stake
        return (utility0, -utility0)


@dataclass(frozen=True, slots=True)
class KuhnPoker:
    name: ClassVar[str] = "kuhn"

    def new_initial_state(self) -> KuhnState:
        return KuhnState()


@dataclass(frozen=True, slots=True)
class LeducState:
    """Two-player limit Leduc poker with two raises/bets per round.

    The six physical cards contain two copies of each rank.  Each player antes
    one chip, receives one private card, and a single public card is dealt
    between betting rounds.  Fixed bet increments are two then four chips.
    """

    max_raises: int = 2
    private_cards: tuple[int, ...] = ()
    public_card: int | None = None
    round_index: int = 0
    to_act: int = 0
    round_contrib: tuple[int, int] = (0, 0)
    total_contrib: tuple[int, int] = (1, 1)
    raises: int = 0
    checks: int = 0
    histories: tuple[tuple[str, ...], tuple[str, ...]] = ((), ())
    awaiting_public: bool = False
    folded: int | None = None
    showdown: bool = False

    @property
    def depth(self) -> int:
        public_depth = 1 if self.public_card is not None else 0
        action_depth = sum(len(round_history) for round_history in self.histories)
        return len(self.private_cards) + public_depth + action_depth

    @property
    def current_player(self) -> int:
        if self.folded is not None or self.showdown:
            return TERMINAL_PLAYER
        if len(self.private_cards) < 2 or self.awaiting_public:
            return CHANCE_PLAYER
        return self.to_act

    @staticmethod
    def rank(card: int) -> int:
        return card // 2

    def _available_cards(self) -> tuple[int, ...]:
        used = set(self.private_cards)
        if self.public_card is not None:
            used.add(self.public_card)
        return tuple(card for card in range(6) if card not in used)

    def chance_outcomes(self) -> tuple[tuple[Action, float], ...]:
        if self.current_player != CHANCE_PLAYER:
            raise ValueError("not a chance node")
        available = self._available_cards()
        probability = 1.0 / len(available)
        return tuple((card, probability) for card in available)

    def legal_actions(self) -> tuple[Action, ...]:
        if self.current_player < 0:
            return ()
        owed = max(self.round_contrib) - self.round_contrib[self.to_act]
        actions: list[Action]
        if owed == 0:
            actions = [CHECK]
        else:
            actions = [CALL, FOLD]
        if self.raises < self.max_raises:
            actions.append(RAISE)
        return tuple(actions)

    def child(self, action: Action) -> "LeducState":
        if self.current_player == CHANCE_PLAYER:
            legal = tuple(card for card, _ in self.chance_outcomes())
            if action not in legal:
                raise ValueError(f"illegal Leduc chance action: {action!r}")
            card = int(action)
            if len(self.private_cards) < 2:
                return replace(self, private_cards=self.private_cards + (card,))
            if not self.awaiting_public:
                raise RuntimeError("unexpected Leduc public chance node")
            return replace(
                self,
                public_card=card,
                round_index=1,
                to_act=0,
                round_contrib=(0, 0),
                raises=0,
                checks=0,
                awaiting_public=False,
            )

        if action not in self.legal_actions():
            raise ValueError(
                f"illegal Leduc action {action!r}; legal={self.legal_actions()!r}"
            )

        actor = self.to_act
        other = 1 - actor
        histories = [list(round_history) for round_history in self.histories]
        histories[self.round_index].append(str(action))
        history_tuple = (tuple(histories[0]), tuple(histories[1]))

        if action == FOLD:
            return replace(self, histories=history_tuple, folded=actor)

        if action == CHECK:
            if self.checks == 1:
                return self._close_round(history_tuple, self.total_contrib)
            return replace(
                self,
                histories=history_tuple,
                to_act=other,
                checks=1,
            )

        owed = max(self.round_contrib) - self.round_contrib[actor]
        round_contrib = list(self.round_contrib)
        total_contrib = list(self.total_contrib)

        if action == CALL:
            round_contrib[actor] += owed
            total_contrib[actor] += owed
            return self._close_round(history_tuple, tuple(total_contrib))

        if action == RAISE:
            increment = 2 if self.round_index == 0 else 4
            payment = owed + increment
            round_contrib[actor] += payment
            total_contrib[actor] += payment
            return replace(
                self,
                histories=history_tuple,
                to_act=other,
                round_contrib=tuple(round_contrib),
                total_contrib=tuple(total_contrib),
                raises=self.raises + 1,
                checks=0,
            )

        raise AssertionError(f"unhandled Leduc action: {action!r}")

    def _close_round(
        self,
        histories: tuple[tuple[str, ...], tuple[str, ...]],
        total_contrib: tuple[int, int],
    ) -> "LeducState":
        if self.round_index == 0:
            return replace(
                self,
                histories=histories,
                total_contrib=total_contrib,
                awaiting_public=True,
                checks=0,
            )
        return replace(
            self,
            histories=histories,
            total_contrib=total_contrib,
            showdown=True,
            checks=0,
        )

    def information_state_key(self, player: int) -> str:
        if player not in (0, 1) or len(self.private_cards) < 2:
            raise ValueError("Leduc information state requires a dealt player")
        own_rank = self.rank(self.private_cards[player])
        public_rank = -1 if self.public_card is None else self.rank(self.public_card)
        round0 = ",".join(self.histories[0]) or "root"
        round1 = ",".join(self.histories[1]) or "root"
        return (
            f"leduc:p{player}:r{own_rank}:board={public_rank}:"
            f"h0={round0}:h1={round1}"
        )

    def returns(self) -> tuple[float, float]:
        if self.current_player != TERMINAL_PLAYER:
            raise ValueError("returns requested from non-terminal Leduc state")
        if self.folded is not None:
            winner = 1 - self.folded
        else:
            if self.public_card is None:
                raise RuntimeError("Leduc showdown without public card")
            board_rank = self.rank(self.public_card)
            ranks = tuple(self.rank(card) for card in self.private_cards)
            strengths = tuple((rank == board_rank, rank) for rank in ranks)
            winner = -1 if strengths[0] == strengths[1] else int(strengths[1] > strengths[0])

        pot = float(sum(self.total_contrib))
        if winner == -1:
            utility0 = pot / 2.0 - self.total_contrib[0]
        elif winner == 0:
            utility0 = pot - self.total_contrib[0]
        else:
            utility0 = -float(self.total_contrib[0])
        return (utility0, -utility0)


@dataclass(frozen=True, slots=True)
class LeducPoker:
    max_raises: int = 2
    name: ClassVar[str] = "leduc"

    def new_initial_state(self) -> LeducState:
        return LeducState(max_raises=self.max_raises)


def make_game(name: str):
    normalized = name.strip().lower()
    if normalized == "kuhn":
        return KuhnPoker()
    if normalized == "leduc":
        return LeducPoker()
    raise ValueError(f"unknown small game: {name!r}")
