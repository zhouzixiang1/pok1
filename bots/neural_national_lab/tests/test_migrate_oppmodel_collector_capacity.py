from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[3]
TOOLS = ROOT / "bots" / "neural_national_lab" / "tools"
TESTS = Path(__file__).resolve().parent
for path in (TOOLS, TESTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import collector_systemd_quiescence as systemd_quiescence  # noqa: E402
import freeze_opponent_role_dataset as freeze  # noqa: E402
import migrate_oppmodel_collector_capacity as migration  # noqa: E402
import test_migrate_oppmodel_collector_concurrency as schema6_fixture  # noqa: E402


UNIT = "neural-v4-collector.service"
REAL_VERIFY_QUIESCENCE = migration._verify_collector_quiescence


def _quiescence(
    source: Path, unit: str = UNIT, running_unit: dict | None = None
) -> dict:
    running_unit = running_unit or _running_unit(source, unit)
    return {
        "schema": migration.QUIESCENCE_SCHEMA,
        "collector_unit": unit,
        "source_dir": str(source.resolve()),
        "load_state": "loaded",
        "transient": "yes",
        "kill_mode": "control-group",
        "restart": "no",
        "collect_mode": "inactive",
        "main_pid": 0,
        "active_state": "inactive",
        "control_group": "/fixture/collector.service",
        "unit_disposition": "loaded_inactive",
        "running_unit": running_unit,
        "control_groups_checked": ["/fixture/collector.service"],
        "control_group_present": False,
        "cgroup_process_count": 0,
        "process_scan_uid": os.getuid(),
        "process_markers": list(migration.PROCESS_MARKERS),
        "matching_process_count": 0,
    }


@pytest.fixture(autouse=True)
def _mock_quiescence(monkeypatch: pytest.MonkeyPatch) -> None:
    def verify(unit, source, running_unit=None):
        return _quiescence(Path(source), unit, running_unit)

    monkeypatch.setattr(
        migration,
        "_verify_collector_quiescence",
        verify,
    )


def _running_unit(
    source: Path, unit: str = UNIT, intent: dict | None = None
) -> dict:
    if intent is None:
        if (source / "collector_state.json").exists():
            intent = migration._migration_intent(
                source, boundary=2, workers=6, probe_workers=4,
                max_active_native_matches=24, capacity_total_slots=28,
                capacity_first_slot=4,
            )
        else:
            intent = {
                "schema": "fixture_migration_intent_v1",
                "source_dir": str(source.resolve()),
                "source_collector_sha256": hashlib.sha256(
                    (TOOLS / "longrun_collect_oppmodel.py").read_bytes()
                ).hexdigest(),
            }
    argv = [
        "python", "longrun_collect_oppmodel.py", "--out-dir",
        str(source.resolve()),
    ]
    unsigned = {
        "schema": migration.RUNNING_UNIT_SCHEMA,
        "collector_unit": unit,
        "source_dir": str(source.resolve()),
        "load_state": "loaded",
        "transient": "yes",
        "kill_mode": "control-group",
        "restart": "no",
        "collect_mode": "inactive",
        "main_pid": 123,
        "active_state": "active",
        "control_group": "/fixture/collector.service",
        "working_directory": str(TOOLS.resolve()),
        "invocation_id": "a" * 32,
        "exec_start": f"{{ path=/python ; argv[]={' '.join(argv)} ; ignore_errors=no ; }}",
        "process_argv": argv,
        "process_scan_uid": os.getuid(),
        "process_start_ticks": 1,
        "process_state": "T",
        "process_cgroups": ["/fixture/collector.service"],
        "process_cwd": str(TOOLS.resolve()),
        "collector_script_path": str(
            (TOOLS / "longrun_collect_oppmodel.py").resolve()
        ),
        "collector_script_sha256": intent["source_collector_sha256"],
        "boot_id": systemd_quiescence._boot_id(),
        "captured_monotonic_ns": time.monotonic_ns(),
        "stop_requested_by_tool": True,
        "migration_intent": intent,
    }
    return {
        **unsigned,
        "receipt_sha256": systemd_quiescence._canonical_sha256(unsigned),
    }


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _resign(payload: dict, *, field: str = "receipt_sha256") -> None:
    unsigned = dict(payload)
    unsigned.pop(field, None)
    payload[field] = freeze._canonical_sha256(unsigned)


def _prepare_full_schema5_source(
    tmp_path: Path, *, legacy_completed_plan: bool = True
) -> tuple[Path, Path, Path]:
    source = schema6_fixture._source(tmp_path)
    manifest_path = source / "collection_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for prefix in ("cf", "opponent_actions"):
        for split_index, split in enumerate(migration.SOURCE_SPLITS):
            row = {
                "opponent": "national_v1",
                "_opponent_label": "national_v1",
                "deck_seed_base": 1_000 + split_index,
                "bot_seed_base": 2_000 + split_index,
                "hand": 1,
                "hand_decision_index": 0,
            }
            (source / f"{prefix}_{split}.jsonl").write_text(
                json.dumps(row, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
    manifest["passes_requested"] = 3
    candidate = source / "candidate"
    candidate.mkdir()
    (candidate / "national_bot.py").write_text("# candidate\n", encoding="utf-8")
    candidate_digest = migration.collector._directory_digest(candidate)
    candidate_execution = (
        source / "candidate_snapshot" / candidate_digest / candidate.name
    ).resolve()
    ratings = source / "ratings.json"
    ratings.write_text(
        json.dumps({"national_v1": {"r": 1500.0, "rd": 50.0}}),
        encoding="utf-8",
    )
    ratings_snapshot = migration.collector._capture_ratings_snapshot(ratings)
    contract = manifest["resume_contract"]
    contract.pop("semantic_field", None)
    contract.update({
        "candidate": str(candidate.resolve()),
        "candidate_sha256": candidate_digest,
        "candidate_execution_path": str(candidate_execution),
        "candidate_snapshot_sha256": candidate_digest,
        "ratings_path": str(ratings.resolve()),
        "hands": 1,
        "timeout_sec": 1,
        "strongest": 1,
        "val_opponents": [],
        "held_out_opponents": [],
        "opponents_per_pass": 1,
        "max_decisions": 1,
        "max_alternatives": 1,
        "decision_sampling": "uniform",
        "hand_windows": [0.0],
        "deck_seed_scheme": "disjoint_match_blocks_v1",
        "deck_seed_base": migration.collector.DEFAULT_DECK_SEED_BASE,
        "deck_seed_guard": migration.collector.DEFAULT_DECK_SEED_GUARD,
        "deck_seed_slots_per_pass": migration.collector.DECK_SEED_SLOTS_PER_PASS,
        "bot_seed_base": migration.collector.DEFAULT_BOT_SEED_BASE,
        "probe_sha256": hashlib.sha256(migration.PROBE_PATH.read_bytes()).hexdigest(),
        "cross_hand_sequence_sha256": hashlib.sha256(
            (TOOLS / "cross_hand_sequence.py").read_bytes()
        ).hexdigest(),
    })
    registry = json.loads(
        (source / "opponent_snapshots" / "registry.json").read_text(
            encoding="utf-8"
        )
    )
    registered = registry["opponents"]["national_v1"]
    plan_hashes = {}
    for pass_number in (1, 2):
        deck_seed = migration.collector._deck_seed_for_task(
            root=contract["deck_seed_base"],
            pass_index=pass_number - 1,
            task_index=0,
            hands=1,
            guard=contract["deck_seed_guard"],
        )
        task = {
            "name": "national_v1",
            "opponent_path": registered["snapshot_path"],
            "split": "train",
            "hands": 1,
            "deck_seed_base": deck_seed,
            "deck_seed_last": deck_seed,
            "bot_seed_base": migration.collector._bot_seed_for_task(
                root=contract["bot_seed_base"],
                pass_index=pass_number - 1,
                task_index=0,
            ),
            "tag_commit": registered["tag_commit"],
            "tag_directory_sha256": registered["tag_directory_sha256"],
            "execution_matches_generation_tag": True,
            "source_path": registered["source_path"],
            "source_checkout_commit": registered["source_checkout_commit"],
            "execution_directory_sha256": registered[
                "execution_directory_sha256"
            ],
        }
        plan = {
            "schema_version": migration.collector.PASS_PLAN_SCHEMA_VERSION,
            "pass": pass_number,
            "seed_scheme": "disjoint_match_blocks_v1",
            "ratings_snapshot": ratings_snapshot,
            "tasks": [task],
        }
        if legacy_completed_plan and pass_number == 1:
            plan.pop("schema_version")
            plan.pop("ratings_snapshot")
        path = source / "pass_plans" / f"pass_{pass_number:04d}.json"
        _write_json(path, plan)
        plan_hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    legacy = manifest["legacy_recovery"]
    for filename in migration.DATA_FILES:
        legacy["reviewed_hashes"][filename] = hashlib.sha256(
            (source / filename).read_bytes()
        ).hexdigest()
    legacy["completed_plan_sha256"] = {"pass_0001.json": plan_hashes["pass_0001.json"]}
    legacy["after"]["recovery_plan_sha256"] = plan_hashes["pass_0002.json"]
    _resign(legacy)
    _write_json(manifest_path, manifest)
    return source, candidate, ratings


def _schema6_source(
    tmp_path: Path,
    *,
    planned_tail: bool = False,
    legacy_completed_plan: bool = True,
) -> tuple[Path, Path, Path]:
    source, candidate, ratings = _prepare_full_schema5_source(
        tmp_path, legacy_completed_plan=legacy_completed_plan
    )
    assert schema6_fixture._run(source, apply=True) == 0
    manifest_path = source / "collection_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # The synthetic schema5->6 tool runs from the current checkout. Rebind its
    # target to a distinct historical schema-6 collector while retaining every
    # receipt cross-link, matching the real on-disk migration shape.
    historical_collector = "e" * 64
    manifest["resume_contract"]["collector_sha256"] = historical_collector
    concurrency = manifest["concurrency_migration"]
    concurrency["after"]["collector_sha256"] = historical_collector
    concurrency["after"]["resume_contract_sha256"] = freeze._canonical_sha256(
        manifest["resume_contract"]
    )
    _resign(concurrency)
    _write_json(manifest_path, manifest)

    if planned_tail:
        second = json.loads(
            (source / "pass_plans" / "pass_0002.json").read_text(
                encoding="utf-8"
            )
        )
        second["pass"] = 3
        task = second["tasks"][0]
        task["deck_seed_base"] = migration.collector._deck_seed_for_task(
            root=manifest["resume_contract"]["deck_seed_base"],
            pass_index=2,
            task_index=0,
            hands=1,
            guard=manifest["resume_contract"]["deck_seed_guard"],
        )
        task["deck_seed_last"] = task["deck_seed_base"]
        task["bot_seed_base"] = migration.collector._bot_seed_for_task(
            root=manifest["resume_contract"]["bot_seed_base"],
            pass_index=2,
            task_index=0,
        )
        _write_json(source / "pass_plans" / "pass_0003.json", second)
    return source, candidate, ratings


def _args(
    source: Path, *, apply: bool, running_receipt: bool = True
) -> list[str]:
    args = [
        "--source-dir", str(source),
        "--expected-boundary", "2",
        "--workers", "6",
        "--probe-workers", "4",
        "--max-active-native-matches", "24",
        "--capacity-total-slots", "28",
        "--capacity-first-slot", "4",
        "--collector-unit", UNIT,
    ]
    if running_receipt:
        receipt_path = source / ".test-running-unit.json"
        receipt_path.write_text(
            json.dumps(_running_unit(source), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        args.extend(["--running-unit-receipt", str(receipt_path)])
    if apply:
        args.append("--apply")
    return args


def test_schema7_capacity_migration_dry_run_is_read_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source, _candidate, _ratings = _schema6_source(tmp_path)
    manifest_path = source / "collection_manifest.json"
    before = manifest_path.read_bytes()
    capsys.readouterr()

    assert migration.main(_args(source, apply=False)) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ready_for_explicit_apply"
    assert result["probe_execution_count"] == 0
    assert result["read_current_ratings"] is False
    assert result["strength_evidence"] is False
    assert result["deployment_policy_value"] is False
    assert manifest_path.read_bytes() == before


def test_schema7_capacity_migration_replays_receipted_legacy_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source, _candidate, _ratings = _schema6_source(
        tmp_path, legacy_completed_plan=True
    )
    legacy_plan = json.loads(
        (source / "pass_plans" / "pass_0001.json").read_text(
            encoding="utf-8"
        )
    )
    assert set(legacy_plan) == {"pass", "seed_scheme", "tasks"}
    capsys.readouterr()

    assert migration.main(_args(source, apply=False)) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ready_for_explicit_apply"
    assert json.loads(
        (source / "pass_plans" / "pass_0001.json").read_text(
            encoding="utf-8"
        )
    ) == legacy_plan

    legacy_plan["tasks"][0]["bot_seed_base"] += 1
    _write_json(source / "pass_plans" / "pass_0001.json", legacy_plan)
    with pytest.raises(
        RuntimeError,
        match="concurrency migration plan changed|legacy completed pass plan changed",
    ):
        migration._migration_intent(
            source,
            boundary=2,
            workers=6,
            probe_workers=4,
            max_active_native_matches=24,
            capacity_total_slots=28,
            capacity_first_slot=4,
        )


def test_cli_rejects_missing_running_unit_receipt(tmp_path: Path) -> None:
    source, _candidate, _ratings = _schema6_source(tmp_path)

    with pytest.raises(SystemExit, match="running-unit-receipt is required"):
        migration.main(_args(source, apply=False, running_receipt=False))


def test_cli_captures_and_embeds_running_unit_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, _candidate, _ratings = _schema6_source(tmp_path)
    running = _running_unit(source)
    receipt_path = tmp_path / "running-unit.json"
    def stop(unit, bound_source, intent_factory, path):
        intent = intent_factory()
        assert unit == UNIT
        assert bound_source == source.resolve()
        assert intent == running["migration_intent"]
        systemd_quiescence.write_running_unit_receipt(path, running)
        return running, _quiescence(source, running_unit=running)

    monkeypatch.setattr(systemd_quiescence, "stop_bound_collector", stop)
    capture_args = _args(source, apply=False, running_receipt=False) + [
        "--capture-running-unit-receipt", str(receipt_path),
    ]

    assert migration.main(capture_args) == 0
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == running
    capsys.readouterr()

    apply_args = _args(source, apply=True, running_receipt=False) + [
        "--running-unit-receipt", str(receipt_path),
    ]
    assert migration.main(apply_args) == 0
    manifest = json.loads(
        (source / "collection_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest[migration.MIGRATION_KEY][
        "collector_quiescence"
    ]["running_unit"] == running


def test_apply_preserves_schema6_receipt_binds_tail_and_replays(
    tmp_path: Path,
) -> None:
    source, _candidate, _ratings = _schema6_source(tmp_path, planned_tail=True)
    manifest_path = source / "collection_manifest.json"
    before = json.loads(manifest_path.read_text(encoding="utf-8"))
    old_receipt = before["concurrency_migration"]
    tail_path = source / "pass_plans" / "pass_0003.json"
    tail_sha256 = hashlib.sha256(tail_path.read_bytes()).hexdigest()
    quiescence = _quiescence(source)

    assert migration.main(_args(source, apply=True)) == 0

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["concurrency_migration"] == old_receipt
    contract = manifest["resume_contract"]
    assert (contract["schema_version"], contract["workers"], contract["probe_workers"]) == (
        7, 6, 4
    )
    assert contract["max_active_native_matches"] == 24
    assert (contract["capacity_first_slot"], contract["capacity_total_slots"]) == (
        4, 28
    )
    receipt = manifest[migration.MIGRATION_KEY]
    assert receipt["planned_tail"]["sha256"] == tail_sha256
    assert receipt["planned_tail"]["published_rows_at_migration"] == 0
    assert receipt["planned_tail"]["execution_status"] == (
        migration.TAIL_EXECUTION_STATUS
    )
    replay = migration.replay_migration(
        manifest,
        completed_passes=2,
        source_dir=source,
        validate_data_prefix=True,
    )
    assert replay is not None
    assert (replay["schema6_boundary"], replay["boundary"]) == (2, 2)

    with pytest.raises(RuntimeError, match="unmigrated schema-6 manifest"):
        migration.build_migration(
            source,
            boundary=2,
            workers=6,
            probe_workers=4,
            max_active_native_matches=24,
            capacity_total_slots=28,
            capacity_first_slot=4,
            collector_quiescence=quiescence,
        )


def test_schema7_resume_reuses_bound_plan_with_no_published_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, candidate, ratings = _schema6_source(tmp_path, planned_tail=True)
    assert migration.main(_args(source, apply=True)) == 0
    tail = source / "pass_plans" / "pass_0003.json"
    tail_before = tail.read_bytes()
    monkeypatch.setattr(
        migration.collector,
        "probe_one",
        lambda _candidate, _opponent, _split, name, *_args: (0, 0, name),
    )

    assert migration.collector.main([
        "--candidate", str(candidate),
        "--out-dir", str(source),
        "--passes", "3",
        "--workers", "6",
        "--probe-workers", "4",
        "--max-active-native-matches", "24",
        "--capacity-total-slots", "28",
        "--capacity-first-slot", "4",
        "--hands", "1",
        "--timeout-sec", "1",
        "--ratings", str(ratings),
        "--strongest", "1",
        "--opponents-per-pass", "1",
        "--max-decisions", "1",
        "--max-alternatives", "1",
        "--decision-sampling", "uniform",
        "--hand-windows", "0.0",
    ]) == 0

    assert tail.read_bytes() == tail_before
    state = json.loads((source / "collector_state.json").read_text(encoding="utf-8"))
    assert state["completed_passes"] == 3


def test_running_lock_nonboundary_and_tmp_outputs_fail_closed(tmp_path: Path) -> None:
    source, _candidate, _ratings = _schema6_source(tmp_path / "running")
    with (source / ".collector.lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SystemExit, match="collector is running"):
            migration.main(_args(source, apply=True))

    source, _candidate, _ratings = _schema6_source(tmp_path / "state")
    state_path = source / "collector_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["completed_passes"] = 1
    _write_json(state_path, state)
    with pytest.raises(RuntimeError, match="requested migration boundary"):
        migration.main(_args(source, apply=True))

    source, _candidate, _ratings = _schema6_source(tmp_path / "tmp")
    (source / "_tmp_inflight.json").write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="atomic schema-6 prefix"):
        migration.main(_args(source, apply=True))


def test_extra_plan_or_data_past_boundary_is_rejected(tmp_path: Path) -> None:
    source, _candidate, _ratings = _schema6_source(tmp_path / "plans")
    (source / "pass_plans" / "pass_0004.json").write_text(
        '{"pass":4}\n', encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="at most one next-pass plan"):
        migration.main(_args(source, apply=True))

    source, _candidate, _ratings = _schema6_source(tmp_path / "data")
    with (source / "cf_train.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"past_boundary":true}\n')
    with pytest.raises(RuntimeError, match="exact atomic prefix"):
        migration.main(_args(source, apply=True))


@pytest.mark.parametrize(
    ("filename", "state_field", "modality"),
    [
        ("cf_train.jsonl", "total_rows", "cf"),
        (
            "opponent_actions_train.jsonl",
            "total_behavior_rows",
            "opponent_actions",
        ),
    ],
)
def test_duplicate_stable_row_identity_is_rejected(
    tmp_path: Path, filename: str, state_field: str, modality: str
) -> None:
    source, _candidate, _ratings = _schema6_source(tmp_path)
    path = source / filename
    path.write_bytes(path.read_bytes() * 2)
    state_path = source / "collector_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state[state_field]["train"] = 2
    _write_json(state_path, state)

    with pytest.raises(
        RuntimeError, match=f"duplicate collector row identity in {modality}"
    ):
        migration.main(_args(source, apply=True))


@pytest.mark.parametrize(
    "target",
    ["state", "pool", "plan", "tail", "data", "registry", "snapshot", "tmp"],
)
def test_publish_revalidates_all_noncooperative_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    source, _candidate, _ratings = _schema6_source(
        tmp_path, planned_tail=True
    )
    manifest_path = source / "collection_manifest.json"
    before = manifest_path.read_bytes()
    original = migration.build_migration

    def racing_build(*args, **kwargs):
        result = original(*args, **kwargs)
        if target == "state":
            (source / "collector_state.json").write_bytes(
                (source / "collector_state.json").read_bytes() + b"\n"
            )
        elif target == "pool":
            with (source / "pool_snapshots.jsonl").open("ab") as handle:
                handle.write(b'{}\n')
        elif target == "plan":
            (source / "pass_plans" / "pass_0002.json").write_bytes(b'{}\n')
        elif target == "tail":
            (source / "pass_plans" / "pass_0003.json").write_bytes(b'{}\n')
        elif target == "data":
            path = source / "cf_train.jsonl"
            row = json.loads(path.read_text(encoding="utf-8"))
            row["race"] = True
            path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        elif target == "registry":
            path = source / "opponent_snapshots" / "registry.json"
            path.write_bytes(path.read_bytes() + b"\n")
        elif target == "snapshot":
            registry = json.loads(
                (source / "opponent_snapshots" / "registry.json").read_text()
            )
            snapshot = Path(
                registry["opponents"]["national_v1"]["snapshot_path"]
            )
            (snapshot / "national_bot.py").write_text(
                "# raced\n", encoding="utf-8"
            )
        else:
            (source / "_tmp_race.json").write_text("{}", encoding="utf-8")
        return result

    monkeypatch.setattr(migration, "build_migration", racing_build)
    with pytest.raises(RuntimeError):
        migration.main(_args(source, apply=True))
    assert manifest_path.read_bytes() == before


@pytest.mark.parametrize(
    "field",
    [
        "schema_version", "workers", "probe_workers",
        "max_active_native_matches", "capacity_total_slots",
        "capacity_first_slot",
    ],
)
def test_float_schema_or_topology_is_rejected(tmp_path: Path, field: str) -> None:
    source, _candidate, _ratings = _schema6_source(tmp_path)
    manifest = json.loads(
        (source / "collection_manifest.json").read_text(encoding="utf-8")
    )
    if field in {"schema_version", "workers", "probe_workers"}:
        old = manifest["resume_contract"]
        old[field] = float(old[field])
        with pytest.raises(RuntimeError, match="integer"):
            migration._new_contract(
                old, workers=6, probe_workers=4,
                max_active_native_matches=24, capacity_total_slots=28,
                capacity_first_slot=4,
            )
    else:
        current = migration._new_contract(
            manifest["resume_contract"], workers=6, probe_workers=4,
            max_active_native_matches=24, capacity_total_slots=28,
            capacity_first_slot=4,
        )
        current[field] = float(current[field])
        assert migration.current_contract_is_reviewed(current) is False


def test_systemd_quiescence_is_machine_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = "\n".join([
        "LoadState=loaded", "Transient=yes", "KillMode=control-group",
        "Restart=no", "MainPID=0", "ActiveState=inactive",
        "ControlGroup=/fixture/collector.service", "CollectMode=inactive",
    ])
    monkeypatch.setattr(
        systemd_quiescence.subprocess,
        "run",
        lambda *_args, **_kwargs: type("Result", (), {"stdout": output})(),
    )
    monkeypatch.setattr(systemd_quiescence, "_proc_matches", lambda _source: [])
    running = _running_unit(tmp_path)

    receipt = REAL_VERIFY_QUIESCENCE(UNIT, tmp_path, running)

    assert receipt["kill_mode"] == "control-group"
    assert receipt["matching_process_count"] == 0
    monkeypatch.setattr(
        systemd_quiescence, "_proc_matches", lambda _source: [123]
    )
    with pytest.raises(RuntimeError, match="still running"):
        REAL_VERIFY_QUIESCENCE(UNIT, tmp_path, running)


def test_systemd_quiescence_accepts_gc_only_with_running_unit_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = "\n".join([
        "LoadState=not-found", "Transient=no", "KillMode=control-group",
        "Restart=no", "MainPID=0", "ActiveState=inactive", "ControlGroup=",
        "CollectMode=inactive",
    ])
    monkeypatch.setattr(
        systemd_quiescence.subprocess,
        "run",
        lambda *_args, **_kwargs: type("Result", (), {"stdout": output})(),
    )
    monkeypatch.setattr(systemd_quiescence, "_proc_matches", lambda _source: [])
    running = _running_unit(tmp_path)

    receipt = REAL_VERIFY_QUIESCENCE(UNIT, tmp_path, running)

    assert receipt["unit_disposition"] == "garbage_collected"
    assert receipt["running_unit"] == running
    with pytest.raises((TypeError, RuntimeError)):
        REAL_VERIFY_QUIESCENCE(UNIT, tmp_path)


def test_running_unit_receipt_tamper_fails_closed(tmp_path: Path) -> None:
    receipt = _running_unit(tmp_path)
    receipt["main_pid"] += 1

    with pytest.raises(RuntimeError, match="receipt"):
        migration._validate_running_unit_receipt(receipt)


@pytest.mark.parametrize("field", ["argv", "cgroup", "stop"])
def test_resigned_running_unit_semantic_tamper_fails_closed(
    tmp_path: Path, field: str
) -> None:
    receipt = _running_unit(tmp_path)
    if field == "argv":
        receipt["process_argv"][-1] = str((tmp_path / "other").resolve())
        receipt["exec_start"] = (
            f"{{ path=/python ; argv[]={' '.join(receipt['process_argv'])} ; }}"
        )
    elif field == "cgroup":
        receipt["process_cgroups"] = ["/fixture/other.service"]
    else:
        receipt["stop_requested_by_tool"] = False
    unsigned = dict(receipt)
    unsigned.pop("receipt_sha256")
    receipt["receipt_sha256"] = systemd_quiescence._canonical_sha256(unsigned)

    with pytest.raises(RuntimeError):
        migration._validate_running_unit_receipt(receipt)


@pytest.mark.parametrize("field", ["boot", "age", "uid"])
def test_current_machine_running_receipt_rejects_stale_identity(
    tmp_path: Path, field: str
) -> None:
    receipt = _running_unit(tmp_path)
    if field == "boot":
        receipt["boot_id"] = "00000000-0000-0000-0000-000000000001"
        if receipt["boot_id"] == systemd_quiescence._boot_id():
            receipt["boot_id"] = "00000000-0000-0000-0000-000000000002"
    elif field == "age":
        receipt["captured_monotonic_ns"] = (
            time.monotonic_ns() - systemd_quiescence.MAX_CAPTURE_AGE_NS - 1
        )
    else:
        receipt["process_scan_uid"] = os.getuid() + 1
    unsigned = dict(receipt)
    unsigned.pop("receipt_sha256")
    receipt["receipt_sha256"] = systemd_quiescence._canonical_sha256(unsigned)

    with pytest.raises(RuntimeError, match="stale"):
        migration._validate_running_unit_receipt(
            receipt, require_current_machine=True
        )


def test_running_unit_receipt_write_is_no_clobber(tmp_path: Path) -> None:
    receipt = _running_unit(tmp_path)
    path = tmp_path / "receipt.json"
    path.write_text("operator-owned", encoding="utf-8")

    with pytest.raises(RuntimeError, match="already exists"):
        systemd_quiescence.write_running_unit_receipt(path, receipt)

    assert path.read_text(encoding="utf-8") == "operator-owned"


@pytest.mark.parametrize("change", ["systemd", "process"])
def test_capture_rejects_invocation_or_pid_identity_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    argv = [
        "python", "longrun_collect_oppmodel.py", "--out-dir",
        str(tmp_path.resolve()),
    ]
    systemd = {
        "LoadState": "loaded",
        "Transient": "yes",
        "KillMode": "control-group",
        "Restart": "no",
        "MainPID": "123",
        "ActiveState": "active",
        "ControlGroup": "/fixture/collector.service",
        "InvocationID": "a" * 32,
        "ExecStart": f"{{ path=/python ; argv[]={' '.join(argv)} ; }}",
        "CollectMode": "inactive",
        "WorkingDirectory": str(TOOLS.resolve()),
        "Id": UNIT,
    }
    process = {
        "process_argv": argv,
        "process_scan_uid": os.getuid(),
        "process_start_ticks": 10,
        "process_state": "T",
        "process_cgroups": ["/fixture/collector.service"],
        "process_cwd": str(TOOLS.resolve()),
        "collector_script_path": str(
            (TOOLS / "longrun_collect_oppmodel.py").resolve()
        ),
        "collector_script_sha256": hashlib.sha256(
            (TOOLS / "longrun_collect_oppmodel.py").read_bytes()
        ).hexdigest(),
    }
    systemd_reads = [dict(systemd), dict(systemd)]
    process_reads = [dict(process), dict(process)]
    if change == "systemd":
        systemd_reads[1]["InvocationID"] = "b" * 32
    else:
        process_reads[1]["process_start_ticks"] = 11
    monkeypatch.setattr(
        systemd_quiescence, "_systemd_show",
        lambda _unit, _properties: systemd_reads.pop(0),
    )
    monkeypatch.setattr(
        systemd_quiescence, "_proc_identity",
        lambda _pid: process_reads.pop(0),
    )

    with pytest.raises(RuntimeError, match="changed during capture"):
        systemd_quiescence._capture_running_unit_snapshot(
            UNIT, tmp_path,
            {
                "schema": "fixture", "source_dir": str(tmp_path.resolve()),
                "source_collector_sha256": process[
                    "collector_script_sha256"
                ],
            },
        )


def test_freeze_waits_until_every_cgroup_process_is_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    running = _running_unit(tmp_path)
    preflight = {
        "LoadState": "loaded", "Transient": "yes",
        "KillMode": "control-group", "Restart": "no", "MainPID": "123",
        "ActiveState": "active", "ControlGroup": "/fixture/collector.service",
        "InvocationID": "a" * 32, "ExecStart": running["exec_start"],
        "CollectMode": "inactive", "WorkingDirectory": str(TOOLS.resolve()),
        "Id": UNIT,
    }
    calls = []
    monkeypatch.setattr(
        systemd_quiescence, "_systemd_show",
        lambda _unit, _properties: preflight,
    )
    monkeypatch.setattr(
        systemd_quiescence, "_cgroup_processes",
        lambda _groups: (True, {123, 124}),
    )

    def state(_pid):
        calls.append(_pid)
        return "S" if len(calls) <= 2 else "T"

    monkeypatch.setattr(systemd_quiescence, "_proc_state", state)
    monkeypatch.setattr(systemd_quiescence.time, "sleep", lambda _delay: None)

    systemd_quiescence._wait_control_group_stopped(preflight)

    assert len(calls) >= 4


def test_bound_stop_freezes_before_intent_and_kills_before_gc_prone_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    running = _running_unit(tmp_path)
    snapshot = dict(running)
    snapshot.pop("receipt_sha256")
    snapshot.pop("stop_requested_by_tool")
    commands = []
    active = {
        "LoadState": "loaded", "Transient": "yes",
        "KillMode": "control-group", "Restart": "no", "MainPID": "123",
        "ActiveState": "active", "ControlGroup": "/fixture/collector.service",
        "InvocationID": "a" * 32, "ExecStart": running["exec_start"],
        "CollectMode": "inactive", "WorkingDirectory": str(TOOLS.resolve()),
        "Id": UNIT,
    }
    monkeypatch.setattr(
        systemd_quiescence, "_systemd_show",
        lambda _unit, _properties: active,
    )
    monkeypatch.setattr(
        systemd_quiescence, "_capture_running_unit_snapshot",
        lambda _unit, _source, _intent: snapshot,
    )
    monkeypatch.setattr(
        systemd_quiescence, "_wait_control_group_stopped", lambda _preflight: None
    )
    monkeypatch.setattr(
        systemd_quiescence, "write_running_unit_receipt",
        lambda _path, _receipt: None,
    )
    monkeypatch.setattr(
        systemd_quiescence, "verify_collector_quiescence",
        lambda _unit, _source, receipt: _quiescence(
            tmp_path, running_unit=receipt
        ),
    )
    monkeypatch.setattr(
        systemd_quiescence.subprocess, "run",
        lambda command, **_kwargs: commands.append(command)
        or type("Result", (), {"returncode": 0})(),
    )
    intent_seen_after = []

    def intent_factory():
        intent_seen_after.append(commands[-1])
        return running["migration_intent"]

    systemd_quiescence.stop_bound_collector(
        UNIT, tmp_path, intent_factory, tmp_path / "receipt.json"
    )

    assert "--signal=SIGSTOP" in intent_seen_after[0]
    stop_index = next(index for index, cmd in enumerate(commands) if "stop" in cmd)
    kill_index = next(
        index for index, cmd in enumerate(commands) if "--signal=SIGKILL" in cmd
    )
    assert kill_index < stop_index


def test_bound_stop_resumes_group_when_frozen_preflight_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    running = _running_unit(tmp_path)
    commands = []
    monkeypatch.setattr(
        systemd_quiescence, "_systemd_show",
        lambda _unit, _properties: {
            "LoadState": "loaded", "Transient": "yes",
            "KillMode": "control-group", "Restart": "no", "MainPID": "123",
            "ActiveState": "active", "ControlGroup": "/fixture/collector.service",
            "InvocationID": "a" * 32, "ExecStart": running["exec_start"],
            "CollectMode": "inactive", "WorkingDirectory": str(TOOLS.resolve()),
            "Id": UNIT,
        },
    )
    monkeypatch.setattr(
        systemd_quiescence.subprocess, "run",
        lambda command, **_kwargs: commands.append(command)
        or type("Result", (), {"returncode": 0})(),
    )
    monkeypatch.setattr(
        systemd_quiescence, "_wait_control_group_stopped", lambda _preflight: None
    )
    monkeypatch.setattr(
        systemd_quiescence, "_resume_frozen_collector",
        lambda _preflight: commands.append(["--signal=SIGCONT"]),
    )

    with pytest.raises(RuntimeError, match="not atomic"):
        systemd_quiescence.stop_bound_collector(
            UNIT, tmp_path,
            lambda: (_ for _ in ()).throw(RuntimeError("not atomic")),
            tmp_path / "receipt.json",
        )

    assert any("--signal=SIGSTOP" in command for command in commands)
    assert any("--signal=SIGCONT" in command for command in commands)
    assert not any("--signal=SIGKILL" in command for command in commands)


def test_failed_cutover_reports_when_collector_cannot_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    running = _running_unit(tmp_path)
    monkeypatch.setattr(
        systemd_quiescence, "_systemd_show",
        lambda _unit, _properties: {
            "LoadState": "loaded", "Transient": "yes",
            "KillMode": "control-group", "Restart": "no", "MainPID": "123",
            "ActiveState": "active", "ControlGroup": "/fixture/collector.service",
            "InvocationID": "a" * 32, "ExecStart": running["exec_start"],
            "CollectMode": "inactive", "WorkingDirectory": str(TOOLS.resolve()),
            "Id": UNIT,
        },
    )
    monkeypatch.setattr(
        systemd_quiescence.subprocess, "run",
        lambda _command, **_kwargs: type("Result", (), {"returncode": 0})(),
    )
    monkeypatch.setattr(
        systemd_quiescence, "_wait_control_group_stopped", lambda _preflight: None
    )
    monkeypatch.setattr(
        systemd_quiescence, "_resume_frozen_collector",
        lambda _preflight: (_ for _ in ()).throw(
            RuntimeError("collector remains frozen after failed cutover")
        ),
    )

    with pytest.raises(RuntimeError, match="remains frozen"):
        systemd_quiescence.stop_bound_collector(
            UNIT, tmp_path,
            lambda: (_ for _ in ()).throw(RuntimeError("not atomic")),
            tmp_path / "receipt.json",
        )


@pytest.mark.parametrize(
    ("property_name", "unsafe"),
    [
        ("LoadState", "failed"),
        ("Transient", "no"),
        ("KillMode", "process"),
        ("Restart", "on-failure"),
        ("MainPID", "123"),
        ("ActiveState", "active"),
    ],
)
def test_systemd_quiescence_rejects_unsafe_unit_properties(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    property_name: str,
    unsafe: str,
) -> None:
    properties = {
        "LoadState": "loaded",
        "Transient": "yes",
        "KillMode": "control-group",
        "Restart": "no",
        "MainPID": "0",
        "ActiveState": "inactive",
        "ControlGroup": "/fixture/collector.service",
        "CollectMode": "inactive",
    }
    properties[property_name] = unsafe
    output = "\n".join(f"{key}={value}" for key, value in properties.items())
    monkeypatch.setattr(
        systemd_quiescence.subprocess,
        "run",
        lambda *_args, **_kwargs: type("Result", (), {"stdout": output})(),
    )
    monkeypatch.setattr(systemd_quiescence, "_proc_matches", lambda _source: [])
    running = _running_unit(tmp_path)

    with pytest.raises(RuntimeError, match="not quiescent"):
        REAL_VERIFY_QUIESCENCE(UNIT, tmp_path, running)


@pytest.mark.parametrize(
    ("tamper", "message"),
    [
        (lambda receipt: receipt["after"].__setitem__("capacity_first_slot", 3), "digest"),
        (lambda receipt: receipt["code_artifacts"].__setitem__("target_runtime_capacity_sha256", "0" * 64), "digest"),
        (lambda receipt: receipt["code_artifacts"].__setitem__("target_national_native_sha256", "0" * 64), "digest"),
        (lambda receipt: receipt["planned_tail"].__setitem__("sha256", "0" * 64), "digest"),
        (lambda receipt: receipt["collector_quiescence"].__setitem__("main_pid", 1), "digest"),
    ],
)
def test_receipt_tamper_fails_closed(tmp_path: Path, tamper, message: str) -> None:
    source, _candidate, _ratings = _schema6_source(tmp_path, planned_tail=True)
    assert migration.main(_args(source, apply=True)) == 0
    manifest = json.loads(
        (source / "collection_manifest.json").read_text(encoding="utf-8")
    )
    tamper(manifest[migration.MIGRATION_KEY])

    with pytest.raises(RuntimeError, match=message):
        migration.replay_migration(
            manifest,
            completed_passes=2,
            source_dir=source,
            validate_data_prefix=True,
        )


def test_resigned_quiescence_from_another_source_fails_replay(
    tmp_path: Path,
) -> None:
    source, _candidate, _ratings = _schema6_source(tmp_path)
    assert migration.main(_args(source, apply=True)) == 0
    manifest = json.loads(
        (source / "collection_manifest.json").read_text(encoding="utf-8")
    )
    capacity = manifest[migration.MIGRATION_KEY]
    quiescence = capacity["collector_quiescence"]
    running = quiescence["running_unit"]
    other = str((tmp_path / "other-source").resolve())
    running["source_dir"] = other
    running["migration_intent"]["source_dir"] = other
    running["process_argv"][-1] = other
    running["exec_start"] = (
        f"{{ path=/python ; argv[]={' '.join(running['process_argv'])} ; }}"
    )
    running_unsigned = dict(running)
    running_unsigned.pop("receipt_sha256")
    running["receipt_sha256"] = systemd_quiescence._canonical_sha256(
        running_unsigned
    )
    quiescence["source_dir"] = other
    _resign(capacity)

    with pytest.raises(RuntimeError, match="migration intent changed"):
        migration.replay_migration(
            manifest,
            completed_passes=2,
            source_dir=source,
            validate_data_prefix=True,
        )


def test_replay_allows_registry_growth_but_rejects_history_rewrite(
    tmp_path: Path,
) -> None:
    source, _candidate, _ratings = _schema6_source(tmp_path)
    assert migration.main(_args(source, apply=True)) == 0
    manifest = json.loads(
        (source / "collection_manifest.json").read_text(encoding="utf-8")
    )
    registry_path = source / "opponent_snapshots" / "registry.json"
    if not registry_path.exists():
        registry_path = source / "opponent_snapshots.completed.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    original_name, original = next(iter(registry["opponents"].items()))
    registry["opponents"]["national_v999"] = dict(original)
    _write_json(registry_path, registry)

    assert migration.replay_migration(
        manifest,
        completed_passes=2,
        source_dir=source,
        validate_data_prefix=True,
    ) is not None

    registry["opponents"][original_name]["tag_commit"] = "0" * 40
    _write_json(registry_path, registry)
    with pytest.raises(RuntimeError, match="registry history changed"):
        migration.replay_migration(
            manifest,
            completed_passes=2,
            source_dir=source,
            validate_data_prefix=True,
        )
