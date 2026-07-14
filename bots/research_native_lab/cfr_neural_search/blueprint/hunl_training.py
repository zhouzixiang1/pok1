"""Content-bound independent-shard ES-MCCFR training for Route-B HUNL."""

from __future__ import annotations

import os
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..core.identity import file_sha256, payload_sha256, require_sha256
from ..core.strict_io import atomic_json_write, load_hashed_json
from .hunl_abstraction import HUNLAbstractionConfig
from .hunl_game import HUNLTrainingGame, hunl_component_identities
from .mccfr import (
    ShardDelta,
    SolverConfig,
    SolverState,
    apply_shards,
    build_shard,
)


HUNL_TRAINING_FORMAT = 2
HUNL_TRAINING_CONTRACT_SCHEMA = "route-b-hunl-esmccfr-contract-v2"
HUNL_SHARD_SCHEMA = "route-b-hunl-independent-shard-v2"
HUNL_CHECKPOINT_SCHEMA = "route-b-hunl-checkpoint-v2"
LIBRARY_RUN_CONTRACT_SCHEMA = "route-b-hunl-library-run-v1"


def library_run_contract() -> dict[str, Any]:
    """Return the explicit non-formal contract used by library/unit callers."""

    return {
        "schema": LIBRARY_RUN_CONTRACT_SCHEMA,
        "authority": "library-and-tests-only",
    }


def _require_exact_keys(
    payload: Mapping[str, Any],
    expected: set[str],
    context: str,
) -> None:
    if type(payload) is not dict:
        raise TypeError(f"{context} must be an exact JSON object")
    if set(payload) != expected:
        raise ValueError(f"{context} keys differ from strict schema")


def _backend_sources() -> dict[str, str]:
    route_root = Path(__file__).parents[1]
    paths = (
        Path(__file__),
        route_root / "blueprint" / "mccfr.py",
        route_root / "blueprint" / "hunl_game.py",
        route_root / "blueprint" / "hunl_abstraction.py",
        route_root / "core" / "strict_io.py",
    )
    return {
        path.relative_to(route_root).as_posix(): file_sha256(path)
        for path in paths
    }


