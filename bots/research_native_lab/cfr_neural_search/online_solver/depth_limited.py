"""Fail-closed depth-limited small-game wrapper.

This module is a correctness scaffold for later public-tree resolving.  A
cutoff state becomes a terminal node whose two-player zero-sum payoff is
supplied by an explicit leaf evaluator.  It deliberately does not claim that
a full-state tabular leaf is a deployable HUNL counterfactual-value network.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Callable

from ..blueprint.evaluation import BehaviorPolicy, action_probabilities
from ..core.game import Action, CHANCE_PLAYER, ExtensiveGame, GameState, TERMINAL_PLAYER

LeafEvaluator = Callable[[GameState], tuple[float, float]]


@dataclass(frozen=True, slots=True)
class LeafValueContract:
    """Explicit identity plus callable used in solver/checkpoint binding."""

    identity: str
    evaluator: LeafEvaluator

    def __post_init__(self) -> None:
        if not self.identity or any(character.isspace() for character in self.identity):
            raise ValueError("leaf identity must be nonempty and contain no whitespace")

    def __call__(self, state: GameState) -> tuple[float, float]:
        return self.evaluator(state)


def _validated_leaf_value(value: tuple[float, float]) -> tuple[float, float]:
    if not isinstance(value, tuple) or len(value) != 2:
        raise ValueError("leaf evaluator must return a two-player tuple")
    normalized = (float(value[0]), float(value[1]))
    if not all(math.isfinite(item) for item in normalized):
        raise ValueError("leaf evaluator returned a non-finite value")
    if abs(normalized[0] + normalized[1]) > 1e-12:
        raise ValueError("leaf evaluator must be zero sum")
    return normalized


def policy_value_from_state(
    state: GameState,
    policy: BehaviorPolicy,
) -> tuple[float, float]:
    """Return exact continuation value from an arbitrary small-game state."""

    actor = state.current_player
    if actor == TERMINAL_PLAYER:
        return state.returns()
    if actor == CHANCE_PLAYER:
        values = [0.0, 0.0]
        for action, probability in state.chance_outcomes():
            child_value = policy_value_from_state(state.child(action), policy)
            values[0] += probability * child_value[0]
            values[1] += probability * child_value[1]
        return (values[0], values[1])

    key = state.information_state_key(actor)
    probabilities = action_probabilities(policy, key, state.legal_actions())
    values = [0.0, 0.0]
    for action, probability in probabilities.items():
        child_value = policy_value_from_state(state.child(action), policy)
        values[0] += probability * child_value[0]
        values[1] += probability * child_value[1]
    return (values[0], values[1])


def rollout_leaf(
    policy: BehaviorPolicy,
    *,
    label: str,
) -> LeafValueContract:
    """Build a snapshotted, hash-bound exact blueprint-rollout leaf."""

    if not label or any(character.isspace() for character in label):
        raise ValueError("rollout leaf label must be nonempty and contain no whitespace")
    snapshot: dict[str, dict[str, float]] = {}
    for key, vector in policy.items():
        if not isinstance(key, str):
            raise TypeError("rollout leaf policy information-state keys must be strings")
        normalized: dict[str, float] = {}
        for action, value in vector.items():
            if not isinstance(action, str):
                raise TypeError("rollout leaf policy actions must be strings")
            probability = float(value)
            if not math.isfinite(probability) or probability < 0.0:
                raise ValueError("rollout leaf policy weights must be finite and nonnegative")
            normalized[action] = probability
        snapshot[key] = normalized
    encoded = json.dumps(
        snapshot,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    identity = f"{label}:sha256:{hashlib.sha256(encoded).hexdigest()}"

    def evaluate(state: GameState) -> tuple[float, float]:
        return policy_value_from_state(state, snapshot)

    return LeafValueContract(identity=identity, evaluator=evaluate)


@dataclass(frozen=True, slots=True)
class DepthLimitedState:
    """State wrapper that turns the configured frontier into terminals."""

    state: GameState
    remaining_depth: int
    leaf: LeafValueContract

    def __post_init__(self) -> None:
        if self.remaining_depth < 0:
            raise ValueError("remaining_depth must be nonnegative")

    @property
    def depth(self) -> int:
        return self.state.depth

    @property
    def current_player(self) -> int:
        if self.state.current_player == TERMINAL_PLAYER or self.remaining_depth == 0:
            return TERMINAL_PLAYER
        return self.state.current_player

    def chance_outcomes(self) -> tuple[tuple[Action, float], ...]:
        if self.current_player != CHANCE_PLAYER:
            raise ValueError("not a chance node")
        return self.state.chance_outcomes()

    def legal_actions(self) -> tuple[Action, ...]:
        if self.current_player < 0:
            return ()
        return self.state.legal_actions()

    def child(self, action: Action) -> "DepthLimitedState":
        if self.current_player == TERMINAL_PLAYER:
            raise ValueError("terminal depth-limited state has no children")
        return DepthLimitedState(
            state=self.state.child(action),
            remaining_depth=self.remaining_depth - 1,
            leaf=self.leaf,
        )

    def information_state_key(self, player: int) -> str:
        if self.current_player < 0:
            raise ValueError("terminal depth-limited state has no information state")
        return self.state.information_state_key(player)

    def returns(self) -> tuple[float, float]:
        if self.current_player != TERMINAL_PLAYER:
            raise ValueError("returns requested from non-terminal depth-limited state")
        if self.state.current_player == TERMINAL_PLAYER:
            return self.state.returns()
        return _validated_leaf_value(self.leaf(self.state))


@dataclass(frozen=True, slots=True)
class DepthLimitedGame:
    """Finite game view cut after ``max_depth`` chance/decision edges."""

    game: ExtensiveGame
    max_depth: int
    leaf: LeafValueContract

    def __post_init__(self) -> None:
        if self.max_depth < 0:
            raise ValueError("max_depth must be nonnegative")

    @property
    def name(self) -> str:
        return f"{self.game.name}:depth={self.max_depth}:leaf={self.leaf.identity}"

    def new_initial_state(self) -> DepthLimitedState:
        return DepthLimitedState(
            state=self.game.new_initial_state(),
            remaining_depth=self.max_depth,
            leaf=self.leaf,
        )
