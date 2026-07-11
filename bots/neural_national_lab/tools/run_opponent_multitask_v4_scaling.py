#!/usr/bin/env python3
"""Run an early-stop-only multi-seed v4 architecture scaling sweep."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from typing import Any, Iterator

import torch

from multitask_training_data import (
    FROZEN_CHECKPOINT_SCHEMA,
    MODEL_TRAINING_ROLES,
    training_data_metadata,
)
from opponent_multitask_model_v4 import MODEL_FORMAT, MODEL_SCALES
from opponent_exposure_ledger import (
    EXPOSURE_ROLES,
    FINAL_BLIND_ROLE,
    SCHEMA as EXPOSURE_LEDGER_SCHEMA,
)
from role_dataset_access import RoleDatasetAccess
from train_opponent_multitask_v4 import (
    ARTIFACT_MANIFEST_SCHEMA as TRAINING_ARTIFACT_SCHEMA,
    CHECKPOINT_SCHEMA as TRAINING_CHECKPOINT_SCHEMA,
    REPORT_SCHEMA as TRAINING_REPORT_SCHEMA,
    _code_artifacts as current_training_code_artifacts,
    _config as build_training_config,
    load_checkpoint,
    training_environment,
)


SUMMARY_SCHEMA = "opponent_multitask_v4_scaling_summary_v3"
RUN_CONTRACT_SCHEMA = "opponent_multitask_v4_scaling_run_contract_v1"
RUN_CONTRACT_NAME = "scaling_run_contract.json"
SUMMARY_NAME = "scaling_summary.json"
SELECTION_METHOD = "lexicographic_componentwise_seed_median_then_worst_v1"
SELECTION_KEY_ORDER = (
    "match_flip_balanced_error",
    "match_balanced_error",
    "match_nll",
    "secondary_v3_value_response_score",
)
ENCODERS = ("none", "deep_set", "gru", "gru_moe", "transformer")
FORMAL_ENCODERS = ("deep_set", "gru", "gru_moe", "transformer")
FORMAL_SCALES = tuple(MODEL_SCALES)
TRAINER = Path(__file__).with_name("train_opponent_multitask_v4.py")
ROOT = Path(__file__).resolve().parents[3]
TRAINING_FILES = {
    "checkpoint.pt",
    "checkpoint_authorization.json",
    "training_report.json",
}
TRAINING_ARTIFACT_FIELDS = {
    "schema", "run_id", "files", "source_collection_complete",
    "deployment_policy_value", "strength_evidence",
}
TRAINING_AUTHORIZATION_FIELDS = {
    "schema", "frozen", "early_stop_complete", "run_id",
    "role_manifest_sha256", "training_roles", "training_artifact_sha256",
    "checkpoint_sha256",
}
TRAINING_CHECKPOINT_FIELDS = {
    "schema", "role_manifest_sha256", "training_artifact_sha256",
    "source_completed_passes", "source_requested_passes",
    "source_collection_complete", "code_artifacts", "training_environment",
    "model_metadata", "training_data", "training_config", "best_epoch",
    "state_dict",
}
TRAINING_REPORT_FIELDS = {
    "schema", "created_at", "run_id", "command", "role_manifest",
    "role_manifest_sha256", "ledger", "source_completed_passes",
    "source_requested_passes", "source_collection_complete",
    "incomplete_smoke", "opened_roles", "model_calibration_opened",
    "policy_roles_opened", "role_counts", "model", "config", "environment",
    "code_artifacts", "history", "best_epoch", "early_stop",
    "checkpoint_sha256", "checkpoint_authorization",
    "deployment_policy_value", "strength_evidence", "native_tcp_evaluated",
}
EXPOSURE_EVENT_FIELDS = {
    "sequence",
    "timestamp_utc",
    "event",
    "role",
    "run_id",
    "opponents",
    "candidate_sha256",
    "artifact_sha256",
}
TRAINING_OPTION_SPECS = (
    ("--moe-experts", "moe_experts"),
    ("--cross-transformer-heads", "cross_transformer_heads"),
    ("--dropout", "dropout"),
    ("--epochs", "epochs"),
    ("--patience", "patience"),
    ("--minimum-improvement", "minimum_improvement"),
    ("--batch-size", "batch_size"),
    ("--learning-rate", "learning_rate"),
    ("--weight-decay", "weight_decay"),
    ("--gradient-clip-norm", "gradient_clip_norm"),
    ("--hand-clip", "hand_clip"),
    ("--tail-clip", "tail_clip"),
    ("--match-clip", "match_clip"),
    ("--mean-loss-weight", "mean_loss_weight"),
    ("--quantile-loss-weight", "quantile_loss_weight"),
    ("--match-ranking-weight", "match_ranking_weight"),
    ("--match-q20-ranking-weight", "match_q20_ranking_weight"),
    ("--ranking-margin", "ranking_margin"),
    ("--ranking-temperature", "ranking_temperature"),
    ("--outcome-loss-weight", "outcome_loss_weight"),
    ("--outcome-pairwise-weight", "outcome_pairwise_weight"),
    ("--outcome-pairwise-temperature", "outcome_pairwise_temperature"),
    ("--response-loss-weight", "response_loss_weight"),
    ("--response-size-weight", "response_size_weight"),
    ("--device", "device"),
)
RUN_CONTRACT_FIELDS = {
    "schema",
    "created_at",
    "output_dir",
    "role_manifest",
    "role_manifest_sha256",
    "ledger",
    "run_id_prefix",
    "requested",
    "jobs",
    "allow_incomplete_smoke",
    "training_options",
    "python_executable",
    "trainer",
    "trainer_sha256",
    "training_code_artifacts",
    "training_roles",
    "environment",
    "git_commit",
    "scaling_tool_sha256",
    "model_format",
    "summary_schema",
    "selection_method",
    "selection_key_order",
    "model_calibration_opened",
    "policy_roles_opened",
    "deployment_policy_value",
    "strength_evidence",
    "payload_sha256",
}


class ProtectedExposureError(ValueError):
    """A training run touched a role that cannot be repaired by retraining."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(value: Any) -> str:
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _is_sha256(value: Any) -> bool:
    normalized = str(value or "")
    return len(normalized) == 64 and all(
        character in "0123456789abcdef" for character in normalized
    )


