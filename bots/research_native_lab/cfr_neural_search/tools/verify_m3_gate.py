"""Deterministically render or verify the route-B M3 gate manifest.

``--write`` derives every locally reproducible evidence row and the exact route
file map before atomically replacing the manifest, then verifies the result.
The default mode is read-only verification.  Empirical wall-time/RSS rows and
the independently produced OpenSpiel half of the cross-check remain recorded
inputs; their route-side values and equality claims are deterministically
recomputed here.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
import tempfile
from typing import Any, Mapping

import bots.research_native_lab.common_contracts as common_contracts_module
from bots.research_native_lab.common_contracts import actions as common_actions_module
from bots.research_native_lab.common_contracts import cards as common_cards_module
from bots.research_native_lab.common_contracts import constants as common_constants_module
from bots.research_native_lab.common_contracts import national_state as common_state_module
from bots.research_native_lab.common_contracts import protocol as common_protocol_module
from bots.research_native_lab.cfr_neural_search.blueprint import (
    evaluation as evaluation_module,
)
from bots.research_native_lab.cfr_neural_search.blueprint import mccfr as mccfr_module
from bots.research_native_lab.cfr_neural_search.blueprint import (
    small_games as small_games_module,
)
from bots.research_native_lab.cfr_neural_search.core import game as core_game_module
from bots.research_native_lab.cfr_neural_search.blueprint.evaluation import (
    expected_returns,
    exploitability,
)
from bots.research_native_lab.cfr_neural_search.blueprint.mccfr import (
    SolverConfig,
    SolverState,
    average_policy,
    train_batches,
)
from bots.research_native_lab.cfr_neural_search.blueprint.small_games import make_game
from bots.research_native_lab.cfr_neural_search.native_runtime.common_adapter import (
    COMMON_CONTRACT_COMMIT,
    COMMON_CONTRACT_GIT_TREE,
    COMMON_RUNTIME_FILE_SHA256,
)
from bots.research_native_lab.cfr_neural_search.native_runtime import (
    common_adapter as common_adapter_module,
)


class M3GateVerificationError(ValueError):
    """The manifest, route artifact, Common dependency, or fixture drifted."""


EXPECTED_FROZEN_STATE_SHA256 = {
    "configs/kuhn_m3_linear.json": "9d8a7b3178b7b76f022b62938636452863960e9edd657b52b82bf631b5957f3c",
    "configs/leduc_m3_linear.json": "91a74660c1118a7523331efbe873bd20a76b8589fbbfc8eef97db41cc2b1bb28",
    "configs/leduc_m3_dcfr.json": "effd5fe427db657d0ff50b1c91b4d91ba69780bc2246fb7769caec1f6964ff14",
}
EXPECTED_REFERENCE_STATE_SHA256 = (
    "0b5f2d6094a43cae5104657828b1572a8208328744f0fbdfd7fd0ba4274370a8"
)
EXPECTED_FIXTURE_STATE_SHA256 = {
    "kuhn_dcfr_40x4": "9bfcbfd8b192bd081b559769787fdf05dea8cad95c83ce471e1024c7fbdd9aa0",
    "leduc_linear_20x2": "8b288589b198ad7bbd3192e65ba9c9377f6b6c7eded0c918db139556f5612ed1",
}
ROUTE_ROOT_DECLARATION = "bots/research_native_lab/cfr_neural_search"
MANIFEST_RELATIVE_PATH = "manifests/m3_gate_20260714.json"
DEFAULT_MANIFEST = Path(__file__).parents[1] / MANIFEST_RELATIVE_PATH
NATIVE_ONLY_MATCH_LIMIT = (
    "M4 and all match evaluation must use sever national TCP/raw sockets or "
    "Common native_harness over sever/engine; top-level engine/, engine/battle.py, "
    "and Botzone JSON stdin/stdout are forbidden"
)
FROZEN_CONFIGS = (
    "configs/kuhn_m3_linear.json",
    "configs/leduc_m3_linear.json",
    "configs/leduc_m3_dcfr.json",
)
FIXTURE_SPECS = (
    {
        "id": "kuhn_dcfr_40x4",
        "game": "kuhn",
        "batches": 40,
        "shards": 2,
        "config": {
            "update_rule": "dcfr",
            "averaging_mode": "sampled",
            "seed": 20260714,
            "samples_per_player": 4,
            "cfr_plus_delay": 0,
            "dcfr_alpha": 1.5,
            "dcfr_beta": 0.0,
            "dcfr_gamma": 2.0,
        },
    },
    {
        "id": "leduc_linear_20x2",
        "game": "leduc",
        "batches": 20,
        "shards": 2,
        "config": {
            "update_rule": "linear",
            "averaging_mode": "sampled",
            "seed": 20260714,
            "samples_per_player": 2,
            "cfr_plus_delay": 0,
            "dcfr_alpha": 1.5,
            "dcfr_beta": 0.0,
            "dcfr_gamma": 2.0,
        },
    },
)
_CACHE_PARTS = frozenset({"__pycache__", ".pytest_cache"})


def _has_symlink_component(path: Path) -> bool:
    normalized = Path(os.path.abspath(os.fspath(path)))
    return any(candidate.is_symlink() for candidate in (normalized, *normalized.parents))


def _module_file(module: Any, label: str) -> Path:
    raw = getattr(module, "__file__", None)
    if type(raw) is not str:
        raise M3GateVerificationError(f"imported module {label} has no exact __file__")
    path = Path(os.path.abspath(raw))
    if _has_symlink_component(path):
        raise M3GateVerificationError(f"imported module {label} uses a symlinked path")
    try:
        return path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise M3GateVerificationError(
            f"imported module {label} source path is absent: {path}"
        ) from exc


def _assert_imported_module_roots(route_root: Path) -> None:
    """Bind every recomputation module to this exact checked route/Common tree."""

    route_bindings = (
        (evaluation_module, "blueprint/evaluation.py"),
        (mccfr_module, "blueprint/mccfr.py"),
        (small_games_module, "blueprint/small_games.py"),
        (core_game_module, "core/game.py"),
        (common_adapter_module, "native_runtime/common_adapter.py"),
    )
    common_root = route_root.parent / "common_contracts"
    common_bindings = (
        (common_contracts_module, "__init__.py"),
        (common_actions_module, "actions.py"),
        (common_cards_module, "cards.py"),
        (common_constants_module, "constants.py"),
        (common_state_module, "national_state.py"),
        (common_protocol_module, "protocol.py"),
    )
    bindings = (
        *((module, route_root / relative) for module, relative in route_bindings),
        *((module, common_root / relative) for module, relative in common_bindings),
    )
    for module, expected in bindings:
        actual = _module_file(module, module.__name__)
        if _has_symlink_component(expected) or actual != expected.resolve(strict=True):
            raise M3GateVerificationError(
                "imported module/root mismatch: "
                f"{module.__name__} loaded from {actual}, expected {expected}"
            )
    tool_path = _module_file(__import__(__name__, fromlist=["*"]), __name__)
    expected_tool = route_root / "tools" / "verify_m3_gate.py"
    if tool_path != expected_tool.resolve(strict=True):
        raise M3GateVerificationError(
            f"verifier module/root mismatch: loaded {tool_path}, expected {expected_tool}"
        )


def _bound_manifest_path(path: str | Path) -> Path:
    """Accept only this imported route's canonical, non-symlinked M3 manifest."""

    raw = Path(os.path.abspath(os.fspath(path)))
    if _has_symlink_component(raw):
        raise M3GateVerificationError("manifest path must not contain symlinks")
    try:
        manifest_path = raw.resolve(strict=True)
        expected = Path(DEFAULT_MANIFEST).resolve(strict=True)
    except FileNotFoundError as exc:
        raise M3GateVerificationError("M3 manifest template does not exist") from exc
    if manifest_path != expected:
        raise M3GateVerificationError(
            "manifest path belongs to a different route tree; fresh-process "
            "verification must be launched from that tree instead"
        )
    route_root = manifest_path.parent.parent
    _assert_imported_module_roots(route_root)
    return manifest_path


