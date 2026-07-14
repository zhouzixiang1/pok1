#!/usr/bin/env python3
"""Audit and atomically recover one complete legacy collector tail.

This tool is deliberately narrower than the collector.  It never launches a
probe and never reads the current live ratings file.  It upgrades an already
complete, uncommitted schema-4 pass using the exact ratings bytes archived by
an evaluation-identity migration, then makes the corpus resumable by the
schema-5 collector.

The default mode is read-only.  ``--apply`` is additionally guarded by an
exclusive collector lock, reviewed content hashes, a poison manifest that
makes both collector generations fail closed during publication, and a
durable rollback journal.

The reviewed expectations document has this shape (all hashes are required):

``schema_version``
    ``1``.
``completed_pass`` / ``recovery_pass``
    Adjacent pass numbers, for example ``75`` and ``76``.
``hashes``
    Exact SHA-256 values for the legacy manifest/state/snapshots/recovery plan,
    every cumulative JSONL, the completed opponent registry, both collector
    code roots, and the archived ratings/migration receipt.
``completed_plan_sha256``
    The exact filename-to-SHA mapping for every plan in the atomic prefix.
``tasks``
    The reviewed split plus exact value/behavior tail count for every recovery
    task.  The tool independently reconstructs and binds every row key.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


TOOLS = Path(__file__).resolve().parent
_COLLECTOR_PATH = TOOLS / "longrun_collect_oppmodel.py"
_COLLECTOR_SPEC = importlib.util.spec_from_file_location(
    "_oppmodel_recovery_current_collector", _COLLECTOR_PATH
)
if _COLLECTOR_SPEC is None or _COLLECTOR_SPEC.loader is None:
    raise RuntimeError(f"cannot load current collector: {_COLLECTOR_PATH}")
collector = importlib.util.module_from_spec(_COLLECTOR_SPEC)
_COLLECTOR_SPEC.loader.exec_module(collector)


EXPECTATIONS_SCHEMA_VERSION = 1
RECOVERY_RECEIPT_SCHEMA_VERSION = 1
TRANSACTION_SCHEMA_VERSION = 1
LEGACY_COLLECTION_SCHEMA_VERSION = 4
TARGET_COLLECTION_SCHEMA_VERSION = 5
TARGET_PASS_PLAN_SCHEMA_VERSION = 2
TRANSACTION_DIRNAME = ".legacy_oppmodel_recovery_transaction"
SOURCE_SPLITS = ("train", "val", "held_out")
DATA_FILES = {
    "value": {split: f"cf_{split}.jsonl" for split in SOURCE_SPLITS},
    "behavior": {
        split: f"opponent_actions_{split}.jsonl" for split in SOURCE_SPLITS
    },
}
LEGACY_CONTRACT_FIELDS = frozenset({
    "schema_version",
    "candidate",
    "candidate_sha256",
    "candidate_execution_path",
    "candidate_snapshot_sha256",
    "ratings_path",
    "workers",
    "probe_workers",
    "hands",
    "timeout_sec",
    "strongest",
    "val_opponents",
    "held_out_opponents",
    "opponents_per_pass",
    "max_decisions",
    "max_alternatives",
    "decision_sampling",
    "hand_windows",
    "deck_seed_scheme",
    "deck_seed_base",
    "deck_seed_guard",
    "deck_seed_slots_per_pass",
    "bot_seed_base",
    "collector_sha256",
    "probe_sha256",
    "cross_hand_sequence_sha256",
})
PUBLISH_TARGETS = (
    "pass_plan",
    "pool_snapshots",
    "collector_state",
    "collection_manifest",
)


class RecoveryError(RuntimeError):
    """The legacy corpus cannot be proven safe to recover."""


@dataclass(frozen=True)
class ArtifactSnapshot:
    path: Path
    raw: bytes
    sha256: str


@dataclass
class AuditResult:
    source_dir: Path
    expectations: dict[str, Any]
    manifest: dict[str, Any]
    manifest_snapshot: ArtifactSnapshot
    state: dict[str, Any]
    state_snapshot: ArtifactSnapshot
    snapshots: list[dict[str, Any]]
    snapshots_snapshot: ArtifactSnapshot
    legacy_plan: dict[str, Any]
    plan_snapshot: ArtifactSnapshot
    upgraded_plan: dict[str, Any]
    upgraded_plan_bytes: bytes
    ratings_snapshot: dict[str, Any]
    rating_rows: dict[str, dict[str, float]]
    tail_evidence: dict[str, Any]
    actual_totals: dict[str, dict[str, int]]
    current_contract: dict[str, Any]
    legacy_collector_sha256: str
    current_collector_sha256: str
    archived_ratings_sha256: str
    identity_migration_sha256: str


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _pretty_json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")


def _require_regular_bytes(path: Path) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise RecoveryError(f"required file is unavailable: {path}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise RecoveryError(f"required path is not a regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RecoveryError(f"cannot open required file: {path}") from exc
    try:
        with os.fdopen(fd, "rb", closefd=True) as handle:
            raw = handle.read()
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise RecoveryError(f"cannot read required file: {path}") from exc
    if (before.st_dev, before.st_ino, before.st_size) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
    ):
        raise RecoveryError(f"file changed while it was read: {path}")
    return raw


def _snapshot(path: Path) -> ArtifactSnapshot:
    raw = _require_regular_bytes(path)
    return ArtifactSnapshot(path=path, raw=raw, sha256=_sha256(raw))


def _load_json_snapshot(path: Path) -> tuple[dict[str, Any], ArtifactSnapshot]:
    snap = _snapshot(path)
    try:
        payload = json.loads(snap.raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecoveryError(f"invalid JSON file: {path}") from exc
    if not isinstance(payload, dict):
        raise RecoveryError(f"JSON root must be an object: {path}")
    return payload, snap


def _require_int(value: object, *, field: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RecoveryError(f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise RecoveryError(f"{field} must be >= {minimum}")
    return value


def _require_digest(value: object, *, field: str) -> str:
    text = str(value or "")
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise RecoveryError(f"{field} must be a lowercase SHA-256 digest")
    return text


def _expected_hash(expectations: dict[str, Any], name: str) -> str:
    hashes = expectations.get("hashes")
    if not isinstance(hashes, dict) or name not in hashes:
        raise RecoveryError(f"expectations are missing hash: {name}")
    return _require_digest(hashes[name], field=f"hashes.{name}")


def _assert_expected_hash(
    expectations: dict[str, Any], name: str, actual: str
) -> None:
    expected = _expected_hash(expectations, name)
    if actual != expected:
        raise RecoveryError(
            f"reviewed hash changed for {name}: expected={expected} actual={actual}"
        )


def _parse_jsonl(raw: bytes, *, path: Path) -> list[dict[str, Any]]:
    if raw and not raw.endswith(b"\n"):
        raise RecoveryError(f"JSONL file has an unterminated tail: {path}")
    rows: list[dict[str, Any]] = []
    for index, line in enumerate(raw.splitlines(), start=1):
        if not line:
            raise RecoveryError(f"blank JSONL row at {path}:{index}")
        try:
            row = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RecoveryError(f"invalid JSONL row at {path}:{index}") from exc
        if not isinstance(row, dict):
            raise RecoveryError(f"JSONL row is not an object at {path}:{index}")
        rows.append(row)
    return rows


def _visit_jsonl(
    path: Path,
    visitor,
) -> tuple[int, str]:
    """Stream a stable regular JSONL file without materializing the corpus."""
    try:
        before = path.lstat()
    except OSError as exc:
        raise RecoveryError(f"required JSONL file is unavailable: {path}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise RecoveryError(f"required JSONL path is not a regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RecoveryError(f"cannot open JSONL file: {path}") from exc
    digest = hashlib.sha256()
    count = 0
    try:
        with os.fdopen(fd, "rb", closefd=True) as handle:
            for line in handle:
                digest.update(line)
                if not line.endswith(b"\n"):
                    raise RecoveryError(f"JSONL file has an unterminated tail: {path}")
                encoded = line[:-1]
                if not encoded:
                    raise RecoveryError(f"blank JSONL row at {path}:{count + 1}")
                try:
                    row = json.loads(encoded.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise RecoveryError(
                        f"invalid JSONL row at {path}:{count + 1}"
                    ) from exc
                if not isinstance(row, dict):
                    raise RecoveryError(
                        f"JSONL row is not an object at {path}:{count + 1}"
                    )
                visitor(count, row)
                count += 1
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise RecoveryError(f"cannot stream JSONL file: {path}") from exc
    try:
        live = path.stat()
    except OSError as exc:
        raise RecoveryError(f"JSONL file disappeared while read: {path}") from exc
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise RecoveryError(f"JSONL file changed while read: {path}")
    if identity != (live.st_dev, live.st_ino, live.st_size, live.st_mtime_ns):
        raise RecoveryError(f"JSONL path changed while read: {path}")
    return count, digest.hexdigest()


@contextlib.contextmanager
def _collector_lock(source_dir: Path, *, writable: bool) -> Iterator[None]:
    path = source_dir / ".collector.lock"
    flags = (os.O_RDWR if writable else os.O_RDONLY) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RecoveryError(f"collector lock is unavailable: {path}") from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RecoveryError(f"collector is already running for {source_dir}") from exc
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _validate_expectations(payload: dict[str, Any]) -> dict[str, Any]:
    if (
        collector.COLLECTION_CONTRACT_SCHEMA_VERSION
        != TARGET_COLLECTION_SCHEMA_VERSION
        or collector.PASS_PLAN_SCHEMA_VERSION
        != TARGET_PASS_PLAN_SCHEMA_VERSION
    ):
        raise RecoveryError(
            "recovery target schemas changed; this schema-4-to-5 migration "
            "requires a new review"
        )
    if payload.get("schema_version") != EXPECTATIONS_SCHEMA_VERSION:
        raise RecoveryError("unsupported recovery expectations schema")
    completed = _require_int(
        payload.get("completed_pass"), field="completed_pass", minimum=1
    )
    recovering = _require_int(
        payload.get("recovery_pass"), field="recovery_pass", minimum=2
    )
    if recovering != completed + 1:
        raise RecoveryError("recovery_pass must immediately follow completed_pass")
    required_hashes = {
        "collection_manifest",
        "collector_state",
        "pool_snapshots",
        "recovery_plan",
        "legacy_collector",
        "current_collector",
        "identity_migration",
        "archived_ratings",
        "opponent_registry",
        *(name for group in DATA_FILES.values() for name in group.values()),
    }
    hashes = payload.get("hashes")
    if not isinstance(hashes, dict) or set(hashes) != required_hashes:
        raise RecoveryError(
            "expectations.hashes must contain exactly the required artifact names"
        )
    for name in required_hashes:
        _require_digest(hashes[name], field=f"hashes.{name}")
    tasks = payload.get("tasks")
    if not isinstance(tasks, dict) or not tasks:
        raise RecoveryError("expectations.tasks must be a non-empty object")
    for name, row in tasks.items():
        if not isinstance(name, str) or not name.startswith("national_v"):
            raise RecoveryError("expectations contain an invalid opponent name")
        if not isinstance(row, dict) or set(row) != {
            "split",
            "value_rows",
            "behavior_rows",
        }:
            raise RecoveryError(f"invalid task expectation for {name}")
        if row["split"] not in SOURCE_SPLITS:
            raise RecoveryError(f"invalid expected split for {name}")
        _require_int(row["value_rows"], field=f"{name}.value_rows", minimum=1)
        _require_int(
            row["behavior_rows"], field=f"{name}.behavior_rows", minimum=1
        )
    completed_plans = payload.get("completed_plan_sha256")
    expected_plan_names = {
        f"pass_{index:04d}.json" for index in range(1, completed + 1)
    }
    if not isinstance(completed_plans, dict) or set(completed_plans) != (
        expected_plan_names
    ):
        raise RecoveryError(
            "completed_plan_sha256 must bind the exact completed plan prefix"
        )
    for name, digest in completed_plans.items():
        _require_digest(digest, field=f"completed_plan_sha256.{name}")
    return payload


def _validate_legacy_contract(
    manifest: dict[str, Any],
    *,
    legacy_collector: Path,
    expectations: dict[str, Any],
) -> tuple[dict[str, Any], str, str]:
    contract = manifest.get("resume_contract")
    if not isinstance(contract, dict) or set(contract) != LEGACY_CONTRACT_FIELDS:
        raise RecoveryError("legacy resume contract has an unexpected field set")
    if contract.get("schema_version") != LEGACY_COLLECTION_SCHEMA_VERSION:
        raise RecoveryError("source collection is not the bound schema-4 corpus")
    if contract.get("deck_seed_scheme") != "disjoint_match_blocks_v1":
        raise RecoveryError("legacy collection uses an unsupported seed scheme")
    workers = _require_int(contract.get("workers"), field="workers", minimum=1)
    probe_workers = _require_int(
        contract.get("probe_workers"), field="probe_workers", minimum=1
    )
    if workers != 1 or workers * probe_workers not in {3, 4}:
        raise RecoveryError("legacy recovery requires one outer worker and 3-4 probes")
    _require_int(contract.get("hands"), field="hands", minimum=1)
    _require_int(contract.get("max_decisions"), field="max_decisions", minimum=1)
    if contract.get("decision_sampling") not in {"first", "uniform"}:
        raise RecoveryError("legacy decision-sampling contract is invalid")
    fractions = contract.get("hand_windows")
    if not isinstance(fractions, list) or not fractions:
        raise RecoveryError("legacy hand-window contract is invalid")
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0.0 <= float(value) <= 1.0
        for value in fractions
    ):
        raise RecoveryError("legacy hand-window contract is invalid")

    legacy_sha = _sha256(_require_regular_bytes(legacy_collector))
    _assert_expected_hash(expectations, "legacy_collector", legacy_sha)
    if contract.get("collector_sha256") != legacy_sha:
        raise RecoveryError("legacy collector bytes do not match the resume contract")
    legacy_tools = legacy_collector.parent
    for field, filename in (
        ("probe_sha256", "native_tcp_counterfactual_probe.py"),
        ("cross_hand_sequence_sha256", "cross_hand_sequence.py"),
    ):
        actual = _sha256(_require_regular_bytes(legacy_tools / filename))
        if contract.get(field) != actual:
            raise RecoveryError(f"legacy {field} does not match its source bytes")

    current_path = Path(collector.__file__).resolve()
    current_sha = _sha256(_require_regular_bytes(current_path))
    _assert_expected_hash(expectations, "current_collector", current_sha)
    current_tools = current_path.parent
    current_probe = _sha256(
        _require_regular_bytes(current_tools / "native_tcp_counterfactual_probe.py")
    )
    current_cross = _sha256(
        _require_regular_bytes(current_tools / "cross_hand_sequence.py")
    )
    if current_probe != contract["probe_sha256"]:
        raise RecoveryError("current probe bytes differ from the legacy contract")
    if current_cross != contract["cross_hand_sequence_sha256"]:
        raise RecoveryError("current cross-hand bytes differ from the legacy contract")

    candidate = Path(str(contract.get("candidate") or ""))
    execution = Path(str(contract.get("candidate_execution_path") or ""))
    if not candidate.is_absolute() or not execution.is_absolute():
        raise RecoveryError("candidate paths in the legacy contract are not absolute")
    candidate_digest = collector._directory_digest(candidate)
    execution_digest = collector._directory_digest(execution)
    if (
        candidate_digest != contract.get("candidate_sha256")
        or execution_digest != contract.get("candidate_snapshot_sha256")
        or candidate_digest != execution_digest
    ):
        raise RecoveryError("candidate or candidate snapshot changed")

    current_contract = dict(contract)
    current_contract["schema_version"] = TARGET_COLLECTION_SCHEMA_VERSION
    current_contract["collector_sha256"] = current_sha
    return current_contract, legacy_sha, current_sha


def _validate_prefix_and_plan_set(
    source_dir: Path,
    *,
    completed_pass: int,
    recovery_pass: int,
    snapshots: list[dict[str, Any]],
    expectations: dict[str, Any],
) -> None:
    if [row.get("pass") for row in snapshots] != list(
        range(1, completed_pass + 1)
    ):
        raise RecoveryError("pool snapshots are not the exact completed prefix")
    plan_dir = source_dir / "pass_plans"
    try:
        entries = list(plan_dir.iterdir())
    except OSError as exc:
        raise RecoveryError("pass-plan directory is unavailable") from exc
    expected = {
        f"pass_{index:04d}.json" for index in range(1, recovery_pass + 1)
    }
    if (
        {entry.name for entry in entries} != expected
        or any(entry.is_symlink() or not entry.is_file() for entry in entries)
    ):
        raise RecoveryError("pass-plan set is not exactly the prefix plus recovery pass")
    all_tasks: dict[str, dict[str, Any]] = {}
    seen_seed_pairs: set[tuple[int, int]] = set()
    for index in range(1, recovery_pass + 1):
        payload, plan_snap = _load_json_snapshot(
            plan_dir / f"pass_{index:04d}.json"
        )
        if payload.get("pass") != index:
            raise RecoveryError(f"pass-plan identity mismatch at pass {index}")
        if payload.get("seed_scheme") != "disjoint_match_blocks_v1":
            raise RecoveryError(f"pass-plan seed scheme mismatch at pass {index}")
        if index <= completed_pass:
            expected_digest = expectations["completed_plan_sha256"][
                f"pass_{index:04d}.json"
            ]
            if plan_snap.sha256 != expected_digest:
                raise RecoveryError(f"completed pass plan changed at pass {index}")
        tasks = payload.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise RecoveryError(f"pass plan has no tasks at pass {index}")
        for task in tasks:
            if not isinstance(task, dict):
                raise RecoveryError(f"pass plan has an invalid task at pass {index}")
            name = str(task.get("name") or "")
            if not name.startswith("national_v") or task.get("split") not in SOURCE_SPLITS:
                raise RecoveryError(f"pass plan task identity is invalid at pass {index}")
            pair = (
                _require_int(task.get("deck_seed_base"), field="deck_seed_base"),
                _require_int(task.get("bot_seed_base"), field="bot_seed_base"),
            )
            if pair in seen_seed_pairs:
                raise RecoveryError("pass plan prefix reuses a deck/bot seed pair")
            seen_seed_pairs.add(pair)
            previous = all_tasks.get(name)
            if previous is not None and any(
                previous.get(field) != task.get(field)
                for field in (
                    "opponent_path",
                    "split",
                    "tag_commit",
                    "execution_directory_sha256",
                )
            ):
                raise RecoveryError(f"opponent provenance changed across plans: {name}")
            all_tasks[name] = task

    registry, registry_snap = _load_json_snapshot(
        source_dir / "opponent_snapshots" / "registry.json"
    )
    _assert_expected_hash(expectations, "opponent_registry", registry_snap.sha256)
    opponents = registry.get("opponents")
    if registry.get("schema") != "opponent_execution_snapshot_v1" or not isinstance(
        opponents, dict
    ):
        raise RecoveryError("opponent snapshot registry is invalid")
    for name, task in all_tasks.items():
        entry = opponents.get(name)
        if not isinstance(entry, dict):
            raise RecoveryError(f"opponent registry is missing {name}")
        if any(
            entry.get(registry_field) != task.get(plan_field)
            for registry_field, plan_field in (
                ("snapshot_path", "opponent_path"),
                ("tag_commit", "tag_commit"),
                ("tag_directory_sha256", "tag_directory_sha256"),
                ("execution_directory_sha256", "execution_directory_sha256"),
                ("source_checkout_commit", "source_checkout_commit"),
            )
        ):
            raise RecoveryError(f"opponent registry/plan mismatch: {name}")
        try:
            collector._verify_frozen_opponent(task)
        except RuntimeError as exc:
            raise RecoveryError(str(exc)) from exc
    if any(source_dir.glob("_tmp*")):
        raise RecoveryError("collector temporary probe files still exist")


def _archived_ratings_snapshot(
    *,
    archived_ratings: Path,
    identity_migration: Path,
    ratings_path: Path,
    last_snapshot: dict[str, Any],
    expectations: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, float]], str, str]:
    migration, migration_snap = _load_json_snapshot(identity_migration)
    _assert_expected_hash(
        expectations, "identity_migration", migration_snap.sha256
    )
    moved = migration.get("moved")
    if not isinstance(moved, list) or "glicko_ratings.json" not in moved:
        raise RecoveryError("identity migration did not archive glicko_ratings.json")
    if archived_ratings.parent.resolve() != identity_migration.parent.resolve():
        raise RecoveryError("archived ratings and migration receipt are not co-located")
    if archived_ratings.resolve() == ratings_path.resolve():
        raise RecoveryError("archived ratings path unexpectedly aliases live ratings")
    ratings_raw = _require_regular_bytes(archived_ratings)
    ratings_sha = _sha256(ratings_raw)
    _assert_expected_hash(expectations, "archived_ratings", ratings_sha)
    if last_snapshot.get("ratings_path") != str(ratings_path.resolve()):
        raise RecoveryError("completed prefix ratings path changed")
    if last_snapshot.get("ratings_sha256") != ratings_sha:
        raise RecoveryError("archived ratings do not match the completed-prefix view")
    try:
        normalized = collector._normalize_ratings(
            json.loads(ratings_raw.decode("utf-8"))
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        raise RecoveryError("archived ratings bytes are invalid") from exc
    snapshot = {
        "schema_version": collector.RATINGS_SNAPSHOT_SCHEMA_VERSION,
        "source": "live_file",
        "ratings_path": str(ratings_path.resolve()),
        "ratings_sha256": ratings_sha,
        "ratings_bytes_base64": base64.b64encode(ratings_raw).decode("ascii"),
        "ratings": normalized,
    }
    snapshot["snapshot_sha256"] = collector._canonical_json_sha256(snapshot)
    try:
        validated = collector._validate_ratings_snapshot(snapshot, ratings_path)
    except RuntimeError as exc:
        raise RecoveryError(str(exc)) from exc
    return snapshot, validated, ratings_sha, migration_snap.sha256


def _row_key(row: dict[str, Any]) -> tuple[str, int, int, int, int]:
    opponent = str(row.get("_opponent_label") or row.get("opponent") or "")
    if not opponent:
        raise RecoveryError("collector row is missing an opponent label")
    return (
        opponent,
        _require_int(row.get("deck_seed_base"), field="deck_seed_base", minimum=0),
        _require_int(row.get("bot_seed_base"), field="bot_seed_base", minimum=0),
        _require_int(row.get("hand"), field="hand", minimum=0),
        _require_int(
            row.get("hand_decision_index"),
            field="hand_decision_index",
            minimum=0,
        ),
    )


def _key_digest(keys: list[tuple[str, int, int, int, int]]) -> str:
    payload = [list(key) for key in sorted(keys)]
    return _sha256(_canonical_json_bytes(payload))


def _scan_tail(
    source_dir: Path,
    *,
    state: dict[str, Any],
    plan_entries: list[dict[str, Any]],
    contract: dict[str, Any],
    recovery_pass: int,
    expectations: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, int]]]:
    expected_tasks = expectations["tasks"]
    by_name = {str(entry["name"]): entry for entry in plan_entries}
    if set(by_name) != set(expected_tasks):
        raise RecoveryError("reviewed task set does not match the recovery plan")
    seed_pairs: dict[tuple[int, int], dict[str, Any]] = {}
    for name, entry in by_name.items():
        if expected_tasks[name]["split"] != entry.get("split"):
            raise RecoveryError(f"reviewed split changed for {name}")
        pair = (
            _require_int(entry.get("deck_seed_base"), field=f"{name}.deck_seed"),
            _require_int(entry.get("bot_seed_base"), field=f"{name}.bot_seed"),
        )
        if pair in seed_pairs:
            raise RecoveryError("recovery plan has a duplicate seed pair")
        seed_pairs[pair] = entry

    hands = _require_int(contract["hands"], field="hands", minimum=1)
    fractions = [float(value) for value in contract["hand_windows"]]
    fraction = fractions[(recovery_pass - 1) % len(fractions)]
    min_hand = max(1, min(hands, 1 + int((hands - 1) * fraction)))
    ratings_path = str(Path(str(contract["ratings_path"])).resolve())
    limits = {
        "value": {
            split: _require_int(
                state["total_rows"][split],
                field=f"total_rows.{split}",
                minimum=0,
            )
            for split in SOURCE_SPLITS
        },
        "behavior": {
            split: _require_int(
                state["total_behavior_rows"][split],
                field=f"total_behavior_rows.{split}",
                minimum=0,
            )
            for split in SOURCE_SPLITS
        },
    }
    evidence: dict[str, Any] = {
        name: {
            "split": str(entry["split"]),
            "deck_seed_base": int(entry["deck_seed_base"]),
            "bot_seed_base": int(entry["bot_seed_base"]),
            "value_rows": 0,
            "behavior_rows": 0,
            "value_keys": [],
            "behavior_keys": [],
            "behavior_max_hand": -1,
        }
        for name, entry in by_name.items()
    }
    seen_keys: dict[str, set[tuple[str, int, int, int, int]]] = {
        "value": set(),
        "behavior": set(),
    }
    actual_totals = {"value": {}, "behavior": {}}

    for kind, split_files in DATA_FILES.items():
        for split, filename in split_files.items():
            path = source_dir / filename
            prefix_rows = limits[kind][split]

            def visit(index: int, row: dict[str, Any]) -> None:
                key = _row_key(row)
                if key in seen_keys[kind]:
                    raise RecoveryError(f"duplicate collector row key in {kind}: {key}")
                seen_keys[kind].add(key)
                annotated_pair = (row.get("_seed_base"), row.get("_bot_seed_base"))
                direct_pair = (row.get("deck_seed_base"), row.get("bot_seed_base"))
                task = seed_pairs.get(direct_pair)
                if index < prefix_rows:
                    if task is not None or annotated_pair in seed_pairs:
                        raise RecoveryError(
                            f"recovery-pass row leaked into committed prefix: {filename}"
                        )
                    return
                if task is None or annotated_pair != direct_pair:
                    raise RecoveryError(
                        f"unplanned row exists after atomic prefix: {filename}:{index + 1}"
                    )
                name = str(task["name"])
                if (
                    task["split"] != split
                    or row.get("_split") != split
                    or row.get("_opponent_label") != name
                    or row.get("opponent") != name
                    or row.get("_collection_hands") != hands
                    or row.get("_min_hand") != min_hand
                    or row.get("_ratings_path") != ratings_path
                    or row.get("status") != "ok"
                ):
                    raise RecoveryError(
                        f"tail row contract mismatch for {name} in {filename}"
                    )
                if not min_hand <= key[3] < hands:
                    raise RecoveryError(
                        f"tail row hand is outside the planned window for {name}"
                    )
                bucket = evidence[name]
                count_field = f"{kind}_rows"
                keys_field = f"{kind}_keys"
                bucket[count_field] += 1
                bucket[keys_field].append(key)
                if kind == "behavior":
                    bucket["behavior_max_hand"] = max(
                        bucket["behavior_max_hand"], key[3]
                    )

            total, digest = _visit_jsonl(path, visit)
            _assert_expected_hash(expectations, filename, digest)
            if total <= prefix_rows:
                raise RecoveryError(f"{filename} has no recoverable tail")
            actual_totals[kind][split] = total

    max_decisions = _require_int(
        contract["max_decisions"], field="max_decisions", minimum=1
    )
    for name, row in evidence.items():
        expected = expected_tasks[name]
        if expected["value_rows"] != max_decisions:
            raise RecoveryError(
                f"reviewed value count for {name} is not the full max-decisions tail"
            )
        if row["value_rows"] != expected["value_rows"]:
            raise RecoveryError(f"value tail count changed for {name}")
        if row["behavior_rows"] != expected["behavior_rows"]:
            raise RecoveryError(f"behavior tail count changed for {name}")
        if row["behavior_max_hand"] != hands - 1:
            raise RecoveryError(f"behavior tail does not reach hand {hands - 1}: {name}")
        row["value_key_sha256"] = _key_digest(row.pop("value_keys"))
        row["behavior_key_sha256"] = _key_digest(row.pop("behavior_keys"))
    for kind in ("value", "behavior"):
        for split in SOURCE_SPLITS:
            expected_total = limits[kind][split] + sum(
                int(evidence[name][f"{kind}_rows"])
                for name in evidence
                if evidence[name]["split"] == split
            )
            if actual_totals[kind][split] != expected_total:
                raise RecoveryError(f"unaccounted {kind} rows exist in {split}")
    return evidence, actual_totals


def audit_recovery(
    *,
    source_dir: Path,
    legacy_collector: Path,
    archived_ratings: Path,
    identity_migration: Path,
    expectations_path: Path,
) -> AuditResult:
    source_dir = source_dir.resolve()
    if source_dir.is_symlink() or not source_dir.is_dir():
        raise RecoveryError("source collection directory is unavailable")
    expectations, expectations_snap = _load_json_snapshot(expectations_path)
    expectations = _validate_expectations(expectations)
    completed = expectations["completed_pass"]
    recovering = expectations["recovery_pass"]

    manifest, manifest_snap = _load_json_snapshot(
        source_dir / "collection_manifest.json"
    )
    _assert_expected_hash(
        expectations, "collection_manifest", manifest_snap.sha256
    )
    state, state_snap = _load_json_snapshot(source_dir / "collector_state.json")
    _assert_expected_hash(expectations, "collector_state", state_snap.sha256)
    if state.get("completed_passes") != completed:
        raise RecoveryError("collector state is not at the reviewed completed pass")
    if _require_int(
        manifest.get("passes_requested"), field="passes_requested", minimum=1
    ) < recovering:
        raise RecoveryError("collection target does not include the recovery pass")

    pool_snap = _snapshot(source_dir / "pool_snapshots.jsonl")
    _assert_expected_hash(expectations, "pool_snapshots", pool_snap.sha256)
    snapshots = _parse_jsonl(pool_snap.raw, path=pool_snap.path)
    _validate_prefix_and_plan_set(
        source_dir,
        completed_pass=completed,
        recovery_pass=recovering,
        snapshots=snapshots,
        expectations=expectations,
    )
    plan_path = source_dir / "pass_plans" / f"pass_{recovering:04d}.json"
    legacy_plan, plan_snap = _load_json_snapshot(plan_path)
    _assert_expected_hash(expectations, "recovery_plan", plan_snap.sha256)
    if "schema_version" in legacy_plan or "ratings_snapshot" in legacy_plan:
        raise RecoveryError("recovery plan is not the reviewed legacy plan")

    current_contract, legacy_sha, current_sha = _validate_legacy_contract(
        manifest,
        legacy_collector=legacy_collector.resolve(),
        expectations=expectations,
    )
    ratings_path = Path(str(current_contract["ratings_path"]))
    if not ratings_path.is_absolute():
        raise RecoveryError("legacy ratings path is not absolute")
    ratings_snapshot, rating_rows, ratings_sha, migration_sha = (
        _archived_ratings_snapshot(
            archived_ratings=archived_ratings.resolve(),
            identity_migration=identity_migration.resolve(),
            ratings_path=ratings_path,
            last_snapshot=snapshots[-1],
            expectations=expectations,
        )
    )
    upgraded_plan = dict(legacy_plan)
    upgraded_plan["schema_version"] = TARGET_PASS_PLAN_SCHEMA_VERSION
    upgraded_plan["ratings_snapshot"] = ratings_snapshot
    try:
        _, _, plan_entries = collector._validate_pass_plan(
            upgraded_plan,
            pass_number=recovering,
            ratings_path=ratings_path,
            hands=int(current_contract["hands"]),
            deck_seed_base=int(current_contract["deck_seed_base"]),
            deck_seed_guard=int(current_contract["deck_seed_guard"]),
            bot_seed_base=int(current_contract["bot_seed_base"]),
            val_opponents=set(current_contract["val_opponents"]),
            held_out_opponents=set(current_contract["held_out_opponents"]),
        )
        for entry in plan_entries:
            collector._verify_frozen_opponent(entry)
    except (RuntimeError, KeyError, TypeError, ValueError) as exc:
        raise RecoveryError(f"upgraded recovery plan is invalid: {exc}") from exc

    tail, actual_totals = _scan_tail(
        source_dir,
        state=state,
        plan_entries=plan_entries,
        contract=current_contract,
        recovery_pass=recovering,
        expectations=expectations,
    )
    return AuditResult(
        source_dir=source_dir,
        expectations={
            **expectations,
            "expectations_sha256": expectations_snap.sha256,
        },
        manifest=manifest,
        manifest_snapshot=manifest_snap,
        state=state,
        state_snapshot=state_snap,
        snapshots=snapshots,
        snapshots_snapshot=pool_snap,
        legacy_plan=legacy_plan,
        plan_snapshot=plan_snap,
        upgraded_plan=upgraded_plan,
        upgraded_plan_bytes=_pretty_json_bytes(upgraded_plan),
        ratings_snapshot=ratings_snapshot,
        rating_rows=rating_rows,
        tail_evidence=tail,
        actual_totals=actual_totals,
        current_contract=current_contract,
        legacy_collector_sha256=legacy_sha,
        current_collector_sha256=current_sha,
        archived_ratings_sha256=ratings_sha,
        identity_migration_sha256=migration_sha,
    )


def _build_recovered_payloads(
    audit: AuditResult,
) -> tuple[dict[str, bytes], dict[str, Any]]:
    completed = int(audit.expectations["completed_pass"])
    recovering = int(audit.expectations["recovery_pass"])
    contract = audit.current_contract
    hands = int(contract["hands"])
    fractions = [float(value) for value in contract["hand_windows"]]
    fraction = fractions[(recovering - 1) % len(fractions)]
    min_hand = max(1, min(hands, 1 + int((hands - 1) * fraction)))
    plan_entries = list(audit.upgraded_plan["tasks"])
    pool_row = {
        "pass": recovering,
        "ratings_path": audit.ratings_snapshot["ratings_path"],
        "ratings_sha256": audit.ratings_snapshot["ratings_sha256"],
        "ratings_snapshot_sha256": audit.ratings_snapshot["snapshot_sha256"],
        "min_hand": min_hand,
        "hands": hands,
        "workers": int(contract["workers"]),
        "probe_workers": int(contract["probe_workers"]),
        "decision_sampling": contract["decision_sampling"],
        "pool": [{
            "name": entry["name"],
            "split": entry["split"],
            "tag_commit": entry["tag_commit"],
            "execution_directory_sha256": entry["execution_directory_sha256"],
            "source_checkout_commit": entry["source_checkout_commit"],
            "glicko": audit.rating_rows.get(str(entry["name"])),
            "deck_seed_base": int(entry["deck_seed_base"]),
            "deck_seed_last": int(entry["deck_seed_last"]),
            "bot_seed_base": int(entry["bot_seed_base"]),
        } for entry in plan_entries],
    }
    pool_bytes = audit.snapshots_snapshot.raw + _canonical_json_bytes(pool_row) + b"\n"
    recovered_state = {
        "completed_passes": recovering,
        "total_rows": dict(audit.actual_totals["value"]),
        "total_behavior_rows": dict(audit.actual_totals["behavior"]),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    state_bytes = _pretty_json_bytes(recovered_state)

    receipt: dict[str, Any] = {
        "schema_version": RECOVERY_RECEIPT_SCHEMA_VERSION,
        "mode": "complete_schema4_tail_to_schema5",
        "completed_prefix_pass": completed,
        "recovered_pass": recovering,
        "expectations_sha256": audit.expectations["expectations_sha256"],
        "recovery_tool_sha256": _sha256(_require_regular_bytes(Path(__file__))),
        "reviewed_hashes": dict(audit.expectations["hashes"]),
        "completed_plan_sha256": dict(
            audit.expectations["completed_plan_sha256"]
        ),
        "before": {
            "collection_manifest_sha256": audit.manifest_snapshot.sha256,
            "collector_state_sha256": audit.state_snapshot.sha256,
            "pool_snapshots_sha256": audit.snapshots_snapshot.sha256,
            "recovery_plan_sha256": audit.plan_snapshot.sha256,
            "legacy_collector_sha256": audit.legacy_collector_sha256,
        },
        "archived_ratings": {
            "ratings_sha256": audit.archived_ratings_sha256,
            "ratings_snapshot_sha256": audit.ratings_snapshot["snapshot_sha256"],
            "identity_migration_sha256": audit.identity_migration_sha256,
        },
        "tail": audit.tail_evidence,
        "after": {
            "collector_schema_version": TARGET_COLLECTION_SCHEMA_VERSION,
            "collector_sha256": audit.current_collector_sha256,
            "pass_plan_schema_version": TARGET_PASS_PLAN_SCHEMA_VERSION,
            "recovery_plan_sha256": _sha256(audit.upgraded_plan_bytes),
            "pool_snapshots_sha256": _sha256(pool_bytes),
            "collector_state_sha256": _sha256(state_bytes),
            "total_rows": recovered_state["total_rows"],
            "total_behavior_rows": recovered_state["total_behavior_rows"],
        },
        "probe_execution_count": 0,
        "read_current_ratings": False,
        "strength_evidence": False,
        "deployment_policy_value": False,
    }
    receipt["receipt_sha256"] = _sha256(_canonical_json_bytes(receipt))
    recovered_manifest = dict(audit.manifest)
    recovered_manifest["resume_contract"] = dict(audit.current_contract)
    recovered_manifest["legacy_recovery"] = receipt
    manifest_bytes = _pretty_json_bytes(recovered_manifest)
    targets = {
        "pass_plan": audit.upgraded_plan_bytes,
        "pool_snapshots": pool_bytes,
        "collector_state": state_bytes,
        "collection_manifest": manifest_bytes,
    }
    return targets, receipt


def _target_paths(audit: AuditResult) -> dict[str, Path]:
    recovering = int(audit.expectations["recovery_pass"])
    return {
        "pass_plan": audit.source_dir / "pass_plans" / f"pass_{recovering:04d}.json",
        "pool_snapshots": audit.source_dir / "pool_snapshots.jsonl",
        "collector_state": audit.source_dir / "collector_state.json",
        "collection_manifest": audit.source_dir / "collection_manifest.json",
    }


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_new_file(path: Path, raw: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            path.unlink()
        raise


def _atomic_replace_bytes(path: Path, raw: bytes) -> None:
    temporary = path.parent / f".{path.name}.recovery-{os.getpid()}-{uuid.uuid4().hex}"
    _write_new_file(temporary, raw)
    try:
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def _transaction_path(source_dir: Path) -> Path:
    return source_dir / TRANSACTION_DIRNAME


def _transaction_entry_name(index: int, suffix: str) -> str:
    return f"target-{index:02d}.{suffix}"


def _prepare_transaction(
    audit: AuditResult,
    *,
    after: dict[str, bytes],
    poison_manifest: bytes,
) -> tuple[Path, dict[str, Any]]:
    transaction = _transaction_path(audit.source_dir)
    if transaction.exists() or transaction.is_symlink():
        raise RecoveryError("a legacy recovery transaction already exists")
    temporary = audit.source_dir / f".{TRANSACTION_DIRNAME}.tmp-{uuid.uuid4().hex}"
    temporary.mkdir(mode=0o700)
    targets = _target_paths(audit)
    expected_before = {
        "pass_plan": audit.plan_snapshot.sha256,
        "pool_snapshots": audit.snapshots_snapshot.sha256,
        "collector_state": audit.state_snapshot.sha256,
        "collection_manifest": audit.manifest_snapshot.sha256,
    }
    entries = []
    try:
        for index, name in enumerate(PUBLISH_TARGETS):
            before = _require_regular_bytes(targets[name])
            if _sha256(before) != expected_before[name]:
                raise RecoveryError(
                    f"publication target changed after final audit: {name}"
                )
            before_name = _transaction_entry_name(index, "before")
            after_name = _transaction_entry_name(index, "after")
            _write_new_file(temporary / before_name, before)
            _write_new_file(temporary / after_name, after[name])
            entries.append({
                "name": name,
                "relative_path": str(targets[name].relative_to(audit.source_dir)),
                "before_file": before_name,
                "before_sha256": _sha256(before),
                "after_file": after_name,
                "after_sha256": _sha256(after[name]),
            })
        _write_new_file(temporary / "poison-manifest", poison_manifest)
        journal = {
            "schema_version": TRANSACTION_SCHEMA_VERSION,
            "receipt_sha256": json.loads(after["collection_manifest"])[
                "legacy_recovery"
            ]["receipt_sha256"],
            "poison_manifest_sha256": _sha256(poison_manifest),
            "entries": entries,
        }
        journal["journal_sha256"] = _sha256(_canonical_json_bytes(journal))
        _write_new_file(temporary / "journal.json", _pretty_json_bytes(journal))
        _fsync_directory(temporary)
        os.rename(temporary, transaction)
        _fsync_directory(audit.source_dir)
        return transaction, journal
    except BaseException:
        with contextlib.suppress(OSError):
            shutil.rmtree(temporary)
        raise


def _load_transaction(
    source_dir: Path,
) -> tuple[Path, dict[str, Any], dict[str, tuple[Path, bytes, bytes]]]:
    transaction = _transaction_path(source_dir)
    if transaction.is_symlink() or not transaction.is_dir():
        raise RecoveryError("recovery transaction is not a regular directory")
    journal, _ = _load_json_snapshot(transaction / "journal.json")
    digest = str(journal.pop("journal_sha256", ""))
    if digest != _sha256(_canonical_json_bytes(journal)):
        raise RecoveryError("recovery transaction journal digest mismatch")
    journal["journal_sha256"] = digest
    if journal.get("schema_version") != TRANSACTION_SCHEMA_VERSION:
        raise RecoveryError("unsupported recovery transaction schema")
    raw_entries = journal.get("entries")
    if not isinstance(raw_entries, list) or len(raw_entries) != len(PUBLISH_TARGETS):
        raise RecoveryError("recovery transaction has invalid targets")
    loaded: dict[str, tuple[Path, bytes, bytes]] = {}
    for index, entry in enumerate(raw_entries):
        if not isinstance(entry, dict) or entry.get("name") != PUBLISH_TARGETS[index]:
            raise RecoveryError("recovery transaction target order changed")
        name = str(entry["name"])
        relative = Path(str(entry.get("relative_path") or ""))
        if relative.is_absolute() or ".." in relative.parts:
            raise RecoveryError("recovery transaction contains an unsafe target path")
        before = _require_regular_bytes(transaction / str(entry["before_file"]))
        after = _require_regular_bytes(transaction / str(entry["after_file"]))
        if _sha256(before) != entry.get("before_sha256"):
            raise RecoveryError("recovery transaction before-image changed")
        if _sha256(after) != entry.get("after_sha256"):
            raise RecoveryError("recovery transaction after-image changed")
        loaded[name] = (source_dir / relative, before, after)
    poison = _require_regular_bytes(transaction / "poison-manifest")
    if _sha256(poison) != journal.get("poison_manifest_sha256"):
        raise RecoveryError("recovery transaction poison manifest changed")
    loaded["poison"] = (
        source_dir / "collection_manifest.json",
        b"",
        poison,
    )
    return transaction, journal, loaded


def _remove_transaction(transaction: Path) -> None:
    parent = transaction.parent
    tombstone = parent / f".{TRANSACTION_DIRNAME}.cleanup-{uuid.uuid4().hex}"
    os.rename(transaction, tombstone)
    _fsync_directory(parent)
    try:
        shutil.rmtree(tombstone)
    finally:
        _fsync_directory(parent)


def _rollback_transaction(source_dir: Path) -> str:
    transaction, _, entries = _load_transaction(source_dir)
    current = {
        name: _sha256(_require_regular_bytes(entries[name][0]))
        for name in PUBLISH_TARGETS
    }
    before = {name: _sha256(entries[name][1]) for name in PUBLISH_TARGETS}
    after = {name: _sha256(entries[name][2]) for name in PUBLISH_TARGETS}
    poison_digest = _sha256(entries["poison"][2])
    for name in PUBLISH_TARGETS:
        allowed = {before[name], after[name]}
        if name == "collection_manifest":
            allowed.add(poison_digest)
        if current[name] not in allowed:
            raise RecoveryError(
                f"transaction target changed outside recovery: {name}; "
                "refusing destructive rollback"
            )
    if current == after:
        _remove_transaction(transaction)
        return "committed"
    if current == before:
        _remove_transaction(transaction)
        return "already_rolled_back"

    manifest_path, _, poison = entries["poison"]
    if current["collection_manifest"] != _sha256(poison):
        _atomic_replace_bytes(manifest_path, poison)
    restored = []
    try:
        for name in ("pass_plan", "pool_snapshots", "collector_state"):
            path, raw, _ = entries[name]
            _atomic_replace_bytes(path, raw)
            restored.append(name)
        if any(
            _sha256(_require_regular_bytes(entries[name][0])) != before[name]
            for name in restored
        ):
            raise RecoveryError("rollback verification failed before manifest restore")
        path, raw, _ = entries["collection_manifest"]
        _atomic_replace_bytes(path, raw)
        if any(
            _sha256(_require_regular_bytes(entries[name][0])) != before[name]
            for name in PUBLISH_TARGETS
        ):
            raise RecoveryError("rollback verification failed")
    except BaseException as exc:
        raise RecoveryError(
            "rollback failed; poison manifest remains and collectors must stay stopped"
        ) from exc
    _remove_transaction(transaction)
    return "rolled_back"


def _poison_manifest(audit: AuditResult, receipt_sha256: str) -> bytes:
    payload = dict(audit.manifest)
    payload["resume_contract"] = {
        "schema_version": "legacy_recovery_in_progress",
        "receipt_sha256": receipt_sha256,
        "source_manifest_sha256": audit.manifest_snapshot.sha256,
    }
    payload["legacy_recovery_in_progress"] = True
    return _pretty_json_bytes(payload)


def apply_recovery(audit: AuditResult) -> dict[str, Any]:
    after, receipt = _build_recovered_payloads(audit)
    poison = _poison_manifest(audit, receipt["receipt_sha256"])
    transaction, _ = _prepare_transaction(
        audit,
        after=after,
        poison_manifest=poison,
    )
    paths = _target_paths(audit)
    published: list[str] = []
    try:
        _atomic_replace_bytes(paths["collection_manifest"], poison)
        for name in ("pass_plan", "pool_snapshots", "collector_state"):
            _atomic_replace_bytes(paths[name], after[name])
            published.append(name)
        _atomic_replace_bytes(
            paths["collection_manifest"], after["collection_manifest"]
        )
        published.append("collection_manifest")
        for name in PUBLISH_TARGETS:
            actual = _sha256(_require_regular_bytes(paths[name]))
            expected = _sha256(after[name])
            if actual != expected:
                raise RecoveryError(f"published target failed verification: {name}")
    except BaseException as exc:
        try:
            outcome = _rollback_transaction(audit.source_dir)
        except BaseException as rollback_exc:
            raise RecoveryError(
                "recovery publication and rollback both failed; collectors remain poisoned"
            ) from rollback_exc
        raise RecoveryError(
            f"recovery publication failed after {published}; rollback={outcome}"
        ) from exc
    try:
        _remove_transaction(transaction)
    except OSError:
        # All four after-images already match.  A later --apply invocation can
        # recognize and remove the committed journal without rolling back.
        pass
    return {
        "status": "recovered",
        "completed_passes": int(audit.expectations["recovery_pass"]),
        "receipt_sha256": receipt["receipt_sha256"],
        "probe_execution_count": 0,
        "read_current_ratings": False,
        "strength_evidence": False,
        "deployment_policy_value": False,
    }


def _existing_transaction_status(source_dir: Path, *, apply: bool) -> str | None:
    transaction = _transaction_path(source_dir)
    if not transaction.exists() and not transaction.is_symlink():
        return None
    if not apply:
        raise RecoveryError(
            "an interrupted recovery transaction exists; explicit --apply is required"
        )
    return _rollback_transaction(source_dir)


def run(
    *,
    source_dir: Path,
    legacy_collector: Path,
    archived_ratings: Path,
    identity_migration: Path,
    expectations_path: Path,
    apply: bool,
) -> dict[str, Any]:
    source_dir = source_dir.resolve()
    with _collector_lock(source_dir, writable=apply):
        transaction_outcome = _existing_transaction_status(
            source_dir, apply=apply
        )
        if transaction_outcome == "committed":
            return {
                "status": "recovered_transaction_finalized",
                "probe_execution_count": 0,
                "read_current_ratings": False,
                "strength_evidence": False,
                "deployment_policy_value": False,
            }
        audit = audit_recovery(
            source_dir=source_dir,
            legacy_collector=legacy_collector,
            archived_ratings=archived_ratings,
            identity_migration=identity_migration,
            expectations_path=expectations_path,
        )
        # Re-read all reviewed mutable inputs immediately before publication.
        if apply:
            refreshed = audit_recovery(
                source_dir=source_dir,
                legacy_collector=legacy_collector,
                archived_ratings=archived_ratings,
                identity_migration=identity_migration,
                expectations_path=expectations_path,
            )
            return apply_recovery(refreshed)
        return {
            "status": "ready_for_explicit_apply",
            "completed_prefix_pass": audit.expectations["completed_pass"],
            "recoverable_pass": audit.expectations["recovery_pass"],
            "tail": audit.tail_evidence,
            "archived_ratings_sha256": audit.archived_ratings_sha256,
            "legacy_collector_sha256": audit.legacy_collector_sha256,
            "current_collector_sha256": audit.current_collector_sha256,
            "probe_execution_count": 0,
            "read_current_ratings": False,
            "strength_evidence": False,
            "deployment_policy_value": False,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--legacy-collector", required=True, type=Path)
    parser.add_argument("--archived-ratings", required=True, type=Path)
    parser.add_argument("--identity-migration", required=True, type=Path)
    parser.add_argument("--expectations", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run(
            source_dir=args.source_dir,
            legacy_collector=args.legacy_collector,
            archived_ratings=args.archived_ratings,
            identity_migration=args.identity_migration,
            expectations_path=args.expectations,
            apply=args.apply,
        )
    except (OSError, RecoveryError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
