"""Strict sparse compressed HUNL blueprint artifact and runtime policy."""

from __future__ import annotations

import hashlib
import math
import random
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from bots.research_native_lab.common_contracts import Action, NationalGameState

from ..core.identity import canonical_json_bytes, file_sha256, payload_sha256, require_sha256
from ..core.strict_io import atomic_write_bytes, read_regular_bytes, strict_json_loads
from .hunl_abstraction import (
    MATERIAL_L1_TOLERANCE,
    HUNLAbstractionConfig,
    backoff_keys_from_exact_key,
    information_descriptor,
    legal_action_map,
)
from .hunl_game import HUNLTrainingGame
from .hunl_training import HUNLTrainingContract
from .mccfr import SolverConfig, SolverState


ARTIFACT_SCHEMA = "route-b-sparse-blueprint-v2-perfect-recall"
ARTIFACT_FORMAT = 2
ARTIFACT_MAGIC = b"RBHUNLBP2\n"
ARTIFACT_CODEC = "zlib-9"
MAX_HEADER_BYTES = 64 * 1024
MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024


def _strict_keys(payload: Mapping[str, Any], expected: set[str], context: str) -> None:
    if type(payload) is not dict:
        raise TypeError(f"{context} must be an exact object")
    if set(payload) != expected:
        raise ValueError(f"{context} keys differ from strict schema")


def _numeric(value: Any, context: str, *, nonnegative: bool = True) -> float:
    if type(value) not in (int, float):
        raise TypeError(f"{context} must be exact numeric, not bool/string")
    result = float(value)
    if not math.isfinite(result) or (nonnegative and result < 0.0):
        raise ValueError(f"{context} must be finite and nonnegative")
    return result


def _distribution_l1(probabilities: tuple[float, ...]) -> float:
    uniform = 1.0 / len(probabilities)
    return math.fsum(abs(value - uniform) for value in probabilities)


def _normalized_row(
    actions: tuple[str, ...],
    accumulator: Mapping[str, float],
) -> tuple[tuple[float, ...], float] | None:
    if set(accumulator) != set(actions):
        raise ValueError("average accumulator action set is incomplete")
    values = tuple(_numeric(accumulator[action], "strategy accumulator") for action in actions)
    total = math.fsum(values)
    if total <= 0.0:
        return None
    probabilities = tuple(value / total for value in values)
    return probabilities, total


def _row_payload(
    actions: tuple[str, ...],
    probabilities: tuple[float, ...],
    total_weight: float,
    source_exact_rows: int,
) -> dict[str, Any]:
    return {
        "actions": list(actions),
        "probabilities": list(probabilities),
        "total_weight": total_weight,
        "source_exact_rows": source_exact_rows,
        "l1_from_uniform": _distribution_l1(probabilities),
    }


