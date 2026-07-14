from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]
TOOLS = ROOT / "bots" / "neural_national_lab" / "tools"
sys.path.insert(0, str(TOOLS))

import freeze_opponent_role_dataset as freeze  # noqa: E402
import longrun_collect_oppmodel as collector  # noqa: E402


OPPONENTS = {
    "train": "national_v1",
    "early_stop": "national_v2",
    "model_calibration": "national_v3",
    "policy_selection": "national_v4",
    "policy_gate": "national_v5",
}
SOURCE_SPLIT = {
    "national_v1": "train",
    "national_v2": "train",
    "national_v3": "val",
    "national_v4": "val",
    "national_v5": "held_out",
}


def _value_row(name: str, deck: int, bot: int) -> dict:
    mask = [0, 0, 1, 1, 0, 0]
    return {
        "status": "ok",
        "opponent": name,
        "_opponent_label": name,
        "_split": SOURCE_SPLIT[name],
        "deck_seed_base": deck,
        "bot_seed_base": bot,
        "_collection_hands": 70,
        "hand": 1,
        "hand_decision_index": 0,
        "invalid_probe_count": 0,
        "decision_sampling": "uniform",
        "eligible_decisions": 24,
        "selected_decisions": 12,
        "decision_inclusion_probability": 0.5,
        "decision_inverse_probability_weight": 2.0,
        "legal_mask": mask,
        "rule_label_id": 2,
        "baseline_match_net_chips": 100,
        "match_delta_vs_rule": [None, None, 0.0, -200.0, None, None],
        "match_action_values": [None, None, 100.0, -100.0, None, None],
        "target_masks": {"match_delta_vs_rule": mask},
        "probes": [],
    }


def _behavior_row(name: str, deck: int, bot: int) -> dict:
    return {
        "status": "ok",
        "opponent": name,
        "_opponent_label": name,
        "_split": SOURCE_SPLIT[name],
        "deck_seed_base": deck,
        "bot_seed_base": bot,
        "_collection_hands": 70,
        "hand": 1,
        "hand_decision_index": 0,
        "decision_serial": 0,
        "stage": "preflop",
        "hero_action": 200,
        "hero_action_label_id": 2,
        "opponent_action": "call",
        "opponent_action_label_id": 2,
        "opponent_action_amount": 100,
        "opponent_action_amount_norm": 100 / 20_000,
        "opponent_action_pot_ratio": 100 / 150,
        "request": {
            "my_id": 0,
            "dealer_id": 0,
            "my_chips": 19_950,
            "opponent_chips": 19_900,
            "my_stage_bet": 50,
            "opponent_stage_bet": 100,
            "pot": 150,
            "to_call": 50,
            "history": [],
            "my_cards": [0, 4],
            "public_cards": [],
        },
        "state": {"round": 0, "pot": 150, "to_call": 50},
    }


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )


def _roles() -> dict[str, set[str]]:
    return {
        role: {name}
        for role, name in OPPONENTS.items()
        if role != "train"
    }


