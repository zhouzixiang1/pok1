"""Exact policy value, best response, NashConv, and exploitability."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from typing import Mapping

from ..core.game import Action, CHANCE_PLAYER, ExtensiveGame, GameState, TERMINAL_PLAYER

BehaviorPolicy = Mapping[str, Mapping[Action, float]]
NUMERICAL_TOLERANCE = 1e-12


def information_state_action_schema(
    game: ExtensiveGame,
) -> dict[str, tuple[Action, ...]]:
    """Collect the complete information-state/action schema of a finite game."""

    result: dict[str, tuple[Action, ...]] = {}

    def visit(state: GameState) -> None:
        actor = state.current_player
        if actor == TERMINAL_PLAYER:
            return
        if actor == CHANCE_PLAYER:
            for action, _ in state.chance_outcomes():
                visit(state.child(action))
            return
        key = state.information_state_key(actor)
        legal = state.legal_actions()
        previous = result.get(key)
        if previous is not None and previous != legal:
            raise ValueError(f"inconsistent legal actions in information state {key}")
        result[key] = legal
        for action in legal:
            visit(state.child(action))

    visit(game.new_initial_state())
    return result


def validate_behavior_policy(game: ExtensiveGame, policy: BehaviorPolicy) -> None:
    """Require a complete exact profile, except for explicit ``{}`` uniform."""

    if not policy:
        return
    schema = information_state_action_schema(game)
    if set(policy) != set(schema):
        missing = set(schema) - set(policy)
        unknown = set(policy) - set(schema)
        raise ValueError(
            "exact policy information-state mismatch: "
            f"missing={sorted(missing, key=repr)!r}, "
            f"unknown={sorted(unknown, key=repr)!r}"
        )
    for key, legal in schema.items():
        action_probabilities(policy, key, legal)


def action_probabilities(
    policy: BehaviorPolicy,
    information_state: str,
    legal_actions: tuple[Action, ...],
) -> dict[Action, float]:
    """Validate an exact legal distribution, with absent-row uniform fallback.

    Only ``policy == {}`` is the explicit uniform-profile shorthand used by
    the small-game tests.  Every nonempty profile must contain every reached
    information-state row.  A row is never clipped or renormalized: every
    legal action must be present exactly once, no unknown action is accepted,
    and finite nonnegative probabilities must sum to one.  This keeps a bad
    checkpoint or policy export from acquiring a plausible-looking "exact"
    exploitability after silent repair.
    """

    if not legal_actions:
        return {}
    if not policy:
        probability = 1.0 / len(legal_actions)
        return {action: probability for action in legal_actions}
    if information_state not in policy:
        raise ValueError(
            f"nonempty exact policy is missing row {information_state!r}"
        )
    raw = policy[information_state]
    if set(raw) != set(legal_actions):
        missing = set(legal_actions) - set(raw)
        unknown = set(raw) - set(legal_actions)
        raise ValueError(
            f"policy row {information_state!r} action mismatch: "
            f"missing={sorted(missing, key=repr)!r}, "
            f"unknown={sorted(unknown, key=repr)!r}"
        )
    if any(type(raw[action]) not in (int, float) for action in legal_actions):
        raise TypeError(
            f"policy row {information_state!r} probabilities must be JSON numbers, "
            "not bool/string"
        )
    probabilities = {action: float(raw[action]) for action in legal_actions}
    if any(not math.isfinite(value) for value in probabilities.values()):
        raise ValueError(f"policy row {information_state!r} contains non-finite values")
    if any(value < 0.0 for value in probabilities.values()):
        raise ValueError(f"policy row {information_state!r} contains negative values")
    total = math.fsum(probabilities.values())
    if not math.isclose(total, 1.0, rel_tol=1e-12, abs_tol=1e-12):
        raise ValueError(
            f"policy row {information_state!r} sums to {total!r}, expected 1"
        )
    return probabilities


def expected_returns(game: ExtensiveGame, policy: BehaviorPolicy) -> tuple[float, float]:
    """Compute exact expected returns under a behavioral policy profile."""

    validate_behavior_policy(game, policy)

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
    validate_behavior_policy(game, policy)

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
    raw_player_improvements: tuple[float, float]
    numerical_tolerance_clamped: bool
    nash_conv: float
    exploitability: float


def exploitability(game: ExtensiveGame, policy: BehaviorPolicy) -> ExploitabilityResult:
    """Return exact two-player zero-sum exploitability diagnostics."""

    on_policy = expected_returns(game, policy)
    responses = (best_response(game, policy, 0), best_response(game, policy, 1))
    br_values = (responses[0].value, responses[1].value)
    raw_improvements = (
        br_values[0] - on_policy[0],
        br_values[1] - on_policy[1],
    )
    if any(value < -NUMERICAL_TOLERANCE for value in raw_improvements):
        raise RuntimeError(
            "best-response improvement is materially negative; exact evaluator "
            f"is inconsistent: {raw_improvements!r}"
        )
    improvements = tuple(
        0.0 if value < 0.0 else value for value in raw_improvements
    )
    nash_conv = math.fsum(improvements)
    if not math.isfinite(nash_conv) or nash_conv < 0.0:
        raise RuntimeError("exact NashConv must be finite and nonnegative")
    return ExploitabilityResult(
        on_policy_returns=on_policy,
        best_response_values=br_values,
        player_improvements=improvements,
        raw_player_improvements=raw_improvements,
        numerical_tolerance_clamped=(improvements != raw_improvements),
        nash_conv=nash_conv,
        exploitability=nash_conv / 2.0,
    )
