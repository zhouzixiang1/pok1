#!/usr/bin/env python3
"""Freeze an independent collection into five opponent-disjoint evidence roles."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Any


TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from freeze_oppmodel_dataset import (  # noqa: E402
    _read_jsonl_snapshot,
    _verify_input_snapshot,
    _write_jsonl,
)
import longrun_collect_oppmodel as collector  # noqa: E402
import migrate_oppmodel_collector_concurrency as concurrency_migration  # noqa: E402
from match_outcome_schema import (  # noqa: E402
    derive_match_outcome_supervision,
    match_outcome_metadata,
)
from opponent_response_schema import (  # noqa: E402
    OPPONENT_RESPONSE_SCHEMA,
    annotate_response_rows,
    response_schema_metadata,
)
from sampling_weights import decision_sampling_weight  # noqa: E402


SCHEMA = "opponent_role_dataset_v4"
STRATEGY_CONTEXT_RUNTIME_MODE = "zero_vector_training_aligned_v1"
SOURCE_SPLITS = ("train", "val", "held_out")
PREFIXES = ("cf", "opponent_actions")
EVIDENCE_ROLES = (
    "train",
    "early_stop",
    "model_calibration",
    "policy_selection",
    "policy_gate",
)
EXPLICIT_ROLES = EVIDENCE_ROLES[1:]
EXPECTED_SOURCE_SPLIT = {
    "early_stop": "train",
    "model_calibration": "val",
    "policy_selection": "val",
    "policy_gate": "held_out",
}
LEGACY_RECOVERY_MODE = "complete_schema4_tail_to_schema5"
LEGACY_RECOVERY_SCHEMA_VERSION = 1
LEGACY_RECOVERY_TARGET_SCHEMA_VERSION = 5
LEGACY_PLAN_FIELDS = {"pass", "seed_scheme", "tasks"}
PLAN_TASK_FIELDS = {
    "name", "opponent_path", "split", "hands", "deck_seed_base",
    "deck_seed_last", "bot_seed_base", "tag_commit",
    "tag_directory_sha256", "execution_matches_generation_tag",
    "source_path", "source_checkout_commit", "execution_directory_sha256",
}
POOL_TASK_FIELDS = {
    "name", "split", "tag_commit", "execution_directory_sha256",
    "source_checkout_commit", "glicko", "deck_seed_base",
    "deck_seed_last", "bot_seed_base",
}
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
BOT_NAME = re.compile(r"^national_v[0-9]+$")


def _concurrency_migration_contract(
    collection_manifest: dict[str, Any], *, completed_passes: int,
    source_dir: Path, validate_data_prefix: bool,
) -> tuple[int, dict[str, Any]] | None:
    """Replay the exact schema-5 -> schema-6 execution-only migration."""
    receipt = collection_manifest.get("concurrency_migration")
    if receipt is None:
        return None
    required = {
        "schema_version", "mode", "boundary_pass", "migration_tool_sha256",
        "previous_manifest", "legacy_recovery_receipt_sha256", "before",
        "after", "completed_prefix", "probe_execution_count",
        "read_current_ratings", "strength_evidence", "deployment_policy_value",
        "receipt_sha256",
    }
    if not isinstance(receipt, dict) or set(receipt) != required:
        raise RuntimeError("concurrency migration receipt fields changed")
    unsigned = dict(receipt)
    recorded = _require_digest(
        unsigned.pop("receipt_sha256"), field="concurrency migration receipt"
    )
    if recorded != _canonical_sha256(unsigned):
        raise RuntimeError("concurrency migration receipt digest mismatch")
    migration_tool = TOOLS / "migrate_oppmodel_collector_concurrency.py"
    if (
        receipt.get("schema_version")
        != concurrency_migration.MIGRATION_SCHEMA_VERSION
        or receipt.get("mode") != concurrency_migration.MIGRATION_MODE
        or receipt.get("migration_tool_sha256") != _sha256(migration_tool)
        or receipt.get("probe_execution_count") != 0
        or receipt.get("read_current_ratings") is not False
        or receipt.get("strength_evidence") is not False
        or receipt.get("deployment_policy_value") is not False
    ):
        raise RuntimeError("concurrency migration receipt is not authoritative")
    boundary = _integer(receipt.get("boundary_pass"), field="migration boundary", minimum=1)
    if completed_passes < boundary:
        raise RuntimeError("concurrency migration boundary exceeds completed prefix")
    previous_details = receipt.get("previous_manifest")
    if not isinstance(previous_details, dict) or set(previous_details) != {
        "bytes", "sha256", "bytes_base64",
    }:
        raise RuntimeError("previous collection manifest receipt changed")
    try:
        previous_raw = base64.b64decode(previous_details["bytes_base64"], validate=True)
        previous_manifest = json.loads(previous_raw)
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("previous collection manifest receipt is invalid") from exc
    if (
        not isinstance(previous_manifest, dict)
        or len(previous_raw)
        != _integer(previous_details.get("bytes"), field="previous manifest bytes")
        or hashlib.sha256(previous_raw).hexdigest()
        != _require_digest(
            previous_details.get("sha256"), field="previous manifest sha256"
        )
        or "concurrency_migration" in previous_manifest
    ):
        raise RuntimeError("previous collection manifest binding changed")
    old_contract = previous_manifest.get("resume_contract")
    current_contract = collection_manifest.get("resume_contract")
    if not isinstance(old_contract, dict) or not isinstance(current_contract, dict):
        raise RuntimeError("concurrency migration contracts are missing")
    changed = {
        key
        for key in set(old_contract) | set(current_contract)
        if old_contract.get(key) != current_contract.get(key)
    }
    if changed != concurrency_migration.ALLOWED_CONTRACT_CHANGES:
        raise RuntimeError("concurrency migration changed semantic collection fields")
    if (
        old_contract.get("schema_version")
        != concurrency_migration.SOURCE_SCHEMA_VERSION
        or old_contract.get("workers") != concurrency_migration.SOURCE_WORKERS
        or old_contract.get("probe_workers")
        != concurrency_migration.SOURCE_PROBE_WORKERS
        or current_contract.get("schema_version")
        != concurrency_migration.TARGET_SCHEMA_VERSION
        or current_contract.get("workers") != concurrency_migration.TARGET_WORKERS
        or current_contract.get("probe_workers")
        != concurrency_migration.TARGET_PROBE_WORKERS
    ):
        raise RuntimeError("concurrency migration topology changed")
    before = receipt.get("before")
    after = receipt.get("after")
    if not isinstance(before, dict) or set(before) != {
        "resume_contract_sha256", "schema_version", "workers", "probe_workers",
        "collector_sha256",
    }:
        raise RuntimeError("concurrency migration before binding changed")
    if not isinstance(after, dict) or set(after) != {
        "resume_contract_sha256", "schema_version", "workers", "probe_workers",
        "collector_sha256", "max_concurrent_native_matches",
    }:
        raise RuntimeError("concurrency migration after binding changed")
    expected_before = {
        "resume_contract_sha256": _canonical_sha256(old_contract),
        "schema_version": old_contract["schema_version"],
        "workers": old_contract["workers"],
        "probe_workers": old_contract["probe_workers"],
        "collector_sha256": old_contract["collector_sha256"],
    }
    expected_after = {
        "resume_contract_sha256": _canonical_sha256(current_contract),
        "schema_version": current_contract["schema_version"],
        "workers": current_contract["workers"],
        "probe_workers": current_contract["probe_workers"],
        "collector_sha256": current_contract["collector_sha256"],
        "max_concurrent_native_matches": (
            current_contract["workers"] * current_contract["probe_workers"]
        ),
    }
    if before != expected_before or after != expected_after:
        raise RuntimeError("concurrency migration contract digest changed")
    legacy = previous_manifest.get("legacy_recovery")
    if (
        not isinstance(legacy, dict)
        or receipt.get("legacy_recovery_receipt_sha256")
        != legacy.get("receipt_sha256")
        or (legacy.get("after") or {}).get("collector_sha256")
        != old_contract.get("collector_sha256")
        or (legacy.get("after") or {}).get("collector_schema_version")
        != old_contract.get("schema_version")
    ):
        raise RuntimeError("concurrency migration/legacy recovery chain changed")
    reconstructed = dict(previous_manifest)
    reconstructed["resume_contract"] = current_contract
    reconstructed["concurrency_migration"] = receipt
    if reconstructed != collection_manifest:
        raise RuntimeError("collection manifest changed outside concurrency migration")
    prefix = receipt.get("completed_prefix")
    if not isinstance(prefix, dict) or set(prefix) != {
        "collector_state", "pool_snapshots", "pass_plan_sha256", "data",
    }:
        raise RuntimeError("concurrency migration prefix fields changed")
    state_details = prefix.get("collector_state")
    if not isinstance(state_details, dict) or set(state_details) != {
        "bytes", "sha256", "bytes_base64",
    }:
        raise RuntimeError("concurrency migration state receipt changed")
    try:
        state_raw = base64.b64decode(state_details["bytes_base64"], validate=True)
        state = json.loads(state_raw)
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("concurrency migration state receipt is invalid") from exc
    if (
        not isinstance(state, dict)
        or len(state_raw) != _integer(state_details.get("bytes"), field="state bytes")
        or hashlib.sha256(state_raw).hexdigest()
        != _require_digest(state_details.get("sha256"), field="state sha256")
        or state.get("completed_passes") != boundary
    ):
        raise RuntimeError("concurrency migration state boundary changed")
    pool_details = prefix.get("pool_snapshots")
    if not isinstance(pool_details, dict) or set(pool_details) != {
        "rows", "bytes", "sha256",
    }:
        raise RuntimeError("concurrency migration pool receipt changed")
    pool_path = source_dir / "pool_snapshots.jsonl"
    if not pool_path.exists():
        pool_path = source_dir / "pool_snapshots.completed.jsonl"
    pool_prefix = _prefix_details(pool_path, boundary)
    if (
        _integer(pool_details.get("rows"), field="migration pool rows") != boundary
        or _integer(pool_details.get("bytes"), field="migration pool bytes")
        != pool_prefix["bytes"]
        or pool_prefix["sha256"]
        != _require_digest(pool_details.get("sha256"), field="migration pool sha256")
    ):
        raise RuntimeError("concurrency migration pool prefix changed")
    plans = prefix.get("pass_plan_sha256")
    expected_names = {f"pass_{index:04d}.json" for index in range(1, boundary + 1)}
    if not isinstance(plans, dict) or set(plans) != expected_names:
        raise RuntimeError("concurrency migration plan prefix changed")
    for name, digest in plans.items():
        if _sha256(source_dir / "pass_plans" / name) != _require_digest(
            digest, field=f"migration plan {name}"
        ):
            raise RuntimeError(f"concurrency migration plan changed: {name}")
    data = prefix.get("data")
    expected_data = {
        f"{prefix_name}_{split}.jsonl"
        for prefix_name in PREFIXES for split in SOURCE_SPLITS
    }
    if not isinstance(data, dict) or set(data) != expected_data:
        raise RuntimeError("concurrency migration data prefix fields changed")
    state_fields = {"cf": "total_rows", "opponent_actions": "total_behavior_rows"}
    for prefix_name, state_field in state_fields.items():
        totals = state.get(state_field)
        if not isinstance(totals, dict) or set(totals) != set(SOURCE_SPLITS):
            raise RuntimeError("concurrency migration state totals changed")
        for split in SOURCE_SPLITS:
            name = f"{prefix_name}_{split}.jsonl"
            details = data[name]
            rows = _integer(totals[split], field=f"{name}.rows")
            if not isinstance(details, dict) or set(details) != {
                "rows", "bytes", "sha256",
            } or details.get("rows") != rows or _integer(
                details.get("bytes"), field=f"{name}.bytes"
            ) < 0:
                raise RuntimeError(f"concurrency migration data receipt changed: {name}")
            _require_digest(details.get("sha256"), field=f"{name}.sha256")
            if validate_data_prefix:
                actual = _prefix_details(source_dir / name, rows)
                if (
                    actual["bytes"] != details["bytes"]
                    or actual["sha256"] != details["sha256"]
                ):
                    raise RuntimeError(
                        f"concurrency migration data prefix changed: {name}"
                    )
    return boundary, dict(old_contract)

def strategy_context_is_absent(row: dict[str, Any]) -> bool:
    """Collector rows for this epoch must carry no runtime strategy context."""
    containers = [row]
    request = row.get("request")
    if isinstance(request, dict):
        containers.append(request)
    for container in containers:
        if (
            "strategy_context_available" in container
            and container["strategy_context_available"] is not False
        ):
            return False
        for field in (
            "strategy_context",
            "strategy_context_features",
            "strategy_context_schema",
        ):
            if field in container:
                return False
    return True


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json_snapshot(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"JSON object required: {path}")
    return payload, hashlib.sha256(raw).hexdigest()


def _integer(value: Any, *, field: str, minimum: int | None = None) -> int:
    if isinstance(value, bool):
        raise RuntimeError(f"{field} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RuntimeError(f"{field} must be an integer") from exc
    if str(value).strip() not in {str(number), f"{number}.0"} and not isinstance(
        value, int
    ):
        try:
            if float(value) != number:
                raise RuntimeError(f"{field} must be an integer")
        except (TypeError, ValueError, OverflowError):
            raise RuntimeError(f"{field} must be an integer") from None
    if minimum is not None and number < minimum:
        raise RuntimeError(f"{field} must be >= {minimum}")
    return number


def _opponent(row: dict[str, Any]) -> str:
    return str(row.get("_opponent_label") or row.get("opponent") or "").strip()


def _cluster_key(row: dict[str, Any]) -> tuple[str, int, int]:
    opponent = _opponent(row)
    if not opponent:
        raise RuntimeError("row is missing opponent label")
    return (
        opponent,
        _integer(row.get("deck_seed_base"), field="deck_seed_base", minimum=0),
        _integer(row.get("bot_seed_base"), field="bot_seed_base", minimum=0),
    )


def _collector_boundary(
    source_dir: Path,
) -> tuple[dict[str, Any], str, int, dict[str, dict[str, int]]]:
    state_path = source_dir / "collector_state.json"
    state, digest = _load_json_snapshot(state_path)
    completed = _integer(
        state.get("completed_passes"), field="completed_passes", minimum=1
    )
    try:
        limits = {
            "cf": {
                split: _integer(
                    state["total_rows"][split],
                    field=f"total_rows.{split}",
                    minimum=0,
                )
                for split in SOURCE_SPLITS
            },
            "opponent_actions": {
                split: _integer(
                    state["total_behavior_rows"][split],
                    field=f"total_behavior_rows.{split}",
                    minimum=0,
                )
                for split in SOURCE_SPLITS
            },
        }
    except KeyError as exc:
        raise RuntimeError(f"collector state is missing {exc}") from exc
    return state, digest, completed, limits


def _canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def _require_digest(value: Any, *, field: str, length: int = 64) -> str:
    text = str(value or "")
    pattern = HEX64 if length == 64 else HEX40
    if not pattern.fullmatch(text):
        raise RuntimeError(f"{field} is not a lowercase digest")
    return text


def _prefix_sha256(path: Path, rows: int) -> str:
    return str(_prefix_details(path, rows)["sha256"])


def _prefix_details(path: Path, rows: int) -> dict[str, int | str]:
    digest = hashlib.sha256()
    count = 0
    size = 0
    with path.open("rb") as handle:
        while count < rows:
            line = handle.readline()
            if not line or not line.endswith(b"\n"):
                raise RuntimeError(f"JSONL prefix is incomplete: {path}")
            digest.update(line)
            size += len(line)
            count += 1
    return {"rows": count, "bytes": size, "sha256": digest.hexdigest()}


def _legacy_recovery_contract(
    collection_manifest: dict[str, Any],
    *,
    completed_passes: int,
    source_dir: Path,
    validate_data_prefix: bool,
    resume_contract: dict[str, Any] | None = None,
) -> tuple[int, int, dict[str, str]] | None:
    receipt = collection_manifest.get("legacy_recovery")
    if receipt is None:
        return None
    required = {
        "schema_version", "mode", "completed_prefix_pass", "recovered_pass",
        "expectations_sha256", "recovery_tool_sha256", "reviewed_hashes",
        "completed_plan_sha256", "before", "archived_ratings", "tail",
        "after", "probe_execution_count", "read_current_ratings",
        "strength_evidence", "deployment_policy_value", "receipt_sha256",
    }
    if not isinstance(receipt, dict) or set(receipt) != required:
        raise RuntimeError("legacy recovery receipt fields changed")
    unsigned = dict(receipt)
    recorded = _require_digest(
        unsigned.pop("receipt_sha256"), field="legacy recovery receipt"
    )
    if recorded != _canonical_sha256(unsigned):
        raise RuntimeError("legacy recovery receipt digest mismatch")
    recovery_tool = TOOLS / "recover_legacy_oppmodel_collection.py"
    if (
        receipt.get("schema_version") != LEGACY_RECOVERY_SCHEMA_VERSION
        or receipt.get("mode") != LEGACY_RECOVERY_MODE
        or receipt.get("recovery_tool_sha256") != _sha256(recovery_tool)
        or receipt.get("probe_execution_count") != 0
        or receipt.get("read_current_ratings") is not False
        or receipt.get("strength_evidence") is not False
        or receipt.get("deployment_policy_value") is not False
    ):
        raise RuntimeError("legacy recovery receipt is not authoritative")
    prefix = _integer(
        receipt.get("completed_prefix_pass"),
        field="completed_prefix_pass", minimum=1,
    )
    recovered = _integer(
        receipt.get("recovered_pass"), field="recovered_pass", minimum=2
    )
    if recovered != prefix + 1 or completed_passes < recovered:
        raise RuntimeError("legacy recovery boundary is invalid")
    plans = receipt.get("completed_plan_sha256")
    expected_names = {f"pass_{index:04d}.json" for index in range(1, prefix + 1)}
    if not isinstance(plans, dict) or set(plans) != expected_names:
        raise RuntimeError("legacy recovery completed-plan prefix changed")
    plan_hashes = {
        name: _require_digest(value, field=f"legacy plan {name}")
        for name, value in plans.items()
    }
    reviewed = receipt.get("reviewed_hashes")
    before = receipt.get("before")
    after = receipt.get("after")
    archived = receipt.get("archived_ratings")
    if not all(isinstance(value, dict) for value in (reviewed, before, after, archived)):
        raise RuntimeError("legacy recovery receipt sections are invalid")
    required_reviewed = {
        "collection_manifest", "collector_state", "pool_snapshots",
        "recovery_plan", "legacy_collector", "current_collector",
        "identity_migration", "archived_ratings", "opponent_registry",
        *(f"{prefix_name}_{split}.jsonl"
          for prefix_name in ("cf", "opponent_actions")
          for split in SOURCE_SPLITS),
    }
    if (
        set(reviewed) != required_reviewed
        or set(before) != {
            "collection_manifest_sha256", "collector_state_sha256",
            "pool_snapshots_sha256", "recovery_plan_sha256",
            "legacy_collector_sha256",
        }
        or set(archived) != {
            "ratings_sha256", "ratings_snapshot_sha256",
            "identity_migration_sha256",
        }
        or set(after) != {
            "collector_schema_version", "collector_sha256",
            "pass_plan_schema_version", "recovery_plan_sha256",
            "pool_snapshots_sha256", "collector_state_sha256",
            "total_rows", "total_behavior_rows",
        }
        or not isinstance(receipt.get("tail"), dict)
        or not receipt["tail"]
    ):
        raise RuntimeError("legacy recovery receipt sections changed")
    for field in (
        "expectations_sha256", "recovery_tool_sha256",
    ):
        _require_digest(receipt.get(field), field=field)
    for key, value in reviewed.items():
        _require_digest(value, field=f"reviewed_hashes.{key}")
    for section_name, section in (
        ("before", before),
        ("after", after), ("archived_ratings", archived),
    ):
        for key, value in section.items():
            if key.endswith("sha256"):
                _require_digest(value, field=f"{section_name}.{key}")
    contract = (
        resume_contract
        if resume_contract is not None
        else collection_manifest.get("resume_contract") or {}
    )
    if (
        after.get("collector_schema_version")
        != LEGACY_RECOVERY_TARGET_SCHEMA_VERSION
        or contract.get("schema_version")
        != LEGACY_RECOVERY_TARGET_SCHEMA_VERSION
        or after.get("pass_plan_schema_version") != collector.PASS_PLAN_SCHEMA_VERSION
        or after.get("collector_sha256") != contract.get("collector_sha256")
    ):
        raise RuntimeError("legacy recovery current-collector binding changed")
    reviewed_pool = _require_digest(
        reviewed.get("pool_snapshots"), field="reviewed_hashes.pool_snapshots"
    )
    reviewed_links = {
        "collection_manifest": before.get("collection_manifest_sha256"),
        "collector_state": before.get("collector_state_sha256"),
        "pool_snapshots": before.get("pool_snapshots_sha256"),
        "recovery_plan": before.get("recovery_plan_sha256"),
        "legacy_collector": before.get("legacy_collector_sha256"),
        "current_collector": after.get("collector_sha256"),
        "identity_migration": archived.get("identity_migration_sha256"),
        "archived_ratings": archived.get("ratings_sha256"),
    }
    if any(reviewed.get(name) != expected for name, expected in reviewed_links.items()):
        raise RuntimeError("legacy recovery reviewed-artifact links changed")
    pool_path = source_dir / "pool_snapshots.jsonl"
    if not pool_path.exists():
        pool_path = source_dir / "pool_snapshots.completed.jsonl"
    if _prefix_sha256(pool_path, prefix) != reviewed_pool:
        raise RuntimeError("legacy completed pool prefix changed")
    if _prefix_sha256(pool_path, recovered) != after.get("pool_snapshots_sha256"):
        raise RuntimeError("legacy recovered pool prefix changed")
    totals = {
        "cf": after.get("total_rows"),
        "opponent_actions": after.get("total_behavior_rows"),
    }
    for prefix_name, split_totals in totals.items():
        if not isinstance(split_totals, dict) or set(split_totals) != set(SOURCE_SPLITS):
            raise RuntimeError("legacy recovery row totals changed")
        for split in SOURCE_SPLITS:
            filename = f"{prefix_name}_{split}.jsonl"
            rows = _integer(
                split_totals[split], field=f"{filename}.rows", minimum=0
            )
            if validate_data_prefix:
                expected = _require_digest(
                    reviewed.get(filename), field=f"reviewed_hashes.{filename}"
                )
                if _prefix_sha256(source_dir / filename, rows) != expected:
                    raise RuntimeError(f"legacy recovered data prefix changed: {filename}")
    if validate_data_prefix:
        registry_path = source_dir / "opponent_snapshots" / "registry.json"
        if (
            not registry_path.is_file()
            or _sha256(registry_path) != reviewed.get("opponent_registry")
        ):
            raise RuntimeError("legacy recovered opponent registry changed")
    return prefix, recovered, plan_hashes


def _legacy_plan_tasks(
    payload: dict[str, Any], *, pass_index: int, contract: dict[str, Any]
) -> list[dict[str, Any]]:
    if set(payload) != LEGACY_PLAN_FIELDS:
        raise RuntimeError(f"legacy pass plan fields changed at pass {pass_index}")
    rows = payload.get("tasks")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"legacy pass plan has no tasks at pass {pass_index}")
    hands = _integer(contract.get("hands"), field="hands", minimum=1)
    val = set(contract.get("val_opponents") or [])
    held = set(contract.get("held_out_opponents") or [])
    seen_names: set[str] = set()
    seen_paths: set[str] = set()
    validated = []
    for task_index, raw in enumerate(rows):
        if not isinstance(raw, dict) or set(raw) != PLAN_TASK_FIELDS:
            raise RuntimeError(f"legacy task fields changed at pass {pass_index}")
        name = str(raw.get("name") or "")
        split = raw.get("split")
        opponent_path = str(raw.get("opponent_path") or "")
        source_path = str(raw.get("source_path") or "")
        expected_split = "held_out" if name in held else "val" if name in val else "train"
        if (
            not BOT_NAME.fullmatch(name) or name in seen_names
            or split != expected_split or not Path(opponent_path).is_absolute()
            or Path(opponent_path).name != name or opponent_path in seen_paths
            or not Path(source_path).is_absolute()
            or not isinstance(raw.get("execution_matches_generation_tag"), bool)
        ):
            raise RuntimeError(f"legacy task identity changed at pass {pass_index}")
        seen_names.add(name)
        seen_paths.add(opponent_path)
        expected_deck = collector._deck_seed_for_task(
            root=_integer(contract.get("deck_seed_base"), field="deck_seed_base"),
            pass_index=pass_index - 1, task_index=task_index, hands=hands,
            guard=_integer(contract.get("deck_seed_guard"), field="deck_seed_guard"),
        )
        expected_bot = collector._bot_seed_for_task(
            root=_integer(contract.get("bot_seed_base"), field="bot_seed_base"),
            pass_index=pass_index - 1, task_index=task_index,
        )
        if (
            _integer(raw.get("hands"), field="task.hands") != hands
            or _integer(raw.get("deck_seed_base"), field="task.deck_seed_base")
            != expected_deck
            or _integer(raw.get("deck_seed_last"), field="task.deck_seed_last")
            != expected_deck + hands - 1
            or _integer(raw.get("bot_seed_base"), field="task.bot_seed_base")
            != expected_bot
        ):
            raise RuntimeError(f"legacy task seed block changed at pass {pass_index}")
        _require_digest(raw.get("tag_commit"), field="tag_commit", length=40)
        _require_digest(
            raw.get("source_checkout_commit"), field="source_checkout_commit", length=40
        )
        _require_digest(raw.get("tag_directory_sha256"), field="tag_directory_sha256")
        _require_digest(
            raw.get("execution_directory_sha256"),
            field="execution_directory_sha256",
        )
        validated.append(dict(raw))
    return validated


def _validate_pool_row(
    row: dict[str, Any], *, pass_index: int, tasks: list[dict[str, Any]],
    contract: dict[str, Any], ratings_snapshot: dict[str, Any] | None,
    rating_rows: dict[str, dict[str, float]] | None,
) -> None:
    hands = int(contract["hands"])
    fractions = [float(value) for value in contract["hand_windows"]]
    min_hand = max(1, min(
        hands, 1 + int((hands - 1) * fractions[(pass_index - 1) % len(fractions)])
    ))
    if (
        row.get("pass") != pass_index
        or row.get("ratings_path") != str(Path(str(contract["ratings_path"])).resolve())
        or not HEX64.fullmatch(str(row.get("ratings_sha256") or ""))
        or row.get("min_hand") != min_hand or row.get("hands") != hands
        or row.get("workers") != contract["workers"]
        or row.get("probe_workers") != contract["probe_workers"]
        or row.get("decision_sampling") != contract["decision_sampling"]
    ):
        raise RuntimeError(f"pool snapshot contract changed at pass {pass_index}")
    if ratings_snapshot is None:
        if "ratings_snapshot_sha256" in row:
            raise RuntimeError("legacy pool snapshot carries unproven ratings evidence")
    elif (
        row.get("ratings_sha256") != ratings_snapshot.get("ratings_sha256")
        or row.get("ratings_snapshot_sha256")
        != ratings_snapshot.get("snapshot_sha256")
    ):
        raise RuntimeError(f"pool ratings binding changed at pass {pass_index}")
    pool = row.get("pool")
    if not isinstance(pool, list) or len(pool) != len(tasks):
        raise RuntimeError(f"pool task count changed at pass {pass_index}")
    compared = (
        "name", "split", "tag_commit", "execution_directory_sha256",
        "source_checkout_commit", "deck_seed_base", "deck_seed_last",
        "bot_seed_base",
    )
    for entry, task in zip(pool, tasks, strict=True):
        if not isinstance(entry, dict) or set(entry) != POOL_TASK_FIELDS:
            raise RuntimeError(f"pool task fields changed at pass {pass_index}")
        if any(entry.get(field) != task.get(field) for field in compared):
            raise RuntimeError(f"pool/plan task binding changed at pass {pass_index}")
        if rating_rows is not None and entry.get("glicko") != rating_rows.get(task["name"]):
            raise RuntimeError(f"pool rating row changed at pass {pass_index}")
        glicko = entry.get("glicko")
        if glicko is not None and (
            not isinstance(glicko, dict)
            or any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value)) for value in glicko.values()
            )
        ):
            raise RuntimeError(f"pool rating is invalid at pass {pass_index}")


def _completed_plans(
    source_dir: Path,
    completed_passes: int,
    *,
    collection_manifest: dict[str, Any],
    pool_rows: list[dict[str, Any]],
    registry: dict[str, Any],
    validate_data_prefix: bool = False,
) -> tuple[dict[tuple[str, int, int], dict[str, Any]], dict[str, str]]:
    resume_contract = collection_manifest.get("resume_contract")
    if (
        not isinstance(resume_contract, dict)
        or resume_contract.get("schema_version")
        != collector.COLLECTION_CONTRACT_SCHEMA_VERSION
    ):
        raise RuntimeError("role freeze requires the current collector contract")
    try:
        ratings_path = Path(str(resume_contract["ratings_path"]))
        hands = _integer(resume_contract["hands"], field="hands", minimum=1)
        deck_seed_base = _integer(
            resume_contract["deck_seed_base"], field="deck_seed_base", minimum=0
        )
        deck_seed_guard = _integer(
            resume_contract["deck_seed_guard"], field="deck_seed_guard", minimum=0
        )
        bot_seed_base = _integer(
            resume_contract["bot_seed_base"], field="bot_seed_base", minimum=0
        )
        val_opponents = set(resume_contract["val_opponents"])
        held_out_opponents = set(resume_contract["held_out_opponents"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("role freeze collector contract is incomplete") from exc
    if not ratings_path.is_absolute() or val_opponents & held_out_opponents:
        raise RuntimeError("role freeze collector split contract is invalid")
    current_code = {
        "collector_sha256": _sha256(Path(collector.__file__).resolve()),
        "probe_sha256": _sha256(TOOLS / "native_tcp_counterfactual_probe.py"),
        "cross_hand_sequence_sha256": _sha256(TOOLS / "cross_hand_sequence.py"),
    }
    if any(
        resume_contract.get(field) != digest
        for field, digest in current_code.items()
    ):
        raise RuntimeError("role freeze collector code trust root changed")
    if len(pool_rows) != completed_passes:
        raise RuntimeError("completed pool snapshot count changed")
    migration = _concurrency_migration_contract(
        collection_manifest,
        completed_passes=completed_passes,
        source_dir=source_dir,
        validate_data_prefix=validate_data_prefix,
    )
    migration_boundary = migration[0] if migration else 0
    historical_contract = migration[1] if migration else resume_contract
    legacy = _legacy_recovery_contract(
        collection_manifest,
        completed_passes=completed_passes,
        source_dir=source_dir,
        validate_data_prefix=validate_data_prefix,
        resume_contract=historical_contract,
    )
    legacy_prefix = legacy[0] if legacy else 0
    recovered_pass = legacy[1] if legacy else 0
    legacy_hashes = legacy[2] if legacy else {}
    opponents = registry.get("opponents")
    if registry.get("schema") != "opponent_execution_snapshot_v1" or not isinstance(
        opponents, dict
    ):
        raise RuntimeError("opponent snapshot registry is invalid")
    if legacy is not None:
        archived_sha = collection_manifest["legacy_recovery"]["archived_ratings"][
            "ratings_sha256"
        ]
        if (
            pool_rows[legacy_prefix - 1].get("ratings_sha256") != archived_sha
            or pool_rows[recovered_pass - 1].get("ratings_sha256") != archived_sha
        ):
            raise RuntimeError("legacy archived ratings/pool binding changed")
    tasks: dict[tuple[str, int, int], dict[str, Any]] = {}
    plan_hashes = {}
    intervals = []
    bot_seeds: dict[int, tuple[str, int, int]] = {}
    for pass_index in range(1, completed_passes + 1):
        pass_contract = (
            historical_contract
            if migration is not None and pass_index <= migration_boundary
            else resume_contract
        )
        path = source_dir / "pass_plans" / f"pass_{pass_index:04d}.json"
        payload, digest = _load_json_snapshot(path)
        plan_hashes[path.name] = digest
        if payload.get("seed_scheme") != "disjoint_match_blocks_v1":
            raise RuntimeError(f"unsupported seed scheme in {path}")
        if _integer(payload.get("pass"), field=f"{path.name}.pass") != pass_index:
            raise RuntimeError(f"pass plan index mismatch: {path}")
        ratings_snapshot = None
        rating_rows = None
        if pass_index <= legacy_prefix:
            if digest != legacy_hashes[path.name]:
                raise RuntimeError(f"legacy completed pass plan changed: {path.name}")
            rows = _legacy_plan_tasks(
                payload, pass_index=pass_index, contract=pass_contract
            )
        else:
            try:
                ratings_snapshot, rating_rows, rows = collector._validate_pass_plan(
                    payload,
                    pass_number=pass_index,
                    ratings_path=Path(str(pass_contract["ratings_path"])),
                    hands=_integer(pass_contract["hands"], field="hands", minimum=1),
                    deck_seed_base=_integer(
                        pass_contract["deck_seed_base"], field="deck_seed_base", minimum=0
                    ),
                    deck_seed_guard=_integer(
                        pass_contract["deck_seed_guard"], field="deck_seed_guard", minimum=0
                    ),
                    bot_seed_base=_integer(
                        pass_contract["bot_seed_base"], field="bot_seed_base", minimum=0
                    ),
                    val_opponents=set(pass_contract["val_opponents"]),
                    held_out_opponents=set(pass_contract["held_out_opponents"]),
                )
            except RuntimeError as exc:
                raise RuntimeError(f"invalid current pass plan {path}: {exc}") from exc
            if pass_index == recovered_pass:
                expected = (collection_manifest["legacy_recovery"]["after"])[
                    "recovery_plan_sha256"
                ]
                if digest != expected:
                    raise RuntimeError("recovered pass plan changed")
        _validate_pool_row(
            pool_rows[pass_index - 1], pass_index=pass_index, tasks=rows,
            contract=pass_contract, ratings_snapshot=ratings_snapshot,
            rating_rows=rating_rows,
        )
        for raw in rows:
            if not isinstance(raw, dict):
                raise RuntimeError(f"invalid task in {path}")
            name = str(raw.get("name") or "").strip()
            split = str(raw.get("split") or "")
            if not name or split not in SOURCE_SPLITS:
                raise RuntimeError(f"invalid opponent/split in {path}")
            base = _integer(
                raw.get("deck_seed_base"), field="deck_seed_base", minimum=0
            )
            last = _integer(
                raw.get("deck_seed_last"), field="deck_seed_last", minimum=base
            )
            hands = _integer(raw.get("hands"), field="hands", minimum=1)
            bot_seed = _integer(
                raw.get("bot_seed_base"), field="bot_seed_base", minimum=0
            )
            if last != base + hands - 1:
                raise RuntimeError(f"deck interval does not match hands in {path}")
            key = (name, base, bot_seed)
            if key in tasks:
                raise RuntimeError(f"duplicate match cluster in pass plans: {key}")
            if bot_seed in bot_seeds:
                raise RuntimeError(
                    f"bot seed reused by match clusters: {bot_seeds[bot_seed]} and {key}"
                )
            bot_seeds[bot_seed] = key
            task = dict(raw)
            task["pass"] = pass_index
            entry = opponents.get(name)
            if not isinstance(entry, dict) or any(
                entry.get(registry_field) != raw.get(plan_field)
                for registry_field, plan_field in (
                    ("snapshot_path", "opponent_path"),
                    ("tag_commit", "tag_commit"),
                    ("tag_directory_sha256", "tag_directory_sha256"),
                    ("execution_matches_generation_tag", "execution_matches_generation_tag"),
                    ("source_path", "source_path"),
                    ("source_checkout_commit", "source_checkout_commit"),
                    ("execution_directory_sha256", "execution_directory_sha256"),
                )
            ):
                raise RuntimeError(f"opponent registry/plan mismatch: {name}")
            tasks[key] = task
            intervals.append((base, last, key))
    intervals.sort()
    for previous, current in zip(intervals, intervals[1:]):
        if current[0] <= previous[1]:
            raise RuntimeError(
                f"overlapping deck blocks: {previous[2]} and {current[2]}"
            )
    return tasks, plan_hashes


def _normalize_roles(
    role_opponents: dict[str, set[str]],
    tasks: dict[tuple[str, int, int], dict[str, Any]],
) -> dict[str, set[str]]:
    if set(role_opponents) != set(EXPLICIT_ROLES):
        raise ValueError(f"explicit roles must be {list(EXPLICIT_ROLES)}")
    normalized = {
        role: {str(name).strip() for name in names if str(name).strip()}
        for role, names in role_opponents.items()
    }
    for role, names in normalized.items():
        if not names:
            raise ValueError(f"{role} requires at least one opponent")
    owners = {}
    for role, names in normalized.items():
        for name in names:
            if name in owners:
                raise ValueError(
                    f"opponent assigned to multiple roles: {name} "
                    f"({owners[name]}, {role})"
                )
            owners[name] = role

    source_by_opponent: dict[str, str] = {}
    for task in tasks.values():
        name = str(task["name"])
        split = str(task["split"])
        previous = source_by_opponent.setdefault(name, split)
        if previous != split:
            raise RuntimeError(
                f"opponent crosses source splits: {name} ({previous}, {split})"
            )
    for role, names in normalized.items():
        expected = EXPECTED_SOURCE_SPLIT[role]
        for name in names:
            actual = source_by_opponent.get(name)
            if actual != expected:
                raise ValueError(
                    f"{role} opponent {name} must come from {expected}, got {actual}"
                )
    val = {name for name, split in source_by_opponent.items() if split == "val"}
    assigned_val = normalized["model_calibration"] | normalized["policy_selection"]
    if val != assigned_val:
        raise ValueError(
            f"val opponents must be partitioned exactly: "
            f"unassigned={sorted(val - assigned_val)} unknown={sorted(assigned_val - val)}"
        )
    held = {
        name for name, split in source_by_opponent.items() if split == "held_out"
    }
    if held != normalized["policy_gate"]:
        raise ValueError(
            f"held-out opponents must equal policy_gate: "
            f"unassigned={sorted(held - normalized['policy_gate'])} "
            f"unknown={sorted(normalized['policy_gate'] - held)}"
        )
    train = {
        name for name, split in source_by_opponent.items() if split == "train"
    } - normalized["early_stop"]
    if not train:
        raise ValueError("no training opponents remain after early-stop partition")
    return {"train": train, **normalized}


def _validate_ipw(row: dict[str, Any]) -> None:
    try:
        decision_sampling_weight(row)
    except ValueError as exc:
        raise RuntimeError(f"value row has inconsistent IPW fields: {exc}") from exc


def _validate_rows(
    data: dict[str, dict[str, list[dict[str, Any]]]],
    tasks: dict[tuple[str, int, int], dict[str, Any]],
) -> None:
    seen_value_decisions = set()
    for prefix in PREFIXES:
        for source_split in SOURCE_SPLITS:
            for row in data[prefix][source_split]:
                key = _cluster_key(row)
                task = tasks.get(key)
                if task is None:
                    raise RuntimeError(f"row does not belong to a completed plan: {key}")
                if task["split"] != source_split:
                    raise RuntimeError(f"row source split disagrees with plan: {key}")
                if row.get("status") != "ok":
                    raise RuntimeError(f"non-ok row in completed collection: {key}")
                hands = _integer(
                    row.get("_collection_hands"),
                    field="_collection_hands",
                    minimum=1,
                )
                if hands != int(task["hands"]):
                    raise RuntimeError(f"row hand count disagrees with plan: {key}")
                if prefix == "cf":
                    if int(row.get("invalid_probe_count", -1)) != 0:
                        raise RuntimeError(f"value row contains invalid probes: {key}")
                    _validate_ipw(row)
                    try:
                        derive_match_outcome_supervision(row, required=True)
                    except ValueError as exc:
                        raise RuntimeError(
                            f"value row has invalid 70-hand outcome: {key}: {exc}"
                        ) from exc
                    decision = (
                        key,
                        _integer(row.get("hand"), field="hand", minimum=1),
                        _integer(
                            row.get("hand_decision_index"),
                            field="hand_decision_index",
                            minimum=0,
                        ),
                    )
                    if decision in seen_value_decisions:
                        raise RuntimeError(f"duplicate sampled decision: {decision}")
                    seen_value_decisions.add(decision)


def _verify_snapshots(
    source_dir: Path,
    collection_manifest: dict[str, Any],
    registry: dict[str, Any],
    tasks: dict[tuple[str, int, int], dict[str, Any]],
) -> dict[str, Any]:
    contract = collection_manifest.get("resume_contract") or {}
    candidate_path = Path(str(contract.get("candidate_execution_path") or ""))
    candidate_digest = str(contract.get("candidate_snapshot_sha256") or "")
    if (
        not candidate_path.is_dir()
        or collector._directory_digest(candidate_path) != candidate_digest
    ):
        raise RuntimeError("candidate snapshot digest mismatch")
    opponents = registry.get("opponents")
    if registry.get("schema") != "opponent_execution_snapshot_v1" or not isinstance(
        opponents, dict
    ):
        raise RuntimeError("invalid opponent snapshot registry")
    used = {}
    for task in tasks.values():
        name = str(task["name"])
        entry = opponents.get(name)
        if not isinstance(entry, dict):
            raise RuntimeError(f"opponent snapshot missing from registry: {name}")
        expected = str(task.get("execution_directory_sha256") or "")
        if entry.get("execution_directory_sha256") != expected:
            raise RuntimeError(f"opponent snapshot registry/plan mismatch: {name}")
        if name not in used:
            path = Path(str(entry.get("snapshot_path") or ""))
            if (
                not path.is_dir()
                or collector._directory_digest(path) != expected
            ):
                raise RuntimeError(f"opponent snapshot digest mismatch: {name}")
            used[name] = dict(entry)
    return {
        "candidate": {
            "path": str(candidate_path.resolve()),
            "name": candidate_path.name,
            "sha256": candidate_digest,
        },
        "opponents": used,
    }


def validate_frozen_role_provenance(
    root: Path,
    manifest: dict[str, Any],
    *,
    expected_passes: int,
) -> None:
    """Replay non-outcome freeze metadata before any formal role is opened."""

    root = root.resolve()
    if isinstance(expected_passes, bool) or not isinstance(expected_passes, int):
        raise ValueError("expected collection passes must be an integer")
    if expected_passes < 1:
        raise ValueError("expected collection passes must be positive")
    if manifest.get("freeze_tool_sha256") != _sha256(Path(__file__)):
        raise ValueError("role dataset freeze-tool trust root changed")
    if manifest.get("role_source_contract") != EXPECTED_SOURCE_SPLIT:
        raise ValueError("role dataset source-role contract changed")

    collection_path = root / "collection_manifest.json"
    collection, collection_digest = _load_json_snapshot(collection_path)
    if collection_digest != manifest.get("collection_manifest_sha256"):
        raise ValueError("role dataset collection manifest changed")
    if (
        _integer(
            collection.get("passes_requested"),
            field="passes_requested",
            minimum=1,
        )
        != manifest.get("source_requested_passes")
        or (collection.get("resume_contract") or {}).get("deck_seed_scheme")
        != "disjoint_match_blocks_v1"
    ):
        raise ValueError("role dataset collection contract changed")

    pool_path = root / "pool_snapshots.completed.jsonl"
    pool_rows, pool_details = _read_jsonl_snapshot(pool_path)
    recorded_pool = manifest.get("frozen_pool_snapshot")
    if not isinstance(recorded_pool, dict) or any(
        pool_details[field] != recorded_pool.get(field)
        for field in ("bytes", "rows", "sha256")
    ):
        raise ValueError("completed pool snapshot changed")
    if [row.get("pass") for row in pool_rows] != list(
        range(1, expected_passes + 1)
    ):
        raise ValueError("completed pool snapshot is not the exact pass prefix")

    registry_path = root / "opponent_snapshots.completed.json"
    registry, registry_digest = _load_json_snapshot(registry_path)
    opponents = registry.get("opponents")
    if (
        registry_digest != manifest.get("frozen_opponent_registry_sha256")
        or registry.get("schema") != "opponent_execution_snapshot_v1"
        or not isinstance(opponents, dict)
    ):
        raise ValueError("role dataset opponent registry changed")

    plan_root = root / "pass_plans"
    expected_plan_names = {
        f"pass_{index:04d}.json" for index in range(1, expected_passes + 1)
    }
    try:
        plan_entries = list(plan_root.iterdir())
    except OSError as exc:
        raise ValueError("frozen pass-plan directory is unavailable") from exc
    if (
        {entry.name for entry in plan_entries} != expected_plan_names
        or any(entry.is_symlink() or not entry.is_file() for entry in plan_entries)
    ):
        raise ValueError("frozen pass-plan set is not exact")
    tasks, plan_hashes = _completed_plans(
        root,
        expected_passes,
        collection_manifest=collection,
        pool_rows=pool_rows,
        registry=registry,
    )
    if plan_hashes != manifest.get("pass_plan_sha256"):
        raise ValueError("frozen pass-plan hashes changed")

    raw_roles = manifest.get("roles")
    if not isinstance(raw_roles, dict) or set(raw_roles) != set(EVIDENCE_ROLES):
        raise ValueError("role dataset manifest has invalid roles")
    explicit_roles = {
        role: {
            str(name).strip()
            for name in raw_roles[role]
            if str(name).strip()
        }
        for role in EXPLICIT_ROLES
    }
    derived_roles = _normalize_roles(explicit_roles, tasks)
    recorded_roles = {
        role: {
            str(name).strip()
            for name in raw_roles[role]
            if str(name).strip()
        }
        for role in EVIDENCE_ROLES
    }
    if derived_roles != recorded_roles:
        raise ValueError("role dataset opponent partition changed")

    task_opponents = {str(task["name"]) for task in tasks.values()}
    if set(opponents) != task_opponents:
        raise ValueError("role dataset opponent registry coverage changed")
    for task in tasks.values():
        name = str(task["name"])
        entry = opponents.get(name)
        expected_digest = str(task.get("execution_directory_sha256") or "")
        if (
            not isinstance(entry, dict)
            or entry.get("execution_directory_sha256") != expected_digest
        ):
            raise ValueError(f"role dataset opponent binding changed: {name}")
        snapshot = Path(str(entry.get("snapshot_path") or ""))
        if (
            not snapshot.is_dir()
            or collector._directory_digest(snapshot) != expected_digest
        ):
            raise ValueError(f"role dataset opponent snapshot changed: {name}")

    candidate = manifest.get("candidate_snapshot")
    contract = collection.get("resume_contract") or {}
    if not isinstance(candidate, dict):
        raise ValueError("role dataset candidate snapshot is invalid")
    candidate_path = Path(str(candidate.get("path") or ""))
    candidate_digest = str(candidate.get("sha256") or "")
    if (
        str(candidate_path) != str(contract.get("candidate_execution_path") or "")
        or candidate_digest != contract.get("candidate_snapshot_sha256")
        or not candidate_path.is_dir()
        or collector._directory_digest(candidate_path) != candidate_digest
    ):
        raise ValueError("role dataset candidate snapshot changed")


def freeze_role_dataset(
    source_dir: Path,
    output_dir: Path,
    *,
    role_opponents: dict[str, set[str]],
    min_value_rows: dict[str, int] | None = None,
    min_behavior_rows: dict[str, int] | None = None,
) -> dict[str, Any]:
    source_dir = source_dir.resolve()
    output_dir = output_dir.resolve()
    if source_dir == output_dir:
        raise ValueError("source and output directories must differ")
    if output_dir.exists():
        if any(output_dir.iterdir()):
            raise FileExistsError(f"output already exists and is not empty: {output_dir}")
        output_dir.rmdir()

    _, state_digest, completed, limits = _collector_boundary(source_dir)
    collection_path = source_dir / "collection_manifest.json"
    registry_path = source_dir / "opponent_snapshots" / "registry.json"
    collection, collection_digest = _load_json_snapshot(collection_path)
    registry, registry_digest = _load_json_snapshot(registry_path)
    requested = _integer(
        collection.get("passes_requested"),
        field="passes_requested",
        minimum=completed,
    )
    if (collection.get("resume_contract") or {}).get("deck_seed_scheme") != (
        "disjoint_match_blocks_v1"
    ):
        raise RuntimeError("collection does not use independent deck blocks")
    pool_path = source_dir / "pool_snapshots.jsonl"
    pool_rows, pool_manifest = _read_jsonl_snapshot(
        pool_path, row_limit=completed
    )
    if [row.get("pass") for row in pool_rows] != list(range(1, completed + 1)):
        raise RuntimeError("completed pool snapshot prefix is not contiguous")
    tasks, plan_hashes = _completed_plans(
        source_dir,
        completed,
        collection_manifest=collection,
        pool_rows=pool_rows,
        registry=registry,
        validate_data_prefix=True,
    )
    roles = _normalize_roles(role_opponents, tasks)

    data: dict[str, dict[str, list[dict[str, Any]]]] = {}
    input_files = {}
    for prefix in PREFIXES:
        data[prefix] = {}
        for split in SOURCE_SPLITS:
            path = source_dir / f"{prefix}_{split}.jsonl"
            rows, details = _read_jsonl_snapshot(
                path, row_limit=limits[prefix][split]
            )
            data[prefix][split] = rows
            input_files[path.name] = details
    _validate_rows(data, tasks)
    response_counts = {}
    for split in SOURCE_SPLITS:
        upgraded = annotate_response_rows(
            data["opponent_actions"][split], strict=True
        )
        data["opponent_actions"][split] = upgraded
        response_counts[split] = {
            "rows": len(upgraded),
            "eligible": sum(bool(row["response_eligible"]) for row in upgraded),
            "observed": sum(bool(row["response_observed"]) for row in upgraded),
            "amount_targets": sum(
                bool(row["response_amount_target_mask"]) for row in upgraded
            ),
        }
    snapshots = _verify_snapshots(source_dir, collection, registry, tasks)

    role_for_opponent = {
        opponent: role for role, names in roles.items() for opponent in names
    }
    output_rows = {
        prefix: {role: [] for role in EVIDENCE_ROLES} for prefix in PREFIXES
    }
    for prefix in PREFIXES:
        for source_split in SOURCE_SPLITS:
            for row in data[prefix][source_split]:
                opponent = _opponent(row)
                role = role_for_opponent.get(opponent)
                if role is None:
                    raise RuntimeError(f"row opponent has no evidence role: {opponent}")
                output_rows[prefix][role].append({
                    **row,
                    "_source_split": source_split,
                    "_split": role,
                    "_evidence_role": role,
                })

    for prefix in PREFIXES:
        for role in EVIDENCE_ROLES:
            observed = {_opponent(row) for row in output_rows[prefix][role]}
            if observed != roles[role]:
                raise RuntimeError(
                    f"{prefix} role opponent coverage mismatch for {role}: "
                    f"missing={sorted(roles[role] - observed)} "
                    f"unexpected={sorted(observed - roles[role])}"
                )
    for prefix in PREFIXES:
        for role in EVIDENCE_ROLES:
            for row in output_rows[prefix][role]:
                if not strategy_context_is_absent(row):
                    raise RuntimeError(
                        "zero-context role freeze found strategy context: "
                        f"{prefix}:{role}:{_opponent(row)}"
                    )

    value_minimums = {role: 1 for role in EVIDENCE_ROLES}
    behavior_minimums = {role: 1 for role in EVIDENCE_ROLES}
    value_minimums.update(min_value_rows or {})
    behavior_minimums.update(min_behavior_rows or {})
    for prefix, minimums in (
        ("cf", value_minimums), ("opponent_actions", behavior_minimums)
    ):
        unknown = set(minimums) - set(EVIDENCE_ROLES)
        if unknown:
            raise ValueError(f"unknown minimum roles: {sorted(unknown)}")
        for role in EVIDENCE_ROLES:
            minimum = _integer(
                minimums[role], field=f"minimum.{prefix}.{role}", minimum=0
            )
            if len(output_rows[prefix][role]) < minimum:
                raise RuntimeError(
                    f"insufficient {prefix} rows for {role}: "
                    f"{len(output_rows[prefix][role])} < {minimum}"
                )

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(
        prefix=f".{output_dir.name}.role-freeze-", dir=output_dir.parent
    ))
    try:
        for prefix in PREFIXES:
            for role in EVIDENCE_ROLES:
                _write_jsonl(
                    temporary / f"{prefix}_{role}.jsonl",
                    output_rows[prefix][role],
                )
        shutil.copyfile(collection_path, temporary / collection_path.name)
        _write_jsonl(temporary / "pool_snapshots.completed.jsonl", pool_rows)
        plans_out = temporary / "pass_plans"
        plans_out.mkdir()
        for pass_index in range(1, completed + 1):
            name = f"pass_{pass_index:04d}.json"
            shutil.copyfile(source_dir / "pass_plans" / name, plans_out / name)
        (temporary / "opponent_snapshots.completed.json").write_text(
            json.dumps({
                "schema": "opponent_execution_snapshot_v1",
                "opponents": snapshots["opponents"],
            }, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        if _sha256(source_dir / "collector_state.json") != state_digest:
            raise RuntimeError("collector advanced while role freeze was running")
        if _sha256(collection_path) != collection_digest:
            raise RuntimeError("collection manifest changed during role freeze")
        if _sha256(registry_path) != registry_digest:
            raise RuntimeError("opponent registry changed during role freeze")
        _verify_input_snapshot(pool_path, pool_manifest)
        for name, details in input_files.items():
            _verify_input_snapshot(source_dir / name, details)
        for name, digest in plan_hashes.items():
            if _sha256(source_dir / "pass_plans" / name) != digest:
                raise RuntimeError(f"pass plan changed during role freeze: {name}")

        outputs = {}
        for prefix in PREFIXES:
            for role in EVIDENCE_ROLES:
                path = temporary / f"{prefix}_{role}.jsonl"
                outputs[path.name] = {
                    "rows": len(output_rows[prefix][role]),
                    "bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                    "opponents": sorted({
                        _opponent(row) for row in output_rows[prefix][role]
                    }),
                    **(
                        {"row_schema": OPPONENT_RESPONSE_SCHEMA}
                        if prefix == "opponent_actions"
                        else {}
                    ),
                }
        manifest = {
            "schema": SCHEMA,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_dir": str(source_dir),
            "source_completed_passes": completed,
            "source_requested_passes": requested,
            "source_collection_complete": completed == requested,
            "candidate_snapshot": snapshots["candidate"],
            "strategy_context_runtime_mode": STRATEGY_CONTEXT_RUNTIME_MODE,
            "roles": {role: sorted(names) for role, names in roles.items()},
            "role_source_contract": EXPECTED_SOURCE_SPLIT,
            "input_files": input_files,
            "completed_pool_snapshot": pool_manifest,
            "frozen_pool_snapshot": {
                "bytes": (
                    temporary / "pool_snapshots.completed.jsonl"
                ).stat().st_size,
                "rows": len(pool_rows),
                "sha256": _sha256(
                    temporary / "pool_snapshots.completed.jsonl"
                ),
            },
            "pass_plan_sha256": plan_hashes,
            "collection_manifest_sha256": collection_digest,
            "collector_state_sha256": state_digest,
            "opponent_registry_sha256": registry_digest,
            "frozen_opponent_registry_sha256": _sha256(
                temporary / "opponent_snapshots.completed.json"
            ),
            "outputs": outputs,
            "behavior_supervision": {
                **response_schema_metadata(),
                "source_split_counts": response_counts,
            },
            "match_outcome_supervision": match_outcome_metadata(),
            "invariants": {
                "opponent_disjoint": True,
                "match_cluster_disjoint": True,
                "deck_blocks_non_overlapping": True,
                "uniform_decision_ipw_validated": True,
                "national_response_v2_validated": True,
                "national_70_hand_outcome_validated": True,
                "artifact_snapshots_verified": True,
                "final_blind_in_dataset": False,
            },
            "freeze_tool_sha256": _sha256(Path(__file__)),
        }
        (temporary / "role_manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
        )
        temporary.replace(output_dir)
        return manifest
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _role_minimums(train: int, evaluation: int) -> dict[str, int]:
    return {
        role: train if role == "train" else evaluation for role in EVIDENCE_ROLES
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    for role in EXPLICIT_ROLES:
        parser.add_argument(
            f"--{role.replace('_', '-')}-opponent",
            action="append",
            required=True,
        )
    parser.add_argument("--min-value-train", type=int, default=500)
    parser.add_argument("--min-value-eval-role", type=int, default=100)
    parser.add_argument("--min-behavior-train", type=int, default=2000)
    parser.add_argument("--min-behavior-eval-role", type=int, default=500)
    args = parser.parse_args(argv)
    try:
        manifest = freeze_role_dataset(
            args.source_dir,
            args.output_dir,
            role_opponents={
                role: set(getattr(args, f"{role}_opponent"))
                for role in EXPLICIT_ROLES
            },
            min_value_rows=_role_minimums(
                args.min_value_train, args.min_value_eval_role
            ),
            min_behavior_rows=_role_minimums(
                args.min_behavior_train, args.min_behavior_eval_role
            ),
        )
    except (FileNotFoundError, FileExistsError, RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
