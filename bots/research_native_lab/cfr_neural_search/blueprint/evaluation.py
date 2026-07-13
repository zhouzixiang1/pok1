"""Exact policy value, best response, NashConv, and exploitability."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from typing import Mapping

from ..core.game import Action, CHANCE_PLAYER, ExtensiveGame, GameState, TERMINAL_PLAYER

BehaviorPolicy = Mapping[str, Mapping[Action, float]]


def action_probabilities(
    policy: BehaviorPolicy,
    information_state: str,
    legal_actions: tuple[Action, ...],
) -> dict[Action, float]:
    """Return a normalized legal distribution, with uniform fallback."""

    if not legal_actions:
        return {}
    raw = policy.get(information_state, {})
    nonnegative = {action: max(0.0, float(raw.get(action, 0.0))) for action in legal_actions}
    total = sum(nonnegative.values())
    if total <= 0.0:
        probability = 1.0 / len(legal_actions)
        return {action: probability for action in legal_actions}
    return {action: value / total for action, value in nonnegative.items()}


def expected_returns(game: ExtensiveGame, policy: BehaviorPolicy) -> tuple[float, float]:
    """Compute exact expected returns under a behavioral policy profile."""

    @lru_cache(maxsize=None)
    def value(state: GameState) -> tuple[float, float]:
        player = state.current_player
        if player == TERMINAL_PLAYER:
            return state.returns()
        if player == CHANCE_PLAYER:
            result = [0.0, 0.0]
            for action, probability in state.chance_outcomes():
                child_value = value(state.child(action))
                result[0] += probability * child_value[0]
                result[1] += probability * child_value[1]
            return (result[0], result[1])
        key = state.information_state_key(player)
        probabilities = action_probabilities(policy, key, state.legal_actions())
        result = [0.0, 0.0]
        for action, probability in probabilities.items():
            child_value = value(state.child(action))
            result[0] += probability * child_value[0]
            result[1] += probability * child_value[1]
        return (result[0], result[1])

    return value(game.new_initial_state())


@dataclass(frozen=True, slots=True)
class BestResponseResult:
    player: int
    value: float
    actions: dict[str, Action]
    counterfactual_values: dict[str, float]


def best_response(
    game: ExtensiveGame,
    policy: BehaviorPolicy,
    player: int,
) -> BestResponseResult:
    """Compute an exact pure best response for a two-player perfect-recall game.

    Information-set states are weighted by opponent-and-chance reach only.
    Best actions are solved lazily from deeper information sets to shallower
    ones, enforcing one action across every history in an information set.
    The result also exposes each reached information set's unnormalized
    counterfactual best-response value under that same pure response.
    """

    if player not in (0, 1):
        raise ValueError("best response player must be 0 or 1")

    infosets: dict[str, list[tuple[GameState, float]]] = defaultdict(list)

    def collect(state: GameState, counterfactual_reach: float) -> None:
        actor = state.current_player
        if actor == TERMINAL_PLAYER:
            return
        if actor == CHANCE_PLAYER:
            for action, probability in state.chance_outcomes():
                collect(state.child(action), counterfactual_reach * probability)
            return
        if actor == player:
            key = state.information_state_key(player)
            infosets[key].append((state, counterfactual_reach))
            for action in state.legal_actions():
                collect(state.child(action), counterfactual_reach)
            return
        key = state.information_state_key(actor)
        probabilities = action_probabilities(policy, key, state.legal_actions())
        for action, probability in probabilities.items():
            if probability > 0.0:
                collect(state.child(action), counterfactual_reach * probability)

    collect(game.new_initial_state(), 1.0)
    chosen_actions: dict[str, Action] = {}

    @lru_cache(maxsize=None)
    def continuation_value(state: GameState) -> float:
        actor = state.current_player
        if actor == TERMINAL_PLAYER:
            return state.returns()[player]
        if actor == CHANCE_PLAYER:
            return sum(
                probability * continuation_value(state.child(action))
                for action, probability in state.chance_outcomes()
            )
        if actor == player:
            key = state.information_state_key(player)
            return continuation_value(state.child(best_action(key)))
        key = state.information_state_key(actor)
        probabilities = action_probabilities(policy, key, state.legal_actions())
        return sum(
            probability * continuation_value(state.child(action))
            for action, probability in probabilities.items()
        )

    def best_action(key: str) -> Action:
        if key in chosen_actions:
            return chosen_actions[key]
        states = infosets[key]
        legal = states[0][0].legal_actions()
        if any(state.legal_actions() != legal for state, _ in states):
            raise ValueError(f"inconsistent legal actions in information set {key}")
        action_values: dict[Action, float] = {}
        for action in legal:
            action_values[action] = sum(
                reach * continuation_value(state.child(action))
                for state, reach in states
            )
        selected = max(legal, key=lambda action: (action_values[action], repr(action)))
        chosen_actions[key] = selected
        return selected

    root_value = continuation_value(game.new_initial_state())
    counterfactual_values = {
        key: math.fsum(reach * continuation_value(state) for state, reach in states)
        for key, states in sorted(infosets.items())
    }
    return BestResponseResult(
        player=player,
        value=root_value,
        actions=chosen_actions,
        counterfactual_values=counterfactual_values,
    )


@dataclass(frozen=True, slots=True)
class ExploitabilityResult:
    on_policy_returns: tuple[float, float]
    best_response_values: tuple[float, float]
    player_improvements: tuple[float, float]
    nash_conv: float
    exploitability: float


def exploitability(game: ExtensiveGame, policy: BehaviorPolicy) -> ExploitabilityResult:
    """Return exact two-player zero-sum exploitability diagnostics."""

    on_policy = expected_returns(game, policy)
    responses = (best_response(game, policy, 0), best_response(game, policy, 1))
    br_values = (responses[0].value, responses[1].value)
    improvements = (
        br_values[0] - on_policy[0],
        br_values[1] - on_policy[1],
    )
    nash_conv = improvements[0] + improvements[1]
    if nash_conv < 0.0 and abs(nash_conv) < 1e-12:
        nash_conv = 0.0
    return ExploitabilityResult(
        on_policy_returns=on_policy,
        best_response_values=br_values,
        player_improvements=improvements,
        nash_conv=nash_conv,
        exploitability=nash_conv / 2.0,
    )
