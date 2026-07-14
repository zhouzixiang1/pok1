from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]
TOOLS = ROOT / "bots" / "neural_national_lab" / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import opponent_role_freeze_plan as role_plan  # noqa: E402
from test_freeze_opponent_role_dataset import (  # noqa: E402
    OPPONENTS,
    _collection,
    _roles,
)


def _minimums() -> dict[str, dict[str, int]]:
    return {
        prefix: dict(rows)
        for prefix, rows in role_plan.FORMAL_MINIMUM_ROWS.items()
    }


def _source(tmp_path: Path) -> tuple[Path, Path]:
    source = _collection(tmp_path)
    manifest_path = source / "collection_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["passes_requested"] = role_plan.FORMAL_EXPECTED_PASSES
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    ledger = tmp_path / "exposure_ledger.json"
    ledger.write_text(json.dumps({
        "schema": "opponent_exposure_ledger_v1",
        "events": [{
            "sequence": 1,
            "timestamp_utc": "2026-07-12T00:00:00+00:00",
            "event": "open",
            "role": "train",
            "run_id": "historical",
            "opponents": ["national_v1"],
            "candidate_sha256": None,
            "artifact_sha256": None,
        }],
    }), encoding="utf-8")
    ledger.with_name(f".{ledger.name}.lock").touch()
    return source, ledger


def _create(tmp_path: Path) -> tuple[Path, Path, Path, dict]:
    source, ledger = _source(tmp_path)
    output = tmp_path / "role_plan.json"
    plan = role_plan.create_plan(
        source, output, ledger_path=ledger, role_opponents=_roles(),
        minimum_rows=_minimums(), apply=True,
    )
    return source, ledger, output, plan


def _cli(source: Path, ledger: Path, output: Path) -> list[str]:
    args = [
        "--source-dir", str(source), "--ledger", str(ledger),
        "--output", str(output),
    ]
    for role, names in _roles().items():
        for name in names:
            args.extend([f"--{role.replace('_', '-')}-opponent", name])
    return args


def test_create_plan_binds_atomic_prefix_and_is_no_clobber(tmp_path: Path) -> None:
    source, _ledger, output, plan = _create(tmp_path)

    raw, loaded, receipt = role_plan.load_plan(output)

    assert loaded == plan
    assert receipt["bytes"] == len(raw)
    assert plan["creation_state"]["completed_passes"] == 1
    assert plan["completed_prefix"]["pool_snapshots"]["rows"] == 1
    assert set(plan["completed_prefix"]["pass_plan_sha256"]) == {
        "pass_0001.json"
    }
    assert plan["roles"] == {
        role: [name]
        for role, name in OPPONENTS.items()
        if role != "train"
    }
    assert plan["minimum_rows"] == _minimums()
    assert plan["deployment_policy_value"] is False
    assert plan["strength_evidence"] is False
    assert Path(plan["source_dir"]) == source.resolve()
    with pytest.raises(FileExistsError):
        role_plan.create_plan(
            source, output, ledger_path=Path(plan["ledger_prefix"]["path"]),
            role_opponents=_roles(), minimum_rows=_minimums(), apply=True,
        )


def test_default_plan_build_is_read_only_and_apply_publishes_mode_0444(
    tmp_path: Path,
) -> None:
    source, ledger = _source(tmp_path)
    output = tmp_path / "role_plan.json"
    preview = role_plan.create_plan(
        source, output, ledger_path=ledger, role_opponents=_roles(),
        minimum_rows=_minimums(),
    )
    assert preview["payload_sha256"]
    assert not output.exists()
    role_plan.create_plan(
        source, output, ledger_path=ledger, role_opponents=_roles(),
        minimum_rows=_minimums(), apply=True,
    )
    assert output.stat().st_mode & 0o777 == 0o444


def test_cli_requires_explicit_apply_and_duplicate_apply_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    source, ledger = _source(tmp_path)
    output = tmp_path / "role_plan.json"
    args = _cli(source, ledger, output)
    assert role_plan.main(args) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["status"] == "ready_for_explicit_apply"
    assert not output.exists()
    assert role_plan.main([*args, "--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["status"] == "applied"
    assert output.stat().st_mode & 0o777 == 0o444
    with pytest.raises(SystemExit, match="File exists"):
        role_plan.main([*args, "--apply"])


def test_plan_rejects_wrong_roles_and_weakened_floor_before_freeze(
    tmp_path: Path,
) -> None:
    source, _ledger, output, _plan = _create(tmp_path)
    wrong = _roles()
    wrong["early_stop"] = {OPPONENTS["train"]}
    with pytest.raises(ValueError, match="roles or floors"):
        role_plan.validate_for_freeze(
            output, source_dir=source, role_opponents=wrong,
            minimum_rows=_minimums(),
        )
    weak = _minimums()
    weak["cf"]["train"] = 499
    with pytest.raises(ValueError, match="roles or floors"):
        role_plan.validate_for_freeze(
            output, source_dir=source, role_opponents=_roles(),
            minimum_rows=weak,
        )


def test_plan_rejects_tampered_creation_data_prefix(tmp_path: Path) -> None:
    source, _ledger, output, _plan = _create(tmp_path)
    state_path = source / "collector_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["completed_passes"] = role_plan.FORMAL_EXPECTED_PASSES
    state_path.write_text(json.dumps(state), encoding="utf-8")
    data_path = source / "cf_train.jsonl"
    rows = data_path.read_bytes().splitlines(keepends=True)
    rows[0] = rows[0].replace(b'"status": "ok"', b'"status": "xx"')
    data_path.write_bytes(b"".join(rows))

    with pytest.raises(ValueError, match="prefix changed"):
        role_plan.validate_for_freeze(
            output, source_dir=source, role_opponents=_roles(),
            minimum_rows=_minimums(),
        )


def test_frozen_manifest_replays_plan_and_floors_without_live_inputs(
    tmp_path: Path,
) -> None:
    source, ledger, output, plan = _create(tmp_path)
    frozen = tmp_path / "frozen"
    frozen.mkdir()
    shutil.copyfile(output, frozen / role_plan.PLAN_FILENAME)
    ledger.unlink()
    ratings = Path(plan["ratings_snapshot"]["path"])
    ratings.unlink()
    raw = (frozen / role_plan.PLAN_FILENAME).read_bytes()
    manifest = {
        "source_dir": str(source.resolve()),
        "collection_manifest_sha256": plan["collection_manifest"]["sha256"],
        "roles": {
            "train": [OPPONENTS["train"]],
            **plan["roles"],
        },
        "role_minimum_rows": _minimums(),
        "role_precommit_plan": {
            "filename": role_plan.PLAN_FILENAME,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "payload_sha256": plan["payload_sha256"],
        },
        "outputs": {
            f"{prefix}_{role}.jsonl": {
                "rows": _minimums()[prefix][role]
            }
            for prefix in role_plan.PREFIXES
            for role in role_plan.EVIDENCE_ROLES
        },
    }

    role_plan.validate_frozen_snapshot(
        frozen, manifest, expected_passes=role_plan.FORMAL_EXPECTED_PASSES,
    )
    manifest["role_minimum_rows"]["cf"]["train"] += 1
    with pytest.raises(ValueError, match="binding changed"):
        role_plan.validate_frozen_snapshot(
            frozen, manifest, expected_passes=role_plan.FORMAL_EXPECTED_PASSES,
        )
