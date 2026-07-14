from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys

import pytest
import torch


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import match_outcome_calibration as outcome_calibration  # noqa: E402
from match_outcome_schema import (  # noqa: E402
    MATCH_OUTCOME_ESTIMAND,
    MATCH_OUTCOME_SCHEMA,
    POSITIVE_OUTCOME_RULE,
)
import opponent_multitask_model_v4 as models  # noqa: E402
import policy_role_evidence as evidence  # noqa: E402
import select_opponent_multitask_v4_policy as policy  # noqa: E402
import export_opponent_multitask_ensemble_v4 as bundle_exporter  # noqa: E402
import v4_runtime_budget as runtime_budget  # noqa: E402
import win_first_policy_v4 as win_first  # noqa: E402


REAL_VERIFY_PRESELECTION_RUNTIME_BUDGET = (
    policy.verify_preselection_runtime_budget
)


@pytest.fixture(autouse=True)
def _verify_static_runtime_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    def verify(path: Path, **kwargs) -> dict:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return runtime_budget.validate_runtime_budget_artifact(
            payload, require_formal=bool(kwargs.get("formal"))
        )

    monkeypatch.setattr(policy, "verify_preselection_runtime_budget", verify)


def _inference_row() -> dict:
    return {
        "encoded_context_schema": "opponent_multitask_inference_context_v3",
        "response_mode": False,
        "state": [0.1] * 81,
        "opponent_profile": [0.2] * 12,
        "history": [],
        "cross_hand_sequence": [],
        "rule_action": [0.0, 0.0, 1.0, 0.0, 0.0, 0.0],
        "strategy_context": [0.0] * 66,
        "strategy_context_available": False,
        "legal_action_mask": [0, 0, 1, 1, 0, 0],
        "opponent": "national_v98",
    }


class _OutcomeModel:
    def __init__(self, logits: list[float]) -> None:
        self.logits = logits

    def eval(self):
        return self

    def forward_match_outcome(self, **inputs):
        batch = inputs["state"].shape[0]
        return torch.tensor([self.logits] * batch, dtype=torch.float32)


def _minimal_calibration(*, scale: float = 1.0, bias: float = 0.0) -> dict:
    return {
        "schema": outcome_calibration.CALIBRATION_SCHEMA,
        "method": outcome_calibration.CALIBRATION_METHOD,
        "scale": scale,
        "bias": bias,
    }


def _bound_calibration(
    checkpoint: str,
    *,
    run_id: str = "run-1",
    role_manifest: str = "b" * 64,
    role_artifact: str = "c" * 64,
    complete: bool = True,
) -> dict:
    result = {
        **_minimal_calibration(),
        "run_id": run_id,
        "model_format": models.MODEL_FORMAT,
        "checkpoint_sha256": checkpoint,
        "role_manifest_sha256": role_manifest,
        "model_calibration_artifact_sha256": role_artifact,
        "model_calibration_opponents": ["national_v142"],
        "source_collection_complete": complete,
        "metrics": {},
        "deployment_policy_value": False,
        "strength_evidence": False,
    }
    result["payload_sha256"] = (
        outcome_calibration.calibration_payload_sha256(result)
    )
    return result


def _selected_policy() -> dict:
    result = win_first.normalize_policy({
        "schema": win_first.POLICY_SCHEMA,
        "selection_priority": win_first.SELECTION_PRIORITY,
        "min_positive_probability_lcb": 0.5,
        "min_probability_uplift_lcb": 0.0,
        "chip_margin": 0.0,
        "hand_weight": 0.25,
        "tail_weight": 0.25,
        "match_weight": 0.50,
        "response_weight": 0.0,
        "min_hand_lcb": 0.0,
        "use_lower": True,
    })
    assert result is not None
    return result