def _load_json(path: Path, *, field: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {field}: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{field} must be a JSON object")
    return payload


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-", dir=path.parent
    )
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(
                json.dumps(payload, indent=2, sort_keys=True, allow_nan=False)
                + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def acquire_run_lock(root: Path):
    root.parent.mkdir(parents=True, exist_ok=True)
    lock_path = root.parent / f".{root.name}.scaling.lock"
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise ValueError(f"v4 scaling root is locked: {root}") from exc
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    os.fsync(handle.fileno())
    return handle


def _is_cuda_device(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("cuda")


def _csv_values(
    raw: str, *, choices: tuple[str, ...], field: str
) -> list[str]:
    values = []
    for value in str(raw).split(","):
        normalized = value.strip()
        if normalized and normalized not in values:
            values.append(normalized)
    invalid = [value for value in values if value not in choices]
    if not values or invalid:
        raise ValueError(f"invalid {field}: {invalid or values}")
    return values


def _seeds(raw: str) -> list[int]:
    values = []
    for value in str(raw).split(","):
        normalized = value.strip()
        if not normalized:
            continue
        try:
            seed = int(normalized)
        except ValueError as exc:
            raise ValueError(f"invalid seed: {normalized}") from exc
        if seed < 0:
            raise ValueError("seeds must be non-negative")
        if seed not in values:
            values.append(seed)
    if not values:
        raise ValueError("at least one seed is required")
    return values


def _slug(scale: str, encoder: str, seed: int) -> str:
    return f"{scale}_{encoder}_seed{seed}"


def _requested_payload(
    args: argparse.Namespace,
    *,
    scales: list[str],
    encoders: list[str],
    seeds: list[int],
) -> dict[str, Any]:
    return {
        "scales": list(scales),
        "encoders": list(encoders),
        "seeds": list(seeds),
        "configurations": len(scales) * len(encoders),
        "device": str(args.device),
        "cross_transformer_heads": int(args.cross_transformer_heads),
    }


def _training_role_contract(args: argparse.Namespace) -> dict[str, Any]:
    dataset = RoleDatasetAccess(
        args.role_manifest,
        ledger_path=args.ledger,
        run_id=f"{args.run_id_prefix}-scaling-plan",
        require_complete=not args.allow_incomplete_smoke,
    )
    boundary = (
        dataset.require_collection_boundary(expected_passes=160)
        if not args.allow_incomplete_smoke
        else {
            "source_completed_passes": dataset.manifest.get(
                "source_completed_passes"
            ),
            "source_requested_passes": dataset.manifest.get(
                "source_requested_passes"
            ),
            "source_collection_complete": dataset.manifest.get(
                "source_collection_complete"
            ),
        }
    )
    roles = {}
    for role in MODEL_TRAINING_ROLES:
        files = {}
        for prefix in ("cf", "opponent_actions"):
            filename = f"{prefix}_{role}.jsonl"
            path = dataset.root / filename
            expected = dataset.outputs[filename]
            if (
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_size != expected["bytes"]
                or _sha256(path) != expected["sha256"]
            ):
                raise ValueError(f"training role artifact changed: {path}")
            files[filename] = {
                "rows": expected["rows"],
                "bytes": expected["bytes"],
                "sha256": expected["sha256"],
            }
        roles[role] = {
            "opponents": list(dataset.roles[role]),
            "artifact_sha256": dataset._role_artifact_sha256(role),
            "files": files,
        }
    return {
        "collection_boundary": boundary,
        "candidate_snapshot": dict(dataset.candidate_snapshot),
        "roles": roles,
    }


def _environment_contract(device: str) -> dict[str, Any]:
    affinity = (
        sorted(os.sched_getaffinity(0))
        if hasattr(os, "sched_getaffinity")
        else None
    )
    result: dict[str, Any] = {
        "python": platform.python_version(),
        "python_executable": str(sys.executable),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_affinity": affinity,
        "torch": str(torch.__version__),
        "torch_cuda": torch.version.cuda,
        "cudnn": torch.backends.cudnn.version(),
        "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"),
        "determinism_environment": {
            name: os.environ.get(name)
            for name in (
                "CUBLAS_WORKSPACE_CONFIG",
                "OMP_NUM_THREADS",
                "MKL_NUM_THREADS",
                "OPENBLAS_NUM_THREADS",
                "NUMEXPR_NUM_THREADS",
            )
        },
        "device": str(device),
    }
    if _is_cuda_device(device):
        if not torch.cuda.is_available():
            raise ValueError("CUDA was requested for scaling but is unavailable")
        torch_device = torch.device(device)
        index = (
            torch.cuda.current_device()
            if torch_device.index is None
            else torch_device.index
        )
        properties = torch.cuda.get_device_properties(index)
        result["cuda_device"] = {
            "index": index,
            "name": properties.name,
            "capability": list(torch.cuda.get_device_capability(index)),
            "total_memory": properties.total_memory,
        }
    return result


def _git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    commit = result.stdout.strip()
    if result.returncode != 0 or len(commit) != 40:
        raise ValueError("v4 scaling checkout has no valid Git commit")
    return commit


def build_run_contract(
    args: argparse.Namespace,
    *,
    root: Path,
    scales: list[str],
    encoders: list[str],
    seeds: list[int],
    created_at: str,
) -> dict[str, Any]:
    jobs = []
    for scale in scales:
        for encoder in encoders:
            for seed in seeds:
                slug = _slug(scale, encoder, seed)
                run_id = f"{args.run_id_prefix}-{slug}"
                output_dir = (root / slug).resolve()
                jobs.append({
                    "scale": scale,
                    "encoder": encoder,
                    "seed": seed,
                    "slug": slug,
                    "run_id": run_id,
                    "output_dir": str(output_dir),
                    "pythonhashseed": str(seed),
                    "training_environment": training_environment(
                        str(args.device), pythonhashseed=str(seed)
                    ),
                    "command": build_training_command(
                        args,
                        scale=scale,
                        encoder=encoder,
                        seed=seed,
                        output_dir=output_dir,
                        run_id=run_id,
                    ),
                })
    unsigned = {
        "schema": RUN_CONTRACT_SCHEMA,
        "created_at": str(created_at),
        "output_dir": str(root.resolve()),
        "role_manifest": str(args.role_manifest.resolve()),
        "role_manifest_sha256": _sha256(args.role_manifest.resolve()),
        "ledger": str(args.ledger.resolve()),
        "run_id_prefix": str(args.run_id_prefix),
        "requested": _requested_payload(
            args, scales=scales, encoders=encoders, seeds=seeds
        ),
        "jobs": jobs,
        "allow_incomplete_smoke": bool(args.allow_incomplete_smoke),
        "training_options": {
            attribute: getattr(args, attribute)
            for _, attribute in TRAINING_OPTION_SPECS
        },
        "python_executable": str(sys.executable),
        "trainer": str(TRAINER.resolve()),
        "trainer_sha256": _sha256(TRAINER.resolve()),
        "training_code_artifacts": current_training_code_artifacts(),
        "training_roles": _training_role_contract(args),
        "environment": _environment_contract(str(args.device)),
        "git_commit": _git_commit(),
        "scaling_tool_sha256": _sha256(Path(__file__).resolve()),
        "model_format": MODEL_FORMAT,
        "summary_schema": SUMMARY_SCHEMA,
        "selection_method": SELECTION_METHOD,
        "selection_key_order": list(SELECTION_KEY_ORDER),
        "model_calibration_opened": False,
        "policy_roles_opened": False,
        "deployment_policy_value": False,
        "strength_evidence": False,
    }
    return {**unsigned, "payload_sha256": _canonical_sha256(unsigned)}


def validate_run_contract(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("v4 scaling run contract must be a JSON object")
    contract = dict(payload)
    payload_sha256 = str(contract.pop("payload_sha256", ""))
    if (
        set(payload) != RUN_CONTRACT_FIELDS
        or contract.get("schema") != RUN_CONTRACT_SCHEMA
        or not isinstance(contract.get("created_at"), str)
        or not contract["created_at"]
        or payload_sha256 != _canonical_sha256(contract)
        or contract.get("summary_schema") != SUMMARY_SCHEMA
        or contract.get("model_format") != MODEL_FORMAT
        or contract.get("selection_method") != SELECTION_METHOD
        or contract.get("selection_key_order") != list(SELECTION_KEY_ORDER)
        or contract.get("model_calibration_opened") is not False
        or contract.get("policy_roles_opened") is not False
        or contract.get("deployment_policy_value") is not False
        or contract.get("strength_evidence") is not False
    ):
        raise ValueError("v4 scaling run contract binding changed")
    requested = contract.get("requested")
    jobs = contract.get("jobs")
    training_options = contract.get("training_options")
    code_artifacts = contract.get("training_code_artifacts")
    training_roles = contract.get("training_roles")
    try:
        scales = list(requested["scales"])
        encoders = list(requested["encoders"])
        seeds = [int(seed) for seed in requested["seeds"]]
        expected_jobs = [
            (scale, encoder, seed)
            for scale in scales
            for encoder in encoders
            for seed in seeds
        ]
        observed_jobs = [
            (str(job["scale"]), str(job["encoder"]), int(job["seed"]))
            for job in jobs
        ]
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("v4 scaling run contract matrix is invalid") from exc
    if (
        not isinstance(requested, dict)
        or set(requested) != {
            "scales", "encoders", "seeds", "configurations", "device",
            "cross_transformer_heads",
        }
        or not scales
        or len(scales) != len(set(scales))
        or any(scale not in MODEL_SCALES for scale in scales)
        or not encoders
        or len(encoders) != len(set(encoders))
        or any(encoder not in ENCODERS for encoder in encoders)
        or not seeds
        or len(seeds) != len(set(seeds))
        or any(seed < 0 for seed in seeds)
        or requested.get("configurations") != len(scales) * len(encoders)
        or not isinstance(jobs, list)
        or observed_jobs != expected_jobs
        or not isinstance(training_options, dict)
        or set(training_options)
        != {attribute for _, attribute in TRAINING_OPTION_SPECS}
        or training_options.get("device") != requested.get("device")
        or training_options.get("cross_transformer_heads")
        != requested.get("cross_transformer_heads")
        or not isinstance(contract.get("allow_incomplete_smoke"), bool)
        or not _is_sha256(contract.get("role_manifest_sha256"))
        or not _is_sha256(contract.get("trainer_sha256"))
        or not _is_sha256(contract.get("scaling_tool_sha256"))
        or not isinstance(code_artifacts, dict)
        or not code_artifacts
        or any(
            not isinstance(details, dict)
            or set(details) != {"bytes", "sha256"}
            or isinstance(details.get("bytes"), bool)
            or not isinstance(details.get("bytes"), int)
            or details["bytes"] < 1
            or not _is_sha256(details.get("sha256"))
            for details in code_artifacts.values()
        )
        or not isinstance(training_roles, dict)
        or set(training_roles)
        != {"collection_boundary", "candidate_snapshot", "roles"}
        or not isinstance(contract.get("environment"), dict)
        or not contract["environment"]
        or len(str(contract.get("git_commit", ""))) != 40
        or any(
            character not in "0123456789abcdef"
            for character in str(contract.get("git_commit", ""))
        )
    ):
        raise ValueError("v4 scaling run contract structure changed")
    root = Path(str(contract.get("output_dir", "")))
    for (scale, encoder, seed), job in zip(expected_jobs, jobs, strict=True):
        slug = _slug(scale, encoder, seed)
        run_id = f"{contract['run_id_prefix']}-{slug}"
        command = job.get("command")
        if (
            set(job) != {
                "scale", "encoder", "seed", "slug", "run_id", "output_dir",
                "pythonhashseed", "training_environment", "command",
            }
            or job.get("slug") != slug
            or job.get("run_id") != run_id
            or job.get("output_dir") != str((root / slug).resolve())
            or job.get("pythonhashseed") != str(seed)
            or not isinstance(job.get("training_environment"), dict)
            or not isinstance(command, list)
            or not all(isinstance(value, str) for value in command)
            or len(command) < 2
            or command[0] != contract.get("python_executable")
            or command[1] != contract.get("trainer")
        ):
            raise ValueError("v4 scaling job contract changed")
    roles = training_roles["roles"]
    if not isinstance(roles, dict) or set(roles) != set(MODEL_TRAINING_ROLES):
        raise ValueError("v4 scaling training-role contract changed")
    for role, details in roles.items():
        files = details.get("files") if isinstance(details, dict) else None
        if (
            set(details or {}) != {"opponents", "artifact_sha256", "files"}
            or not isinstance(details.get("opponents"), list)
            or not details["opponents"]
            or not _is_sha256(details.get("artifact_sha256"))
            or not isinstance(files, dict)
            or set(files)
            != {f"cf_{role}.jsonl", f"opponent_actions_{role}.jsonl"}
            or any(
                not isinstance(file_contract, dict)
                or set(file_contract) != {"rows", "bytes", "sha256"}
                or not isinstance(file_contract.get("rows"), int)
                or isinstance(file_contract.get("rows"), bool)
                or file_contract["rows"] < 1
                or not isinstance(file_contract.get("bytes"), int)
                or isinstance(file_contract.get("bytes"), bool)
                or file_contract["bytes"] < 1
                or not _is_sha256(file_contract.get("sha256"))
                for file_contract in files.values()
            )
        ):
            raise ValueError("v4 scaling training-role contract changed")
    return payload


def prepare_run_root(
    args: argparse.Namespace,
    *,
    root: Path,
    scales: list[str],
    encoders: list[str],
    seeds: list[int],
) -> dict[str, Any]:
    contract_path = root / RUN_CONTRACT_NAME
    if root.exists():
        if not args.resume:
            raise ValueError(f"output directory already exists: {root}")
        if root.is_symlink() or not root.is_dir():
            raise ValueError("v4 scaling resume root is not a real directory")
        existing = validate_run_contract(
            _load_json(contract_path, field="v4 scaling run contract")
        )
        expected = build_run_contract(
            args,
            root=root,
            scales=scales,
            encoders=encoders,
            seeds=seeds,
            created_at=str(existing["created_at"]),
        )
        if existing != expected:
            raise ValueError("v4 scaling resume arguments or provenance changed")
        return existing
    if args.resume:
        raise ValueError(f"v4 scaling resume root does not exist: {root}")
    root.parent.mkdir(parents=True, exist_ok=True)
    initialization = Path(tempfile.mkdtemp(
        prefix=f".{root.name}.init-", dir=root.parent
    ))
    try:
        contract = validate_run_contract(build_run_contract(
            args,
            root=root,
            scales=scales,
            encoders=encoders,
            seeds=seeds,
            created_at=datetime.now(timezone.utc).isoformat(),
        ))
        _write_json_atomic(initialization / RUN_CONTRACT_NAME, contract)
        os.replace(initialization, root)
        directory = os.open(root.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return contract
    finally:
        shutil.rmtree(initialization, ignore_errors=True)


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def quarantine_stale_training_temporaries(
    root: Path, *, slugs: set[str]
) -> None:
    quarantine = root.parent / f".{root.name}.abandoned-partials"
    moved = False
    for path in list(root.iterdir()):
        match = re.fullmatch(r"\.(.+)\.tmp-([0-9]+)", path.name)
        if match is None or match.group(1) not in slugs:
            continue
        pid = int(match.group(2))
        if path.is_symlink() or not path.is_dir():
            raise ValueError(f"invalid v4 training temporary: {path}")
        if _pid_is_running(pid):
            raise ValueError(f"v4 training temporary still has a live PID: {path}")
        quarantine.mkdir(exist_ok=True)
        destination = quarantine / (
            f"{path.name}.abandoned-{path.stat().st_mtime_ns}"
        )
        if destination.exists():
            raise ValueError(f"v4 training temporary quarantine collision: {path}")
        os.replace(path, destination)
        moved = True
    if moved:
        for directory_path in (root, quarantine, root.parent):
            directory = os.open(
                directory_path, os.O_RDONLY | os.O_DIRECTORY
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)


def quarantine_stale_metadata_temporaries(root: Path) -> None:
    prefixes = {
        f".{RUN_CONTRACT_NAME}.tmp-",
        f".{SUMMARY_NAME}.tmp-",
    }
    stale = [
        path
        for path in root.iterdir()
        if any(path.name.startswith(prefix) for prefix in prefixes)
    ]
    if not stale:
        return
    quarantine = root.parent / f".{root.name}.abandoned-partials"
    quarantine.mkdir(parents=True, exist_ok=True)
    for path in stale:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"invalid v4 metadata temporary: {path}")
        destination = quarantine / (
            f"{path.name}.abandoned-{path.stat().st_mtime_ns}"
        )
        if destination.exists():
            raise ValueError(f"v4 metadata quarantine collision: {destination}")
        os.replace(path, destination)
    for directory_path in (root, quarantine, root.parent):
        directory = os.open(directory_path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def quarantine_invalid_job(
    root: Path, *, output_dir: Path, reason: str
) -> Path:
    if output_dir.parent != root or output_dir.is_symlink() or not output_dir.is_dir():
        raise ValueError(f"cannot quarantine invalid v4 job output: {output_dir}")
    quarantine = root.parent / f".{root.name}.invalid-jobs"
    quarantine.mkdir(exist_ok=True)
    destination = quarantine / (
        f"{output_dir.name}.invalid-{output_dir.stat().st_mtime_ns}"
    )
    if destination.exists():
        destination = quarantine / (
            f"{destination.name}-{os.getpid()}"
        )
    if destination.exists():
        raise ValueError(f"v4 invalid-job quarantine collision: {output_dir}")
    os.replace(output_dir, destination)
    _write_json_atomic(destination / "QUARANTINE.json", {
        "schema": "opponent_multitask_v4_invalid_training_job_v1",
        "source": str(output_dir),
        "reason": str(reason),
        "authoritative": False,
        "deployment_policy_value": False,
        "strength_evidence": False,
    })
    for directory_path in (root, quarantine, root.parent):
        directory = os.open(directory_path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    return destination


def validate_run_root_entries(
    root: Path,
    *,
    jobs: list[tuple[str, str, int]],
    contract: dict[str, Any],
) -> None:
    slugs = {_slug(scale, encoder, seed) for scale, encoder, seed in jobs}
    quarantine_stale_training_temporaries(root, slugs=slugs)
    quarantine_stale_metadata_temporaries(root)
    allowed = {
        RUN_CONTRACT_NAME,
        SUMMARY_NAME,
        *(f"{slug}.log" for slug in slugs),
        *slugs,
    }
    for path in root.iterdir():
        if path.name not in allowed or path.is_symlink():
            raise ValueError(f"unexpected v4 scaling resume entry: {path}")
        if path.name in slugs and not path.is_dir():
            raise ValueError(f"v4 scaling job output is not a directory: {path}")
        if path.name not in slugs and not path.is_file():
            raise ValueError(f"v4 scaling metadata entry is not a file: {path}")
    summary_path = root / SUMMARY_NAME
    if summary_path.exists():
        summary = _load_json(summary_path, field="existing v4 scaling summary")
        if (
            summary.get("schema") != SUMMARY_SCHEMA
            or summary.get("run_contract_sha256")
            != contract.get("payload_sha256")
            or summary.get("requested") != contract.get("requested")
        ):
            raise ValueError("existing v4 scaling summary binding changed")


def final_verified_rows(
    args: argparse.Namespace,
    *,
    root: Path,
    jobs: list[tuple[str, str, int]],
    contract: dict[str, Any],
    ledger_snapshot: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    current = validate_run_contract(build_run_contract(
        args,
        root=root,
        scales=list(contract["requested"]["scales"]),
        encoders=list(contract["requested"]["encoders"]),
        seeds=[int(seed) for seed in contract["requested"]["seeds"]],
        created_at=str(contract["created_at"]),
    ))
    if current != contract:
        raise ValueError("v4 scaling contract changed before final publication")
    validate_run_root_entries(root, jobs=jobs, contract=contract)
    rows = []
    for scale, encoder, seed in jobs:
        output_dir = root / _slug(scale, encoder, seed)
        try:
            rows.append(validated_completed_row(
                args,
                root=root,
                scale=scale,
                encoder=encoder,
                seed=seed,
                contract=contract,
                ledger_snapshot=ledger_snapshot,
            ))
        except ProtectedExposureError:
            raise
        except Exception as exc:
            if output_dir.is_dir() and not output_dir.is_symlink():
                quarantine_invalid_job(
                    root, output_dir=output_dir, reason=str(exc)
                )
            raise ValueError(
                f"v4 scaling final job verification failed: {output_dir}"
            ) from exc
    return rows


def build_training_command(
    args: argparse.Namespace,
    *,
    scale: str,
    encoder: str,
    seed: int,
    output_dir: Path,
    run_id: str,
) -> list[str]:
    command = [
        sys.executable,
        str(TRAINER),
        "--role-manifest",
        str(args.role_manifest.resolve()),
        "--ledger",
        str(args.ledger.resolve()),
        "--run-id",
        run_id,
        "--out-dir",
        str(output_dir),
        "--scale",
        scale,
        "--cross-encoder",
        encoder,
        "--seed",
        str(seed),
    ]
    for flag, attribute in TRAINING_OPTION_SPECS:
        command.extend((flag, str(getattr(args, attribute))))
    if args.allow_incomplete_smoke:
        command.append("--allow-incomplete-smoke")
    return command


def _finite_selection_key(raw: Any) -> list[float]:
    if not isinstance(raw, list) or len(raw) != len(SELECTION_KEY_ORDER):
        raise ValueError("v4 training report has an invalid selection key")
    if any(isinstance(value, bool) for value in raw):
        raise ValueError("v4 training report has a boolean selection key")
    key = [float(value) for value in raw]
    if any(not math.isfinite(value) for value in key):
        raise ValueError("v4 training report has a non-finite selection key")
    return key


def validate_training_report(
    report: dict[str, Any],
    *,
    scale: str,
    encoder: str,
    seed: int,
    run_id: str,
    device: str,
    transformer_heads: int = 4,
) -> list[float]:
    config = report.get("config") or {}
    model = report.get("model") or {}
    early = report.get("early_stop") or {}
    environment = report.get("environment") or {}
    if (
        report.get("schema") != TRAINING_REPORT_SCHEMA
        or report.get("run_id") != run_id
        or report.get("opened_roles") != ["train", "early_stop"]
        or report.get("model_calibration_opened") is not False
        or report.get("policy_roles_opened") is not False
        or report.get("deployment_policy_value") is not False
        or report.get("strength_evidence") is not False
        or report.get("native_tcp_evaluated") is not False
        or model.get("format") != MODEL_FORMAT
        or model.get("scale") != scale
        or model.get("cross_encoder") != encoder
        or (
            encoder == "transformer"
            and (
                model.get("cross_transformer_heads") != transformer_heads
                or config.get("cross_transformer_heads") != transformer_heads
            )
        )
        or int(config.get("seed", -1)) != seed
        or environment.get("device") != device
        or early.get("selection_key_order") != list(SELECTION_KEY_ORDER)
        or early.get("selection_key_is_lexicographic") is not True
        or early.get("selection_score_is_strength_evidence") is not False
    ):
        raise ValueError("training report violates the v4 scaling role contract")
    return _finite_selection_key(early.get("selection_key"))


@contextmanager
def locked_exposure_ledger_snapshot(
    ledger_path: Path,
) -> Iterator[dict[str, Any]]:
    ledger_path = ledger_path.resolve()
    lock_path = ledger_path.with_name(f".{ledger_path.name}.lock")
    try:
        lock_handle = lock_path.open("a+", encoding="utf-8")
    except OSError as exc:
        raise ProtectedExposureError("training exposure ledger is invalid") from exc
    with lock_handle as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
        try:
            try:
                ledger = _load_json(
                    ledger_path, field="training exposure ledger"
                )
            except (OSError, TypeError, ValueError) as exc:
                raise ProtectedExposureError(
                    "training exposure ledger is invalid"
                ) from exc
            yield ledger
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def validate_training_job_exposures(
    ledger_path: Path,
    *,
    run_id: str,
    role_contracts: dict[str, Any],
    ledger_snapshot: dict[str, Any] | None = None,
) -> str:
    ledger_path = ledger_path.resolve()
    if ledger_snapshot is None:
        with locked_exposure_ledger_snapshot(ledger_path) as snapshot:
            return validate_training_job_exposures(
                ledger_path,
                run_id=run_id,
                role_contracts=role_contracts,
                ledger_snapshot=snapshot,
            )
    ledger = ledger_snapshot
    events = ledger.get("events")
    if (
        set(ledger) != {"schema", "events"}
        or ledger.get("schema") != EXPOSURE_LEDGER_SCHEMA
        or not isinstance(events, list)
    ):
        raise ProtectedExposureError("training exposure ledger is invalid")
    job_events = []
    for index, event in enumerate(events, start=1):
        opponents = event.get("opponents") if isinstance(event, dict) else None
        candidate_sha256 = (
            event.get("candidate_sha256") if isinstance(event, dict) else None
        )
        artifact_sha256 = (
            event.get("artifact_sha256") if isinstance(event, dict) else None
        )
        try:
            timestamp = datetime.fromisoformat(
                str(event.get("timestamp_utc"))
            ) if isinstance(event, dict) else None
        except ValueError:
            timestamp = None
        if (
            not isinstance(event, dict)
            or set(event) != EXPOSURE_EVENT_FIELDS
            or isinstance(event.get("sequence"), bool)
            or event.get("sequence") != index
            or not isinstance(event.get("timestamp_utc"), str)
            or not event["timestamp_utc"]
            or timestamp is None
            or timestamp.tzinfo is None
            or timestamp.utcoffset() != timezone.utc.utcoffset(timestamp)
            or event.get("event") not in {"open", "reserve", "release"}
            or event.get("role") not in EXPOSURE_ROLES
            or not isinstance(event.get("run_id"), str)
            or not event["run_id"].strip()
            or not isinstance(opponents, list)
            or not opponents
            or any(
                not isinstance(opponent, str) or not opponent.strip()
                for opponent in opponents
            )
            or opponents != sorted(set(opponents))
            or (
                candidate_sha256 is not None
                and not _is_sha256(candidate_sha256)
            )
            or (
                artifact_sha256 is not None
                and not _is_sha256(artifact_sha256)
            )
            or (
                event.get("event") in {"reserve", "release"}
                and event.get("role") != FINAL_BLIND_ROLE
            )
            or (
                event.get("event") == "reserve"
                and (
                    candidate_sha256 is None
                    or artifact_sha256 is not None
                )
            )
            or (
                event.get("event") == "release"
                and (
                    candidate_sha256 is not None
                    or artifact_sha256 is not None
                )
            )
            or (
                event.get("event") == "open"
                and event.get("role") == FINAL_BLIND_ROLE
                and (
                    candidate_sha256 is None
                    or artifact_sha256 is None
                )
            )
        ):
            raise ProtectedExposureError("training exposure ledger event is invalid")
        if event["run_id"] == run_id:
            job_events.append(event)
    if len(job_events) != len(MODEL_TRAINING_ROLES):
        raise ProtectedExposureError(
            "training job ledger exposure binding changed"
        )
    for event, role in zip(job_events, MODEL_TRAINING_ROLES, strict=True):
        details = role_contracts.get(role) or {}
        if (
            event.get("event") != "open"
            or event.get("role") != role
            or event.get("opponents") != sorted(details.get("opponents") or [])
            or event.get("candidate_sha256") is not None
            or event.get("artifact_sha256") != details.get("artifact_sha256")
        ):
            raise ProtectedExposureError(
                "training job ledger exposure binding changed"
            )
    return _canonical_sha256({
        "schema": "opponent_multitask_v4_training_exposure_receipt_v1",
        "run_id": run_id,
        "events": job_events,
    })


def _validate_job_exposures(
    args: argparse.Namespace,
    *,
    run_id: str,
    contract: dict[str, Any],
    ledger_snapshot: dict[str, Any] | None = None,
) -> str:
    if str(args.ledger.resolve()) != contract.get("ledger"):
        raise ProtectedExposureError("training exposure ledger path changed")
    role_contracts = (contract.get("training_roles") or {}).get("roles") or {}
    return validate_training_job_exposures(
        args.ledger,
        run_id=run_id,
        role_contracts=role_contracts,
        ledger_snapshot=ledger_snapshot,
    )


def validated_completed_row(
    args: argparse.Namespace,
    *,
    root: Path,
    scale: str,
    encoder: str,
    seed: int,
    contract: dict[str, Any],
    ledger_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    slug = _slug(scale, encoder, seed)
    run_id = f"{args.run_id_prefix}-{slug}"
    output_dir = root / slug
    if output_dir.is_symlink() or not output_dir.is_dir():
        raise ValueError(f"v4 scaling job output is incomplete: {output_dir}")
    expected_entries = {*TRAINING_FILES, "artifact_manifest.json"}
    actual_entries = {path.name for path in output_dir.iterdir()}
    if actual_entries != expected_entries or any(
        path.is_symlink() for path in output_dir.iterdir()
    ):
        raise ValueError("v4 scaling job directory contains unexpected artifacts")
    artifact = _load_json(
        output_dir / "artifact_manifest.json", field="v4 training artifact"
    )
    files = artifact.get("files")
    if (
        set(artifact) != TRAINING_ARTIFACT_FIELDS
        or artifact.get("schema") != TRAINING_ARTIFACT_SCHEMA
        or artifact.get("run_id") != run_id
        or not isinstance(files, dict)
        or set(files) != TRAINING_FILES
        or artifact.get("deployment_policy_value") is not False
        or artifact.get("strength_evidence") is not False
    ):
        raise ValueError("v4 training artifact manifest binding changed")
    for name, file_contract in files.items():
        path = output_dir / name
        if (
            not isinstance(file_contract, dict)
            or set(file_contract) != {"bytes", "sha256"}
            or isinstance(file_contract.get("bytes"), bool)
            or not isinstance(file_contract.get("bytes"), int)
            or path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != int(file_contract.get("bytes", -1))
            or _sha256(path) != file_contract.get("sha256")
        ):
            raise ValueError(f"v4 training artifact file changed: {path}")
    report = _load_json(
        output_dir / "training_report.json", field="v4 training report"
    )
    authorization = _load_json(
        output_dir / "checkpoint_authorization.json",
        field="v4 checkpoint authorization",
    )
    if set(report) != TRAINING_REPORT_FIELDS:
        raise ValueError("v4 training report fields changed")
    if set(authorization) != TRAINING_AUTHORIZATION_FIELDS:
        raise ValueError("v4 checkpoint authorization fields changed")
    checkpoint_path = output_dir / "checkpoint.pt"
    model, checkpoint = load_checkpoint(checkpoint_path, device="cpu")
    del model
    selection_key = validate_training_report(
        report,
        scale=scale,
        encoder=encoder,
        seed=seed,
        run_id=run_id,
        device=str(args.device),
        transformer_heads=int(args.cross_transformer_heads),
    )
    expected_command = build_training_command(
        args,
        scale=scale,
        encoder=encoder,
        seed=seed,
        output_dir=output_dir,
        run_id=run_id,
    )
    config_args = argparse.Namespace(**vars(args))
    config_args.scale = scale
    config_args.cross_encoder = encoder
    config_args.seed = seed
    expected_config = build_training_config(config_args)
    planned_jobs = [
        job for job in contract.get("jobs", [])
        if (
            job.get("scale") == scale
            and job.get("encoder") == encoder
            and job.get("seed") == seed
        )
    ]
    if len(planned_jobs) != 1:
        raise ValueError("v4 training job is absent from the run contract")
    planned_job = planned_jobs[0]
    role_contracts = (contract.get("training_roles") or {}).get("roles") or {}
    expected_training_artifacts = {
        role: details["artifact_sha256"]
        for role, details in role_contracts.items()
    }
    report_role_counts = report.get("role_counts") or {}
    expected_role_counts = {
        role: {
            "opponents": details["opponents"],
            "value": details["files"][f"cf_{role}.jsonl"]["rows"],
            "behavior": details["files"][
                f"opponent_actions_{role}.jsonl"
            ]["rows"],
            "provenance": {
                "artifact_sha256": details["artifact_sha256"],
                "manifest_sha256": contract["role_manifest_sha256"],
                "candidate_sha256": None,
            },
        }
        for role, details in role_contracts.items()
    }
    if (
        planned_job.get("command") != expected_command
        or report.get("command") != expected_command
        or report.get("environment") != planned_job.get("training_environment")
        or report.get("config") != expected_config
        or report.get("role_manifest") != str(args.role_manifest.resolve())
        or report.get("role_manifest_sha256")
        != contract.get("role_manifest_sha256")
        or report.get("ledger") != str(args.ledger.resolve())
        or report.get("code_artifacts")
        != contract.get("training_code_artifacts")
        or report.get("checkpoint_sha256") != _sha256(checkpoint_path)
        or report.get("checkpoint_authorization") != authorization
        or authorization.get("run_id") != run_id
        or authorization.get("schema") != FROZEN_CHECKPOINT_SCHEMA
        or authorization.get("frozen") is not True
        or authorization.get("early_stop_complete") is not True
        or authorization.get("training_roles") != list(MODEL_TRAINING_ROLES)
        or authorization.get("checkpoint_sha256")
        != report.get("checkpoint_sha256")
        or authorization.get("role_manifest_sha256")
        != contract.get("role_manifest_sha256")
        or authorization.get("training_artifact_sha256")
        != expected_training_artifacts
        or report_role_counts != expected_role_counts
        or artifact.get("source_collection_complete")
        is not report.get("source_collection_complete")
        or checkpoint.get("schema") != TRAINING_CHECKPOINT_SCHEMA
        or set(checkpoint) != TRAINING_CHECKPOINT_FIELDS
        or checkpoint.get("role_manifest_sha256")
        != report.get("role_manifest_sha256")
        or checkpoint.get("training_artifact_sha256")
        != expected_training_artifacts
        or checkpoint.get("model_metadata") != report.get("model")
        or checkpoint.get("training_config") != report.get("config")
        or checkpoint.get("training_data") != training_data_metadata()
        or checkpoint.get("best_epoch") != report.get("best_epoch")
        or checkpoint.get("code_artifacts") != report.get("code_artifacts")
        or checkpoint.get("training_environment")
        != report.get("environment")
        or checkpoint.get("source_completed_passes")
        != report.get("source_completed_passes")
        or checkpoint.get("source_requested_passes")
        != report.get("source_requested_passes")
        or checkpoint.get("source_collection_complete")
        is not report.get("source_collection_complete")
    ):
        raise ValueError("v4 training job provenance changed")
    forbidden = (
        "calibration.json",
        "outcome_calibration.json",
        "policy_selection_result.json",
        "policy_gate_result.json",
    )
    if any((output_dir / name).exists() for name in forbidden):
        raise ValueError("scaling run wrote a protected downstream artifact")
    training_exposure_sha256 = _validate_job_exposures(
        args,
        run_id=run_id,
        contract=contract,
        ledger_snapshot=ledger_snapshot,
    )
    return {
        "scale": scale,
        "encoder": encoder,
        "seed": seed,
        "slug": slug,
        "run_id": run_id,
        "output_dir": str(output_dir),
        "log": str(root / f"{slug}.log"),
        "returncode": 0,
        "completed": True,
        "selection_key": selection_key,
        "selection_key_order": list(SELECTION_KEY_ORDER),
        "best_epoch": int(report["best_epoch"]),
        "parameters": int(report["model"]["parameters"]),
        "checkpoint_sha256": report["checkpoint_sha256"],
        "role_manifest_sha256": report["role_manifest_sha256"],
        "source_collection_complete": report["source_collection_complete"],
        "source_completed_passes": report.get("source_completed_passes"),
        "source_requested_passes": report.get("source_requested_passes"),
        "incomplete_smoke": report["incomplete_smoke"],
        "training_device": str((report.get("environment") or {}).get("device")),
        "training_exposure_sha256": training_exposure_sha256,
        "cross_transformer_heads": (
            int(args.cross_transformer_heads)
            if encoder == "transformer"
            else None
        ),
    }


def _run_one(
    args: argparse.Namespace,
    *,
    root: Path,
    scale: str,
    encoder: str,
    seed: int,
    contract: dict[str, Any],
) -> dict[str, Any]:
    slug = _slug(scale, encoder, seed)
    run_id = f"{args.run_id_prefix}-{slug}"
    output_dir = root / slug
    command = build_training_command(
        args,
        scale=scale,
        encoder=encoder,
        seed=seed,
        output_dir=output_dir,
        run_id=run_id,
    )
    environment = dict(os.environ)
    environment["PYTHONHASHSEED"] = str(seed)
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )
    log_path = root / f"{slug}.log"
    log_path.write_text(
        "COMMAND\n"
        + json.dumps(command)
        + "\n\nSTDOUT\n"
        + result.stdout
        + "\nSTDERR\n"
        + result.stderr,
        encoding="utf-8",
    )
    row: dict[str, Any] = {
        "scale": scale,
        "encoder": encoder,
        "seed": seed,
        "slug": slug,
        "run_id": run_id,
        "output_dir": str(output_dir),
        "log": str(log_path),
        "returncode": result.returncode,
        "completed": False,
    }
    if result.returncode != 0:
        row["error"] = "trainer_failed"
        return row
    try:
        return validated_completed_row(
            args,
            root=root,
            scale=scale,
            encoder=encoder,
            seed=seed,
            contract=contract,
        )
    except ProtectedExposureError:
        raise
    except Exception as exc:
        if output_dir.is_dir() and not output_dir.is_symlink():
            quarantine_invalid_job(
                root, output_dir=output_dir, reason=str(exc)
            )
        row["error"] = f"invalid_training_artifact: {exc}"
        return row


def summarize_runs(
    rows: list[dict[str, Any]], *, required_seeds: list[int]
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((row["scale"], row["encoder"]), []).append(row)
    summaries = []
    for (scale, encoder), group in sorted(grouped.items()):
        complete = sorted(
            (row for row in group if row.get("completed") is True),
            key=lambda row: int(row["seed"]),
        )
        observed_seeds = [int(row["seed"]) for row in complete]
        keys = [row["selection_key"] for row in complete]
        parameter_values = {int(row["parameters"]) for row in complete}
        median_key = [
            statistics.median(key[index] for key in keys)
            for index in range(len(SELECTION_KEY_ORDER))
        ] if keys else None
        mean_key = [
            statistics.mean(key[index] for key in keys)
            for index in range(len(SELECTION_KEY_ORDER))
        ] if keys else None
        worst_key = [
            max(key[index] for key in keys)
            for index in range(len(SELECTION_KEY_ORDER))
        ] if keys else None
        summaries.append({
            "scale": scale,
            "encoder": encoder,
            "requested_seeds": sorted(required_seeds),
            "completed_seeds": observed_seeds,
            "all_seeds_completed": observed_seeds == sorted(required_seeds),
            "parameters_consistent": len(parameter_values) == 1,
            "parameters": (
                next(iter(parameter_values))
                if len(parameter_values) == 1
                else None
            ),
            "selection_key_order": list(SELECTION_KEY_ORDER),
            "median_selection_key": median_key,
            "mean_selection_key": mean_key,
            "worst_selection_key": worst_key,
        })
    eligible = [
        row for row in summaries
        if row["all_seeds_completed"] and row["parameters_consistent"]
    ]
    best = min(
        eligible,
        key=lambda row: (
            tuple(row["median_selection_key"]),
            tuple(row["worst_selection_key"]),
            int(row["parameters"]),
            row["scale"],
            row["encoder"],
        ),
        default=None,
    )
    return summaries, dict(best) if best is not None else None


def formal_selection_allowed(
    rows: list[dict[str, Any]],
    configurations: list[dict[str, Any]],
    best: dict[str, Any] | None,
    *,
    allow_incomplete_smoke: bool,
) -> bool:
    try:
        seeds = {int(row.get("seed")) for row in rows}
        jobs = [
            (
                str(row.get("scale")),
                str(row.get("encoder")),
                int(row.get("seed")),
            )
            for row in rows
        ]
        configuration_pairs = [
            (str(row.get("scale")), str(row.get("encoder")))
            for row in configurations
        ]
        configuration_seed_contracts = [
            (
                sorted(int(seed) for seed in row.get("requested_seeds", [])),
                sorted(int(seed) for seed in row.get("completed_seeds", [])),
            )
            for row in configurations
        ]
    except (TypeError, ValueError, OverflowError):
        return False
    expected_jobs = {
        (scale, encoder, seed)
        for scale in FORMAL_SCALES
        for encoder in FORMAL_ENCODERS
        for seed in seeds
    }
    expected_pairs = {
        (scale, encoder)
        for scale in FORMAL_SCALES
        for encoder in FORMAL_ENCODERS
    }
    return bool(
        not allow_incomplete_smoke
        and best is not None
        and len(seeds) >= 3
        and len(jobs) == len(expected_jobs)
        and set(jobs) == expected_jobs
        and len(configuration_pairs) == len(expected_pairs)
        and set(configuration_pairs) == expected_pairs
        and all(row.get("completed") is True for row in rows)
        and all(row.get("all_seeds_completed") is True for row in configurations)
        and all(row.get("parameters_consistent") is True for row in configurations)
        and all(
            requested == sorted(seeds) and completed == sorted(seeds)
            for requested, completed in configuration_seed_contracts
        )
        and all(row.get("source_collection_complete") is True for row in rows)
        and all(row.get("incomplete_smoke") is False for row in rows)
        and all(
            row.get("source_completed_passes") == 160
            and row.get("source_requested_passes") == 160
            for row in rows
        )
        and all(_is_cuda_device(row.get("training_device")) for row in rows)
    )


def _publish_scaling_result(
    args: argparse.Namespace,
    *,
    root: Path,
    seeds: list[int],
    contract: dict[str, Any],
    rows: list[dict[str, Any]],
) -> int:
    rows.sort(key=lambda row: (row["scale"], row["encoder"], row["seed"]))
    configurations, best = summarize_runs(rows, required_seeds=seeds)
    formal = formal_selection_allowed(
        rows,
        configurations,
        best,
        allow_incomplete_smoke=args.allow_incomplete_smoke,
    )
    summary = {
        "schema": SUMMARY_SCHEMA,
        "created_at": contract["created_at"],
        "role_manifest": str(args.role_manifest.resolve()),
        "ledger": str(args.ledger.resolve()),
        "requested": contract["requested"],
        "run_contract": contract,
        "run_contract_sha256": contract["payload_sha256"],
        "selection_key_order": list(SELECTION_KEY_ORDER),
        "selection_method": SELECTION_METHOD,
        "scaling_tool_sha256": contract["scaling_tool_sha256"],
        "runs": rows,
        "configurations": configurations,
        "selection_eligible": formal,
        "selected_configuration": best if formal else None,
        "provisional_best_configuration": best,
        "model_calibration_opened": False,
        "policy_roles_opened": False,
        "source_collection_complete": bool(
            rows and all(row.get("source_collection_complete") is True for row in rows)
        ),
        "incomplete_smoke": args.allow_incomplete_smoke,
        "deployment_policy_value": False,
        "strength_evidence": False,
        "native_tcp_evaluated": False,
    }
    _write_json_atomic(root / SUMMARY_NAME, summary)
    print(json.dumps({
        "out_dir": str(root),
        "runs": len(rows),
        "completed": sum(row.get("completed") is True for row in rows),
        "selection_eligible": formal,
        "selected_configuration": summary["selected_configuration"],
        "provisional_best_configuration": best,
        "strength_evidence": False,
    }, indent=2, sort_keys=True))
    completed = all(row.get("completed") is True for row in rows)
    return 0 if completed and (args.allow_incomplete_smoke or formal) else 1


def _execute_scaling_run(
    args: argparse.Namespace,
    *,
    root: Path,
    jobs: list[tuple[str, str, int]],
    seeds: list[int],
    contract: dict[str, Any],
    rows: list[dict[str, Any]],
    pending_jobs: list[tuple[str, str, int]],
) -> int:
    with ThreadPoolExecutor(max_workers=args.training_workers) as executor:
        futures = {
            executor.submit(
                _run_one,
                args,
                root=root,
                scale=scale,
                encoder=encoder,
                seed=seed,
                contract=contract,
            ): (scale, encoder, seed)
            for scale, encoder, seed in pending_jobs
        }
        for future in as_completed(futures):
            row = future.result()
            rows.append(row)
            print(
                f"[v4-scaling] {row['slug']} completed={row['completed']} "
                f"key={row.get('selection_key')}",
                flush=True,
            )
    completed = len(rows) == len(jobs) and all(
        row.get("completed") is True for row in rows
    )
    ledger_context = (
        locked_exposure_ledger_snapshot(args.ledger)
        if completed
        else nullcontext(None)
    )
    with ledger_context as ledger_snapshot:
        if completed:
            rows = final_verified_rows(
                args,
                root=root,
                jobs=jobs,
                contract=contract,
                ledger_snapshot=ledger_snapshot,
            )
        return _publish_scaling_result(
            args,
            root=root,
            seeds=seeds,
            contract=contract,
            rows=rows,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role-manifest", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--run-id-prefix", required=True)
    parser.add_argument("--scales", default="small,medium,large")
    parser.add_argument("--encoders", default=",".join(FORMAL_ENCODERS))
    parser.add_argument("--seeds", default="101,211,307")
    parser.add_argument("--training-workers", type=int, default=1)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--allow-incomplete-smoke", action="store_true")
    parser.add_argument("--moe-experts", type=int, default=4)
    parser.add_argument("--cross-transformer-heads", type=int, default=4)
    parser.add_argument("--dropout", type=float, default=0.10)
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--patience", type=int, default=15)
    parser.add_argument("--minimum-improvement", type=float, default=1.0e-5)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--learning-rate", type=float, default=5.0e-4)
    parser.add_argument("--weight-decay", type=float, default=1.0e-5)
    parser.add_argument("--gradient-clip-norm", type=float, default=1.0)
    parser.add_argument("--hand-clip", type=float, default=2_000.0)
    parser.add_argument("--tail-clip", type=float, default=2_000.0)
    parser.add_argument("--match-clip", type=float, default=2_000.0)
    parser.add_argument("--mean-loss-weight", type=float, default=1.0)
    parser.add_argument("--quantile-loss-weight", type=float, default=1.0)
    parser.add_argument("--match-ranking-weight", type=float, default=0.5)
    parser.add_argument("--match-q20-ranking-weight", type=float, default=0.25)
    parser.add_argument("--ranking-margin", type=float, default=100.0)
    parser.add_argument("--ranking-temperature", type=float, default=0.25)
    parser.add_argument("--outcome-loss-weight", type=float, default=2.0)
    parser.add_argument("--outcome-pairwise-weight", type=float, default=0.5)
    parser.add_argument("--outcome-pairwise-temperature", type=float, default=1.0)
    parser.add_argument("--response-loss-weight", type=float, default=1.0)
    parser.add_argument("--response-size-weight", type=float, default=0.25)
    parser.add_argument("--device", default="cuda")
    args = parser.parse_args(argv)
    try:
        scales = _csv_values(
            args.scales, choices=tuple(MODEL_SCALES), field="scales"
        )
        encoders = _csv_values(args.encoders, choices=ENCODERS, field="encoders")
        seeds = _seeds(args.seeds)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if not 1 <= args.training_workers <= 3:
        raise SystemExit("training-workers must be in [1, 3]")
    if "transformer" in encoders and (
        args.cross_transformer_heads <= 0
        or any(
            MODEL_SCALES[scale]["cross_hidden"]
            % args.cross_transformer_heads
            for scale in scales
        )
    ):
        raise SystemExit(
            "cross-transformer-heads must positively divide every selected scale"
        )
    if not args.allow_incomplete_smoke and (
        len(seeds) < 3
        or set(scales) != set(FORMAL_SCALES)
        or set(encoders) != set(FORMAL_ENCODERS)
        or not _is_cuda_device(args.device)
    ):
        raise SystemExit(
            "formal v4 scaling requires three seeds, all model scales, all of "
            "deep_set/gru/gru_moe/transformer, and --device cuda"
        )
    root = args.out_dir.resolve()
    jobs = [
        (scale, encoder, seed)
        for scale in scales
        for encoder in encoders
        for seed in seeds
    ]
    run_lock = None
    try:
        run_lock = acquire_run_lock(root)
        contract = prepare_run_root(
            args,
            root=root,
            scales=scales,
            encoders=encoders,
            seeds=seeds,
        )
        validate_run_root_entries(root, jobs=jobs, contract=contract)
        rows = []
        pending_jobs = []
        for scale, encoder, seed in jobs:
            output_dir = root / _slug(scale, encoder, seed)
            if output_dir.exists():
                try:
                    row = validated_completed_row(
                        args,
                        root=root,
                        scale=scale,
                        encoder=encoder,
                        seed=seed,
                        contract=contract,
                    )
                except ProtectedExposureError:
                    raise
                except Exception as exc:
                    destination = quarantine_invalid_job(
                        root, output_dir=output_dir, reason=str(exc)
                    )
                    pending_jobs.append((scale, encoder, seed))
                    print(
                        f"[v4-scaling] {_slug(scale, encoder, seed)} "
                        f"reused=false quarantined={destination}",
                        flush=True,
                    )
                else:
                    rows.append(row)
                    print(
                        f"[v4-scaling] {row['slug']} reused=true "
                        f"key={row.get('selection_key')}",
                        flush=True,
                    )
            else:
                pending_jobs.append((scale, encoder, seed))
        return _execute_scaling_run(
            args,
            root=root,
            jobs=jobs,
            seeds=seeds,
            contract=contract,
            rows=rows,
            pending_jobs=pending_jobs,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        if run_lock is not None:
            run_lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
