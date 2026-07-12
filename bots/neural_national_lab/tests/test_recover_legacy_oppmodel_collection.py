from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import shutil
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
TOOL_PATH = (
    ROOT
    / "bots"
    / "neural_national_lab"
    / "tools"
    / "recover_legacy_oppmodel_collection.py"
)


def _load_tool():
    spec = importlib.util.spec_from_file_location(
        "recover_legacy_oppmodel_collection", TOOL_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys_modules = __import__("sys").modules
    sys_modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


def _bot(path: Path, body: str) -> Path:
    path.mkdir(parents=True)
    (path / "national_bot.py").write_text(body, encoding="utf-8")
    return path


def _task(tool, *, source: Path, name: str, split: str, pass_index: int, index: int):
    opponent_source = _bot(source.parent / "sources" / name, f"# {name}\n")
    digest = tool.collector._directory_digest(opponent_source)
    opponent = source / "opponent_snapshots" / digest / name
    opponent.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(opponent_source, opponent)
    hands = 3
    deck = tool.collector._deck_seed_for_task(
        root=tool.collector.DEFAULT_DECK_SEED_BASE,
        pass_index=pass_index,
        task_index=index,
        hands=hands,
        guard=tool.collector.DEFAULT_DECK_SEED_GUARD,
    )
    bot_seed = tool.collector._bot_seed_for_task(
        root=tool.collector.DEFAULT_BOT_SEED_BASE,
        pass_index=pass_index,
        task_index=index,
    )
    return {
        "name": name,
        "opponent_path": str(opponent.resolve()),
        "split": split,
        "hands": hands,
        "deck_seed_base": deck,
        "deck_seed_last": deck + hands - 1,
        "bot_seed_base": bot_seed,
        "tag_commit": hashlib.sha1((name + "-tag").encode()).hexdigest(),
        "tag_directory_sha256": digest,
        "execution_matches_generation_tag": True,
        "source_path": str(opponent_source.resolve()),
        "source_checkout_commit": hashlib.sha1((name + "-src").encode()).hexdigest(),
        "execution_directory_sha256": digest,
    }


def _row(
    task: dict,
    *,
    ratings_path: Path,
    split: str,
    hand: int,
    decision: int,
) -> dict:
    return {
        "_split": split,
        "_opponent_label": task["name"],
        "_seed_base": task["deck_seed_base"],
        "_bot_seed_base": task["bot_seed_base"],
        "_collection_hands": task["hands"],
        "_min_hand": 1,
        "_ratings_path": str(ratings_path.resolve()),
        "deck_seed_base": task["deck_seed_base"],
        "bot_seed_base": task["bot_seed_base"],
        "hand": hand,
        "hand_decision_index": decision,
        "opponent": task["name"],
        "status": "ok",
    }


def _fixture(tmp_path: Path):
    tool = _load_tool()
    source = tmp_path / "collection"
    source.mkdir()
    (source / ".collector.lock").write_text("stale", encoding="utf-8")
    active_ratings = tmp_path / "live" / "glicko_ratings.json"
    archived_dir = tmp_path / "archive" / "identity-rotation"
    archived_dir.mkdir(parents=True)
    archived_ratings = archived_dir / "glicko_ratings.json"
    archived_payload = {
        "national_v1": {"rating": 1600, "rd": 40},
        "national_v2": {"rating": 1550, "rd": 50},
        "national_v3": {"rating": 1500, "rd": 60},
    }
    archived_ratings.write_text(
        json.dumps(archived_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    identity_migration = archived_dir / "migration.json"
    _write_json(identity_migration, {
        "archived_at": "2026-01-02T03:04:05",
        "reason": "test identity rotation",
        "moved": ["glicko_ratings.json", "head_to_head.json"],
    })

    legacy_tools = tmp_path / "legacy_tools"
    legacy_tools.mkdir()
    legacy_collector = legacy_tools / "longrun_collect_oppmodel.py"
    legacy_collector.write_text("# schema-4 collector fixture\n", encoding="utf-8")
    current_tools = Path(tool.collector.__file__).resolve().parent
    for filename in ("native_tcp_counterfactual_probe.py", "cross_hand_sequence.py"):
        shutil.copyfile(current_tools / filename, legacy_tools / filename)

    candidate = _bot(tmp_path / "candidate", "# candidate\n")
    candidate_digest = tool.collector._directory_digest(candidate)
    candidate_execution = (
        source / "candidate_snapshot" / candidate_digest / candidate.name
    )
    candidate_execution.parent.mkdir(parents=True)
    shutil.copytree(candidate, candidate_execution)

    names = (
        ("national_v1", "train"),
        ("national_v2", "val"),
        ("national_v3", "held_out"),
    )
    completed_tasks = [
        _task(
            tool,
            source=source,
            name=name,
            split=split,
            pass_index=0,
            index=index,
        )
        for index, (name, split) in enumerate(names)
    ]
    # Reuse immutable snapshots but move seeds to the next disjoint pass block.
    recovery_tasks = []
    for index, completed_task in enumerate(completed_tasks):
        task = dict(completed_task)
        deck = tool.collector._deck_seed_for_task(
            root=tool.collector.DEFAULT_DECK_SEED_BASE,
            pass_index=1,
            task_index=index,
            hands=3,
            guard=tool.collector.DEFAULT_DECK_SEED_GUARD,
        )
        task["deck_seed_base"] = deck
        task["deck_seed_last"] = deck + 2
        task["bot_seed_base"] = tool.collector._bot_seed_for_task(
            root=tool.collector.DEFAULT_BOT_SEED_BASE,
            pass_index=1,
            task_index=index,
        )
        recovery_tasks.append(task)

    registry_path = source / "opponent_snapshots" / "registry.json"
    _write_json(registry_path, {
        "schema": "opponent_execution_snapshot_v1",
        "opponents": {
            task["name"]: {
                "tag_commit": task["tag_commit"],
                "tag_directory_sha256": task["tag_directory_sha256"],
                "execution_matches_generation_tag": task[
                    "execution_matches_generation_tag"
                ],
                "source_path": task["source_path"],
                "source_checkout_commit": task["source_checkout_commit"],
                "snapshot_path": task["opponent_path"],
                "execution_directory_sha256": task[
                    "execution_directory_sha256"
                ],
            }
            for task in recovery_tasks
        },
    })

    plans = source / "pass_plans"
    plans.mkdir()
    _write_json(plans / "pass_0001.json", {
        "pass": 1,
        "seed_scheme": "disjoint_match_blocks_v1",
        "tasks": completed_tasks,
    })
    recovery_plan = plans / "pass_0002.json"
    _write_json(recovery_plan, {
        "pass": 2,
        "seed_scheme": "disjoint_match_blocks_v1",
        "tasks": recovery_tasks,
    })

    ratings_sha = _sha(archived_ratings)
    pool_snapshots = source / "pool_snapshots.jsonl"
    _write_jsonl(pool_snapshots, [{
        "pass": 1,
        "ratings_path": str(active_ratings.resolve()),
        "ratings_sha256": ratings_sha,
        "min_hand": 1,
        "hands": 3,
        "workers": 1,
        "probe_workers": 3,
        "decision_sampling": "uniform",
        "pool": [],
    }])
    state_path = source / "collector_state.json"
    _write_json(state_path, {
        "completed_passes": 1,
        "total_rows": {split: 1 for split in tool.SOURCE_SPLITS},
        "total_behavior_rows": {split: 1 for split in tool.SOURCE_SPLITS},
        "updated_at": "2026-01-02T03:04:04+0000",
    })

    contract = {
        "schema_version": tool.LEGACY_COLLECTION_SCHEMA_VERSION,
        "candidate": str(candidate.resolve()),
        "candidate_sha256": candidate_digest,
        "candidate_execution_path": str(candidate_execution.resolve()),
        "candidate_snapshot_sha256": candidate_digest,
        "ratings_path": str(active_ratings.resolve()),
        "workers": 1,
        "probe_workers": 3,
        "hands": 3,
        "timeout_sec": 5,
        "strongest": 3,
        "val_opponents": ["national_v2"],
        "held_out_opponents": ["national_v3"],
        "opponents_per_pass": 3,
        "max_decisions": 2,
        "max_alternatives": 1,
        "decision_sampling": "uniform",
        "hand_windows": [0.0],
        "deck_seed_scheme": "disjoint_match_blocks_v1",
        "deck_seed_base": tool.collector.DEFAULT_DECK_SEED_BASE,
        "deck_seed_guard": tool.collector.DEFAULT_DECK_SEED_GUARD,
        "deck_seed_slots_per_pass": tool.collector.DECK_SEED_SLOTS_PER_PASS,
        "bot_seed_base": tool.collector.DEFAULT_BOT_SEED_BASE,
        "collector_sha256": _sha(legacy_collector),
        "probe_sha256": _sha(legacy_tools / "native_tcp_counterfactual_probe.py"),
        "cross_hand_sequence_sha256": _sha(legacy_tools / "cross_hand_sequence.py"),
    }
    manifest_path = source / "collection_manifest.json"
    _write_json(manifest_path, {
        "passes_requested": 2,
        "ratings_sha256_at_start": ratings_sha,
        "resume_contract": contract,
        "start_pass": 0,
    })

    for kind, files in tool.DATA_FILES.items():
        for split, filename in files.items():
            completed_task = next(row for row in completed_tasks if row["split"] == split)
            recovery_task = next(row for row in recovery_tasks if row["split"] == split)
            prefix = _row(
                completed_task,
                ratings_path=active_ratings,
                split=split,
                hand=0,
                decision=0,
            )
            if kind == "value":
                tail = [
                    _row(
                        recovery_task,
                        ratings_path=active_ratings,
                        split=split,
                        hand=index,
                        decision=0,
                    )
                    for index in (1, 2)
                ]
            else:
                tail = [
                    _row(
                        recovery_task,
                        ratings_path=active_ratings,
                        split=split,
                        hand=index,
                        decision=0,
                    )
                    for index in (1, 2)
                ]
            _write_jsonl(source / filename, [prefix, *tail])

    expectations_path = tmp_path / "expectations.json"
    expectations = {
        "schema_version": tool.EXPECTATIONS_SCHEMA_VERSION,
        "completed_pass": 1,
        "recovery_pass": 2,
        "hashes": {
            "collection_manifest": _sha(manifest_path),
            "collector_state": _sha(state_path),
            "pool_snapshots": _sha(pool_snapshots),
            "recovery_plan": _sha(recovery_plan),
            "legacy_collector": _sha(legacy_collector),
            "current_collector": _sha(Path(tool.collector.__file__).resolve()),
            "identity_migration": _sha(identity_migration),
            "archived_ratings": _sha(archived_ratings),
            "opponent_registry": _sha(registry_path),
            **{
                filename: _sha(source / filename)
                for files in tool.DATA_FILES.values()
                for filename in files.values()
            },
        },
        "completed_plan_sha256": {
            "pass_0001.json": _sha(plans / "pass_0001.json")
        },
        "tasks": {
            name: {"split": split, "value_rows": 2, "behavior_rows": 2}
            for name, split in names
        },
    }
    _write_json(expectations_path, expectations)
    return tool, {
        "source_dir": source,
        "legacy_collector": legacy_collector,
        "archived_ratings": archived_ratings,
        "identity_migration": identity_migration,
        "expectations_path": expectations_path,
        "active_ratings": active_ratings,
        "mutable": {
            "manifest": manifest_path,
            "state": state_path,
            "snapshots": pool_snapshots,
            "plan": recovery_plan,
        },
    }


def _run(tool, fixture: dict, *, apply: bool = False):
    return tool.run(
        source_dir=fixture["source_dir"],
        legacy_collector=fixture["legacy_collector"],
        archived_ratings=fixture["archived_ratings"],
        identity_migration=fixture["identity_migration"],
        expectations_path=fixture["expectations_path"],
        apply=apply,
    )


def _mutable_bytes(fixture: dict) -> dict[str, bytes]:
    return {name: path.read_bytes() for name, path in fixture["mutable"].items()}


def _refresh_data_hash(tool, fixture: dict, filename: str) -> None:
    payload = json.loads(fixture["expectations_path"].read_text(encoding="utf-8"))
    payload["hashes"][filename] = _sha(fixture["source_dir"] / filename)
    _write_json(fixture["expectations_path"], payload)


def test_dry_run_is_read_only_and_never_reads_current_ratings(tmp_path: Path) -> None:
    tool, fixture = _fixture(tmp_path)
    before = _mutable_bytes(fixture)
    fixture["active_ratings"].parent.mkdir()
    fixture["active_ratings"].write_text("not-json-current-ratings", encoding="utf-8")

    result = _run(tool, fixture)

    assert result["status"] == "ready_for_explicit_apply"
    assert result["probe_execution_count"] == 0
    assert result["read_current_ratings"] is False
    assert _mutable_bytes(fixture) == before
    assert not (fixture["source_dir"] / tool.TRANSACTION_DIRNAME).exists()


@pytest.mark.parametrize(
    "constant",
    ("COLLECTION_CONTRACT_SCHEMA_VERSION", "PASS_PLAN_SCHEMA_VERSION"),
)
def test_future_target_schema_requires_a_new_migration_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, constant: str
) -> None:
    tool, fixture = _fixture(tmp_path)
    before = _mutable_bytes(fixture)
    monkeypatch.setattr(
        tool.collector,
        constant,
        getattr(tool.collector, constant) + 1,
    )

    with pytest.raises(tool.RecoveryError, match="target schemas changed"):
        _run(tool, fixture)

    assert _mutable_bytes(fixture) == before


def test_apply_upgrades_exact_tail_without_running_probe(
    tmp_path: Path, monkeypatch
) -> None:
    tool, fixture = _fixture(tmp_path)
    archived_raw = fixture["archived_ratings"].read_bytes()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("recovery must not probe or capture live ratings")

    monkeypatch.setattr(tool.collector, "probe_one", forbidden)
    monkeypatch.setattr(tool.collector, "_capture_ratings_snapshot", forbidden)

    result = _run(tool, fixture, apply=True)

    assert result["status"] == "recovered"
    assert result["completed_passes"] == 2
    assert result["probe_execution_count"] == 0
    manifest = json.loads(fixture["mutable"]["manifest"].read_text(encoding="utf-8"))
    state = json.loads(fixture["mutable"]["state"].read_text(encoding="utf-8"))
    plan = json.loads(fixture["mutable"]["plan"].read_text(encoding="utf-8"))
    snapshots = [
        json.loads(line)
        for line in fixture["mutable"]["snapshots"].read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert manifest["resume_contract"]["schema_version"] == (
        tool.collector.COLLECTION_CONTRACT_SCHEMA_VERSION
    )
    assert manifest["legacy_recovery"]["probe_execution_count"] == 0
    assert manifest["legacy_recovery"]["read_current_ratings"] is False
    assert manifest["legacy_recovery"]["recovery_tool_sha256"] == _sha(
        TOOL_PATH
    )
    assert state["completed_passes"] == 2
    assert state["total_rows"] == {split: 3 for split in tool.SOURCE_SPLITS}
    assert state["total_behavior_rows"] == {
        split: 3 for split in tool.SOURCE_SPLITS
    }
    assert plan["schema_version"] == tool.collector.PASS_PLAN_SCHEMA_VERSION
    assert __import__("base64").b64decode(
        plan["ratings_snapshot"]["ratings_bytes_base64"], validate=True
    ) == archived_raw
    assert [row["pass"] for row in snapshots] == [1, 2]
    assert snapshots[-1]["ratings_sha256"] == _sha(fixture["archived_ratings"])
    assert not (fixture["source_dir"] / tool.TRANSACTION_DIRNAME).exists()


def test_recovery_receipt_replays_exact_published_data_prefix(
    tmp_path: Path,
) -> None:
    tool, fixture = _fixture(tmp_path)
    _run(tool, fixture, apply=True)
    import freeze_opponent_role_dataset as freeze

    manifest = json.loads(
        fixture["mutable"]["manifest"].read_text(encoding="utf-8")
    )
    boundary = freeze._legacy_recovery_contract(
        manifest,
        completed_passes=2,
        source_dir=fixture["source_dir"],
        validate_data_prefix=True,
    )

    assert boundary is not None
    assert boundary[:2] == (1, 2)


def test_reviewed_hash_mismatch_fails_without_mutation(tmp_path: Path) -> None:
    tool, fixture = _fixture(tmp_path)
    before = _mutable_bytes(fixture)
    expectations = json.loads(
        fixture["expectations_path"].read_text(encoding="utf-8")
    )
    expectations["hashes"]["collector_state"] = "0" * 64
    _write_json(fixture["expectations_path"], expectations)

    with pytest.raises(tool.RecoveryError, match="reviewed hash changed"):
        _run(tool, fixture)

    assert _mutable_bytes(fixture) == before


def test_schema4_manifest_binding_fails_closed(tmp_path: Path) -> None:
    tool, fixture = _fixture(tmp_path)
    manifest_path = fixture["mutable"]["manifest"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["resume_contract"]["schema_version"] = 5
    _write_json(manifest_path, manifest)
    expectations = json.loads(
        fixture["expectations_path"].read_text(encoding="utf-8")
    )
    expectations["hashes"]["collection_manifest"] = _sha(manifest_path)
    _write_json(fixture["expectations_path"], expectations)

    with pytest.raises(tool.RecoveryError, match="not the bound schema-4"):
        _run(tool, fixture)


def test_legacy_collector_bytes_must_match_manifest_and_review(tmp_path: Path) -> None:
    tool, fixture = _fixture(tmp_path)
    fixture["legacy_collector"].write_text("# different legacy code\n", encoding="utf-8")
    expectations = json.loads(
        fixture["expectations_path"].read_text(encoding="utf-8")
    )
    expectations["hashes"]["legacy_collector"] = _sha(
        fixture["legacy_collector"]
    )
    _write_json(fixture["expectations_path"], expectations)

    with pytest.raises(tool.RecoveryError, match="do not match the resume contract"):
        _run(tool, fixture)


def test_completed_plan_prefix_hash_is_reviewed(tmp_path: Path) -> None:
    tool, fixture = _fixture(tmp_path)
    plan = fixture["source_dir"] / "pass_plans" / "pass_0001.json"
    payload = json.loads(plan.read_text(encoding="utf-8"))
    payload["tasks"][0]["source_checkout_commit"] = "f" * 40
    _write_json(plan, payload)

    with pytest.raises(tool.RecoveryError, match="completed pass plan changed"):
        _run(tool, fixture)


@pytest.mark.parametrize(
    "corruption,match",
    (
        ("duplicate", "duplicate collector row key"),
        ("unplanned_seed", "unplanned row"),
        ("bad_status", "tail row contract mismatch"),
        ("before_min_hand", "outside the planned window"),
        ("missing_final_hand", "does not reach hand"),
        ("short_value", "value tail count changed"),
    ),
)
def test_tail_corruption_fails_closed(
    tmp_path: Path, corruption: str, match: str
) -> None:
    tool, fixture = _fixture(tmp_path)
    filename = (
        "opponent_actions_train.jsonl"
        if corruption == "missing_final_hand"
        else "cf_train.jsonl"
    )
    path = fixture["source_dir"] / filename
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    if corruption == "duplicate":
        rows.append(dict(rows[-1]))
    elif corruption == "unplanned_seed":
        rows[-1]["_bot_seed_base"] += 99
        rows[-1]["bot_seed_base"] += 99
    elif corruption == "bad_status":
        rows[-1]["status"] = "partial"
    elif corruption == "before_min_hand":
        rows[-1]["hand"] = 0
    elif corruption == "missing_final_hand":
        rows[-1]["hand"] = 1
        rows[-1]["hand_decision_index"] = 1
    elif corruption == "short_value":
        rows.pop()
    _write_jsonl(path, rows)
    _refresh_data_hash(tool, fixture, filename)

    with pytest.raises(tool.RecoveryError, match=match):
        _run(tool, fixture)


def test_archived_ratings_must_match_completed_prefix(tmp_path: Path) -> None:
    tool, fixture = _fixture(tmp_path)
    fixture["archived_ratings"].write_text(
        '{"national_v1":{"rating":9999,"rd":1}}\n', encoding="utf-8"
    )
    expectations = json.loads(
        fixture["expectations_path"].read_text(encoding="utf-8")
    )
    expectations["hashes"]["archived_ratings"] = _sha(
        fixture["archived_ratings"]
    )
    _write_json(fixture["expectations_path"], expectations)

    with pytest.raises(tool.RecoveryError, match="completed-prefix view"):
        _run(tool, fixture)


def test_held_collector_lock_rejects_recovery(tmp_path: Path) -> None:
    tool, fixture = _fixture(tmp_path)
    fd = os.open(fixture["source_dir"] / ".collector.lock", os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(tool.RecoveryError, match="already running"):
            _run(tool, fixture)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_publication_failure_rolls_back_all_targets(
    tmp_path: Path, monkeypatch
) -> None:
    tool, fixture = _fixture(tmp_path)
    before = _mutable_bytes(fixture)
    real_replace = tool._atomic_replace_bytes
    calls = 0

    def fail_once(path, raw):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("injected publish failure")
        return real_replace(path, raw)

    monkeypatch.setattr(tool, "_atomic_replace_bytes", fail_once)
    with pytest.raises(tool.RecoveryError, match="rollback=rolled_back"):
        _run(tool, fixture, apply=True)

    assert _mutable_bytes(fixture) == before
    assert not (fixture["source_dir"] / tool.TRANSACTION_DIRNAME).exists()


def test_rollback_refuses_unknown_concurrent_target_bytes(tmp_path: Path) -> None:
    tool, fixture = _fixture(tmp_path)
    audit = tool.audit_recovery(
        source_dir=fixture["source_dir"],
        legacy_collector=fixture["legacy_collector"],
        archived_ratings=fixture["archived_ratings"],
        identity_migration=fixture["identity_migration"],
        expectations_path=fixture["expectations_path"],
    )
    after, receipt = tool._build_recovered_payloads(audit)
    poison = tool._poison_manifest(audit, receipt["receipt_sha256"])
    tool._prepare_transaction(audit, after=after, poison_manifest=poison)
    unknown = b'{"concurrent":"collector metadata"}'
    fixture["mutable"]["state"].write_bytes(unknown)

    with pytest.raises(tool.RecoveryError, match="refusing destructive rollback"):
        tool._rollback_transaction(fixture["source_dir"])

    assert fixture["mutable"]["state"].read_bytes() == unknown
    assert (fixture["source_dir"] / tool.TRANSACTION_DIRNAME).is_dir()


def test_prepare_rechecks_targets_after_final_audit(tmp_path: Path) -> None:
    tool, fixture = _fixture(tmp_path)
    audit = tool.audit_recovery(
        source_dir=fixture["source_dir"],
        legacy_collector=fixture["legacy_collector"],
        archived_ratings=fixture["archived_ratings"],
        identity_migration=fixture["identity_migration"],
        expectations_path=fixture["expectations_path"],
    )
    after, receipt = tool._build_recovered_payloads(audit)
    poison = tool._poison_manifest(audit, receipt["receipt_sha256"])
    fixture["mutable"]["state"].write_bytes(b'{"changed":"after audit"}')

    with pytest.raises(tool.RecoveryError, match="changed after final audit"):
        tool._prepare_transaction(audit, after=after, poison_manifest=poison)

    assert not (fixture["source_dir"] / tool.TRANSACTION_DIRNAME).exists()


def test_interrupted_poisoned_transaction_is_rolled_back_then_reapplied(
    tmp_path: Path, monkeypatch
) -> None:
    tool, fixture = _fixture(tmp_path)
    real_replace = tool._atomic_replace_bytes
    calls = 0

    def fail_publish_and_first_rollback(path, raw):
        nonlocal calls
        calls += 1
        if calls in {3, 4}:
            raise OSError("simulated process-loss boundary")
        return real_replace(path, raw)

    monkeypatch.setattr(
        tool, "_atomic_replace_bytes", fail_publish_and_first_rollback
    )
    with pytest.raises(tool.RecoveryError, match="both failed"):
        _run(tool, fixture, apply=True)
    assert (fixture["source_dir"] / tool.TRANSACTION_DIRNAME).is_dir()
    poisoned = json.loads(
        fixture["mutable"]["manifest"].read_text(encoding="utf-8")
    )
    assert poisoned["resume_contract"]["schema_version"] == (
        "legacy_recovery_in_progress"
    )

    monkeypatch.setattr(tool, "_atomic_replace_bytes", real_replace)
    result = _run(tool, fixture, apply=True)

    assert result["status"] == "recovered"
    assert not (fixture["source_dir"] / tool.TRANSACTION_DIRNAME).exists()
