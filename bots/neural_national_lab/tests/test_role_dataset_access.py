from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import freeze_opponent_role_dataset as freeze  # noqa: E402
from match_outcome_schema import (  # noqa: E402
    MATCH_OUTCOME_ESTIMAND,
    match_outcome_metadata,
)
import opponent_exposure_ledger as ledger  # noqa: E402
from opponent_response_schema import (  # noqa: E402
    annotate_response_row,
    response_schema_metadata,
)
import role_dataset_access as access  # noqa: E402
from bots.neural_national_lab.tests.role_provenance_fixture import (  # noqa: E402
    add_formal_role_provenance,
    convert_to_legacy_recovery_prefix,
)


CANDIDATE_SHA = "a" * 64


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _dataset(tmp_path: Path, *, complete: bool = True) -> tuple[Path, Path]:
    root = tmp_path / "dataset"
    root.mkdir()
    candidate = root / "candidate"
    candidate.mkdir()
    roles = {
        role: [f"national_v{index}"]
        for index, role in enumerate(freeze.EVIDENCE_ROLES, 1)
    }
    outputs = {}
    for prefix in freeze.PREFIXES:
        for role, opponents in roles.items():
            filename = f"{prefix}_{role}.jsonl"
            row = {
                "opponent": opponents[0],
                "_split": role,
                "_evidence_role": role,
            }
            if prefix == "cf":
                mask = [0, 0, 1, 1, 0, 0]
                row.update({
                    "_collection_hands": 70,
                    "legal_mask": mask,
                    "rule_label_id": 2,
                    "baseline_match_net_chips": 100,
                    "match_delta_vs_rule": [
                        None, None, 0.0, -200.0, None, None,
                    ],
                    "match_action_values": [
                        None, None, 100.0, -100.0, None, None,
                    ],
                    "target_masks": {"match_delta_vs_rule": mask},
                    "probes": [],
                })
            else:
                row.update({
                    "stage": "preflop",
                    "hero_action": 200,
                    "hero_action_label_id": 2,
                    "opponent_action": "call",
                    "opponent_action_label_id": 2,
                    "opponent_action_amount": 100,
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
                        "public_cards": [],
                    },
                    "state": {"round": 0, "pot": 150, "to_call": 50},
                })
                row = annotate_response_row(row)
            raw = (json.dumps(row) + "\n").encode()
            (root / filename).write_bytes(raw)
            outputs[filename] = {
                "rows": 1,
                "bytes": len(raw),
                "sha256": _sha(raw),
                "opponents": opponents,
                **(
                    {"row_schema": "national_opponent_response_v2"}
                    if prefix == "opponent_actions"
                    else {}
                ),
            }
    manifest = {
        "schema": freeze.SCHEMA,
        "source_collection_complete": complete,
        "source_completed_passes": 160 if complete else 2,
        "source_requested_passes": 160,
        "candidate_snapshot": {
            "path": str(candidate),
            "name": candidate.name,
            "sha256": "f" * 64,
        },
        "strategy_context_runtime_mode": (
            freeze.STRATEGY_CONTEXT_RUNTIME_MODE
        ),
        "roles": roles,
        "outputs": outputs,
        "behavior_supervision": response_schema_metadata(),
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
    }
    if complete:
        add_formal_role_provenance(root, manifest)
    manifest_path = root / "role_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path, tmp_path / "ledger.json"


def _access(tmp_path: Path, *, complete: bool = True) -> access.RoleDatasetAccess:
    manifest, ledger_path = _dataset(tmp_path, complete=complete)
    return access.RoleDatasetAccess(
        manifest,
        ledger_path=ledger_path,
        run_id="run-1",
    )


def _selection_result(
    dataset: access.RoleDatasetAccess,
    path: Path,
    *,
    passed: bool = True,
) -> Path:
    path.write_text(json.dumps({
        "schema": access.POLICY_SELECTION_RESULT_SCHEMA,
        "passed": passed,
        "run_id": dataset.run_id,
        "candidate_sha256": CANDIDATE_SHA,
        "role_manifest_sha256": dataset.manifest_sha256,
        "offline_estimand": access.POLICY_OFFLINE_ESTIMAND,
        "match_outcome_estimand": MATCH_OUTCOME_ESTIMAND,
        "deployment_policy_value": False,
        "strength_evidence": False,
        "policy_gate_opened": False,
        "policy_selection_artifact_sha256": dataset._role_artifact_sha256(
            "policy_selection"
        ),
        "calibration_payload_sha256": "c" * 64,
        "evaluation_report_sha256": "d" * 64,
        "selected_policy_sha256": "e" * 64,
    }), encoding="utf-8")
    return path


