"""Independent full-tree LCFR differential oracle for M5b.

This small-game implementation shares only the exact clean-room Kuhn/Leduc
rules.  Its fixed checkpoint digests provide a pre-HUNL guard for stable
infoset identity, linear weighting, regret updates and average policies.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable, Hashable

from ..common_runtime import kuhn, leduc
from .m5b_contract import canonical_bytes


FROZEN_FOUR_ITERATION_DIGESTS = {
    "kuhn": "15bc1b64a0c1d8cbe801e1a5f25a5e0c0899221fa1ca5e9883f90ad1e7a3fe46",
    "leduc": "9cbf5504ddd994aedf2700aba92441795c5f5deadbd73e2b191e15ec83515f3a",
}


@dataclass(frozen=True, slots=True)
class _Game:
    name: str
    deals: tuple[Any, ...]
    infosets: tuple[Hashable, ...]
    actions_by_infoset: dict[Hashable, tuple[str, ...]]
    initial: Callable[[], Any]
    terminal: Callable[[Any], bool]
    actor: Callable[[Any], int]
    infoset: Callable[[Any, Any], Hashable]
    legal: Callable[[Any], tuple[str, ...]]
    apply: Callable[[Any, str], Any]
    utility: Callable[[Any, Any, int], float]


def _kuhn_game() -> _Game:
    infosets = kuhn.all_infosets()
    actions = {key: kuhn.legal_actions(key[2]) for key in infosets}
    return _Game(
        "kuhn",
        kuhn.ordered_deals(),
        infosets,
        actions,
        lambda: "",
        kuhn.is_terminal,
        kuhn.current_player,
        lambda history, deal: (kuhn.current_player(history), deal[kuhn.current_player(history)], history),
        kuhn.legal_actions,
        kuhn.next_history,
        lambda history, deal, player: kuhn.terminal_utility(deal, history, player),
    )


def _leduc_game() -> _Game:
    infosets = leduc.all_infosets()
    return _Game(
        "leduc",
        leduc.ordered_deals(),
        infosets,
        leduc.actions_by_infoset(),
        leduc.initial_state,
        lambda state: state.terminal,
        lambda state: state.actor,
        leduc.information_set,
        leduc.legal_actions,
        leduc.apply_action,
        lambda state, deal, player: leduc.terminal_utility(state, deal, player),
    )


class ExactLinearCFRGate:
    """Frozen-policy alternating full-tree Linear CFR."""

    def __init__(self, game: str) -> None:
        if game == "kuhn":
            self.game = _kuhn_game()
        elif game == "leduc":
            self.game = _leduc_game()
        else:
            raise ValueError("exact M5b gate supports only Kuhn and Leduc")
        self.iterations_completed = 0
        self.regrets = {
            key: [0.0] * len(self.game.actions_by_infoset[key])
            for key in self.game.infosets
        }
        self.strategy_sums = {
            key: [0.0] * len(self.game.actions_by_infoset[key])
            for key in self.game.infosets
        }

    def current_strategy(self, key: Hashable) -> tuple[float, ...]:
        positive = [max(0.0, value) for value in self.regrets[key]]
        total = sum(positive)
        if total > 0.0:
            return tuple(value / total for value in positive)
        return tuple(1.0 / len(positive) for _ in positive)

    def _traverse(
        self,
        state: Any,
        deal: Any,
        reaches: tuple[float, float],
        chance_reach: float,
        update_player: int,
        frozen: dict[Hashable, tuple[float, ...]],
        regret_delta: dict[Hashable, list[float]],
        average_delta: dict[Hashable, list[float]],
    ) -> float:
        if self.game.terminal(state):
            return self.game.utility(state, deal, update_player)
        actor = self.game.actor(state)
        key = self.game.infoset(state, deal)
        actions = self.game.legal(state)
        strategy = frozen[key]
        if actor == update_player:
            for slot, probability in enumerate(strategy):
                average_delta[key][slot] += (
                    chance_reach * reaches[actor] * probability
                )
        action_values: list[float] = []
        node_value = 0.0
        for slot, action in enumerate(actions):
            next_reaches = list(reaches)
            next_reaches[actor] *= strategy[slot]
            value = self._traverse(
                self.game.apply(state, action),
                deal,
                (next_reaches[0], next_reaches[1]),
                chance_reach,
                update_player,
                frozen,
                regret_delta,
                average_delta,
            )
            action_values.append(value)
            node_value += strategy[slot] * value
        if actor == update_player:
            counterfactual_reach = chance_reach * reaches[1 - actor]
            for slot, value in enumerate(action_values):
                regret_delta[key][slot] += counterfactual_reach * (
                    value - node_value
                )
        return node_value

    def train(self, iterations: int) -> None:
        if type(iterations) is not int or iterations < 0:
            raise ValueError("iterations must be a non-negative integer")
        chance = 1.0 / len(self.game.deals)
        for _ in range(iterations):
            iteration = self.iterations_completed + 1
            for update_player in (0, 1):
                frozen = {
                    key: self.current_strategy(key) for key in self.game.infosets
                }
                regret_delta = {
                    key: [0.0] * len(self.regrets[key]) for key in self.game.infosets
                }
                average_delta = {
                    key: [0.0] * len(self.strategy_sums[key])
                    for key in self.game.infosets
                }
                for deal in self.game.deals:
                    self._traverse(
                        self.game.initial(),
                        deal,
                        (1.0, 1.0),
                        chance,
                        update_player,
                        frozen,
                        regret_delta,
                        average_delta,
                    )
                for key in self.game.infosets:
                    for slot in range(len(self.regrets[key])):
                        self.regrets[key][slot] += iteration * regret_delta[key][slot]
                        self.strategy_sums[key][slot] += (
                            iteration * average_delta[key][slot]
                        )
            self.iterations_completed = iteration

    def average_strategy(self) -> dict[Hashable, dict[str, float]]:
        result: dict[Hashable, dict[str, float]] = {}
        for key in self.game.infosets:
            values = self.strategy_sums[key]
            total = sum(values)
            probabilities = (
                [value / total for value in values]
                if total > 0.0
                else [1.0 / len(values)] * len(values)
            )
            result[key] = dict(
                zip(self.game.actions_by_infoset[key], probabilities, strict=True)
            )
        return result

    @staticmethod
    def _encode_kuhn(key: Hashable) -> str:
        player, card, history = key
        return f"{player}|{card}|{history}"

    @staticmethod
    def _encode_leduc(key: Hashable) -> str:
        player, private_rank, public_rank, history = key
        return f"{player}|{private_rank}|{public_rank}|{history}"

    def checkpoint_payload(self) -> dict[str, object]:
        encode = self._encode_kuhn if self.game.name == "kuhn" else self._encode_leduc
        common = {
            "iterations_completed": self.iterations_completed,
            "regrets": {
                encode(key): list(self.regrets[key]) for key in self.game.infosets
            },
            "strategy_sums": {
                encode(key): list(self.strategy_sums[key]) for key in self.game.infosets
            },
        }
        if self.game.name == "kuhn":
            return {
                "format": "route-a-kuhn-lcfr-v1",
                "game": "kuhn_poker",
                "algorithm": "alternating_linear_cfr",
                **common,
            }
        return {
            "format": "route-a-leduc-lcfr-v1",
            "game": "limit_leduc_clean_room_v1",
            "algorithm": "alternating_linear_cfr",
            "fidelity": {
                "lcfr": "paper-faithful-clean-room-algorithm",
                "decisionholdem_blueprint": "unresolved-lcfr-vs-mccfr-gap",
            },
            **common,
        }

    def checkpoint_digest(self) -> str:
        return hashlib.sha256(canonical_bytes(self.checkpoint_payload())).hexdigest()

    def assert_frozen_four_iteration_differential(self) -> dict[str, object]:
        if self.iterations_completed != 4:
            raise ValueError("frozen exact differential requires four iterations")
        digest = self.checkpoint_digest()
        if digest != FROZEN_FOUR_ITERATION_DIGESTS[self.game.name]:
            raise ValueError(f"{self.game.name} exact LCFR differential failed")
        return {
            "game": self.game.name,
            "iterations": 4,
            "infoset_count": len(self.game.infosets),
            "checkpoint_sha256": digest,
            "stable_infoset_identity": True,
            "linear_regret_and_average_weighting": True,
        }