def _prepared_rows(
    *,
    negative_second: bool = False,
    clusters_per_opponent: int = 2,
    decisions_per_cluster: int = 1,
) -> list[dict]:
    rows = []
    outcomes = win_first.aggregate_member_probabilities(
        [[0.1, 0.1, 0.2, 0.8, 0.1, 0.1]],
        uncertainty_std_weight=0.0,
    )
    for opponent_index, opponent in enumerate(("national_v98", "national_v142")):
        for cluster in range(clusters_per_opponent):
            for decision in range(decisions_per_cluster):
                observed = -200.0 if negative_second and opponent_index else 100.0
                values = {
                    field: {
                        "mean": [0.0, 0.0, 0.0, 100.0, 0.0, 0.0],
                        "lower": [0.0, 0.0, 0.0, 100.0, 0.0, 0.0],
                    }
                    for field in (
                        "delta_vs_rule",
                        "tail_delta_vs_rule",
                        "match_delta_vs_rule",
                    )
                }
                rows.append({
                    "source_row_index": len(rows),
                    "opponent": opponent,
                    "cluster": f"{opponent}|{cluster}",
                    "rule_id": 2,
                    "sampling_weight": 1.0,
                    "outcomes": outcomes,
                    "values": values,
                    "match_outcome": {
                        "schema": MATCH_OUTCOME_SCHEMA,
                        "estimand": MATCH_OUTCOME_ESTIMAND,
                        "hands": 70,
                        "positive_outcome_rule": POSITIVE_OUTCOME_RULE,
                        "baseline_match_net_chips": 100.0,
                        "baseline_match_positive": 1,
                    },
                    "decision": {"cluster": cluster, "decision": decision},
                    "candidates": [{
                        "label_id": 3,
                        "label": "raise_pot",
                        "action": 300,
                        "response_signal": 0.0,
                        "hand_delta": observed,
                        "tail_delta": observed,
                        "match_delta": observed,
                        "match_outcome_schema": MATCH_OUTCOME_SCHEMA,
                        "forced_match_net_chips": 100.0 + observed,
                        "forced_match_positive": int(100.0 + observed > 0.0),
                        "match_positive_uplift": (
                            int(100.0 + observed > 0.0) - 1
                        ),
                    }],
                })
    return rows


def _selection(
    rows: list[dict], *, bootstrap_samples: int = 100
) -> dict:
    return policy.select_policy(
        rows,
        chip_margins=[0.0],
        hand_weights=[0.25],
        tail_weights=[0.25],
        response_weights=[0.0],
        min_match_weight=0.5,
        min_overrides=1,
        min_selection_clusters=1,
        min_override_clusters=1,
        min_overrides_per_opponent=1,
        min_override_hand_mean=0.0,
        bootstrap_samples=bootstrap_samples,
        bootstrap_seed=7,
        min_cluster_ci_lower=0.0,
        min_opponent_stratified_ci_lower=0.0,
        min_match_positive_rate_ci_lower=0.5,
        min_match_positive_uplift_ci_lower=0.0,
        min_opponent_match_positive_rate=0.5,
    )


def test_torch_outcomes_call_shared_calibration_and_aggregation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    shared = policy.aggregate_member_probabilities

    def tracked(values, *, uncertainty_std_weight):
        calls.append((values, uncertainty_std_weight))
        return shared(values, uncertainty_std_weight=uncertainty_std_weight)

    monkeypatch.setattr(policy, "aggregate_member_probabilities", tracked)
    result = policy.aggregate_outcome_predictions(
        [
            _OutcomeModel([0.0, 0.0, 0.0, 1.0, 0.0, 0.0]),
            _OutcomeModel([0.0, 0.0, 0.0, 2.0, 0.0, 0.0]),
        ],
        [_minimal_calibration(), _minimal_calibration(bias=0.25)],
        [_inference_row()],
        uncertainty_std_weight=1.0,
        batch_size=8,
        device="cpu",
    )

    assert len(calls) == 1
    assert calls[0][1] == 1.0
    assert result[0] == shared(calls[0][0], uncertainty_std_weight=1.0)


def test_policy_selection_calls_shared_win_first_and_uses_observed_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    shared = policy.select_candidate

    def tracked(*args, **kwargs):
        calls.append((args, kwargs))
        return shared(*args, **kwargs)

    monkeypatch.setattr(policy, "select_candidate", tracked)
    positive = _selection(_prepared_rows())
    negative = _selection(_prepared_rows(negative_second=True))

    assert positive["selected"] is not None
    selected_policy = positive["selected"]["config"]
    assert selected_policy["min_positive_probability_lcb"] == 0.5
    assert selected_policy["min_probability_uplift_lcb"] == 0.0
    assert selected_policy["min_hand_lcb"] == 0.0
    assert positive["selected"]["override_clusters"] == 4
    assert positive["bootstrap_contract"] == {
        "schema": evidence.BOOTSTRAP_CONTRACT_SCHEMA,
        "samples": 100,
        "seed": 7,
        "observed_70_hand_match_clusters": True,
        "ordinary": True,
        "opponent_stratified": True,
    }
    assert calls
    assert negative["selected"] is None
    assert any(
        "cluster_ci" in error or "national_v142:mean<0" in error
        for error in negative["grid"][0]["eligibility_errors"]
    )