@dataclass(frozen=True, slots=True)
class HUNLTrainingContract:
    component_identities: tuple[tuple[str, str], ...]
    solver_config: Mapping[str, Any]
    solver_config_sha256: str
    backend_sources: tuple[tuple[str, str], ...]
    backend_sha256: str
    run_contract: Mapping[str, Any]
    run_contract_sha256: str

    def __post_init__(self) -> None:
        if type(self.component_identities) is not tuple or type(self.backend_sources) is not tuple:
            raise TypeError("training contract identities must be tuples")
        for context, rows in (
            ("component identities", self.component_identities),
            ("backend sources", self.backend_sources),
        ):
            if any(
                type(row) is not tuple
                or len(row) != 2
                or type(row[0]) is not str
                or type(row[1]) is not str
                for row in rows
            ):
                raise TypeError(f"{context} rows must be exact string pairs")
            if tuple(sorted(rows)) != rows or len({key for key, _ in rows}) != len(rows):
                raise ValueError(f"{context} must be sorted and unique")
            for key, digest in rows:
                require_sha256(digest, f"{context}.{key}")
        if type(self.solver_config) is not dict:
            raise TypeError("solver_config must be an exact mapping")
        if type(self.run_contract) is not dict:
            raise TypeError("run_contract must be an exact mapping")
        require_sha256(self.solver_config_sha256, "solver config digest")
        require_sha256(self.backend_sha256, "backend digest")
        require_sha256(self.run_contract_sha256, "run contract digest")
        if payload_sha256(self.solver_config) != self.solver_config_sha256:
            raise ValueError("solver config digest mismatch")
        if payload_sha256({"files": dict(self.backend_sources)}) != self.backend_sha256:
            raise ValueError("backend source digest mismatch")
        if payload_sha256(self.run_contract) != self.run_contract_sha256:
            raise ValueError("run contract digest mismatch")

    @classmethod
    def from_game_state(
        cls,
        game: HUNLTrainingGame,
        state: SolverState,
        run_contract: Mapping[str, Any] | None = None,
    ) -> "HUNLTrainingContract":
        if type(game) is not HUNLTrainingGame or type(state) is not SolverState:
            raise TypeError("contract requires exact HUNL game and SolverState")
        state.validate()
        components = dict(hunl_component_identities(game))
        if state.game_identity_sha256 != components["game_sha256"]:
            raise ValueError("solver state is not bound to this full HUNL identity")
        solver_payload = state.config.to_payload()
        sources = _backend_sources()
        run_payload = dict(
            library_run_contract() if run_contract is None else run_contract
        )
        return cls(
            component_identities=tuple(sorted(components.items())),
            solver_config=solver_payload,
            solver_config_sha256=payload_sha256(solver_payload),
            backend_sources=tuple(sorted(sources.items())),
            backend_sha256=payload_sha256({"files": sources}),
            run_contract=run_payload,
            run_contract_sha256=payload_sha256(run_payload),
        )

    @property
    def digest(self) -> str:
        return payload_sha256(self.to_payload())

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": HUNL_TRAINING_CONTRACT_SCHEMA,
            "component_identities": dict(self.component_identities),
            "solver_config": dict(self.solver_config),
            "solver_config_sha256": self.solver_config_sha256,
            "backend_sources": dict(self.backend_sources),
            "backend_sha256": self.backend_sha256,
            "run_contract": dict(self.run_contract),
            "run_contract_sha256": self.run_contract_sha256,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "HUNLTrainingContract":
        _require_exact_keys(
            payload,
            {
                "schema",
                "component_identities",
                "solver_config",
                "solver_config_sha256",
                "backend_sources",
                "backend_sha256",
                "run_contract",
                "run_contract_sha256",
            },
            "training contract",
        )
        if payload["schema"] != HUNL_TRAINING_CONTRACT_SCHEMA:
            raise ValueError("unsupported HUNL training contract")
        components = payload["component_identities"]
        sources = payload["backend_sources"]
        config = payload["solver_config"]
        run_contract = payload["run_contract"]
        if (
            type(components) is not dict
            or type(sources) is not dict
            or type(config) is not dict
            or type(run_contract) is not dict
        ):
            raise TypeError("training contract maps must be exact objects")
        return cls(
            component_identities=tuple(sorted(components.items())),
            solver_config=config,
            solver_config_sha256=payload["solver_config_sha256"],
            backend_sources=tuple(sorted(sources.items())),
            backend_sha256=payload["backend_sha256"],
            run_contract=run_contract,
            run_contract_sha256=payload["run_contract_sha256"],
        )


@dataclass(frozen=True, slots=True)
class HUNLShardEnvelope:
    contract: HUNLTrainingContract
    contract_sha256: str
    base_solver_sha256: str
    shard: ShardDelta

    def __post_init__(self) -> None:
        if type(self.contract) is not HUNLTrainingContract:
            raise TypeError("shard contract must be exact HUNLTrainingContract")
        require_sha256(self.contract_sha256, "shard contract digest")
        require_sha256(self.base_solver_sha256, "shard base digest")
        if self.contract.digest != self.contract_sha256:
            raise ValueError("shard contract digest mismatch")
        if type(self.shard) is not ShardDelta:
            raise TypeError("shard payload must be exact ShardDelta")
        if self.shard.base_digest != self.base_solver_sha256:
            raise ValueError("shard generic/base envelope digest mismatch")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": HUNL_SHARD_SCHEMA,
            "format_version": HUNL_TRAINING_FORMAT,
            "contract": self.contract.to_payload(),
            "contract_sha256": self.contract_sha256,
            "base_solver_sha256": self.base_solver_sha256,
            "shard": self.shard.to_payload(),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "HUNLShardEnvelope":
        _require_exact_keys(
            payload,
            {
                "schema",
                "format_version",
                "contract",
                "contract_sha256",
                "base_solver_sha256",
                "shard",
            },
            "HUNL shard envelope",
        )
        if payload["schema"] != HUNL_SHARD_SCHEMA or payload["format_version"] != HUNL_TRAINING_FORMAT:
            raise ValueError("unsupported HUNL shard envelope")
        if type(payload["contract"]) is not dict or type(payload["shard"]) is not dict:
            raise TypeError("HUNL shard nested payloads must be exact objects")
        return cls(
            contract=HUNLTrainingContract.from_payload(payload["contract"]),
            contract_sha256=payload["contract_sha256"],
            base_solver_sha256=payload["base_solver_sha256"],
            shard=ShardDelta.from_payload(payload["shard"]),
        )


