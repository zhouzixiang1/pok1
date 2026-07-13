"""Deterministic Leduc-LCFR seed training and national projection export.

LCFR training is paper-faithful at the algorithm level.  Projecting its small-
game policy into versioned national abstractions is a functional adaptation,
not a reconstruction of DecisionHoldem's missing blueprint assets.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any, Mapping

from ..common_runtime.leduc import LeducStrategy
from .a2_runtime import (
    ACTION_ABSTRACTION_VERSION,
    ACTION_IDS,
    BLUEPRINT_ALGORITHM,
    BLUEPRINT_FIDELITY,
    BLUEPRINT_SCHEMA,
    BLUEPRINT_SOURCE_GAME,
    HAND_ABSTRACTION_VERSION,
    MADE_NAMES,
    STREETS,
    SparseBlueprint,
    canonical_digest,
    information_key,
)
from .leduc_linear_cfr import LeducLinearCFR


TRAINING_CHECKPOINT_SCHEMA = "route-a2-blueprint-training-v1"
TRAINING_CHECKPOINT_FIDELITY = {
    "lcfr_kernel": "paper-faithful-clean-room-small-game",
    "national_projection": "functional-adaptation-not-decisionholdem-blueprint",
}
EXPORT_MANIFEST_SCHEMA = "route-a2-blueprint-export-manifest-v1"
BLUEPRINT_FILENAME = "blueprint.json"
EXPORT_FILES = ("national_bot.py", "a2_runtime.py", BLUEPRINT_FILENAME)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if hasattr(os, "O_DIRECTORY"):
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _normalize_policy(values: Mapping[str, float]) -> dict[str, float]:
    normalized = {
        action: max(0.0, float(values.get(action, 0.0))) for action in ACTION_IDS
    }
    total = sum(normalized.values())
    if not math.isfinite(total) or total <= 0.0:
        raise ValueError("projected blueprint policy has no finite probability")
    result = {action: value / total for action, value in normalized.items() if value > 0.0}
    correction = 1.0 - sum(result.values())
    last = next(reversed(result))
    result[last] += correction
    return result


def _leduc_action_summary(
    profile: LeducStrategy,
    private_rank: int,
    facing: str,
) -> dict[str, float]:
    rows: list[Mapping[str, float]] = []
    for key, policy in profile.items():
        if key[1] != private_rank:
            continue
        is_facing = "fold" in policy
        if (facing == "raise") == is_facing:
            rows.append(policy)
    if not rows:
        raise ValueError("Leduc profile has no projection rows")
    actions = {action for row in rows for action in row}
    return {
        action: sum(row.get(action, 0.0) for row in rows) / len(rows)
        for action in actions
    }


def _spread_aggression(probability: float) -> dict[str, float]:
    weights = {
        "exact_min": 0.35,
        "0.5p": 0.30,
        "1p": 0.20,
        "1.5p": 0.10,
        "allin": 0.05,
    }
    return {action: probability * weight for action, weight in weights.items()}


def _project_policy(
    profile: LeducStrategy,
    private_rank: int,
    facing: str,
) -> dict[str, float]:
    if facing in {"none", "check"}:
        row = _leduc_action_summary(profile, private_rank, "none")
        result = {"check_call": row.get("check", 0.0)}
        result.update(_spread_aggression(row.get("raise", 0.0)))
        return _normalize_policy(result)
    row = _leduc_action_summary(profile, private_rank, "raise")
    if facing == "allin":
        return _normalize_policy(
            {"fold": row.get("fold", 0.0), "check_call": row.get("call", 0.0)}
        )
    result = {
        "fold": row.get("fold", 0.0),
        "check_call": row.get("call", 0.0),
    }
    result.update(_spread_aggression(row.get("raise", 0.0)))
    return _normalize_policy(result)


def _projection_rank(bucket: str) -> int:
    if bucket in {"tier:weak", "made:high_card"}:
        return 0
    if bucket in {"tier:medium", "made:pair"}:
        return 1
    return 2


def build_sparse_blueprint_payload(solver: LeducLinearCFR) -> dict[str, Any]:
    if solver.iterations_completed <= 0:
        raise ValueError("blueprint export requires at least one LCFR iteration")
    profile = solver.average_strategy()
    policies: dict[str, dict[str, float]] = {}
    facings = ("none", "check", "raise", "allin")

    for street in STREETS:
        for facing in facings:
            policies[
                information_key(
                    street=street,
                    hand="*",
                    position="*",
                    facing=facing,
                    raises="*",
                )
            ] = _project_policy(profile, 1, facing)

    for bucket in ("tier:weak", "tier:medium", "tier:strong", "tier:premium"):
        for facing in facings:
            policies[
                information_key(
                    street="preflop",
                    hand=bucket,
                    position="*",
                    facing=facing,
                    raises="*",
                )
            ] = _project_policy(profile, _projection_rank(bucket), facing)

    for street in ("flop", "turn", "river"):
        for name in MADE_NAMES:
            bucket = f"made:{name}"
            for facing in facings:
                policies[
                    information_key(
                        street=street,
                        hand=bucket,
                        position="*",
                        facing=facing,
                        raises="*",
                    )
                ] = _project_policy(profile, _projection_rank(bucket), facing)

    payload = {
        "schema": BLUEPRINT_SCHEMA,
        "algorithm": BLUEPRINT_ALGORITHM,
        "source_game": BLUEPRINT_SOURCE_GAME,
        "iterations_completed": solver.iterations_completed,
        "training_checkpoint_digest": solver.checkpoint_digest(),
        "hand_abstraction": HAND_ABSTRACTION_VERSION,
        "action_abstraction": ACTION_ABSTRACTION_VERSION,
        "fidelity": dict(BLUEPRINT_FIDELITY),
        "policies": dict(sorted(policies.items())),
    }
    SparseBlueprint(payload)
    return payload


class BlueprintTrainer:
    def __init__(self, solver: LeducLinearCFR | None = None) -> None:
        self.solver = solver or LeducLinearCFR()

    @property
    def iterations_completed(self) -> int:
        return self.solver.iterations_completed

    def train_to(self, target_iterations: int) -> None:
        if type(target_iterations) is not int or target_iterations < 1:
            raise ValueError("target_iterations must be a positive integer")
        if target_iterations < self.iterations_completed:
            raise ValueError("cannot train backwards from a resumed checkpoint")
        self.solver.train(target_iterations - self.iterations_completed)

    def checkpoint_payload(self) -> dict[str, Any]:
        return {
            "schema": TRAINING_CHECKPOINT_SCHEMA,
            "fidelity": dict(TRAINING_CHECKPOINT_FIDELITY),
            "solver": self.solver.checkpoint_payload(),
        }

    def checkpoint_digest(self) -> str:
        return canonical_digest(self.checkpoint_payload())

    def save_checkpoint(self, path: str | Path) -> None:
        rendered = json.dumps(
            self.checkpoint_payload(),
            indent=2,
            sort_keys=True,
        ).encode("utf-8") + b"\n"
        _atomic_write(Path(path), rendered)

    @classmethod
    def load_checkpoint(cls, path: str | Path) -> "BlueprintTrainer":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if payload.get("schema") != TRAINING_CHECKPOINT_SCHEMA:
            raise ValueError("unsupported blueprint training checkpoint")
        if payload.get("fidelity") != TRAINING_CHECKPOINT_FIDELITY:
            raise ValueError("blueprint checkpoint fidelity boundary differs")
        solver_payload = payload.get("solver")
        if not isinstance(solver_payload, dict):
            raise ValueError("blueprint checkpoint lacks its LCFR solver")
        descriptor, temporary = tempfile.mkstemp(prefix="route-a2-resume-", suffix=".json")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(solver_payload, handle, sort_keys=True)
            solver = LeducLinearCFR.load_checkpoint(temporary)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        return cls(solver)

    def blueprint_payload(self) -> dict[str, Any]:
        return build_sparse_blueprint_payload(self.solver)


def _source_files() -> dict[str, bytes]:
    package = Path(__file__).resolve().parent
    return {
        "national_bot.py": (package / "native_entry.py").read_bytes(),
        "a2_runtime.py": (package / "a2_runtime.py").read_bytes(),
    }


def export_blueprint_atomic(
    trainer: BlueprintTrainer,
    destination: str | Path,
) -> dict[str, Any]:
    target = Path(destination)
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"export destination already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent)
    )
    try:
        sources = _source_files()
        payload = trainer.blueprint_payload()
        rendered_blueprint = json.dumps(
            payload,
            indent=2,
            sort_keys=True,
        ).encode("utf-8") + b"\n"
        files = sources | {BLUEPRINT_FILENAME: rendered_blueprint}
        for name, data in files.items():
            path = temporary / name
            _atomic_write(path, data)
            path.chmod(0o755 if name == "national_bot.py" else 0o644)
        manifest = {
            "schema": EXPORT_MANIFEST_SCHEMA,
            "entrypoint": "national_bot.py",
            "blueprint": BLUEPRINT_FILENAME,
            "blueprint_digest": canonical_digest(payload),
            "hand_abstraction": HAND_ABSTRACTION_VERSION,
            "action_abstraction": ACTION_ABSTRACTION_VERSION,
            "fidelity": payload["fidelity"],
            "files": {
                name: {
                    "sha256": _sha256_bytes(data),
                    "bytes": len(data),
                    "mode": 0o755 if name == "national_bot.py" else 0o644,
                }
                for name, data in sorted(files.items())
            },
        }
        _atomic_write(
            temporary / "manifest.json",
            json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n",
        )
        os.replace(temporary, target)
        if hasattr(os, "O_DIRECTORY"):
            directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return manifest
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def verify_blueprint_export(root: str | Path) -> dict[str, Any]:
    target = Path(root)
    metadata = target.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("blueprint export root must be a real directory")
    expected_names = set(EXPORT_FILES) | {"manifest.json"}
    actual_names = {path.name for path in target.iterdir()}
    if actual_names != expected_names:
        raise ValueError("blueprint export file set differs from its schema")
    manifest_path = target / "manifest.json"
    manifest_info = manifest_path.lstat()
    if stat.S_ISLNK(manifest_info.st_mode) or not stat.S_ISREG(manifest_info.st_mode):
        raise ValueError("blueprint export manifest must be a real regular file")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != EXPORT_MANIFEST_SCHEMA:
        raise ValueError("blueprint export manifest schema mismatch")
    if manifest.get("entrypoint") != "national_bot.py":
        raise ValueError("blueprint export entrypoint mismatch")
    if manifest.get("blueprint") != BLUEPRINT_FILENAME:
        raise ValueError("blueprint export filename mismatch")
    recorded_files = manifest.get("files")
    if not isinstance(recorded_files, dict) or set(recorded_files) != set(EXPORT_FILES):
        raise ValueError("blueprint export manifest file set differs")
    for name in EXPORT_FILES:
        path = target / name
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ValueError(f"blueprint export contains a non-regular file: {name}")
        recorded = recorded_files.get(name)
        data = path.read_bytes()
        expected = {
            "sha256": _sha256_bytes(data),
            "bytes": len(data),
            "mode": stat.S_IMODE(info.st_mode),
        }
        if recorded != expected:
            raise ValueError(f"blueprint export file binding differs: {name}")
    blueprint = SparseBlueprint.load(target / BLUEPRINT_FILENAME)
    if blueprint.digest != manifest.get("blueprint_digest"):
        raise ValueError("blueprint digest differs from export manifest")
    if manifest.get("hand_abstraction") != HAND_ABSTRACTION_VERSION:
        raise ValueError("export hand abstraction differs")
    if manifest.get("action_abstraction") != ACTION_ABSTRACTION_VERSION:
        raise ValueError("export action abstraction differs")
    if manifest.get("fidelity") != blueprint.payload.get("fidelity"):
        raise ValueError("export fidelity boundary differs from blueprint")
    return manifest