def _collection(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "pass_plans").mkdir()
    candidate = source / "candidate_snapshot" / "candidate"
    candidate.mkdir(parents=True)
    (candidate / "national_bot.py").write_text("# candidate\n", encoding="utf-8")
    candidate_digest = collector._directory_digest(candidate)
    ratings_path = source / "ratings.json"
    ratings_path.write_text(json.dumps({
        name: {"r": 1500.0, "rd": 50.0, "sigma": 0.06, "last_period": "x"}
        for name in SOURCE_SPLIT
    }), encoding="utf-8")
    ratings_snapshot = collector._capture_ratings_snapshot(ratings_path)

    registry = {"schema": "opponent_execution_snapshot_v1", "opponents": {}}
    tasks = []
    rows = {split: [] for split in freeze.SOURCE_SPLITS}
    behaviors = {split: [] for split in freeze.SOURCE_SPLITS}
    for index, name in enumerate(SOURCE_SPLIT):
        deck = 5_000_000 + index * 80
        bot = 1_000_000 + index
        split = SOURCE_SPLIT[name]
        snapshot = source / "opponent_snapshots" / name
        snapshot.mkdir(parents=True)
        (snapshot / "national_bot.py").write_text(f"# {name}\n", encoding="utf-8")
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
        tasks.append({
            "name": name,
            "split": split,
            "opponent_path": str(snapshot),
            "source_path": str(snapshot),
            "tag_commit": "1" * 40,
            "tag_directory_sha256": digest,
            "execution_matches_generation_tag": True,
            "source_checkout_commit": "2" * 40,
            "deck_seed_base": deck,
            "deck_seed_last": deck + 69,
            "bot_seed_base": bot,
            "hands": 70,
            "execution_directory_sha256": digest,
        })
        rows[split].append(_value_row(name, deck, bot))
        behaviors[split].append(_behavior_row(name, deck, bot))

    for split in freeze.SOURCE_SPLITS:
        _write_jsonl(source / f"cf_{split}.jsonl", rows[split])
        _write_jsonl(
            source / f"opponent_actions_{split}.jsonl", behaviors[split]
        )
    (source / "opponent_snapshots" / "registry.json").write_text(
        json.dumps(registry), encoding="utf-8"
    )
    (source / "collection_manifest.json").write_text(json.dumps({
        "passes_requested": 2,
        "resume_contract": {
            "schema_version": collector.ACTIVE_COLLECTION_CONTRACT_SCHEMA_VERSION,
            "candidate": str(candidate),
            "candidate_sha256": candidate_digest,
            "ratings_path": str(ratings_path.resolve()),
            "workers": collector.MAX_OUTER_WORKERS,
            "probe_workers": collector.MAX_PROBE_WORKERS,
            "max_active_native_matches": collector.MAX_CONCURRENT_NATIVE_MATCHES,
            "capacity_total_slots": collector.CAPACITY_TOTAL_SLOTS,
            "capacity_first_slot": collector.CAPACITY_FIRST_SLOT,
            "hands": 70,
            "timeout_sec": 55,
            "strongest": 5,
            "val_opponents": ["national_v3", "national_v4"],
            "held_out_opponents": ["national_v5"],
            "opponents_per_pass": 5,
            "max_decisions": 12,
            "max_alternatives": 5,
            "decision_sampling": "uniform",
            "hand_windows": [0.0],
            "deck_seed_scheme": "disjoint_match_blocks_v1",
            "deck_seed_base": 5_000_000,
            "deck_seed_guard": 10,
            "deck_seed_slots_per_pass": collector.DECK_SEED_SLOTS_PER_PASS,
            "bot_seed_base": 1_000_000,
            "collector_sha256": hashlib.sha256(
                Path(collector.__file__).read_bytes()
            ).hexdigest(),
            "probe_sha256": hashlib.sha256(
                (TOOLS / "native_tcp_counterfactual_probe.py").read_bytes()
            ).hexdigest(),
            "cross_hand_sequence_sha256": hashlib.sha256(
                (TOOLS / "cross_hand_sequence.py").read_bytes()
            ).hexdigest(),
            "runtime_capacity_sha256": hashlib.sha256(
                (ROOT / "web" / "core" / "runtime_capacity.py").read_bytes()
            ).hexdigest(),
            "national_native_sha256": hashlib.sha256(
                (ROOT / "web" / "core" / "national_native.py").read_bytes()
            ).hexdigest(),
            "candidate_execution_path": str(candidate),
            "candidate_snapshot_sha256": candidate_digest,
        },
    }), encoding="utf-8")
    (source / "collector_state.json").write_text(json.dumps({
        "completed_passes": 1,
        "total_rows": {split: len(rows[split]) for split in freeze.SOURCE_SPLITS},
        "total_behavior_rows": {
            split: len(behaviors[split]) for split in freeze.SOURCE_SPLITS
        },
    }), encoding="utf-8")
    _write_jsonl(source / "pool_snapshots.jsonl", [{
        "pass": 1,
        "ratings_path": str(ratings_path.resolve()),
        "ratings_sha256": ratings_snapshot["ratings_sha256"],
        "ratings_snapshot_sha256": ratings_snapshot["snapshot_sha256"],
        "min_hand": 1,
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
            "execution_directory_sha256": task["execution_directory_sha256"],
            "source_checkout_commit": task["source_checkout_commit"],
            "glicko": ratings_snapshot["ratings"].get(task["name"]),
            "deck_seed_base": task["deck_seed_base"],
            "deck_seed_last": task["deck_seed_last"],
            "bot_seed_base": task["bot_seed_base"],
        } for task in tasks],
    }])
    (source / "pass_plans" / "pass_0001.json").write_text(json.dumps({
        "schema_version": collector.PASS_PLAN_SCHEMA_VERSION,
        "pass": 1,
        "seed_scheme": "disjoint_match_blocks_v1",
        "ratings_snapshot": ratings_snapshot,
        "tasks": tasks,
    }), encoding="utf-8")
    return source


def _freeze(source: Path, output: Path) -> dict:
    return freeze.freeze_role_dataset(
        source,
        output,
        role_opponents=_roles(),
        min_value_rows={role: 1 for role in freeze.EVIDENCE_ROLES},
        min_behavior_rows={role: 1 for role in freeze.EVIDENCE_ROLES},
    )