def test_incomplete_evaluation_never_freezes_a_policy() -> None:
    selection = _selection(_prepared_rows())

    evaluation = policy.policy_evaluation(selection, incomplete_smoke=True)

    assert evaluation["selected_policy"] is None
    assert evaluation["provisional_selected_policy"] is not None
    assert evaluation["source_collection_complete"] is False
    assert evaluation["deployment_policy_value"] is False
    assert evaluation["strength_evidence"] is False
    assert evaluation["bootstrap_contract"] == selection["bootstrap_contract"]


class _Dataset:
    run_id = "run-1"
    manifest_sha256 = "b" * 64
    candidate_snapshot = {"name": "v140_test", "sha256": "a" * 64}
    strategy_context_runtime_mode = "zero_vector_training_aligned_v1"
    manifest = {
        "source_collection_complete": True,
        "candidate_snapshot": candidate_snapshot,
        "strategy_context_runtime_mode": strategy_context_runtime_mode,
    }
    roles = {
        "model_calibration": ["national_v142"],
        "policy_selection": ["national_v98", "national_v142"],
    }

    def __init__(self, exposed_candidate_sha256: str | None = None) -> None:
        self.exposed_candidate_sha256 = exposed_candidate_sha256

    def _role_artifact_sha256(self, role: str) -> str:
        assert role == "model_calibration"
        return "c" * 64

    def require_collection_boundary(self, expected_passes: int = 160) -> dict:
        assert expected_passes == 160
        return {
            "schema": "complete_atomic_collection_boundary_v1",
            "source_completed_passes": 160,
            "source_requested_passes": 160,
            "source_collection_complete": True,
        }

    def runtime_context_contract(self) -> dict:
        return {
            "candidate_snapshot": dict(self.candidate_snapshot),
            "strategy_context_runtime_mode": self.strategy_context_runtime_mode,
        }

    def _role_was_opened(self, role: str, *, candidate_sha256: str) -> bool:
        assert role == "policy_selection"
        return candidate_sha256 == self.exposed_candidate_sha256

    def open_role(self, role: str, *, candidate_sha256: str, **kwargs) -> dict:
        assert role == "policy_selection"
        assert kwargs == {}
        assert candidate_sha256 == self.exposed_candidate_sha256
        return {
            "artifact_sha256": "9" * 64,
            "opponents": list(self.roles["policy_selection"]),
            "value": [{"protected_selection_value": True}],
            "behavior": [{"protected_selection_behavior": True}],
        }


def _calibrated(member_count: int = 3) -> dict:
    members = []
    for index in range(member_count):
        checkpoint = f"{index + 1:064x}"
        members.append({
            "seed": index + 101,
            "checkpoint_sha256": checkpoint,
            "outcome_calibration": _bound_calibration(checkpoint),
        })
    return {
        "members": members,
        "models": [object() for _ in members],
        "outcome_uncertainty_std_weight": 1.0,
        "ensemble_manifest_sha256": "d" * 64,
        "calibration_payload_sha256": "e" * 64,
        "artifact_manifest_sha256": "f" * 64,
    }


def _formal_policy_contract(calibrated: dict) -> dict:
    return {
        "schema": win_first.POLICY_SCHEMA,
        "selection_priority": win_first.SELECTION_PRIORITY,
        "min_positive_probability_lcb": 0.5,
        "min_probability_uplift_lcb": 0.0,
        "min_hand_lcb": 0.0,
        "chip_margins": [0.0, 25.0],
        "hand_weights": [0.25],
        "tail_weights": [0.25],
        "response_weights": [0.0],
        "min_match_weight": 0.5,
        "outcome_aggregation": win_first.OUTCOME_AGGREGATION_METHOD,
        "outcome_uncertainty_std_weight": calibrated[
            "outcome_uncertainty_std_weight"
        ],
        "min_overrides": 12,
        "min_selection_clusters": 8,
        "min_override_clusters": 8,
        "min_overrides_per_opponent": 4,
        "min_override_hand_mean": 0.0,
        "min_cluster_ci_lower": 0.0,
        "min_opponent_stratified_ci_lower": 0.0,
        "min_match_positive_rate_ci_lower": 0.5,
        "min_match_positive_uplift_ci_lower": 0.0,
        "min_opponent_match_positive_rate": 0.5,
        "bootstrap_samples": 2000,
        "bootstrap_seed": 7,
        "offline_estimand": policy.POLICY_OFFLINE_ESTIMAND_V4,
    }


def _formal_preparation(rows: list[dict]) -> dict:
    return {
        "prepared_rows": len(rows),
        "outcome_predictions": len(rows),
        "outcome_aggregation": win_first.OUTCOME_AGGREGATION_METHOD,
    }


