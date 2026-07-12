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
        if self.update_rule not in UPDATE_RULES:
            raise ValueError(f"unknown update rule: {self.update_rule!r}")
        if self.averaging_mode not in AVERAGING_MODES:
            raise ValueError(f"unknown averaging mode: {self.averaging_mode!r}")
        if self.samples_per_player <= 0:
            raise ValueError("samples_per_player must be positive")
        if self.cfr_plus_delay < 0:
            raise ValueError("cfr_plus_delay must be nonnegative")
        if not all(
            math.isfinite(value)
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
        return cls(
            update_rule=str(payload["update_rule"]),
            averaging_mode=str(payload["averaging_mode"]),
            seed=int(payload["seed"]),
            samples_per_player=int(payload["samples_per_player"]),
            cfr_plus_delay=int(payload["cfr_plus_delay"]),
            dcfr_alpha=float(payload["dcfr_alpha"]),
            dcfr_beta=float(payload["dcfr_beta"]),
            dcfr_gamma=float(payload["dcfr_gamma"]),
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
        if int(payload["format_version"]) != FORMAT_VERSION:
            raise ValueError(f"unsupported checkpoint format: {payload['format_version']}")
        state = cls(
            game_name=str(payload["game_name"]),
            config=SolverConfig.from_payload(payload["config"]),
            batch_index=int(payload["batch_index"]),
            actions={
                str(key): tuple(str(action) for action in actions)
                for key, actions in payload["actions"].items()
            },
            regrets={
                str(key): {str(action): float(value) for action, value in values.items()}
                for key, values in payload["regrets"].items()
            },
            strategy_sum={
                str(key): {str(action): float(value) for action, value in values.items()}
                for key, values in payload["strategy_sum"].items()
            },
            trajectories=int(payload.get("trajectories", 0)),
            node_touches=int(payload.get("node_touches", 0)),
        )
        state.validate()
        return state

    def validate(self) -> None:
        if self.batch_index < 0 or self.trajectories < 0 or self.node_touches < 0:
            raise ValueError("solver counters must be nonnegative")
        for table_name, table in (
            ("regrets", self.regrets),
            ("strategy_sum", self.strategy_sum),
        ):
            orphan_keys = set(table) - set(self.actions)
            if orphan_keys:
                raise ValueError(
                    f"{table_name} has information states absent from actions: "
                    f"{sorted(orphan_keys)!r}"
                )
        for key, actions in self.actions.items():
            if not actions or len(actions) != len(set(actions)):
                raise ValueError(f"invalid action set for {key}: {actions!r}")
            for table_name, table in (
                ("regrets", self.regrets),
                ("strategy_sum", self.strategy_sum),
            ):
                values = table.get(key, {})
                if set(values) - set(actions):
                    raise ValueError(f"{table_name} has unknown actions for {key}")
                if any(not math.isfinite(value) for value in values.values()):
                    raise ValueError(f"{table_name} has non-finite values for {key}")

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
        return cls(
            traverser=int(payload["traverser"]),
            sample_id=int(payload["sample_id"]),
            action_sets={
                str(key): tuple(str(action) for action in actions)
                for key, actions in payload["action_sets"].items()
            },
            regret_delta={
                str(key): {str(action): float(value) for action, value in values.items()}
                for key, values in payload["regret_delta"].items()
            },
            strategy_delta={
                str(key): {str(action): float(value) for action, value in values.items()}
                for key, values in payload["strategy_delta"].items()
            },
            node_touches=int(payload["node_touches"]),
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
        if int(payload["format_version"]) != FORMAT_VERSION:
            raise ValueError("unsupported shard format")
        return cls(
            base_digest=str(payload["base_digest"]),
            batch_index=int(payload["batch_index"]),
            shard_index=int(payload["shard_index"]),
            shard_count=int(payload["shard_count"]),
            samples_per_player=int(payload["samples_per_player"]),
            samples=tuple(SampleDelta.from_payload(item) for item in payload["samples"]),
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

    if shard_count <= 0 or not 0 <= shard_index < shard_count:
        raise ValueError("invalid shard index/count")
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


def _ordered_samples(state: SolverState, shards: Iterable[ShardDelta]) -> list[SampleDelta]:
    shard_list = list(shards)
    if not shard_list:
        raise ValueError("at least one shard is required")
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
        if sample.traverser not in (0, 1):
            raise ValueError("sample traverser must be 0 or 1")
        if sample.sample_id < 0 or sample.node_touches <= 0:
            raise ValueError("sample counters must be positive")
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
                unknown_actions = set(vector) - set(sample.action_sets[key])
                if unknown_actions:
                    raise ValueError(
                        f"{table_name} has unknown actions for {key}: "
                        f"{sorted(unknown_actions)!r}"
                    )
                if any(not math.isfinite(value) for value in vector.values()):
                    raise ValueError(f"{table_name} has non-finite values for {key}")
                if table_name == "strategy_delta" and any(
                    value < 0.0 for value in vector.values()
                ):
                    raise ValueError(f"strategy_delta has negative values for {key}")


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
    if batches < 0:
        raise ValueError("batches must be nonnegative")
    for _ in range(batches):
        shards = [build_shard(game, state, index, shard_count) for index in range(shard_count)]
        apply_shards(game, state, shards)
    return state


def current_policy(state: SolverState) -> dict[str, dict[str, float]]:
    return {
        key: regret_matching(state.regrets, key, actions)
        for key, actions in sorted(state.actions.items())
    }


def average_policy(state: SolverState) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    fallback = current_policy(state)
    for key, actions in sorted(state.actions.items()):
        values = {
            action: max(0.0, state.strategy_sum.get(key, {}).get(action, 0.0))
            for action in actions
        }
        total = sum(values.values())
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
    finally:
        temporary.unlink(missing_ok=True)


def save_checkpoint(path: str | Path, state: SolverState) -> str:
    payload = state.to_payload()
    digest = _sha256(payload)
    envelope = {"payload": payload, "sha256": digest}
    _atomic_json_write(Path(path), envelope)
    return digest


def load_checkpoint(path: str | Path) -> SolverState:
    envelope = json.loads(Path(path).read_text(encoding="utf-8"))
    payload = envelope["payload"]
    actual = _sha256(payload)
    if actual != envelope["sha256"]:
        raise ValueError("checkpoint SHA-256 mismatch")
    return SolverState.from_payload(payload)


def save_shard(path: str | Path, shard: ShardDelta) -> str:
    payload = shard.to_payload()
    digest = _sha256(payload)
    _atomic_json_write(Path(path), {"payload": payload, "sha256": digest})
    return digest


def load_shard(path: str | Path) -> ShardDelta:
    envelope = json.loads(Path(path).read_text(encoding="utf-8"))
    payload = envelope["payload"]
    if _sha256(payload) != envelope["sha256"]:
        raise ValueError("shard SHA-256 mismatch")
    return ShardDelta.from_payload(payload)
