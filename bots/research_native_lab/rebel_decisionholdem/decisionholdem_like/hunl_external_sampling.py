"""Deterministic external-sampling Linear CFR over the Common HUNL game.

This is a correctness-oriented low-budget backend.  Chance and opponent action
sampling are counter based, so checkpoint boundaries and sequential checkpoint
segment layout cannot change the sampled trajectory.  A segment is not an
independently mergeable parallel shard.  This module makes no performance or
convergence claim beyond the committed smoke configuration.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from ...common_contracts.constants import (
    BIG_BLIND,
    CONTRACT_VERSION,
    INITIAL_CHIPS,
    SMALL_BLIND,
)
from ...common_contracts.national_state import NationalGameState, Street
from .hunl_abstraction import (
    HUNL_ACTION_IDS,
    abstract_actions,
    abstraction_contract,
    information_abstraction,
    parse_infoset_key,
)
from .secure_files import (
    atomic_json_write,
    canonical_bytes,
    secure_file_map,
    stable_read_path,
    stable_selected_file_map,
    strict_json_loads,
)


HUNL_LCFR_ALGORITHM = "external-sampling-linear-cfr-simple-average-v2"
HUNL_CHECKPOINT_SCHEMA = "route-a2-hunl-lcfr-checkpoint-v5"
HUNL_SHARD_SCHEMA = "route-a2-hunl-lcfr-sequential-checkpoint-segment-v5"
HUNL_TRAINING_IDENTITY_SCHEMA = "route-a2-hunl-frozen-training-identity-v4"
HUNL_SIMPLE_AVERAGE_REFERENCE = (
    "OpenSpiel external_sampling_mccfr.cc UpdateRegrets AverageType::kSimple"
)
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
RESEARCH_LAB_ROOT = PACKAGE_ROOT.parent
COMMON_ROOT = PACKAGE_ROOT.parent / "common_contracts"
TRAINING_IDENTITY_SOURCE_FILES = (
    "__init__.py",
    "decisionholdem_like/__init__.py",
    "decisionholdem_like/hunl_abstraction.py",
    "decisionholdem_like/hunl_blueprint.py",
    "decisionholdem_like/hunl_external_sampling.py",
    "decisionholdem_like/secure_files.py",
    "tools/__init__.py",
    "tools/train_hunl_blueprint.py",
)
TRAINING_PACKAGE_ANCESTRY_FILES = ("__init__.py",)
_IDENTITY_IGNORED_PARTS = {
    ".pytest_cache",
    "__pycache__",
    "checkpoints",
    "data",
    "results",
}


def _canonical_bytes(value: object) -> bytes:
    return canonical_bytes(value)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256(path.read_bytes())


def _file_map_digest(files: Mapping[str, str]) -> str:
    return _sha256(_canonical_bytes(dict(files)))


def training_identity_snapshot() -> dict[str, object]:
    """Freeze every implementation/rules input that can affect resumed training."""

    common_files = secure_file_map(
        COMMON_ROOT,
        ignored_directories=_IDENTITY_IGNORED_PARTS,
    )
    route_files = stable_selected_file_map(
        PACKAGE_ROOT,
        TRAINING_IDENTITY_SOURCE_FILES,
    )
    ancestry_files = stable_selected_file_map(
        RESEARCH_LAB_ROOT,
        TRAINING_PACKAGE_ANCESTRY_FILES,
    )
    return {
        "abstraction": abstraction_contract(),
        "assets": {
            "external_assets": [],
            "hole_combo_space": 1326,
            "rank_or_equity_table": "Common cards.py exact evaluator; no external table",
        },
        "common": {
            "contract_version": CONTRACT_VERSION,
            "file_count": len(common_files),
            "files": common_files,
            "tree_sha256": _file_map_digest(common_files),
        },
        "package_ancestry": {
            "files": ancestry_files,
            "tree_sha256": _file_map_digest(ancestry_files),
        },
        "route_sources": {
            "files": route_files,
            "tree_sha256": _file_map_digest(route_files),
        },
        "rules": {
            "big_blind": BIG_BLIND,
            "initial_chips": INITIAL_CHIPS,
            "players": 2,
            "small_blind": SMALL_BLIND,
            "streets": ["preflop", "flop", "turn", "river"],
        },
        "schema": HUNL_TRAINING_IDENTITY_SCHEMA,
        "semantics": {
            "actions": "Common LegalActionSet plus route HUNL abstract_actions",
            "average_strategy": (
                "linear-weighted simple sampled average: during player-p regret "
                "traversal, add iteration*current_policy only at the other "
                "player's sampled decision nodes"
            ),
            "average_strategy_reference": HUNL_SIMPLE_AVERAGE_REFERENCE,
            "cards": "Common 0..51 cards, exact removal and rank_seven",
            "chance": "counter-based exact 52-card deal; no external RNG state",
            "transition": "Common NationalGameState apply_action/apply_chance",
            "utility": (
                "Common NationalGameState.terminal_utility divided by config "
                "utility_unit_chips"
            ),
        },
    }


def training_identity_digest() -> str:
    return _sha256(_canonical_bytes(training_identity_snapshot()))


def _atomic_json_write(path: Path, payload: object) -> None:
    atomic_json_write(path, payload)


def _exact_int(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class HUNLTrainingConfig:
    seed: int
    utility_unit_chips: int = 100
    max_nodes_per_traversal: int = 250_000

    def __post_init__(self) -> None:
        _exact_int(self.seed, "seed")
        _exact_int(self.utility_unit_chips, "utility_unit_chips", minimum=1)
        _exact_int(self.max_nodes_per_traversal, "max_nodes_per_traversal", minimum=1)
        if self.seed >= 2**63:
            raise ValueError("seed must fit in an unsigned 63-bit contract value")

    def to_dict(self) -> dict[str, int]:
        return {
            "max_nodes_per_traversal": self.max_nodes_per_traversal,
            "seed": self.seed,
            "utility_unit_chips": self.utility_unit_chips,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "HUNLTrainingConfig":
        if not isinstance(payload, dict) or set(payload) != {
            "max_nodes_per_traversal",
            "seed",
            "utility_unit_chips",
        }:
            raise ValueError("training config fields are invalid")
        return cls(
            seed=_exact_int(payload["seed"], "seed"),
            utility_unit_chips=_exact_int(
                payload["utility_unit_chips"], "utility_unit_chips", minimum=1
            ),
            max_nodes_per_traversal=_exact_int(
                payload["max_nodes_per_traversal"],
                "max_nodes_per_traversal",
                minimum=1,
            ),
        )


def regret_matching(regrets: Mapping[str, float], actions: Sequence[str]) -> dict[str, float]:
    if not actions or len(set(actions)) != len(actions):
        raise ValueError("regret matching requires unique actions")
    if any(action not in HUNL_ACTION_IDS for action in actions):
        raise ValueError("regret matching received an unknown action")
    if any(action not in actions for action in regrets):
        raise ValueError("regret row contains an excess action")
    normalized: dict[str, float] = {}
    for action in actions:
        value = regrets.get(action, 0.0)
        if type(value) not in (int, float):
            raise ValueError("regrets must be exact numeric values")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("regrets must be finite")
        normalized[action] = number
    positive = {action: max(0.0, normalized[action]) for action in actions}
    total = sum(positive.values())
    if total <= 0.0:
        probability = 1.0 / len(actions)
        return {action: probability for action in actions}
    return {action: positive[action] / total for action in actions}


def linear_regret_delta(
    action_values: Mapping[str, float],
    strategy: Mapping[str, float],
    iteration_weight: int,
) -> dict[str, float]:
    """Production LCFR node equation, kept pure for an independent oracle."""

    weight = _exact_int(iteration_weight, "iteration_weight", minimum=1)
    if set(action_values) != set(strategy) or not action_values:
        raise ValueError("action values and strategy must share a non-empty action set")
    if any(type(value) not in (int, float) for value in action_values.values()):
        raise ValueError("action values must be numeric")
    if any(type(value) not in (int, float) for value in strategy.values()):
        raise ValueError("strategy probabilities must be numeric")
    values = {key: float(value) for key, value in action_values.items()}
    probabilities = {key: float(value) for key, value in strategy.items()}
    if any(not math.isfinite(value) for value in values.values()):
        raise ValueError("action values must be finite")
    if any(not math.isfinite(value) or value < 0.0 for value in probabilities.values()):
        raise ValueError("strategy probabilities must be finite and nonnegative")
    if abs(sum(probabilities.values()) - 1.0) > 1e-12:
        raise ValueError("strategy probabilities must sum to one")
    node_value = sum(probabilities[action] * values[action] for action in values)
    return {
        action: float(weight) * (value - node_value)
        for action, value in values.items()
    }


def linear_simple_average_delta(
    strategy: Mapping[str, float],
    iteration_weight: int,
) -> dict[str, float]:
    """Linear-weighted OpenSpiel-style simple average at an opponent node."""

    weight = _exact_int(iteration_weight, "iteration_weight", minimum=1)
    if not strategy:
        raise ValueError("strategy must be non-empty")
    probabilities: dict[str, float] = {}
    for action, raw in strategy.items():
        if action not in HUNL_ACTION_IDS or type(raw) not in (int, float):
            raise ValueError("strategy contains an invalid action or probability")
        value = float(raw)
        if not math.isfinite(value) or value < 0.0:
            raise ValueError("strategy probabilities must be finite and nonnegative")
        probabilities[action] = value
    if abs(sum(probabilities.values()) - 1.0) > 1e-12:
        raise ValueError("strategy probabilities must sum to one")
    return {action: float(weight) * value for action, value in probabilities.items()}


def _counter_unit(seed: int, *parts: object) -> float:
    material = _canonical_bytes([seed, *parts])
    return int.from_bytes(hashlib.sha256(material).digest(), "big") / (1 << 256)


def deterministic_deal(seed: int, iteration: int) -> tuple[tuple[int, int], tuple[int, int], tuple[int, ...]]:
    _exact_int(seed, "seed")
    _exact_int(iteration, "iteration", minimum=1)
    deck = sorted(
        range(52),
        key=lambda card: (
            hashlib.sha256(
                _canonical_bytes([seed, "deal", iteration, card])
            ).digest(),
            card,
        ),
    )
    return (
        (deck[0], deck[1]),
        (deck[2], deck[3]),
        tuple(deck[4:9]),
    )


def _sample_action(
    strategy: Mapping[str, float],
    random_unit: float,
) -> str:
    cumulative = 0.0
    last = next(reversed(strategy))
    for action, probability in strategy.items():
        cumulative += probability
        if random_unit < cumulative:
            return action
    return last


@dataclass(slots=True)
class _Accumulator:
    regret: dict[str, dict[str, float]]
    strategy: dict[str, dict[str, float]]
    nodes: int = 0

    @classmethod
    def empty(cls) -> "_Accumulator":
        return cls({}, {})

    @staticmethod
    def _add(
        table: dict[str, dict[str, float]],
        key: str,
        values: Mapping[str, float],
    ) -> None:
        row = table.setdefault(key, {action: 0.0 for action in values})
        if set(row) != set(values):
            raise AssertionError("infoset action signature changed inside one iteration")
        for action, value in values.items():
            row[action] += float(value)


class HUNLExternalSamplingLCFR:
    """Deterministic sequential checkpoint segments over sampled HUNL trees."""

    def __init__(self, config: HUNLTrainingConfig):
        if type(config) is not HUNLTrainingConfig:
            raise TypeError("config must be the exact HUNLTrainingConfig type")
        self.config = config
        self.iterations_completed = 0
        self.traversals_completed = 0
        self.sampled_deals = 0
        self.nodes_visited = 0
        self.regrets: dict[str, dict[str, float]] = {}
        self.strategy_sums: dict[str, dict[str, float]] = {}

    def _strategy(
        self,
        key: str,
        actions: Sequence[str],
        base_regrets: Mapping[str, Mapping[str, float]],
    ) -> dict[str, float]:
        row = base_regrets.get(key, {})
        if row and set(row) != set(actions):
            raise AssertionError("checkpoint infoset action signature drifted")
        return regret_matching(row, actions)

    @staticmethod
    def _next_chance_cards(state: NationalGameState, board: Sequence[int]) -> tuple[int, ...]:
        if state.street is Street.PREFLOP:
            return tuple(board[:3])
        if state.street is Street.FLOP:
            return tuple(board[3:4])
        if state.street is Street.TURN:
            return tuple(board[4:5])
        raise AssertionError("river state cannot request chance")

    def _traverse(
        self,
        state: NationalGameState,
        board: Sequence[int],
        *,
        traverser: int,
        iteration: int,
        base_regrets: Mapping[str, Mapping[str, float]],
        accumulator: _Accumulator,
        path: tuple[str, ...],
    ) -> float:
        accumulator.nodes += 1
        if accumulator.nodes > self.config.max_nodes_per_traversal:
            raise RuntimeError("HUNL traversal exceeded the configured correctness guard")
        if state.is_terminal:
            return (
                state.terminal_utility()[traverser]
                / self.config.utility_unit_chips
            )
        if state.chance_pending:
            next_state = state.apply_chance(self._next_chance_cards(state, board))
            return self._traverse(
                next_state,
                board,
                traverser=traverser,
                iteration=iteration,
                base_regrets=base_regrets,
                accumulator=accumulator,
                path=path + (f"chance:{next_state.street.value}",),
            )

        actor = state.actor
        assert actor in (0, 1)
        specs = abstract_actions(state)
        abstraction = information_abstraction(state, actor)
        action_ids = tuple(spec.action_id for spec in specs)
        strategy = self._strategy(abstraction.key, action_ids, base_regrets)
        weight = iteration
        if actor == traverser:
            action_values: dict[str, float] = {}
            for spec in specs:
                action_values[spec.action_id] = self._traverse(
                    state.apply_action(spec.action),
                    board,
                    traverser=traverser,
                    iteration=iteration,
                    base_regrets=base_regrets,
                    accumulator=accumulator,
                    path=path + (f"p{actor}:{spec.action_id}",),
                )
            _Accumulator._add(
                accumulator.regret,
                abstraction.key,
                linear_regret_delta(action_values, strategy, weight),
            )
            return sum(
                strategy[action] * action_values[action] for action in action_ids
            )

        # OpenSpiel's simple external-sampling average is accumulated at the
        # sampled opponent node, not at the traverser's expanded nodes.  The
        # global iteration index supplies LCFR's linear weighting.
        _Accumulator._add(
            accumulator.strategy,
            abstraction.key,
            linear_simple_average_delta(strategy, weight),
        )
        random_unit = _counter_unit(
            self.config.seed,
            "opponent",
            iteration,
            traverser,
            state.full_state_id(),
            path,
        )
        selected = _sample_action(strategy, random_unit)
        spec = next(item for item in specs if item.action_id == selected)
        return self._traverse(
            state.apply_action(spec.action),
            board,
            traverser=traverser,
            iteration=iteration,
            base_regrets=base_regrets,
            accumulator=accumulator,
            path=path + (f"p{actor}:{selected}",),
        )

    @staticmethod
    def _merge(
        destination: dict[str, dict[str, float]],
        delta: Mapping[str, Mapping[str, float]],
    ) -> None:
        for key, values in delta.items():
            row = destination.setdefault(key, {action: 0.0 for action in values})
            if set(row) != set(values):
                raise AssertionError("LCFR table action signature changed")
            for action, value in values.items():
                row[action] += float(value)

    def _train_direct(self, count: int) -> None:
        count = _exact_int(count, "count", minimum=1)
        for iteration in range(
            self.iterations_completed + 1,
            self.iterations_completed + count + 1,
        ):
            first_hole, second_hole, board = deterministic_deal(
                self.config.seed, iteration
            )
            state = NationalGameState.new_hand(
                (iteration - 1) % 70 + 1,
                small_blind=(iteration - 1) % 2,
                hole_cards=(first_hole, second_hole),
            )
            base_regrets = {
                key: dict(values) for key, values in self.regrets.items()
            }
            combined = _Accumulator.empty()
            for traverser in (0, 1):
                current = _Accumulator.empty()
                self._traverse(
                    state,
                    board,
                    traverser=traverser,
                    iteration=iteration,
                    base_regrets=base_regrets,
                    accumulator=current,
                    path=(f"iteration:{iteration}", f"traverser:{traverser}"),
                )
                if current.nodes > self.config.max_nodes_per_traversal:
                    raise AssertionError("node guard was not enforced")
                combined.nodes += current.nodes
                self._merge(combined.regret, current.regret)
                self._merge(combined.strategy, current.strategy)
            self._merge(self.regrets, combined.regret)
            self._merge(self.strategy_sums, combined.strategy)
            self.iterations_completed = iteration
            self.traversals_completed += 2
            self.sampled_deals += 1
            self.nodes_visited += combined.nodes

    def average_strategy(self) -> dict[str, dict[str, float]]:
        result: dict[str, dict[str, float]] = {}
        for key in sorted(self.strategy_sums):
            legal = tuple(parse_infoset_key(key)["legal"])
            values = self.strategy_sums[key]
            total = sum(values.get(action, 0.0) for action in legal)
            if not math.isfinite(total) or total < 0.0:
                raise ValueError("strategy sums must be finite and nonnegative")
            if total <= 0.0:
                raise ValueError("simple-average row must have positive sampled mass")
            result[key] = {
                action: values.get(action, 0.0) / total for action in legal
            }
        return result

    @staticmethod
    def _validate_table(
        payload: object,
        *,
        name: str,
        nonnegative: bool,
    ) -> dict[str, dict[str, float]]:
        if not isinstance(payload, dict):
            raise ValueError(f"{name} must be an object")
        table: dict[str, dict[str, float]] = {}
        for key, raw_row in payload.items():
            parsed = parse_infoset_key(key)
            legal = tuple(parsed["legal"])
            if not isinstance(raw_row, dict) or set(raw_row) != set(legal):
                raise ValueError(f"{name} row action signature mismatch")
            row: dict[str, float] = {}
            for action in legal:
                value = raw_row[action]
                if type(value) not in (int, float):
                    raise ValueError(f"{name} contains a non-numeric value")
                normalized = float(value)
                if not math.isfinite(normalized) or (nonnegative and normalized < 0.0):
                    raise ValueError(f"{name} contains an invalid value")
                row[action] = normalized
            table[key] = row
        return table

    def checkpoint_payload(self) -> dict[str, object]:
        body = {
            "abstraction": abstraction_contract(),
            "algorithm": HUNL_LCFR_ALGORITHM,
            "config": self.config.to_dict(),
            "iterations_completed": self.iterations_completed,
            "nodes_visited": self.nodes_visited,
            "regrets": self.regrets,
            "sampled_deals": self.sampled_deals,
            "strategy_sums": self.strategy_sums,
            "training_identity": training_identity_snapshot(),
            "traversals_completed": self.traversals_completed,
        }
        return {
            "body": body,
            "body_sha256": _sha256(_canonical_bytes(body)),
            "schema": HUNL_CHECKPOINT_SCHEMA,
        }

    def checkpoint_digest(self) -> str:
        return str(self.checkpoint_payload()["body_sha256"])

    @classmethod
    def from_checkpoint_payload(cls, payload: object) -> "HUNLExternalSamplingLCFR":
        if not isinstance(payload, dict) or set(payload) != {
            "body",
            "body_sha256",
            "schema",
        }:
            raise ValueError("checkpoint wrapper fields are invalid")
        if payload["schema"] != HUNL_CHECKPOINT_SCHEMA:
            raise ValueError("checkpoint schema mismatch")
        body = payload["body"]
        if not isinstance(body, dict) or set(body) != {
            "abstraction",
            "algorithm",
            "config",
            "iterations_completed",
            "nodes_visited",
            "regrets",
            "sampled_deals",
            "strategy_sums",
            "training_identity",
            "traversals_completed",
        }:
            raise ValueError("checkpoint body fields are invalid")
        digest = payload["body_sha256"]
        if type(digest) is not str or digest != _sha256(_canonical_bytes(body)):
            raise ValueError("checkpoint content hash mismatch")
        if body["algorithm"] != HUNL_LCFR_ALGORITHM:
            raise ValueError("checkpoint algorithm mismatch")
        if body["abstraction"] != abstraction_contract():
            raise ValueError("checkpoint abstraction contract mismatch")
        if body["training_identity"] != training_identity_snapshot():
            raise ValueError("checkpoint frozen training identity mismatch")
        config = HUNLTrainingConfig.from_dict(body["config"])
        iterations = _exact_int(body["iterations_completed"], "iterations_completed")
        traversals = _exact_int(body["traversals_completed"], "traversals_completed")
        sampled = _exact_int(body["sampled_deals"], "sampled_deals")
        nodes = _exact_int(body["nodes_visited"], "nodes_visited")
        if traversals != iterations * 2 or sampled != iterations:
            raise ValueError("checkpoint counters are inconsistent")
        regrets = cls._validate_table(body["regrets"], name="regrets", nonnegative=False)
        strategy = cls._validate_table(
            body["strategy_sums"], name="strategy_sums", nonnegative=True
        )
        trainer = cls(config)
        trainer.iterations_completed = iterations
        trainer.traversals_completed = traversals
        trainer.sampled_deals = sampled
        trainer.nodes_visited = nodes
        trainer.regrets = regrets
        trainer.strategy_sums = strategy
        return trainer

    @classmethod
    def load_checkpoint(cls, path: str | Path) -> "HUNLExternalSamplingLCFR":
        payload = strict_json_loads(stable_read_path(path))
        return cls.from_checkpoint_payload(payload)

    def save_checkpoint(self, path: str | Path) -> None:
        _atomic_json_write(Path(path), self.checkpoint_payload())

    @staticmethod
    def _validate_shard(payload: object) -> tuple[dict[str, Any], "HUNLExternalSamplingLCFR"]:
        if not isinstance(payload, dict) or set(payload) != {
            "body",
            "body_sha256",
            "schema",
        }:
            raise ValueError("shard wrapper fields are invalid")
        if payload["schema"] != HUNL_SHARD_SCHEMA:
            raise ValueError("shard schema mismatch")
        body = payload["body"]
        if not isinstance(body, dict) or set(body) != {
            "end_iteration",
            "result_checkpoint",
            "start_checkpoint_sha256",
            "start_iteration",
            "training_identity_sha256",
        }:
            raise ValueError("shard body fields are invalid")
        if payload["body_sha256"] != _sha256(_canonical_bytes(body)):
            raise ValueError("shard content hash mismatch")
        start = _exact_int(body["start_iteration"], "start_iteration")
        end = _exact_int(body["end_iteration"], "end_iteration", minimum=1)
        if end <= start:
            raise ValueError("shard range must advance training")
        start_digest = body["start_checkpoint_sha256"]
        if type(start_digest) is not str or len(start_digest) != 64:
            raise ValueError("shard start digest is invalid")
        result = HUNLExternalSamplingLCFR.from_checkpoint_payload(
            body["result_checkpoint"]
        )
        if body["training_identity_sha256"] != training_identity_digest():
            raise ValueError("shard frozen training identity mismatch")
        if result.iterations_completed != end:
            raise ValueError("shard result counter does not match its range")
        return body, result

    def build_shard(self, count: int) -> dict[str, object]:
        count = _exact_int(count, "count", minimum=1)
        start = self.iterations_completed
        start_digest = self.checkpoint_digest()
        working = self.from_checkpoint_payload(self.checkpoint_payload())
        working._train_direct(count)
        body = {
            "end_iteration": working.iterations_completed,
            "result_checkpoint": working.checkpoint_payload(),
            "start_checkpoint_sha256": start_digest,
            "start_iteration": start,
            "training_identity_sha256": training_identity_digest(),
        }
        return {
            "body": body,
            "body_sha256": _sha256(_canonical_bytes(body)),
            "schema": HUNL_SHARD_SCHEMA,
        }

    def apply_shard(self, payload: object) -> None:
        """Validate completely, then atomically replace in-memory training state."""

        body, result = self._validate_shard(payload)
        if body["start_iteration"] != self.iterations_completed:
            raise ValueError("shard does not start at the current iteration")
        if body["start_checkpoint_sha256"] != self.checkpoint_digest():
            raise ValueError("shard input checkpoint binding mismatch")
        if result.config != self.config:
            raise ValueError("shard training config mismatch")
        self.iterations_completed = result.iterations_completed
        self.traversals_completed = result.traversals_completed
        self.sampled_deals = result.sampled_deals
        self.nodes_visited = result.nodes_visited
        self.regrets = result.regrets
        self.strategy_sums = result.strategy_sums

    def train_to(self, target_iterations: int, *, shard_size: int = 1) -> None:
        target = _exact_int(target_iterations, "target_iterations")
        size = _exact_int(shard_size, "shard_size", minimum=1)
        if target < self.iterations_completed:
            raise ValueError("cannot train backwards")
        while self.iterations_completed < target:
            count = min(size, target - self.iterations_completed)
            self.apply_shard(self.build_shard(count))

    @staticmethod
    def save_shard(path: str | Path, payload: object) -> None:
        HUNLExternalSamplingLCFR._validate_shard(payload)
        _atomic_json_write(Path(path), payload)

    @staticmethod
    def load_shard(path: str | Path) -> dict[str, object]:
        payload = strict_json_loads(stable_read_path(path))
        HUNLExternalSamplingLCFR._validate_shard(payload)
        return payload
