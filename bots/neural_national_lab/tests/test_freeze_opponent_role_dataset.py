from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]
TOOLS = ROOT / "bots" / "neural_national_lab" / "tools"
sys.path.insert(0, str(TOOLS))

import freeze_opponent_role_dataset as freeze  # noqa: E402
from longrun_collect_oppmodel import _directory_digest  # noqa: E402


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
        digest = _directory_digest(snapshot)
        registry["opponents"][name] = {
            "snapshot_path": str(snapshot),
            "execution_directory_sha256": digest,
        }
        tasks.append({
            "name": name,
            "split": split,
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
            "deck_seed_scheme": "disjoint_match_blocks_v1",
            "candidate_execution_path": str(candidate),
            "candidate_snapshot_sha256": _directory_digest(candidate),
        },
    }), encoding="utf-8")
    (source / "collector_state.json").write_text(json.dumps({
        "completed_passes": 1,
        "total_rows": {split: len(rows[split]) for split in freeze.SOURCE_SPLITS},
        "total_behavior_rows": {
            split: len(behaviors[split]) for split in freeze.SOURCE_SPLITS
        },
    }), encoding="utf-8")
    _write_jsonl(source / "pool_snapshots.jsonl", [{"pass": 1}])
    (source / "pass_plans" / "pass_0001.json").write_text(json.dumps({
        "pass": 1,
        "seed_scheme": "disjoint_match_blocks_v1",
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

    assert manifest["schema"] == "opponent_role_dataset_v3"
    assert manifest["source_completed_passes"] == 1
    assert manifest["source_requested_passes"] == 2
    assert manifest["source_collection_complete"] is False
    assert manifest["invariants"]["deck_blocks_non_overlapping"] is True
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

    with pytest.raises(RuntimeError, match="overlapping deck blocks"):
        _freeze(source, tmp_path / "out")


def test_freeze_rejects_inconsistent_ipw(tmp_path: Path) -> None:
    source = _collection(tmp_path)
    path = source / "cf_train.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]["decision_inverse_probability_weight"] = 1.0
    _write_jsonl(path, rows)

    with pytest.raises(RuntimeError, match="inconsistent IPW"):
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
    _write_jsonl(
        source / "pool_snapshots.jsonl", [{"pass": 1}, {"pass": 2}]
    )
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