def _strict_json(path: Path) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise M3GateVerificationError(f"non-standard JSON constant {value!r}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise M3GateVerificationError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    payload = json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=reject_constant,
        object_pairs_hook=unique_object,
    )
    if type(payload) is not dict:
        raise M3GateVerificationError("manifest root must be a JSON object")
    return payload


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ignored(path: Path) -> bool:
    return any(part in _CACHE_PARTS for part in path.parts) or path.suffix in {
        ".pyc",
        ".pyo",
    }


def _route_file_map(root: Path, *, excluded: frozenset[str]) -> dict[str, str]:
    """Derive the exact sorted route file map used by both writer and verifier."""

    files: dict[str, str] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        relative_text = relative.as_posix()
        if _ignored(relative) or relative_text in excluded:
            continue
        if path.is_symlink():
            raise M3GateVerificationError(
                f"route artifact symlinks are forbidden: {relative_text}"
            )
        if path.is_file():
            files[relative_text] = _sha256(path)
    return dict(sorted(files.items()))


def _atomic_bytes_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="wb",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
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


def _atomic_json_write(path: Path, payload: Mapping[str, Any]) -> None:
    """Durably replace one manifest without exposing a partially written file."""

    encoded = (
        json.dumps(payload, indent=2, sort_keys=False, allow_nan=False) + "\n"
    ).encode("utf-8")
    _atomic_bytes_write(path, encoded)


