"""Deterministic external-sampling MCCFR correctness foundation.

The solver intentionally uses synchronous batches: every shard samples from
the same frozen regret table, returns per-trajectory additive deltas, and a
single canonical reducer applies those deltas.  This makes checkpoint/resume
and shard-layout invariance testable before a scalable HUNL storage backend is
introduced.  It is a documented engineering adaptation, not a claim of
bit-for-bit equivalence with sequential single-trajectory MCCFR.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..core.game import Action, CHANCE_PLAYER, ExtensiveGame, GameState, TERMINAL_PLAYER

FORMAT_VERSION = 2
UPDATE_RULES = frozenset({"vanilla", "linear", "cfr_plus", "dcfr"})
AVERAGING_MODES = frozenset({"sampled", "full"})


def _require_keys(
    payload: Mapping[str, Any],
    expected: frozenset[str],
    context: str,
) -> None:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{context} must be a JSON object")
    if set(payload) != expected:
        missing = expected - set(payload)
        extra = set(payload) - expected
        raise ValueError(
            f"{context} keys mismatch: missing={sorted(missing)!r}, "
            f"extra={sorted(extra)!r}"
        )


def _json_string(value: Any, context: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{context} must be a JSON string")
    return value


def _json_integer(value: Any, context: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{context} must be a JSON integer, not bool/float/string")
    return value


def _json_number(value: Any, context: str) -> float:
    if type(value) not in (int, float):
        raise TypeError(f"{context} must be a finite JSON number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{context} must be finite")
    return result


def _json_object(value: Any, context: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise TypeError(f"{context} must be a JSON object")
    return value


def _json_array(value: Any, context: str) -> list[Any]:
    if type(value) is not list:
        raise TypeError(f"{context} must be a JSON array")
    return value


def _parse_action_table(value: Any, context: str) -> ActionTable:
    payload = _json_object(value, context)
    result: ActionTable = {}
    for raw_key, raw_actions in payload.items():
        key = _json_string(raw_key, f"{context} key")
        actions = _json_array(raw_actions, f"{context}[{key!r}]")
        result[key] = tuple(
            _json_string(action, f"{context}[{key!r}] action")
            for action in actions
        )
    return result


def _parse_vector_table(value: Any, context: str) -> VectorTable:
    payload = _json_object(value, context)
    result: VectorTable = {}
    for raw_key, raw_vector in payload.items():
        key = _json_string(raw_key, f"{context} key")
        vector = _json_object(raw_vector, f"{context}[{key!r}]")
        result[key] = {
            _json_string(action, f"{context}[{key!r}] action"): _json_number(
                number,
                f"{context}[{key!r}][{action!r}]",
            )
            for action, number in vector.items()
        }
    return result


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SolverConfig:
    """Algorithm and deterministic-sampling configuration."""

    update_rule: str = "linear"
    averaging_mode: str = "sampled"
    seed: int = 20260712
    samples_per_player: int = 1
    cfr_plus_delay: int = 0
    dcfr_alpha: float = 1.5
    dcfr_beta: float = 0.0
    dcfr_gamma: float = 2.0

    def __post_init__(self) -> None:
        if type(self.update_rule) is not str or type(self.averaging_mode) is not str:
            raise TypeError("solver update/averaging modes must be strings")
        if self.update_rule not in UPDATE_RULES:
            raise ValueError(f"unknown update rule: {self.update_rule!r}")
        if self.averaging_mode not in AVERAGING_MODES:
            raise ValueError(f"unknown averaging mode: {self.averaging_mode!r}")
        if type(self.seed) is not int:
            raise TypeError("solver seed must be an integer, not bool/float/string")
        if type(self.samples_per_player) is not int:
            raise TypeError("samples_per_player must be an integer")
        if type(self.cfr_plus_delay) is not int:
            raise TypeError("cfr_plus_delay must be an integer")
        if self.samples_per_player <= 0:
            raise ValueError("samples_per_player must be positive")
        if self.cfr_plus_delay < 0:
            raise ValueError("cfr_plus_delay must be nonnegative")
        if any(
            type(value) not in (int, float)
            for value in (self.dcfr_alpha, self.dcfr_beta, self.dcfr_gamma)
        ) or not all(
            math.isfinite(float(value))
            for value in (self.dcfr_alpha, self.dcfr_beta, self.dcfr_gamma)
        ):
            raise ValueError("DCFR exponents must be finite")

    def to_payload(self) -> dict[str, Any]:
        return {
            "update_rule": self.update_rule,
            "averaging_mode": self.averaging_mode,
            "seed": self.seed,
            "samples_per_player": self.samples_per_player,
            "cfr_plus_delay": self.cfr_plus_delay,
            "dcfr_alpha": self.dcfr_alpha,
            "dcfr_beta": self.dcfr_beta,
            "dcfr_gamma": self.dcfr_gamma,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SolverConfig":
        _require_keys(
            payload,
            frozenset(
                {
                    "update_rule",
                    "averaging_mode",
                    "seed",
                    "samples_per_player",
                    "cfr_plus_delay",
                    "dcfr_alpha",
                    "dcfr_beta",
                    "dcfr_gamma",
                }
            ),
            "solver config",
        )
        return cls(
            update_rule=_json_string(payload["update_rule"], "config.update_rule"),
            averaging_mode=_json_string(
                payload["averaging_mode"], "config.averaging_mode"
            ),
            seed=_json_integer(payload["seed"], "config.seed"),
            samples_per_player=_json_integer(
                payload["samples_per_player"], "config.samples_per_player"
            ),
            cfr_plus_delay=_json_integer(
                payload["cfr_plus_delay"], "config.cfr_plus_delay"
            ),
            dcfr_alpha=_json_number(payload["dcfr_alpha"], "config.dcfr_alpha"),
            dcfr_beta=_json_number(payload["dcfr_beta"], "config.dcfr_beta"),
            dcfr_gamma=_json_number(payload["dcfr_gamma"], "config.dcfr_gamma"),
        )


VectorTable = dict[str, dict[str, float]]
ActionTable = dict[str, tuple[str, ...]]


@dataclass(slots=True)
class SolverState:
    """Separated regret and average-strategy accumulators."""

    game_name: str
    config: SolverConfig
    batch_index: int = 0
    actions: ActionTable = field(default_factory=dict)
    regrets: VectorTable = field(default_factory=dict)
    strategy_sum: VectorTable = field(default_factory=dict)
    trajectories: int = 0
    node_touches: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "format_version": FORMAT_VERSION,
            "game_name": self.game_name,
            "config": self.config.to_payload(),
            "batch_index": self.batch_index,
            "actions": {key: list(actions) for key, actions in sorted(self.actions.items())},
            "regrets": {
                key: {action: values[action] for action in sorted(values)}
                for key, values in sorted(self.regrets.items())
            },
            "strategy_sum": {
                key: {action: values[action] for action in sorted(values)}
                for key, values in sorted(self.strategy_sum.items())
            },
            "trajectories": self.trajectories,
            "node_touches": self.node_touches,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SolverState":
        _require_keys(
            payload,
            frozenset(
                {
                    "format_version",
                    "game_name",
                    "config",
                    "batch_index",
                    "actions",
                    "regrets",
                    "strategy_sum",
                    "trajectories",
                    "node_touches",
                }
            ),
            "solver checkpoint payload",
        )
        if _json_integer(payload["format_version"], "format_version") != FORMAT_VERSION:
            raise ValueError(f"unsupported checkpoint format: {payload['format_version']}")
        state = cls(
            game_name=_json_string(payload["game_name"], "game_name"),
            config=SolverConfig.from_payload(
                _json_object(payload["config"], "config")
            ),
            batch_index=_json_integer(payload["batch_index"], "batch_index"),
            actions=_parse_action_table(payload["actions"], "actions"),
            regrets=_parse_vector_table(payload["regrets"], "regrets"),
            strategy_sum=_parse_vector_table(
                payload["strategy_sum"], "strategy_sum"
            ),
            trajectories=_json_integer(payload["trajectories"], "trajectories"),
            node_touches=_json_integer(payload["node_touches"], "node_touches"),
        )
        state.validate()
        return state

    def validate(self) -> None:
        if type(self.game_name) is not str or not self.game_name:
            raise ValueError("game_name must be a nonempty string")
        if not isinstance(self.config, SolverConfig):
            raise TypeError("config must be SolverConfig")
        for name, value in (
            ("batch_index", self.batch_index),
            ("trajectories", self.trajectories),
            ("node_touches", self.node_touches),
        ):
            if type(value) is not int:
                raise TypeError(f"{name} must be an integer")
        if self.batch_index < 0 or self.trajectories < 0 or self.node_touches < 0:
            raise ValueError("solver counters must be nonnegative")
        if type(self.actions) is not dict:
            raise TypeError("actions must be a dictionary")
        for table_name, table in (
            ("regrets", self.regrets),
            ("strategy_sum", self.strategy_sum),
        ):
            if type(table) is not dict:
                raise TypeError(f"{table_name} must be a dictionary")
            orphan_keys = set(table) - set(self.actions)
            if orphan_keys:
                raise ValueError(
                    f"{table_name} has information states absent from actions: "
                    f"{sorted(orphan_keys)!r}"
                )
        for key, actions in self.actions.items():
            if type(key) is not str or type(actions) is not tuple:
                raise TypeError("action table requires string keys and tuple rows")
            if (
                not actions
                or any(type(action) is not str for action in actions)
                or len(actions) != len(set(actions))
            ):
                raise ValueError(f"invalid action set for {key}: {actions!r}")
            for table_name, table in (
                ("regrets", self.regrets),
                ("strategy_sum", self.strategy_sum),
            ):
                values = table.get(key, {})
                if type(values) is not dict or any(
                    type(action) is not str for action in values
                ):
                    raise TypeError(f"{table_name} vectors require string action keys")
                if values and set(values) != set(actions):
                    raise ValueError(
                        f"{table_name} must contain the complete action set for {key}"
                    )
                if any(
                    type(value) not in (int, float)
                    or not math.isfinite(float(value))
                    for value in values.values()
                ):
                    raise ValueError(f"{table_name} has non-finite values for {key}")
                if table_name == "strategy_sum" and any(
                    value < 0.0 for value in values.values()
                ):
                    raise ValueError(f"strategy_sum has negative values for {key}")

    @property
    def digest(self) -> str:
        return _sha256(self.to_payload())


def _string_actions(actions: tuple[Action, ...]) -> tuple[str, ...]:
    if any(not isinstance(action, str) for action in actions):
        raise TypeError("M3 checkpoint format supports string decision actions only")
    return tuple(str(action) for action in actions)


def _record_action_set(
    action_sets: ActionTable,
    information_state: str,
    actions: tuple[Action, ...],
) -> tuple[str, ...]:
    normalized = _string_actions(actions)
    existing = action_sets.get(information_state)
    if existing is not None and existing != normalized:
        raise ValueError(f"inconsistent actions for {information_state}")
    action_sets[information_state] = normalized
    return normalized


def regret_matching(
    regrets: Mapping[str, Mapping[str, float]],
    information_state: str,
    legal_actions: tuple[str, ...],
) -> dict[str, float]:
    positive = {
        action: max(0.0, float(regrets.get(information_state, {}).get(action, 0.0)))
        for action in legal_actions
    }
    total = sum(positive.values())
    if total <= 0.0:
        probability = 1.0 / len(legal_actions)
        return {action: probability for action in legal_actions}
    return {action: value / total for action, value in positive.items()}


def _sample(distribution: Iterable[tuple[Action, float]], rng: random.Random) -> Action:
    threshold = rng.random()
    cumulative = 0.0
    last_action: Action | None = None
    for action, probability in distribution:
        last_action = action
        cumulative += probability
        if threshold < cumulative:
            return action
    if last_action is None:
        raise ValueError("cannot sample an empty distribution")
    return last_action


def _derived_seed(base_seed: int, batch_index: int, player: int, sample_id: int) -> int:
    material = f"m3:{base_seed}:{batch_index}:{player}:{sample_id}".encode("ascii")
    return int.from_bytes(hashlib.blake2b(material, digest_size=16).digest(), "big")


def _add_value(table: VectorTable, key: str, action: str, value: float) -> None:
    vector = table.setdefault(key, {})
    vector[action] = vector.get(action, 0.0) + value


@dataclass(frozen=True, slots=True)
class SampleDelta:
    traverser: int
    sample_id: int
    action_sets: ActionTable
    regret_delta: VectorTable
    strategy_delta: VectorTable
    node_touches: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "traverser": self.traverser,
            "sample_id": self.sample_id,
            "action_sets": {key: list(value) for key, value in sorted(self.action_sets.items())},
            "regret_delta": self.regret_delta,
            "strategy_delta": self.strategy_delta,
            "node_touches": self.node_touches,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SampleDelta":
        _require_keys(
            payload,
            frozenset(
                {
                    "traverser",
                    "sample_id",
                    "action_sets",
                    "regret_delta",
                    "strategy_delta",
                    "node_touches",
                }
            ),
            "sample delta",
        )
        return cls(
            traverser=_json_integer(payload["traverser"], "sample.traverser"),
            sample_id=_json_integer(payload["sample_id"], "sample.sample_id"),
            action_sets=_parse_action_table(
                payload["action_sets"], "sample.action_sets"
            ),
            regret_delta=_parse_vector_table(
                payload["regret_delta"], "sample.regret_delta"
            ),
            strategy_delta=_parse_vector_table(
                payload["strategy_delta"], "sample.strategy_delta"
            ),
            node_touches=_json_integer(
                payload["node_touches"], "sample.node_touches"
            ),
        )


@dataclass(frozen=True, slots=True)
class ShardDelta:
    base_digest: str
    batch_index: int
    shard_index: int
    shard_count: int
    samples_per_player: int
    samples: tuple[SampleDelta, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "format_version": FORMAT_VERSION,
            "base_digest": self.base_digest,
            "batch_index": self.batch_index,
            "shard_index": self.shard_index,
            "shard_count": self.shard_count,
            "samples_per_player": self.samples_per_player,
            "samples": [sample.to_payload() for sample in self.samples],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ShardDelta":
        _require_keys(
            payload,
            frozenset(
                {
                    "format_version",
                    "base_digest",
                    "batch_index",
                    "shard_index",
                    "shard_count",
                    "samples_per_player",
                    "samples",
                }
            ),
            "shard payload",
        )
        if _json_integer(payload["format_version"], "format_version") != FORMAT_VERSION:
            raise ValueError("unsupported shard format")
        samples = _json_array(payload["samples"], "shard.samples")
        return cls(
            base_digest=_json_string(payload["base_digest"], "shard.base_digest"),
            batch_index=_json_integer(payload["batch_index"], "shard.batch_index"),
            shard_index=_json_integer(payload["shard_index"], "shard.shard_index"),
            shard_count=_json_integer(payload["shard_count"], "shard.shard_count"),
            samples_per_player=_json_integer(
                payload["samples_per_player"], "shard.samples_per_player"
            ),
            samples=tuple(
                SampleDelta.from_payload(_json_object(item, "shard sample"))
                for item in samples
            ),
        )


def _external_sample(
    root: GameState,
    state: SolverState,
    traverser: int,
    sample_id: int,
) -> SampleDelta:
    rng = random.Random(
        _derived_seed(state.config.seed, state.batch_index, traverser, sample_id)
    )
    action_sets: ActionTable = {}
    regret_delta: VectorTable = {}
    strategy_delta: VectorTable = {}
    node_touches = 0

    def traverse(node: GameState) -> float:
        nonlocal node_touches
        node_touches += 1
        actor = node.current_player
        if actor == TERMINAL_PLAYER:
            return node.returns()[traverser]
        if actor == CHANCE_PLAYER:
            action = _sample(node.chance_outcomes(), rng)
            return traverse(node.child(action))

        key = node.information_state_key(actor)
        legal = _record_action_set(action_sets, key, node.legal_actions())
        policy = regret_matching(state.regrets, key, legal)

        if actor != traverser:
            if state.config.averaging_mode == "sampled":
                for action in legal:
                    _add_value(strategy_delta, key, action, policy[action])
            sampled_action = _sample(tuple(policy.items()), rng)
            return traverse(node.child(sampled_action))

        action_values: dict[str, float] = {}
        expected_value = 0.0
        for action in legal:
            action_values[action] = traverse(node.child(action))
            expected_value += policy[action] * action_values[action]
        for action in legal:
            _add_value(
                regret_delta,
                key,
                action,
                action_values[action] - expected_value,
            )
        return expected_value

    traverse(root)
    return SampleDelta(
        traverser=traverser,
        sample_id=sample_id,
        action_sets=action_sets,
        regret_delta=regret_delta,
        strategy_delta=strategy_delta,
        node_touches=node_touches,
    )


def build_shard(
    game: ExtensiveGame,
    state: SolverState,
    shard_index: int,
    shard_count: int,
) -> ShardDelta:
    """Build one deterministic shard from a frozen solver state."""

    if type(shard_index) is not int or type(shard_count) is not int:
        raise TypeError("shard index/count must be exact integers")
    if shard_count <= 0 or not 0 <= shard_index < shard_count:
        raise ValueError("invalid shard index/count")
    state.validate()
    samples: list[SampleDelta] = []
    for traverser in (0, 1):
        for sample_id in range(shard_index, state.config.samples_per_player, shard_count):
            samples.append(
                _external_sample(game.new_initial_state(), state, traverser, sample_id)
            )
    return ShardDelta(
        base_digest=state.digest,
        batch_index=state.batch_index,
        shard_index=shard_index,
        shard_count=shard_count,
        samples_per_player=state.config.samples_per_player,
        samples=tuple(samples),
    )


def _validate_in_memory_sample(sample: SampleDelta) -> None:
    if type(sample) is not SampleDelta:
        raise TypeError("shard samples must be exact SampleDelta objects")
    for name, value in (
        ("traverser", sample.traverser),
        ("sample_id", sample.sample_id),
        ("node_touches", sample.node_touches),
    ):
        if type(value) is not int:
            raise TypeError(f"sample {name} must be an exact integer")
    if sample.traverser not in (0, 1):
        raise ValueError("sample traverser must be 0 or 1")
    if sample.sample_id < 0 or sample.node_touches <= 0:
        raise ValueError("sample counters must be positive")
    if type(sample.action_sets) is not dict:
        raise TypeError("sample action_sets must be a dictionary")
    for key, actions in sample.action_sets.items():
        if type(key) is not str or type(actions) is not tuple:
            raise TypeError("sample action sets require string keys and tuple rows")
        if (
            not actions
            or any(type(action) is not str for action in actions)
            or len(actions) != len(set(actions))
        ):
            raise ValueError(f"invalid sample action set for {key}")
    for table_name, table in (
        ("regret_delta", sample.regret_delta),
        ("strategy_delta", sample.strategy_delta),
    ):
        if type(table) is not dict:
            raise TypeError(f"sample {table_name} must be a dictionary")
        orphan_keys = set(table) - set(sample.action_sets)
        if orphan_keys:
            raise ValueError(
                f"{table_name} has information states without action sets: "
                f"{sorted(orphan_keys)!r}"
            )
        for key, vector in table.items():
            if type(key) is not str or type(vector) is not dict:
                raise TypeError(f"sample {table_name} vectors must be dictionaries")
            if any(type(action) is not str for action in vector):
                raise TypeError(f"sample {table_name} action keys must be strings")
            if set(vector) != set(sample.action_sets[key]):
                raise ValueError(
                    f"{table_name} must contain the complete action set for {key}"
                )
            for value in vector.values():
                if type(value) not in (int, float):
                    raise TypeError(
                        f"sample {table_name} values must be exact numeric types"
                    )
                if not math.isfinite(float(value)):
                    raise ValueError(f"{table_name} has non-finite values for {key}")
                if table_name == "strategy_delta" and value < 0.0:
                    raise ValueError(f"strategy_delta has negative values for {key}")


def _validate_in_memory_shard(shard: ShardDelta) -> None:
    if type(shard) is not ShardDelta:
        raise TypeError("apply_shards requires exact ShardDelta objects")
    if (
        type(shard.base_digest) is not str
        or len(shard.base_digest) != 64
        or any(character not in "0123456789abcdef" for character in shard.base_digest)
    ):
        raise ValueError("shard base digest must be lowercase SHA-256")
    for name, value in (
        ("batch_index", shard.batch_index),
        ("shard_index", shard.shard_index),
        ("shard_count", shard.shard_count),
        ("samples_per_player", shard.samples_per_player),
    ):
        if type(value) is not int:
            raise TypeError(f"shard {name} must be an exact integer")
    if shard.batch_index < 0:
        raise ValueError("shard batch_index must be nonnegative")
    if shard.shard_count <= 0 or not 0 <= shard.shard_index < shard.shard_count:
        raise ValueError("invalid in-memory shard index/count")
    if shard.samples_per_player <= 0:
        raise ValueError("shard samples_per_player must be positive")
    if type(shard.samples) is not tuple:
        raise TypeError("shard samples must be a tuple")
    for sample in shard.samples:
        _validate_in_memory_sample(sample)


def _ordered_samples(state: SolverState, shards: Iterable[ShardDelta]) -> list[SampleDelta]:
    shard_list = list(shards)
    if not shard_list:
        raise ValueError("at least one shard is required")
    for shard in shard_list:
        _validate_in_memory_shard(shard)
    shard_count = shard_list[0].shard_count
    samples_per_player = state.config.samples_per_player
    expected_indices = set(range(shard_count))
    actual_indices = {shard.shard_index for shard in shard_list}
    if len(actual_indices) != len(shard_list) or actual_indices != expected_indices:
        raise ValueError("shard set must contain every unique shard index exactly once")
    for shard in shard_list:
        if shard.base_digest != state.digest:
            raise ValueError("shard base digest does not match solver state")
        if shard.batch_index != state.batch_index:
            raise ValueError("shard batch index does not match solver state")
        if shard.shard_count != shard_count:
            raise ValueError("shards disagree on shard_count")
        if shard.samples_per_player != samples_per_player:
            raise ValueError("shards disagree on samples_per_player")
        if any(
            sample.sample_id % shard_count != shard.shard_index
            for sample in shard.samples
        ):
            raise ValueError("sample is assigned to the wrong deterministic shard")

    ordered = sorted(
        (sample for shard in shard_list for sample in shard.samples),
        key=lambda sample: (sample.traverser, sample.sample_id),
    )
    expected_samples = {
        (traverser, sample_id)
        for traverser in (0, 1)
        for sample_id in range(samples_per_player)
    }
    actual_samples = [(sample.traverser, sample.sample_id) for sample in ordered]
    if len(actual_samples) != len(set(actual_samples)):
        raise ValueError("duplicate trajectory in shard set")
    if set(actual_samples) != expected_samples:
        raise ValueError("shard set does not cover the configured trajectories")
    return ordered


def _validate_sample_deltas(state: SolverState, samples: Iterable[SampleDelta]) -> None:
    """Validate all untrusted shard payloads before staging any state update."""

    known_actions = dict(state.actions)
    for sample in samples:
        _validate_in_memory_sample(sample)
        for key, actions in sample.action_sets.items():
            if not actions or len(actions) != len(set(actions)):
                raise ValueError(f"invalid sample action set for {key}")
            existing = known_actions.get(key)
            if existing is not None and existing != actions:
                raise ValueError(f"action drift for information state {key}")
            known_actions[key] = actions
        for table_name, table in (
            ("regret_delta", sample.regret_delta),
            ("strategy_delta", sample.strategy_delta),
        ):
            orphan_keys = set(table) - set(sample.action_sets)
            if orphan_keys:
                raise ValueError(
                    f"{table_name} has information states without action sets: "
                    f"{sorted(orphan_keys)!r}"
                )
            for key, vector in table.items():
                expected_actions = set(sample.action_sets[key])
                if set(vector) != expected_actions:
                    raise ValueError(
                        f"{table_name} must contain the complete action set for {key}"
                    )
                # Exact types, finiteness, and strategy nonnegativity were
                # checked before shard identity/order logic.


def _merge_action_sets(state: SolverState, samples: Iterable[SampleDelta]) -> None:
    for sample in samples:
        for key, actions in sample.action_sets.items():
            existing = state.actions.get(key)
            if existing is not None and existing != actions:
                raise ValueError(f"action drift for information state {key}")
            state.actions[key] = actions


def _aggregate_samples(
    samples: Iterable[SampleDelta],
    attribute: str,
    denominator: int,
) -> VectorTable:
    values: dict[tuple[str, str], list[float]] = {}
    for sample in samples:
        table: VectorTable = getattr(sample, attribute)
        for key in sorted(table):
            for action in sorted(table[key]):
                values.setdefault((key, action), []).append(table[key][action])
    result: VectorTable = {}
    for (key, action), contributions in sorted(values.items()):
        _add_value(result, key, action, math.fsum(contributions) / denominator)
    return result


def _full_average_delta(game: ExtensiveGame, state: SolverState) -> tuple[ActionTable, VectorTable]:
    action_sets: ActionTable = {}
    strategy_delta: VectorTable = {}
    seen_reach: dict[tuple[int, str], float] = {}

    def traverse(node: GameState, reach: tuple[float, float]) -> None:
        actor = node.current_player
        if actor == TERMINAL_PLAYER:
            return
        if actor == CHANCE_PLAYER:
            for action, _ in node.chance_outcomes():
                traverse(node.child(action), reach)
            return
        key = node.information_state_key(actor)
        legal = _record_action_set(action_sets, key, node.legal_actions())
        policy = regret_matching(state.regrets, key, legal)
        marker = (actor, key)
        previous_reach = seen_reach.get(marker)
        if previous_reach is None:
            seen_reach[marker] = reach[actor]
            for action in legal:
                _add_value(strategy_delta, key, action, reach[actor] * policy[action])
        elif abs(previous_reach - reach[actor]) > 1e-12:
            raise ValueError(f"imperfect-recall own reach detected at {key}")
        for action in legal:
            next_reach = list(reach)
            next_reach[actor] *= policy[action]
            traverse(node.child(action), (next_reach[0], next_reach[1]))

    traverse(game.new_initial_state(), (1.0, 1.0))
    return action_sets, strategy_delta


def _updated_regret(config: SolverConfig, t: int, old: float, delta: float) -> float:
    """Apply one paper-ordered accumulator update.

    DCFR first adds the instantaneous regret, then selects the positive or
    negative discount from the sign of that updated cumulative value.  The
    order matters whenever a regret crosses zero.  LCFR is DCFR(1, 1, 1), so
    it uses the same post-add discount convention.
    """

    provisional = old + delta
    if config.update_rule == "vanilla":
        return provisional
    if config.update_rule == "cfr_plus":
        return max(0.0, provisional)
    if config.update_rule == "linear":
        return provisional * (t / (t + 1.0))
    exponent = config.dcfr_alpha if provisional >= 0.0 else config.dcfr_beta
    power = t**exponent
    return provisional * (power / (power + 1.0))


def _new_strategy_weight(config: SolverConfig, t: int) -> float:
    if config.update_rule == "cfr_plus":
        return float(max(0, t - config.cfr_plus_delay))
    if config.update_rule == "linear":
        return float(t)
    if config.update_rule == "dcfr":
        return float(t**config.dcfr_gamma)
    return 1.0


def apply_shards(
    game: ExtensiveGame,
    state: SolverState,
    shards: Iterable[ShardDelta],
) -> SolverState:
    """Validate and canonically apply one complete synchronous batch."""

    if state.game_name != game.name:
        raise ValueError(f"state is for {state.game_name}, game is {game.name}")
    state.validate()
    samples = _ordered_samples(state, shards)
    _validate_sample_deltas(state, samples)
    # Build the next state transactionally.  Any action drift, perfect-recall
    # failure, or non-finite update leaves the caller's checkpoint untouched.
    working = SolverState.from_payload(state.to_payload())
    _merge_action_sets(working, samples)
    regret_delta = _aggregate_samples(
        samples, "regret_delta", working.config.samples_per_player
    )

    if working.config.averaging_mode == "full":
        full_actions, strategy_delta = _full_average_delta(game, working)
        for key, actions in full_actions.items():
            existing = working.actions.get(key)
            if existing is not None and existing != actions:
                raise ValueError(f"action drift for information state {key}")
            working.actions[key] = actions
    else:
        strategy_delta = _aggregate_samples(
            samples, "strategy_delta", working.config.samples_per_player
        )

    t = working.batch_index + 1
    all_regret_keys = set(working.actions) | set(regret_delta)
    for key in sorted(all_regret_keys):
        actions = working.actions[key]
        current = working.regrets.setdefault(key, {action: 0.0 for action in actions})
        for action in actions:
            old = current.get(action, 0.0)
            current[action] = _updated_regret(
                working.config,
                t,
                old,
                regret_delta.get(key, {}).get(action, 0.0),
            )

    new_strategy_weight = _new_strategy_weight(working.config, t)
    all_strategy_keys = set(working.actions) | set(strategy_delta)
    for key in sorted(all_strategy_keys):
        actions = working.actions[key]
        current = working.strategy_sum.setdefault(
            key, {action: 0.0 for action in actions}
        )
        for action in actions:
            current[action] = (
                current.get(action, 0.0)
                + new_strategy_weight * strategy_delta.get(key, {}).get(action, 0.0)
            )

    working.batch_index = t
    working.trajectories += len(samples)
    working.node_touches += sum(sample.node_touches for sample in samples)
    working.validate()

    state.batch_index = working.batch_index
    state.actions = working.actions
    state.regrets = working.regrets
    state.strategy_sum = working.strategy_sum
    state.trajectories = working.trajectories
    state.node_touches = working.node_touches
    return state


def train_batches(
    game: ExtensiveGame,
    state: SolverState,
    batches: int,
    shard_count: int = 1,
) -> SolverState:
    """Train for ``batches`` complete deterministic synchronous batches."""

    if state.game_name != game.name:
        raise ValueError(f"state is for {state.game_name}, game is {game.name}")
    if type(batches) is not int or type(shard_count) is not int:
        raise TypeError("batches/shard_count must be exact integers")
    if batches < 0:
        raise ValueError("batches must be nonnegative")
    if shard_count <= 0:
        raise ValueError("shard_count must be positive")
    for _ in range(batches):
        shards = [build_shard(game, state, index, shard_count) for index in range(shard_count)]
        apply_shards(game, state, shards)
    return state


def current_policy(state: SolverState) -> dict[str, dict[str, float]]:
    state.validate()
    return {
        key: regret_matching(state.regrets, key, actions)
        for key, actions in sorted(state.actions.items())
    }


def average_policy(state: SolverState) -> dict[str, dict[str, float]]:
    state.validate()
    result: dict[str, dict[str, float]] = {}
    fallback = current_policy(state)
    for key, actions in sorted(state.actions.items()):
        stored = state.strategy_sum.get(key, {})
        values = (
            {action: stored[action] for action in actions}
            if stored
            else {action: 0.0 for action in actions}
        )
        total = math.fsum(values.values())
        if total <= 0.0:
            result[key] = fallback[key]
        else:
            result[key] = {action: values[action] / total for action in actions}
    return result


def _atomic_json_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(_canonical_json(payload))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _strict_json_read(path: Path) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON numeric constant {value!r} is forbidden")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON object key {key!r}")
            result[key] = value
        return result

    payload = json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=reject_constant,
        object_pairs_hook=unique_object,
    )
    return _json_object(payload, "checkpoint envelope")


def _envelope_payload(path: Path) -> Mapping[str, Any]:
    envelope = _strict_json_read(path)
    _require_keys(envelope, frozenset({"payload", "sha256"}), "checkpoint envelope")
    payload = _json_object(envelope["payload"], "checkpoint payload")
    expected = _json_string(envelope["sha256"], "checkpoint sha256")
    if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
        raise ValueError("checkpoint SHA-256 must be 64 lowercase hexadecimal digits")
    actual = _sha256(payload)
    if actual != expected:
        raise ValueError("checkpoint SHA-256 mismatch")
    return payload


def save_checkpoint(path: str | Path, state: SolverState) -> str:
    state.validate()
    payload = state.to_payload()
    digest = _sha256(payload)
    envelope = {"payload": payload, "sha256": digest}
    _atomic_json_write(Path(path), envelope)
    return digest


def load_checkpoint(path: str | Path) -> SolverState:
    return SolverState.from_payload(_envelope_payload(Path(path)))


def save_shard(path: str | Path, shard: ShardDelta) -> str:
    _validate_in_memory_shard(shard)
    payload = shard.to_payload()
    digest = _sha256(payload)
    _atomic_json_write(Path(path), {"payload": payload, "sha256": digest})
    return digest


def load_shard(path: str | Path) -> ShardDelta:
    return ShardDelta.from_payload(_envelope_payload(Path(path)))
