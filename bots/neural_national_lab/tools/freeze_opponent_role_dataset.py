#!/usr/bin/env python3
"""Freeze an independent collection into five opponent-disjoint evidence roles."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
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
from longrun_collect_oppmodel import _directory_digest  # noqa: E402
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


SCHEMA = "opponent_role_dataset_v3"
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


def _completed_plans(
    source_dir: Path, completed_passes: int
) -> tuple[dict[tuple[str, int, int], dict[str, Any]], dict[str, str]]:
    tasks: dict[tuple[str, int, int], dict[str, Any]] = {}
    plan_hashes = {}
    intervals = []
    bot_seeds: dict[int, tuple[str, int, int]] = {}
    for pass_index in range(1, completed_passes + 1):
        path = source_dir / "pass_plans" / f"pass_{pass_index:04d}.json"
        payload, digest = _load_json_snapshot(path)
        plan_hashes[path.name] = digest
        if payload.get("seed_scheme") != "disjoint_match_blocks_v1":
            raise RuntimeError(f"unsupported seed scheme in {path}")
        if _integer(payload.get("pass"), field=f"{path.name}.pass") != pass_index:
            raise RuntimeError(f"pass plan index mismatch: {path}")
        rows = payload.get("tasks")
        if not isinstance(rows, list) or not rows:
            raise RuntimeError(f"pass plan has no tasks: {path}")
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
    if not candidate_path.is_dir() or _directory_digest(candidate_path) != candidate_digest:
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
            if not path.is_dir() or _directory_digest(path) != expected:
                raise RuntimeError(f"opponent snapshot digest mismatch: {name}")
            used[name] = dict(entry)
    return {
        "candidate": {
            "path": str(candidate_path.resolve()),
            "sha256": candidate_digest,
        },
        "opponents": used,
    }


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
    tasks, plan_hashes = _completed_plans(source_dir, completed)
    roles = _normalize_roles(role_opponents, tasks)

    pool_path = source_dir / "pool_snapshots.jsonl"
    pool_rows, pool_manifest = _read_jsonl_snapshot(
        pool_path, row_limit=completed
    )
    if [row.get("pass") for row in pool_rows] != list(range(1, completed + 1)):
        raise RuntimeError("completed pool snapshot prefix is not contiguous")

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
            "roles": {role: sorted(names) for role, names in roles.items()},
            "role_source_contract": EXPECTED_SOURCE_SPLIT,
            "input_files": input_files,
            "completed_pool_snapshot": pool_manifest,
            "pass_plan_sha256": plan_hashes,
            "collection_manifest_sha256": collection_digest,
            "collector_state_sha256": state_digest,
            "opponent_registry_sha256": registry_digest,
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