def _formal_v4_result_fields(dataset: access.RoleDatasetAccess) -> dict:
    bootstrap = {
        "schema": "observed_70_hand_match_cluster_bootstrap_v1",
        "samples": 2000,
        "seed": 17,
        "observed_70_hand_match_clusters": True,
        "ordinary": True,
        "opponent_stratified": True,
    }
    return {
        "errors": [],
        "formal_selection": True,
        "source_collection_complete": True,
        "candidate_snapshot": dict(dataset.candidate_snapshot),
        "strategy_context_runtime_mode": (
            dataset.strategy_context_runtime_mode
        ),
        "thresholds": {
            "min_overrides": 12,
            "min_selection_clusters": 8,
            "min_override_clusters": 8,
            "min_overrides_per_opponent": 4,
            "min_override_hand_mean": 0.0,
            "bootstrap_samples": 2000,
            "min_cluster_ci_lower": 0.0,
            "min_opponent_stratified_ci_lower": 0.0,
            "min_match_outcome_coverage": 1.0,
            "min_match_positive_rate_ci_lower": 0.5,
            "min_match_positive_uplift_ci_lower": 0.0,
            "min_opponent_match_positive_rate": 0.5,
        },
        "summary": {"bootstrap_contract": bootstrap},
    }


def test_manifest_load_does_not_touch_role_files(tmp_path: Path) -> None:
    manifest, ledger_path = _dataset(tmp_path)
    policy_gate = manifest.parent / "cf_policy_gate.jsonl"
    policy_gate.unlink()

    dataset = access.RoleDatasetAccess(
        manifest, ledger_path=ledger_path, run_id="run-1"
    )

    assert dataset.roles["policy_gate"] == ["national_v5"]
    assert not ledger_path.exists()


