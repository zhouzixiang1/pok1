#!/usr/bin/env python3
"""Precommit one protected opponent-role split before collection completes."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Any


SCHEMA = "opponent_role_freeze_plan_v1"
PLAN_FILENAME = "role_precommit_plan.json"
FORMAL_EXPECTED_PASSES = 160
SOURCE_SPLITS = ("train", "val", "held_out")
PREFIXES = ("cf", "opponent_actions")
EVIDENCE_ROLES = (
    "train", "early_stop", "model_calibration", "policy_selection", "policy_gate",
)
EXPLICIT_ROLES = EVIDENCE_ROLES[1:]
EXPECTED_SOURCE_SPLIT = {
    "early_stop": "train",
    "model_calibration": "val",
    "policy_selection": "val",
    "policy_gate": "held_out",
}
FORMAL_MINIMUM_ROWS = {
    "cf": {
        "train": 500,
        "early_stop": 100,
        "model_calibration": 100,
        "policy_selection": 100,
        "policy_gate": 100,
    },
    "opponent_actions": {
        "train": 2000,
        "early_stop": 500,
        "model_calibration": 500,
        "policy_selection": 500,
        "policy_gate": 500,
    },
}
ROOT = Path(__file__).resolve().parents[3]
FREEZE_TOOL = Path(__file__).with_name("freeze_opponent_role_dataset.py")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _integer(value: Any, *, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _digest(value: Any, *, field: str, length: int = 64) -> str:
    text = str(value or "")
    pattern = HEX64 if length == 64 else HEX40
    if not pattern.fullmatch(text):
        raise ValueError(f"{field} must be a lowercase digest")
    return text


def _read_regular(path: Path, *, field: str) -> tuple[bytes, dict[str, Any]]:
    path = path.resolve()
    if path.is_symlink():
        raise ValueError(f"{field} must not be a symlink")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"cannot read {field}: {path}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"{field} is not a regular file")
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity = lambda row: (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns, row.st_ctime_ns)
    raw = b"".join(chunks)
    if identity(before) != identity(after) or len(raw) != before.st_size:
        raise ValueError(f"{field} changed while it was read")
    return raw, {
        "path": str(path), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _embedded_receipt(path: Path, *, field: str) -> tuple[bytes, dict[str, Any]]:
    raw, receipt = _read_regular(path, field=field)
    return raw, {**receipt, "bytes_base64": base64.b64encode(raw).decode("ascii")}


def _decode_receipt(receipt: Any, *, field: str) -> bytes:
    if not isinstance(receipt, dict) or set(receipt) != {
        "path", "bytes", "sha256", "bytes_base64",
    }:
        raise ValueError(f"{field} receipt is invalid")
    path = receipt.get("path")
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise ValueError(f"{field} path is invalid")
    try:
        raw = base64.b64decode(receipt["bytes_base64"], validate=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} embedded bytes are invalid") from exc
    if (
        len(raw) != _integer(receipt.get("bytes"), field=f"{field}.bytes")
        or hashlib.sha256(raw).hexdigest()
        != _digest(receipt.get("sha256"), field=f"{field}.sha256")
    ):
        raise ValueError(f"{field} embedded bytes changed")
    return raw


def _json_object(raw: bytes, *, field: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {field}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{field} must be an object")
    return payload


def _prefix_details(path: Path, rows: int) -> dict[str, Any]:
    rows = _integer(rows, field=f"{path.name}.rows")
    digest, count, size = hashlib.sha256(), 0, 0
    with path.open("rb") as handle:
        while count < rows:
            line = handle.readline()
            if not line or not line.endswith(b"\n"):
                raise ValueError(f"JSONL prefix is incomplete: {path}")
            digest.update(line)
            size += len(line)
            count += 1
    return {
        "path": str(path.resolve()), "rows": count, "bytes": size,
        "sha256": digest.hexdigest(),
    }


def _verify_prefix(receipt: Any, *, field: str) -> None:
    if not isinstance(receipt, dict) or set(receipt) != {
        "path", "rows", "bytes", "sha256",
    }:
        raise ValueError(f"{field} prefix receipt is invalid")
    actual = _prefix_details(
        Path(str(receipt.get("path") or "")),
        _integer(receipt.get("rows"), field=f"{field}.rows"),
    )
    if actual != receipt:
        raise ValueError(f"{field} prefix changed")


def current_git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
        check=False,
    )
    commit = result.stdout.strip()
    if result.returncode or not HEX40.fullmatch(commit):
        raise ValueError("role-plan checkout has no valid Git commit")
    return commit


def normalize_roles(role_opponents: dict[str, set[str] | list[str]]) -> dict[str, list[str]]:
    if set(role_opponents) != set(EXPLICIT_ROLES):
        raise ValueError(f"explicit roles must be {list(EXPLICIT_ROLES)}")
    result = {
        role: sorted({str(name).strip() for name in role_opponents[role] if str(name).strip()})
        for role in EXPLICIT_ROLES
    }
    owners: dict[str, str] = {}
    for role, names in result.items():
        if not names:
            raise ValueError(f"{role} requires at least one opponent")
        for name in names:
            if name in owners:
                raise ValueError(f"opponent assigned to multiple roles: {name}")
            owners[name] = role
    return result


def normalize_minimum_rows(
    min_value_rows: dict[str, int] | None,
    min_behavior_rows: dict[str, int] | None,
) -> dict[str, dict[str, int]]:
    result = {
        "cf": {role: 1 for role in EVIDENCE_ROLES},
        "opponent_actions": {role: 1 for role in EVIDENCE_ROLES},
    }
    for prefix, updates in (
        ("cf", min_value_rows or {}), ("opponent_actions", min_behavior_rows or {}),
    ):
        if set(updates) - set(EVIDENCE_ROLES):
            raise ValueError("unknown minimum roles")
        for role, value in updates.items():
            result[prefix][role] = _integer(value, field=f"minimum.{prefix}.{role}")
    return result


def require_formal_minimums(minimum_rows: dict[str, dict[str, int]]) -> None:
    if set(minimum_rows) != set(PREFIXES):
        raise ValueError("role-plan minimum modalities changed")
    for prefix in PREFIXES:
        rows = minimum_rows.get(prefix)
        if not isinstance(rows, dict) or set(rows) != set(EVIDENCE_ROLES):
            raise ValueError("role-plan minimum roles changed")
        for role, floor in FORMAL_MINIMUM_ROWS[prefix].items():
            if _integer(rows.get(role), field=f"minimum.{prefix}.{role}") < floor:
                raise ValueError(f"formal minimum was weakened: {prefix}.{role}")


def _source_roles(
    collection: dict[str, Any], plan_paths: list[Path], roles: dict[str, list[str]],
) -> None:
    contract = collection.get("resume_contract")
    if not isinstance(contract, dict):
        raise ValueError("collection resume contract is missing")
    val = {str(name) for name in contract.get("val_opponents") or []}
    held = {str(name) for name in contract.get("held_out_opponents") or []}
    if val != set(roles["model_calibration"]) | set(roles["policy_selection"]):
        raise ValueError("role plan must partition validation opponents exactly")
    if held != set(roles["policy_gate"]):
        raise ValueError("role plan must assign every held-out opponent to policy_gate")
    observed: dict[str, str] = {}
    for path in plan_paths:
        payload = _json_object(path.read_bytes(), field=path.name)
        for task in payload.get("tasks") or []:
            if not isinstance(task, dict):
                raise ValueError(f"invalid task in {path}")
            name, split = str(task.get("name") or ""), str(task.get("split") or "")
            if not name or split not in SOURCE_SPLITS:
                raise ValueError(f"invalid opponent split in {path}")
            previous = observed.setdefault(name, split)
            if previous != split:
                raise ValueError(f"opponent crosses source splits: {name}")
    for role, names in roles.items():
        expected = EXPECTED_SOURCE_SPLIT[role]
        for name in names:
            if observed.get(name) != expected:
                raise ValueError(f"{role} opponent {name} must come from {expected}")
    if not ({name for name, split in observed.items() if split == "train"} - set(roles["early_stop"])):
        raise ValueError("role plan leaves no training opponents")


def _ledger_snapshot(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    path = path.resolve()
    lock_path = path.with_name(f".{path.name}.lock")
    if lock_path.is_symlink() or not lock_path.is_file():
        raise ValueError("exposure ledger lock is missing")
    with lock_path.open("r", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        raw, receipt = _embedded_receipt(path, field="exposure ledger")
        payload = _json_object(raw, field="exposure ledger")
        fcntl.flock(lock, fcntl.LOCK_UN)
    events = payload.get("events")
    if payload.get("schema") != "opponent_exposure_ledger_v1" or not isinstance(events, list):
        raise ValueError("invalid exposure ledger")
    return payload, receipt


def _creation_snapshot(source_dir: Path) -> dict[str, Any]:
    source_dir = source_dir.resolve()
    for _attempt in range(4):
        state_raw, state_receipt = _embedded_receipt(
            source_dir / "collector_state.json", field="collector state",
        )
        state = _json_object(state_raw, field="collector state")
        completed = _integer(state.get("completed_passes"), field="completed_passes", minimum=1)
        try:
            totals = {
                "cf": {split: _integer(state["total_rows"][split], field=split) for split in SOURCE_SPLITS},
                "opponent_actions": {
                    split: _integer(state["total_behavior_rows"][split], field=split)
                    for split in SOURCE_SPLITS
                },
            }
        except (KeyError, TypeError) as exc:
            raise ValueError("collector state row totals are invalid") from exc
        pool = _prefix_details(source_dir / "pool_snapshots.jsonl", completed)
        plan_paths = [
            source_dir / "pass_plans" / f"pass_{index:04d}.json"
            for index in range(1, completed + 1)
        ]
        plans = {path.name: _sha256(path) for path in plan_paths}
        data = {
            f"{prefix}_{split}.jsonl": _prefix_details(
                source_dir / f"{prefix}_{split}.jsonl", totals[prefix][split]
            )
            for prefix in PREFIXES for split in SOURCE_SPLITS
        }
        state_raw_after, _ = _read_regular(
            source_dir / "collector_state.json", field="collector state",
        )
        if state_raw_after == state_raw:
            return {
                "state": state, "state_receipt": state_receipt, "completed": completed,
                "totals": totals, "pool": pool, "plans": plans, "plan_paths": plan_paths,
                "data": data,
            }
    raise ValueError("collector state advanced while role plan was created")


def _finalize(unsigned: dict[str, Any]) -> dict[str, Any]:
    return {**unsigned, "payload_sha256": canonical_sha256(unsigned)}


def create_plan(
    source_dir: Path,
    output_path: Path,
    *,
    ledger_path: Path,
    role_opponents: dict[str, set[str] | list[str]],
    minimum_rows: dict[str, dict[str, int]],
    expected_passes: int = FORMAL_EXPECTED_PASSES,
    apply: bool = False,
) -> dict[str, Any]:
    expected_passes = _integer(expected_passes, field="expected_passes", minimum=1)
    if expected_passes != FORMAL_EXPECTED_PASSES:
        raise ValueError(f"formal role plan requires exactly {FORMAL_EXPECTED_PASSES} passes")
    roles = normalize_roles(role_opponents)
    require_formal_minimums(minimum_rows)
    source_dir = source_dir.resolve()
    collection_raw, collection_receipt = _embedded_receipt(
        source_dir / "collection_manifest.json", field="collection manifest",
    )
    collection = _json_object(collection_raw, field="collection manifest")
    if collection.get("passes_requested") != expected_passes:
        raise ValueError("collection target does not match formal role plan")
    snapshot = _creation_snapshot(source_dir)
    if snapshot["completed"] >= expected_passes:
        raise ValueError("role plan must be created before collection completes")
    _source_roles(collection, snapshot["plan_paths"], roles)
    ratings_path = Path(str((collection.get("resume_contract") or {}).get("ratings_path") or ""))
    if not ratings_path.is_absolute():
        raise ValueError("collection ratings path is not absolute")
    ratings_raw, ratings_receipt = _embedded_receipt(ratings_path, field="ratings snapshot")
    _json_object(ratings_raw, field="ratings snapshot")
    ledger, ledger_receipt = _ledger_snapshot(ledger_path)
    events = ledger["events"]
    unsigned = {
        "schema": SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_dir": str(source_dir),
        "expected_passes": expected_passes,
        "creation_git_commit": current_git_commit(),
        "toolchain": {
            "plan_tool_sha256": _sha256(Path(__file__)),
            "freeze_tool_sha256": _sha256(FREEZE_TOOL),
        },
        "collection_manifest": collection_receipt,
        "creation_state": {
            "file": snapshot["state_receipt"],
            "completed_passes": snapshot["completed"],
            "total_rows": snapshot["state"]["total_rows"],
            "total_behavior_rows": snapshot["state"]["total_behavior_rows"],
        },
        "completed_prefix": {
            "pool_snapshots": snapshot["pool"],
            "pass_plan_sha256": snapshot["plans"],
            "data": snapshot["data"],
        },
        "ratings_snapshot": ratings_receipt,
        "ledger_prefix": {
            "path": str(Path(ledger_path).resolve()),
            "file_at_creation": ledger_receipt,
            "schema": ledger["schema"],
            "event_count": len(events),
            "events": events,
            "events_sha256": canonical_sha256(events),
        },
        "roles": roles,
        "minimum_rows": minimum_rows,
        "deployment_policy_value": False,
        "strength_evidence": False,
    }
    plan = _finalize(unsigned)
    if apply:
        _write_noreplace(output_path.resolve(), json.dumps(
            plan, indent=2, sort_keys=True, allow_nan=False
        ).encode("utf-8"))
    return plan


def _write_noreplace(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        os.chmod(path, 0o444)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def load_plan(path: Path) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    raw, receipt = _read_regular(path, field="role precommit plan")
    plan = _json_object(raw, field="role precommit plan")
    unsigned = dict(plan)
    recorded = _digest(unsigned.pop("payload_sha256", None), field="plan payload_sha256")
    if recorded != canonical_sha256(unsigned) or unsigned.get("schema") != SCHEMA:
        raise ValueError("role precommit plan self-hash changed")
    _validate_internal(plan)
    return raw, plan, receipt


def _validate_internal(plan: dict[str, Any]) -> None:
    if (
        plan.get("expected_passes") != FORMAL_EXPECTED_PASSES
        or plan.get("deployment_policy_value") is not False
        or plan.get("strength_evidence") is not False
        or not HEX40.fullmatch(str(plan.get("creation_git_commit") or ""))
    ):
        raise ValueError("role precommit plan authority or boundary changed")
    toolchain = plan.get("toolchain")
    if not isinstance(toolchain, dict) or set(toolchain) != {
        "plan_tool_sha256", "freeze_tool_sha256",
    }:
        raise ValueError("role precommit plan toolchain changed")
    _digest(toolchain["plan_tool_sha256"], field="plan tool")
    _digest(toolchain["freeze_tool_sha256"], field="freeze tool")
    source = Path(str(plan.get("source_dir") or ""))
    if not source.is_absolute() or str(source) != str(source.resolve()):
        raise ValueError("role precommit source path changed")
    collection = _json_object(
        _decode_receipt(plan.get("collection_manifest"), field="collection manifest"),
        field="collection manifest",
    )
    if (
        plan["collection_manifest"]["path"]
        != str((source / "collection_manifest.json").resolve())
        or collection.get("passes_requested") != FORMAL_EXPECTED_PASSES
    ):
        raise ValueError("role precommit collection target changed")
    state_section = plan.get("creation_state")
    if not isinstance(state_section, dict) or set(state_section) != {
        "file", "completed_passes", "total_rows", "total_behavior_rows",
    }:
        raise ValueError("role precommit state receipt changed")
    state = _json_object(
        _decode_receipt(state_section["file"], field="collector state"),
        field="collector state",
    )
    completed = _integer(state_section["completed_passes"], field="completed_passes", minimum=1)
    if (
        state_section["file"]["path"]
        != str((source / "collector_state.json").resolve())
        or completed >= FORMAL_EXPECTED_PASSES
        or state.get("completed_passes") != completed
    ):
        raise ValueError("role plan was not created before the formal boundary")
    if (
        state.get("total_rows") != state_section["total_rows"]
        or state.get("total_behavior_rows") != state_section["total_behavior_rows"]
    ):
        raise ValueError("role precommit state totals changed")
    ratings = plan.get("ratings_snapshot")
    _json_object(
        _decode_receipt(ratings, field="ratings snapshot"),
        field="ratings snapshot",
    )
    if ratings["path"] != str((collection.get("resume_contract") or {}).get("ratings_path") or ""):
        raise ValueError("role precommit ratings path changed")
    ledger = plan.get("ledger_prefix")
    if not isinstance(ledger, dict) or set(ledger) != {
        "path", "file_at_creation", "schema", "event_count", "events", "events_sha256",
    }:
        raise ValueError("role precommit ledger prefix changed")
    recorded_ledger = _json_object(
        _decode_receipt(ledger["file_at_creation"], field="exposure ledger"),
        field="exposure ledger",
    )
    if (
        ledger["path"] != ledger["file_at_creation"]["path"]
        or ledger["schema"] != "opponent_exposure_ledger_v1"
        or recorded_ledger != {"schema": ledger["schema"], "events": ledger["events"]}
        or ledger["event_count"] != len(ledger["events"])
        or ledger["events_sha256"] != canonical_sha256(ledger["events"])
    ):
        raise ValueError("role precommit ledger evidence changed")
    normalize_roles(plan.get("roles") or {})
    require_formal_minimums(plan.get("minimum_rows") or {})
    prefix = plan.get("completed_prefix")
    if not isinstance(prefix, dict) or set(prefix) != {
        "pool_snapshots", "pass_plan_sha256", "data",
    }:
        raise ValueError("role precommit completed prefix changed")
    plans = prefix["pass_plan_sha256"]
    expected_names = {f"pass_{index:04d}.json" for index in range(1, completed + 1)}
    if not isinstance(plans, dict) or set(plans) != expected_names:
        raise ValueError("role precommit plan prefix changed")
    for name, digest in plans.items():
        _digest(digest, field=name)
    data = prefix["data"]
    expected_data = {f"{p}_{s}.jsonl" for p in PREFIXES for s in SOURCE_SPLITS}
    if not isinstance(data, dict) or set(data) != expected_data:
        raise ValueError("role precommit data prefix changed")
    for name, receipt in {"pool": prefix["pool_snapshots"], **data}.items():
        if not isinstance(receipt, dict) or set(receipt) != {
            "path", "rows", "bytes", "sha256",
        }:
            raise ValueError(f"role precommit prefix receipt changed: {name}")
        _integer(receipt["rows"], field=f"{name}.rows")
        _integer(receipt["bytes"], field=f"{name}.bytes")
        _digest(receipt["sha256"], field=f"{name}.sha256")
        expected_path = source / (
            "pool_snapshots.jsonl" if name == "pool" else name
        )
        if receipt["path"] != str(expected_path.resolve()):
            raise ValueError(f"role precommit prefix path changed: {name}")


def validate_for_freeze(
    plan_path: Path,
    *,
    source_dir: Path,
    role_opponents: dict[str, set[str] | list[str]],
    minimum_rows: dict[str, dict[str, int]],
) -> dict[str, Any]:
    raw, plan, receipt = load_plan(plan_path)
    source_dir = source_dir.resolve()
    roles = normalize_roles(role_opponents)
    if (
        plan["source_dir"] != str(source_dir)
        or plan["roles"] != roles
        or plan["minimum_rows"] != minimum_rows
    ):
        raise ValueError("role precommit plan roles or floors changed")
    if plan["creation_git_commit"] != current_git_commit():
        raise ValueError("role precommit Git commit changed")
    if (
        plan["toolchain"]["plan_tool_sha256"] != _sha256(Path(__file__))
        or plan["toolchain"]["freeze_tool_sha256"] != _sha256(FREEZE_TOOL)
    ):
        raise ValueError("role precommit toolchain changed")
    collection_raw, _ = _read_regular(
        source_dir / "collection_manifest.json", field="collection manifest",
    )
    if collection_raw != _decode_receipt(plan["collection_manifest"], field="collection manifest"):
        raise ValueError("collection manifest changed after role precommit")
    state_raw, _ = _read_regular(source_dir / "collector_state.json", field="collector state")
    state = _json_object(state_raw, field="collector state")
    if state.get("completed_passes") != FORMAL_EXPECTED_PASSES:
        raise ValueError("role freeze requires the exact 160-pass state")
    creation = plan["creation_state"]
    for field in ("total_rows", "total_behavior_rows"):
        for split in SOURCE_SPLITS:
            if _integer(state[field][split], field=f"{field}.{split}") < _integer(
                creation[field][split], field=f"creation.{field}.{split}"
            ):
                raise ValueError("collector state did not extend the precommit prefix")
    prefix = plan["completed_prefix"]
    _verify_prefix(prefix["pool_snapshots"], field="pool snapshots")
    for name, digest in prefix["pass_plan_sha256"].items():
        if _sha256(source_dir / "pass_plans" / name) != digest:
            raise ValueError(f"precommitted pass plan changed: {name}")
    for name, details in prefix["data"].items():
        _verify_prefix(details, field=name)
    ledger, _ = _ledger_snapshot(Path(plan["ledger_prefix"]["path"]))
    count = plan["ledger_prefix"]["event_count"]
    if ledger["events"][:count] != plan["ledger_prefix"]["events"]:
        raise ValueError("exposure ledger prefix changed after role precommit")
    return {
        "raw": raw,
        "plan": plan,
        "receipt": {
            "filename": PLAN_FILENAME,
            "bytes": receipt["bytes"],
            "sha256": receipt["sha256"],
            "payload_sha256": plan["payload_sha256"],
        },
    }


def validate_frozen_snapshot(
    root: Path, manifest: dict[str, Any], *, expected_passes: int,
) -> dict[str, Any]:
    if expected_passes != FORMAL_EXPECTED_PASSES:
        raise ValueError("formal role plan requires exactly 160 passes")
    details = manifest.get("role_precommit_plan")
    if not isinstance(details, dict) or set(details) != {
        "filename", "bytes", "sha256", "payload_sha256",
    } or details.get("filename") != PLAN_FILENAME:
        raise ValueError("role dataset precommit-plan receipt is missing")
    path = root.resolve() / PLAN_FILENAME
    raw, plan, receipt = load_plan(path)
    if (
        receipt["bytes"] != details["bytes"]
        or receipt["sha256"] != details["sha256"]
        or plan["payload_sha256"] != details["payload_sha256"]
        or plan["creation_git_commit"] != current_git_commit()
        or plan["toolchain"]["plan_tool_sha256"] != _sha256(Path(__file__))
        or plan["toolchain"]["freeze_tool_sha256"] != _sha256(FREEZE_TOOL)
        or plan["source_dir"] != manifest.get("source_dir")
        or plan["roles"] != {
            role: manifest["roles"][role] for role in EXPLICIT_ROLES
        }
        or plan["minimum_rows"] != manifest.get("role_minimum_rows")
        or plan["collection_manifest"]["sha256"]
        != manifest.get("collection_manifest_sha256")
    ):
        raise ValueError("role dataset precommit-plan binding changed")
    for prefix in PREFIXES:
        for role in EVIDENCE_ROLES:
            filename = f"{prefix}_{role}.jsonl"
            if manifest["outputs"][filename]["rows"] < plan["minimum_rows"][prefix][role]:
                raise ValueError(f"role dataset output is below precommitted floor: {filename}")
    return {"raw": raw, "plan": plan, "receipt": details}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    for role in EXPLICIT_ROLES:
        parser.add_argument(f"--{role.replace('_', '-')}-opponent", action="append", required=True)
    parser.add_argument("--min-value-train", type=int, default=500)
    parser.add_argument("--min-value-eval-role", type=int, default=100)
    parser.add_argument("--min-behavior-train", type=int, default=2000)
    parser.add_argument("--min-behavior-eval-role", type=int, default=500)
    args = parser.parse_args(argv)
    minimums = {
        "cf": {
            role: args.min_value_train if role == "train" else args.min_value_eval_role
            for role in EVIDENCE_ROLES
        },
        "opponent_actions": {
            role: args.min_behavior_train if role == "train" else args.min_behavior_eval_role
            for role in EVIDENCE_ROLES
        },
    }
    try:
        plan = create_plan(
            args.source_dir, args.output, ledger_path=args.ledger,
            role_opponents={
                role: set(getattr(args, f"{role}_opponent")) for role in EXPLICIT_ROLES
            }, minimum_rows=minimums, apply=args.apply,
        )
    except (FileExistsError, OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps({
        "output": str(args.output.resolve()) if args.apply else None,
        "status": "applied" if args.apply else "ready_for_explicit_apply",
        "payload_sha256": plan["payload_sha256"],
        "creation_completed_passes": plan["creation_state"]["completed_passes"],
        "expected_passes": plan["expected_passes"],
        "deployment_policy_value": False,
        "strength_evidence": False,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
