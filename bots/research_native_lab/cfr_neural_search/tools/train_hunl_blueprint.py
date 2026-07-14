"""Crash-safe correctness-scale HUNL blueprint training CLI.

Every complete synchronous batch is merged transactionally, checkpointed
atomically, and followed by a heartbeat.  Cancellation or process death can
therefore lose at most an uncommitted batch; ``--resume`` accepts only the
fully content-bound checkpoint for the current rules/abstraction/Common tree.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import bots.research_native_lab.common_contracts as common_contracts

from ..blueprint.artifact import (
    LoadedBlueprint,
    compile_blueprint_payload,
    load_blueprint_artifact,
    save_blueprint_artifact,
)
from ..blueprint.hunl_abstraction import HUNLAbstractionConfig
from ..blueprint.hunl_game import HUNLTrainingGame, _regular_source_manifest
from ..blueprint.hunl_training import (
    apply_hunl_shards,
    build_independent_hunl_shards,
    load_hunl_checkpoint_with_digest,
    save_hunl_checkpoint,
)
from ..blueprint.mccfr import SolverConfig, SolverState
from ..core.identity import file_sha256, payload_sha256, require_sha256
from ..core.selector_invalidation import assert_workspace_not_invalidated
from ..core.strict_io import (
    atomic_json_write,
    stable_tree_manifest,
    strict_json_read,
    validate_real_directory,
)


CONFIG_SCHEMA = "route-b-hunl-m4-config-v2"
HEARTBEAT_SCHEMA = "route-b-hunl-training-heartbeat-v1"
FORMAL_RUN_CONTRACT_SCHEMA = "route-b-hunl-formal-cli-run-v1"
DEFAULT_CONFIG = Path(__file__).parents[1] / "configs" / "hunl_m4_blueprint.json"
RUNTIME_ROOT_NAME = "runtime_outputs"
FROZEN_SCALE_CANDIDATES = (2, 4, 8, 16, 32, 64)
SOURCE_SNAPSHOT_EXCLUDED_PATHS = frozenset(
    {
        RUNTIME_ROOT_NAME,
        "artifacts/m4/blueprint.rbbp",
        "artifacts/m4/training_scale_selection.json",
        "artifacts/m4/local_native_evidence.json",
        "artifacts/m4/selector_events.jsonl",
        "artifacts/m4/selector_events",
        "artifacts/m4/selector_heartbeat.json",
        "manifests/m4_gate_20260714.json",
    }
)


def _keys(payload: Mapping[str, Any], expected: set[str], context: str) -> None:
    if type(payload) is not dict or set(payload) != expected:
        raise ValueError(f"{context} differs from strict schema")


def _positive_exact_int(value: Any, context: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{context} must be a positive exact integer")
    return value


def _validate_frozen_observation(row: Mapping[str, Any], threshold: int) -> None:
    _keys(
        row,
        {
            "batches",
            "passed",
            "materially_nonuniform_all_rows",
            "materially_nonuniform_exact_rows",
            "materially_nonuniform_backoff_rows",
            "max_l1_from_uniform",
            "exact_row_count",
            "backoff_row_count",
            "training_trajectories",
            "training_node_touches",
            "solver_information_rows",
            "solver_sha256",
        },
        "frozen selector observation",
    )
    if type(row["batches"]) is not int or row["batches"] <= 0:
        raise ValueError("selector observation batches must be a positive exact int")
    if type(row["passed"]) is not bool:
        raise TypeError("selector observation passed must be an exact boolean")
    for field in (
        "materially_nonuniform_all_rows",
        "materially_nonuniform_exact_rows",
        "materially_nonuniform_backoff_rows",
        "exact_row_count",
        "backoff_row_count",
        "training_trajectories",
        "training_node_touches",
        "solver_information_rows",
    ):
        if type(row[field]) is not int or row[field] < 0:
            raise ValueError(f"selector observation {field} must be nonnegative exact int")
    l1 = row["max_l1_from_uniform"]
    if type(l1) not in (int, float) or not math.isfinite(float(l1)) or l1 < 0:
        raise ValueError("selector observation max L1 must be finite/nonnegative")
    require_sha256(row["solver_sha256"], "selector observation solver digest")
    if row["passed"] is not (
        row["materially_nonuniform_all_rows"] >= threshold
    ):
        raise ValueError("selector observation pass flag differs from material metric")


def build_scale_observation(
    state: SolverState,
    compiled: Mapping[str, Any],
    threshold: int,
) -> dict[str, Any]:
    """Project only training state/artifact statistics used by the selector."""

    if type(state) is not SolverState or type(compiled) is not dict:
        raise TypeError("scale observation requires exact solver/compiled payloads")
    _positive_exact_int(threshold, "scale observation threshold")
    statistics = compiled["statistics"]
    resources = compiled["resources"]
    material = statistics["materially_nonuniform_all_rows"]
    row = {
        "batches": state.batch_index,
        "passed": material >= threshold,
        "materially_nonuniform_all_rows": material,
        "materially_nonuniform_exact_rows": statistics[
            "materially_nonuniform_exact_rows"
        ],
        "materially_nonuniform_backoff_rows": statistics[
            "materially_nonuniform_backoff_rows"
        ],
        "max_l1_from_uniform": statistics["max_l1_from_uniform"],
        "exact_row_count": statistics["exact_row_count"],
        "backoff_row_count": statistics["backoff_row_count"],
        "training_trajectories": resources["training_trajectories"],
        "training_node_touches": resources["training_node_touches"],
        "solver_information_rows": resources["solver_information_rows"],
        "solver_sha256": state.digest,
    }
    _validate_frozen_observation(row, threshold)
    return row


def build_loaded_scale_observation(
    blueprint: LoadedBlueprint,
    threshold: int,
) -> dict[str, Any]:
    """Project a loaded formal artifact onto the exact selector row schema."""

    if type(blueprint) is not LoadedBlueprint:
        raise TypeError("scale observation requires an exact LoadedBlueprint")
    _positive_exact_int(threshold, "scale observation threshold")
    statistics = blueprint.statistics
    resources = blueprint.resources
    row = {
        "batches": resources["training_batches"],
        "passed": statistics["materially_nonuniform_all_rows"] >= threshold,
        "materially_nonuniform_all_rows": statistics[
            "materially_nonuniform_all_rows"
        ],
        "materially_nonuniform_exact_rows": statistics[
            "materially_nonuniform_exact_rows"
        ],
        "materially_nonuniform_backoff_rows": statistics[
            "materially_nonuniform_backoff_rows"
        ],
        "max_l1_from_uniform": statistics["max_l1_from_uniform"],
        "exact_row_count": statistics["exact_row_count"],
        "backoff_row_count": statistics["backoff_row_count"],
        "training_trajectories": resources["training_trajectories"],
        "training_node_touches": resources["training_node_touches"],
        "solver_information_rows": resources["solver_information_rows"],
        "solver_sha256": blueprint.source_solver_sha256,
    }
    _validate_frozen_observation(row, threshold)
    return row


def require_frozen_final_observation(
    state: SolverState,
    compiled: Mapping[str, Any],
    frozen: Mapping[str, Any],
    threshold: int,
) -> dict[str, Any]:
    """Require every formal solver/stat/resource field to equal the selector."""

    if type(frozen) is not dict:
        raise TypeError("frozen final observation must be an exact object")
    _validate_frozen_observation(frozen, threshold)
    actual = build_scale_observation(state, compiled, threshold)
    if actual != frozen:
        raise RuntimeError("formal target state/statistics differ from frozen selector")
    return actual


def _route_root() -> Path:
    route = Path(__file__).parents[1]
    if route.is_symlink() or not route.is_dir():
        raise ValueError("route root must be a real non-symlink directory")
    resolved = route.resolve(strict=True)
    expected_suffix = Path("bots/research_native_lab/cfr_neural_search")
    if Path(*resolved.parts[-3:]) != expected_suffix:
        raise ValueError("training tool resolved from an alternate route root")
    return resolved


def _assert_module_provenance(route: Path) -> None:
    import bots.research_native_lab.cfr_neural_search.blueprint.artifact as artifact
    import bots.research_native_lab.cfr_neural_search.blueprint.hunl_game as hunl_game
    import bots.research_native_lab.cfr_neural_search.blueprint.hunl_training as training
    import bots.research_native_lab.cfr_neural_search.blueprint.mccfr as mccfr
    import bots.research_native_lab.cfr_neural_search.core.strict_io as strict_io

    for module in (artifact, hunl_game, training, mccfr, strict_io):
        source = Path(module.__file__)
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"route module is not a real file: {module.__name__}")
        try:
            source.resolve(strict=True).relative_to(route)
        except ValueError as exc:
            raise ValueError(f"route module came from an alternate root: {module.__name__}") from exc
    common_root = route.parent / "common_contracts"
    imported_common = Path(common_contracts.__file__).resolve(strict=True).parent
    if imported_common != common_root.resolve(strict=True) or common_root.is_symlink():
        raise ValueError("Common package came from an alternate/symlink root")


def _route_source_manifest(route: Path) -> dict[str, str]:
    return stable_tree_manifest(
        route,
        excluded_paths=SOURCE_SNAPSHOT_EXCLUDED_PATHS,
    )


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    route_files: tuple[tuple[str, str], ...]
    common_files: tuple[tuple[str, str], ...]
    excluded_route_paths: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "route_files": dict(self.route_files),
            "common_files": dict(self.common_files),
            "excluded_route_paths": list(self.excluded_route_paths),
        }

    @property
    def digest(self) -> str:
        return payload_sha256(self.to_payload())


def capture_source_snapshot(route: Path) -> SourceSnapshot:
    common = _regular_source_manifest(route.parent / "common_contracts")
    return SourceSnapshot(
        tuple(sorted(_route_source_manifest(route).items())),
        tuple(sorted(common.items())),
        tuple(sorted(SOURCE_SNAPSHOT_EXCLUDED_PATHS)),
    )


def _formal_run_contract(
    config: Mapping[str, Any],
    snapshot: SourceSnapshot,
) -> dict[str, Any]:
    if type(config) is not dict:
        raise TypeError("formal run config must be an exact object")
    training = config["training"]
    return {
        "schema": FORMAL_RUN_CONTRACT_SCHEMA,
        "pinned_config": config,
        "pinned_config_sha256": payload_sha256(config),
        "target_batches": training["batches"],
        "shard_count": training["shard_count"],
        "max_workers": training["max_workers"],
        "cli_source_sha256": file_sha256(Path(__file__)),
        "source_snapshot": snapshot.to_payload(),
        "source_snapshot_sha256": snapshot.digest,
    }


def _load_config(route: Path, *, require_frozen: bool = True) -> dict[str, Any]:
    config_path = DEFAULT_CONFIG.resolve(strict=True)
    expected = route / "configs" / "hunl_m4_blueprint.json"
    if config_path != expected or expected.is_symlink():
        raise ValueError("training accepts only the repository-pinned M4 config")
    payload = strict_json_read(config_path, root=route, context="M4 training config")
    _keys(
        payload,
        {"schema", "training", "diagnostic_evidence", "influence_gate", "scale_gate"},
        "M4 config",
    )
    if payload["schema"] != CONFIG_SCHEMA:
        raise ValueError("unsupported M4 training config")
    training = payload["training"]
    _keys(
        training,
        {
            "abstraction",
            "solver",
            "batches",
            "shard_count",
            "max_workers",
            "checkpoint_every_complete_batches",
        },
        "M4 training section",
    )
    for field in (
        "batches",
        "shard_count",
        "max_workers",
        "checkpoint_every_complete_batches",
    ):
        _positive_exact_int(training[field], f"training.{field}")
    if training["max_workers"] > training["shard_count"]:
        raise ValueError("training max_workers cannot exceed shard_count")
    if type(training["abstraction"]) is not dict or type(training["solver"]) is not dict:
        raise TypeError("training abstraction/solver must be exact objects")
    HUNLAbstractionConfig(**training["abstraction"])
    solver_config = SolverConfig.from_payload(training["solver"])
    scale = payload["scale_gate"]
    _keys(
        scale,
        {
            "schema",
            "candidate_batches",
            "selection_metric",
            "selection_threshold",
            "selection_rule",
            "input_authority",
            "forbidden_inputs",
            "selection_status",
            "frozen_observations",
            "frozen_selected_batches",
            "large_training_forbidden_until",
            "correctness_gate_max_batches",
            "correctness_gate_max_samples_per_player",
        },
        "M4 scale gate",
    )
    if scale["schema"] != "route-b-hunl-training-only-scale-v1":
        raise ValueError("unsupported training-only scale schema")
    candidates = scale["candidate_batches"]
    if (
        type(candidates) is not list
        or any(type(value) is not int for value in candidates)
        or tuple(candidates) != FROZEN_SCALE_CANDIDATES
    ):
        raise ValueError("scale candidates differ from the preregistered sequence")
    threshold = _positive_exact_int(
        scale["selection_threshold"],
        "scale.selection_threshold",
    )
    if (
        scale["selection_metric"] != "materially_nonuniform_all_rows"
        or scale["selection_rule"] != "first_candidate_meeting_threshold"
        or scale["input_authority"] != "training_only"
        or scale["forbidden_inputs"]
        != [
            "diagnostic_tcp_results",
            "chip_results",
            "diagnostic_deck_seed",
            "external_policy_seeds",
        ]
    ):
        raise ValueError("training-only selector authority/rule drifted")
    observations = scale["frozen_observations"]
    if type(observations) is not list or any(type(row) is not dict for row in observations):
        raise TypeError("frozen selector observations must be exact objects")
    for row in observations:
        _validate_frozen_observation(row, threshold)
    status = scale["selection_status"]
    selected = scale["frozen_selected_batches"]
    if status == "pending_discovery":
        if observations or selected is not None or training["batches"] != candidates[-1]:
            raise ValueError("pending selector config must have no frozen result")
        if require_frozen:
            raise ValueError("formal training is blocked until selector trace is frozen")
    elif status == "frozen_first_pass":
        if type(selected) is not int or selected not in candidates:
            raise ValueError("frozen selected batch target is invalid")
        if training["batches"] != selected:
            raise ValueError("formal training target differs from frozen selector")
        observed_batches = [row.get("batches") for row in observations]
        expected_batches = [value for value in candidates if value <= selected]
        if observed_batches != expected_batches:
            raise ValueError("frozen selector trace skips or adds a candidate")
        if (
            not observations
            or any(row.get("passed") is not False for row in observations[:-1])
            or observations[-1].get("passed") is not True
        ):
            raise ValueError("frozen selector trace does not encode the first pass")
    else:
        raise ValueError("unknown selector freeze status")
    max_batches = _positive_exact_int(
        scale["correctness_gate_max_batches"],
        "scale.correctness_gate_max_batches",
    )
    max_samples = _positive_exact_int(
        scale["correctness_gate_max_samples_per_player"],
        "scale.correctness_gate_max_samples_per_player",
    )
    prerequisites = scale["large_training_forbidden_until"]
    if max_batches != candidates[-1]:
        raise ValueError("scale max batches must equal the last preregistered candidate")
    if (
        type(prerequisites) is not list
        or not prerequisites
        or any(type(value) is not str or not value for value in prerequisites)
    ):
        raise ValueError("scale prerequisites must be nonempty exact strings")
    if (
        training["batches"] > max_batches
        or solver_config.samples_per_player > max_samples
    ):
        raise ValueError("large training is forbidden before all M4 correctness gates")
    if training["checkpoint_every_complete_batches"] != 1:
        raise ValueError("M4 requires an atomic checkpoint after every complete batch")
    diagnostic = payload["diagnostic_evidence"]
    _keys(
        diagnostic,
        {
            "hands",
            "deck_root_seed",
            "policy_seeds",
            "seed_domains_must_be_distinct_from_training",
            "local_wire_mode",
            "local_action_delay_sec",
            "result_authority",
            "chip_result_acceptance_weight",
        },
        "M4 diagnostic evidence",
    )
    if diagnostic["hands"] != 70 or type(diagnostic["hands"]) is not int:
        raise ValueError("diagnostic evidence must contain exactly 70 hands")
    if diagnostic["seed_domains_must_be_distinct_from_training"] is not True:
        raise ValueError("diagnostic seed separation must be exact true")
    if (
        diagnostic["local_wire_mode"] != "local-sever-lf"
        or diagnostic["result_authority"] != "diagnostic_only"
        or type(diagnostic["chip_result_acceptance_weight"]) is not int
        or diagnostic["chip_result_acceptance_weight"] != 0
    ):
        raise ValueError("diagnostic authority/wire/chip policy drifted")
    delay = diagnostic["local_action_delay_sec"]
    if type(delay) not in (int, float) or not math.isfinite(float(delay)) or delay != 0:
        raise ValueError("local diagnostic action delay must be exact finite zero")
    influence = payload["influence_gate"]
    _keys(
        influence,
        {
            "material_l1_tolerance",
            "required_material_rows",
            "required_material_decisions_per_side",
            "allowed_material_sources",
        },
        "M4 influence gate",
    )
    tolerance = influence["material_l1_tolerance"]
    if type(tolerance) not in (int, float) or not math.isfinite(float(tolerance)) or tolerance <= 0:
        raise ValueError("material tolerance must be positive finite numeric")
    _positive_exact_int(influence["required_material_rows"], "required material rows")
    if influence["required_material_rows"] != threshold:
        raise ValueError("selector threshold differs from influence material-row gate")
    _positive_exact_int(
        influence["required_material_decisions_per_side"],
        "required material decisions per side",
    )
    allowed_sources = influence["allowed_material_sources"]
    if allowed_sources != ["exact", "backoff"]:
        raise ValueError("material source allowlist drifted")
    training_seed = solver_config.seed
    evidence = payload["diagnostic_evidence"]
    if type(evidence["policy_seeds"]) is not list or len(evidence["policy_seeds"]) != 2:
        raise ValueError("diagnostic policy_seeds must contain exactly two roots")
    roots = [training_seed, evidence["deck_root_seed"], *evidence["policy_seeds"]]
    if any(type(value) is not int for value in roots) or len(set(roots)) != len(roots):
        raise ValueError("training/deck/policy seed domains must be distinct exact integers")
    return payload


def _heartbeat(
    workspace: Path,
    *,
    status: str,
    state: SolverState,
    checkpoint_sha256: str | None,
    source_snapshot: SourceSnapshot,
) -> None:
    atomic_json_write(
        workspace / "heartbeat.json",
        {
            "schema": HEARTBEAT_SCHEMA,
            "status": status,
            "epoch_ns": time.time_ns(),
            "pid": os.getpid(),
            "batch_index": state.batch_index,
            "solver_sha256": state.digest,
            "checkpoint_sha256": checkpoint_sha256,
            "source_snapshot_sha256": source_snapshot.digest,
        },
        root=workspace,
    )


def run_training(
    workspace: Path,
    *,
    resume: bool,
    max_new_batches: int | None = None,
) -> dict[str, Any]:
    if max_new_batches is not None and (
        type(max_new_batches) is not int or max_new_batches < 0
    ):
        raise ValueError("max_new_batches must be a nonnegative exact integer or None")
    route = _route_root()
    _assert_module_provenance(route)
    runtime_root = validate_real_directory(route / RUNTIME_ROOT_NAME)
    workspace = validate_real_directory(workspace)
    try:
        workspace.relative_to(runtime_root)
    except ValueError as exc:
        raise ValueError("training workspace must be below route runtime_outputs") from exc
    if workspace.is_symlink() or not workspace.is_dir():
        raise ValueError("training workspace must be a real directory")
    assert_workspace_not_invalidated(route, workspace)
    config = _load_config(route)
    training = config["training"]
    abstraction = HUNLAbstractionConfig(**training["abstraction"])
    game = HUNLTrainingGame(abstraction)
    solver_config = SolverConfig.from_payload(training["solver"])
    checkpoint = workspace / "checkpoint.json"
    baseline = capture_source_snapshot(route)
    run_contract = _formal_run_contract(config, baseline)
    last_checkpoint_sha: str | None
    if resume:
        if not checkpoint.is_file() or checkpoint.is_symlink():
            raise ValueError("--resume requires an existing real checkpoint")
        state, last_checkpoint_sha = load_hunl_checkpoint_with_digest(
            checkpoint,
            game,
            root=workspace,
            run_contract=run_contract,
        )
        if state.config != solver_config:
            raise ValueError("resume checkpoint solver config differs from pinned config")
    else:
        if checkpoint.exists() or checkpoint.is_symlink():
            raise ValueError("checkpoint exists; explicit --resume is required")
        state = SolverState.new_for_game(game, solver_config)
        last_checkpoint_sha = None
    target = training["batches"]
    if state.batch_index > target:
        raise ValueError("checkpoint already exceeds the pinned correctness-scale target")
    completed_now = 0
    _heartbeat(
        workspace,
        status="running",
        state=state,
        checkpoint_sha256=last_checkpoint_sha,
        source_snapshot=baseline,
    )
    try:
        while state.batch_index < target:
            assert_workspace_not_invalidated(route, workspace)
            if (workspace / "CANCEL").exists():
                _heartbeat(
                    workspace,
                    status="cancelled_at_batch_boundary",
                    state=state,
                    checkpoint_sha256=last_checkpoint_sha,
                    source_snapshot=baseline,
                )
                return {"status": "cancelled", "batch_index": state.batch_index}
            if max_new_batches is not None and completed_now >= max_new_batches:
                _heartbeat(
                    workspace,
                    status="paused_after_checkpoint",
                    state=state,
                    checkpoint_sha256=last_checkpoint_sha,
                    source_snapshot=baseline,
                )
                return {"status": "paused", "batch_index": state.batch_index}
            shards = build_independent_hunl_shards(
                game,
                state,
                training["shard_count"],
                max_workers=training["max_workers"],
                run_contract=run_contract,
            )
            apply_hunl_shards(
                game,
                state,
                shards,
                run_contract=run_contract,
            )
            assert_workspace_not_invalidated(route, workspace)
            if capture_source_snapshot(route) != baseline:
                raise RuntimeError("route/Common source changed during training batch")
            last_checkpoint_sha = save_hunl_checkpoint(
                checkpoint,
                game,
                state,
                root=workspace,
                run_contract=run_contract,
            )
            _heartbeat(
                workspace,
                status="checkpointed",
                state=state,
                checkpoint_sha256=last_checkpoint_sha,
                source_snapshot=baseline,
            )
            completed_now += 1
    except KeyboardInterrupt:
        _heartbeat(
            workspace,
            status="interrupted_last_complete_checkpoint_preserved",
            state=state,
            checkpoint_sha256=last_checkpoint_sha,
            source_snapshot=baseline,
        )
        raise
    if capture_source_snapshot(route) != baseline:
        raise RuntimeError("route/Common source changed before artifact compilation")
    assert_workspace_not_invalidated(route, workspace)
    compiled = compile_blueprint_payload(
        game,
        state,
        run_contract=run_contract,
    )
    required_material = config["influence_gate"]["required_material_rows"]
    require_frozen_final_observation(
        state,
        compiled,
        config["scale_gate"]["frozen_observations"][-1],
        required_material,
    )
    if (
        type(required_material) is not int
        or required_material <= 0
        or compiled["statistics"]["materially_nonuniform_all_rows"]
        < required_material
    ):
        raise RuntimeError("frozen training target did not produce material policy rows")
    artifact_path = workspace / "blueprint.rbbp"
    assert_workspace_not_invalidated(route, workspace)
    artifact_sha = save_blueprint_artifact(
        artifact_path,
        game,
        state,
        root=workspace,
        run_contract=run_contract,
    )
    loaded = load_blueprint_artifact(artifact_path, game, root=workspace)
    if loaded.statistics != compiled["statistics"]:
        raise RuntimeError("saved artifact statistics differ from compiled gate evidence")
    assert_workspace_not_invalidated(route, workspace)
    _heartbeat(
        workspace,
        status="complete",
        state=state,
        checkpoint_sha256=last_checkpoint_sha,
        source_snapshot=baseline,
    )
    assert_workspace_not_invalidated(route, workspace)
    return {
        "status": "complete",
        "batch_index": state.batch_index,
        "solver_sha256": state.digest,
        "checkpoint_sha256": last_checkpoint_sha,
        "artifact_sha256": artifact_sha,
        "source_snapshot_sha256": baseline.digest,
        "run_contract_sha256": payload_sha256(run_contract),
        "materially_nonuniform_rows": loaded.statistics[
            "materially_nonuniform_all_rows"
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-new-batches", type=int, default=None)
    args = parser.parse_args(argv)
    result = run_training(
        args.workspace,
        resume=args.resume,
        max_new_batches=args.max_new_batches,
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["status"] in {"complete", "paused", "cancelled"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
