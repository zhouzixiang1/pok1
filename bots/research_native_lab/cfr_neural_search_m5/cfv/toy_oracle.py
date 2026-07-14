"""Exact Kuhn/Leduc range-CFV, derivative, regret and cutoff oracles."""

from __future__ import annotations

import math
from collections.abc import Hashable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from bots.research_native_lab.cfr_neural_search.blueprint.small_games import (
    KuhnState,
    LeducState,
)
from bots.research_native_lab.cfr_neural_search.core.game import (
    CHANCE_PLAYER,
    TERMINAL_PLAYER,
)
from bots.research_native_lab.cfr_neural_search.core.identity import payload_sha256


ToyAction = Hashable
ToyPolicy = Mapping[str, Mapping[ToyAction, float]]


@dataclass(frozen=True, slots=True)
class KuhnPublicState:
    history: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.history) is not tuple or any(type(action) is not str for action in self.history):
            raise TypeError("Kuhn public history must be an immutable string tuple")
        try:
            KuhnState(cards=(0, 1), history=self.history).current_player
        except RuntimeError as exc:
            raise ValueError("invalid Kuhn public history") from exc

    @property
    def deck_size(self) -> int:
        return 3

    @property
    def blocked_cards(self) -> frozenset[int]:
        return frozenset()

    def instantiate(self, first: int, second: int) -> KuhnState:
        if first == second or first not in range(3) or second not in range(3):
            raise ValueError("Kuhn private cards must be distinct physical cards")
        return KuhnState(cards=(first, second), history=self.history)

    def to_payload(self) -> dict[str, Any]:
        return {"game": "kuhn", "history": list(self.history)}


@dataclass(frozen=True, slots=True)
class LeducPublicState:
    max_raises: int
    public_card: int | None
    round_index: int
    to_act: int
    round_contrib: tuple[int, int]
    total_contrib: tuple[int, int]
    raises: int
    checks: int
    histories: tuple[tuple[str, ...], tuple[str, ...]]
    awaiting_public: bool
    folded: int | None
    showdown: bool

    def __post_init__(self) -> None:
        if type(self.max_raises) is not int or self.max_raises < 0:
            raise ValueError("Leduc max raises must be a nonnegative exact integer")
        if self.public_card is not None and (
            type(self.public_card) is not int or self.public_card not in range(6)
        ):
            raise ValueError("Leduc public card must be physical card 0..5 or None")
        if type(self.round_index) is not int or self.round_index not in (0, 1):
            raise ValueError("Leduc round index must be 0/1")
        if type(self.to_act) is not int or self.to_act not in (0, 1):
            raise ValueError("Leduc to-act must be player 0/1")
        for name, pair in (
            ("round_contrib", self.round_contrib),
            ("total_contrib", self.total_contrib),
        ):
            if type(pair) is not tuple or len(pair) != 2 or any(
                type(value) is not int or value < 0 for value in pair
            ):
                raise ValueError(f"Leduc {name} must be a nonnegative integer pair")
        if type(self.raises) is not int or not 0 <= self.raises <= self.max_raises:
            raise ValueError("Leduc raise count is invalid")
        if type(self.checks) is not int or self.checks not in (0, 1):
            raise ValueError("Leduc check count is invalid")
        if type(self.histories) is not tuple or len(self.histories) != 2 or any(
            type(row) is not tuple or any(type(action) is not str for action in row)
            for row in self.histories
        ):
            raise TypeError("Leduc public histories must be two immutable string tuples")
        if type(self.awaiting_public) is not bool or type(self.showdown) is not bool:
            raise TypeError("Leduc terminal/chance flags must be exact bools")
        if self.folded is not None and (
            type(self.folded) is not int or self.folded not in (0, 1)
        ):
            raise ValueError("Leduc folded player must be 0/1 or None")
        if self.public_card is None and self.round_index != 0:
            raise ValueError("Leduc round one requires a public card")
        available = tuple(card for card in range(6) if card != self.public_card)
        state = self.instantiate(available[0], available[1])
        try:
            state.current_player
            if state.current_player >= 0:
                state.legal_actions()
            elif state.current_player == TERMINAL_PLAYER:
                state.returns()
        except (RuntimeError, ValueError) as exc:
            raise ValueError("Leduc public state is internally inconsistent") from exc

    @classmethod
    def from_state(cls, state: LeducState) -> "LeducPublicState":
        if type(state) is not LeducState or len(state.private_cards) != 2:
            raise TypeError("Leduc public projection requires a two-card concrete state")
        return cls(
            max_raises=state.max_raises,
            public_card=state.public_card,
            round_index=state.round_index,
            to_act=state.to_act,
            round_contrib=state.round_contrib,
            total_contrib=state.total_contrib,
            raises=state.raises,
            checks=state.checks,
            histories=state.histories,
            awaiting_public=state.awaiting_public,
            folded=state.folded,
            showdown=state.showdown,
        )

    @property
    def deck_size(self) -> int:
        return 6

    @property
    def blocked_cards(self) -> frozenset[int]:
        return frozenset() if self.public_card is None else frozenset({self.public_card})

    def instantiate(self, first: int, second: int) -> LeducState:
        if (
            type(first) is not int
            or type(second) is not int
            or first not in range(6)
            or second not in range(6)
            or first == second
            or first in self.blocked_cards
            or second in self.blocked_cards
        ):
            raise ValueError("Leduc private cards conflict with each other/public card")
        return LeducState(
            max_raises=self.max_raises,
            private_cards=(first, second),
            public_card=self.public_card,
            round_index=self.round_index,
            to_act=self.to_act,
            round_contrib=self.round_contrib,
            total_contrib=self.total_contrib,
            raises=self.raises,
            checks=self.checks,
            histories=self.histories,
            awaiting_public=self.awaiting_public,
            folded=self.folded,
            showdown=self.showdown,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "game": "leduc",
            "max_raises": self.max_raises,
            "public_card": self.public_card,
            "round_index": self.round_index,
            "to_act": self.to_act,
            "round_contrib": list(self.round_contrib),
            "total_contrib": list(self.total_contrib),
            "raises": self.raises,
            "checks": self.checks,
            "histories": [list(row) for row in self.histories],
            "awaiting_public": self.awaiting_public,
            "folded": self.folded,
            "showdown": self.showdown,
        }


