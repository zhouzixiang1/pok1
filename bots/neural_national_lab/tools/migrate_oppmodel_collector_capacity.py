#!/usr/bin/env python3
"""Atomically migrate an idle schema-6 collector prefix to schema 7.

This execution-only migration never launches a probe or reads current ratings.
It preserves the schema-5 -> schema-6 receipt, binds the exact completed data
prefix, and optionally binds one already-persisted next-pass plan for which no
rows were published. Prior execution is deliberately recorded as unknown; the
bound plan is safe to reuse because the atomic prefix contains none of its rows.
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

import collector_systemd_quiescence as systemd_quiescence
import longrun_collect_oppmodel as collector
import migrate_oppmodel_collector_concurrency as schema6_migration


MIGRATION_SCHEMA_VERSION = 1
MIGRATION_MODE = "atomic_collector_capacity_schema6_to_schema7_v1"
MIGRATION_KEY = "capacity_migration"
MIGRATION_INTENT_SCHEMA = "collector_capacity_migration_intent_v1"
SOURCE_SCHEMA_VERSION = schema6_migration.TARGET_SCHEMA_VERSION
SOURCE_WORKERS = schema6_migration.TARGET_WORKERS
SOURCE_PROBE_WORKERS = schema6_migration.TARGET_PROBE_WORKERS
TARGET_SCHEMA_VERSION = collector.ACTIVE_COLLECTION_CONTRACT_SCHEMA_VERSION
TARGET_WORKERS = collector.MAX_OUTER_WORKERS
TARGET_PROBE_WORKERS = collector.MAX_PROBE_WORKERS
TARGET_MAX_ACTIVE_NATIVE_MATCHES = collector.MAX_CONCURRENT_NATIVE_MATCHES
TARGET_CAPACITY_TOTAL_SLOTS = collector.CAPACITY_TOTAL_SLOTS
TARGET_CAPACITY_FIRST_SLOT = collector.CAPACITY_FIRST_SLOT
ALLOWED_CONTRACT_CHANGES = {
    "schema_version",
    "probe_workers",
    "max_active_native_matches",
    "capacity_total_slots",
    "capacity_first_slot",
    "collector_sha256",
    "runtime_capacity_sha256",
    "national_native_sha256",
}
SOURCE_SPLITS = schema6_migration.SOURCE_SPLITS
DATA_FILES = schema6_migration.DATA_FILES
ROOT = Path(__file__).resolve().parents[3]
RUNTIME_CAPACITY_PATH = ROOT / "web" / "core" / "runtime_capacity.py"
NATIONAL_NATIVE_PATH = ROOT / "web" / "core" / "national_native.py"
PROBE_PATH = Path(__file__).resolve().parent / "native_tcp_counterfactual_probe.py"
ROW_IDENTITY_SCHEMA = "collector_row_identity_v1"
ROW_IDENTITY_FIELDS = (
    "opponent", "deck_seed_base", "bot_seed_base", "hand",
    "hand_decision_index",
)
RUNNING_UNIT_SCHEMA = systemd_quiescence.RUNNING_UNIT_SCHEMA
QUIESCENCE_SCHEMA = systemd_quiescence.QUIESCENCE_SCHEMA
PROCESS_MARKERS = systemd_quiescence.PROCESS_MARKERS
TAIL_EXECUTION_STATUS = "unknown_no_published_output"


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


def _digest(value: Any, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise RuntimeError(f"{field} must be a sha256 digest")
    return value


def _row_identity(row: dict[str, Any]) -> tuple[str, int, int, int, int]:
    label = row.get("_opponent_label")
    source = row.get("opponent")
    opponent = label or source
    if (
        not isinstance(opponent, str)
        or not opponent
        or opponent.strip() != opponent
        or (label is not None and source is not None and label != source)
    ):
        raise RuntimeError("collector row identity is missing opponent")
    return (
        opponent,
        _integer(row.get("deck_seed_base"), field="deck_seed_base"),
        _integer(row.get("bot_seed_base"), field="bot_seed_base"),
        _integer(row.get("hand"), field="hand"),
        _integer(
            row.get("hand_decision_index"), field="hand_decision_index"
        ),
    )


def _identity_sha256(keys: set[tuple[str, int, int, int, int]]) -> str:
    raw = json.dumps(
        [list(key) for key in sorted(keys)],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(raw)


def stable_row_identity_receipt(
    rows_by_modality: dict[str, Any],
) -> dict[str, Any]:
    if set(rows_by_modality) != {"cf", "opponent_actions"}:
        raise RuntimeError("collector row identity modalities changed")
    modalities = {}
    for modality in ("cf", "opponent_actions"):
        seen: set[tuple[str, int, int, int, int]] = set()
        rows = 0
        for row in rows_by_modality[modality]:
            if not isinstance(row, dict):
                raise RuntimeError("collector row identity requires JSON objects")
            identity = _row_identity(row)
            if identity in seen:
                raise RuntimeError(
                    f"duplicate collector row identity in {modality}: {identity}"
                )
            seen.add(identity)
            rows += 1
        modalities[modality] = {
            "rows": rows,
            "unique_rows": len(seen),
            "identity_sha256": _identity_sha256(seen),
        }
    return {
        "schema": ROW_IDENTITY_SCHEMA,
        "fields": list(ROW_IDENTITY_FIELDS),
        "modalities": modalities,
    }


def _validate_identity_receipt(
    receipt: Any, *, expected_rows: dict[str, int]
) -> None:
    if (
        not isinstance(receipt, dict)
        or set(receipt) != {"schema", "fields", "modalities"}
        or receipt.get("schema") != ROW_IDENTITY_SCHEMA
        or receipt.get("fields") != list(ROW_IDENTITY_FIELDS)
        or not isinstance(receipt.get("modalities"), dict)
        or set(receipt["modalities"]) != {"cf", "opponent_actions"}
    ):
        raise RuntimeError("collector row identity receipt changed")
    for modality, expected in expected_rows.items():
        details = receipt["modalities"].get(modality)
        if (
            not isinstance(details, dict)
            or set(details) != {"rows", "unique_rows", "identity_sha256"}
            or _integer(details.get("rows"), field=f"{modality}.rows")
            != expected
            or _integer(
                details.get("unique_rows"), field=f"{modality}.unique_rows"
            ) != expected
        ):
            raise RuntimeError("collector row identity counts changed")
        _digest(
            details.get("identity_sha256"),
            field=f"{modality}.identity_sha256",
        )


def validate_row_identity_receipt(
    receipt: Any, *, expected_rows: dict[str, int]
) -> None:
    _validate_identity_receipt(receipt, expected_rows=expected_rows)


def validate_frozen_row_identity_manifest(
    manifest: dict[str, Any], *, prefixes: tuple[str, ...], roles: tuple[str, ...]
) -> None:
    outputs = manifest.get("outputs")
    try:
        expected = {
            prefix: sum(
                _integer(
                    outputs[f"{prefix}_{role}.jsonl"]["rows"],
                    field="output rows",
                )
                for role in roles
            )
            for prefix in prefixes
        }
        _validate_identity_receipt(
            manifest.get("row_identity"), expected_rows=expected
        )
    except (KeyError, TypeError, RuntimeError) as exc:
        raise ValueError("role dataset row-identity receipt changed") from exc
    if (
        (manifest.get("invariants") or {}).get("stable_row_identity_unique")
        is not True
    ):
        raise ValueError("role dataset row-identity invariant changed")


_validate_running_unit_receipt = (
    systemd_quiescence.validate_running_unit_receipt
)
_write_running_unit_receipt = systemd_quiescence.write_running_unit_receipt
_verify_collector_quiescence = systemd_quiescence.verify_collector_quiescence
_validate_quiescence_receipt = systemd_quiescence.validate_quiescence_receipt


def _migration_intent(
    source_dir: Path, *, boundary: int, workers: int, probe_workers: int,
    max_active_native_matches: int, capacity_total_slots: int,
    capacity_first_slot: int,
) -> dict[str, Any]:
    source_dir = source_dir.resolve()
    manifest_path = source_dir / "collection_manifest.json"
    try:
        manifest_raw = manifest_path.read_bytes()
        manifest = json.loads(manifest_raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("migration intent collection manifest is invalid") from exc
    if (
        not isinstance(manifest, dict)
        or MIGRATION_KEY in manifest
        or "concurrency_migration" not in manifest
        or list(source_dir.glob("_tmp*"))
    ):
        raise RuntimeError("migration intent is not an atomic schema-6 prefix")
    contract = manifest.get("resume_contract")
    if not isinstance(contract, dict):
        raise RuntimeError("migration intent collector contract is missing")
    state_payload, state = schema6_migration._state_prefix(source_dir, boundary)
    pool = schema6_migration._pool_prefix(
        source_dir / "pool_snapshots.jsonl", boundary
    )
    plans, tail = _plan_prefix(
        source_dir,
        boundary=boundary,
        contract=contract,
        collection_manifest=manifest,
    )
    data, identity = _data_prefix_with_identity(
        source_dir, state_payload, exact=True
    )
    registry, registry_details = _registry_snapshot(source_dir)
    schema6_migration._verify_completed_opponent_snapshots(
        source_dir, boundary=boundary, registry=registry
    )
    return {
        "schema": MIGRATION_INTENT_SCHEMA,
        "source_dir": str(source_dir),
        "expected_boundary": boundary,
        "workers": workers,
        "probe_workers": probe_workers,
        "max_active_native_matches": max_active_native_matches,
        "capacity_total_slots": capacity_total_slots,
        "capacity_first_slot": capacity_first_slot,
        "source_collector_sha256": _digest(
            contract.get("collector_sha256"), field="intent source collector"
        ),
        "collection_manifest_sha256": _sha256_bytes(manifest_raw),
        "collector_state_sha256": state["sha256"],
        "pool_snapshots_sha256": pool["sha256"],
        "pool_snapshot_rows": pool["rows"],
        "pass_plan_prefix_sha256": _canonical_sha256({
            "plans": plans, "planned_tail": tail,
        }),
        "data_prefix_sha256": _canonical_sha256({
            "data": data, "row_identity": identity,
        }),
        "opponent_registry_sha256": registry_details["sha256"],
        "temporary_outputs_absent": True,
        "migration_tool_sha256": _sha256(Path(__file__).resolve()),
        "systemd_quiescence_sha256": _sha256(
            Path(systemd_quiescence.__file__).resolve()
        ),
    }


def _validate_migration_intent(
    running_unit: dict[str, Any], expected: dict[str, Any]
) -> None:
    if running_unit.get("migration_intent") != expected:
        raise RuntimeError("collector migration intent changed")


def _current_code_artifacts(old: dict[str, Any]) -> dict[str, str]:
    return {
        "schema6_migration_tool_sha256": _sha256(
            Path(schema6_migration.__file__).resolve()
        ),
        "source_collector_sha256": _digest(
            old.get("collector_sha256"), field="source collector sha256"
        ),
        "source_probe_sha256": _digest(
            old.get("probe_sha256"), field="source probe sha256"
        ),
        "target_collector_sha256": _sha256(Path(collector.__file__).resolve()),
        "target_probe_sha256": _sha256(PROBE_PATH),
        "target_runtime_capacity_sha256": _sha256(RUNTIME_CAPACITY_PATH),
        "target_national_native_sha256": _sha256(NATIONAL_NATIVE_PATH),
        "target_systemd_quiescence_sha256": _sha256(
            Path(systemd_quiescence.__file__).resolve()
        ),
    }


def current_contract_is_reviewed(contract: object) -> bool:
    if not isinstance(contract, dict):
        return False
    expected = (
        TARGET_SCHEMA_VERSION, TARGET_WORKERS, TARGET_PROBE_WORKERS,
        TARGET_MAX_ACTIVE_NATIVE_MATCHES, TARGET_CAPACITY_TOTAL_SLOTS,
        TARGET_CAPACITY_FIRST_SLOT,
    )
    try:
        observed = tuple(
            _integer(contract.get(field), field=field)
            for field in (
                "schema_version", "workers", "probe_workers",
                "max_active_native_matches", "capacity_total_slots",
                "capacity_first_slot",
            )
        )
    except RuntimeError:
        return False
    return observed == expected


def current_runtime_code_bindings() -> dict[str, str]:
    return {
        "runtime_capacity_sha256": _sha256(RUNTIME_CAPACITY_PATH),
        "national_native_sha256": _sha256(NATIONAL_NATIVE_PATH),
    }


def validate_pool_capacity(
    row: dict[str, Any], contract: dict[str, Any], pass_index: int
) -> None:
    fields = {
        "max_active_native_matches": TARGET_MAX_ACTIVE_NATIVE_MATCHES,
        "capacity_total_slots": TARGET_CAPACITY_TOTAL_SLOTS,
        "capacity_first_slot": TARGET_CAPACITY_FIRST_SLOT,
    }
    schema_version = _integer(
        contract.get("schema_version"), field="pool contract schema_version"
    )
    if schema_version == TARGET_SCHEMA_VERSION:
        try:
            observed = {
                field: _integer(row.get(field), field=f"pool.{field}")
                for field in fields
            }
        except RuntimeError as exc:
            raise RuntimeError(
                f"pool snapshot capacity contract changed at pass {pass_index}"
            ) from exc
        if observed != fields:
            raise RuntimeError(
                f"pool snapshot capacity contract changed at pass {pass_index}"
            )
    elif any(field in row for field in fields):
        raise RuntimeError(
            f"historical pool snapshot carries capacity fields at pass {pass_index}"
        )


def resolve_contract_history(
    collection_manifest: dict[str, Any], *, completed_passes: int,
    source_dir: Path, validate_data_prefix: bool,
    current_contract: dict[str, Any],
) -> tuple[int, int, dict[str, Any], dict[str, Any]]:
    capacity = replay_migration(
        collection_manifest, completed_passes=completed_passes,
        source_dir=source_dir, validate_data_prefix=validate_data_prefix,
    )
    if capacity is not None:
        return (
            int(capacity["schema6_boundary"]), int(capacity["boundary"]),
            dict(capacity["source_contract"]), dict(capacity["schema5_contract"]),
        )
    import freeze_opponent_role_dataset as role_freeze

    migration = role_freeze._concurrency_migration_contract(
        collection_manifest, completed_passes=completed_passes,
        source_dir=source_dir, validate_data_prefix=validate_data_prefix,
    )
    return (
        migration[0] if migration else 0, 0, current_contract,
        migration[1] if migration else current_contract,
    )


def contract_for_pass(
    history: tuple[int, int, dict[str, Any], dict[str, Any]], pass_index: int,
    current_contract: dict[str, Any],
) -> dict[str, Any]:
    schema6_boundary, capacity_boundary, schema6_contract, schema5_contract = history
    if schema6_boundary and pass_index <= schema6_boundary:
        return schema5_contract
    if capacity_boundary and pass_index <= capacity_boundary:
        return schema6_contract
    return current_contract


def _new_contract(
    old: dict[str, Any],
    *,
    workers: int,
    probe_workers: int,
    max_active_native_matches: int,
    capacity_total_slots: int,
    capacity_first_slot: int,
) -> dict[str, Any]:
    expected_target = (
        TARGET_WORKERS,
        TARGET_PROBE_WORKERS,
        TARGET_MAX_ACTIVE_NATIVE_MATCHES,
        TARGET_CAPACITY_TOTAL_SLOTS,
        TARGET_CAPACITY_FIRST_SLOT,
    )
    requested = (
        _integer(workers, field="workers", minimum=1),
        _integer(probe_workers, field="probe_workers", minimum=1),
        _integer(
            max_active_native_matches,
            field="max_active_native_matches",
            minimum=1,
        ),
        _integer(capacity_total_slots, field="capacity_total_slots", minimum=1),
        _integer(capacity_first_slot, field="capacity_first_slot"),
    )
    if requested != expected_target:
        raise RuntimeError(
            "reviewed schema-7 topology is workers=6 probe_workers=4 "
            "max_active_native_matches=24 capacity slots 4..27"
        )
    source_topology = tuple(
        _integer(old.get(field), field=f"source {field}", minimum=1)
        for field in ("schema_version", "workers", "probe_workers")
    )
    if (
        source_topology
        != (SOURCE_SCHEMA_VERSION, SOURCE_WORKERS, SOURCE_PROBE_WORKERS)
        or any(
            field in old
            for field in (
                "max_active_native_matches",
                "capacity_total_slots",
                "capacity_first_slot",
                "runtime_capacity_sha256",
                "national_native_sha256",
            )
        )
    ):
        raise RuntimeError("capacity migration source topology must be schema-6 6x2")
    code = _current_code_artifacts(old)
    if code["source_probe_sha256"] != code["target_probe_sha256"]:
        raise RuntimeError("capacity migration cannot change probe semantics")
    updated = dict(old)
    updated.update({
        "schema_version": TARGET_SCHEMA_VERSION,
        "workers": TARGET_WORKERS,
        "probe_workers": TARGET_PROBE_WORKERS,
        "max_active_native_matches": TARGET_MAX_ACTIVE_NATIVE_MATCHES,
        "capacity_total_slots": TARGET_CAPACITY_TOTAL_SLOTS,
        "capacity_first_slot": TARGET_CAPACITY_FIRST_SLOT,
        "collector_sha256": code["target_collector_sha256"],
        "runtime_capacity_sha256": code["target_runtime_capacity_sha256"],
        "national_native_sha256": code["target_national_native_sha256"],
    })
    changed = {
        key
        for key in set(old) | set(updated)
        if old.get(key) != updated.get(key)
    }
    if changed != ALLOWED_CONTRACT_CHANGES:
        raise RuntimeError(
            f"unsupported schema-7 collector contract changes: {sorted(changed)}"
        )
    return updated


def _validate_plan_payload(
    payload: object,
    *,
    pass_number: int,
    contract: dict[str, Any],
) -> dict[str, Any]:
    collector._validate_pass_plan(
        payload,
        pass_number=pass_number,
        ratings_path=Path(str(contract["ratings_path"])),
        hands=_integer(contract["hands"], field="hands", minimum=1),
        deck_seed_base=_integer(
            contract["deck_seed_base"], field="deck_seed_base"
        ),
        deck_seed_guard=_integer(
            contract["deck_seed_guard"], field="deck_seed_guard"
        ),
        bot_seed_base=_integer(
            contract["bot_seed_base"], field="bot_seed_base"
        ),
        val_opponents=set(contract["val_opponents"]),
        held_out_opponents=set(contract["held_out_opponents"]),
    )
    if not isinstance(payload, dict):  # Kept explicit for the return type.
        raise RuntimeError("persisted pass plan must be an object")
    return payload


def _plan_prefix(
    source_dir: Path,
    *,
    boundary: int,
    contract: dict[str, Any],
    collection_manifest: dict[str, Any],
) -> tuple[dict[str, str], dict[str, Any] | None]:
    # A schema-6 source can contain three immutable plan eras: legacy schema-4
    # completed plans, the schema-5 recovery plan, and native schema-6 plans.
    # Replay the signed migration history instead of applying the latest plan
    # validator retroactively to every historical file.
    import freeze_opponent_role_dataset as role_freeze

    history = resolve_contract_history(
        collection_manifest,
        completed_passes=boundary,
        source_dir=source_dir,
        validate_data_prefix=False,
        current_contract=contract,
    )
    historical_contract = history[3]
    legacy = role_freeze._legacy_recovery_contract(
        collection_manifest,
        completed_passes=boundary,
        source_dir=source_dir,
        validate_data_prefix=False,
        resume_contract=historical_contract,
    )
    if legacy is None or legacy[1] != history[0]:
        raise RuntimeError(
            "schema-6 concurrency boundary is not the recovered legacy pass"
        )
    legacy_prefix, recovered_pass, legacy_hashes = legacy
    root = source_dir / "pass_plans"
    completed_names = {
        f"pass_{index:04d}.json" for index in range(1, boundary + 1)
    }
    tail_name = f"pass_{boundary + 1:04d}.json"
    entries = list(root.iterdir())
    observed = {entry.name for entry in entries}
    if (
        not completed_names.issubset(observed)
        or observed - completed_names - {tail_name}
        or any(entry.is_symlink() or not entry.is_file() for entry in entries)
    ):
        raise RuntimeError(
            "capacity migration requires the exact completed plans plus at most "
            "one next-pass plan with no published rows"
        )
    completed = {}
    for index in range(1, boundary + 1):
        name = f"pass_{index:04d}.json"
        path = root / name
        try:
            raw = path.read_bytes()
            payload = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"invalid pass plan: {path}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"pass plan must be an object: {path}")
        digest = _sha256_bytes(raw)
        pass_contract = contract_for_pass(history, index, contract)
        if index <= legacy_prefix:
            if digest != legacy_hashes.get(name):
                raise RuntimeError(f"legacy completed pass plan changed: {name}")
            if (
                payload.get("seed_scheme") != "disjoint_match_blocks_v1"
                or _integer(payload.get("pass"), field=f"{name}.pass", minimum=1)
                != index
            ):
                raise RuntimeError(f"legacy pass-plan identity changed: {path}")
            role_freeze._legacy_plan_tasks(
                payload, pass_index=index, contract=pass_contract
            )
        else:
            _validate_plan_payload(
                payload, pass_number=index, contract=pass_contract
            )
            if index == recovered_pass:
                expected = collection_manifest["legacy_recovery"]["after"][
                    "recovery_plan_sha256"
                ]
                if digest != expected:
                    raise RuntimeError("recovered pass plan changed")
        completed[name] = digest
    if tail_name not in observed:
        return completed, None
    tail_path = root / tail_name
    try:
        raw = tail_path.read_bytes()
        tail_payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid pass plan: {tail_path}") from exc
    tail_contract = contract_for_pass(history, boundary + 1, contract)
    _validate_plan_payload(
        tail_payload, pass_number=boundary + 1, contract=tail_contract
    )
    return completed, {
        "name": tail_name,
        "bytes": len(raw),
        "sha256": _sha256_bytes(raw),
        "published_rows_at_migration": 0,
        "execution_status": TAIL_EXECUTION_STATUS,
    }


def _prefix_details(path: Path, rows: int) -> dict[str, Any]:
    digest = hashlib.sha256()
    size = 0
    count = 0
    with path.open("rb") as handle:
        while count < rows:
            line = handle.readline()
            if not line or not line.endswith(b"\n"):
                raise RuntimeError(f"capacity migration prefix is incomplete: {path}")
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"invalid JSONL prefix: {path}") from exc
            if not isinstance(payload, dict):
                raise RuntimeError(f"JSONL prefix row is not an object: {path}")
            digest.update(line)
            size += len(line)
            count += 1
    return {"rows": count, "bytes": size, "sha256": digest.hexdigest()}


def _data_prefix_with_identity(
    source_dir: Path, state: dict[str, Any], *, exact: bool
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    rows_by_modality: dict[str, list[dict[str, Any]]] = {
        "cf": [], "opponent_actions": [],
    }
    data = {}
    for modality, state_field in (
        ("cf", "total_rows"),
        ("opponent_actions", "total_behavior_rows"),
    ):
        totals = state.get(state_field)
        if not isinstance(totals, dict) or set(totals) != set(SOURCE_SPLITS):
            raise RuntimeError("capacity migration state totals changed")
        for split in SOURCE_SPLITS:
            name = f"{modality}_{split}.jsonl"
            path = source_dir / name
            rows = _integer(totals[split], field=f"{name}.rows")
            details = _prefix_details(path, rows)
            parsed = []
            with path.open("rb") as handle:
                for _ in range(rows):
                    parsed.append(json.loads(handle.readline()))
                if exact and handle.readline():
                    raise RuntimeError(
                        f"capacity migration requires exact atomic prefix: {name}"
                    )
            rows_by_modality[modality].extend(parsed)
            data[name] = details
    return data, stable_row_identity_receipt(rows_by_modality)


def _registry_snapshot(
    source_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    path = source_dir / "opponent_snapshots" / "registry.json"
    if not path.exists():
        path = source_dir / "opponent_snapshots.completed.json"
    try:
        raw = path.read_bytes()
        registry = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("opponent snapshot registry is invalid") from exc
    if not isinstance(registry, dict):
        raise RuntimeError("opponent snapshot registry is invalid")
    return registry, {
        "bytes": len(raw),
        "sha256": _sha256_bytes(raw),
        "bytes_base64": base64.b64encode(raw).decode("ascii"),
    }


def _embedded_registry(details: Any) -> dict[str, Any]:
    if not isinstance(details, dict) or set(details) != {
        "bytes", "sha256", "bytes_base64",
    }:
        raise RuntimeError("capacity migration registry receipt changed")
    try:
        raw = base64.b64decode(details["bytes_base64"], validate=True)
        registry = json.loads(raw)
    except (
        KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError
    ) as exc:
        raise RuntimeError("capacity migration registry receipt is invalid") from exc
    if (
        len(raw) != _integer(details.get("bytes"), field="registry bytes", minimum=1)
        or _sha256_bytes(raw)
        != _digest(details.get("sha256"), field="registry sha256")
        or not isinstance(registry, dict)
        or registry.get("schema") != "opponent_execution_snapshot_v1"
        or not isinstance(registry.get("opponents"), dict)
    ):
        raise RuntimeError("capacity migration registry binding changed")
    return registry


def _validate_registry_extension(
    boundary_registry: dict[str, Any], current_registry: dict[str, Any]
) -> None:
    boundary = boundary_registry["opponents"]
    current = current_registry.get("opponents")
    if (
        current_registry.get("schema") != boundary_registry.get("schema")
        or not isinstance(current, dict)
        or any(current.get(name) != details for name, details in boundary.items())
    ):
        raise RuntimeError("capacity migration opponent registry history changed")


def replay_migration(
    collection_manifest: dict[str, Any],
    *,
    completed_passes: int,
    source_dir: Path,
    validate_data_prefix: bool,
) -> dict[str, Any] | None:
    """Strictly replay one schema-6 -> schema-7 receipt for formal freezing."""
    receipt = collection_manifest.get(MIGRATION_KEY)
    if receipt is None:
        return None
    required = {
        "schema_version",
        "mode",
        "boundary_pass",
        "migration_tool_sha256",
        "previous_manifest",
        "schema6_concurrency_receipt_sha256",
        "before",
        "after",
        "code_artifacts",
        "completed_prefix",
        "planned_tail",
        "collector_quiescence",
        "probe_execution_count",
        "read_current_ratings",
        "strength_evidence",
        "deployment_policy_value",
        "receipt_sha256",
    }
    if not isinstance(receipt, dict) or set(receipt) != required:
        raise RuntimeError("capacity migration receipt fields changed")
    unsigned = dict(receipt)
    recorded = _digest(
        unsigned.pop("receipt_sha256"), field="capacity migration receipt"
    )
    if recorded != _canonical_sha256(unsigned):
        raise RuntimeError("capacity migration receipt digest mismatch")
    if (
        receipt.get("schema_version") != MIGRATION_SCHEMA_VERSION
        or receipt.get("mode") != MIGRATION_MODE
        or receipt.get("migration_tool_sha256")
        != _sha256(Path(__file__).resolve())
        or receipt.get("probe_execution_count") != 0
        or receipt.get("read_current_ratings") is not False
        or receipt.get("strength_evidence") is not False
        or receipt.get("deployment_policy_value") is not False
    ):
        raise RuntimeError("capacity migration receipt is not authoritative")
    _validate_quiescence_receipt(receipt.get("collector_quiescence"))
    boundary = _integer(
        receipt.get("boundary_pass"), field="capacity migration boundary", minimum=1
    )
    if completed_passes < boundary:
        raise RuntimeError("capacity migration boundary exceeds completed prefix")

    previous_details = receipt.get("previous_manifest")
    if not isinstance(previous_details, dict) or set(previous_details) != {
        "bytes",
        "sha256",
        "bytes_base64",
    }:
        raise RuntimeError("schema-6 collection manifest receipt changed")
    try:
        previous_raw = base64.b64decode(
            previous_details["bytes_base64"], validate=True
        )
        previous_manifest = json.loads(previous_raw)
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise RuntimeError("schema-6 collection manifest receipt is invalid") from exc
    if (
        not isinstance(previous_manifest, dict)
        or len(previous_raw)
        != _integer(previous_details.get("bytes"), field="previous manifest bytes")
        or _sha256_bytes(previous_raw)
        != _digest(
            previous_details.get("sha256"), field="previous manifest sha256"
        )
        or MIGRATION_KEY in previous_manifest
    ):
        raise RuntimeError("schema-6 collection manifest binding changed")
    old_contract = previous_manifest.get("resume_contract")
    current_contract = collection_manifest.get("resume_contract")
    if not isinstance(old_contract, dict) or not isinstance(current_contract, dict):
        raise RuntimeError("capacity migration contracts are missing")
    changed = {
        key
        for key in set(old_contract) | set(current_contract)
        if old_contract.get(key) != current_contract.get(key)
    }
    if changed != ALLOWED_CONTRACT_CHANGES:
        raise RuntimeError("capacity migration changed semantic collection fields")
    expected_contract = _new_contract(
        old_contract,
        workers=TARGET_WORKERS,
        probe_workers=TARGET_PROBE_WORKERS,
        max_active_native_matches=TARGET_MAX_ACTIVE_NATIVE_MATCHES,
        capacity_total_slots=TARGET_CAPACITY_TOTAL_SLOTS,
        capacity_first_slot=TARGET_CAPACITY_FIRST_SLOT,
    )
    if (
        not current_contract_is_reviewed(current_contract)
        or _canonical_sha256(current_contract)
        != _canonical_sha256(expected_contract)
    ):
        raise RuntimeError("capacity migration topology or code binding changed")
    code = receipt.get("code_artifacts")
    if code != _current_code_artifacts(old_contract):
        raise RuntimeError("capacity migration code artifacts changed")
    before = receipt.get("before")
    expected_before = {
        "resume_contract_sha256": _canonical_sha256(old_contract),
        "schema_version": old_contract["schema_version"],
        "workers": old_contract["workers"],
        "probe_workers": old_contract["probe_workers"],
        "collector_sha256": old_contract["collector_sha256"],
        "probe_sha256": old_contract["probe_sha256"],
    }
    after = receipt.get("after")
    expected_after = {
        "resume_contract_sha256": _canonical_sha256(current_contract),
        "schema_version": current_contract["schema_version"],
        "workers": current_contract["workers"],
        "probe_workers": current_contract["probe_workers"],
        "max_active_native_matches": current_contract[
            "max_active_native_matches"
        ],
        "capacity_total_slots": current_contract["capacity_total_slots"],
        "capacity_first_slot": current_contract["capacity_first_slot"],
        "collector_sha256": current_contract["collector_sha256"],
        "probe_sha256": current_contract["probe_sha256"],
        "runtime_capacity_sha256": current_contract[
            "runtime_capacity_sha256"
        ],
        "national_native_sha256": current_contract[
            "national_native_sha256"
        ],
    }
    if before != expected_before or after != expected_after:
        raise RuntimeError("capacity migration before/after binding changed")
    concurrency = previous_manifest.get("concurrency_migration")
    if (
        not isinstance(concurrency, dict)
        or receipt.get("schema6_concurrency_receipt_sha256")
        != concurrency.get("receipt_sha256")
    ):
        raise RuntimeError("schema-6 concurrency receipt cross-link changed")
    reconstructed = dict(previous_manifest)
    reconstructed["resume_contract"] = current_contract
    reconstructed[MIGRATION_KEY] = receipt
    if reconstructed != collection_manifest:
        raise RuntimeError("collection manifest changed outside capacity migration")

    # Replay the preserved schema-5 -> schema-6 chain before trusting its output.
    import freeze_opponent_role_dataset as role_freeze

    schema6_replay = role_freeze._concurrency_migration_contract(
        previous_manifest,
        completed_passes=boundary,
        source_dir=source_dir,
        validate_data_prefix=validate_data_prefix,
    )
    if schema6_replay is None:
        raise RuntimeError("schema-6 concurrency receipt did not replay")

    prefix = receipt.get("completed_prefix")
    if not isinstance(prefix, dict) or set(prefix) != {
        "collector_state",
        "pool_snapshots",
        "pass_plan_sha256",
        "data",
        "row_identity",
        "opponent_registry",
    }:
        raise RuntimeError("capacity migration prefix fields changed")
    state_details = prefix.get("collector_state")
    if not isinstance(state_details, dict) or set(state_details) != {
        "bytes",
        "sha256",
        "bytes_base64",
    }:
        raise RuntimeError("capacity migration state receipt changed")
    try:
        state_raw = base64.b64decode(
            state_details["bytes_base64"], validate=True
        )
        state = json.loads(state_raw)
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        raise RuntimeError("capacity migration state receipt is invalid") from exc
    if (
        not isinstance(state, dict)
        or len(state_raw)
        != _integer(state_details.get("bytes"), field="state bytes")
        or _sha256_bytes(state_raw)
        != _digest(state_details.get("sha256"), field="state sha256")
        or state.get("completed_passes") != boundary
    ):
        raise RuntimeError("capacity migration state boundary changed")
    pool_details = prefix.get("pool_snapshots")
    if not isinstance(pool_details, dict) or set(pool_details) != {
        "rows",
        "bytes",
        "sha256",
    }:
        raise RuntimeError("capacity migration pool receipt changed")
    pool_path = source_dir / "pool_snapshots.jsonl"
    if not pool_path.exists():
        pool_path = source_dir / "pool_snapshots.completed.jsonl"
    actual_pool = _prefix_details(pool_path, boundary)
    if actual_pool != pool_details:
        raise RuntimeError("capacity migration pool prefix changed")
    plans = prefix.get("pass_plan_sha256")
    expected_plans = {
        f"pass_{index:04d}.json" for index in range(1, boundary + 1)
    }
    if not isinstance(plans, dict) or set(plans) != expected_plans:
        raise RuntimeError("capacity migration plan prefix changed")
    for name, digest in plans.items():
        if _sha256(source_dir / "pass_plans" / name) != _digest(
            digest, field=f"capacity migration plan {name}"
        ):
            raise RuntimeError(f"capacity migration plan changed: {name}")
    planned_tail = receipt.get("planned_tail")
    if planned_tail is not None:
        if not isinstance(planned_tail, dict) or set(planned_tail) != {
            "name",
            "bytes",
            "sha256",
            "published_rows_at_migration",
            "execution_status",
        }:
            raise RuntimeError("capacity migration planned tail receipt changed")
        expected_name = f"pass_{boundary + 1:04d}.json"
        tail_path = source_dir / "pass_plans" / expected_name
        if (
            planned_tail.get("name") != expected_name
            or planned_tail.get("published_rows_at_migration") != 0
            or planned_tail.get("execution_status") != TAIL_EXECUTION_STATUS
            or not tail_path.is_file()
            or tail_path.stat().st_size != planned_tail.get("bytes")
            or _sha256(tail_path)
            != _digest(planned_tail.get("sha256"), field="planned tail sha256")
        ):
            raise RuntimeError("capacity migration planned tail changed")
    data = prefix.get("data")
    if not isinstance(data, dict) or set(data) != set(DATA_FILES):
        raise RuntimeError("capacity migration data prefix fields changed")
    state_fields = {"cf": "total_rows", "opponent_actions": "total_behavior_rows"}
    expected_identity_rows = {}
    for prefix_name, state_field in state_fields.items():
        totals = state.get(state_field)
        if not isinstance(totals, dict) or set(totals) != set(SOURCE_SPLITS):
            raise RuntimeError("capacity migration state totals changed")
        expected_identity_rows[prefix_name] = sum(
            _integer(totals[split], field=f"{prefix_name}.{split}.rows")
            for split in SOURCE_SPLITS
        )
        for split in SOURCE_SPLITS:
            name = f"{prefix_name}_{split}.jsonl"
            rows = _integer(totals[split], field=f"{name}.rows")
            details = data.get(name)
            if not isinstance(details, dict) or set(details) != {
                "rows",
                "bytes",
                "sha256",
            }:
                raise RuntimeError(f"capacity migration data receipt changed: {name}")
            if details.get("rows") != rows:
                raise RuntimeError(f"capacity migration data rows changed: {name}")
            _integer(details.get("bytes"), field=f"{name}.bytes")
            _digest(details.get("sha256"), field=f"{name}.sha256")
    identity = prefix.get("row_identity")
    _validate_identity_receipt(identity, expected_rows=expected_identity_rows)
    registry_details = prefix.get("opponent_registry")
    boundary_registry = _embedded_registry(registry_details)
    expected_intent = {
        "schema": MIGRATION_INTENT_SCHEMA,
        "source_dir": str(source_dir.resolve()),
        "expected_boundary": boundary,
        "workers": current_contract["workers"],
        "probe_workers": current_contract["probe_workers"],
        "max_active_native_matches": current_contract[
            "max_active_native_matches"
        ],
        "capacity_total_slots": current_contract["capacity_total_slots"],
        "capacity_first_slot": current_contract["capacity_first_slot"],
        "source_collector_sha256": old_contract["collector_sha256"],
        "collection_manifest_sha256": previous_details["sha256"],
        "collector_state_sha256": state_details["sha256"],
        "pool_snapshots_sha256": pool_details["sha256"],
        "pool_snapshot_rows": pool_details["rows"],
        "pass_plan_prefix_sha256": _canonical_sha256({
            "plans": plans, "planned_tail": planned_tail,
        }),
        "data_prefix_sha256": _canonical_sha256({
            "data": data, "row_identity": identity,
        }),
        "opponent_registry_sha256": registry_details["sha256"],
        "temporary_outputs_absent": True,
        "migration_tool_sha256": _sha256(Path(__file__).resolve()),
        "systemd_quiescence_sha256": _sha256(
            Path(systemd_quiescence.__file__).resolve()
        ),
    }
    _validate_migration_intent(
        receipt["collector_quiescence"]["running_unit"], expected_intent
    )
    if validate_data_prefix:
        actual_data, actual_identity = _data_prefix_with_identity(
            source_dir, state, exact=False
        )
        if actual_data != data or actual_identity != identity:
            raise RuntimeError("capacity migration data identity prefix changed")
        registry, _actual_registry = _registry_snapshot(source_dir)
        _validate_registry_extension(boundary_registry, registry)
        schema6_migration._verify_completed_opponent_snapshots(
            source_dir, boundary=boundary, registry=registry
        )
    return {
        "boundary": boundary,
        "source_manifest": previous_manifest,
        "source_contract": old_contract,
        "schema6_boundary": schema6_replay[0],
        "schema5_contract": schema6_replay[1],
    }


def build_migration(
    source_dir: Path,
    *,
    boundary: int,
    workers: int,
    probe_workers: int,
    max_active_native_matches: int,
    capacity_total_slots: int,
    capacity_first_slot: int,
    collector_quiescence: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], bytes]:
    source_dir = source_dir.resolve()
    manifest_path = source_dir / "collection_manifest.json"
    previous_raw = manifest_path.read_bytes()
    try:
        previous = json.loads(previous_raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("collection manifest is invalid") from exc
    if (
        not isinstance(previous, dict)
        or MIGRATION_KEY in previous
        or "concurrency_migration" not in previous
    ):
        raise RuntimeError(
            "capacity migration requires one unmigrated schema-6 manifest"
        )
    temporary_outputs = list(source_dir.glob("_tmp*"))
    if temporary_outputs:
        raise RuntimeError("unfinished probe temporary files block capacity migration")
    old_contract = previous.get("resume_contract")
    if not isinstance(old_contract, dict):
        raise RuntimeError("collection has no schema-6 resume contract")
    new_contract = _new_contract(
        old_contract,
        workers=workers,
        probe_workers=probe_workers,
        max_active_native_matches=max_active_native_matches,
        capacity_total_slots=capacity_total_slots,
        capacity_first_slot=capacity_first_slot,
    )
    _validate_quiescence_receipt(collector_quiescence)
    if collector_quiescence["source_dir"] != str(source_dir):
        raise RuntimeError("collector quiescence source directory changed")
    expected_intent = _migration_intent(
        source_dir, boundary=boundary, workers=workers,
        probe_workers=probe_workers,
        max_active_native_matches=max_active_native_matches,
        capacity_total_slots=capacity_total_slots,
        capacity_first_slot=capacity_first_slot,
    )
    _validate_migration_intent(
        collector_quiescence["running_unit"], expected_intent
    )
    state, state_details = schema6_migration._state_prefix(source_dir, boundary)
    pool = schema6_migration._pool_prefix(
        source_dir / "pool_snapshots.jsonl", boundary
    )
    plans, planned_tail = _plan_prefix(
        source_dir,
        boundary=boundary,
        contract=old_contract,
        collection_manifest=previous,
    )
    data, row_identity = _data_prefix_with_identity(
        source_dir, state, exact=True
    )
    registry, registry_details = _registry_snapshot(source_dir)
    schema6_migration._verify_completed_opponent_snapshots(
        source_dir, boundary=boundary, registry=registry
    )

    # Lazy import avoids the formal freezer -> migration import cycle.
    import freeze_opponent_role_dataset as role_freeze

    schema6_replay = role_freeze._concurrency_migration_contract(
        previous,
        completed_passes=boundary,
        source_dir=source_dir,
        validate_data_prefix=True,
    )
    if schema6_replay is None:
        raise RuntimeError("schema-6 concurrency receipt did not replay")
    code = _current_code_artifacts(old_contract)
    previous_details = {
        "bytes": len(previous_raw),
        "sha256": _sha256_bytes(previous_raw),
        "bytes_base64": base64.b64encode(previous_raw).decode("ascii"),
    }
    before = {
        "resume_contract_sha256": _canonical_sha256(old_contract),
        "schema_version": old_contract["schema_version"],
        "workers": old_contract["workers"],
        "probe_workers": old_contract["probe_workers"],
        "collector_sha256": old_contract["collector_sha256"],
        "probe_sha256": old_contract["probe_sha256"],
    }
    after = {
        "resume_contract_sha256": _canonical_sha256(new_contract),
        "schema_version": new_contract["schema_version"],
        "workers": new_contract["workers"],
        "probe_workers": new_contract["probe_workers"],
        "max_active_native_matches": new_contract[
            "max_active_native_matches"
        ],
        "capacity_total_slots": new_contract["capacity_total_slots"],
        "capacity_first_slot": new_contract["capacity_first_slot"],
        "collector_sha256": new_contract["collector_sha256"],
        "probe_sha256": new_contract["probe_sha256"],
        "runtime_capacity_sha256": new_contract["runtime_capacity_sha256"],
        "national_native_sha256": new_contract["national_native_sha256"],
    }
    unsigned = {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "mode": MIGRATION_MODE,
        "boundary_pass": boundary,
        "migration_tool_sha256": _sha256(Path(__file__).resolve()),
        "previous_manifest": previous_details,
        "schema6_concurrency_receipt_sha256": previous[
            "concurrency_migration"
        ]["receipt_sha256"],
        "before": before,
        "after": after,
        "code_artifacts": code,
        "completed_prefix": {
            "collector_state": state_details,
            "pool_snapshots": pool,
            "pass_plan_sha256": plans,
            "data": data,
            "row_identity": row_identity,
            "opponent_registry": registry_details,
        },
        "planned_tail": planned_tail,
        "collector_quiescence": collector_quiescence,
        "probe_execution_count": 0,
        "read_current_ratings": False,
        "strength_evidence": False,
        "deployment_policy_value": False,
    }
    receipt = {**unsigned, "receipt_sha256": _canonical_sha256(unsigned)}
    migrated = dict(previous)
    migrated["resume_contract"] = new_contract
    migrated[MIGRATION_KEY] = receipt
    return migrated, receipt, previous_raw


def _revalidate_source_evidence(
    source_dir: Path, receipt: dict[str, Any]
) -> None:
    if list(source_dir.glob("_tmp*")):
        raise RuntimeError("unfinished probe temporary files block capacity migration")
    boundary = _integer(
        receipt.get("boundary_pass"), field="capacity migration boundary", minimum=1
    )
    previous_details = receipt.get("previous_manifest") or {}
    try:
        previous_raw = base64.b64decode(
            previous_details["bytes_base64"], validate=True
        )
        previous = json.loads(previous_raw)
        old_contract = previous["resume_contract"]
    except (
        KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError
    ) as exc:
        raise RuntimeError("capacity migration source receipt is invalid") from exc
    if not isinstance(old_contract, dict):
        raise RuntimeError("capacity migration source contract is invalid")
    after = receipt.get("after") or {}
    expected_intent = _migration_intent(
        source_dir, boundary=boundary,
        workers=_integer(after.get("workers"), field="intent workers"),
        probe_workers=_integer(
            after.get("probe_workers"), field="intent probe_workers"
        ),
        max_active_native_matches=_integer(
            after.get("max_active_native_matches"), field="intent matches"
        ),
        capacity_total_slots=_integer(
            after.get("capacity_total_slots"), field="intent total slots"
        ),
        capacity_first_slot=_integer(
            after.get("capacity_first_slot"), field="intent first slot"
        ),
    )
    _validate_migration_intent(
        receipt["collector_quiescence"]["running_unit"], expected_intent
    )
    prefix = receipt.get("completed_prefix") or {}
    state, state_details = schema6_migration._state_prefix(source_dir, boundary)
    if state_details != prefix.get("collector_state"):
        raise RuntimeError("collector state changed before migration publish")
    pool = schema6_migration._pool_prefix(
        source_dir / "pool_snapshots.jsonl", boundary
    )
    if pool != prefix.get("pool_snapshots"):
        raise RuntimeError("pool snapshots changed before migration publish")
    plans, tail = _plan_prefix(
        source_dir,
        boundary=boundary,
        contract=old_contract,
        collection_manifest=previous,
    )
    if plans != prefix.get("pass_plan_sha256") or tail != receipt.get(
        "planned_tail"
    ):
        raise RuntimeError("pass plans changed before migration publish")
    data, identity = _data_prefix_with_identity(source_dir, state, exact=True)
    if data != prefix.get("data") or identity != prefix.get("row_identity"):
        raise RuntimeError("collector data changed before migration publish")
    registry, registry_details = _registry_snapshot(source_dir)
    if registry_details != prefix.get("opponent_registry"):
        raise RuntimeError("opponent registry changed before migration publish")
    schema6_migration._verify_completed_opponent_snapshots(
        source_dir, boundary=boundary, registry=registry
    )
    if receipt.get("code_artifacts") != _current_code_artifacts(old_contract):
        raise RuntimeError("capacity migration code changed before publish")
    quiescence = _verify_collector_quiescence(
        str((receipt.get("collector_quiescence") or {}).get("collector_unit") or ""),
        source_dir,
        running_unit=(receipt.get("collector_quiescence") or {}).get("running_unit"),
    )
    if quiescence != receipt.get("collector_quiescence"):
        raise RuntimeError("collector quiescence changed before migration publish")


def _atomic_write(path: Path, payload: dict[str, Any], expected_raw: bytes) -> None:
    if path.read_bytes() != expected_raw:
        raise RuntimeError("collection manifest changed before migration publish")
    raw = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    fd, name = tempfile.mkstemp(
        prefix=f".{path.name}.capacity-migration-", dir=path.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        receipt = payload.get(MIGRATION_KEY)
        if not isinstance(receipt, dict):
            raise RuntimeError("capacity migration receipt is missing")
        _revalidate_source_evidence(path.parent, receipt)
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
        raise RuntimeError("published capacity migration changed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--expected-boundary", required=True, type=int)
    parser.add_argument("--workers", required=True, type=int)
    parser.add_argument("--probe-workers", required=True, type=int)
    parser.add_argument(
        "--max-active-native-matches", required=True, type=int
    )
    parser.add_argument("--capacity-total-slots", required=True, type=int)
    parser.add_argument("--capacity-first-slot", required=True, type=int)
    parser.add_argument("--collector-unit", required=True)
    parser.add_argument("--capture-running-unit-receipt", type=Path)
    parser.add_argument("--running-unit-receipt", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    source_dir = args.source_dir.resolve()
    if (
        args.workers != TARGET_WORKERS
        or args.probe_workers != TARGET_PROBE_WORKERS
        or args.max_active_native_matches != TARGET_MAX_ACTIVE_NATIVE_MATCHES
        or args.capacity_total_slots != TARGET_CAPACITY_TOTAL_SLOTS
        or args.capacity_first_slot != TARGET_CAPACITY_FIRST_SLOT
    ):
        raise SystemExit("capture requires the reviewed schema-7 topology")
    if args.capture_running_unit_receipt is not None:
        if args.apply or args.running_unit_receipt is not None:
            raise SystemExit("running-unit capture cannot publish a migration")
        running_unit, quiescence = systemd_quiescence.stop_bound_collector(
            args.collector_unit, source_dir,
            lambda: _migration_intent(
                source_dir, boundary=args.expected_boundary,
                workers=args.workers, probe_workers=args.probe_workers,
                max_active_native_matches=args.max_active_native_matches,
                capacity_total_slots=args.capacity_total_slots,
                capacity_first_slot=args.capacity_first_slot,
            ),
            args.capture_running_unit_receipt,
        )
        print(json.dumps({
            "status": "collector_stopped_and_bound",
            "collector_unit": args.collector_unit,
            "main_pid": running_unit["main_pid"],
            "unit_disposition": quiescence["unit_disposition"],
            "receipt_sha256": running_unit["receipt_sha256"],
            "strength_evidence": False,
            "deployment_policy_value": False,
        }, indent=2, sort_keys=True))
        return 0
    intent = _migration_intent(
        source_dir, boundary=args.expected_boundary, workers=args.workers,
        probe_workers=args.probe_workers,
        max_active_native_matches=args.max_active_native_matches,
        capacity_total_slots=args.capacity_total_slots,
        capacity_first_slot=args.capacity_first_slot,
    )
    if args.running_unit_receipt is None:
        raise SystemExit("--running-unit-receipt is required after bound stop")
    try:
        running_unit = json.loads(args.running_unit_receipt.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit("cannot read the running-unit receipt") from exc
    _validate_running_unit_receipt(running_unit, require_current_machine=True)
    _validate_migration_intent(running_unit, intent)
    lock_path = source_dir / ".collector.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SystemExit(
                "collector is running; migrate only at an atomic boundary"
            ) from exc
        try:
            collector_quiescence = _verify_collector_quiescence(
                args.collector_unit, source_dir, running_unit=running_unit
            )
            migrated, receipt, previous_raw = build_migration(
                source_dir,
                boundary=args.expected_boundary,
                workers=args.workers,
                probe_workers=args.probe_workers,
                max_active_native_matches=args.max_active_native_matches,
                capacity_total_slots=args.capacity_total_slots,
                capacity_first_slot=args.capacity_first_slot,
                collector_quiescence=collector_quiescence,
            )
            if args.apply:
                _atomic_write(
                    source_dir / "collection_manifest.json",
                    migrated,
                    previous_raw,
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
        "max_active_native_matches": receipt["after"][
            "max_active_native_matches"
        ],
        "capacity_first_slot": receipt["after"]["capacity_first_slot"],
        "capacity_total_slots": receipt["after"]["capacity_total_slots"],
        "planned_tail": receipt["planned_tail"],
        "probe_execution_count": 0,
        "read_current_ratings": False,
        "strength_evidence": False,
        "deployment_policy_value": False,
        "receipt_sha256": receipt["receipt_sha256"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