def test_formal_calibrated_ensemble_requires_three_members_on_one_role() -> None:
    validated = policy._validated_calibrated_ensemble(
        _calibrated(), dataset=_Dataset(), run_id="run-1", formal=True
    )
    assert len(validated["outcome_calibrations"]) == 3

    with pytest.raises(ValueError, match="three-seed"):
        policy._validated_calibrated_ensemble(
            _calibrated(2), dataset=_Dataset(), run_id="run-1", formal=True
        )

    drifted = _calibrated()
    drifted["members"][1]["outcome_calibration"] = _bound_calibration(
        drifted["members"][1]["checkpoint_sha256"],
        role_artifact="9" * 64,
    )
    with pytest.raises(ValueError, match="one protected calibration role"):
        policy._validated_calibrated_ensemble(
            drifted, dataset=_Dataset(), run_id="run-1", formal=True
        )


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _policy_artifacts(root: Path, calibrated: dict) -> None:
    root.mkdir()
    budget_artifact = runtime_budget._artifact(
        bundle_bytes=1_000,
        bundle_sha256="e" * 64,
        runtime_identity_sha256="d" * 64,
        source_collection_complete=True,
        cpu_ns=[1] * runtime_budget.MEASURED_REPEATS,
        wall_ns=[2] * runtime_budget.MEASURED_REPEATS,
        warmups_completed=runtime_budget.WARMUP_ROUNDS,
        errors=[],
    )
    _write_json(root / policy.RUNTIME_BUDGET_NAME, budget_artifact)
    budget_sha256 = budget_artifact["payload_sha256"]
    identity_sha256 = budget_artifact["runtime_identity_sha256"]
    rows = _prepared_rows(clusters_per_opponent=4, decisions_per_cluster=2)
    policy_contract = _formal_policy_contract(calibrated)
    selection = policy.select_policy(
        rows, **policy._selection_kwargs_from_contract(policy_contract)
    )
    evaluation = policy.policy_evaluation(selection, incomplete_smoke=False)
    selected = evaluation["selected_policy"]
    assert selected == _selected_policy()
    code_artifacts = policy.selector_code_artifacts()
    runtime_context = _Dataset().runtime_context_contract()
    candidate = {
        "schema": policy.POLICY_CANDIDATE_SCHEMA,
        "run_id": "run-1",
        "role_manifest_sha256": _Dataset.manifest_sha256,
        "ensemble_manifest_sha256": calibrated["ensemble_manifest_sha256"],
        "calibration_artifact_manifest_sha256": calibrated[
            "artifact_manifest_sha256"
        ],
        "calibration_payload_sha256": calibrated["calibration_payload_sha256"],
        "member_checkpoint_sha256": [
            member["checkpoint_sha256"] for member in calibrated["members"]
        ],
        "outcome_calibration_payload_sha256": [
            member["outcome_calibration"]["payload_sha256"]
            for member in calibrated["members"]
        ],
        "runtime_budget_payload_sha256": budget_sha256,
        "runtime_identity_sha256": identity_sha256,
        "source_collection_complete": True,
        "formal_selection": True,
        "collection_boundary": _Dataset().require_collection_boundary(),
        "inference_contract": {
            "device": "cpu",
            "batch_size": 8,
            "torch_version": torch.__version__,
            "value_aggregation": (
                "mean_member_quantile_minus_mean_value_std_plus_calibration"
            ),
            "response_aggregation": "mean_member_logits_then_temperature",
            "outcome_aggregation": win_first.OUTCOME_AGGREGATION_METHOD,
        },
        "policy_contract": policy_contract,
        **runtime_context,
        "code_artifacts": code_artifacts,
        "deployment_policy_value": False,
        "strength_evidence": False,
    }
    candidate_path = root / "candidate_manifest.json"
    _write_json(candidate_path, candidate)
    candidate_sha = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
    preparation = _formal_preparation(rows)
    evaluation["preparation"] = preparation
    evaluation["policy_contract"] = policy_contract
    evaluation["code_artifacts"] = code_artifacts
    evaluation["runtime_budget_payload_sha256"] = budget_sha256
    evaluation["runtime_identity_sha256"] = identity_sha256
    evaluation.update(runtime_context)
    _write_json(root / "policy_evaluation.json", evaluation)
    phase = {
        "schema": evidence.POLICY_SELECTION_PHASE_SCHEMA_V4,
        "run_id": "run-1",
        "candidate_sha256": candidate_sha,
        "role_manifest_sha256": _Dataset.manifest_sha256,
        "policy_selection_artifact_sha256": "9" * 64,
        "calibration_payload_sha256": calibrated["calibration_payload_sha256"],
    }
    result = evidence.build_policy_selection_result(
        phase,
        evaluation,
        thresholds=policy._selection_thresholds_from_contract(policy_contract),
        contract="v4",
    )
    result["source_collection_complete"] = True
    result["formal_selection"] = True
    result["runtime_budget_payload_sha256"] = budget_sha256
    result["runtime_identity_sha256"] = identity_sha256
    result.update(runtime_context)
    assert result["passed"] is True
    result_path = root / "policy_selection_result.json"
    _write_json(result_path, result)
    report = {
        "schema": policy.POLICY_REPORT_SCHEMA,
        "run_id": "run-1",
        "candidate_sha256": candidate_sha,
        "calibration_payload_sha256": calibrated["calibration_payload_sha256"],
        "selection_result_sha256": hashlib.sha256(
            result_path.read_bytes()
        ).hexdigest(),
        "selected_policy": selected,
        "selected_policy_sha256": policy.v3._canonical_sha256(selected),
        "selection_passed": True,
        "selection_errors": [],
        "runtime_budget_payload_sha256": budget_sha256,
        "runtime_identity_sha256": identity_sha256,
        "incomplete_smoke": False,
        "source_collection_complete": True,
        "policy_selection_opponents": list(_Dataset.roles["policy_selection"]),
        "policy_selection_artifact_sha256": "9" * 64,
        "role_manifest_sha256": _Dataset.manifest_sha256,
        "policy_selection_value_rows": 1,
        "policy_selection_behavior_rows": 1,
        "preparation": preparation,
        "grid_size": len(selection["grid"]),
        "policy_gate_opened": False,
        "code_artifacts": code_artifacts,
        **runtime_context,
        "deployment_policy_value": False,
        "strength_evidence": False,
    }
    _write_json(root / "policy_selection_report.json", report)
    names = {
        policy.RUNTIME_BUDGET_NAME,
        "candidate_manifest.json",
        "policy_evaluation.json",
        "policy_selection_result.json",
        "policy_selection_report.json",
    }
    _write_json(root / "artifact_manifest.json", {
        "schema": policy.POLICY_ARTIFACT_SCHEMA,
        "run_id": "run-1",
        "candidate_sha256": candidate_sha,
        "runtime_budget_payload_sha256": budget_sha256,
        "runtime_identity_sha256": identity_sha256,
        **runtime_context,
        "policy_gate_opened": False,
        "deployment_policy_value": False,
        "strength_evidence": False,
        "files": {
            name: {
                "bytes": (root / name).stat().st_size,
                "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest(),
            }
            for name in names
        },
    })