def compile_blueprint_payload(
    game: HUNLTrainingGame,
    state: SolverState,
    *,
    run_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile only positive average accumulators; no dense hand matrix."""

    contract = HUNLTrainingContract.from_game_state(game, state, run_contract)
    exact_rows: dict[str, dict[str, Any]] = {}
    backoff_accumulators: dict[str, dict[str, float]] = {}
    backoff_sources: dict[str, int] = {}
    backoff_actions: dict[str, tuple[str, ...]] = {}

    for exact_key, actions in sorted(state.actions.items()):
        accumulator = state.strategy_sum.get(exact_key)
        if not accumulator:
            continue
        normalized = _normalized_row(actions, accumulator)
        if normalized is None:
            continue
        probabilities, total = normalized
        exact_rows[exact_key] = _row_payload(actions, probabilities, total, 1)
        for backoff_key in backoff_keys_from_exact_key(exact_key):
            previous_actions = backoff_actions.setdefault(backoff_key, actions)
            if previous_actions != actions:
                raise ValueError("backoff key merged incompatible legal action sets")
            row = backoff_accumulators.setdefault(
                backoff_key,
                {action: 0.0 for action in actions},
            )
            for action in actions:
                row[action] += float(accumulator[action])
            backoff_sources[backoff_key] = backoff_sources.get(backoff_key, 0) + 1

    backoff_rows: dict[str, dict[str, Any]] = {}
    for key, accumulator in sorted(backoff_accumulators.items()):
        actions = backoff_actions[key]
        normalized = _normalized_row(actions, accumulator)
        if normalized is None:
            continue
        probabilities, total = normalized
        backoff_rows[key] = _row_payload(
            actions,
            probabilities,
            total,
            backoff_sources[key],
        )

    l1_values = tuple(
        float(row["l1_from_uniform"])
        for table in (exact_rows, backoff_rows)
        for row in table.values()
    )
    exact_l1 = tuple(float(row["l1_from_uniform"]) for row in exact_rows.values())
    backoff_l1 = tuple(float(row["l1_from_uniform"]) for row in backoff_rows.values())
    return {
        "schema": ARTIFACT_SCHEMA,
        "format_version": ARTIFACT_FORMAT,
        "training_contract": contract.to_payload(),
        "training_contract_sha256": contract.digest,
        "abstraction_config": game.abstraction.to_payload(),
        "compiler": {
            "schema": "route-b-blueprint-compiler-v2",
            "source_sha256": file_sha256(Path(__file__)),
        },
        "source_solver_sha256": state.digest,
        "seeds": {
            "training": state.config.seed,
            "domain": "training-only-counter-root-v1",
        },
        "exact_rows": exact_rows,
        "backoff_rows": backoff_rows,
        "statistics": {
            "material_l1_tolerance": MATERIAL_L1_TOLERANCE,
            "exact_row_count": len(exact_rows),
            "backoff_row_count": len(backoff_rows),
            "materially_nonuniform_exact_rows": sum(
                value > MATERIAL_L1_TOLERANCE for value in exact_l1
            ),
            "materially_nonuniform_backoff_rows": sum(
                value > MATERIAL_L1_TOLERANCE for value in backoff_l1
            ),
            "materially_nonuniform_all_rows": sum(
                value > MATERIAL_L1_TOLERANCE for value in l1_values
            ),
            "max_l1_from_uniform": max(l1_values, default=0.0),
        },
        "resources": {
            "training_batches": state.batch_index,
            "training_trajectories": state.trajectories,
            "training_node_touches": state.node_touches,
            "solver_information_rows": len(state.actions),
            "stored_probability_values": sum(
                len(row["probabilities"])
                for table in (exact_rows, backoff_rows)
                for row in table.values()
            ),
        },
    }


def save_blueprint_artifact(
    path: str | Path,
    game: HUNLTrainingGame,
    state: SolverState,
    *,
    root: str | Path | None = None,
    run_contract: Mapping[str, Any] | None = None,
) -> str:
    payload = compile_blueprint_payload(game, state, run_contract=run_contract)
    raw = canonical_json_bytes(payload)
    compressed = zlib.compress(raw, level=9)
    header = {
        "schema": ARTIFACT_SCHEMA,
        "format_version": ARTIFACT_FORMAT,
        "codec": ARTIFACT_CODEC,
        "payload_sha256": hashlib.sha256(raw).hexdigest(),
        "compressed_sha256": hashlib.sha256(compressed).hexdigest(),
        "uncompressed_bytes": len(raw),
        "compressed_bytes": len(compressed),
        "training_contract_sha256": payload["training_contract_sha256"],
        "source_solver_sha256": payload["source_solver_sha256"],
    }
    header_bytes = canonical_json_bytes(header)
    if len(header_bytes) > MAX_HEADER_BYTES:
        raise ValueError("artifact header exceeds strict limit")
    content = ARTIFACT_MAGIC + struct.pack(">I", len(header_bytes)) + header_bytes + compressed
    atomic_write_bytes(path, content, root=root)
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True, slots=True)
class BlueprintRow:
    actions: tuple[str, ...]
    probabilities: tuple[float, ...]
    total_weight: float
    source_exact_rows: int
    l1_from_uniform: float

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "BlueprintRow":
        _strict_keys(
            payload,
            {
                "actions",
                "probabilities",
                "total_weight",
                "source_exact_rows",
                "l1_from_uniform",
            },
            "blueprint row",
        )
        actions_raw = payload["actions"]
        probabilities_raw = payload["probabilities"]
        if type(actions_raw) is not list or type(probabilities_raw) is not list:
            raise TypeError("row actions/probabilities must be arrays")
        actions = tuple(actions_raw)
        if (
            not actions
            or any(type(action) is not str or not action for action in actions)
            or len(set(actions)) != len(actions)
        ):
            raise ValueError("row actions must be unique nonempty strings")
        probabilities = tuple(
            _numeric(value, "row probability") for value in probabilities_raw
        )
        if len(probabilities) != len(actions):
            raise ValueError("row probability count differs from actions")
        if abs(math.fsum(probabilities) - 1.0) > 1e-12:
            raise ValueError("row probabilities do not sum to one")
        source_count = payload["source_exact_rows"]
        if type(source_count) is not int or source_count <= 0:
            raise ValueError("source_exact_rows must be a positive exact integer")
        total_weight = _numeric(payload["total_weight"], "row total weight")
        if total_weight <= 0.0:
            raise ValueError("row total weight must be positive")
        l1 = _numeric(payload["l1_from_uniform"], "row L1")
        actual_l1 = _distribution_l1(probabilities)
        if abs(actual_l1 - l1) > 1e-12:
            raise ValueError("row L1 statistic disagrees with probabilities")
        return cls(actions, probabilities, total_weight, source_count, l1)


@dataclass(frozen=True, slots=True)
class LoadedBlueprint:
    artifact_sha256: str
    payload_sha256: str
    source_solver_sha256: str
    contract: HUNLTrainingContract
    abstraction: HUNLAbstractionConfig
    exact_rows: Mapping[str, BlueprintRow]
    backoff_rows: Mapping[str, BlueprintRow]
    statistics: Mapping[str, Any]
    resources: Mapping[str, Any]
    training_seed: int


def _parse_rows(value: Any, level: str) -> dict[str, BlueprintRow]:
    if type(value) is not dict:
        raise TypeError(f"{level} rows must be an exact object")
    result: dict[str, BlueprintRow] = {}
    required_marker = f":{level}:" if level == "exact" else ":backoff"
    for key, row in value.items():
        if type(key) is not str or required_marker not in key:
            raise ValueError(f"invalid {level} row key")
        if type(row) is not dict:
            raise TypeError(f"{level} row must be an exact object")
        result[key] = BlueprintRow.from_payload(row)
    return result


def load_blueprint_artifact(
    path: str | Path,
    game: HUNLTrainingGame,
    *,
    root: str | Path | None = None,
) -> LoadedBlueprint:
    content = read_regular_bytes(path, root=root, max_bytes=MAX_UNCOMPRESSED_BYTES)
    artifact_sha = hashlib.sha256(content).hexdigest()
    prefix = len(ARTIFACT_MAGIC)
    if not content.startswith(ARTIFACT_MAGIC) or len(content) < prefix + 4:
        raise ValueError("artifact magic is invalid")
    header_size = struct.unpack(">I", content[prefix : prefix + 4])[0]
    if not 0 < header_size <= MAX_HEADER_BYTES:
        raise ValueError("artifact header size is invalid")
    header_end = prefix + 4 + header_size
    if header_end > len(content):
        raise ValueError("artifact header is truncated")
    header = strict_json_loads(content[prefix + 4 : header_end], context="artifact header")
    _strict_keys(
        header,
        {
            "schema",
            "format_version",
            "codec",
            "payload_sha256",
            "compressed_sha256",
            "uncompressed_bytes",
            "compressed_bytes",
            "training_contract_sha256",
            "source_solver_sha256",
        },
        "artifact header",
    )
    if header["schema"] != ARTIFACT_SCHEMA or header["format_version"] != ARTIFACT_FORMAT:
        raise ValueError("unsupported artifact format")
    if header["codec"] != ARTIFACT_CODEC:
        raise ValueError("unsupported artifact codec")
    for field in (
        "payload_sha256",
        "compressed_sha256",
        "training_contract_sha256",
        "source_solver_sha256",
    ):
        require_sha256(header[field], f"artifact header {field}")
    for field in ("uncompressed_bytes", "compressed_bytes"):
        if type(header[field]) is not int or header[field] < 0:
            raise ValueError(f"artifact header {field} must be nonnegative exact int")
    if header["uncompressed_bytes"] > MAX_UNCOMPRESSED_BYTES:
        raise ValueError("artifact declares excessive expansion")
    compressed = content[header_end:]
    if len(compressed) != header["compressed_bytes"]:
        raise ValueError("artifact compressed byte count mismatch")
    if hashlib.sha256(compressed).hexdigest() != header["compressed_sha256"]:
        raise ValueError("artifact compressed digest mismatch")
    decompressor = zlib.decompressobj()
    raw = decompressor.decompress(compressed, header["uncompressed_bytes"] + 1)
    if (
        len(raw) > header["uncompressed_bytes"]
        or
        not decompressor.eof
        or decompressor.unused_data
        or decompressor.unconsumed_tail
        or len(raw) != header["uncompressed_bytes"]
    ):
        raise ValueError("artifact compressed stream is malformed or size-drifted")
    if hashlib.sha256(raw).hexdigest() != header["payload_sha256"]:
        raise ValueError("artifact payload digest mismatch")
    payload = strict_json_loads(raw, context="artifact payload")
    _strict_keys(
        payload,
        {
            "schema",
            "format_version",
            "training_contract",
            "training_contract_sha256",
            "abstraction_config",
            "compiler",
            "source_solver_sha256",
            "seeds",
            "exact_rows",
            "backoff_rows",
            "statistics",
            "resources",
        },
        "artifact payload",
    )
    if payload["schema"] != ARTIFACT_SCHEMA or payload["format_version"] != ARTIFACT_FORMAT:
        raise ValueError("artifact payload format differs from header")
    if payload["training_contract_sha256"] != header["training_contract_sha256"]:
        raise ValueError("artifact contract header/payload mismatch")
    if payload["source_solver_sha256"] != header["source_solver_sha256"]:
        raise ValueError("artifact solver header/payload mismatch")
    if type(payload["training_contract"]) is not dict:
        raise TypeError("artifact training contract must be an exact object")
    contract = HUNLTrainingContract.from_payload(payload["training_contract"])
    if contract.digest != payload["training_contract_sha256"]:
        raise ValueError("artifact training contract digest mismatch")
    solver_config = SolverConfig.from_payload(contract.solver_config)
    expected_state = SolverState.new_for_game(game, solver_config)
    expected_contract = HUNLTrainingContract.from_game_state(
        game,
        expected_state,
        contract.run_contract,
    )
    if contract.to_payload() != expected_contract.to_payload():
        raise ValueError("artifact contract drifts from current game/backend/assets")
    abstraction_payload = payload["abstraction_config"]
    if type(abstraction_payload) is not dict:
        raise TypeError("artifact abstraction_config must be an exact object")
    abstraction = HUNLAbstractionConfig(**abstraction_payload)
    if abstraction != game.abstraction:
        raise ValueError("artifact abstraction config differs from current game")
    compiler = payload["compiler"]
    _strict_keys(compiler, {"schema", "source_sha256"}, "artifact compiler")
    if compiler["schema"] != "route-b-blueprint-compiler-v2":
        raise ValueError("unsupported artifact compiler schema")
    require_sha256(compiler["source_sha256"], "artifact compiler source")
    if compiler["source_sha256"] != file_sha256(Path(__file__)):
        raise ValueError("artifact compiler/loader source drifted")
    exact = _parse_rows(payload["exact_rows"], "exact")
    backoff = _parse_rows(payload["backoff_rows"], "backoff")
    seeds = payload["seeds"]
    _strict_keys(seeds, {"training", "domain"}, "artifact seeds")
    if (
        type(seeds["training"]) is not int
        or seeds["domain"] != "training-only-counter-root-v1"
    ):
        raise ValueError("artifact training seed domain is invalid")
    statistics = payload["statistics"]
    resources = payload["resources"]
    if type(statistics) is not dict or type(resources) is not dict:
        raise TypeError("artifact statistics/resources must be exact objects")
    expected_l1 = tuple(row.l1_from_uniform for row in (*exact.values(), *backoff.values()))
    expected_stats = {
        "material_l1_tolerance": MATERIAL_L1_TOLERANCE,
        "exact_row_count": len(exact),
        "backoff_row_count": len(backoff),
        "materially_nonuniform_exact_rows": sum(
            row.l1_from_uniform > MATERIAL_L1_TOLERANCE for row in exact.values()
        ),
        "materially_nonuniform_backoff_rows": sum(
            row.l1_from_uniform > MATERIAL_L1_TOLERANCE for row in backoff.values()
        ),
        "materially_nonuniform_all_rows": sum(
            value > MATERIAL_L1_TOLERANCE for value in expected_l1
        ),
        "max_l1_from_uniform": max(expected_l1, default=0.0),
    }
    if statistics != expected_stats:
        raise ValueError("artifact row statistics disagree with stored policy")
    expected_resource_keys = {
        "training_batches",
        "training_trajectories",
        "training_node_touches",
        "solver_information_rows",
        "stored_probability_values",
    }
    if set(resources) != expected_resource_keys or any(
        type(value) is not int or value < 0 for value in resources.values()
    ):
        raise ValueError("artifact resources have invalid schema or counters")
    stored_values = sum(
        len(row.probabilities) for row in (*exact.values(), *backoff.values())
    )
    if resources["stored_probability_values"] != stored_values:
        raise ValueError("artifact stored probability count disagrees with rows")
    if resources["solver_information_rows"] < len(exact):
        raise ValueError("artifact exact rows exceed source solver row count")
    return LoadedBlueprint(
        artifact_sha,
        header["payload_sha256"],
        header["source_solver_sha256"],
        contract,
        abstraction,
        exact,
        backoff,
        statistics,
        resources,
        seeds["training"],
    )


@dataclass(slots=True)
class BlueprintLookupCounters:
    exact_hits: int = 0
    backoff_hits: int = 0
    uniform_emergency: int = 0
    materially_nonuniform_decisions: int = 0


@dataclass(frozen=True, slots=True)
class BlueprintDecision:
    action: Action
    source: str
    probabilities: tuple[float, ...]
    action_labels: tuple[str, ...]
    l1_from_uniform: float


class BlueprintPolicy:
    """Exact row, then accumulator-derived backoff, then uniform emergency."""

    def __init__(self, blueprint: LoadedBlueprint):
        if type(blueprint) is not LoadedBlueprint:
            raise TypeError("policy requires exact LoadedBlueprint")
        self.blueprint = blueprint
        self.counters = BlueprintLookupCounters()

    def decide(
        self,
        state: NationalGameState,
        player: int,
        *,
        policy_seed: int,
        decision_counter: int,
    ) -> BlueprintDecision:
        if type(policy_seed) is not int or type(decision_counter) is not int:
            raise TypeError("policy seed/counter must be exact integers")
        if decision_counter < 0:
            raise ValueError("decision_counter must be nonnegative")
        descriptor = information_descriptor(state, player, self.blueprint.abstraction)
        labels = descriptor.action_labels
        row = self.blueprint.exact_rows.get(descriptor.exact_key)
        source = "exact"
        if row is None:
            for index, key in enumerate(descriptor.backoff_keys, start=1):
                candidate = self.blueprint.backoff_rows.get(key)
                if candidate is not None:
                    row = candidate
                    source = f"backoff{index}"
                    break
        if row is not None and row.actions != labels:
            # A content-bound key should make this impossible.  Treat it as a
            # fail-closed miss, not as permission to remap probabilities.
            row = None
        if row is None:
            probabilities = tuple(1.0 / len(labels) for _ in labels)
            source = "uniform_emergency"
            self.counters.uniform_emergency += 1
        else:
            probabilities = row.probabilities
            if source == "exact":
                self.counters.exact_hits += 1
            else:
                self.counters.backoff_hits += 1
        l1 = _distribution_l1(probabilities)
        if l1 > MATERIAL_L1_TOLERANCE:
            self.counters.materially_nonuniform_decisions += 1
        seed_material = (
            f"{self.blueprint.artifact_sha256}:{policy_seed}:"
            f"{decision_counter}:{descriptor.exact_key}"
        ).encode("ascii")
        rng = random.Random(int.from_bytes(hashlib.sha256(seed_material).digest()[:16], "big"))
        threshold = rng.random()
        cumulative = 0.0
        chosen = len(labels) - 1
        for index, probability in enumerate(probabilities):
            cumulative += probability
            if threshold < cumulative:
                chosen = index
                break
        action_map = {item.label: item.common_action for item in legal_action_map(state)}
        action = action_map[labels[chosen]]
        return BlueprintDecision(action, source, probabilities, labels, l1)