def test_freeze_creates_five_opponent_disjoint_roles(tmp_path: Path) -> None:
    source = _collection(tmp_path)
    output = tmp_path / "frozen"

    manifest = _freeze(source, output)

    freeze.validate_frozen_role_provenance(
        output, manifest, expected_passes=1
    )

    assert manifest["schema"] == "opponent_role_dataset_v4"
    assert manifest["source_completed_passes"] == 1
    assert manifest["source_requested_passes"] == 2
    assert manifest["source_collection_complete"] is False
    assert manifest["candidate_snapshot"]["name"] == "candidate"
    assert manifest["strategy_context_runtime_mode"] == (
        freeze.STRATEGY_CONTEXT_RUNTIME_MODE
    )
    assert manifest["invariants"]["deck_blocks_non_overlapping"] is True
    assert manifest["row_identity"]["schema"] == (
        "collector_row_identity_v1"
    )
    assert manifest["row_identity"]["modalities"]["cf"]["rows"] == 5
    assert manifest["row_identity"]["modalities"]["opponent_actions"][
        "rows"
    ] == 5
    assert manifest["invariants"]["national_response_v2_validated"] is True
    assert manifest["invariants"]["national_70_hand_outcome_validated"] is True
    assert manifest["match_outcome_supervision"]["hands"] == 70
    assert manifest["behavior_supervision"]["schema"] == (
        "national_opponent_response_v2"
    )
    assert manifest["roles"] == {
        role: [name] for role, name in OPPONENTS.items()
    }
    for prefix in freeze.PREFIXES:
        for role, opponent in OPPONENTS.items():
            path = output / f"{prefix}_{role}.jsonl"
            row = json.loads(path.read_text().strip())
            assert row["opponent"] == opponent
            assert row["_source_split"] == SOURCE_SPLIT[opponent]
            assert row["_evidence_role"] == role
            if prefix == "opponent_actions":
                assert row["response_schema"] == "national_opponent_response_v2"
                assert row["response_legal_action_mask"][row["opponent_action_label_id"]]


def test_freeze_rejects_role_overlap(tmp_path: Path) -> None:
    source = _collection(tmp_path)
    roles = _roles()
    roles["early_stop"].add(OPPONENTS["model_calibration"])

    with pytest.raises(ValueError, match="multiple roles"):
        freeze.freeze_role_dataset(
            source, tmp_path / "out", role_opponents=roles
        )


def test_freeze_requires_exact_val_and_gate_partition(tmp_path: Path) -> None:
    source = _collection(tmp_path)
    roles = _roles()
    roles["policy_selection"] = {"national_missing"}

    with pytest.raises(ValueError, match="must come from val"):
        freeze.freeze_role_dataset(
            source, tmp_path / "out", role_opponents=roles
        )


def test_freeze_rejects_overlapping_deck_blocks(tmp_path: Path) -> None:
    source = _collection(tmp_path)
    path = source / "pass_plans" / "pass_0001.json"
    plan = json.loads(path.read_text())
    plan["tasks"][1]["deck_seed_base"] = plan["tasks"][0]["deck_seed_base"] + 1
    plan["tasks"][1]["deck_seed_last"] = plan["tasks"][1]["deck_seed_base"] + 69
    path.write_text(json.dumps(plan), encoding="utf-8")

    with pytest.raises(RuntimeError, match="deck block mismatch|overlapping deck blocks"):
        _freeze(source, tmp_path / "out")


@pytest.mark.parametrize(
    "field",
    [
        "max_active_native_matches",
        "capacity_total_slots",
        "capacity_first_slot",
    ],
)
def test_freeze_rejects_float_pool_capacity(
    tmp_path: Path, field: str
) -> None:
    source = _collection(tmp_path)
    path = source / "pool_snapshots.jsonl"
    row = json.loads(path.read_text(encoding="utf-8"))
    row[field] = float(row[field])
    _write_jsonl(path, [row])

    with pytest.raises(RuntimeError, match="pool snapshot capacity contract"):
        _freeze(source, tmp_path / "out")


def test_formal_role_freeze_rejects_legacy_plan_without_ratings_evidence(
    tmp_path: Path,
) -> None:
    source = _collection(tmp_path)
    path = source / "pass_plans" / "pass_0001.json"
    plan = json.loads(path.read_text())
    plan.pop("schema_version")
    plan.pop("ratings_snapshot")
    path.write_text(json.dumps(plan), encoding="utf-8")

    with pytest.raises(RuntimeError, match="predates frozen ratings evidence"):
        _freeze(source, tmp_path / "out")