def build_hunl_shard(
    game: HUNLTrainingGame,
    state: SolverState,
    shard_index: int,
    shard_count: int,
    *,
    run_contract: Mapping[str, Any] | None = None,
) -> HUNLShardEnvelope:
    contract = HUNLTrainingContract.from_game_state(game, state, run_contract)
    shard = build_shard(game, state, shard_index, shard_count)
    return HUNLShardEnvelope(contract, contract.digest, state.digest, shard)


def _worker_build_hunl_shard(
    arguments: tuple[dict[str, Any], dict[str, Any], int, int, dict[str, Any]],
) -> dict[str, Any]:
    (
        abstraction_payload,
        solver_payload,
        shard_index,
        shard_count,
        run_contract,
    ) = arguments
    game = HUNLTrainingGame(HUNLAbstractionConfig(**abstraction_payload))
    state = SolverState.from_payload(solver_payload)
    return build_hunl_shard(
        game,
        state,
        shard_index,
        shard_count,
        run_contract=run_contract,
    ).to_payload()


def build_independent_hunl_shards(
    game: HUNLTrainingGame,
    state: SolverState,
    shard_count: int,
    *,
    max_workers: int | None = None,
    run_contract: Mapping[str, Any] | None = None,
) -> tuple[HUNLShardEnvelope, ...]:
    """Build every shard in a fresh worker from identical serialized base bytes."""

    if type(shard_count) is not int or shard_count <= 0:
        raise ValueError("shard_count must be a positive exact integer")
    if max_workers is not None and (type(max_workers) is not int or max_workers <= 0):
        raise ValueError("max_workers must be a positive exact integer or None")
    # Validate before processes inherit/receive any bytes.
    contract = HUNLTrainingContract.from_game_state(game, state, run_contract)
    frozen = state.to_payload()
    frozen_run_contract = dict(contract.run_contract)
    tasks = tuple(
        (
            game.abstraction.to_payload(),
            frozen,
            index,
            shard_count,
            frozen_run_contract,
        )
        for index in range(shard_count)
    )
    workers = min(shard_count, max_workers or max(1, os.cpu_count() or 1))
    # Linux is the repository's audited training platform.  An explicit fork
    # context also keeps library/CLI invocations independent of whether the
    # caller's ``__main__`` originated from a file, pytest, or stdin.  Workers
    # still reconstruct both game and state from the immutable task payload.
    context = multiprocessing.get_context("fork")
    with ProcessPoolExecutor(max_workers=workers, mp_context=context) as executor:
        payloads = tuple(executor.map(_worker_build_hunl_shard, tasks))
    return tuple(HUNLShardEnvelope.from_payload(payload) for payload in payloads)


def apply_hunl_shards(
    game: HUNLTrainingGame,
    state: SolverState,
    shards: Iterable[HUNLShardEnvelope],
    *,
    run_contract: Mapping[str, Any] | None = None,
) -> SolverState:
    expected = HUNLTrainingContract.from_game_state(game, state, run_contract)
    expected_payload = expected.to_payload()
    envelopes = tuple(shards)
    if not envelopes:
        raise ValueError("at least one HUNL shard is required")
    generic: list[ShardDelta] = []
    for envelope in envelopes:
        if type(envelope) is not HUNLShardEnvelope:
            raise TypeError("HUNL merge requires exact shard envelopes")
        if envelope.contract.to_payload() != expected_payload:
            raise ValueError("shard rules/abstraction/assets/backend contract differs")
        if envelope.contract_sha256 != expected.digest:
            raise ValueError("shard training contract digest differs")
        if envelope.base_solver_sha256 != state.digest:
            raise ValueError("shard frozen base differs from current checkpoint")
        generic.append(envelope.shard)
    # Generic reducer proves exact unique coverage and stages the update before
    # mutating the caller's state.
    return apply_shards(game, state, generic)


