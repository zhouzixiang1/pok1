#!/usr/bin/env python3
"""Atomically migrate a completed opponent-model prefix to schema-6 concurrency.

This is a narrow, one-time control-plane migration.  It never launches a probe,
reads live ratings, or edits collected rows.  The receipt preserves the exact
schema-5 manifest and binds every completed pass/data prefix so later formal
role freezing can replay the schema-5 -> schema-6 trust chain.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

import longrun_collect_oppmodel as collector


MIGRATION_SCHEMA_VERSION = 1
MIGRATION_MODE = "atomic_collector_concurrency_schema5_to_schema6_v1"
SOURCE_SPLITS = ("train", "val", "held_out")
DATA_FILES = tuple(
    f"{prefix}_{split}.jsonl"
    for prefix in ("cf", "opponent_actions")
    for split in SOURCE_SPLITS
)
ALLOWED_CONTRACT_CHANGES = {
    "schema_version", "workers", "probe_workers", "collector_sha256",
}
SOURCE_SCHEMA_VERSION = 5
SOURCE_WORKERS = 1
SOURCE_PROBE_WORKERS = 4
TARGET_SCHEMA_VERSION = 6
TARGET_WORKERS = 6
TARGET_PROBE_WORKERS = 2


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _canonical_sha256(payload: dict[str, Any]) -> str:
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _sha256_bytes(raw)


def _integer(value: Any, *, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RuntimeError(f"{field} must be an integer >= {minimum}")
    return value


def _jsonl_prefix(path: Path, rows: int) -> dict[str, Any]:
    digest = hashlib.sha256()
    size = 0
    count = 0
    with path.open("rb") as handle:
        while count < rows:
            line = handle.readline()
            if not line or not line.endswith(b"\n"):
                raise RuntimeError(f"JSONL prefix is incomplete: {path}")
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"invalid JSONL row in {path}") from exc
            if not isinstance(payload, dict):
                raise RuntimeError(f"JSONL row is not an object: {path}")
            digest.update(line)
            size += len(line)
            count += 1
        if handle.readline():
            raise RuntimeError(
                f"migration requires an exact atomic prefix; {path} has rows past {rows}"
            )
    return {"rows": count, "bytes": size, "sha256": digest.hexdigest()}


def _pool_prefix(path: Path, boundary: int) -> dict[str, Any]:
    details = _jsonl_prefix(path, boundary)
    passes = []
    with path.open("r", encoding="utf-8") as handle:
        for _ in range(boundary):
            passes.append(json.loads(handle.readline()).get("pass"))
    if passes != list(range(1, boundary + 1)):
        raise RuntimeError("pool snapshot prefix is not contiguous")
    return details


def _state_prefix(source_dir: Path, boundary: int) -> tuple[dict, dict]:
    state_path = source_dir / "collector_state.json"
    raw = state_path.read_bytes()
    try:
        state = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("collector_state.json is invalid") from exc
    if not isinstance(state, dict):
        raise RuntimeError("collector_state.json is not an object")
    if _integer(
        state.get("completed_passes"), field="completed_passes", minimum=1
    ) != boundary:
        raise RuntimeError("collector state is not at the requested migration boundary")
    for field in ("total_rows", "total_behavior_rows"):
        values = state.get(field)
        if not isinstance(values, dict) or set(values) != set(SOURCE_SPLITS):
            raise RuntimeError(f"collector state has invalid {field}")
        for split in SOURCE_SPLITS:
            _integer(values[split], field=f"{field}.{split}")
    return state, {
        "bytes": len(raw),
        "sha256": _sha256_bytes(raw),
        "bytes_base64": base64.b64encode(raw).decode("ascii"),
    }


def _plan_prefix(source_dir: Path, boundary: int) -> dict[str, str]:
    plan_root = source_dir / "pass_plans"
    expected = {f"pass_{index:04d}.json" for index in range(1, boundary + 1)}
    entries = list(plan_root.iterdir())
    if (
        {entry.name for entry in entries} != expected
        or any(entry.is_symlink() or not entry.is_file() for entry in entries)
    ):
        raise RuntimeError("migration requires the exact completed pass-plan prefix")
    result = {}
    for name in sorted(expected):
        path = plan_root / name
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"invalid pass plan: {path}") from exc
        if payload.get("pass") != int(name[5:9]):
            raise RuntimeError(f"pass-plan index mismatch: {path}")
        result[name] = _sha256(path)
    return result


def _data_prefix(source_dir: Path, state: dict) -> dict[str, dict[str, Any]]:
    result = {}
    for prefix, count_field in (
        ("cf", "total_rows"),
        ("opponent_actions", "total_behavior_rows"),
    ):
        for split in SOURCE_SPLITS:
            filename = f"{prefix}_{split}.jsonl"
            rows = int(state[count_field][split])
            result[filename] = _jsonl_prefix(source_dir / filename, rows)
    return result


def _verify_completed_opponent_snapshots(
    source_dir: Path, *, boundary: int, registry: dict[str, Any]
) -> None:
    opponents = registry.get("opponents")
    if (
        registry.get("schema") != "opponent_execution_snapshot_v1"
        or not isinstance(opponents, dict)
        or not opponents
    ):
        raise RuntimeError("opponent snapshot registry is invalid")
    compared = (
        ("snapshot_path", "opponent_path"),
        ("tag_commit", "tag_commit"),
        ("tag_directory_sha256", "tag_directory_sha256"),
        ("execution_matches_generation_tag", "execution_matches_generation_tag"),
        ("source_path", "source_path"),
        ("source_checkout_commit", "source_checkout_commit"),
        ("execution_directory_sha256", "execution_directory_sha256"),
    )
    verified: set[tuple[str, str]] = set()
    for pass_index in range(1, boundary + 1):
        plan_path = source_dir / "pass_plans" / f"pass_{pass_index:04d}.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        tasks = plan.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise RuntimeError(f"completed pass plan has no tasks: {plan_path}")
        for task in tasks:
            name = str(task.get("name") or "") if isinstance(task, dict) else ""
            registered = opponents.get(name)
            if not isinstance(registered, dict) or any(
                registered.get(registry_field) != task.get(plan_field)
                for registry_field, plan_field in compared
            ):
                raise RuntimeError(f"opponent registry/plan mismatch: {name}")
            key = (str(task["opponent_path"]), str(task["execution_directory_sha256"]))
            if key not in verified:
                collector._verify_frozen_opponent(task)
                verified.add(key)


def _new_contract(
    old: dict[str, Any], *, workers: int, probe_workers: int
) -> dict[str, Any]:
    if old.get("schema_version") != SOURCE_SCHEMA_VERSION:
        raise RuntimeError("concurrency migration requires the schema-5 contract")
    workers = _integer(workers, field="workers", minimum=1)
    probe_workers = _integer(probe_workers, field="probe_workers", minimum=1)
    if workers > collector.MAX_OUTER_WORKERS:
        raise RuntimeError(f"workers exceeds {collector.MAX_OUTER_WORKERS}")
    if probe_workers > collector.MAX_PROBE_WORKERS:
        raise RuntimeError(f"probe_workers exceeds {collector.MAX_PROBE_WORKERS}")
    if workers * probe_workers > collector.MAX_CONCURRENT_NATIVE_MATCHES:
        raise RuntimeError(
            "workers * probe_workers exceeds host-wide native match capacity"
        )
    if (workers, probe_workers) != (TARGET_WORKERS, TARGET_PROBE_WORKERS):
        raise RuntimeError(
            f"reviewed migration topology is {TARGET_WORKERS}x{TARGET_PROBE_WORKERS}"
        )
    if (old.get("workers"), old.get("probe_workers")) != (
        SOURCE_WORKERS,
        SOURCE_PROBE_WORKERS,
    ):
        raise RuntimeError("migration source topology must be 1x4")
    updated = dict(old)
    updated.update({
        "schema_version": collector.COLLECTION_CONTRACT_SCHEMA_VERSION,
        "workers": workers,
        "probe_workers": probe_workers,
        "collector_sha256": _sha256(Path(collector.__file__).resolve()),
    })
    changed = {key for key in set(old) | set(updated) if old.get(key) != updated.get(key)}
    if changed != ALLOWED_CONTRACT_CHANGES:
        raise RuntimeError(f"unsupported collector contract changes: {sorted(changed)}")
    if updated["schema_version"] != TARGET_SCHEMA_VERSION:
        raise RuntimeError("migration target must be schema 6")
    return updated


def build_migration(
    source_dir: Path, *, boundary: int, workers: int, probe_workers: int
) -> tuple[dict[str, Any], dict[str, Any], bytes]:
    source_dir = source_dir.resolve()
    manifest_path = source_dir / "collection_manifest.json"
    previous_raw = manifest_path.read_bytes()
    try:
        previous = json.loads(previous_raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("collection_manifest.json is invalid") from exc
    if not isinstance(previous, dict) or "concurrency_migration" in previous:
        raise RuntimeError("collection already has a concurrency migration")
    if list(source_dir.glob("_tmp*")):
        raise RuntimeError("unfinished probe temporary files block migration")
    if (source_dir / ".legacy_oppmodel_recovery_transaction").exists():
        raise RuntimeError("legacy recovery transaction is still present")

    old_contract = previous.get("resume_contract")
    if not isinstance(old_contract, dict):
        raise RuntimeError("collection has no resume contract")
    new_contract = _new_contract(
        old_contract, workers=workers, probe_workers=probe_workers
    )
    state, state_details = _state_prefix(source_dir, boundary)
    pool_details = _pool_prefix(source_dir / "pool_snapshots.jsonl", boundary)
    plans = _plan_prefix(source_dir, boundary)
    data = _data_prefix(source_dir, state)
    # Import lazily to avoid a module-import cycle: the formal freezer imports
    # this migration module to replay the resulting receipt.
    import freeze_opponent_role_dataset as role_freeze

    legacy_result = role_freeze._legacy_recovery_contract(
        previous,
        completed_passes=boundary,
        source_dir=source_dir,
        validate_data_prefix=True,
        resume_contract=old_contract,
    )
    legacy = previous.get("legacy_recovery")
    legacy_after = legacy.get("after") if isinstance(legacy, dict) else None
    if (
        legacy_result is None
        or legacy_result[1] != boundary
        or not isinstance(legacy_after, dict)
        or legacy_after.get("collector_schema_version") != SOURCE_SCHEMA_VERSION
        or legacy_after.get("collector_sha256") != old_contract.get("collector_sha256")
        or legacy_after.get("pool_snapshots_sha256") != pool_details["sha256"]
        or legacy_after.get("collector_state_sha256") != state_details["sha256"]
        or legacy_after.get("total_rows") != state["total_rows"]
        or legacy_after.get("total_behavior_rows")
        != state["total_behavior_rows"]
    ):
        raise RuntimeError("schema-5 legacy-recovery boundary is not exactly bound")
    registry_path = source_dir / "opponent_snapshots" / "registry.json"
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("opponent snapshot registry is invalid") from exc
    _verify_completed_opponent_snapshots(
        source_dir, boundary=boundary, registry=registry
    )
    previous_details = {
        "bytes": len(previous_raw),
        "sha256": _sha256_bytes(previous_raw),
        "bytes_base64": base64.b64encode(previous_raw).decode("ascii"),
    }
    unsigned = {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "mode": MIGRATION_MODE,
        "boundary_pass": boundary,
        "migration_tool_sha256": _sha256(Path(__file__).resolve()),
        "previous_manifest": previous_details,
        "legacy_recovery_receipt_sha256": legacy.get("receipt_sha256"),
        "before": {
            "resume_contract_sha256": _canonical_sha256(old_contract),
            "schema_version": old_contract["schema_version"],
            "workers": old_contract["workers"],
            "probe_workers": old_contract["probe_workers"],
            "collector_sha256": old_contract["collector_sha256"],
        },
        "after": {
            "resume_contract_sha256": _canonical_sha256(new_contract),
            "schema_version": new_contract["schema_version"],
            "workers": new_contract["workers"],
            "probe_workers": new_contract["probe_workers"],
            "collector_sha256": new_contract["collector_sha256"],
            "max_concurrent_native_matches": workers * probe_workers,
        },
        "completed_prefix": {
            "collector_state": state_details,
            "pool_snapshots": pool_details,
            "pass_plan_sha256": plans,
            "data": data,
        },
        "probe_execution_count": 0,
        "read_current_ratings": False,
        "strength_evidence": False,
        "deployment_policy_value": False,
    }
    receipt = {**unsigned, "receipt_sha256": _canonical_sha256(unsigned)}
    migrated = dict(previous)
    migrated["resume_contract"] = new_contract
    migrated["concurrency_migration"] = receipt
    return migrated, receipt, previous_raw


def _atomic_write(path: Path, payload: dict[str, Any], expected_raw: bytes) -> None:
    if path.read_bytes() != expected_raw:
        raise RuntimeError("collection manifest changed before migration publish")
    raw = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.migration-", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        if path.read_bytes() != expected_raw:
            raise RuntimeError("collection manifest changed during migration publish")
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    if path.read_bytes() != raw:
        raise RuntimeError("published concurrency migration changed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--expected-boundary", required=True, type=int)
    parser.add_argument("--workers", required=True, type=int)
    parser.add_argument("--probe-workers", required=True, type=int)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    source_dir = args.source_dir.resolve()
    lock_path = source_dir / ".collector.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SystemExit("collector is running; migrate only at an atomic boundary") from exc
        try:
            migrated, receipt, previous_raw = build_migration(
                source_dir,
                boundary=args.expected_boundary,
                workers=args.workers,
                probe_workers=args.probe_workers,
            )
            if args.apply:
                _atomic_write(
                    source_dir / "collection_manifest.json", migrated, previous_raw
                )
                status = "migrated"
            else:
                status = "ready_for_explicit_apply"
        finally:
            fcntl.flock(lock_handle, fcntl.LOCK_UN)

    print(json.dumps({
        "status": status,
        "boundary_pass": receipt["boundary_pass"],
        "workers": receipt["after"]["workers"],
        "probe_workers": receipt["after"]["probe_workers"],
        "max_concurrent_native_matches": receipt["after"][
            "max_concurrent_native_matches"
        ],
        "probe_execution_count": 0,
        "read_current_ratings": False,
        "strength_evidence": False,
        "deployment_policy_value": False,
        "receipt_sha256": receipt["receipt_sha256"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
