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
from pathlib import Path
from typing import Any, Callable, Mapping

from ..blueprint.evaluation import (
    BehaviorPolicy,
    action_probabilities,
    information_state_action_schema,
    validate_behavior_policy,
)
from ..core.game import Action, CHANCE_PLAYER, ExtensiveGame, GameState, TERMINAL_PLAYER
from ..core.identity import (
    GAME_IDENTITY_SCHEMA,
    file_sha256,
    game_identity_sha256,
    payload_sha256,
)

LeafEvaluator = Callable[[GameState], tuple[float, float]]


class LeafValueContract:
    """Sealed exact-rollout leaf used in solver/checkpoint binding.

    Caller-asserted text cannot content-bind an arbitrary lambda or model, so
    M3 exposes no public arbitrary-callable constructor.  ``rollout_leaf`` is
    the sole authority: it validates, snapshots, and hashes a complete policy.
    A future neural artifact needs a separate signed content receipt.
    """

    __slots__ = ("_identity", "_game_binding", "_evaluator", "_sealed")

    def __init__(
        self,
        identity: str,
        game_binding: str,
        evaluator: LeafEvaluator,
        *,
        _authority: object | None = None,
    ) -> None:
        if _authority is not _ROLLOUT_LEAF_AUTHORITY:
            raise PermissionError("LeafValueContract must be created by rollout_leaf")
        if not identity or any(character.isspace() for character in identity):
            raise ValueError("leaf identity must be nonempty and contain no whitespace")
        object.__setattr__(self, "_identity", identity)
        object.__setattr__(self, "_game_binding", game_binding)
        object.__setattr__(self, "_evaluator", evaluator)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("LeafValueContract is immutable")
        object.__setattr__(self, name, value)

    @property
    def identity(self) -> str:
        return self._identity

    @property
    def game_binding(self) -> str:
        return self._game_binding

    def __call__(self, state: GameState) -> tuple[float, float]:
        return self._evaluator(state)


_ROLLOUT_LEAF_AUTHORITY = object()


def _validated_leaf_value(value: tuple[float, float]) -> tuple[float, float]:
    if type(value) is not tuple or len(value) != 2:
        raise ValueError("leaf evaluator must return a two-player tuple")
    if any(type(item) not in (int, float) for item in value):
        raise TypeError("leaf values must be numeric and must not be bool/string")
    normalized = (float(value[0]), float(value[1]))
    if not all(math.isfinite(item) for item in normalized):
        raise ValueError("leaf evaluator returned a non-finite value")
    if abs(normalized[0] + normalized[1]) > 1e-12:
        raise ValueError("leaf evaluator must be zero sum")
    return normalized


def _canonical_action(action: Action) -> dict[str, Any]:
    if type(action) is str:
        return {"type": "str", "value": action}
    if type(action) is int:
        return {"type": "int", "value": action}
    if type(action) is tuple:
        return {
            "type": "tuple",
            "value": [_canonical_action(item) for item in action],
        }
    raise TypeError(
        "M3 semantic fingerprint supports exact str/int/tuple actions only"
    )


