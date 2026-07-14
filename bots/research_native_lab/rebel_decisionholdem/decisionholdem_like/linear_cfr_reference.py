"""Independent equation-oriented Kuhn LCFR reference.

This intentionally does not share the production solver's recursive traversal.
For each information set it reconstructs player reaches from the public path,
then applies the defining equations directly:

``R_t(I,a) += t * pi_-i(I) * (v(I,a) - v(I))``
``S_t(I,a) += t * pi_i(I) * sigma(I,a)``

Chance reach is included in both accumulators.  The implementation is small and
slow by design; it is a differential oracle for M3, not a training backend.
"""

from __future__ import annotations

from ..common_runtime.kuhn import (
    InfoSet,
    all_infosets,
    current_player,
    is_terminal,
    legal_actions,
    next_history,
    ordered_deals,
    terminal_utility,
)


def _regret_matching(regrets: list[float]) -> tuple[float, ...]:
    positive = [max(0.0, value) for value in regrets]
    total = sum(positive)
    if total > 0.0:
        return tuple(value / total for value in positive)
    return tuple(1.0 / len(positive) for _ in positive)


def _continuation(
    deal: tuple[int, int],
    history: str,
    profile: dict[InfoSet, tuple[float, ...]],
    player: int,
) -> float:
    if is_terminal(history):
        return terminal_utility(deal, history, player)
    actor = current_player(history)
    key = (actor, deal[actor], history)
    return sum(
        probability * _continuation(deal, next_history(history, action), profile, player)
        for action, probability in zip(
            legal_actions(history), profile[key], strict=True
        )
    )


def _path_reaches(
    deal: tuple[int, int],
    target_history: str,
    profile: dict[InfoSet, tuple[float, ...]],
) -> tuple[float, float]:
    reaches = [1.0, 1.0]
    history = ""
    if target_history == "":
        return 1.0, 1.0
    for action in target_history.split("-"):
        actor = current_player(history)
        actions = legal_actions(history)
        index = actions.index(action)
        reaches[actor] *= profile[(actor, deal[actor], history)][index]
        history = next_history(history, action)
    if history != target_history:
        raise AssertionError("Kuhn public path reconstruction diverged")
    return reaches[0], reaches[1]


class EquationLinearCFRReference:
    """Small, independent full-tree alternating-LCFR differential oracle."""

    def __init__(self) -> None:
        self.iterations_completed = 0
        self.regrets = {
            key: [0.0] * len(legal_actions(key[2])) for key in all_infosets()
        }
        self.strategy_sums = {
            key: [0.0] * len(legal_actions(key[2])) for key in all_infosets()
        }

    def _profile(self) -> dict[InfoSet, tuple[float, ...]]:
        return {
            key: _regret_matching(self.regrets[key]) for key in all_infosets()
        }

    def train(self, iterations: int) -> None:
        if type(iterations) is not int or iterations < 0:
            raise ValueError("iterations must be a non-negative integer")
        chance = 1.0 / len(ordered_deals())
        for _ in range(iterations):
            iteration = self.iterations_completed + 1
            for update_player in (0, 1):
                profile = self._profile()
                regret_delta = {
                    key: [0.0] * len(self.regrets[key]) for key in all_infosets()
                }
                average_delta = {
                    key: [0.0] * len(self.strategy_sums[key])
                    for key in all_infosets()
                }
                for key in all_infosets():
                    actor, private_card, history = key
                    if actor != update_player:
                        continue
                    actions = legal_actions(history)
                    strategy = profile[key]
                    for deal in ordered_deals():
                        if deal[actor] != private_card:
                            continue
                        reaches = _path_reaches(deal, history, profile)
                        for index, probability in enumerate(strategy):
                            average_delta[key][index] += (
                                chance * reaches[actor] * probability
                            )
                        action_values = [
                            _continuation(
                                deal,
                                next_history(history, action),
                                profile,
                                update_player,
                            )
                            for action in actions
                        ]
                        node_value = sum(
                            probability * value
                            for probability, value in zip(
                                strategy, action_values, strict=True
                            )
                        )
                        opponent_reach = reaches[1 - actor]
                        for index, value in enumerate(action_values):
                            regret_delta[key][index] += (
                                chance * opponent_reach * (value - node_value)
                            )
                weight = float(iteration)
                for key in all_infosets():
                    for index in range(len(self.regrets[key])):
                        self.regrets[key][index] += weight * regret_delta[key][index]
                        self.strategy_sums[key][index] += (
                            weight * average_delta[key][index]
                        )
            self.iterations_completed = iteration