def verify_artifact_map(
    root: Path,
    declared: Mapping[str, str],
    *,
    excluded: frozenset[str],
) -> int:
    """Require exact file-set and SHA equality, excluding only named files/caches."""

    if type(declared) is not dict or any(
        type(path) is not str or type(digest) is not str
        for path, digest in declared.items()
    ):
        raise M3GateVerificationError("artifact map must be string-to-string JSON")
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if (path.is_file() or path.is_symlink())
        and not _ignored(path.relative_to(root))
        and path.relative_to(root).as_posix() not in excluded
    }
    expected = set(declared)
    if actual != expected:
        raise M3GateVerificationError(
            "route artifact file set mismatch: "
            f"missing={sorted(expected - actual)!r}, "
            f"extra={sorted(actual - expected)!r}"
        )
    for relative_path, expected_sha256 in sorted(declared.items()):
        if len(expected_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in expected_sha256
        ):
            raise M3GateVerificationError(
                f"invalid declared SHA-256 for {relative_path}"
            )
        artifact_path = root / relative_path
        if artifact_path.is_symlink():
            raise M3GateVerificationError(
                f"route artifact symlinks are forbidden: {relative_path}"
            )
        actual_sha256 = _sha256(artifact_path)
        if actual_sha256 != expected_sha256:
            raise M3GateVerificationError(
                f"artifact SHA-256 mismatch for {relative_path}: "
                f"expected {expected_sha256}, got {actual_sha256}"
            )
    return len(actual)


def _git_object_digest(kind: bytes, payload: bytes) -> bytes:
    header = kind + b" " + str(len(payload)).encode("ascii") + b"\0"
    return hashlib.sha1(header + payload).digest()


def _working_tree_git_oid(directory: Path) -> bytes:
    entries: list[tuple[bytes, bool, bytes, bytes]] = []
    for child in directory.iterdir():
        relative = child.relative_to(directory)
        if _ignored(relative):
            continue
        name = os.fsencode(child.name)
        if child.is_symlink():
            data = os.fsencode(os.readlink(child))
            entries.append((name, False, b"120000", _git_object_digest(b"blob", data)))
        elif child.is_dir():
            entries.append((name, True, b"40000", _working_tree_git_oid(child)))
        elif child.is_file():
            mode = b"100755" if os.access(child, os.X_OK) else b"100644"
            entries.append(
                (name, False, mode, _git_object_digest(b"blob", child.read_bytes()))
            )
        else:
            raise M3GateVerificationError(f"unsupported Common path type: {child}")
    entries.sort(key=lambda item: item[0] + (b"/" if item[1] else b""))
    payload = b"".join(
        mode + b" " + name + b"\0" + object_id
        for name, _is_directory, mode, object_id in entries
    )
    return _git_object_digest(b"tree", payload)