def test_freeze_rejects_inconsistent_ipw(tmp_path: Path) -> None:
    source = _collection(tmp_path)
    path = source / "cf_train.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]["decision_inverse_probability_weight"] = 1.0
    _write_jsonl(path, rows)

    with pytest.raises(RuntimeError, match="inconsistent IPW"):
        _freeze(source, tmp_path / "out")


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
def test_freeze_rejects_duplicate_stable_row_identity(
    tmp_path: Path, filename: str, state_field: str, modality: str
) -> None:
    source = _collection(tmp_path)
    path = source / filename
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows.append(dict(rows[0]))
    _write_jsonl(path, rows)
    state_path = source / "collector_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state[state_field]["train"] = len(rows)
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(
        RuntimeError, match=f"duplicate collector row identity in {modality}"
    ):
        _freeze(source, tmp_path / "out")


def test_freeze_rejects_illegal_response_supervision(tmp_path: Path) -> None:
    source = _collection(tmp_path)
    path = source / "opponent_actions_train.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]["opponent_action"] = "check"
    rows[0]["opponent_action_label_id"] = 1
    _write_jsonl(path, rows)

    with pytest.raises(ValueError, match="illegal"):
        _freeze(source, tmp_path / "out")


def test_freeze_uses_only_atomic_completed_prefix(tmp_path: Path) -> None:
    source = _collection(tmp_path)
    pool_path = source / "pool_snapshots.jsonl"
    first_pool = json.loads(pool_path.read_text(encoding="utf-8").splitlines()[0])
    _write_jsonl(pool_path, [first_pool, {"pass": 2}])
    extra = _value_row("national_v1", 6_000_000, 2_000_000)
    with (source / "cf_train.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(extra) + "\n")

    manifest = _freeze(source, tmp_path / "out")

    assert manifest["source_completed_passes"] == 1
    assert manifest["input_files"]["cf_train.jsonl"]["rows"] == 2
    assert manifest["input_files"]["cf_train.jsonl"][
        "truncated_to_collector_state"
    ] is True


def test_freeze_binds_candidate_snapshot_digest(tmp_path: Path) -> None:
    source = _collection(tmp_path)
    manifest_path = source / "collection_manifest.json"
    payload = json.loads(manifest_path.read_text())
    payload["resume_contract"]["candidate_snapshot_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="candidate snapshot digest mismatch"):
        _freeze(source, tmp_path / "out")


@pytest.mark.parametrize(
    "field",
    [
        "collector_sha256",
        "probe_sha256",
        "cross_hand_sequence_sha256",
        "runtime_capacity_sha256",
        "national_native_sha256",
    ],
)
def test_freeze_requires_current_collector_code_trust_root(
    tmp_path: Path, field: str
) -> None:
    source = _collection(tmp_path)
    manifest_path = source / "collection_manifest.json"
    payload = json.loads(manifest_path.read_text())
    payload["resume_contract"][field] = "0" * 64
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="collector code trust root"):
        _freeze(source, tmp_path / "out")


@pytest.mark.parametrize(
    "tamper",
    [
        lambda row: row.update({"strategy_context_available": True}),
        lambda row: row.update({"strategy_context_available": "true"}),
        lambda row: row.update({"strategy_context_available": 1}),
        lambda row: row["request"].update({
            "strategy_context_features": [0.0] * 65 + [1.0]
        }),
        lambda row: row["request"].update({"strategy_context": {}}),
    ],
)
def test_freeze_rejects_unobserved_strategy_context(
    tmp_path: Path, tamper,
) -> None:
    source = _collection(tmp_path)
    path = source / "cf_train.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0].setdefault("request", {})
    tamper(rows[0])
    _write_jsonl(path, rows)

    with pytest.raises(RuntimeError, match="zero-context role freeze"):
        _freeze(source, tmp_path / "out")


def test_freeze_requires_each_role_opponent_in_both_modalities(
    tmp_path: Path,
) -> None:
    source = _collection(tmp_path)
    path = source / "cf_train.jsonl"
    rows = [
        json.loads(line) for line in path.read_text().splitlines()
        if json.loads(line)["opponent"] != OPPONENTS["early_stop"]
    ]
    _write_jsonl(path, rows)
    state_path = source / "collector_state.json"
    state = json.loads(state_path.read_text())
    state["total_rows"]["train"] = len(rows)
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(RuntimeError, match="opponent coverage mismatch"):
        _freeze(source, tmp_path / "out")


def test_formal_160_freeze_requires_precommit_plan_before_data_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _collection(tmp_path)
    manifest_path = source / "collection_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["passes_requested"] = 160
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    state_path = source / "collector_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["completed_passes"] = 160
    state_path.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(
        freeze, "_read_jsonl_snapshot",
        lambda *args, **kwargs: pytest.fail("formal data was read before plan check"),
    )

    with pytest.raises(RuntimeError, match="requires --role-plan"):
        _freeze(source, tmp_path / "out")
