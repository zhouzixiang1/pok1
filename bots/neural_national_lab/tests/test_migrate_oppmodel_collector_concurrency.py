from __future__ import annotations

import fcntl
import base64
import hashlib
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]
TOOLS = ROOT / "bots" / "neural_national_lab" / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import freeze_opponent_role_dataset as freeze  # noqa: E402
import migrate_oppmodel_collector_concurrency as migration  # noqa: E402
from role_provenance_fixture import (  # noqa: E402
    add_formal_role_provenance,
    convert_to_legacy_recovery_prefix,
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "collection"
    source.mkdir(parents=True)
    (source / ".collector.lock").write_text("stale\n", encoding="utf-8")
    snapshot = source / "opponent_snapshots" / "fixture" / "national_v1"
    snapshot.mkdir(parents=True)
    (snapshot / "national_bot.py").write_text("# opponent\n", encoding="utf-8")
    snapshot_sha = migration.collector._directory_digest(snapshot)
    registry_entry = {
        "snapshot_path": str(snapshot),
        "tag_commit": "1" * 40,
        "tag_directory_sha256": snapshot_sha,
        "execution_matches_generation_tag": True,
        "source_path": str(snapshot),
        "source_checkout_commit": "2" * 40,
        "execution_directory_sha256": snapshot_sha,
    }
    plan_task = {
        "name": "national_v1",
        "opponent_path": str(snapshot),
        **{key: value for key, value in registry_entry.items() if key != "snapshot_path"},
    }
    plan_root = source / "pass_plans"
    plan_root.mkdir()
    for index in (1, 2):
        _write_json(
            plan_root / f"pass_{index:04d}.json",
            {"pass": index, "tasks": [plan_task]},
        )
    ratings_sha = "c" * 64
    ratings_snapshot_sha = "d" * 64
    pool_lines = [
        (json.dumps({"pass": 1, "ratings_sha256": ratings_sha}) + "\n").encode(),
        (json.dumps({
            "pass": 2,
            "ratings_sha256": ratings_sha,
            "ratings_snapshot_sha256": ratings_snapshot_sha,
        }) + "\n").encode(),
    ]
    (source / "pool_snapshots.jsonl").write_bytes(b"".join(pool_lines))
    totals = {split: 1 for split in migration.SOURCE_SPLITS}
    state_path = source / "collector_state.json"
    _write_json(state_path, {
        "completed_passes": 2,
        "total_rows": totals,
        "total_behavior_rows": totals,
        "updated_at": "fixture",
    })
    for filename in migration.DATA_FILES:
        (source / filename).write_text('{"fixture":1}\n', encoding="utf-8")
    registry_path = source / "opponent_snapshots" / "registry.json"
    _write_json(registry_path, {
        "schema": "opponent_execution_snapshot_v1",
        "opponents": {"national_v1": registry_entry},
    })
    old_collector = "a" * 64
    reviewed = {
        "collection_manifest": "1" * 64,
        "collector_state": "2" * 64,
        "pool_snapshots": hashlib.sha256(pool_lines[0]).hexdigest(),
        "recovery_plan": "3" * 64,
        "legacy_collector": "4" * 64,
        "current_collector": old_collector,
        "identity_migration": "5" * 64,
        "archived_ratings": ratings_sha,
        "opponent_registry": hashlib.sha256(registry_path.read_bytes()).hexdigest(),
        **{
            filename: hashlib.sha256((source / filename).read_bytes()).hexdigest()
            for filename in migration.DATA_FILES
        },
    }
    legacy_unsigned = {
        "schema_version": freeze.LEGACY_RECOVERY_SCHEMA_VERSION,
        "mode": freeze.LEGACY_RECOVERY_MODE,
        "completed_prefix_pass": 1,
        "recovered_pass": 2,
        "expectations_sha256": "8" * 64,
        "recovery_tool_sha256": hashlib.sha256(
            (TOOLS / "recover_legacy_oppmodel_collection.py").read_bytes()
        ).hexdigest(),
        "reviewed_hashes": reviewed,
        "completed_plan_sha256": {
            "pass_0001.json": hashlib.sha256(
                (plan_root / "pass_0001.json").read_bytes()
            ).hexdigest(),
        },
        "before": {
            "collection_manifest_sha256": reviewed["collection_manifest"],
            "collector_state_sha256": reviewed["collector_state"],
            "pool_snapshots_sha256": reviewed["pool_snapshots"],
            "recovery_plan_sha256": reviewed["recovery_plan"],
            "legacy_collector_sha256": reviewed["legacy_collector"],
        },
        "archived_ratings": {
            "ratings_sha256": reviewed["archived_ratings"],
            "ratings_snapshot_sha256": ratings_snapshot_sha,
            "identity_migration_sha256": reviewed["identity_migration"],
        },
        "tail": {"fixture": {"value_rows": 1, "behavior_rows": 1}},
        "after": {
            "collector_schema_version": migration.SOURCE_SCHEMA_VERSION,
            "collector_sha256": old_collector,
            "pass_plan_schema_version": migration.collector.PASS_PLAN_SCHEMA_VERSION,
            "recovery_plan_sha256": hashlib.sha256(
                (plan_root / "pass_0002.json").read_bytes()
            ).hexdigest(),
            "pool_snapshots_sha256": hashlib.sha256(
                b"".join(pool_lines)
            ).hexdigest(),
            "collector_state_sha256": hashlib.sha256(
                state_path.read_bytes()
            ).hexdigest(),
            "total_rows": totals,
            "total_behavior_rows": totals,
        },
        "probe_execution_count": 0,
        "read_current_ratings": False,
        "strength_evidence": False,
        "deployment_policy_value": False,
    }
    legacy_receipt = {
        **legacy_unsigned,
        "receipt_sha256": freeze._canonical_sha256(legacy_unsigned),
    }
    _write_json(source / "collection_manifest.json", {
        "passes_requested": 160,
        "resume_contract": {
            "schema_version": migration.SOURCE_SCHEMA_VERSION,
            "workers": migration.SOURCE_WORKERS,
            "probe_workers": migration.SOURCE_PROBE_WORKERS,
            "collector_sha256": old_collector,
            "semantic_field": "unchanged",
        },
        "legacy_recovery": legacy_receipt,
    })
    return source


def _run(source: Path, *, apply: bool) -> int:
    args = [
        "--source-dir", str(source),
        "--expected-boundary", "2",
        "--workers", "6",
        "--probe-workers", "2",
    ]
    if apply:
        args.append("--apply")
    return migration.main(args)


def test_dry_run_is_read_only_and_has_false_authority(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _source(tmp_path)
    before = (source / "collection_manifest.json").read_bytes()

    assert _run(source, apply=False) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ready_for_explicit_apply"
    assert result["max_concurrent_native_matches"] == 12
    assert result["probe_execution_count"] == 0
    assert result["read_current_ratings"] is False
    assert result["strength_evidence"] is False
    assert result["deployment_policy_value"] is False
    assert (source / "collection_manifest.json").read_bytes() == before


def test_apply_publishes_replayable_schema6_receipt_and_survives_append(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    assert _run(source, apply=True) == 0
    manifest = json.loads(
        (source / "collection_manifest.json").read_text(encoding="utf-8")
    )
    contract = manifest["resume_contract"]
    assert (contract["schema_version"], contract["workers"], contract["probe_workers"]) == (
        6, 6, 2
    )
    receipt = manifest["concurrency_migration"]
    unsigned = dict(receipt)
    assert unsigned.pop("receipt_sha256") == freeze._canonical_sha256(unsigned)
    assert freeze._concurrency_migration_contract(
        manifest,
        completed_passes=2,
        source_dir=source,
        validate_data_prefix=True,
    )[0] == 2

    with (source / "pool_snapshots.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"pass":3}\n')
    _write_json(source / "pass_plans" / "pass_0003.json", {"pass": 3})
    for filename in migration.DATA_FILES:
        with (source / filename).open("a", encoding="utf-8") as handle:
            handle.write('{"fixture":2}\n')
    assert freeze._concurrency_migration_contract(
        manifest,
        completed_passes=3,
        source_dir=source,
        validate_data_prefix=True,
    )[0] == 2


def test_receipt_tamper_and_semantic_contract_change_fail_closed(tmp_path: Path) -> None:
    source = _source(tmp_path)
    assert _run(source, apply=True) == 0
    manifest = json.loads(
        (source / "collection_manifest.json").read_text(encoding="utf-8")
    )
    manifest["concurrency_migration"]["after"]["workers"] = 5
    with pytest.raises(RuntimeError, match="receipt digest mismatch"):
        freeze._concurrency_migration_contract(
            manifest,
            completed_passes=2,
            source_dir=source,
            validate_data_prefix=True,
        )

    manifest = json.loads(
        (source / "collection_manifest.json").read_text(encoding="utf-8")
    )
    manifest["resume_contract"]["semantic_field"] = "changed"
    unsigned = dict(manifest["concurrency_migration"])
    unsigned.pop("receipt_sha256")
    manifest["concurrency_migration"]["receipt_sha256"] = freeze._canonical_sha256(
        unsigned
    )
    with pytest.raises(RuntimeError, match="semantic collection fields"):
        freeze._concurrency_migration_contract(
            manifest,
            completed_passes=2,
            source_dir=source,
            validate_data_prefix=True,
        )


def test_invalid_legacy_receipt_fails_before_manifest_publish(tmp_path: Path) -> None:
    source = _source(tmp_path)
    path = source / "collection_manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["legacy_recovery"]["receipt_sha256"] = "0" * 64
    _write_json(path, manifest)
    before = path.read_bytes()

    with pytest.raises(RuntimeError, match="legacy recovery receipt digest mismatch"):
        _run(source, apply=True)

    assert path.read_bytes() == before


def test_resigned_legacy_cross_link_tamper_fails_before_publish(tmp_path: Path) -> None:
    source = _source(tmp_path)
    path = source / "collection_manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    legacy = manifest["legacy_recovery"]
    legacy["reviewed_hashes"]["current_collector"] = "e" * 64
    unsigned = dict(legacy)
    unsigned.pop("receipt_sha256")
    legacy["receipt_sha256"] = freeze._canonical_sha256(unsigned)
    _write_json(path, manifest)
    before = path.read_bytes()

    with pytest.raises(RuntimeError, match="reviewed-artifact links changed"):
        _run(source, apply=True)

    assert path.read_bytes() == before


def test_registry_and_snapshot_tamper_fail_before_publish(tmp_path: Path) -> None:
    source = _source(tmp_path / "registry")
    manifest_path = source / "collection_manifest.json"
    registry_path = source / "opponent_snapshots" / "registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["tampered"] = True
    _write_json(registry_path, registry)
    before = manifest_path.read_bytes()
    with pytest.raises(RuntimeError, match="opponent registry changed"):
        _run(source, apply=True)
    assert manifest_path.read_bytes() == before

    source = _source(tmp_path / "snapshot")
    manifest_path = source / "collection_manifest.json"
    registry = json.loads(
        (source / "opponent_snapshots" / "registry.json").read_text(
            encoding="utf-8"
        )
    )
    snapshot = Path(registry["opponents"]["national_v1"]["snapshot_path"])
    (snapshot / "national_bot.py").write_text("# tampered\n", encoding="utf-8")
    before = manifest_path.read_bytes()
    with pytest.raises(RuntimeError, match="snapshot changed"):
        _run(source, apply=True)
    assert manifest_path.read_bytes() == before

def test_running_collector_and_non_atomic_plan_prefix_block_apply(tmp_path: Path) -> None:
    source = _source(tmp_path)
    with (source / ".collector.lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SystemExit, match="collector is running"):
            _run(source, apply=True)
    _write_json(source / "pass_plans" / "pass_0003.json", {"pass": 3})
    with pytest.raises(RuntimeError, match="exact completed pass-plan prefix"):
        _run(source, apply=True)


def test_reviewed_topology_and_exact_change_set_are_enforced(tmp_path: Path) -> None:
    source = _source(tmp_path)
    manifest = json.loads(
        (source / "collection_manifest.json").read_text(encoding="utf-8")
    )
    with pytest.raises(RuntimeError, match="reviewed migration topology"):
        migration._new_contract(
            manifest["resume_contract"], workers=4, probe_workers=3
        )
    current_hash = hashlib.sha256(Path(migration.collector.__file__).read_bytes()).hexdigest()
    manifest["resume_contract"]["collector_sha256"] = current_hash
    with pytest.raises(RuntimeError, match="unsupported collector contract changes"):
        migration._new_contract(
            manifest["resume_contract"], workers=6, probe_workers=2
        )


def test_formal_freezer_replays_mixed_1x4_then_6x2_profiles(tmp_path: Path) -> None:
    root = tmp_path / "frozen"
    root.mkdir()
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "national_bot.py").write_text("# candidate\n", encoding="utf-8")
    role_manifest = {
        "candidate_snapshot": {"path": str(candidate), "sha256": ""},
        "roles": {
            "train": ["national_v1"],
            "early_stop": ["national_v2"],
            "model_calibration": ["national_v3"],
            "policy_selection": ["national_v4"],
            "policy_gate": ["national_v5"],
        },
    }
    add_formal_role_provenance(root, role_manifest, passes=3)
    convert_to_legacy_recovery_prefix(root, role_manifest, completed_prefix=1)

    collection_path = root / "collection_manifest.json"
    previous = json.loads(collection_path.read_text(encoding="utf-8"))
    old_contract = dict(previous["resume_contract"])
    old_contract.update({
        "schema_version": migration.SOURCE_SCHEMA_VERSION,
        "workers": migration.SOURCE_WORKERS,
        "probe_workers": migration.SOURCE_PROBE_WORKERS,
        "collector_sha256": "a" * 64,
    })
    previous["resume_contract"] = old_contract
    previous["legacy_recovery"]["after"].update({
        "collector_schema_version": migration.SOURCE_SCHEMA_VERSION,
        "collector_sha256": old_contract["collector_sha256"],
    })
    previous["legacy_recovery"]["reviewed_hashes"]["current_collector"] = (
        old_contract["collector_sha256"]
    )
    legacy_unsigned = dict(previous["legacy_recovery"])
    legacy_unsigned.pop("receipt_sha256")
    previous["legacy_recovery"]["receipt_sha256"] = freeze._canonical_sha256(
        legacy_unsigned
    )
    previous_raw = json.dumps(previous, sort_keys=True).encode("utf-8")

    current_contract = dict(old_contract)
    current_contract.update({
        "schema_version": migration.TARGET_SCHEMA_VERSION,
        "workers": migration.TARGET_WORKERS,
        "probe_workers": migration.TARGET_PROBE_WORKERS,
        "collector_sha256": hashlib.sha256(
            Path(migration.collector.__file__).read_bytes()
        ).hexdigest(),
    })
    pool_path = root / "pool_snapshots.completed.jsonl"
    pool_lines = pool_path.read_bytes().splitlines(keepends=True)
    third = json.loads(pool_lines[2])
    third.update({"workers": 6, "probe_workers": 2})
    pool_lines[2] = (json.dumps(third, separators=(",", ":")) + "\n").encode()
    pool_path.write_bytes(b"".join(pool_lines))
    boundary_pool = b"".join(pool_lines[:2])
    state = {
        "completed_passes": 2,
        "total_rows": {split: 0 for split in migration.SOURCE_SPLITS},
        "total_behavior_rows": {split: 0 for split in migration.SOURCE_SPLITS},
        "updated_at": "fixture",
    }
    state_raw = json.dumps(state, indent=2, sort_keys=True).encode()
    empty_sha = hashlib.sha256(b"").hexdigest()
    before = {
        "resume_contract_sha256": freeze._canonical_sha256(old_contract),
        "schema_version": 5,
        "workers": 1,
        "probe_workers": 4,
        "collector_sha256": old_contract["collector_sha256"],
    }
    after = {
        "resume_contract_sha256": freeze._canonical_sha256(current_contract),
        "schema_version": 6,
        "workers": 6,
        "probe_workers": 2,
        "collector_sha256": current_contract["collector_sha256"],
        "max_concurrent_native_matches": 12,
    }
    unsigned = {
        "schema_version": migration.MIGRATION_SCHEMA_VERSION,
        "mode": migration.MIGRATION_MODE,
        "boundary_pass": 2,
        "migration_tool_sha256": hashlib.sha256(
            Path(migration.__file__).read_bytes()
        ).hexdigest(),
        "previous_manifest": {
            "bytes": len(previous_raw),
            "sha256": hashlib.sha256(previous_raw).hexdigest(),
            "bytes_base64": base64.b64encode(previous_raw).decode("ascii"),
        },
        "legacy_recovery_receipt_sha256": previous["legacy_recovery"][
            "receipt_sha256"
        ],
        "before": before,
        "after": after,
        "completed_prefix": {
            "collector_state": {
                "bytes": len(state_raw),
                "sha256": hashlib.sha256(state_raw).hexdigest(),
                "bytes_base64": base64.b64encode(state_raw).decode("ascii"),
            },
            "pool_snapshots": {
                "rows": 2,
                "bytes": len(boundary_pool),
                "sha256": hashlib.sha256(boundary_pool).hexdigest(),
            },
            "pass_plan_sha256": {
                f"pass_{index:04d}.json": hashlib.sha256(
                    (root / "pass_plans" / f"pass_{index:04d}.json").read_bytes()
                ).hexdigest()
                for index in (1, 2)
            },
            "data": {
                filename: {"rows": 0, "bytes": 0, "sha256": empty_sha}
                for filename in migration.DATA_FILES
            },
        },
        "probe_execution_count": 0,
        "read_current_ratings": False,
        "strength_evidence": False,
        "deployment_policy_value": False,
    }
    receipt = {**unsigned, "receipt_sha256": freeze._canonical_sha256(unsigned)}
    current = dict(previous)
    current["resume_contract"] = current_contract
    current["concurrency_migration"] = receipt
    collection_raw = json.dumps(current, sort_keys=True).encode()
    collection_path.write_bytes(collection_raw)

    pool_raw = pool_path.read_bytes()
    role_manifest["collection_manifest_sha256"] = hashlib.sha256(
        collection_raw
    ).hexdigest()
    role_manifest["completed_pool_snapshot"].update({
        "bytes": len(pool_raw), "rows": 3,
        "sha256": hashlib.sha256(pool_raw).hexdigest(),
        "source_bytes_at_read": len(pool_raw),
    })
    role_manifest["frozen_pool_snapshot"] = {
        "bytes": len(pool_raw), "rows": 3,
        "sha256": hashlib.sha256(pool_raw).hexdigest(),
    }

    freeze.validate_frozen_role_provenance(
        root, role_manifest, expected_passes=3
    )