@dataclass(frozen=True, slots=True)
class M3InputSnapshot:
    route_files: tuple[tuple[str, str], ...]
    common_git_tree: str
    common_critical_files: tuple[tuple[str, str], ...]
    solver_input_digest: str


def _capture_input_snapshot(
    route_root: Path,
    *,
    manifest_relative: str,
) -> M3InputSnapshot:
    common_root = route_root.parent / "common_contracts"
    return M3InputSnapshot(
        route_files=tuple(
            _route_file_map(
                route_root,
                excluded=frozenset({manifest_relative}),
            ).items()
        ),
        common_git_tree=_working_tree_git_oid(common_root).hex(),
        common_critical_files=tuple(
            (relative_path, _sha256(common_root / relative_path))
            for relative_path, _expected in COMMON_RUNTIME_FILE_SHA256
        ),
        solver_input_digest=_solver_input_digest(route_root),
    )


def _require_unchanged_snapshot(
    expected: M3InputSnapshot,
    actual: M3InputSnapshot,
    context: str,
) -> None:
    if actual != expected:
        raise M3GateVerificationError(
            f"M3 inputs changed during {context}; refusing a mixed-time receipt"
        )


def _render_common_dependency(
    route_root: Path,
    existing: Mapping[str, Any],
) -> dict[str, Any]:
    """Rebuild Common declarations, refusing to bless unreviewed dependency drift."""

    if type(existing) is not dict:
        raise M3GateVerificationError("common_dependency template must be an object")
    common_root = route_root.parent / "common_contracts"
    actual_tree = _working_tree_git_oid(common_root).hex()
    if actual_tree != COMMON_CONTRACT_GIT_TREE:
        raise M3GateVerificationError(
            f"complete Common tree drifted: expected {COMMON_CONTRACT_GIT_TREE}, "
            f"got {actual_tree}"
        )
    for relative_path, expected_sha256 in COMMON_RUNTIME_FILE_SHA256:
        if _sha256(common_root / relative_path) != expected_sha256:
            raise M3GateVerificationError(
                f"Common critical file drifted: {relative_path}"
            )
    result = dict(existing)
    result.update(
        {
            "commit": COMMON_CONTRACT_COMMIT,
            "git_tree": actual_tree,
            "critical_files": dict(COMMON_RUNTIME_FILE_SHA256),
            "enforcement": (
                "generator and verifier hash every critical file and the complete "
                "Common Git tree; byte drift reopens the M3 integration gate"
            ),
        }
    )
    return result


def _verify_common_dependency(route_root: Path, payload: Mapping[str, Any]) -> str:
    if type(payload) is not dict:
        raise M3GateVerificationError("common_dependency must be a JSON object")
    if payload.get("commit") != COMMON_CONTRACT_COMMIT:
        raise M3GateVerificationError("Common commit binding drifted")
    if payload.get("git_tree") != COMMON_CONTRACT_GIT_TREE:
        raise M3GateVerificationError("Common tree declaration drifted")
    expected_critical = dict(COMMON_RUNTIME_FILE_SHA256)
    if payload.get("critical_files") != expected_critical:
        raise M3GateVerificationError("Common critical-file declaration drifted")
    common_root = route_root.parent / "common_contracts"
    for relative_path, expected_sha256 in expected_critical.items():
        actual = _sha256(common_root / relative_path)
        if actual != expected_sha256:
            raise M3GateVerificationError(
                f"Common critical file drifted: {relative_path}"
            )
    actual_tree = _working_tree_git_oid(common_root).hex()
    if actual_tree != COMMON_CONTRACT_GIT_TREE:
        raise M3GateVerificationError(
            f"complete Common tree drifted: expected {COMMON_CONTRACT_GIT_TREE}, "
            f"got {actual_tree}"
        )
    return actual_tree