ToyPublicState = KuhnPublicState | LeducPublicState


def _toy_range(
    values: tuple[float, ...],
    public_state: ToyPublicState,
) -> tuple[float, ...]:
    if type(values) is not tuple or len(values) != public_state.deck_size:
        raise TypeError("toy reach range has the wrong physical deck length")
    result: list[float] = []
    for index, value in enumerate(values):
        if type(value) not in (int, float):
            raise TypeError("toy reach weights must be exact JSON numbers")
        numeric = float(value)
        if not math.isfinite(numeric) or numeric < 0.0:
            raise ValueError("toy reach weights must be finite and nonnegative")
        if index in public_state.blocked_cards and numeric != 0.0:
            raise ValueError("public-card-blocked toy reach must be canonical zero")
        result.append(numeric)
    if not math.isclose(math.fsum(result), 1.0, rel_tol=1e-12, abs_tol=1e-12):
        raise ValueError("toy reach range must sum to one")
    return tuple(result)


@dataclass(frozen=True, slots=True)
class ToyRangeQuery:
    public_state: ToyPublicState
    private_ranges: tuple[tuple[float, ...], tuple[float, ...]]

    def __post_init__(self) -> None:
        if type(self.public_state) not in (KuhnPublicState, LeducPublicState):
            raise TypeError("toy query requires an exact Kuhn/Leduc public state")
        if type(self.private_ranges) is not tuple or len(self.private_ranges) != 2:
            raise TypeError("toy query requires two immutable reach ranges")
        object.__setattr__(
            self,
            "private_ranges",
            (
                _toy_range(self.private_ranges[0], self.public_state),
                _toy_range(self.private_ranges[1], self.public_state),
            ),
        )

    @property
    def valid_masks(self) -> tuple[tuple[bool, ...], tuple[bool, ...]]:
        result: list[tuple[bool, ...]] = []
        for player in (0, 1):
            opponent = self.private_ranges[1 - player]
            result.append(
                tuple(
                    card not in self.public_state.blocked_cards
                    and any(
                        weight > 0.0 and other != card
                        for other, weight in enumerate(opponent)
                    )
                    for card in range(self.public_state.deck_size)
                )
            )
        return result[0], result[1]

    @property
    def joint_compatible_mass(self) -> float:
        return math.fsum(
            self.private_ranges[0][first] * self.private_ranges[1][second]
            for first in range(self.public_state.deck_size)
            for second in range(self.public_state.deck_size)
            if first != second
            and first not in self.public_state.blocked_cards
            and second not in self.public_state.blocked_cards
        )

    @property
    def digest(self) -> str:
        return payload_sha256(
            {
                "schema": "route-b-m5-toy-range-query-v1",
                "public_state": self.public_state.to_payload(),
                "private_ranges": [list(row) for row in self.private_ranges],
            }
        )


