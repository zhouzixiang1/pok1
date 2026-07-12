"""Realistic non-outcome provenance for protected role-dataset tests."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import freeze_opponent_role_dataset as freeze  # noqa: E402
import longrun_collect_oppmodel as collector  # noqa: E402
import migrate_oppmodel_collector_capacity as capacity_migration  # noqa: E402


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def add_formal_role_provenance(
    root: Path,
    manifest: dict[str, Any],
    *,
    passes: int = 160,
) -> None:
    outputs = manifest.setdefault("outputs", {})
    for prefix in freeze.PREFIXES:
        for role in freeze.EVIDENCE_ROLES:
            outputs.setdefault(f"{prefix}_{role}.jsonl", {"rows": 0})
    row_counts = {
        prefix: sum(
            int(outputs[f"{prefix}_{role}.jsonl"]["rows"])
            for role in freeze.EVIDENCE_ROLES
        )
        for prefix in freeze.PREFIXES
    }
    manifest["row_identity"] = {
        "schema": capacity_migration.ROW_IDENTITY_SCHEMA,
        "fields": list(capacity_migration.ROW_IDENTITY_FIELDS),
        "modalities": {
            prefix: {
                "rows": rows,
                "unique_rows": rows,
                "identity_sha256": _sha(
                    f"fixture:{prefix}:{rows}".encode("utf-8")
                ),
            }
            for prefix, rows in row_counts.items()
        },
    }
    manifest.setdefault("invariants", {})[
        "stable_row_identity_unique"
    ] = True
    candidate = Path(manifest["candidate_snapshot"]["path"])
    candidate_digest = collector._directory_digest(candidate)
    manifest["candidate_snapshot"]["sha256"] = candidate_digest
    all_opponents = sorted({
        name
        for role in freeze.EVIDENCE_ROLES
        for name in manifest["roles"][role]
    })
    ratings_path = root / "fixture_ratings.json"
    ratings_path.write_text(json.dumps({
        name: {"r": 1500.0, "rd": 50.0, "sigma": 0.06, "last_period": "x"}
        for name in all_opponents
    }, sort_keys=True), encoding="utf-8")
    ratings_snapshot = collector._capture_ratings_snapshot(ratings_path)
    val_opponents = sorted({
        *manifest["roles"]["model_calibration"],
        *manifest["roles"]["policy_selection"],
    })
    held_out_opponents = sorted(manifest["roles"]["policy_gate"])
    deck_seed_base = 10_000_000
    deck_seed_guard = 10
    bot_seed_base = 20_000_000
    resume_contract = {
        "schema_version": collector.ACTIVE_COLLECTION_CONTRACT_SCHEMA_VERSION,
        "candidate": str(candidate),
        "candidate_sha256": candidate_digest,
        "candidate_execution_path": str(candidate),
        "candidate_snapshot_sha256": candidate_digest,
        "ratings_path": str(ratings_path.resolve()),
        "workers": collector.MAX_OUTER_WORKERS,
        "probe_workers": collector.MAX_PROBE_WORKERS,
        "max_active_native_matches": collector.MAX_CONCURRENT_NATIVE_MATCHES,
        "capacity_total_slots": collector.CAPACITY_TOTAL_SLOTS,
        "capacity_first_slot": collector.CAPACITY_FIRST_SLOT,
        "hands": 70,
        "timeout_sec": 55,
        "strongest": len(all_opponents),
        "val_opponents": val_opponents,
        "held_out_opponents": held_out_opponents,
        "opponents_per_pass": len(all_opponents),
        "max_decisions": 12,
        "max_alternatives": 5,
        "decision_sampling": "uniform",
        "hand_windows": [0.0, 0.2, 0.4, 0.6, 0.8],
        "deck_seed_scheme": "disjoint_match_blocks_v1",
        "deck_seed_base": deck_seed_base,
        "deck_seed_guard": deck_seed_guard,
        "deck_seed_slots_per_pass": collector.DECK_SEED_SLOTS_PER_PASS,
        "bot_seed_base": bot_seed_base,
        "collector_sha256": _sha(Path(collector.__file__).read_bytes()),
        "probe_sha256": _sha(
            (TOOLS / "native_tcp_counterfactual_probe.py").read_bytes()
        ),
        "cross_hand_sequence_sha256": _sha(
            (TOOLS / "cross_hand_sequence.py").read_bytes()
        ),
        "runtime_capacity_sha256": _sha(
            (ROOT / "web" / "core" / "runtime_capacity.py").read_bytes()
        ),
        "national_native_sha256": _sha(
            (ROOT / "web" / "core" / "national_native.py").read_bytes()
        ),
    }
    collection = {
        "passes_requested": passes,
        "resume_contract": resume_contract,
    }
    collection_raw = json.dumps(collection, sort_keys=True).encode()
    (root / "collection_manifest.json").write_bytes(collection_raw)

    source_split = {
        "train": "train",
        "early_stop": "train",
        "model_calibration": "val",
        "policy_selection": "val",
        "policy_gate": "held_out",
    }
    opponent_root = root / "fixture_opponent_snapshots"
    opponent_root.mkdir()
    registry: dict[str, Any] = {
        "schema": "opponent_execution_snapshot_v1",
        "opponents": {},
    }
    opponents: list[tuple[str, str, str]] = []
    for role in freeze.EVIDENCE_ROLES:
        for name in manifest["roles"][role]:
            snapshot = opponent_root / name
            snapshot.mkdir()
            (snapshot / "national_bot.py").write_text(
                f"# {name}\n", encoding="utf-8"
            )
            digest = collector._directory_digest(snapshot)
            registry["opponents"][name] = {
                "snapshot_path": str(snapshot),
                "tag_commit": "1" * 40,
                "tag_directory_sha256": digest,
                "execution_matches_generation_tag": True,
                "source_path": str(snapshot),
                "source_checkout_commit": "2" * 40,
                "execution_directory_sha256": digest,
            }
            opponents.append((name, source_split[role], digest))
    registry_raw = json.dumps(registry, indent=2, sort_keys=True).encode()
    (root / "opponent_snapshots.completed.json").write_bytes(registry_raw)

    plan_root = root / "pass_plans"
    plan_root.mkdir()
    plan_hashes = {}
    pool_rows = []
    for pass_index in range(1, passes + 1):
        tasks = []
        for opponent_index, (name, split, digest) in enumerate(opponents):
            deck_seed = collector._deck_seed_for_task(
                root=deck_seed_base,
                pass_index=pass_index - 1,
                task_index=opponent_index,
                hands=70,
                guard=deck_seed_guard,
            )
            task_bot_seed = collector._bot_seed_for_task(
                root=bot_seed_base,
                pass_index=pass_index - 1,
                task_index=opponent_index,
            )
            snapshot = Path(registry["opponents"][name]["snapshot_path"])
            tasks.append({
                "name": name,
                "split": split,
                "opponent_path": str(snapshot),
                "source_path": str(snapshot),
                "tag_commit": "1" * 40,
                "tag_directory_sha256": digest,
                "execution_matches_generation_tag": True,
                "source_checkout_commit": "2" * 40,
                "deck_seed_base": deck_seed,
                "deck_seed_last": deck_seed + 69,
                "bot_seed_base": task_bot_seed,
                "hands": 70,
                "execution_directory_sha256": digest,
            })
        raw = json.dumps({
            "schema_version": collector.PASS_PLAN_SCHEMA_VERSION,
            "pass": pass_index,
            "seed_scheme": "disjoint_match_blocks_v1",
            "ratings_snapshot": ratings_snapshot,
            "tasks": tasks,
        }, sort_keys=True).encode()
        name = f"pass_{pass_index:04d}.json"
        (plan_root / name).write_bytes(raw)
        plan_hashes[name] = _sha(raw)
        pool_rows.append({
            "pass": pass_index,
            "ratings_path": str(ratings_path.resolve()),
            "ratings_sha256": ratings_snapshot["ratings_sha256"],
            "ratings_snapshot_sha256": ratings_snapshot["snapshot_sha256"],
            "min_hand": max(1, min(
                70,
                1 + int(69 * resume_contract["hand_windows"][
                    (pass_index - 1) % len(resume_contract["hand_windows"])
                ]),
            )),
            "hands": 70,
            "workers": collector.MAX_OUTER_WORKERS,
            "probe_workers": collector.MAX_PROBE_WORKERS,
            "max_active_native_matches": collector.MAX_CONCURRENT_NATIVE_MATCHES,
            "capacity_total_slots": collector.CAPACITY_TOTAL_SLOTS,
            "capacity_first_slot": collector.CAPACITY_FIRST_SLOT,
            "decision_sampling": "uniform",
            "pool": [{
                "name": task["name"],
                "split": task["split"],
                "tag_commit": task["tag_commit"],
                "execution_directory_sha256": task[
                    "execution_directory_sha256"
                ],
                "source_checkout_commit": task["source_checkout_commit"],
                "glicko": ratings_snapshot["ratings"].get(task["name"]),
                "deck_seed_base": task["deck_seed_base"],
                "deck_seed_last": task["deck_seed_last"],
                "bot_seed_base": task["bot_seed_base"],
            } for task in tasks],
        })

    pool_raw = b"".join(
        (json.dumps(row, separators=(",", ":")) + "\n").encode()
        for row in pool_rows
    )
    (root / "pool_snapshots.completed.jsonl").write_bytes(pool_raw)

    manifest.update({
        "source_dir": str(root),
        "source_completed_passes": passes,
        "source_requested_passes": passes,
        "source_collection_complete": True,
        "role_source_contract": dict(freeze.EXPECTED_SOURCE_SPLIT),
        "completed_pool_snapshot": {
            "bytes": len(pool_raw),
            "rows": passes,
            "sha256": _sha(pool_raw),
            "source_bytes_at_read": len(pool_raw),
            "truncated_to_collector_state": True,
        },
        "frozen_pool_snapshot": {
            "bytes": len(pool_raw),
            "rows": passes,
            "sha256": _sha(pool_raw),
        },
        "pass_plan_sha256": plan_hashes,
        "collection_manifest_sha256": _sha(collection_raw),
        "collector_state_sha256": "0" * 64,
        "opponent_registry_sha256": _sha(registry_raw),
        "frozen_opponent_registry_sha256": _sha(registry_raw),
        "input_files": {},
        "freeze_tool_sha256": _sha(Path(freeze.__file__).read_bytes()),
    })


def convert_to_legacy_recovery_prefix(
    root: Path,
    manifest: dict[str, Any],
    *,
    completed_prefix: int,
) -> None:
    """Turn a current-plan fixture into the exact mixed recovery shape."""

    recovered = completed_prefix + 1
    passes = int(manifest["source_completed_passes"])
    if not 1 <= completed_prefix < passes:
        raise ValueError("legacy prefix must end before the completed boundary")
    plan_root = root / "pass_plans"
    completed_hashes = {}
    for pass_index in range(1, completed_prefix + 1):
        path = plan_root / f"pass_{pass_index:04d}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.pop("schema_version")
        payload.pop("ratings_snapshot")
        raw = json.dumps(payload, sort_keys=True).encode()
        path.write_bytes(raw)
        completed_hashes[path.name] = _sha(raw)

    pool_path = root / "pool_snapshots.completed.jsonl"
    pool_rows = [json.loads(line) for line in pool_path.read_text().splitlines()]
    for row in pool_rows[:completed_prefix]:
        row.pop("ratings_snapshot_sha256")
    pool_lines = [
        (json.dumps(row, separators=(",", ":")) + "\n").encode()
        for row in pool_rows
    ]
    pool_path.write_bytes(b"".join(pool_lines))
    prefix_pool = _sha(b"".join(pool_lines[:completed_prefix]))
    recovered_pool = _sha(b"".join(pool_lines[:recovered]))
    collection_path = root / "collection_manifest.json"
    collection = json.loads(collection_path.read_text(encoding="utf-8"))
    ratings_sha = pool_rows[completed_prefix - 1]["ratings_sha256"]
    current_collector_sha = collection["resume_contract"]["collector_sha256"]
    reviewed = {
        "pool_snapshots": prefix_pool,
        "collection_manifest": "5" * 64,
        "collector_state": "6" * 64,
        "recovery_plan": "7" * 64,
        "legacy_collector": "8" * 64,
        "current_collector": current_collector_sha,
        "identity_migration": "9" * 64,
        "archived_ratings": ratings_sha,
        "opponent_registry": "b" * 64,
        **{
            f"{prefix}_{split}.jsonl": "3" * 64
            for prefix in ("cf", "opponent_actions")
            for split in freeze.SOURCE_SPLITS
        },
    }
    receipt = {
        "schema_version": 1,
        "mode": "complete_schema4_tail_to_schema5",
        "completed_prefix_pass": completed_prefix,
        "recovered_pass": recovered,
        "expectations_sha256": "4" * 64,
        "recovery_tool_sha256": _sha(
            (TOOLS / "recover_legacy_oppmodel_collection.py").read_bytes()
        ),
        "reviewed_hashes": reviewed,
        "completed_plan_sha256": completed_hashes,
        "before": {
            "collection_manifest_sha256": reviewed["collection_manifest"],
            "collector_state_sha256": reviewed["collector_state"],
            "pool_snapshots_sha256": prefix_pool,
            "recovery_plan_sha256": reviewed["recovery_plan"],
            "legacy_collector_sha256": reviewed["legacy_collector"],
        },
        "archived_ratings": {
            "ratings_sha256": reviewed["archived_ratings"],
            "ratings_snapshot_sha256": pool_rows[recovered - 1][
                "ratings_snapshot_sha256"
            ],
            "identity_migration_sha256": reviewed["identity_migration"],
        },
        "tail": {"fixture": {"value_rows": 1, "behavior_rows": 1}},
        "after": {
            "collector_schema_version": collector.COLLECTION_CONTRACT_SCHEMA_VERSION,
            "collector_sha256": current_collector_sha,
            "pass_plan_schema_version": collector.PASS_PLAN_SCHEMA_VERSION,
            "recovery_plan_sha256": _sha(
                (plan_root / f"pass_{recovered:04d}.json").read_bytes()
            ),
            "pool_snapshots_sha256": recovered_pool,
            "collector_state_sha256": "a" * 64,
            "total_rows": {split: 0 for split in freeze.SOURCE_SPLITS},
            "total_behavior_rows": {split: 0 for split in freeze.SOURCE_SPLITS},
        },
        "probe_execution_count": 0,
        "read_current_ratings": False,
        "strength_evidence": False,
        "deployment_policy_value": False,
    }
    receipt["receipt_sha256"] = freeze._canonical_sha256(receipt)
    collection["legacy_recovery"] = receipt
    collection_raw = json.dumps(collection, sort_keys=True).encode()
    collection_path.write_bytes(collection_raw)
    manifest["collection_manifest_sha256"] = _sha(collection_raw)
    manifest["pass_plan_sha256"] = {
        path.name: _sha(path.read_bytes()) for path in sorted(plan_root.iterdir())
    }
    pool_raw = pool_path.read_bytes()
    manifest["completed_pool_snapshot"].update({
        "bytes": len(pool_raw), "rows": passes, "sha256": _sha(pool_raw),
        "source_bytes_at_read": len(pool_raw),
    })
    manifest["frozen_pool_snapshot"] = {
        "bytes": len(pool_raw), "rows": passes, "sha256": _sha(pool_raw),
    }


__all__ = ["add_formal_role_provenance", "convert_to_legacy_recovery_prefix"]