def _solver_input_digest(route_root: Path) -> str:
    """Cache key for deterministic evidence owned by the local solver."""

    paths = [
        "blueprint/evaluation.py",
        "blueprint/mccfr.py",
        "blueprint/small_games.py",
        *FROZEN_CONFIGS,
    ]
    payload = "\n".join(f"{path}:{_sha256(route_root / path)}" for path in paths)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_experiment(route_root: Path, relative_path: str) -> tuple[Any, SolverConfig, int, int]:
    payload = _strict_json(route_root / relative_path)
    if set(payload) != {"game", "batches", "shards", "solver"}:
        raise M3GateVerificationError(f"experiment schema drifted: {relative_path}")
    if type(payload["game"]) is not str or type(payload["solver"]) is not dict:
        raise M3GateVerificationError(f"experiment game/solver type drifted: {relative_path}")
    if type(payload["batches"]) is not int or type(payload["shards"]) is not int:
        raise M3GateVerificationError(f"experiment counters drifted: {relative_path}")
    if payload["batches"] < 0 or payload["shards"] <= 0:
        raise M3GateVerificationError(f"experiment counters are invalid: {relative_path}")
    return (
        make_game(payload["game"]),
        SolverConfig.from_payload(payload["solver"]),
        payload["batches"],
        payload["shards"],
    )


