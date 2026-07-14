"""Reproduce the frozen training-only M4 batch-scale selection trace."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from ..blueprint.artifact import compile_blueprint_payload
from ..blueprint.hunl_abstraction import HUNLAbstractionConfig
from ..blueprint.hunl_game import HUNLTrainingGame
from ..blueprint.hunl_training import (
    apply_hunl_shards,
    build_independent_hunl_shards,
    load_hunl_checkpoint_with_digest,
    save_hunl_checkpoint,
)
from ..blueprint.mccfr import SolverConfig, SolverState
from ..core.identity import file_sha256, payload_sha256
from ..core.run_journal import DurableRunJournal
from ..core.selector_invalidation import assert_workspace_not_invalidated
from ..core.strict_io import atomic_json_write, load_hashed_json, validate_real_directory
from .train_hunl_blueprint import (
    FROZEN_SCALE_CANDIDATES,
    DEFAULT_CONFIG,
    RUNTIME_ROOT_NAME,
    _assert_module_provenance,
    _load_config,
    _route_root,
    _validate_frozen_observation,
    build_scale_observation,
    capture_source_snapshot,
)


SELECTION_EVIDENCE_SCHEMA = "route-b-hunl-training-scale-selection-evidence-v1"
SELECTION_RUN_SCHEMA = "route-b-hunl-training-scale-selector-run-v1"
FROZEN_CANDIDATES = FROZEN_SCALE_CANDIDATES


def _keys(payload: Mapping[str, Any], expected: set[str], context: str) -> None:
    if type(payload) is not dict or set(payload) != expected:
        raise ValueError(f"{context} differs from strict schema")


def _selection_run_contract(
    config: Mapping[str, Any],
    source_snapshot: Any,
) -> dict[str, Any]:
    return {
        "schema": SELECTION_RUN_SCHEMA,
        "pinned_config": config,
        "pinned_config_sha256": payload_sha256(config),
        "candidate_batches": list(FROZEN_CANDIDATES),
        "target_batches": FROZEN_CANDIDATES[-1],
        "selector_source_sha256": file_sha256(Path(__file__)),
        "source_snapshot": source_snapshot.to_payload(),
        "source_snapshot_sha256": source_snapshot.digest,
        "input_authority": "training_only",
        "forbidden_inputs": [
            "diagnostic_tcp_results",
            "chip_results",
            "diagnostic_deck_seed",
            "external_policy_seeds",
        ],
    }


def _trace_payload(
    *,
    status: str,
    config: Mapping[str, Any],
    run_contract: Mapping[str, Any],
    source_snapshot: Any,
    observations: list[dict[str, Any]],
    checkpoint_sha256: str | None,
) -> dict[str, Any]:
    selected = next(
        (row["batches"] for row in observations if row["passed"]),
        None,
    )
    return {
        "schema": SELECTION_EVIDENCE_SCHEMA,
        "status": status,
        "input_authority": "training_only",
        "pinned_config_sha256": payload_sha256(config),
        "run_contract_sha256": payload_sha256(run_contract),
        "source_snapshot_sha256": source_snapshot.digest,
        "candidate_batches": list(FROZEN_CANDIDATES),
        "selection_metric": "materially_nonuniform_all_rows",
        "selection_threshold": config["influence_gate"]["required_material_rows"],
        "selection_rule": "first_candidate_meeting_threshold",
        "observations": observations,
        "selected_batches": selected,
        "checkpoint_sha256": checkpoint_sha256,
        "forbidden_inputs": run_contract["forbidden_inputs"],
    }


def _load_existing_trace(
    path: Path,
    workspace: Path,
    *,
    config: Mapping[str, Any],
    run_contract: Mapping[str, Any],
    source_snapshot_sha256: str,
    checkpoint_sha256: str,
    allow_stale_checkpoint: bool = False,
) -> tuple[list[dict[str, Any]], str | None]:
    if type(allow_stale_checkpoint) is not bool:
        raise TypeError("allow_stale_checkpoint must be an exact boolean")
    payload = load_hashed_json(path, root=workspace)
    _keys(
        payload,
        {
            "schema",
            "status",
            "input_authority",
            "pinned_config_sha256",
            "run_contract_sha256",
            "source_snapshot_sha256",
            "candidate_batches",
            "selection_metric",
            "selection_threshold",
            "selection_rule",
            "observations",
            "selected_batches",
            "checkpoint_sha256",
            "forbidden_inputs",
        },
        "selector evidence",
    )
    if payload["schema"] != SELECTION_EVIDENCE_SCHEMA:
        raise ValueError("unsupported selector evidence")
    if (
        payload["pinned_config_sha256"] != payload_sha256(config)
        or payload["run_contract_sha256"] != payload_sha256(run_contract)
        or payload["source_snapshot_sha256"] != source_snapshot_sha256
        or payload["candidate_batches"] != list(FROZEN_CANDIDATES)
        or payload["input_authority"] != "training_only"
        or payload["selection_rule"] != "first_candidate_meeting_threshold"
        or payload["selection_metric"] != "materially_nonuniform_all_rows"
        or payload["selection_threshold"]
        != config["influence_gate"]["required_material_rows"]
        or payload["forbidden_inputs"] != run_contract["forbidden_inputs"]
    ):
        raise ValueError("selector evidence differs from the current frozen contract")
    observations = payload["observations"]
    if type(observations) is not list or any(type(row) is not dict for row in observations):
        raise TypeError("selector observations must be exact objects")
    for row in observations:
        _validate_frozen_observation(
            row,
            config["influence_gate"]["required_material_rows"],
        )
    observed_batches = [row["batches"] for row in observations]
    if observed_batches != list(FROZEN_CANDIDATES[: len(observed_batches)]):
        raise ValueError("selector observations are not a unique ordered candidate prefix")
    passed = [row["batches"] for row in observations if row["passed"]]
    derived_selected = passed[0] if passed else None
    if len(passed) > 1 or (passed and observations[-1]["passed"] is not True):
        raise ValueError("selector trace continues beyond its first passing candidate")
    if payload["selected_batches"] != derived_selected:
        raise ValueError("selector selected_batches is not derived from observations")
    if (derived_selected is not None) is not (payload["status"] == "complete"):
        raise ValueError("selector status is inconsistent with its first-pass result")
    if derived_selected is None and payload["status"] not in {
        "candidate_failed",
        "paused_after_checkpoint",
        "cancelled_at_batch_boundary",
    }:
        raise ValueError("selector unfinished status is invalid")
    checkpoint_sha = payload["checkpoint_sha256"]
    if checkpoint_sha is not None and (
        type(checkpoint_sha) is not str
        or len(checkpoint_sha) != 64
        or any(character not in "0123456789abcdef" for character in checkpoint_sha)
    ):
        raise ValueError("selector checkpoint digest is invalid")
    if checkpoint_sha != checkpoint_sha256 and not allow_stale_checkpoint:
        raise ValueError("selector trace checkpoint digest differs from checkpoint file")
    return observations, checkpoint_sha


def _replay_resume_checkpoint(
    *,
    route: Path,
    game: HUNLTrainingGame,
    solver_config: SolverConfig,
    training: Mapping[str, Any],
    config: Mapping[str, Any],
    run_contract: Mapping[str, Any],
    source_snapshot: Any,
    checkpoint_state: SolverState,
    observations: list[dict[str, Any]],
    progress: Callable[[SolverState], None] | None = None,
) -> None:
    """Replay from batch 0 so a locally re-signed trace cannot be trusted."""

    replay = SolverState.new_for_game(game, solver_config)
    rows_by_batch = {row["batches"]: row for row in observations}
    threshold = config["influence_gate"]["required_material_rows"]
    while replay.batch_index < checkpoint_state.batch_index:
        shards = build_independent_hunl_shards(
            game,
            replay,
            training["shard_count"],
            max_workers=training["max_workers"],
            run_contract=run_contract,
        )
        apply_hunl_shards(game, replay, shards, run_contract=run_contract)
        if capture_source_snapshot(route) != source_snapshot:
            raise RuntimeError("Route/Common source changed during selector replay")
        if progress is not None:
            progress(replay)
        if replay.batch_index in FROZEN_CANDIDATES:
            if capture_source_snapshot(route) != source_snapshot:
                raise RuntimeError("Route/Common source changed before replay observation")
            compiled = compile_blueprint_payload(
                game,
                replay,
                run_contract=run_contract,
            )
            row = build_scale_observation(replay, compiled, threshold)
            recorded = rows_by_batch.get(replay.batch_index)
            if recorded is not None and row != recorded:
                raise ValueError("selector resume trace differs from batch-0 replay")
            if recorded is None and replay.batch_index < checkpoint_state.batch_index:
                raise ValueError("selector resume trace omitted an earlier candidate")
            if row["passed"] and replay.batch_index < checkpoint_state.batch_index:
                raise ValueError("selector checkpoint continued beyond replayed first pass")
            if capture_source_snapshot(route) != source_snapshot:
                raise RuntimeError("Route/Common source changed after replay observation")
    if replay.digest != checkpoint_state.digest:
        raise ValueError("selector checkpoint state differs from deterministic batch-0 replay")


def run_selection(
    workspace: Path,
    *,
    resume: bool,
    discover: bool = False,
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
        raise ValueError("selector workspace must be below runtime_outputs") from exc
    if type(discover) is not bool:
        raise TypeError("discover must be an exact boolean")
    assert_workspace_not_invalidated(route, workspace)
    config = _load_config(route, require_frozen=not discover)
    scale = config["scale_gate"]
    if tuple(scale["candidate_batches"]) != FROZEN_CANDIDATES:
        raise ValueError("config candidate batches differ from the frozen selector")
    frozen_observations = scale["frozen_observations"]
    frozen_selected = scale["frozen_selected_batches"]
    if discover and scale["selection_status"] != "pending_discovery":
        raise ValueError("--discover requires the explicitly pending config")
    if not discover and scale["selection_status"] != "frozen_first_pass":
        raise ValueError("reproduction requires a frozen first-pass config")
    training = config["training"]
    if not discover and training["batches"] != frozen_selected:
        raise ValueError("formal target differs from frozen selected batches")
    game = HUNLTrainingGame(HUNLAbstractionConfig(**training["abstraction"]))
    solver_config = SolverConfig.from_payload(training["solver"])
    snapshot = capture_source_snapshot(route)
    run_contract = _selection_run_contract(config, snapshot)
    config_file_sha256 = file_sha256(DEFAULT_CONFIG)
    route_files = dict(snapshot.route_files)
    if (
        route_files.get("configs/hunl_m4_blueprint.json") != config_file_sha256
        or route_files.get("tools/select_hunl_scale.py")
        != run_contract["selector_source_sha256"]
        or capture_source_snapshot(route) != snapshot
    ):
        raise RuntimeError("selector config/source changed around run-contract capture")
    checkpoint = workspace / "selector_checkpoint.json"
    trace_path = workspace / "selection.json"
    if not resume and (checkpoint.exists() or trace_path.exists()):
        raise ValueError("selector outputs exist; explicit --resume is required")
    journal_identity = {
        "run_contract_sha256": payload_sha256(run_contract),
        "source_snapshot_sha256": snapshot.digest,
        "config_payload_sha256": payload_sha256(config),
        "config_file_sha256": config_file_sha256,
    }
    journal = DurableRunJournal.open(
        workspace,
        journal_identity,
        resume=resume,
    )
    prior = journal.previous_heartbeat
    tip = journal.last_event
    known_batches = 0 if tip is None else int(tip["completed_batches"])
    known_checkpoint = None if tip is None else tip["checkpoint_sha256"]
    start_event = "selector_resume_started" if resume else "selector_started"
    journal.append(
        start_event,
        completed_batches=known_batches,
        checkpoint_sha256=known_checkpoint,
        details={
            "discover": discover,
            "max_new_batches": max_new_batches,
            "orphaned_event_temps": dict(journal.orphaned_event_temps),
        },
    )
    journal.heartbeat(
        "started",
        detail=start_event,
        completed_batches=known_batches,
        checkpoint_sha256=known_checkpoint,
    )
    state: SolverState | None = None
    checkpoint_sha: str | None = known_checkpoint
    durable_batch_index = known_batches
    observations: list[dict[str, Any]] = []

    def transition(
        event: str,
        heartbeat_status: str,
        detail: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        detail_payload = {} if details is None else dict(details)
        detail_payload["durable_batch_index"] = durable_batch_index
        detail_payload["in_memory_batch_index"] = (
            None if state is None else state.batch_index
        )
        journal.append(
            event,
            completed_batches=durable_batch_index,
            checkpoint_sha256=checkpoint_sha,
            details=detail_payload,
        )
        journal.heartbeat(
            heartbeat_status,
            detail=detail,
            completed_batches=durable_batch_index,
            checkpoint_sha256=checkpoint_sha,
        )

    try:
        if resume:
            state, checkpoint_sha = load_hunl_checkpoint_with_digest(
                checkpoint,
                game,
                root=workspace,
                run_contract=run_contract,
            )
            durable_batch_index = state.batch_index
            if trace_path.is_file() and not trace_path.is_symlink():
                observations, trace_checkpoint_sha = _load_existing_trace(
                    trace_path,
                    workspace,
                    config=config,
                    run_contract=run_contract,
                    source_snapshot_sha256=snapshot.digest,
                    checkpoint_sha256=checkpoint_sha,
                    allow_stale_checkpoint=True,
                )
            else:
                observations = []
                trace_checkpoint_sha = None
            if known_batches > state.batch_index:
                raise ValueError("resume heartbeat is ahead of the durable checkpoint")
            if known_batches == state.batch_index and known_checkpoint != checkpoint_sha:
                raise ValueError("resume heartbeat checkpoint digest drifted")
            recovered_gap = state.batch_index - known_batches
            if recovered_gap and not any(
                event["event"] in {"batch_completed", "batch_checkpoint_recovered"}
                and event["completed_batches"] == state.batch_index
                for event in journal.events
            ):
                journal.append(
                    "batch_checkpoint_recovered",
                    completed_batches=durable_batch_index,
                    checkpoint_sha256=checkpoint_sha,
                    details={
                        "durable_batch_index": durable_batch_index,
                        "heartbeat_completed_batches": known_batches,
                        "solver_sha256": state.digest,
                    },
                )
                journal.heartbeat(
                    "running",
                    detail="batch_checkpoint_recovered",
                    completed_batches=durable_batch_index,
                    checkpoint_sha256=checkpoint_sha,
                )
            transition(
                "resume_checkpoint_loaded",
                "running",
                "resume_checkpoint_loaded",
                details={
                    "heartbeat_completed_batches": known_batches,
                    "recovered_checkpoint_gap": recovered_gap,
                    "solver_sha256": state.digest,
                },
            )
        else:
            state = SolverState.new_for_game(game, solver_config)
            checkpoint_sha = None
        if state.config != solver_config or state.batch_index > FROZEN_CANDIDATES[-1]:
            raise ValueError("selector checkpoint differs from the pinned solver/scale")
        observed_batches = [row.get("batches") for row in observations]
        required_prior = [
            value for value in FROZEN_CANDIDATES if value < state.batch_index
        ]
        allowed = [required_prior]
        if state.batch_index in FROZEN_CANDIDATES:
            allowed.append([*required_prior, state.batch_index])
        if observed_batches not in allowed:
            raise ValueError(
                "selector trace must be the exact prior prefix with at most current candidate"
            )
        if any(row.get("passed") for row in observations[:-1]):
            raise ValueError("selector checkpoint continued beyond an earlier first pass")
        if not discover and observations != frozen_observations[: len(observations)]:
            raise ValueError("selector resume trace differs from frozen observation prefix")
        if resume:
            def replay_progress(replay: SolverState) -> None:
                journal.append(
                    "resume_replay_batch",
                    completed_batches=durable_batch_index,
                    checkpoint_sha256=checkpoint_sha,
                    details={
                        "durable_batch_index": durable_batch_index,
                        "replay_batch_index": replay.batch_index,
                        "solver_sha256": replay.digest,
                    },
                )
                journal.heartbeat(
                    "running",
                    detail="resume_replay_batch",
                    completed_batches=durable_batch_index,
                    checkpoint_sha256=checkpoint_sha,
                )

            _replay_resume_checkpoint(
                route=route,
                game=game,
                solver_config=solver_config,
                training=training,
                config=config,
                run_contract=run_contract,
                source_snapshot=snapshot,
                checkpoint_state=state,
                observations=observations,
                progress=replay_progress,
            )
            if trace_path.is_file() and trace_checkpoint_sha != checkpoint_sha:
                atomic_json_write(
                    trace_path,
                    _trace_payload(
                        status="paused_after_checkpoint",
                        config=config,
                        run_contract=run_contract,
                        source_snapshot=snapshot,
                        observations=observations,
                        checkpoint_sha256=checkpoint_sha,
                    ),
                    root=workspace,
                )
                if capture_source_snapshot(route) != snapshot:
                    raise RuntimeError(
                        "Route/Common source changed while recovering selector trace"
                    )
                transition(
                    "trace_checkpoint_recovered",
                    "running",
                    "trace_checkpoint_recovered",
                    details={
                        "previous_trace_checkpoint_sha256": trace_checkpoint_sha,
                        "observed_candidates": [
                            row["batches"] for row in observations
                        ],
                    },
                )

        def record_current_candidate() -> bool:
            nonlocal observations
            assert state is not None
            assert_workspace_not_invalidated(route, workspace)
            if state.batch_index not in FROZEN_CANDIDATES:
                return False
            existing = [
                row for row in observations if row.get("batches") == state.batch_index
            ]
            if existing:
                if len(existing) != 1 or observations[-1] is not existing[0]:
                    raise ValueError(
                        "current selector candidate is duplicated or out of order"
                    )
                transition(
                    "candidate_observation_confirmed",
                    "running",
                    "candidate_observation_confirmed",
                    details={"observation": existing[0]},
                )
                return bool(existing[0]["passed"])
            if capture_source_snapshot(route) != snapshot:
                raise RuntimeError("Route/Common source changed before selector compilation")
            compiled = compile_blueprint_payload(
                game,
                state,
                run_contract=run_contract,
            )
            row = build_scale_observation(
                state,
                compiled,
                config["influence_gate"]["required_material_rows"],
            )
            expected_index = len(observations)
            if not discover and (
                expected_index >= len(frozen_observations)
                or row != frozen_observations[expected_index]
            ):
                raise RuntimeError(
                    "recomputed selector observation differs from frozen trace"
                )
            observations = [*observations, row]
            candidate_status = "complete" if row["passed"] else "candidate_failed"
            atomic_json_write(
                trace_path,
                _trace_payload(
                    status=candidate_status,
                    config=config,
                    run_contract=run_contract,
                    source_snapshot=snapshot,
                    observations=observations,
                    checkpoint_sha256=checkpoint_sha,
                ),
                root=workspace,
            )
            if capture_source_snapshot(route) != snapshot:
                raise RuntimeError(
                    "Route/Common source changed while publishing selector trace"
                )
            transition(
                "candidate_observed",
                "running",
                "candidate_observed",
                details={"observation": row},
            )
            return bool(row["passed"])

        if record_current_candidate():
            if not discover and state.batch_index != frozen_selected:
                raise RuntimeError("selector first pass differs from frozen target")
            result = _trace_payload(
                status="complete",
                config=config,
                run_contract=run_contract,
                source_snapshot=snapshot,
                observations=observations,
                checkpoint_sha256=checkpoint_sha,
            )
            if capture_source_snapshot(route) != snapshot:
                raise RuntimeError(
                    "Route/Common source changed before selector success return"
                )
            assert_workspace_not_invalidated(route, workspace)
            transition(
                "selector_completed",
                "completed",
                "first_passing_candidate_completed",
                details={"selected_batches": state.batch_index},
            )
            assert_workspace_not_invalidated(route, workspace)
            return result

        completed_now = 0
        while state.batch_index < FROZEN_CANDIDATES[-1]:
            assert_workspace_not_invalidated(route, workspace)
            if (workspace / "CANCEL").exists():
                status = "cancelled_at_batch_boundary"
                break
            if max_new_batches is not None and completed_now >= max_new_batches:
                status = "paused_after_checkpoint"
                break
            shards = build_independent_hunl_shards(
                game,
                state,
                training["shard_count"],
                max_workers=training["max_workers"],
                run_contract=run_contract,
            )
            apply_hunl_shards(game, state, shards, run_contract=run_contract)
            assert_workspace_not_invalidated(route, workspace)
            if capture_source_snapshot(route) != snapshot:
                raise RuntimeError("route/Common source changed during selector batch")
            checkpoint_sha = save_hunl_checkpoint(
                checkpoint,
                game,
                state,
                root=workspace,
                run_contract=run_contract,
            )
            durable_batch_index = state.batch_index
            completed_now += 1
            transition(
                "batch_completed",
                "running",
                "batch_completed",
                details={
                    "solver_sha256": state.digest,
                    "training_trajectories": state.trajectories,
                    "training_node_touches": state.node_touches,
                },
            )
            if record_current_candidate():
                if not discover and state.batch_index != frozen_selected:
                    raise RuntimeError("selector first pass differs from frozen target")
                status = "complete"
                break
        else:
            raise RuntimeError("all frozen selector candidates failed the material gate")
        payload = _trace_payload(
            status=status,
            config=config,
            run_contract=run_contract,
            source_snapshot=snapshot,
            observations=observations,
            checkpoint_sha256=checkpoint_sha,
        )
        atomic_json_write(trace_path, payload, root=workspace)
        assert_workspace_not_invalidated(route, workspace)
        if capture_source_snapshot(route) != snapshot:
            raise RuntimeError("Route/Common source changed before selector return")
        if status == "complete":
            transition(
                "selector_completed",
                "completed",
                "first_passing_candidate_completed",
                details={"selected_batches": state.batch_index},
            )
        else:
            transition(
                "selector_cancelled",
                "cancelled",
                status,
                details={"reason": status},
            )
        assert_workspace_not_invalidated(route, workspace)
        return payload
    except BaseException as exc:
        failure_details = {
            "exception_type": type(exc).__name__,
            "message": str(exc)[:1000],
        }
        try:
            transition(
                "selector_failed",
                "failed",
                "selector_failed",
                details=failure_details,
            )
        except BaseException:
            try:
                if journal.events:
                    authoritative_tip = journal.events[-1]
                    journal.heartbeat(
                        "failed",
                        detail="selector_failed_journal_update_incomplete",
                        completed_batches=authoritative_tip["completed_batches"],
                        checkpoint_sha256=authoritative_tip["checkpoint_sha256"],
                    )
            except BaseException:
                pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--discover", action="store_true")
    parser.add_argument("--max-new-batches", type=int, default=None)
    arguments = parser.parse_args(argv)
    result = run_selection(
        arguments.workspace,
        resume=arguments.resume,
        discover=arguments.discover,
        max_new_batches=arguments.max_new_batches,
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