def _rewrite_evidence_artifacts(
    root: Path, *, evaluation: dict, result: dict
) -> None:
    candidate_sha = hashlib.sha256(
        (root / "candidate_manifest.json").read_bytes()
    ).hexdigest()
    selected = evaluation.get("selected_policy")
    selected_sha = (
        policy.v3._canonical_sha256(selected)
        if isinstance(selected, dict) else None
    )
    result["candidate_sha256"] = candidate_sha
    result["evaluation_report_sha256"] = policy.v3._canonical_sha256(evaluation)
    result["selected_policy_sha256"] = selected_sha
    _write_json(root / "policy_evaluation.json", evaluation)
    _write_json(root / "policy_selection_result.json", result)
    report_path = root / "policy_selection_report.json"
    report = json.loads(report_path.read_text())
    report["candidate_sha256"] = candidate_sha
    report["selected_policy"] = selected
    report["selected_policy_sha256"] = selected_sha
    report["selection_passed"] = result["passed"]
    report["selection_errors"] = result["errors"]
    report["selection_result_sha256"] = hashlib.sha256(
        (root / "policy_selection_result.json").read_bytes()
    ).hexdigest()
    _write_json(report_path, report)
    artifact_path = root / "artifact_manifest.json"
    artifact = json.loads(artifact_path.read_text())
    artifact["candidate_sha256"] = candidate_sha
    for name in artifact["files"]:
        artifact["files"][name] = {
            "bytes": (root / name).stat().st_size,
            "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest(),
        }
    _write_json(artifact_path, artifact)