def test_manifest_rejects_cross_role_opponent_overlap_even_if_self_reported(
    tmp_path: Path,
) -> None:
    manifest_path, ledger_path = _dataset(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    shared = manifest["roles"]["train"][0]
    manifest["roles"]["early_stop"] = [shared]
    for prefix in freeze.PREFIXES:
        manifest["outputs"][f"{prefix}_early_stop.jsonl"]["opponents"] = [
            shared
        ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="multiple roles"):
        access.RoleDatasetAccess(
            manifest_path,
            ledger_path=ledger_path,
            run_id="overlap",
        )


def test_formal_provenance_replays_plan_roles_after_hash_rewrite(
    tmp_path: Path,
) -> None:
    manifest_path, ledger_path = _dataset(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    plan_path = manifest_path.parent / "pass_plans" / "pass_0001.json"
    plan = json.loads(plan_path.read_text())
    early_stop = manifest["roles"]["early_stop"][0]
    next(task for task in plan["tasks"] if task["name"] == early_stop)[
        "split"
    ] = "held_out"
    raw = json.dumps(plan, sort_keys=True).encode()
    plan_path.write_bytes(raw)
    manifest["pass_plan_sha256"][plan_path.name] = _sha(raw)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(
        (ValueError, RuntimeError),
        match="role mismatch|crosses source splits|must come from train",
    ):
        access.RoleDatasetAccess(
            manifest_path,
            ledger_path=ledger_path,
            run_id="plan-rewrite",
        )
    assert not ledger_path.exists()


def test_open_records_exposure_and_validates_both_modalities(tmp_path: Path) -> None:
    dataset = _access(tmp_path)

    opened = dataset.open_role("train")

    assert len(opened["value"]) == 1
    assert len(opened["behavior"]) == 1
    report = ledger.status(dataset.ledger_path)
    exposure = report["opponents"]["national_v1"]["exposures"][0]
    assert exposure["role"] == "train"
    assert exposure["artifact_sha256"] == opened["artifact_sha256"]


def test_policy_role_cannot_open_before_prerequisites(tmp_path: Path) -> None:
    dataset = _access(tmp_path)
    gate_path = dataset.root / "cf_policy_selection.jsonl"
    gate_path.unlink()

    with pytest.raises(RuntimeError, match="prerequisite"):
        dataset.open_role(
            "policy_selection", candidate_sha256=CANDIDATE_SHA
        )
    assert not dataset.ledger_path.exists()


def test_policy_gate_is_bound_to_same_frozen_candidate(tmp_path: Path) -> None:
    dataset = _access(tmp_path)
    for role in ("train", "early_stop", "model_calibration"):
        dataset.open_role(role)
    dataset.open_role("policy_selection", candidate_sha256=CANDIDATE_SHA)
    result = _selection_result(dataset, tmp_path / "selection_result.json")

    with pytest.raises(RuntimeError, match="prerequisite"):
        dataset.open_role("policy_gate", candidate_sha256="b" * 64)
    opened = dataset.open_role(
        "policy_gate",
        candidate_sha256=CANDIDATE_SHA,
        prerequisite_report=result,
    )

    assert opened["candidate_sha256"] == CANDIDATE_SHA
    assert opened["prerequisite_sha256"] is not None
    assert opened["prerequisite_calibration_payload_sha256"] == "c" * 64


def test_failed_policy_selection_does_not_open_policy_gate_data(
    tmp_path: Path,
) -> None:
    dataset = _access(tmp_path)
    for role in ("train", "early_stop", "model_calibration"):
        dataset.open_role(role)
    dataset.open_role("policy_selection", candidate_sha256=CANDIDATE_SHA)
    result = _selection_result(
        dataset, tmp_path / "selection_result.json", passed=False
    )
    gate_path = dataset.root / "cf_policy_gate.jsonl"
    gate_path.unlink()

    with pytest.raises(RuntimeError, match="did not authorize"):
        dataset.open_role(
            "policy_gate",
            candidate_sha256=CANDIDATE_SHA,
            prerequisite_report=result,
        )

    report = ledger.status(dataset.ledger_path)
    assert "national_v5" not in report["opponents"]


def test_policy_gate_rejects_selection_role_artifact_mismatch(
    tmp_path: Path,
) -> None:
    dataset = _access(tmp_path)
    for role in ("train", "early_stop", "model_calibration"):
        dataset.open_role(role)
    dataset.open_role("policy_selection", candidate_sha256=CANDIDATE_SHA)
    result = _selection_result(dataset, tmp_path / "selection_result.json")
    payload = json.loads(result.read_text())
    payload["policy_selection_artifact_sha256"] = "0" * 64
    result.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="did not authorize"):
        dataset.open_role(
            "policy_gate",
            candidate_sha256=CANDIDATE_SHA,
            prerequisite_report=result,
        )

    report = ledger.status(dataset.ledger_path)
    assert "national_v5" not in report["opponents"]


def test_policy_gate_accepts_only_registered_v4_schema_estimand_pair(
    tmp_path: Path,
) -> None:
    dataset = _access(tmp_path)
    for role in ("train", "early_stop", "model_calibration"):
        dataset.open_role(role)
    dataset.open_role("policy_selection", candidate_sha256=CANDIDATE_SHA)
    result = _selection_result(dataset, tmp_path / "selection_result_v4.json")
    payload = json.loads(result.read_text())
    payload.update({
        "schema": access.POLICY_SELECTION_RESULT_SCHEMA_V4,
        "offline_estimand": access.POLICY_OFFLINE_ESTIMAND_V4,
        **_formal_v4_result_fields(dataset),
    })
    result.write_text(json.dumps(payload), encoding="utf-8")

    opened = dataset.open_role(
        "policy_gate",
        candidate_sha256=CANDIDATE_SHA,
        prerequisite_report=result,
    )
    assert opened["candidate_sha256"] == CANDIDATE_SHA

    payload["offline_estimand"] = access.POLICY_OFFLINE_ESTIMAND
    result.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="did not authorize"):
        dataset.open_role(
            "policy_gate",
            candidate_sha256=CANDIDATE_SHA,
            prerequisite_report=result,
        )


def test_requested_v4_gate_contract_rejects_v3_result_before_exposure(
    tmp_path: Path,
) -> None:
    dataset = _access(tmp_path)
    for role in ("train", "early_stop", "model_calibration"):
        dataset.open_role(role)
    dataset.open_role("policy_selection", candidate_sha256=CANDIDATE_SHA)
    result = _selection_result(dataset, tmp_path / "selection_result_v3.json")

    with pytest.raises(RuntimeError, match="did not authorize"):
        dataset.open_role(
            "policy_gate",
            candidate_sha256=CANDIDATE_SHA,
            prerequisite_report=result,
            prerequisite_schema=access.POLICY_SELECTION_RESULT_SCHEMA_V4,
            prerequisite_offline_estimand=access.POLICY_OFFLINE_ESTIMAND_V4,
        )

    report = ledger.status(dataset.ledger_path)
    assert "national_v5" not in report["opponents"]


