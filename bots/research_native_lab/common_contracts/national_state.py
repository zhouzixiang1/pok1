"""Immutable, exact public betting state for the national heads-up game.

The local TCP engine, training traversals and online search must agree on this
state transition contract.  It intentionally contains no strategy logic.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Iterable, Sequence

from .actions import Action, ActionKind, LegalActionSet
from .cards import canonical_combo, compare_hands, validate_card
from .constants import (
    BIG_BLIND,
    CONTRACT_VERSION,
    HANDS_PER_MATCH,
    INITIAL_CHIPS,
    MIN_RAISE_POSTFLOP,
    MIN_RAISE_PREFLOP,
    RAISE_TO_MULTIPLIER,
    SMALL_BLIND,
)


class Street(str, Enum):
    PREFLOP = "preflop"
    FLOP = "flop"
    TURN = "turn"
    RIVER = "river"


_NEXT_STREET = {
    Street.PREFLOP: (Street.FLOP, 3),
    Street.FLOP: (Street.TURN, 1),
    Street.TURN: (Street.RIVER, 1),
}
_BOARD_LENGTH = {
    Street.PREFLOP: 0,
    Street.FLOP: 3,
    Street.TURN: 4,
    Street.RIVER: 5,
}


class IllegalActionError(ValueError):
    pass


class StateInvariantError(ValueError):
    pass


class IncompleteTerminalError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ActionRecord:
    actor: int
    action: Action
    street: Street
    inferred_from_boundary: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "kind": self.action.kind.value,
            "amount": self.action.amount,
            "street": self.street.value,
            "inferred_from_boundary": self.inferred_from_boundary,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ActionRecord":
        return cls(
            actor=int(payload["actor"]),
            action=Action(ActionKind(payload["kind"]), payload.get("amount")),
            street=Street(payload["street"]),
            inferred_from_boundary=bool(payload.get("inferred_from_boundary", False)),
        )


def _pair_set(values: tuple[int, int], index: int, value: int) -> tuple[int, int]:
    result = list(values)
    result[index] = value
    return int(result[0]), int(result[1])


@dataclass(frozen=True, slots=True)
class NationalGameState:
    """One national-competition hand, represented from fixed player indices.

    Player indices are stable for a hand.  The small blind is explicit because
    connection order and blind order are different concepts.  Stack fields are
    remaining chips; contribution fields are cumulative from the start of the
    hand; street bets are raise-to totals for the current street.
    """

    hand_number: int
    small_blind: int
    street: Street
    actor: int | None
    stacks: tuple[int, int]
    total_contributions: tuple[int, int]
    street_bets: tuple[int, int]
    action_counts: tuple[int, int]
    street_actions: tuple[ActionRecord, ...] = ()
    hand_history: tuple[ActionRecord, ...] = ()
    board: tuple[int, ...] = ()
    hole_cards: tuple[tuple[int, ...], tuple[int, ...]] = ((), ())
    match_net_before: tuple[int, int] = (0, 0)
    allin_occurred: bool = False
    chance_pending: bool = False
    runout_pending: bool = False
    terminal_reason: str | None = None
    winner: int | None = None
    _history_guard: object = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not hasattr(self, "_history_guard"):
            object.__setattr__(self, "_history_guard", None)
        self._assert_structural_invariants()

    @property
    def big_blind(self) -> int:
        return 1 - self.small_blind

    @property
    def pot(self) -> int:
        return sum(self.total_contributions)

    @property
    def is_terminal(self) -> bool:
        return self.terminal_reason is not None

    @classmethod
    def new_hand(
        cls,
        hand_number: int,
        *,
        small_blind: int,
        hole_cards: tuple[Sequence[int], Sequence[int]] = ((), ()),
        match_net_before: tuple[int, int] = (0, 0),
    ) -> "NationalGameState":
        normalized_holes: list[tuple[int, ...]] = []
        for hand in hole_cards:
            if not hand:
                normalized_holes.append(())
            else:
                normalized_holes.append(canonical_combo(hand))
        state = cls(
            hand_number=hand_number,
            small_blind=small_blind,
            street=Street.PREFLOP,
            actor=small_blind,
            stacks=(INITIAL_CHIPS - SMALL_BLIND, INITIAL_CHIPS - BIG_BLIND)
            if small_blind == 0
            else (INITIAL_CHIPS - BIG_BLIND, INITIAL_CHIPS - SMALL_BLIND),
            total_contributions=(SMALL_BLIND, BIG_BLIND)
            if small_blind == 0
            else (BIG_BLIND, SMALL_BLIND),
            street_bets=(SMALL_BLIND, BIG_BLIND)
            if small_blind == 0
            else (BIG_BLIND, SMALL_BLIND),
            action_counts=(0, 0),
            hole_cards=(normalized_holes[0], normalized_holes[1]),
            match_net_before=match_net_before,
        )
        return _issue_trusted_state(state)

    def _require_trusted_history(self) -> None:
        guard = self._history_guard
        if not callable(guard) or guard(self) is not True:
            raise StateInvariantError(
                "state history has not passed replay validation; use new_hand/from_dict"
            )

    def assert_invariants(self) -> None:
        self._require_trusted_history()
        self._assert_structural_invariants()

    def _assert_structural_invariants(self) -> None:
        if not 1 <= self.hand_number <= HANDS_PER_MATCH:
            raise StateInvariantError(f"hand_number must be in 1..{HANDS_PER_MATCH}")
        if self.small_blind not in (0, 1):
            raise StateInvariantError("small_blind must be player 0 or 1")
        for name, pair in (
            ("stacks", self.stacks),
            ("total_contributions", self.total_contributions),
            ("street_bets", self.street_bets),
            ("action_counts", self.action_counts),
            ("match_net_before", self.match_net_before),
        ):
            if len(pair) != 2 or not all(isinstance(value, int) for value in pair):
                raise StateInvariantError(f"{name} must be an integer pair")
        if sum(self.match_net_before) != 0:
            raise StateInvariantError("match_net_before must be zero-sum")
        for player in (0, 1):
            if self.stacks[player] < 0 or self.total_contributions[player] < 0:
                raise StateInvariantError("negative stack or contribution")
            if self.stacks[player] + self.total_contributions[player] != INITIAL_CHIPS:
                raise StateInvariantError("stack plus contribution must equal initial chips")
            if not 0 <= self.street_bets[player] <= self.total_contributions[player]:
                raise StateInvariantError("street bet outside cumulative contribution")
            if self.action_counts[player] < 0:
                raise StateInvariantError("negative action count")
        if len(self.board) != _BOARD_LENGTH[self.street]:
            raise StateInvariantError(
                f"{self.street.value} requires {_BOARD_LENGTH[self.street]} public cards"
            )
        known_cards = list(self.board)
        for hand in self.hole_cards:
            if len(hand) not in (0, 2):
                raise StateInvariantError("each known private hand has zero or two cards")
            known_cards.extend(hand)
        for card in known_cards:
            validate_card(card)
        if len(set(known_cards)) != len(known_cards):
            raise StateInvariantError("known cards conflict")
        for record in self.street_actions:
            if record.actor not in (0, 1):
                raise StateInvariantError("action actor outside player set")
            if record.street is not self.street:
                raise StateInvariantError("current street action has the wrong street label")
        if len(self.hand_history) < len(self.street_actions):
            raise StateInvariantError("append-only hand history lost current-street actions")
        if self.street_actions and self.hand_history[-len(self.street_actions) :] != self.street_actions:
            raise StateInvariantError("current-street actions are not the hand-history suffix")
        for record in self.hand_history:
            if record.actor not in (0, 1):
                raise StateInvariantError("hand-history actor outside player set")
        if self.is_terminal:
            if self.actor is not None or self.chance_pending or self.runout_pending:
                raise StateInvariantError("terminal states cannot await actions or chance")
            if self.terminal_reason == "fold" and self.winner not in (0, 1):
                raise StateInvariantError("fold terminal requires a winner")
            if self.terminal_reason == "showdown" and self.street is not Street.RIVER:
                raise StateInvariantError("showdown terminal requires river board")
        elif self.chance_pending:
            if self.actor is not None:
                raise StateInvariantError("chance nodes cannot have an actor")
            if self.street is Street.RIVER:
                raise StateInvariantError("river cannot await another public card")
            if self.runout_pending and not self.allin_occurred:
                raise StateInvariantError("runout requires an all-in")
        elif self.actor not in (0, 1):
            raise StateInvariantError("nonterminal decision states require an actor")

    def _last_raise_to(self) -> int | None:
        for record in reversed(self.street_actions):
            if record.action.kind is ActionKind.RAISE:
                return int(record.action.amount)
        return None

    def _minimum_raise_to(self) -> int:
        assert self.actor is not None
        actor = self.actor
        minimum = self.street_bets[actor] + 1
        last_raise = self._last_raise_to()
        if last_raise is not None:
            minimum = max(minimum, last_raise * RAISE_TO_MULTIPLIER)
        elif self.street is not Street.PREFLOP:
            minimum = max(minimum, MIN_RAISE_POSTFLOP)
        elif actor == self.small_blind and self.action_counts[actor] == 0:
            minimum = max(minimum, MIN_RAISE_PREFLOP)
        elif actor == self.big_blind and self.action_counts[actor] == 0:
            if self.street_actions and self.street_actions[-1].action.kind is ActionKind.CALL:
                minimum = max(minimum, MIN_RAISE_PREFLOP)
        return minimum

    def validate_action(self, action: Action) -> tuple[bool, str]:
        self._require_trusted_history()
        if self.is_terminal:
            return False, "hand is terminal"
        if self.chance_pending:
            return False, "public chance card is pending"
        if self.actor is None:
            return False, "no pending actor"

        actor = self.actor
        other = 1 - actor
        first_in_stage = not self.street_actions

        if action.kind is ActionKind.FOLD:
            return True, ""

        if action.kind is ActionKind.CALL:
            if self.street is not Street.PREFLOP and first_in_stage:
                return False, "call is illegal as first action in flop/turn/river"
            if (
                self.street is Street.PREFLOP
                and actor == self.big_blind
                and self.action_counts[actor] == 0
                and self.street_actions
                and self.street_actions[-1].action.kind is ActionKind.CALL
            ):
                return False, "BB call is illegal after SB call in preflop"
            return True, ""

        if action.kind is ActionKind.CHECK:
            if self.street is Street.PREFLOP:
                if not (
                    actor == self.big_blind
                    and self.action_counts[actor] == 0
                    and self.street_bets[other] <= self.street_bets[actor]
                ):
                    return False, "check in preflop only allowed as BB first action with no pending bet"
                return True, ""
            if not first_in_stage:
                return False, "check is illegal after the first action in flop/turn/river"
            return True, ""

        if action.kind is ActionKind.ALLIN:
            if self.allin_occurred:
                return False, "consecutive allin is illegal"
            if self.stacks[actor] <= 0:
                return False, "no chips available for allin"
            return True, ""

        assert action.kind is ActionKind.RAISE
        amount = int(action.amount)
        player_bet = self.street_bets[actor]
        chips = self.stacks[actor]
        if amount <= 0:
            return False, "raise amount must be positive"
        if amount <= player_bet:
            return False, "raise-to amount must exceed current player bet"
        needed = amount - player_bet
        if needed == chips:
            return False, "must use allin when raise equals remaining chips"
        if needed > chips:
            return False, "raise amount exceeds player chips"
        if self.allin_occurred:
            return False, "raise is illegal after allin; only call or fold"

        last_raise = self._last_raise_to()
        if self.street is Street.PREFLOP:
            if actor == self.small_blind and self.action_counts[actor] == 0:
                if amount < MIN_RAISE_PREFLOP:
                    return False, f"preflop SB first raise must be >= {MIN_RAISE_PREFLOP}"
            elif actor == self.big_blind and self.action_counts[actor] == 0:
                if self.street_actions:
                    last_action = self.street_actions[-1].action
                    if last_action.kind is ActionKind.CALL and amount < MIN_RAISE_PREFLOP:
                        return False, f"preflop BB raise must be >= {MIN_RAISE_PREFLOP} after SB call"
                    if (
                        last_action.kind is ActionKind.RAISE
                        and amount < int(last_action.amount) * RAISE_TO_MULTIPLIER
                    ):
                        return False, "preflop BB raise must be >= 2x SB raise"
        elif last_raise is None and amount < MIN_RAISE_POSTFLOP:
            return False, f"first raise in {self.street.value} must be >= {MIN_RAISE_POSTFLOP}"

        if last_raise is not None and amount < last_raise * RAISE_TO_MULTIPLIER:
            return False, f"consecutive raise must be >= 2x previous ({last_raise})"
        return True, ""

    def legal_actions(self) -> LegalActionSet:
        self._require_trusted_history()
        if self.is_terminal or self.chance_pending or self.actor is None:
            return LegalActionSet(False, False, False, False, None, None)
        fold = self.validate_action(Action(ActionKind.FOLD))[0]
        check = self.validate_action(Action(ActionKind.CHECK))[0]
        call = self.validate_action(Action(ActionKind.CALL))[0]
        allin = self.validate_action(Action(ActionKind.ALLIN))[0]
        actor = self.actor
        minimum = self._minimum_raise_to()
        maximum = self.street_bets[actor] + self.stacks[actor] - 1
        if minimum > maximum or not self.validate_action(Action(ActionKind.RAISE, minimum))[0]:
            minimum = maximum = None
        return LegalActionSet(fold, check, call, allin, minimum, maximum)

    def apply_action(
        self,
        action: Action,
        *,
        inferred_from_boundary: bool = False,
    ) -> "NationalGameState":
        legal, reason = self.validate_action(action)
        if not legal:
            raise IllegalActionError(f"{action.to_wire()}: {reason}")
        assert self.actor is not None
        actor = self.actor
        other = 1 - actor
        counts = _pair_set(self.action_counts, actor, self.action_counts[actor] + 1)
        records = self.street_actions + (
            ActionRecord(
                actor,
                action,
                self.street,
                inferred_from_boundary=inferred_from_boundary,
            ),
        )
        history = self.hand_history + (records[-1],)

        if action.kind is ActionKind.FOLD:
            return _issue_trusted_state(replace(
                self,
                actor=None,
                action_counts=counts,
                street_actions=records,
                hand_history=history,
                terminal_reason="fold",
                winner=other,
                chance_pending=False,
                runout_pending=False,
            ))

        if action.kind is ActionKind.RAISE:
            amount = int(action.amount)
            needed = amount - self.street_bets[actor]
            return _issue_trusted_state(replace(
                self,
                actor=other,
                stacks=_pair_set(self.stacks, actor, self.stacks[actor] - needed),
                total_contributions=_pair_set(
                    self.total_contributions,
                    actor,
                    self.total_contributions[actor] + needed,
                ),
                street_bets=_pair_set(self.street_bets, actor, amount),
                action_counts=counts,
                street_actions=records,
                hand_history=history,
            ))

        if action.kind is ActionKind.ALLIN:
            amount = self.stacks[actor]
            return _issue_trusted_state(replace(
                self,
                actor=other,
                stacks=_pair_set(self.stacks, actor, 0),
                total_contributions=_pair_set(
                    self.total_contributions,
                    actor,
                    self.total_contributions[actor] + amount,
                ),
                street_bets=_pair_set(
                    self.street_bets,
                    actor,
                    self.street_bets[actor] + amount,
                ),
                action_counts=counts,
                street_actions=records,
                hand_history=history,
                allin_occurred=True,
            ))

        if action.kind is ActionKind.CHECK:
            checked = _issue_trusted_state(replace(
                self,
                actor=other,
                action_counts=counts,
                street_actions=records,
                hand_history=history,
            ))
            if (
                self.street is Street.PREFLOP
                and actor == self.big_blind
                and len(records) >= 2
                and records[-2].action.kind is ActionKind.CALL
            ):
                return checked._close_street(runout=False)
            return checked

        assert action.kind is ActionKind.CALL
        difference = max(0, self.street_bets[other] - self.street_bets[actor])
        paid = min(difference, self.stacks[actor])
        stacks = _pair_set(self.stacks, actor, self.stacks[actor] - paid)
        called = _issue_trusted_state(replace(
            self,
            actor=other,
            stacks=stacks,
            total_contributions=_pair_set(
                self.total_contributions,
                actor,
                self.total_contributions[actor] + paid,
            ),
            street_bets=_pair_set(
                self.street_bets,
                actor,
                self.street_bets[actor] + paid,
            ),
            action_counts=counts,
            street_actions=records,
            hand_history=history,
            allin_occurred=self.allin_occurred or stacks[actor] == 0,
        ))
        if self.allin_occurred or stacks[actor] == 0:
            return called._close_street(runout=True)
        if self.action_counts[other] > 0:
            return called._close_street(runout=False)
        return called

    def _close_street(self, *, runout: bool) -> "NationalGameState":
        self._require_trusted_history()
        if self.street is Street.RIVER:
            return _issue_trusted_state(replace(
                self,
                actor=None,
                terminal_reason="showdown",
                winner=None,
                chance_pending=False,
                runout_pending=False,
            ))
        return _issue_trusted_state(replace(
            self,
            actor=None,
            chance_pending=True,
            runout_pending=runout,
        ))

    def apply_chance(self, cards: Iterable[int]) -> "NationalGameState":
        self._require_trusted_history()
        if self.is_terminal:
            raise StateInvariantError("cannot deal cards to a terminal hand")
        if not self.chance_pending:
            raise StateInvariantError("public cards are not pending")
        if self.street not in _NEXT_STREET:
            raise StateInvariantError("no street follows river")
        next_street, count = _NEXT_STREET[self.street]
        dealt = tuple(validate_card(card) for card in cards)
        if len(dealt) != count:
            raise StateInvariantError(f"{next_street.value} requires {count} new card(s)")
        known = set(self.board)
        known.update(self.hole_cards[0])
        known.update(self.hole_cards[1])
        if len(set(dealt)) != len(dealt) or known.intersection(dealt):
            raise StateInvariantError("dealt cards conflict with known cards")

        board = self.board + dealt
        if self.runout_pending:
            if next_street is Street.RIVER:
                return _issue_trusted_state(replace(
                    self,
                    street=next_street,
                    board=board,
                    actor=None,
                    street_bets=(0, 0),
                    action_counts=(0, 0),
                    street_actions=(),
                    chance_pending=False,
                    runout_pending=False,
                    terminal_reason="showdown",
                    winner=None,
                ))
            return _issue_trusted_state(replace(
                self,
                street=next_street,
                board=board,
                actor=None,
                street_bets=(0, 0),
                action_counts=(0, 0),
                street_actions=(),
                chance_pending=True,
                runout_pending=True,
            ))

        return _issue_trusted_state(replace(
            self,
            street=next_street,
            board=board,
            actor=self.big_blind,
            street_bets=(0, 0),
            action_counts=(0, 0),
            street_actions=(),
            chance_pending=False,
            runout_pending=False,
            allin_occurred=False,
        ))

    def infer_omitted_closing_action(self) -> tuple["NationalGameState", ActionRecord]:
        """Infer only the action strictly proven by a later street/showdown.

        This method does not accept a bare settlement as proof: a settlement
        can be either a fold or a showdown until ``oppo_hands`` arrives.
        """

        if self.is_terminal or self.chance_pending or self.actor is None:
            raise StateInvariantError("state does not need a closing-action inference")
        if not self.street_actions:
            raise StateInvariantError("a street boundary cannot skip the opening action")
        if (
            self.street is Street.PREFLOP
            and self.actor == self.big_blind
            and self.street_actions[-1].action.kind is ActionKind.CALL
        ):
            action = Action(ActionKind.CHECK)
        else:
            action = Action(ActionKind.CALL)
        if not self.validate_action(action)[0]:
            raise StateInvariantError("no uniquely legal closing call/check is implied")
        closed = self.apply_action(action, inferred_from_boundary=True)
        if not (closed.chance_pending or closed.is_terminal):
            raise StateInvariantError("inferred action did not close the street")
        return closed, closed.street_actions[-1]

    def with_hole_cards(self, player: int, cards: Sequence[int]) -> "NationalGameState":
        self._require_trusted_history()
        if player not in (0, 1):
            raise ValueError("player must be 0 or 1")
        hand = canonical_combo(cards)
        holes = list(self.hole_cards)
        holes[player] = hand
        return _issue_trusted_state(
            replace(self, hole_cards=(holes[0], holes[1]))
        )

    def terminal_utility(
        self,
        *,
        hole_cards: tuple[Sequence[int], Sequence[int]] | None = None,
    ) -> tuple[int, int]:
        self._require_trusted_history()
        if not self.is_terminal:
            raise IncompleteTerminalError("utility requested before terminal state")
        final_stacks = list(self.stacks)
        if self.terminal_reason == "fold":
            assert self.winner is not None
            final_stacks[self.winner] += self.pot
        elif self.terminal_reason == "showdown":
            holes = hole_cards if hole_cards is not None else self.hole_cards
            if len(holes[0]) != 2 or len(holes[1]) != 2 or len(self.board) != 5:
                raise IncompleteTerminalError("showdown utility requires both hands and five board cards")
            first = canonical_combo(holes[0])
            second = canonical_combo(holes[1])
            if set(first).intersection(second) or set(first + second).intersection(self.board):
                raise IncompleteTerminalError("showdown hole cards conflict")
            comparison = compare_hands(first + self.board, second + self.board)
            if comparison > 0:
                final_stacks[0] += self.pot
            elif comparison < 0:
                final_stacks[1] += self.pot
            else:
                half = self.pot // 2
                final_stacks[self.small_blind] += half
                final_stacks[self.big_blind] += self.pot - half
        else:
            raise IncompleteTerminalError(f"unknown terminal reason {self.terminal_reason!r}")
        utility = (final_stacks[0] - INITIAL_CHIPS, final_stacks[1] - INITIAL_CHIPS)
        if sum(utility) != 0:
            raise StateInvariantError("terminal utility is not zero-sum")
        return utility

    def to_validator_state(self) -> dict[str, Any]:
        self._require_trusted_history()
        if self.actor is None:
            raise StateInvariantError("validator projection requires an actor")
        return {
            "stage": self.street.value,
            "actions": [
                (record.action.kind.value, record.action.amount)
                for record in self.street_actions
            ],
            "player_chips": self.stacks[self.actor],
            "player_bet": self.street_bets[self.actor],
            "opponent_bet": self.street_bets[1 - self.actor],
            "is_small_blind": self.actor == self.small_blind,
            "is_big_blind": self.actor == self.big_blind,
            "allin_occurred": self.allin_occurred,
            "player_action_count": self.action_counts[self.actor],
        }

    def _state_payload(self) -> dict[str, Any]:
        return {
            "contract_version": CONTRACT_VERSION,
            "hand_number": self.hand_number,
            "small_blind": self.small_blind,
            "street": self.street.value,
            "actor": self.actor,
            "stacks": list(self.stacks),
            "total_contributions": list(self.total_contributions),
            "street_bets": list(self.street_bets),
            "action_counts": list(self.action_counts),
            "street_actions": [record.to_dict() for record in self.street_actions],
            "hand_history": [record.to_dict() for record in self.hand_history],
            "board": list(self.board),
            "hole_cards": [list(hand) for hand in self.hole_cards],
            "match_net_before": list(self.match_net_before),
            "allin_occurred": self.allin_occurred,
            "chance_pending": self.chance_pending,
            "runout_pending": self.runout_pending,
            "terminal_reason": self.terminal_reason,
            "winner": self.winner,
        }

    def to_dict(self) -> dict[str, Any]:
        self._require_trusted_history()
        return self._state_payload()

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "NationalGameState":
        if payload.get("contract_version") != CONTRACT_VERSION:
            raise ValueError("unsupported or missing national state contract version")
        state = cls(
            hand_number=int(payload["hand_number"]),
            small_blind=int(payload["small_blind"]),
            street=Street(payload["street"]),
            actor=None if payload.get("actor") is None else int(payload["actor"]),
            stacks=tuple(int(value) for value in payload["stacks"]),
            total_contributions=tuple(int(value) for value in payload["total_contributions"]),
            street_bets=tuple(int(value) for value in payload["street_bets"]),
            action_counts=tuple(int(value) for value in payload["action_counts"]),
            street_actions=tuple(
                ActionRecord.from_dict(record) for record in payload["street_actions"]
            ),
            hand_history=tuple(
                ActionRecord.from_dict(record) for record in payload["hand_history"]
            ),
            board=tuple(int(card) for card in payload["board"]),
            hole_cards=tuple(
                tuple(int(card) for card in hand) for hand in payload["hole_cards"]
            ),
            match_net_before=tuple(int(value) for value in payload["match_net_before"]),
            allin_occurred=bool(payload["allin_occurred"]),
            chance_pending=bool(payload["chance_pending"]),
            runout_pending=bool(payload["runout_pending"]),
            terminal_reason=payload.get("terminal_reason"),
            winner=None if payload.get("winner") is None else int(payload["winner"]),
        )
        state._assert_history_replay()
        return _issue_trusted_state(state)

    def _assert_history_replay(self) -> None:
        """Reject checkpoint payloads whose claimed state does not follow history."""

        replay = NationalGameState.new_hand(
            self.hand_number,
            small_blind=self.small_blind,
            hole_cards=self.hole_cards,
            match_net_before=self.match_net_before,
        )

        def deal_to_next(current: NationalGameState) -> NationalGameState:
            if current.street is Street.PREFLOP:
                cards = self.board[:3]
            elif current.street is Street.FLOP:
                cards = self.board[3:4]
            elif current.street is Street.TURN:
                cards = self.board[4:5]
            else:
                raise StateInvariantError("history attempts to deal beyond river")
            try:
                return current.apply_chance(cards)
            except (StateInvariantError, IllegalActionError) as exc:
                raise StateInvariantError("board cannot be replayed from action history") from exc

        for record in self.hand_history:
            while replay.street is not record.street:
                if not replay.chance_pending:
                    raise StateInvariantError("history changes street before a betting close")
                replay = deal_to_next(replay)
            if replay.chance_pending or replay.is_terminal:
                raise StateInvariantError("history contains an action outside a decision node")
            if replay.actor != record.actor:
                raise StateInvariantError("history actor order is inconsistent")
            try:
                replay = replay.apply_action(
                    record.action,
                    inferred_from_boundary=record.inferred_from_boundary,
                )
            except IllegalActionError as exc:
                raise StateInvariantError("history contains an illegal action") from exc

        while replay.street is not self.street:
            if not replay.chance_pending:
                raise StateInvariantError("state street is not reachable from history")
            replay = deal_to_next(replay)

        fields = (
            "street",
            "actor",
            "stacks",
            "total_contributions",
            "street_bets",
            "action_counts",
            "street_actions",
            "hand_history",
            "board",
            "allin_occurred",
            "chance_pending",
            "runout_pending",
            "terminal_reason",
            "winner",
        )
        mismatches = [name for name in fields if getattr(replay, name) != getattr(self, name)]
        if mismatches:
            raise StateInvariantError(
                "serialized state disagrees with replayed history: " + ",".join(mismatches)
            )

    @staticmethod
    def _digest(payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def canonical_json(self) -> str:
        """Canonical full-state serialization for checkpoints, not infoset keys."""

        self._require_trusted_history()
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def hand_public_dict(self) -> dict[str, Any]:
        """Public state for single-hand solving, excluding match context."""

        self._require_trusted_history()
        payload = self.to_dict()
        payload.pop("hole_cards")
        payload.pop("hand_number")
        payload.pop("match_net_before")
        # Whether an official transport relayed or suppressed a proven closing
        # action is audit provenance, not part of the poker information set.
        for field_name in ("street_actions", "hand_history"):
            payload[field_name] = [
                {
                    "actor": record["actor"],
                    "kind": record["kind"],
                    "amount": record["amount"],
                    "street": record["street"],
                }
                for record in payload[field_name]
            ]
        return payload

    def hand_public_state_id(self) -> str:
        """Hash public cards, bets, positions and the complete single-hand line."""

        return self._digest(self.hand_public_dict())

    def information_state_id(self, player: int) -> str:
        """Hash public state plus exactly the acting player's known private hand."""

        if player not in (0, 1):
            raise ValueError("player must be 0 or 1")
        payload = self.hand_public_dict()
        payload["information_player"] = player
        payload["private_hand"] = list(self.hole_cards[player])
        return self._digest(payload)

    def match_context_id(self) -> str:
        """Hash only legal cross-hand context for a separately tested controller."""

        return self._digest(
            {
                "contract_version": CONTRACT_VERSION,
                "hand_number": self.hand_number,
                "match_net_before": list(self.match_net_before),
            }
        )

    def observation_id(self, player: int) -> str:
        """Bind hand infoset and match context without using it as a CFR key."""

        return self._digest(
            {
                "information_state_id": self.information_state_id(player),
                "match_context_id": self.match_context_id(),
            }
        )

    def full_state_id(self) -> str:
        """Hash all known cards; use only for exact simulation and checkpoints."""

        return self._digest(self.to_dict())


def _issue_trusted_state(state: NationalGameState) -> NationalGameState:
    """Seal one state instance produced by this module's transition paths.

    The capability is instance- and content-bound.  Standard construction,
    ``dataclasses.replace`` and copy/deepcopy therefore cannot inherit trust;
    deserialization separately proves the complete history before issuance.
    Candidate bots run outside the trusted evaluator process and never receive
    this issuer.
    """

    sealed_digest = NationalGameState._digest(state._state_payload())
    owner = state

    def issued_instance(
        candidate: object,
        expected_owner: NationalGameState = owner,
        expected_digest: str = sealed_digest,
    ) -> bool:
        return (
            candidate is expected_owner
            and isinstance(candidate, NationalGameState)
            and NationalGameState._digest(candidate._state_payload())
            == expected_digest
        )

    object.__setattr__(state, "_history_guard", issued_instance)
    return state