def test_policy_artifact_verifier_binds_v4_policy_and_calibration(
    tmp_path: Path,
) -> None:
    calibrated = policy._validated_calibrated_ensemble(
        _calibrated(), dataset=_Dataset(), run_id="run-1", formal=True
    )
    root = tmp_path / "policy"
    _policy_artifacts(root, calibrated)

    verified = policy.verify_policy_artifacts(
        root,
        calibrated=calibrated,
        dataset=_Dataset(),
        run_id="run-1",
        formal=True,
    )

    assert verified["selection_passed"] is True
    assert verified["selected_policy"] == _selected_policy()
    assert set(verified) == {
        "root",
        "candidate_sha256",
        "evaluation_sha256",
        "result_sha256",
        "artifact_manifest_sha256",
        "runtime_budget_path",
        "runtime_budget_payload_sha256",
        "runtime_identity_sha256",
        "selected_policy",
        "selected_policy_sha256",
        "selection_passed",
        "candidate_snapshot",
        "strategy_context_runtime_mode",
    }

    candidate = json.loads((root / "candidate_manifest.json").read_text())
    candidate["calibration_payload_sha256"] = "0" * 64
    _write_json(root / "candidate_manifest.json", candidate)
    with pytest.raises(ValueError, match="artifact changed"):
        policy.verify_policy_artifacts(
            root,
            calibrated=calibrated,
            dataset=_Dataset(),
            run_id="run-1",
            formal=True,
        )


def _recompute_formal_selection(
    root: Path,
    calibrated: dict,
    monkeypatch: pytest.MonkeyPatch,
    *,
    exposed_candidate_sha256: str | None = None,
) -> dict:
    rows = _prepared_rows(clusters_per_opponent=4, decisions_per_cluster=2)

    def prepare(raw_rows, loaded, *, batch_size, device):
        assert raw_rows == [{"protected_selection_value": True}]
        assert loaded is calibrated
        assert batch_size == 8
        assert str(device) == "cpu"
        return copy.deepcopy(rows), _formal_preparation(rows)

    monkeypatch.setattr(policy, "prepare_policy_rows", prepare)
    if exposed_candidate_sha256 is None:
        exposed_candidate_sha256 = hashlib.sha256(
            (root / "candidate_manifest.json").read_bytes()
        ).hexdigest()
    return policy.recompute_and_verify_formal_policy_selection(
        root,
        calibrated=calibrated,
        dataset=_Dataset(exposed_candidate_sha256),
        run_id="run-1",
        device="cpu",
        batch_size=8,
    )


def test_formal_policy_replay_reopens_selection_and_rebuilds_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calibrated = policy._validated_calibrated_ensemble(
        _calibrated(), dataset=_Dataset(), run_id="run-1", formal=True
    )
    root = tmp_path / "replay"
    _policy_artifacts(root, calibrated)

    verified = _recompute_formal_selection(root, calibrated, monkeypatch)

    assert verified["selected_policy"] == _selected_policy()
    assert verified["policy_contract"]["chip_margins"] == [0.0, 25.0]
    assert verified["recomputed_evaluation_sha256"] == policy.v3._canonical_sha256(
        json.loads((root / "policy_evaluation.json").read_text())
    )


def test_formal_policy_replay_rejects_self_consistent_out_of_domain_ci(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calibrated = policy._validated_calibrated_ensemble(
        _calibrated(), dataset=_Dataset(), run_id="run-1", formal=True
    )
    root = tmp_path / "ci-999"
    _policy_artifacts(root, calibrated)
    evaluation = json.loads((root / "policy_evaluation.json").read_text())
    result = json.loads((root / "policy_selection_result.json").read_text())
    for field in (
        "match_positive_rate_cluster_bootstrap_ci",
        "match_positive_rate_opponent_stratified_cluster_ci",
        "rule_match_positive_rate_cluster_bootstrap_ci",
        "rule_match_positive_rate_opponent_stratified_cluster_ci",
    ):
        evaluation[field] = {"lower": 999.0, "mean": 999.0, "upper": 999.0}
    evaluation["match_positive_rate"] = 999.0
    evaluation["rule_match_positive_rate"] = 999.0
    for row in evaluation["by_opponent"].values():
        row["match_positive_rate"] = 999.0
        row["rule_match_positive_rate"] = 999.0
    result["summary"] = {
        key: copy.deepcopy(evaluation.get(key))
        for key in result["summary"]
    }
    _rewrite_evidence_artifacts(root, evaluation=evaluation, result=result)

    with pytest.raises(ValueError, match="domain"):
        _recompute_formal_selection(root, calibrated, monkeypatch)


@pytest.mark.parametrize(
    ("chip_margin", "message"),
    [(999_999.0, "not the grid"), (25.0, "not the grid evidence winner")],
)
def test_formal_policy_replay_rejects_forged_selected_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    chip_margin: float,
    message: str,
) -> None:
    calibrated = policy._validated_calibrated_ensemble(
        _calibrated(), dataset=_Dataset(), run_id="run-1", formal=True
    )
    root = tmp_path / f"margin-{chip_margin}"
    _policy_artifacts(root, calibrated)
    evaluation = json.loads((root / "policy_evaluation.json").read_text())
    result = json.loads((root / "policy_selection_result.json").read_text())
    evaluation["config"]["chip_margin"] = chip_margin
    evaluation["selected_policy"]["chip_margin"] = chip_margin
    _rewrite_evidence_artifacts(root, evaluation=evaluation, result=result)

    with pytest.raises(ValueError, match=message):
        _recompute_formal_selection(root, calibrated, monkeypatch)