def _canonical_json(payload: Any) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _semantic_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _walk_state_semantics(
    state: GameState,
    policy: BehaviorPolicy | None,
    value_lookup: dict[str, tuple[float, float]] | None = None,
) -> tuple[dict[str, Any], tuple[float, float] | None]:
    """Exhaustively bind actor/chance/infoset/action/transition/terminal semantics."""

    actor = state.current_player
    depth = state.depth
    if type(actor) is not int or type(depth) is not int or depth < 0:
        raise TypeError("game state actor/depth must be exact integers")
    if actor == TERMINAL_PLAYER:
        value = _validated_leaf_value(state.returns())
        payload: dict[str, Any] = {
            "node": "terminal",
            "depth": depth,
            "returns": list(value),
        }
    elif actor == CHANCE_PLAYER:
        outcomes = state.chance_outcomes()
        if type(outcomes) is not tuple or not outcomes:
            raise TypeError("chance outcomes must be a nonempty tuple")
        rows: list[dict[str, Any]] = []
        weighted_values: list[tuple[float, tuple[float, float] | None]] = []
        seen_actions: set[bytes] = set()
        for outcome in outcomes:
            if type(outcome) is not tuple or len(outcome) != 2:
                raise TypeError("chance outcome must be an (action, probability) tuple")
            action, raw_probability = outcome
            if type(raw_probability) not in (int, float):
                raise TypeError("chance probability must be numeric, not bool/string")
            probability = float(raw_probability)
            if not math.isfinite(probability) or probability <= 0.0:
                raise ValueError("chance probability must be finite and positive")
            canonical_action = _canonical_action(action)
            action_bytes = _canonical_json(canonical_action)
            if action_bytes in seen_actions:
                raise ValueError("chance outcomes contain a duplicate action")
            seen_actions.add(action_bytes)
            child_payload, child_value = _walk_state_semantics(
                state.child(action),
                policy,
                value_lookup,
            )
            rows.append(
                {
                    "action": canonical_action,
                    "probability": probability,
                    "child": child_payload,
                }
            )
            weighted_values.append((probability, child_value))
        total = math.fsum(row["probability"] for row in rows)
        if not math.isclose(total, 1.0, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError(f"chance probabilities sum to {total!r}, expected 1")
        rows.sort(key=lambda row: _canonical_json(row["action"]))
        payload = {"node": "chance", "depth": depth, "outcomes": rows}
        if policy is None:
            value = None
        else:
            if any(child_value is None for _, child_value in weighted_values):
                raise AssertionError("policy evaluation lost a chance child value")
            value = (
                math.fsum(
                    probability * child_value[0]  # type: ignore[index]
                    for probability, child_value in weighted_values
                ),
                math.fsum(
                    probability * child_value[1]  # type: ignore[index]
                    for probability, child_value in weighted_values
                ),
            )
    else:
        if actor not in (0, 1):
            raise ValueError("M3 leaf games must have exactly players 0 and 1")
        information_state = state.information_state_key(actor)
        if type(information_state) is not str:
            raise TypeError("information-state key must be a string")
        legal_actions = state.legal_actions()
        if type(legal_actions) is not tuple or not legal_actions:
            raise TypeError("legal actions must be a nonempty tuple")
        canonical_actions = [_canonical_action(action) for action in legal_actions]
        action_bytes = [_canonical_json(action) for action in canonical_actions]
        if len(action_bytes) != len(set(action_bytes)):
            raise ValueError("legal action tuple contains duplicates")
        children: list[dict[str, Any]] = []
        child_values: dict[Action, tuple[float, float] | None] = {}
        for action, canonical_action in zip(
            legal_actions,
            canonical_actions,
            strict=True,
        ):
            child_payload, child_value = _walk_state_semantics(
                state.child(action),
                policy,
                value_lookup,
            )
            children.append({"action": canonical_action, "child": child_payload})
            child_values[action] = child_value
        payload = {
            "node": "decision",
            "depth": depth,
            "actor": actor,
            "information_state": information_state,
            "actions": canonical_actions,
            "children": children,
        }
        if policy is None:
            value = None
        else:
            probabilities = action_probabilities(
                policy,
                information_state,
                legal_actions,
            )
            if any(child_values[action] is None for action in legal_actions):
                raise AssertionError("policy evaluation lost a decision child value")
            value = (
                math.fsum(
                    probabilities[action] * child_values[action][0]  # type: ignore[index]
                    for action in legal_actions
                ),
                math.fsum(
                    probabilities[action] * child_values[action][1]  # type: ignore[index]
                    for action in legal_actions
                ),
            )
    if value_lookup is not None and value is not None:
        semantic_id = _semantic_sha256(payload)
        previous = value_lookup.get(semantic_id)
        if previous is not None and previous != value:
            raise ValueError("identical state semantics produced inconsistent leaf values")
        value_lookup[semantic_id] = value
    return payload, value


def _game_binding_from_root(game: ExtensiveGame, root_payload: Mapping[str, Any]) -> str:
    if type(game.name) is not str or not game.name:
        raise TypeError("game name must be a nonempty string")
    return _semantic_sha256(
        {
            "game_name": game.name,
            "game_type": f"{type(game).__module__}.{type(game).__qualname__}",
            "game_repr": repr(game),
            "root": root_payload,
        }
    )


def _game_binding(game: ExtensiveGame) -> str:
    root_payload, _ = _walk_state_semantics(
        game.new_initial_state(),
        policy=None,
    )
    return _game_binding_from_root(game, root_payload)


def policy_value_from_state(
    state: GameState,
    policy: BehaviorPolicy,
) -> tuple[float, float]:
    """Return exact continuation value from an arbitrary small-game state."""

    _payload, value = _walk_state_semantics(state, policy)
    if value is None:
        raise AssertionError("policy evaluation did not produce a value")
    return value


def rollout_leaf(
    policy: BehaviorPolicy,
    *,
    game: ExtensiveGame,
    label: str,
) -> LeafValueContract:
    """Build a snapshotted, hash-bound exact blueprint-rollout leaf."""

    if not label or any(character.isspace() for character in label):
        raise ValueError("rollout leaf label must be nonempty and contain no whitespace")
    validate_behavior_policy(game, policy)
    schema = information_state_action_schema(game)
    source_policy: BehaviorPolicy = policy
    if not policy:
        source_policy = {
            key: {action: 1.0 / len(actions) for action in actions}
            for key, actions in schema.items()
        }
    snapshot: dict[str, dict[str, float]] = {}
    for key, vector in source_policy.items():
        if not isinstance(key, str):
            raise TypeError("rollout leaf policy information-state keys must be strings")
        normalized: dict[str, float] = {}
        for action, value in vector.items():
            if not isinstance(action, str):
                raise TypeError("rollout leaf policy actions must be strings")
            if type(value) not in (int, float):
                raise TypeError(
                    "rollout leaf probabilities must be numeric, not bool/string"
                )
            probability = float(value)
            if not math.isfinite(probability) or probability < 0.0:
                raise ValueError("rollout leaf policy weights must be finite and nonnegative")
            normalized[action] = probability
        snapshot[key] = normalized
    value_lookup: dict[str, tuple[float, float]] = {}
    root_payload, root_value = _walk_state_semantics(
        game.new_initial_state(),
        snapshot,
        value_lookup,
    )
    if root_value is None:
        raise AssertionError("rollout freeze did not produce a root value")
    game_binding = _game_binding_from_root(game, root_payload)
    encoded = json.dumps(
        {"game_binding": game_binding, "policy": snapshot},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    identity = f"{label}:sha256:{hashlib.sha256(encoded).hexdigest()}"

    def evaluate(state: GameState) -> tuple[float, float]:
        state_payload, _ = _walk_state_semantics(state, policy=None)
        semantic_id = _semantic_sha256(state_payload)
        if semantic_id not in value_lookup:
            raise ValueError(
                "leaf state semantics are absent from the frozen game/policy receipt"
            )
        return value_lookup[semantic_id]

    return LeafValueContract(
        identity=identity,
        game_binding=game_binding,
        evaluator=evaluate,
        _authority=_ROLLOUT_LEAF_AUTHORITY,
    )


@dataclass(frozen=True, slots=True)
class DepthLimitedState:
    """State wrapper that turns the configured frontier into terminals."""

    state: GameState
    remaining_depth: int
    leaf: LeafValueContract

    def __post_init__(self) -> None:
        if type(self.remaining_depth) is not int or self.remaining_depth < 0:
            raise ValueError("remaining_depth must be nonnegative")
        if type(self.leaf) is not LeafValueContract:
            raise TypeError("leaf must be the exact sealed LeafValueContract type")

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
        if type(self.max_depth) is not int or self.max_depth < 0:
            raise ValueError("max_depth must be nonnegative")
        if type(self.leaf) is not LeafValueContract:
            raise TypeError("leaf must be the exact sealed LeafValueContract type")
        if self.leaf.game_binding != _game_binding(self.game):
            raise ValueError("leaf content receipt is bound to a different game")

    @property
    def name(self) -> str:
        return f"{self.game.name}:depth={self.max_depth}:leaf={self.leaf.identity}"

    def identity_sha256(self) -> str:
        route_root = Path(__file__).parents[1]
        return payload_sha256(
            {
                "schema": GAME_IDENTITY_SCHEMA,
                "game": self.name,
                "semantics": "exact-depth-limited-wrapper-v2",
                "base_game_identity": game_identity_sha256(self.game),
                "max_depth": self.max_depth,
                "leaf_identity": self.leaf.identity,
                "leaf_game_binding": self.leaf.game_binding,
                "source": file_sha256(Path(__file__)),
                "core_identity_source": file_sha256(
                    route_root / "core" / "identity.py"
                ),
            }
        )

    def new_initial_state(self) -> DepthLimitedState:
        return DepthLimitedState(
            state=self.game.new_initial_state(),
            remaining_depth=self.max_depth,
            leaf=self.leaf,
        )