def train_hunl_batches(
    game: HUNLTrainingGame,
    state: SolverState,
    batches: int,
    *,
    shard_count: int,
    max_workers: int | None = None,
    run_contract: Mapping[str, Any] | None = None,
) -> SolverState:
    if type(batches) is not int or batches < 0:
        raise ValueError("batches must be a nonnegative exact integer")
    for _ in range(batches):
        apply_hunl_shards(
            game,
            state,
            build_independent_hunl_shards(
                game,
                state,
                shard_count,
                max_workers=max_workers,
                run_contract=run_contract,
            ),
            run_contract=run_contract,
        )
    return state


def save_hunl_checkpoint(
    path: str | Path,
    game: HUNLTrainingGame,
    state: SolverState,
    *,
    root: str | Path | None = None,
    run_contract: Mapping[str, Any] | None = None,
) -> str:
    contract = HUNLTrainingContract.from_game_state(game, state, run_contract)
    payload = {
        "schema": HUNL_CHECKPOINT_SCHEMA,
        "format_version": HUNL_TRAINING_FORMAT,
        "contract": contract.to_payload(),
        "contract_sha256": contract.digest,
        "solver": state.to_payload(),
        "solver_sha256": state.digest,
        "resources": {
            "batches": state.batch_index,
            "trajectories": state.trajectories,
            "node_touches": state.node_touches,
            "information_rows": len(state.actions),
        },
    }
    return atomic_json_write(path, payload, root=root)


def load_hunl_checkpoint(
    path: str | Path,
    game: HUNLTrainingGame,
    *,
    root: str | Path | None = None,
    run_contract: Mapping[str, Any] | None = None,
) -> SolverState:
    payload = load_hashed_json(path, root=root)
    return _decode_hunl_checkpoint(payload, game, run_contract)


def load_hunl_checkpoint_with_digest(
    path: str | Path,
    game: HUNLTrainingGame,
    *,
    root: str | Path | None = None,
    run_contract: Mapping[str, Any] | None = None,
) -> tuple[SolverState, str]:
    """Load one stable envelope and preserve its existing payload digest."""

    payload = load_hashed_json(path, root=root)
    state = _decode_hunl_checkpoint(payload, game, run_contract)
    return state, payload_sha256(payload)


def _decode_hunl_checkpoint(
    payload: Mapping[str, Any],
    game: HUNLTrainingGame,
    run_contract: Mapping[str, Any] | None,
) -> SolverState:
    _require_exact_keys(
        payload,
        {
            "schema",
            "format_version",
            "contract",
            "contract_sha256",
            "solver",
            "solver_sha256",
            "resources",
        },
        "HUNL checkpoint",
    )
    if payload["schema"] != HUNL_CHECKPOINT_SCHEMA or payload["format_version"] != HUNL_TRAINING_FORMAT:
        raise ValueError("unsupported HUNL checkpoint")
    if type(payload["contract"]) is not dict or type(payload["solver"]) is not dict:
        raise TypeError("checkpoint nested payloads must be exact objects")
    state = SolverState.from_payload(payload["solver"])
    contract = HUNLTrainingContract.from_payload(payload["contract"])
    expected = HUNLTrainingContract.from_game_state(game, state, run_contract)
    if contract.to_payload() != expected.to_payload():
        raise ValueError("checkpoint rules/abstraction/assets/backend contract drifted")
    if payload["contract_sha256"] != contract.digest:
        raise ValueError("checkpoint contract digest mismatch")
    if payload["solver_sha256"] != state.digest:
        raise ValueError("checkpoint solver digest mismatch")
    expected_resources = {
        "batches": state.batch_index,
        "trajectories": state.trajectories,
        "node_touches": state.node_touches,
        "information_rows": len(state.actions),
    }
    if payload["resources"] != expected_resources:
        raise ValueError("checkpoint resource counters disagree with solver payload")
    return state