@lru_cache(maxsize=4)
def _cached_frozen_training_runs(route_root_text: str, input_digest: str) -> str:
    del input_digest
    route_root = Path(route_root_text)
    rows: list[dict[str, Any]] = []
    for relative_path in FROZEN_CONFIGS:
        game, config, batches, shards = _load_experiment(route_root, relative_path)
        state = SolverState.new_for_game(game, config)
        train_batches(game, state, batches=batches, shard_count=shards)
        result = exploitability(game, average_policy(state))
        expected_digest = EXPECTED_FROZEN_STATE_SHA256[relative_path]
        if state.digest != expected_digest:
            raise M3GateVerificationError(
                f"frozen training state drifted for {relative_path}: "
                f"expected {expected_digest}, got {state.digest}"
            )
        rows.append(
            {
                "config": relative_path,
                "config_sha256": _sha256(route_root / relative_path),
                "batches": state.batch_index,
                "shards": shards,
                "trajectories": state.trajectories,
                "node_touches": state.node_touches,
                "infosets_seen": len(state.actions),
                "exploitability": result.exploitability,
                "state_sha256": state.digest,
            }
        )
    return json.dumps(rows, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _derive_frozen_training_runs(route_root: Path) -> list[dict[str, Any]]:
    encoded = _cached_frozen_training_runs(
        str(route_root.resolve()),
        _solver_input_digest(route_root),
    )
    return json.loads(encoded)


def _derive_state_fixtures() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    observed: dict[str, str] = {}
    for raw_spec in FIXTURE_SPECS:
        spec = copy.deepcopy(raw_spec)
        game = make_game(spec["game"])
        config = SolverConfig.from_payload(spec["config"])
        state = SolverState.new_for_game(game, config)
        train_batches(game, state, spec["batches"], spec["shards"])
        fixture_id = spec["id"]
        observed[fixture_id] = state.digest
        rows.append(
            {
                **spec,
                "state_sha256": state.digest,
                "trajectories": state.trajectories,
                "node_touches": state.node_touches,
                "infosets_seen": len(state.actions),
            }
        )
    if observed != EXPECTED_FIXTURE_STATE_SHA256:
        raise M3GateVerificationError(
            f"deterministic state fixtures drifted: {observed!r}"
        )
    return rows


@lru_cache(maxsize=4)
def _cached_trained_leduc_route(input_digest: str) -> str:
    del input_digest
    game = make_game("leduc")
    config = SolverConfig(
        update_rule="linear",
        averaging_mode="sampled",
        seed=23,
        samples_per_player=1,
    )
    state = SolverState.new_for_game(game, config)
    train_batches(game, state, batches=500, shard_count=1)
    policy = average_policy(state)
    result = exploitability(game, policy)
    route_value = expected_returns(game, policy)
    if state.digest != EXPECTED_REFERENCE_STATE_SHA256:
        raise M3GateVerificationError(
            "trained Leduc cross-check state drifted: "
            f"expected {EXPECTED_REFERENCE_STATE_SHA256}, got {state.digest}"
        )
    payload = {
        "algorithm": "synchronous_external_sampling_mccfr",
        "update_rule": config.update_rule,
        "seed": config.seed,
        "batches": state.batch_index,
        "samples_per_player": config.samples_per_player,
        "shards": 1,
        "trajectories": state.trajectories,
        "information_states": len(policy),
        "state_sha256": state.digest,
        "route_value": list(route_value),
        "route_nash_conv": result.nash_conv,
        "route_exploitability": result.exploitability,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _derive_trained_leduc_route(route_root: Path) -> dict[str, Any]:
    return json.loads(_cached_trained_leduc_route(_solver_input_digest(route_root)))


def _verify_frozen_evidence(manifest: Mapping[str, Any], route_root: Path) -> None:
    if manifest.get("frozen_training_runs") != _derive_frozen_training_runs(route_root):
        raise M3GateVerificationError("frozen training evidence was not generator-derived")


def _verify_independent_reference(
    payload: Any,
    route_root: Path,
) -> dict[str, Any]:
    if type(payload) is not dict or type(payload.get("trained_leduc")) is not dict:
        raise M3GateVerificationError("independent reference evidence is malformed")
    if payload.get("checks_passed") != 10 or payload.get("checks_total") != 10:
        raise M3GateVerificationError("independent cross-check count is not 10/10")
    trained = payload["trained_leduc"]
    route = _derive_trained_leduc_route(route_root)
    for key, value in route.items():
        if trained.get(key) != value:
            raise M3GateVerificationError(
                f"trained Leduc route evidence drifted at {key}"
            )
    open_value = trained.get("open_spiel_value")
    if type(open_value) is not list or len(open_value) != 2 or any(
        type(value) not in (int, float) for value in open_value
    ):
        raise M3GateVerificationError("OpenSpiel trained value is malformed")
    if any(
        not math.isclose(route["route_value"][index], open_value[index], abs_tol=1e-12)
        for index in (0, 1)
    ):
        raise M3GateVerificationError("route/OpenSpiel trained policy values differ")
    for route_key, spiel_key in (
        ("route_nash_conv", "open_spiel_nash_conv"),
        ("route_exploitability", "open_spiel_exploitability"),
    ):
        spiel_value = trained.get(spiel_key)
        if type(spiel_value) not in (int, float) or not math.isclose(
            route[route_key],
            spiel_value,
            abs_tol=1e-12,
        ):
            raise M3GateVerificationError(
                f"route/OpenSpiel trained policy evidence differs at {spiel_key}"
            )
    return route


def _verify_state_fixtures(payload: Any) -> dict[str, str]:
    expected = _derive_state_fixtures()
    if payload != expected:
        raise M3GateVerificationError("state fixtures were not generator-derived")
    return {row["id"]: row["state_sha256"] for row in expected}


@dataclass(frozen=True, slots=True)
class M3GateVerificationReceipt:
    files_verified: int
    common_git_tree: str
    state_fixtures: dict[str, str]
    frozen_state_sha256: dict[str, str]
    reference_state_sha256: str


def _render_independent_reference(
    existing: Any,
    route_root: Path,
) -> dict[str, Any]:
    if type(existing) is not dict or type(existing.get("trained_leduc")) is not dict:
        raise M3GateVerificationError(
            "independent reference template lacks trained Leduc evidence"
        )
    result = copy.deepcopy(existing)
    trained = dict(result["trained_leduc"])
    trained.update(_derive_trained_leduc_route(route_root))
    trained["interpretation"] = "correctness cross-check only; not a strength claim"
    result["trained_leduc"] = trained
    _verify_independent_reference(result, route_root)
    return result


def render_m3_gate_manifest(
    path: str | Path = DEFAULT_MANIFEST,
    *,
    write: bool = False,
) -> dict[str, Any]:
    """Rebuild deterministic evidence/file hashes and optionally atomically write."""

    if write:
        manifest, _receipt = write_m3_gate_manifest(path)
        return manifest
    manifest_path = _bound_manifest_path(path)
    manifest = copy.deepcopy(_strict_json(manifest_path))
    if manifest.get("schema_version") != 2:
        raise M3GateVerificationError("unsupported M3 gate manifest schema")
    if manifest.get("status") != "passed_local_m3_only":
        raise M3GateVerificationError("refusing to render a non-passing M3 template")
    route_root = manifest_path.parent.parent
    manifest_relative = manifest_path.relative_to(route_root).as_posix()
    before = _capture_input_snapshot(
        route_root,
        manifest_relative=manifest_relative,
    )
    manifest["common_dependency"] = _render_common_dependency(
        route_root,
        manifest.get("common_dependency"),
    )
    manifest["frozen_training_runs"] = _derive_frozen_training_runs(route_root)
    manifest["independent_reference"] = _render_independent_reference(
        manifest.get("independent_reference"),
        route_root,
    )
    manifest["deterministic_state_fixtures"] = _derive_state_fixtures()
    manifest["generator"] = {
        "tool": "tools/verify_m3_gate.py",
        "write_command": (
            "PYTHONDONTWRITEBYTECODE=1 python -m "
            "bots.research_native_lab.cfr_neural_search.tools.verify_m3_gate --write"
        ),
        "generated_fields": [
            "common_dependency",
            "frozen_training_runs",
            "independent_reference.trained_leduc.route_*",
            "deterministic_state_fixtures",
            "artifact_scope",
        ],
        "atomic_replace": True,
    }
    limits = manifest.get("known_limits")
    if type(limits) is not list or any(type(item) is not str for item in limits):
        raise M3GateVerificationError("known_limits template must be a string array")
    manifest["known_limits"] = [
        item for item in limits if "top-level engine/" not in item
    ] + [NATIVE_ONLY_MATCH_LIMIT]
    manifest["artifact_scope"] = {
        "root": ROUTE_ROOT_DECLARATION,
        "excluded": [manifest_relative],
        "reason_for_exclusion": "manifest cannot contain its own SHA-256",
        "files": dict(before.route_files),
    }
    after = _capture_input_snapshot(
        route_root,
        manifest_relative=manifest_relative,
    )
    _require_unchanged_snapshot(before, after, "manifest rendering")
    return manifest


def verify_m3_gate_payload(
    manifest: Mapping[str, Any],
    path: str | Path = DEFAULT_MANIFEST,
) -> M3GateVerificationReceipt:
    """Verify an in-memory rendered payload against the route working tree."""

    manifest_path = _bound_manifest_path(path)
    if type(manifest) is not dict:
        raise M3GateVerificationError("manifest root must be a JSON object")
    if manifest.get("schema_version") != 2:
        raise M3GateVerificationError("unsupported M3 gate manifest schema")
    if manifest.get("status") != "passed_local_m3_only":
        raise M3GateVerificationError("M3 gate status is not the frozen local pass")
    generator = manifest.get("generator")
    if (
        type(generator) is not dict
        or generator.get("tool") != "tools/verify_m3_gate.py"
        or generator.get("atomic_replace") is not True
    ):
        raise M3GateVerificationError("manifest generator declaration drifted")
    limits = manifest.get("known_limits")
    if type(limits) is not list or limits.count(NATIVE_ONLY_MATCH_LIMIT) != 1:
        raise M3GateVerificationError("native-only future match boundary is absent")
    scope = manifest.get("artifact_scope")
    if type(scope) is not dict or type(scope.get("files")) is not dict:
        raise M3GateVerificationError("artifact_scope is malformed")
    route_root = manifest_path.parent.parent
    expected_manifest_relative = manifest_path.relative_to(route_root).as_posix()
    before = _capture_input_snapshot(
        route_root,
        manifest_relative=expected_manifest_relative,
    )
    if scope.get("root") != ROUTE_ROOT_DECLARATION:
        raise M3GateVerificationError("artifact root declaration drifted")
    if scope.get("excluded") != [expected_manifest_relative]:
        raise M3GateVerificationError("manifest self-exclusion declaration drifted")
    files_verified = verify_artifact_map(
        route_root,
        scope["files"],
        excluded=frozenset({expected_manifest_relative}),
    )
    common_tree = _verify_common_dependency(
        route_root,
        manifest.get("common_dependency"),
    )
    _verify_frozen_evidence(manifest, route_root)
    _verify_independent_reference(manifest.get("independent_reference"), route_root)
    fixtures = _verify_state_fixtures(manifest.get("deterministic_state_fixtures"))
    after = _capture_input_snapshot(
        route_root,
        manifest_relative=expected_manifest_relative,
    )
    _require_unchanged_snapshot(before, after, "manifest verification")
    return M3GateVerificationReceipt(
        files_verified=files_verified,
        common_git_tree=common_tree,
        state_fixtures=fixtures,
        frozen_state_sha256=dict(EXPECTED_FROZEN_STATE_SHA256),
        reference_state_sha256=EXPECTED_REFERENCE_STATE_SHA256,
    )


def verify_m3_gate_manifest(path: str | Path = DEFAULT_MANIFEST) -> M3GateVerificationReceipt:
    manifest_path = _bound_manifest_path(path)
    frozen_bytes = manifest_path.read_bytes()
    manifest = _strict_json(manifest_path)
    if manifest_path.read_bytes() != frozen_bytes:
        raise M3GateVerificationError("manifest changed while it was being read")
    receipt = verify_m3_gate_payload(manifest, manifest_path)
    if manifest_path.read_bytes() != frozen_bytes:
        raise M3GateVerificationError("manifest changed during verification")
    return receipt


def write_m3_gate_manifest(
    path: str | Path = DEFAULT_MANIFEST,
) -> tuple[dict[str, Any], M3GateVerificationReceipt]:
    """Render, atomically replace, verify, and rollback on any detected drift."""

    manifest_path = _bound_manifest_path(path)
    original_bytes = manifest_path.read_bytes()
    manifest = render_m3_gate_manifest(manifest_path)
    route_root = manifest_path.parent.parent
    manifest_relative = manifest_path.relative_to(route_root).as_posix()
    baseline = _capture_input_snapshot(
        route_root,
        manifest_relative=manifest_relative,
    )
    scope = manifest.get("artifact_scope")
    if type(scope) is not dict or scope.get("files") != dict(baseline.route_files):
        raise M3GateVerificationError(
            "route inputs changed after render and before manifest write"
        )
    if manifest_path.read_bytes() != original_bytes:
        raise M3GateVerificationError(
            "manifest template changed before atomic replace; refusing to clobber it"
        )
    wrote = False
    try:
        _atomic_json_write(manifest_path, manifest)
        wrote = True
        post_write = _capture_input_snapshot(
            route_root,
            manifest_relative=manifest_relative,
        )
        _require_unchanged_snapshot(baseline, post_write, "manifest replacement")
        receipt = verify_m3_gate_manifest(manifest_path)
    except BaseException:
        if wrote:
            _atomic_bytes_write(manifest_path, original_bytes)
        raise
    return manifest, receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "manifest",
        nargs="?",
        default=DEFAULT_MANIFEST,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--render",
        action="store_true",
        help="print the deterministically regenerated manifest without writing",
    )
    mode.add_argument(
        "--write",
        action="store_true",
        help="atomically regenerate the manifest and verify the written result",
    )
    args = parser.parse_args(argv)
    if args.render:
        print(
            json.dumps(
                render_m3_gate_manifest(args.manifest),
                indent=2,
                sort_keys=False,
                allow_nan=False,
            )
        )
        return 0
    if args.write:
        _manifest, receipt = write_m3_gate_manifest(args.manifest)
    else:
        receipt = verify_m3_gate_manifest(args.manifest)
    print(json.dumps(asdict(receipt), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
