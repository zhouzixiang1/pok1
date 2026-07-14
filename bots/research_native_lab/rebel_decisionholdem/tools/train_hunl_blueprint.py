"""Train/export the bounded route-A2 HUNL smoke blueprint and scale evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import time
from pathlib import Path
from typing import Any

from ..decisionholdem_like.hunl_blueprint import (
    HUNL_MATERIAL_POLICY_L1_THRESHOLD,
    HUNLBlueprint,
    build_hunl_blueprint_payload,
    policy_l1_from_uniform,
    save_hunl_blueprint,
)
from ..decisionholdem_like.hunl_external_sampling import (
    HUNLExternalSamplingLCFR,
    HUNLTrainingConfig,
    strict_json_loads,
    training_identity_snapshot,
)
from ..decisionholdem_like.secure_files import (
    assert_real_directory,
    atomic_json_write,
    canonical_bytes,
    pretty_json_bytes,
    stable_read_path,
)


CONFIG_SCHEMA = "route-a2-hunl-m4-smoke-config-v4"
SCALE_SCHEMA = "route-a2-hunl-m4-scale-gate-v5"
SEED_INDEPENDENCE_CONTRACT = "route-a2-hunl-independent-root-seeds-v1"
ITERATION_SELECTION_CONTRACT = "route-a2-hunl-training-only-first-pass-v1"
ITERATION_CANDIDATES = (2, 4, 8, 16, 32)
TRAINING_RUN_CHECKPOINT_SCHEMA = "route-a2-hunl-training-run-checkpoint-v1"
TRAINING_HEARTBEAT_SCHEMA = "route-a2-hunl-training-heartbeat-v1"
TRAINING_RUN_CONTRACT = "route-a2-hunl-durable-training-run-v1"
PACKAGE_ROOT = Path(__file__).resolve().parents[1]


class TrainingRunCancelled(RuntimeError):
    """Raised only after a cancellation marker is observed at a durable boundary."""


def _canonical_bytes(value: object) -> bytes:
    return canonical_bytes(value)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _exact_int(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def load_config_payload(payload: object) -> dict[str, Any]:
    """Validate a decoded config after a canonical strict-JSON round trip."""

    payload = strict_json_loads(_canonical_bytes(payload))
    if not isinstance(payload, dict) or set(payload) != {
        "artifact_path",
        "scale_estimate_iterations",
        "schema",
        "source_commit",
        "tcp_client_policy_seeds",
        "tcp_deck_root_seed",
        "training",
    }:
        raise ValueError("HUNL smoke config fields are invalid")
    if payload["schema"] != CONFIG_SCHEMA:
        raise ValueError("HUNL smoke config schema mismatch")
    if type(payload["artifact_path"]) is not str or not payload["artifact_path"]:
        raise ValueError("artifact_path must be a non-empty relative path")
    artifact = Path(payload["artifact_path"])
    if artifact.is_absolute() or ".." in artifact.parts:
        raise ValueError("artifact_path must stay inside the route package")
    deck_seed = _exact_int(payload["tcp_deck_root_seed"], "tcp_deck_root_seed")
    if deck_seed >= 2**63:
        raise ValueError("tcp_deck_root_seed must fit in an unsigned 63-bit value")
    policy_seeds = payload["tcp_client_policy_seeds"]
    if (
        not isinstance(policy_seeds, list)
        or len(policy_seeds) != 2
        or any(type(seed) is not int or not 0 <= seed < 2**63 for seed in policy_seeds)
    ):
        raise ValueError("tcp_client_policy_seeds must contain two unsigned 63-bit integers")
    _exact_int(
        payload["scale_estimate_iterations"],
        "scale_estimate_iterations",
        minimum=1,
    )
    training = payload["training"]
    if not isinstance(training, dict) or set(training) != {
        "iteration_candidates",
        "frozen_selected_iterations",
        "max_nodes_per_traversal",
        "material_nonuniform_l1_threshold",
        "minimum_each_backoff_nonuniform_rows",
        "minimum_exact_nonuniform_rows",
        "seed",
        "shard_size",
        "utility_unit_chips",
    }:
        raise ValueError("HUNL smoke training config fields are invalid")
    training_config = HUNLTrainingConfig(
        seed=_exact_int(training["seed"], "seed"),
        utility_unit_chips=_exact_int(
            training["utility_unit_chips"], "utility_unit_chips", minimum=1
        ),
        max_nodes_per_traversal=_exact_int(
            training["max_nodes_per_traversal"],
            "max_nodes_per_traversal",
            minimum=1,
        ),
    )
    if training["iteration_candidates"] != list(ITERATION_CANDIDATES):
        raise ValueError("iteration_candidates must equal the preregistered sequence")
    if _exact_int(
        training["frozen_selected_iterations"],
        "frozen_selected_iterations",
        minimum=1,
    ) != 32:
        raise ValueError("frozen_selected_iterations must equal the training-only result")
    if training["material_nonuniform_l1_threshold"] != (
        HUNL_MATERIAL_POLICY_L1_THRESHOLD
    ):
        raise ValueError("material nonuniform threshold differs from the frozen contract")
    if _exact_int(
        training["minimum_exact_nonuniform_rows"],
        "minimum_exact_nonuniform_rows",
        minimum=1,
    ) != 1:
        raise ValueError("minimum_exact_nonuniform_rows must equal one")
    if _exact_int(
        training["minimum_each_backoff_nonuniform_rows"],
        "minimum_each_backoff_nonuniform_rows",
        minimum=1,
    ) != 1:
        raise ValueError("minimum_each_backoff_nonuniform_rows must equal one")
    _exact_int(training["shard_size"], "shard_size", minimum=1)
    roots = (training_config.seed, deck_seed, *policy_seeds)
    if len(set(roots)) != len(roots):
        raise ValueError("training, deck and client policy root seeds must be distinct")
    return payload


def load_config(path: str | Path) -> dict[str, Any]:
    return load_config_payload(strict_json_loads(stable_read_path(path)))


def seed_independence_snapshot_from_roots(
    *,
    training_seed: int,
    deck_seed: int,
    policy_seeds: tuple[int, int] | list[int],
) -> dict[str, object]:
    if type(policy_seeds) not in (tuple, list) or len(policy_seeds) != 2:
        raise ValueError("policy_seeds must be an exact two-item tuple or list")
    roots = (training_seed, deck_seed, *policy_seeds)
    if (
        any(type(seed) is not int or not 0 <= seed < 2**63 for seed in roots)
        or len(set(roots)) != 4
    ):
        raise ValueError("training, deck and two policy seeds must be distinct uint63 roots")
    return {
        "all_root_seeds_distinct": True,
        "contract": SEED_INDEPENDENCE_CONTRACT,
        "smoke_inputs_excluded_from_blueprint_build": True,
        "tcp_deck": {
            "domain": "route-a2-sever-deck-v1",
            "root_seed": deck_seed,
        },
        "tcp_policy": [
            {
                "domain": f"CommonA2StrategyRuntime-policy-client-{index}",
                "root_seed": seed,
            }
            for index, seed in enumerate(policy_seeds)
        ],
        "training": {
            "domain": "HUNLExternalSamplingLCFR-deal-and-opponent-v1",
            "root_seed": training_seed,
        },
    }


def seed_independence_snapshot(config: dict[str, Any]) -> dict[str, object]:
    """Bind disjoint predeclared RNG roots and their separate hash domains."""

    validated = load_config_payload(config)
    training_seed = validated["training"]["seed"]
    deck_seed = validated["tcp_deck_root_seed"]
    policy_seeds = list(validated["tcp_client_policy_seeds"])
    return seed_independence_snapshot_from_roots(
        training_seed=training_seed,
        deck_seed=deck_seed,
        policy_seeds=policy_seeds,
    )


def blueprint_nonuniformity_snapshot(blueprint: HUNLBlueprint) -> dict[str, object]:
    if type(blueprint) is not HUNLBlueprint:
        raise TypeError("blueprint must be the exact HUNLBlueprint type")

    def table_snapshot(rows: dict[str, dict[str, float]]) -> dict[str, object]:
        distances = [policy_l1_from_uniform(row) for row in rows.values()]
        return {
            "materially_nonuniform_rows": sum(
                distance > HUNL_MATERIAL_POLICY_L1_THRESHOLD
                for distance in distances
            ),
            "max_l1_from_uniform": max(distances, default=0.0),
            "rows": len(rows),
        }

    exact = table_snapshot(blueprint.policies)
    backoff = {
        level: table_snapshot(rows)
        for level, rows in blueprint.trained_backoff_policies.items()
    }
    all_tables = [exact, *backoff.values()]
    return {
        "exact": exact,
        "l1_threshold": HUNL_MATERIAL_POLICY_L1_THRESHOLD,
        "total_materially_nonuniform_rows": sum(
            int(table["materially_nonuniform_rows"]) for table in all_tables
        ),
        "total_rows": sum(int(table["rows"]) for table in all_tables),
        "trained_backoff": backoff,
    }


def _new_trainer(training: dict[str, Any]) -> HUNLExternalSamplingLCFR:
    return HUNLExternalSamplingLCFR(
        HUNLTrainingConfig(
            seed=training["seed"],
            utility_unit_chips=training["utility_unit_chips"],
            max_nodes_per_traversal=training["max_nodes_per_traversal"],
        )
    )


def _candidate_trace_row(
    trainer: HUNLExternalSamplingLCFR,
    training: dict[str, Any],
    *,
    source_commit: str,
) -> dict[str, object]:
    candidate = HUNLBlueprint(
        build_hunl_blueprint_payload(trainer, source_commit=source_commit)
    )
    snapshot = blueprint_nonuniformity_snapshot(candidate)
    exact_count = int(snapshot["exact"]["materially_nonuniform_rows"])
    backoff_counts = {
        level: int(values["materially_nonuniform_rows"])
        for level, values in snapshot["trained_backoff"].items()
    }
    failures: list[str] = []
    if exact_count < training["minimum_exact_nonuniform_rows"]:
        failures.append("exact_materially_nonuniform_rows")
    if any(
        count < training["minimum_each_backoff_nonuniform_rows"]
        for count in backoff_counts.values()
    ):
        failures.append("each_backoff_materially_nonuniform_rows")
    return {
        "failure_reasons": failures,
        "finite_and_normalized": True,
        "iterations": trainer.iterations_completed,
        "passed": not failures,
        "policy_nonuniformity": snapshot,
    }


def select_training_candidate(
    training: dict[str, Any],
    *,
    source_commit: str,
) -> tuple[HUNLExternalSamplingLCFR, list[dict[str, object]]]:
    """Select the first training-only candidate; never inspect TCP evidence."""

    trainer = _new_trainer(training)
    trace: list[dict[str, object]] = []
    for iterations in ITERATION_CANDIDATES:
        trainer.train_to(iterations, shard_size=training["shard_size"])
        row = _candidate_trace_row(
            trainer,
            training,
            source_commit=source_commit,
        )
        trace.append(row)
        if row["passed"]:
            if iterations != training["frozen_selected_iterations"]:
                raise RuntimeError(
                    "first passing candidate differs from frozen training-only selection"
                )
            return trainer, trace
    raise RuntimeError("no preregistered training iteration candidate passed")


def _absolute_path(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _reject_overlapping_paths(paths: dict[str, Path | None]) -> None:
    seen: dict[Path, str] = {}
    for label, raw_path in paths.items():
        if raw_path is None:
            continue
        path = _absolute_path(raw_path)
        previous = seen.get(path)
        if previous is not None:
            raise ValueError(f"{label} path overlaps {previous}: {path}")
        seen[path] = label


def _default_training_workspace(
    config: dict[str, Any],
    *,
    output: Path,
) -> Path:
    identity = {
        "artifact_target": str(_absolute_path(output)),
        "config_sha256": _sha256(_canonical_bytes(config)),
    }
    suffix = _sha256(_canonical_bytes(identity))[:20]
    return PACKAGE_ROOT / "checkpoints" / f"hunl-m4-{suffix}"


def _create_default_training_workspace(path: Path) -> None:
    checkpoint_root = path.parent
    if not checkpoint_root.exists():
        checkpoint_root.mkdir(mode=0o700)
    assert_real_directory(checkpoint_root)
    if not path.exists():
        path.mkdir(mode=0o700)
    assert_real_directory(path)


def _resolve_training_run_paths(
    config: dict[str, Any],
    *,
    output: Path,
    checkpoint: Path | None,
    resume_checkpoint: Path | None,
    heartbeat: Path | None,
) -> tuple[Path, Path, Path, bool, bool]:
    if checkpoint is not None and resume_checkpoint is not None:
        if _absolute_path(checkpoint) != _absolute_path(resume_checkpoint):
            raise ValueError(
                "--checkpoint and --resume-checkpoint must not name different runs"
            )
    resume = resume_checkpoint is not None
    explicit = checkpoint is not None or resume_checkpoint is not None
    selected = resume_checkpoint or checkpoint
    if selected is None:
        workspace = _default_training_workspace(config, output=output)
        _create_default_training_workspace(workspace)
        checkpoint_path = workspace / "run_checkpoint.json"
    else:
        checkpoint_path = _absolute_path(selected)
        workspace = checkpoint_path.parent
        assert_real_directory(workspace)
    heartbeat_path = (
        _absolute_path(heartbeat)
        if heartbeat is not None
        else workspace / "heartbeat.json"
    )
    if heartbeat_path.parent != workspace:
        raise ValueError("heartbeat must stay in the fixed checkpoint workspace")
    cancel_path = workspace / "CANCEL"
    _reject_overlapping_paths(
        {
            "artifact output": output,
            "training checkpoint/selection journal": checkpoint_path,
            "training heartbeat": heartbeat_path,
            "training cancel marker": cancel_path,
        }
    )
    return checkpoint_path, heartbeat_path, cancel_path, resume, explicit


def _training_run_binding(
    config: dict[str, Any],
    *,
    output: Path,
    checkpoint_path: Path,
    heartbeat_path: Path,
    cancel_path: Path,
    config_source: Path | None = None,
) -> dict[str, object]:
    training_identity = training_identity_snapshot()
    if config_source is None:
        config_source_binding: dict[str, object] = {
            "authority": "validated_in_memory_payload",
        }
    else:
        source_path = _absolute_path(config_source)
        source_bytes = stable_read_path(source_path)
        if load_config_payload(strict_json_loads(source_bytes)) != config:
            raise RuntimeError("live config source differs from the validated payload")
        config_source_binding = {
            "authority": "stable_regular_file",
            "path": str(source_path),
            "sha256": _sha256(source_bytes),
        }
    return {
        "artifact_target": str(_absolute_path(output)),
        "cancel_marker_path": str(_absolute_path(cancel_path)),
        "candidate_sequence": list(ITERATION_CANDIDATES),
        "checkpoint_path": str(_absolute_path(checkpoint_path)),
        "config": config,
        "config_source": config_source_binding,
        "config_sha256": _sha256(_canonical_bytes(config)),
        "contract": TRAINING_RUN_CONTRACT,
        "heartbeat_path": str(_absolute_path(heartbeat_path)),
        "selection_contract": ITERATION_SELECTION_CONTRACT,
        "source_commit": config["source_commit"],
        "target_iterations": config["training"]["frozen_selected_iterations"],
        "training_identity": training_identity,
        "training_identity_sha256": _sha256(_canonical_bytes(training_identity)),
        "workspace": str(_absolute_path(checkpoint_path.parent)),
    }


def _assert_live_run_identity(run_binding: dict[str, object]) -> None:
    current = training_identity_snapshot()
    if (
        current != run_binding["training_identity"]
        or _sha256(_canonical_bytes(current))
        != run_binding["training_identity_sha256"]
    ):
        raise RuntimeError("training implementation identity drifted during the run")
    source = run_binding["config_source"]
    if not isinstance(source, dict):
        raise RuntimeError("training config source binding is invalid")
    if source.get("authority") == "stable_regular_file":
        source_bytes = stable_read_path(str(source["path"]))
        if (
            _sha256(source_bytes) != source.get("sha256")
            or load_config_payload(strict_json_loads(source_bytes))
            != run_binding["config"]
        ):
            raise RuntimeError("training config source drifted during the run")
    elif source != {"authority": "validated_in_memory_payload"}:
        raise RuntimeError("training config source authority is invalid")


def _training_run_checkpoint_payload(
    trainer: HUNLExternalSamplingLCFR,
    trace: list[dict[str, object]],
    *,
    run_binding: dict[str, object],
    selected_iterations: int | None,
) -> dict[str, object]:
    trainer_checkpoint = trainer.checkpoint_payload()
    trainer_body = trainer_checkpoint.get("body")
    if (
        not isinstance(trainer_body, dict)
        or trainer_body.get("training_identity")
        != run_binding["training_identity"]
    ):
        raise RuntimeError(
            "trainer checkpoint identity differs from the captured run identity"
        )
    body = {
        "run_binding": run_binding,
        "selected_iterations": selected_iterations,
        "selection_trace": trace,
        "trainer_checkpoint": trainer_checkpoint,
    }
    return {
        "body": body,
        "body_sha256": _sha256(_canonical_bytes(body)),
        "schema": TRAINING_RUN_CHECKPOINT_SCHEMA,
    }


def _persist_training_run_checkpoint(
    path: Path,
    trainer: HUNLExternalSamplingLCFR,
    trace: list[dict[str, object]],
    *,
    run_binding: dict[str, object],
    selected_iterations: int | None,
) -> dict[str, object]:
    _assert_live_run_identity(run_binding)
    payload = _training_run_checkpoint_payload(
        trainer,
        trace,
        run_binding=run_binding,
        selected_iterations=selected_iterations,
    )
    atomic_json_write(path, payload)
    _assert_live_run_identity(run_binding)
    if strict_json_loads(stable_read_path(path)) != payload:
        raise RuntimeError("published training checkpoint failed stable readback")
    return payload


def _replay_selection_trace(
    training: dict[str, Any],
    trace: list[dict[str, object]],
    *,
    source_commit: str,
) -> HUNLExternalSamplingLCFR:
    if len(trace) > len(ITERATION_CANDIDATES):
        raise ValueError("resume selection trace is longer than the candidate sequence")
    replay = _new_trainer(training)
    for index, recorded in enumerate(trace):
        expected_iteration = ITERATION_CANDIDATES[index]
        replay.train_to(expected_iteration, shard_size=training["shard_size"])
        expected = _candidate_trace_row(
            replay,
            training,
            source_commit=source_commit,
        )
        if recorded != expected:
            raise ValueError(
                f"resume selection trace differs at candidate {expected_iteration}"
            )
    return replay


def _load_training_run_checkpoint(
    path: Path,
    *,
    training: dict[str, Any],
    run_binding: dict[str, object],
) -> tuple[HUNLExternalSamplingLCFR, list[dict[str, object]], int | None]:
    payload = strict_json_loads(stable_read_path(path))
    if not isinstance(payload, dict) or set(payload) != {
        "body",
        "body_sha256",
        "schema",
    }:
        raise ValueError("training run checkpoint wrapper fields are invalid")
    if payload["schema"] != TRAINING_RUN_CHECKPOINT_SCHEMA:
        raise ValueError("training run checkpoint schema mismatch")
    body = payload["body"]
    if not isinstance(body, dict) or set(body) != {
        "run_binding",
        "selected_iterations",
        "selection_trace",
        "trainer_checkpoint",
    }:
        raise ValueError("training run checkpoint body fields are invalid")
    if payload["body_sha256"] != _sha256(_canonical_bytes(body)):
        raise ValueError("training run checkpoint content hash mismatch")
    if body["run_binding"] != run_binding:
        raise ValueError("training run checkpoint config/target/identity binding mismatch")
    raw_trainer_checkpoint = body["trainer_checkpoint"]
    if (
        not isinstance(raw_trainer_checkpoint, dict)
        or not isinstance(raw_trainer_checkpoint.get("body"), dict)
        or raw_trainer_checkpoint["body"].get("training_identity")
        != run_binding["training_identity"]
    ):
        raise ValueError(
            "training run trainer identity differs from its captured binding"
        )
    raw_trace = body["selection_trace"]
    if not isinstance(raw_trace, list) or any(
        not isinstance(row, dict) for row in raw_trace
    ):
        raise ValueError("training run checkpoint selection trace is invalid")
    trace: list[dict[str, object]] = list(raw_trace)
    trainer = HUNLExternalSamplingLCFR.from_checkpoint_payload(raw_trainer_checkpoint)
    if trainer.config != _new_trainer(training).config:
        raise ValueError("training run checkpoint trainer config mismatch")
    replay = _replay_selection_trace(
        training,
        trace,
        source_commit=str(run_binding["source_commit"]),
    )
    evaluated = [int(row["iterations"]) for row in trace]
    if evaluated != list(ITERATION_CANDIDATES[: len(trace)]):
        raise ValueError("training run selection trace is not a candidate prefix")
    passed = [row for row in trace if row.get("passed") is True]
    selected = body["selected_iterations"]
    if selected is not None and type(selected) is not int:
        raise ValueError("training run selected_iterations must be null or integer")
    if passed:
        if len(passed) != 1 or passed[-1] is not trace[-1]:
            raise ValueError("training run continued after the first passing candidate")
        if selected != trace[-1]["iterations"]:
            raise ValueError("training run selected candidate disagrees with its trace")
    elif selected is not None:
        raise ValueError("training run selected a candidate without a passing trace")
    if selected is not None and selected != training["frozen_selected_iterations"]:
        raise ValueError("training run selected candidate differs from the frozen target")
    last_evaluated = evaluated[-1] if evaluated else 0
    next_candidate = (
        ITERATION_CANDIDATES[len(trace)]
        if len(trace) < len(ITERATION_CANDIDATES)
        else ITERATION_CANDIDATES[-1]
    )
    if not last_evaluated <= trainer.iterations_completed <= next_candidate:
        raise ValueError("training run trainer counter lies outside its trace frontier")
    if selected is not None and trainer.iterations_completed != selected:
        raise ValueError("completed training run counter differs from selected candidate")
    replay.train_to(
        trainer.iterations_completed,
        shard_size=training["shard_size"],
    )
    if replay.checkpoint_payload() != trainer.checkpoint_payload():
        raise ValueError("training run trainer checkpoint is not deterministic replay")
    return trainer, trace, selected


def _write_training_heartbeat(
    path: Path,
    trainer: HUNLExternalSamplingLCFR,
    *,
    checkpoint_path: Path,
    trace: list[dict[str, object]],
    segments_this_process: int,
    phase: str,
    started: float,
    run_binding: dict[str, object],
) -> None:
    checkpoint_bytes = stable_read_path(checkpoint_path)
    body = {
        "checkpoint_path": str(_absolute_path(checkpoint_path)),
        "checkpoint_sha256": _sha256(checkpoint_bytes),
        "elapsed_sec_this_process": time.perf_counter() - started,
        "iterations_completed": trainer.iterations_completed,
        "nodes_visited": trainer.nodes_visited,
        "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "phase": phase,
        "run_binding_sha256": _sha256(_canonical_bytes(run_binding)),
        "segments_completed_this_process": segments_this_process,
        "selection_trace_entries": len(trace),
        "training_identity_sha256": run_binding["training_identity_sha256"],
        "traversals_completed": trainer.traversals_completed,
    }
    payload = {
        "body": body,
        "body_sha256": _sha256(_canonical_bytes(body)),
        "schema": TRAINING_HEARTBEAT_SCHEMA,
    }
    atomic_json_write(path, payload)
    if stable_read_path(path) != pretty_json_bytes(payload):
        raise RuntimeError("published training heartbeat failed exact byte readback")


def _cancel_requested(cancel_path: Path) -> bool:
    try:
        stable_read_path(cancel_path)
    except FileNotFoundError:
        return False
    return True


def _raise_if_cancelled(
    cancel_path: Path,
    heartbeat_path: Path,
    trainer: HUNLExternalSamplingLCFR,
    *,
    checkpoint_path: Path,
    trace: list[dict[str, object]],
    segments_this_process: int,
    started: float,
    run_binding: dict[str, object],
) -> None:
    if not _cancel_requested(cancel_path):
        return
    _write_training_heartbeat(
        heartbeat_path,
        trainer,
        checkpoint_path=checkpoint_path,
        trace=trace,
        segments_this_process=segments_this_process,
        phase="cancelled_at_checkpoint",
        started=started,
        run_binding=run_binding,
    )
    raise TrainingRunCancelled(
        f"training cancelled at durable iteration {trainer.iterations_completed}; "
        f"remove {cancel_path} and resume {checkpoint_path}"
    )


def select_training_candidate_resumable(
    config: dict[str, Any],
    *,
    output: Path,
    checkpoint_path: Path,
    heartbeat_path: Path,
    cancel_path: Path,
    resume: bool,
    stop_after_segments: int | None = None,
    run_binding: dict[str, object] | None = None,
    progress: dict[str, int] | None = None,
) -> tuple[HUNLExternalSamplingLCFR, list[dict[str, object]]]:
    """Persist every sequential segment and replay all prior selection evidence."""

    training = config["training"]
    if run_binding is None:
        run_binding = _training_run_binding(
            config,
            output=output,
            checkpoint_path=checkpoint_path,
            heartbeat_path=heartbeat_path,
            cancel_path=cancel_path,
        )
    if stop_after_segments is not None:
        _exact_int(stop_after_segments, "stop_after_segments", minimum=1)
    started = time.perf_counter()
    segments = 0
    if progress is not None:
        progress.clear()
        progress["segments_completed_this_process"] = 0
    if resume:
        trainer, trace, selected = _load_training_run_checkpoint(
            checkpoint_path,
            training=training,
            run_binding=run_binding,
        )
    else:
        if checkpoint_path.exists():
            raise FileExistsError(
                "training run checkpoint already exists; pass --resume-checkpoint"
            )
        if heartbeat_path.exists():
            raise FileExistsError("training heartbeat already exists in a new workspace")
        trainer, trace, selected = _new_trainer(training), [], None
        _persist_training_run_checkpoint(
            checkpoint_path,
            trainer,
            trace,
            run_binding=run_binding,
            selected_iterations=None,
        )
        _write_training_heartbeat(
            heartbeat_path,
            trainer,
            checkpoint_path=checkpoint_path,
            trace=trace,
            segments_this_process=segments,
            phase="initialized",
            started=started,
            run_binding=run_binding,
        )
    _assert_live_run_identity(run_binding)
    _raise_if_cancelled(
        cancel_path,
        heartbeat_path,
        trainer,
        checkpoint_path=checkpoint_path,
        trace=trace,
        segments_this_process=segments,
        started=started,
        run_binding=run_binding,
    )
    if selected is not None:
        _write_training_heartbeat(
            heartbeat_path,
            trainer,
            checkpoint_path=checkpoint_path,
            trace=trace,
            segments_this_process=segments,
            phase="selected_checkpoint_reloaded",
            started=started,
            run_binding=run_binding,
        )
        return trainer, trace
    for candidate in ITERATION_CANDIDATES[len(trace) :]:
        while trainer.iterations_completed < candidate:
            _assert_live_run_identity(run_binding)
            count = min(
                training["shard_size"],
                candidate - trainer.iterations_completed,
            )
            trainer.apply_shard(trainer.build_shard(count))
            segments += 1
            if progress is not None:
                progress["segments_completed_this_process"] = segments
            _persist_training_run_checkpoint(
                checkpoint_path,
                trainer,
                trace,
                run_binding=run_binding,
                selected_iterations=None,
            )
            _write_training_heartbeat(
                heartbeat_path,
                trainer,
                checkpoint_path=checkpoint_path,
                trace=trace,
                segments_this_process=segments,
                phase="segment_committed",
                started=started,
                run_binding=run_binding,
            )
            _raise_if_cancelled(
                cancel_path,
                heartbeat_path,
                trainer,
                checkpoint_path=checkpoint_path,
                trace=trace,
                segments_this_process=segments,
                started=started,
                run_binding=run_binding,
            )
            if stop_after_segments is not None and segments >= stop_after_segments:
                raise RuntimeError("simulated interruption after durable segment")
        _assert_live_run_identity(run_binding)
        row = _candidate_trace_row(
            trainer,
            training,
            source_commit=config["source_commit"],
        )
        _assert_live_run_identity(run_binding)
        if row["passed"] and candidate != training["frozen_selected_iterations"]:
            raise RuntimeError(
                "first passing candidate differs from frozen training-only selection"
            )
        trace.append(row)
        selected = candidate if row["passed"] else None
        _persist_training_run_checkpoint(
            checkpoint_path,
            trainer,
            trace,
            run_binding=run_binding,
            selected_iterations=selected,
        )
        _write_training_heartbeat(
            heartbeat_path,
            trainer,
            checkpoint_path=checkpoint_path,
            trace=trace,
            segments_this_process=segments,
            phase="candidate_evaluated",
            started=started,
            run_binding=run_binding,
        )
        _raise_if_cancelled(
            cancel_path,
            heartbeat_path,
            trainer,
            checkpoint_path=checkpoint_path,
            trace=trace,
            segments_this_process=segments,
            started=started,
            run_binding=run_binding,
        )
        if row["passed"]:
            return trainer, trace
    raise RuntimeError("no preregistered training iteration candidate passed")


def train_and_export(
    config: dict[str, Any],
    *,
    output: Path,
    scale_evidence: Path | None = None,
    checkpoint: Path | None = None,
    resume_checkpoint: Path | None = None,
    heartbeat: Path | None = None,
    config_source: Path | None = None,
    _stop_after_segments: int | None = None,
) -> dict[str, object]:
    config = load_config_payload(config)
    output = _absolute_path(output)
    checkpoint_path, heartbeat_path, cancel_path, resume, explicit_checkpoint = (
        _resolve_training_run_paths(
            config,
            output=output,
            checkpoint=checkpoint,
            resume_checkpoint=resume_checkpoint,
            heartbeat=heartbeat,
        )
    )
    _reject_overlapping_paths(
        {
            "config source": config_source,
            "artifact output": output,
            "scale evidence": scale_evidence,
            "training checkpoint/selection journal": checkpoint_path,
            "training heartbeat": heartbeat_path,
            "training cancel marker": cancel_path,
        }
    )
    assert_real_directory(output.parent)
    if scale_evidence is not None:
        assert_real_directory(_absolute_path(scale_evidence).parent)
    run_binding = _training_run_binding(
        config,
        output=output,
        checkpoint_path=checkpoint_path,
        heartbeat_path=heartbeat_path,
        cancel_path=cancel_path,
        config_source=config_source,
    )
    starting_iterations = 0
    starting_nodes = 0
    if resume:
        starting_wrapper = strict_json_loads(stable_read_path(checkpoint_path))
        starting_body = starting_wrapper["body"]["trainer_checkpoint"]["body"]
        starting_iterations = _exact_int(
            starting_body["iterations_completed"], "starting iterations"
        )
        starting_nodes = _exact_int(starting_body["nodes_visited"], "starting nodes")
    started = time.perf_counter()
    run_progress: dict[str, int] = {}
    trainer, selection_trace = select_training_candidate_resumable(
        config,
        output=output,
        checkpoint_path=checkpoint_path,
        heartbeat_path=heartbeat_path,
        cancel_path=cancel_path,
        resume=resume,
        stop_after_segments=_stop_after_segments,
        run_binding=run_binding,
        progress=run_progress,
    )
    elapsed = time.perf_counter() - started
    _assert_live_run_identity(run_binding)
    _raise_if_cancelled(
        cancel_path,
        heartbeat_path,
        trainer,
        checkpoint_path=checkpoint_path,
        trace=selection_trace,
        segments_this_process=run_progress["segments_completed_this_process"],
        started=started,
        run_binding=run_binding,
    )
    payload = build_hunl_blueprint_payload(
        trainer,
        source_commit=config["source_commit"],
    )
    _assert_live_run_identity(run_binding)
    _raise_if_cancelled(
        cancel_path,
        heartbeat_path,
        trainer,
        checkpoint_path=checkpoint_path,
        trace=selection_trace,
        segments_this_process=run_progress["segments_completed_this_process"],
        started=started,
        run_binding=run_binding,
    )
    expected_artifact_bytes = pretty_json_bytes(payload)
    save_hunl_blueprint(output, payload)
    _assert_live_run_identity(run_binding)
    if stable_read_path(output) != expected_artifact_bytes:
        raise RuntimeError("published HUNL artifact failed exact byte readback")
    reloaded = HUNLBlueprint.load(output)
    _assert_live_run_identity(run_binding)
    run_checkpoint_bytes = stable_read_path(checkpoint_path)
    run_checkpoint_payload = strict_json_loads(run_checkpoint_bytes)
    raw_checkpoint_payload = run_checkpoint_payload["body"]["trainer_checkpoint"]
    if (
        run_checkpoint_payload["body"]["selection_trace"] != selection_trace
        or run_checkpoint_payload["body"]["selected_iterations"]
        != trainer.iterations_completed
    ):
        raise RuntimeError("final training checkpoint and selected trace disagree")
    raw_checkpoint_bytes = pretty_json_bytes(raw_checkpoint_payload)
    checkpoint_bytes = len(raw_checkpoint_bytes)
    iterations = trainer.iterations_completed
    iterations_advanced = iterations - starting_iterations
    nodes_advanced = trainer.nodes_visited - starting_nodes
    if iterations_advanced < 0 or nodes_advanced < 0:
        raise RuntimeError("resumed trainer counters moved backwards")
    estimate_iterations = config["scale_estimate_iterations"]
    body = {
        "artifact_bytes": len(stable_read_path(output)),
        "artifact_sha256": reloaded.digest,
        "checkpoint_bytes": checkpoint_bytes,
        "checkpoint_retained": True,
        "checkpoint_sha256": raw_checkpoint_payload["body_sha256"],
        "checkpoint_training_identity_sha256": run_binding[
            "training_identity_sha256"
        ],
        "correctness_gate_passed": True,
        "durable_resume": {
            "cancel_marker_checked_at_durable_boundaries": True,
            "checkpoint_and_selection_journal_combined": True,
            "checkpoint_file_sha256": _sha256(run_checkpoint_bytes),
            "checkpoint_schema": TRAINING_RUN_CHECKPOINT_SCHEMA,
            "every_segment_atomic": True,
            "full_config_source_candidate_target_binding": True,
            "heartbeat_schema": TRAINING_HEARTBEAT_SCHEMA,
            "resume_replays_prior_selection_trace": True,
            "resumed_this_process": resume,
            "run_contract": TRAINING_RUN_CONTRACT,
            "workspace_auto_selected": not explicit_checkpoint,
        },
        "elapsed_sec_this_process": elapsed,
        "estimated_elapsed_sec_at_configured_scale_from_this_process": (
            None
            if iterations_advanced == 0
            else elapsed * estimate_iterations / iterations_advanced
        ),
        "estimate_caveat": (
            "linear smoke extrapolation only; infoset growth and cache effects are not modeled"
        ),
        "estimate_iterations": estimate_iterations,
        "iterations": iterations,
        "iterations_advanced_this_process": iterations_advanced,
        "iteration_selection": {
            "candidate_sequence": list(ITERATION_CANDIDATES),
            "contract": ITERATION_SELECTION_CONTRACT,
            "selected_iterations": iterations,
            "selection_inputs_exclude_tcp_smoke": True,
            "trace": selection_trace,
        },
        "nodes_advanced_this_process": nodes_advanced,
        "nodes_per_sec_this_process": nodes_advanced / max(elapsed, 1e-12),
        "nodes_visited": trainer.nodes_visited,
        "parallel_checkpoint_segment_merge_supported": False,
        "peak_rss_kib_this_process": resource.getrusage(
            resource.RUSAGE_SELF
        ).ru_maxrss,
        "policy_nonuniformity": blueprint_nonuniformity_snapshot(reloaded),
        "policy_rows": len(reloaded.policies),
        "scale_authorized": False,
        "seed_independence": seed_independence_snapshot(config),
        "trained_backoff_rows": {
            level: len(rows)
            for level, rows in reloaded.trained_backoff_policies.items()
        },
        "training_config": trainer.config.to_dict(),
    }
    result = {
        "body": body,
        "body_sha256": _sha256(_canonical_bytes(body)),
        "schema": SCALE_SCHEMA,
    }
    _assert_live_run_identity(run_binding)
    _raise_if_cancelled(
        cancel_path,
        heartbeat_path,
        trainer,
        checkpoint_path=checkpoint_path,
        trace=selection_trace,
        segments_this_process=run_progress["segments_completed_this_process"],
        started=started,
        run_binding=run_binding,
    )
    if scale_evidence is not None:
        atomic_json_write(scale_evidence, result)
        _assert_live_run_identity(run_binding)
        if stable_read_path(scale_evidence) != pretty_json_bytes(result):
            raise RuntimeError("published scale evidence failed exact byte readback")
    _write_training_heartbeat(
        heartbeat_path,
        trainer,
        checkpoint_path=checkpoint_path,
        trace=selection_trace,
        segments_this_process=run_progress["segments_completed_this_process"],
        phase="published",
        started=started,
        run_binding=run_binding,
    )
    _assert_live_run_identity(run_binding)
    _assert_live_run_identity(run_binding)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=PACKAGE_ROOT / "configs/hunl_m4_smoke.json",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--scale-evidence", type=Path)
    parser.add_argument(
        "--checkpoint",
        type=Path,
        help="start a new durable run journal at this path",
    )
    parser.add_argument(
        "--resume-checkpoint",
        "--resume",
        dest="resume_checkpoint",
        type=Path,
        help="strictly resume an existing durable run journal",
    )
    parser.add_argument(
        "--heartbeat",
        type=Path,
        help="heartbeat path in the same fixed workspace as the run journal",
    )
    args = parser.parse_args()
    config = load_config(args.config)
    output = args.output or PACKAGE_ROOT / config["artifact_path"]
    result = train_and_export(
        config,
        output=output,
        scale_evidence=args.scale_evidence,
        checkpoint=args.checkpoint,
        resume_checkpoint=args.resume_checkpoint,
        heartbeat=args.heartbeat,
        config_source=args.config,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