def test_unknown_selection_contract_cannot_match_missing_estimand(
    tmp_path: Path,
) -> None:
    dataset = _access(tmp_path)
    for role in ("train", "early_stop", "model_calibration"):
        dataset.open_role(role)
    dataset.open_role("policy_selection", candidate_sha256=CANDIDATE_SHA)
    result = _selection_result(dataset, tmp_path / "unknown_selection.json")
    payload = json.loads(result.read_text())
    payload["schema"] = "unknown_policy_selection_result"
    payload.pop("offline_estimand")
    result.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="did not authorize"):
        dataset.open_role(
            "policy_gate",
            candidate_sha256=CANDIDATE_SHA,
            prerequisite_report=result,
        )

    report = ledger.status(dataset.ledger_path)
    assert "national_v5" not in report["opponents"]


def test_stale_role_artifact_exposure_does_not_satisfy_prerequisite(
    tmp_path: Path,
) -> None:
    dataset = _access(tmp_path)
    ledger.open_exposure(
        dataset.ledger_path,
        role="train",
        opponents=dataset.roles["train"],
        run_id=dataset.run_id,
        artifact_sha256="0" * 64,
    )
    for role in ("early_stop", "model_calibration"):
        dataset.open_role(role)

    with pytest.raises(RuntimeError, match="prerequisite"):
        dataset.open_role(
            "policy_selection", candidate_sha256=CANDIDATE_SHA
        )


def test_failed_file_validation_remains_conservatively_exposed(
    tmp_path: Path,
) -> None:
    dataset = _access(tmp_path)
    path = dataset.root / "cf_train.jsonl"
    path.write_text("corrupt\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="artifact changed"):
        dataset.open_role("train")

    report = ledger.status(dataset.ledger_path)
    assert report["opponents"]["national_v1"]["exposures"][0]["role"] == "train"


def test_canonical_response_validation_cannot_be_bypassed_by_manifest(
    tmp_path: Path,
) -> None:
    manifest_path, ledger_path = _dataset(tmp_path)
    path = manifest_path.parent / "opponent_actions_train.jsonl"
    row = json.loads(path.read_text())
    row["response_legal_action_mask"][row["opponent_action_label_id"]] = 0
    raw = (json.dumps(row) + "\n").encode()
    path.write_bytes(raw)
    manifest = json.loads(manifest_path.read_text())
    manifest["outputs"][path.name]["bytes"] = len(raw)
    manifest["outputs"][path.name]["sha256"] = _sha(raw)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    dataset = access.RoleDatasetAccess(
        manifest_path, ledger_path=ledger_path, run_id="run-1"
    )

    with pytest.raises(RuntimeError, match="invalid response row"):
        dataset.open_role("train")

    report = ledger.status(ledger_path)
    assert report["opponents"]["national_v1"]["exposures"][0]["role"] == "train"


@pytest.mark.parametrize(
    ("filename", "context"),
    [
        ("cf_train.jsonl", {"strategy_context_features": [1.0] * 66}),
        ("cf_train.jsonl", {"strategy_context_available": "true"}),
        ("cf_train.jsonl", {"strategy_context_available": 1}),
        ("cf_train.jsonl", {"strategy_context": {}}),
        (
            "opponent_actions_train.jsonl",
            {"strategy_context_available": "true"},
        ),
    ],
)
def test_zero_context_validation_cannot_be_bypassed_by_manifest(
    tmp_path: Path, filename: str, context: dict,
) -> None:
    manifest_path, ledger_path = _dataset(tmp_path)
    path = manifest_path.parent / filename
    row = json.loads(path.read_text())
    row["request"] = context
    raw = (json.dumps(row) + "\n").encode()
    path.write_bytes(raw)
    manifest = json.loads(manifest_path.read_text())
    manifest["outputs"][path.name]["bytes"] = len(raw)
    manifest["outputs"][path.name]["sha256"] = _sha(raw)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    dataset = access.RoleDatasetAccess(
        manifest_path, ledger_path=ledger_path, run_id="run-1"
    )

    with pytest.raises(RuntimeError, match="zero-context"):
        dataset.open_role("train")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("errors", ["forged"]),
        ("formal_selection", False),
        ("source_collection_complete", False),
    ],
)
def test_policy_gate_rejects_minimally_forged_v4_prerequisite(
    tmp_path: Path, field: str, value,
) -> None:
    dataset = _access(tmp_path)
    for role in ("train", "early_stop", "model_calibration"):
        dataset.open_role(role)
    dataset.open_role("policy_selection", candidate_sha256=CANDIDATE_SHA)
    result = _selection_result(dataset, tmp_path / "selection_result_v4.json")
    payload = json.loads(result.read_text())
    payload.update({
        "schema": access.POLICY_SELECTION_RESULT_SCHEMA_V4,
        "offline_estimand": access.POLICY_OFFLINE_ESTIMAND_V4,
        **_formal_v4_result_fields(dataset),
        field: value,
    })
    result.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="did not authorize"):
        dataset.open_role(
            "policy_gate",
            candidate_sha256=CANDIDATE_SHA,
            prerequisite_report=result,
            prerequisite_schema=access.POLICY_SELECTION_RESULT_SCHEMA_V4,
            prerequisite_offline_estimand=access.POLICY_OFFLINE_ESTIMAND_V4,
        )


