"""Identity and migration boundary for authoritative national rating data."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any, Iterator

from bot_artifact import canonical_digest
from bot_namespace import EVALUATION_EPOCH
from workflow_profiles import get_workflow_profile


ROOT = Path(__file__).resolve().parents[2]
MANIFEST_NAME = "evaluation_data_manifest.json"
IDENTITY_SCHEMA_VERSION = 1
PROFILE_ID = "national-native-rating-authority-v2-current-runtime-overlay"
SEMANTIC_PATHS = (
    "sever/engine/deck.py",
    "sever/engine/evaluator.py",
    "sever/engine/game.py",
    "sever/engine/validator.py",
    "sever/server/protocol.py",
    "web/core/glicko2.py",
    "web/core/elo_daemon.py",
    "web/core/evolution_infra.py",
    "web/core/national_bot_launcher.py",
    "web/core/national_game_runtime.py",
    "web/core/national_native.py",
    "web/core/national_transport.py",
    "web/core/rating_snapshot.py",
    "web/core/strength_order.py",
)
AUTHORITATIVE_FILES = (
    "glicko_ratings.json",
    "head_to_head.json",
    "bot_stats.json",
    "daemon_stats.json",
    "match_history.jsonl",
    "rating_history.jsonl",
)
GENERATION_DIR_RE = re.compile(r"v[0-9]+")
GENERATION_EVIDENCE_DIR = "evidence_snapshot"


class EvaluationDataIdentityError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def base_evaluation_identity() -> dict[str, Any]:
    profile = get_workflow_profile()
    native_strength_runtime_overlay: dict[str, Any] = {}
    if str(getattr(profile, "national_execution_mode", "")) == "native_tcp":
        from national_native import current_strength_runtime_overlay_identity

        native_strength_runtime_overlay = current_strength_runtime_overlay_identity()
    payload = {
        "schema_version": IDENTITY_SCHEMA_VERSION,
        "profile_id": PROFILE_ID,
        "evaluation_epoch": EVALUATION_EPOCH,
        "authority": "rating_daemon_only",
        "protocol": str(getattr(profile, "rating_protocol", "")),
        "national_execution_mode": str(
            getattr(profile, "national_execution_mode", "")
        ),
        "national_hands": int(getattr(profile, "national_rating_hands", 70)),
        "official_exe_strength_weight": 0,
        "native_strength_runtime_overlay": native_strength_runtime_overlay,
        "semantic_files": {
            path: _sha256(ROOT / path) if (ROOT / path).is_file() else "missing"
            for path in SEMANTIC_PATHS
        },
    }
    return {**payload, "identity_digest": canonical_digest(payload)}


def manifest_path(results_dir: str | Path) -> Path:
    return Path(results_dir) / MANIFEST_NAME


@contextmanager
def _manifest_lock(results_dir: Path) -> Iterator[None]:
    results_dir.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(results_dir / f".{MANIFEST_NAME}.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _has_authoritative_payload(results_dir: Path) -> bool:
    return any(
        (results_dir / name).is_file() and (results_dir / name).stat().st_size > 0
        for name in AUTHORITATIVE_FILES
    )


def ensure_evaluation_data_identity(
    results_dir: str | Path,
    *,
    runtime_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = Path(results_dir)
    expected = base_evaluation_identity()
    path = manifest_path(root)
    with _manifest_lock(root):
        if not path.exists():
            if _has_authoritative_payload(root):
                raise EvaluationDataIdentityError(
                    "authoritative rating data has no evaluation identity; "
                    "archive it with scripts/evaluation_data_identity.py before restart"
                )
            payload = {
                "schema_version": IDENTITY_SCHEMA_VERSION,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "base_identity": expected,
                "runtime_profile": runtime_profile,
            }
            payload["manifest_digest"] = canonical_digest(payload)
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(path)
            return payload
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise EvaluationDataIdentityError(
                f"evaluation identity manifest is unreadable: {type(exc).__name__}"
            ) from exc
        digest = str(payload.get("manifest_digest") or "")
        expected_digest = canonical_digest({
            key: value for key, value in payload.items() if key != "manifest_digest"
        })
        if digest != expected_digest:
            raise EvaluationDataIdentityError("evaluation identity manifest digest mismatch")
        if payload.get("base_identity") != expected:
            raise EvaluationDataIdentityError(
                "authoritative rating evaluator identity changed; archive and restart ratings"
            )
        if runtime_profile is not None:
            recorded = payload.get("runtime_profile")
            if recorded is None:
                payload["runtime_profile"] = runtime_profile
                payload["manifest_digest"] = canonical_digest({
                    key: value for key, value in payload.items() if key != "manifest_digest"
                })
                path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            elif recorded != runtime_profile:
                raise EvaluationDataIdentityError(
                    "rating daemon runtime profile changed; archive and restart ratings"
                )
        return payload


def current_evaluation_digest(results_dir: str | Path) -> str:
    return str(ensure_evaluation_data_identity(results_dir).get("manifest_digest") or "")


def _generation_evidence_snapshots(results_dir: Path) -> list[Path]:
    """Return safe, identity-bound generation snapshots below ``results_dir``.

    H2H snapshots are derived from the authoritative rating payload and embed
    its manifest digest.  They therefore belong to the same explicit migration
    boundary as the top-level rating files.  Validate the complete set before
    moving anything so a symlink or malformed path cannot make a partial
    migration follow data outside the results directory.
    """
    snapshots: list[Path] = []
    for version_dir in results_dir.iterdir():
        if not GENERATION_DIR_RE.fullmatch(version_dir.name):
            continue
        if version_dir.is_symlink():
            raise EvaluationDataIdentityError(
                f"unsafe generation results path during evaluator migration: {version_dir.name}"
            )
        if not version_dir.is_dir():
            continue
        snapshot = version_dir / GENERATION_EVIDENCE_DIR
        if snapshot.is_symlink():
            raise EvaluationDataIdentityError(
                "unsafe generation evidence snapshot during evaluator migration: "
                f"{version_dir.name}/{GENERATION_EVIDENCE_DIR}"
            )
        if snapshot.exists() and not snapshot.is_dir():
            raise EvaluationDataIdentityError(
                "invalid generation evidence snapshot during evaluator migration: "
                f"{version_dir.name}/{GENERATION_EVIDENCE_DIR}"
            )
        if snapshot.is_dir():
            snapshots.append(snapshot)
    return sorted(snapshots, key=lambda path: int(path.parent.name[1:]))


def archive_and_initialize(
    results_dir: str | Path,
    *,
    reason: str,
) -> dict[str, Any]:
    root = Path(results_dir)
    # Resolve/import every evaluator identity dependency before moving any
    # authoritative payload.  A broken CLI environment must fail while the old
    # ratings are still intact, rather than leaving a complete archive but no
    # replacement manifest.
    base_evaluation_identity()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = root / "archive" / "evaluation_identity" / timestamp
    with _manifest_lock(root):
        generation_snapshots = _generation_evidence_snapshots(root)
        destination.mkdir(parents=True, exist_ok=False)
        moved: list[str] = []
        for name in (*AUTHORITATIVE_FILES, MANIFEST_NAME):
            source = root / name
            if source.exists():
                shutil.move(str(source), str(destination / name))
                moved.append(name)
        for snapshot in generation_snapshots:
            relative = snapshot.relative_to(root)
            archived = destination / "generation_snapshots" / relative
            archived.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(snapshot), str(archived))
            moved.append(relative.as_posix())
        (destination / "migration.json").write_text(
            json.dumps({
                "archived_at": datetime.now().isoformat(timespec="seconds"),
                "reason": reason,
                "moved": moved,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    manifest = ensure_evaluation_data_identity(root)
    return {
        "archive_dir": str(destination),
        "moved": moved,
        "manifest": manifest,
    }