@dataclass(frozen=True, slots=True)
class ToyCFVResult:
    values: tuple[tuple[float, ...], tuple[float, ...]]
    valid_masks: tuple[tuple[bool, ...], tuple[bool, ...]]
    zero_sum_residual: float
    pair_utility0: tuple[tuple[float, ...], ...]


def _action_probabilities(
    policy: ToyPolicy,
    information_state: str,
    legal_actions: tuple[ToyAction, ...],
) -> tuple[tuple[ToyAction, float], ...]:
    if not legal_actions:
        return ()
    if not policy:
        probability = 1.0 / len(legal_actions)
        return tuple((action, probability) for action in legal_actions)
    if information_state not in policy:
        raise ValueError("nonempty toy policy is missing a reached information state")
    row = policy[information_state]
    if set(row) != set(legal_actions):
        raise ValueError("toy policy row differs from exact legal actions")
    probabilities: list[tuple[ToyAction, float]] = []
    for action in legal_actions:
        value = row[action]
        if type(value) not in (int, float):
            raise TypeError("toy policy probabilities must be JSON numbers")
        probability = float(value)
        if not math.isfinite(probability) or probability < 0.0:
            raise ValueError("toy policy probabilities must be finite/nonnegative")
        probabilities.append((action, probability))
    if not math.isclose(
        math.fsum(probability for _, probability in probabilities),
        1.0,
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        raise ValueError("toy policy probabilities must sum to one")
    return tuple(probabilities)


def _continuation_evaluator(policy: ToyPolicy):
    @lru_cache(maxsize=None)
    def continuation(state: KuhnState | LeducState) -> tuple[float, float]:
        actor = state.current_player
        if actor == TERMINAL_PLAYER:
            return state.returns()
        if actor == CHANCE_PLAYER:
            outcomes = state.chance_outcomes()
            return (
                math.fsum(
                    probability * continuation(state.child(action))[0]
                    for action, probability in outcomes
                ),
                math.fsum(
                    probability * continuation(state.child(action))[1]
                    for action, probability in outcomes
                ),
            )
        legal = state.legal_actions()
        probabilities = _action_probabilities(
            policy,
            state.information_state_key(actor),
            legal,
        )
        return (
            math.fsum(
                probability * continuation(state.child(action))[0]
                for action, probability in probabilities
            ),
            math.fsum(
                probability * continuation(state.child(action))[1]
                for action, probability in probabilities
            ),
        )

    return continuation


def exact_toy_cfv(query: ToyRangeQuery, policy: ToyPolicy) -> ToyCFVResult:
    if type(query) is not ToyRangeQuery:
        raise TypeError("toy CFV requires exact ToyRangeQuery")
    continuation = _continuation_evaluator(policy)
    size = query.public_state.deck_size
    matrix = [[0.0] * size for _ in range(size)]
    for first in range(size):
        for second in range(size):
            if (
                first == second
                or first in query.public_state.blocked_cards
                or second in query.public_state.blocked_cards
            ):
                continue
            matrix[first][second] = continuation(
                query.public_state.instantiate(first, second)
            )[0]
    masks = query.valid_masks
    values0 = tuple(
        math.fsum(
            query.private_ranges[1][second] * matrix[first][second]
            for second in range(size)
            if second != first
        )
        if masks[0][first]
        else 0.0
        for first in range(size)
    )
    values1 = tuple(
        math.fsum(
            -query.private_ranges[0][first] * matrix[first][second]
            for first in range(size)
            if first != second
        )
        if masks[1][second]
        else 0.0
        for second in range(size)
    )
    residual = math.fsum(
        query.private_ranges[0][index] * values0[index]
        + query.private_ranges[1][index] * values1[index]
        for index in range(size)
    )
    if abs(residual) > 1e-10:
        raise ValueError("exact toy CFV violated range-weighted zero sum")
    return ToyCFVResult(
        values=(values0, values1),
        valid_masks=masks,
        zero_sum_residual=residual,
        pair_utility0=tuple(tuple(row) for row in matrix),
    )


def raw_bilinear_value0(
    pair_utility0: tuple[tuple[float, ...], ...],
    raw_range0: tuple[float, ...],
    raw_range1: tuple[float, ...],
) -> float:
    size = len(pair_utility0)
    if (
        type(raw_range0) is not tuple
        or type(raw_range1) is not tuple
        or len(raw_range0) != size
        or len(raw_range1) != size
        or any(len(row) != size for row in pair_utility0)
    ):
        raise TypeError("raw bilinear value dimensions differ")
    return math.fsum(
        float(raw_range0[first])
        * float(raw_range1[second])
        * pair_utility0[first][second]
        for first in range(size)
        for second in range(size)
    )


def conditioned_profile_value0(query: ToyRangeQuery, result: ToyCFVResult) -> float:
    mass = query.joint_compatible_mass
    if mass <= 0.0:
        raise ValueError("toy query has no compatible joint reach")
    raw = math.fsum(
        query.private_ranges[0][index] * result.values[0][index]
        for index in range(query.public_state.deck_size)
    )
    return raw / mass


@dataclass(frozen=True, slots=True)
class ToyOneStepResult:
    actor: int
    actions: tuple[ToyAction, ...]
    action_values: Mapping[ToyAction, tuple[float, ...]]
    policy_values: tuple[float, ...]
    regrets: Mapping[ToyAction, tuple[float, ...]]
    greedy_actions: tuple[ToyAction | None, ...]
    greedy_profile_value: float


def exact_one_step_cutoff(query: ToyRangeQuery, policy: ToyPolicy) -> ToyOneStepResult:
    continuation = _continuation_evaluator(policy)
    size = query.public_state.deck_size
    representative_actor: int | None = None
    actions: tuple[ToyAction, ...] | None = None
    pair_action_values: dict[ToyAction, list[list[float]]] = {}
    for first in range(size):
        for second in range(size):
            if (
                first == second
                or first in query.public_state.blocked_cards
                or second in query.public_state.blocked_cards
            ):
                continue
            state = query.public_state.instantiate(first, second)
            actor = state.current_player
            if actor not in (0, 1):
                raise ValueError("one-step cutoff requires a public decision node")
            legal = state.legal_actions()
            if representative_actor is None:
                representative_actor = actor
                actions = legal
                pair_action_values = {
                    action: [[0.0] * size for _ in range(size)] for action in legal
                }
            if actor != representative_actor or legal != actions:
                raise ValueError("toy public node changed actor/actions by private deal")
            for action in legal:
                pair_action_values[action][first][second] = continuation(
                    state.child(action)
                )[actor]
    if representative_actor is None or actions is None:
        raise ValueError("toy query has no compatible decision deal")
    actor = representative_actor
    masks = query.valid_masks[actor]
    action_values: dict[ToyAction, tuple[float, ...]] = {}
    for action in actions:
        matrix = pair_action_values[action]
        if actor == 0:
            row = tuple(
                math.fsum(
                    query.private_ranges[1][second] * matrix[first][second]
                    for second in range(size)
                    if second != first
                )
                if masks[first]
                else 0.0
                for first in range(size)
            )
        else:
            row = tuple(
                math.fsum(
                    query.private_ranges[0][first] * matrix[first][second]
                    for first in range(size)
                    if first != second
                )
                if masks[second]
                else 0.0
                for second in range(size)
            )
        action_values[action] = row

    policy_values: list[float] = [0.0] * size
    greedy_actions: list[ToyAction | None] = [None] * size
    for own_card in range(size):
        if not masks[own_card]:
            continue
        other_card = next(
            card
            for card, weight in enumerate(query.private_ranges[1 - actor])
            if weight > 0.0 and card != own_card
        )
        state = (
            query.public_state.instantiate(own_card, other_card)
            if actor == 0
            else query.public_state.instantiate(other_card, own_card)
        )
        probabilities = dict(
            _action_probabilities(policy, state.information_state_key(actor), actions)
        )
        policy_values[own_card] = math.fsum(
            probabilities[action] * action_values[action][own_card]
            for action in actions
        )
        greedy_actions[own_card] = max(
            actions,
            key=lambda action: (action_values[action][own_card], repr(action)),
        )
    regrets = {
        action: tuple(
            action_values[action][card] - policy_values[card]
            if masks[card]
            else 0.0
            for card in range(size)
        )
        for action in actions
    }
    greedy_value = math.fsum(
        query.private_ranges[actor][card]
        * max(action_values[action][card] for action in actions)
        for card in range(size)
        if masks[card]
    )
    return ToyOneStepResult(
        actor=actor,
        actions=actions,
        action_values=action_values,
        policy_values=tuple(policy_values),
        regrets=regrets,
        greedy_actions=tuple(greedy_actions),
        greedy_profile_value=greedy_value,
    )