def test_complete_collection_is_required_for_training_access(
    tmp_path: Path,
) -> None:
    manifest, ledger_path = _dataset(tmp_path, complete=False)

    with pytest.raises(ValueError, match="incomplete"):
        access.RoleDatasetAccess(
            manifest, ledger_path=ledger_path, run_id="run-1"
        )
    smoke = access.RoleDatasetAccess(
        manifest,
        ledger_path=ledger_path,
        run_id="smoke",
        require_complete=False,
    )
    assert smoke.manifest["source_collection_complete"] is False


def test_formal_collection_boundary_requires_exact_atomic_160_passes(
    tmp_path: Path,
) -> None:
    dataset = _access(tmp_path)
    boundary = dataset.require_collection_boundary()
    assert boundary == {
        "schema": "complete_atomic_collection_boundary_v1",
        "source_completed_passes": 160,
        "source_requested_passes": 160,
        "source_collection_complete": True,
    }


def test_formal_boundary_accepts_strict_75_plus_recovered_76_prefix(
    tmp_path: Path,
) -> None:
    manifest_path, ledger_path = _dataset(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    convert_to_legacy_recovery_prefix(
        manifest_path.parent, manifest, completed_prefix=75
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    dataset = access.RoleDatasetAccess(
        manifest_path, ledger_path=ledger_path, run_id="legacy-recovered"
    )

    assert dataset.require_collection_boundary()["source_completed_passes"] == 160


def test_formal_boundary_rejects_re_signed_invalid_legacy_receipt(
    tmp_path: Path,
) -> None:
    manifest_path, ledger_path = _dataset(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    convert_to_legacy_recovery_prefix(
        manifest_path.parent, manifest, completed_prefix=75
    )
    collection_path = manifest_path.parent / "collection_manifest.json"
    collection = json.loads(collection_path.read_text(encoding="utf-8"))
    collection["legacy_recovery"]["completed_prefix_pass"] = 74
    unsigned = dict(collection["legacy_recovery"])
    unsigned.pop("receipt_sha256")
    collection["legacy_recovery"]["receipt_sha256"] = freeze._canonical_sha256(
        unsigned
    )
    raw = json.dumps(collection, sort_keys=True).encode()
    collection_path.write_bytes(raw)
    manifest["collection_manifest_sha256"] = _sha(raw)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match="completed-plan prefix|boundary"):
        access.RoleDatasetAccess(
            manifest_path, ledger_path=ledger_path, run_id="invalid-recovery"
        )
    assert not ledger_path.exists()


@pytest.mark.parametrize(
    "updates",
    [
        {"source_completed_passes": 2, "source_requested_passes": 2},
        {"source_completed_passes": True},
        {"source_requested_passes": True},
        {"remove": "source_completed_passes"},
        {"remove": "source_requested_passes"},
    ],
)
def test_formal_collection_boundary_rejects_partial_or_missing_fields(
    tmp_path: Path, updates: dict,
) -> None:
    manifest_path, ledger_path = _dataset(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    remove = updates.pop("remove", None)
    manifest.update(updates)
    if remove is not None:
        manifest.pop(remove)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="complete atomic 160-pass"):
        access.RoleDatasetAccess(
            manifest_path,
            ledger_path=ledger_path,
            run_id="formal-boundary",
        )


def test_complete_constructor_replays_physical_plan_prefix_before_any_open(
    tmp_path: Path,
) -> None:
    manifest_path, ledger_path = _dataset(tmp_path)
    (manifest_path.parent / "pass_plans" / "pass_0160.json").unlink()

    with pytest.raises(ValueError, match="pass-plan set"):
        access.RoleDatasetAccess(
            manifest_path,
            ledger_path=ledger_path,
            run_id="short-physical-prefix",
        )
    assert not ledger_path.exists()


def test_formal_collection_boundary_rejects_boolean_expected_passes(
    tmp_path: Path,
) -> None:
    dataset = _access(tmp_path)
    with pytest.raises(ValueError, match="positive integer"):
        dataset.require_collection_boundary(True)