@pytest.mark.parametrize("drift", ["bootstrap_seed", "grid"])
def test_formal_policy_replay_rejects_candidate_contract_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    calibrated = policy._validated_calibrated_ensemble(
        _calibrated(), dataset=_Dataset(), run_id="run-1", formal=True
    )
    root = tmp_path / drift
    _policy_artifacts(root, calibrated)
    candidate_path = root / "candidate_manifest.json"
    exposed_candidate_sha256 = hashlib.sha256(
        candidate_path.read_bytes()
    ).hexdigest()
    candidate = json.loads(candidate_path.read_text())
    if drift == "bootstrap_seed":
        candidate["policy_contract"]["bootstrap_seed"] += 1
    else:
        candidate["policy_contract"]["chip_margins"].append(50.0)
    _write_json(candidate_path, candidate)
    evaluation = json.loads((root / "policy_evaluation.json").read_text())
    result = json.loads((root / "policy_selection_result.json").read_text())
    _rewrite_evidence_artifacts(root, evaluation=evaluation, result=result)

    with pytest.raises(ValueError, match="selection"):
        _recompute_formal_selection(
            root,
            calibrated,
            monkeypatch,
            exposed_candidate_sha256=exposed_candidate_sha256,
        )


def test_policy_artifact_verifier_rejects_current_code_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calibrated = policy._validated_calibrated_ensemble(
        _calibrated(), dataset=_Dataset(), run_id="run-1", formal=True
    )
    root = tmp_path / "policy"
    _policy_artifacts(root, calibrated)
    monkeypatch.setattr(
        policy,
        "selector_code_artifacts",
        lambda: {"selector": {"bytes": 1, "sha256": "0" * 64}},
    )

    with pytest.raises(ValueError, match="bindings are invalid"):
        policy.verify_policy_artifacts(
            root,
            calibrated=calibrated,
            dataset=_Dataset(),
            run_id="run-1",
            formal=True,
        )


@pytest.mark.parametrize("forgery", ["missing_bootstrap", "weak_thresholds", "summary_drift"])
def test_formal_policy_verifier_recomputes_selection_evidence(
    tmp_path: Path, forgery: str,
) -> None:
    calibrated = policy._validated_calibrated_ensemble(
        _calibrated(), dataset=_Dataset(), run_id="run-1", formal=True
    )
    root = tmp_path / forgery
    _policy_artifacts(root, calibrated)
    evaluation = json.loads((root / "policy_evaluation.json").read_text())
    result = json.loads((root / "policy_selection_result.json").read_text())
    if forgery == "missing_bootstrap":
        evaluation.pop("bootstrap_contract")
        result["summary"].pop("bootstrap_contract")
    elif forgery == "weak_thresholds":
        result["thresholds"].update({
            "min_overrides": 1,
            "min_selection_clusters": 1,
            "min_override_clusters": 1,
            "min_overrides_per_opponent": 1,
            "bootstrap_samples": 100,
        })
    else:
        result["summary"]["overrides"] = (
            int(result["summary"]["overrides"]) + 1
        )
    _rewrite_evidence_artifacts(
        root,
        evaluation=copy.deepcopy(evaluation),
        result=copy.deepcopy(result),
    )

    with pytest.raises(ValueError, match="selection|bootstrap"):
        policy.verify_policy_artifacts(
            root,
            calibrated=calibrated,
            dataset=_Dataset(),
            run_id="run-1",
            formal=True,
        )


def test_formal_selector_cli_rejects_weakened_coverage_before_data_access(
    tmp_path: Path,
) -> None:
    with pytest.raises(SystemExit, match="coverage cannot be weakened"):
        policy.main([
            "--calibration-dir", str(tmp_path / "calibration"),
            "--role-manifest", str(tmp_path / "roles.json"),
            "--ledger", str(tmp_path / "ledger.json"),
            "--run-id", "formal-low-coverage",
            "--out-dir", str(tmp_path / "out"),
            "--device", "cpu",
            "--min-overrides", "11",
        ])


def test_formal_selector_checks_160_pass_boundary_before_calibration_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Dataset:
        def require_collection_boundary(self, expected_passes=160):
            assert expected_passes == 160
            raise ValueError("formal role dataset requires complete atomic boundary")

        def open_role(self, *args, **kwargs):
            raise AssertionError("policy role must remain unopened")

    monkeypatch.setattr(policy, "RoleDatasetAccess", lambda *args, **kwargs: Dataset())
    monkeypatch.setattr(
        policy,
        "load_calibrated_ensemble",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("calibration loader must not run")
        ),
    )

    with pytest.raises(SystemExit, match="complete atomic boundary"):
        policy.main([
            "--calibration-dir", str(tmp_path / "calibration"),
            "--role-manifest", str(tmp_path / "roles.json"),
            "--ledger", str(tmp_path / "ledger.json"),
            "--run-id", "formal-boundary",
            "--out-dir", str(tmp_path / "out"),
            "--device", "cpu",
        ])


def test_runtime_budget_failure_prevents_policy_selection_exposure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = _Dataset()
    monkeypatch.setattr(policy, "RoleDatasetAccess", lambda *a, **k: dataset)
    monkeypatch.setattr(
        policy, "load_calibrated_ensemble", lambda *a, **k: _calibrated()
    )
    monkeypatch.setattr(
        policy,
        "assess_preselection_runtime_budget",
        lambda *a, **k: (_ for _ in ()).throw(
            ValueError("runtime budget failed before selection")
        ),
    )
    monkeypatch.setattr(
        policy,
        "open_policy_selection",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("policy_selection must remain unopened")
        ),
    )

    with pytest.raises(SystemExit, match="runtime budget failed"):
        policy.main([
            "--calibration-dir", str(tmp_path / "calibration"),
            "--role-manifest", str(tmp_path / "roles.json"),
            "--ledger", str(tmp_path / "ledger.json"),
            "--run-id", "run-1",
            "--out-dir", str(tmp_path / "out"),
            "--device", "cpu",
        ])


def test_preselection_budget_cannot_bind_an_earlier_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = {
        "schema": "test-bundle",
        "format": "test-format",
        "member_payload_sha256": ["a" * 64],
        "calibration": {},
        "source": {},
        "export_contract": {},
    }
    raw = bundle_exporter.checked_canonical_bundle_bytes(bundle)
    artifact = runtime_budget._artifact(
        bundle_bytes=len(raw),
        bundle_sha256=hashlib.sha256(raw).hexdigest(),
        runtime_identity_sha256=(
            runtime_budget.bundle_runtime_identity_sha256(bundle)
        ),
        preselection_runtime_budget_payload_sha256="b" * 64,
        source_collection_complete=True,
        cpu_ns=[1] * runtime_budget.MEASURED_REPEATS,
        wall_ns=[2] * runtime_budget.MEASURED_REPEATS,
        warmups_completed=runtime_budget.WARMUP_ROUNDS,
        errors=[],
    )
    monkeypatch.setattr(
        bundle_exporter,
        "build_preselection_bundle_payload",
        lambda **_kwargs: bundle,
    )
    monkeypatch.setattr(
        runtime_budget,
        "measure_bundle_runtime_budget_subprocess",
        lambda *_args, **_kwargs: artifact,
    )

    with pytest.raises(ValueError, match="unexpectedly binds"):
        policy.assess_preselection_runtime_budget(
            {}, dataset=_Dataset(), run_id="run-1", formal=True
        )

    path = tmp_path / policy.RUNTIME_BUDGET_NAME
    _write_json(path, artifact)
    with pytest.raises(ValueError, match="unexpectedly binds"):
        REAL_VERIFY_PRESELECTION_RUNTIME_BUDGET(
            path,
            calibrated={},
            dataset=_Dataset(),
            run_id="run-1",
            formal=True,
        )


def test_v4_selection_result_summary_binds_bootstrap_contract() -> None:
    selection = _selection(_prepared_rows())
    evaluation = policy.policy_evaluation(selection, incomplete_smoke=False)
    phase = {
        "schema": evidence.POLICY_SELECTION_PHASE_SCHEMA_V4,
        "run_id": "selection-v4",
        "candidate_sha256": "a" * 64,
        "role_manifest_sha256": "b" * 64,
        "policy_selection_artifact_sha256": "c" * 64,
        "calibration_payload_sha256": "d" * 64,
    }
    result = evidence.build_policy_selection_result(
        phase,
        evaluation,
        thresholds={
            "min_overrides": 1,
            "min_selection_clusters": 1,
            "min_override_clusters": 1,
            "min_overrides_per_opponent": 1,
            "min_override_hand_mean": 0.0,
            "bootstrap_samples": 100,
        },
        contract="v4",
    )
    assert result["summary"]["bootstrap_contract"] == evaluation[
        "bootstrap_contract"
    ]
