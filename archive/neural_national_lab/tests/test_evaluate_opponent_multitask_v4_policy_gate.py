from __future__ import annotations

from pathlib import Path
import sys

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import evaluate_opponent_multitask_v4_policy_gate as gate  # noqa: E402
from match_outcome_schema import (  # noqa: E402
    MATCH_OUTCOME_ESTIMAND,
    MATCH_OUTCOME_SCHEMA,
    POSITIVE_OUTCOME_RULE,
)
import policy_role_evidence as evidence  # noqa: E402
import win_first_policy_v4 as win_first  # noqa: E402


def _policy() -> dict:
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


def _rows(*, negative_second: bool = False) -> list[dict]:
    rows = []
    outcomes = win_first.aggregate_member_probabilities(
        [[0.1, 0.1, 0.2, 0.8, 0.1, 0.1]],
        uncertainty_std_weight=0.0,
    )
    for opponent_index, opponent in enumerate(("national_v57", "national_v66")):
        for cluster in range(4):
            for decision in range(2):
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
                    "decision": {"decision": decision},
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


def _phase() -> dict:
    return {
        "schema": evidence.POLICY_GATE_PHASE_SCHEMA_V4,
        "run_id": "gate-v4",
        "candidate_sha256": "a" * 64,
        "role_manifest_sha256": "b" * 64,
        "policy_gate_artifact_sha256": "c" * 64,
        "selection_result_sha256": "d" * 64,
        "calibration_payload_sha256": "e" * 64,
        "deployment_policy_value": False,
        "strength_evidence": False,
    }


def test_fixed_v4_gate_uses_shared_selector_without_policy_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    shared = gate.select_win_first_candidate

    def tracked(row, selected_policy):
        calls.append((row, selected_policy))
        return shared(row, selected_policy)

    monkeypatch.setattr(gate, "select_win_first_candidate", tracked)
    evaluation = gate.evaluate_fixed_policy(
        _rows(),
        _policy(),
        bootstrap_samples=100,
        bootstrap_seed=13,
    )
    result = evidence.build_policy_gate_result(
        _phase(), evaluation, contract="v4"
    )

    assert calls
    assert evaluation["schema"] == gate.GATE_EVALUATION_SCHEMA
    assert evaluation["config"] == _policy()
    assert evaluation["selected_policy"] == _policy()
    assert evaluation["policy_search_performed"] is False
    assert evaluation["bootstrap_contract"] == {
        "schema": evidence.BOOTSTRAP_CONTRACT_SCHEMA,
        "samples": 100,
        "seed": 13,
        "observed_70_hand_match_clusters": True,
        "ordinary": True,
        "opponent_stratified": True,
    }
    assert evaluation["code_artifacts"] == gate.gate_code_artifacts()
    assert evaluation["overrides"] == 16
    assert evaluation["override_clusters"] == 8
    assert result["schema"] == evidence.POLICY_GATE_RESULT_SCHEMA_V4
    assert result["passed"] is True
    assert result["native_candidate_build_authorized"] is True
    assert result["calibration_payload_sha256"] == "e" * 64
    assert result["summary"]["bootstrap_contract"] == evaluation[
        "bootstrap_contract"
    ]
    assert result["deployment_policy_value"] is False
    assert result["strength_evidence"] is False


def test_fixed_v4_gate_rejects_negative_observed_opponent() -> None:
    evaluation = gate.evaluate_fixed_policy(
        _rows(negative_second=True),
        _policy(),
        bootstrap_samples=100,
        bootstrap_seed=17,
    )
    result = evidence.build_policy_gate_result(
        _phase(), evaluation, contract="v4"
    )

    assert result["passed"] is False
    assert result["native_candidate_build_authorized"] is False
    assert "national_v66:negative_mean" in result["errors"]


def test_gate_probability_domain_binds_rule_rate_and_uplift() -> None:
    evaluation = gate.evaluate_fixed_policy(
        _rows(), _policy(), bootstrap_samples=100, bootstrap_seed=13
    )
    evaluation["rule_match_positive_rate"] = 0.5
    with pytest.raises(ValueError, match="uplift is inconsistent"):
        gate.validate_gate_probability_domain(evaluation)

    evaluation = gate.evaluate_fixed_policy(
        _rows(), _policy(), bootstrap_samples=100, bootstrap_seed=13
    )
    opponent = next(iter(evaluation["by_opponent"]))
    evaluation["by_opponent"][opponent]["rule_match_positive_rate"] = 0.5
    with pytest.raises(ValueError, match="opponent probability"):
        gate.validate_gate_probability_domain(evaluation)


def test_fixed_v4_gate_rejects_policy_substitution_or_threshold_drift() -> None:
    substituted = _policy()
    substituted["national_v141_threshold"] = 0.9
    with pytest.raises(ValueError, match="unknown or missing"):
        gate.evaluate_fixed_policy(
            _rows(),
            substituted,
            bootstrap_samples=10,
            bootstrap_seed=19,
        )

    weakened = _policy()
    weakened["min_positive_probability_lcb"] = 0.55
    with pytest.raises(ValueError, match="safety floors are fixed"):
        gate.evaluate_fixed_policy(
            _rows(),
            weakened,
            bootstrap_samples=10,
            bootstrap_seed=21,
        )

    evaluation = gate.evaluate_fixed_policy(
        _rows(),
        _policy(),
        bootstrap_samples=10,
        bootstrap_seed=23,
    )
    evaluation["config"] = {**_policy(), "chip_margin": 999.0}
    with pytest.raises(ValueError, match="invalid offline policy gate"):
        evidence.build_policy_gate_result(
            _phase(), evaluation, contract="v4"
        )


class _Dataset:
    run_id = "run-v4"
    manifest_sha256 = "b" * 64

    def __init__(self) -> None:
        self.calls = []

    def open_role(self, role: str, **kwargs) -> dict:
        self.calls.append((role, kwargs))
        return {
            "artifact_sha256": "c" * 64,
            "prerequisite_sha256": "d" * 64,
            "prerequisite_calibration_payload_sha256": "e" * 64,
            "opponents": ["national_v66"],
            "value": [{"role": role}],
            "behavior": [{"role": role}],
        }


def test_v4_evidence_phases_use_independent_contract() -> None:
    dataset = _Dataset()
    selection = evidence.open_policy_selection(
        dataset,
        candidate_sha256="a" * 64,
        calibration_payload_sha256="e" * 64,
        contract="v4",
    )
    gate_phase = evidence.open_policy_gate(
        dataset,
        candidate_sha256="a" * 64,
        selection_result_path=Path("selection-v4.json"),
        contract="v4",
    )

    assert selection["schema"] == evidence.POLICY_SELECTION_PHASE_SCHEMA_V4
    assert gate_phase["schema"] == evidence.POLICY_GATE_PHASE_SCHEMA_V4
    assert dataset.calls == [
        ("policy_selection", {"candidate_sha256": "a" * 64}),
        (
            "policy_gate",
            {
                "candidate_sha256": "a" * 64,
                "prerequisite_report": Path("selection-v4.json"),
                "prerequisite_schema": (
                    evidence.POLICY_SELECTION_RESULT_SCHEMA_V4
                ),
                "prerequisite_offline_estimand": (
                    evidence.POLICY_OFFLINE_ESTIMAND_V4
                ),
            },
        ),
    ]


def test_v4_evidence_rejects_negative_chip_ci_floor() -> None:
    evaluation = gate.evaluate_fixed_policy(
        _rows(),
        _policy(),
        bootstrap_samples=10,
        bootstrap_seed=29,
    )

    with pytest.raises(ValueError, match="CI floors cannot be negative"):
        evidence.build_policy_gate_result(
            _phase(),
            evaluation,
            thresholds={
                "min_cluster_ci_lower": -1.0,
                "min_opponent_stratified_ci_lower": -1.0,
            },
            contract="v4",
        )


def test_v4_evidence_rejects_bootstrap_contract_drift() -> None:
    evaluation = gate.evaluate_fixed_policy(
        _rows(),
        _policy(),
        bootstrap_samples=10,
        bootstrap_seed=31,
    )
    evaluation["bootstrap_contract"]["ordinary"] = False

    with pytest.raises(ValueError, match="bootstrap contract changed"):
        evidence.build_policy_gate_result(
            _phase(), evaluation, contract="v4"
        )


def test_formal_gate_cli_rejects_weakened_coverage_before_data_access(
    tmp_path: Path,
) -> None:
    with pytest.raises(SystemExit, match="coverage cannot be weakened"):
        gate.main([
            "--calibration-dir", str(tmp_path / "calibration"),
            "--policy-dir", str(tmp_path / "policy"),
            "--bundle", str(tmp_path / "bundle.json"),
            "--role-manifest", str(tmp_path / "roles.json"),
            "--ledger", str(tmp_path / "ledger.json"),
            "--run-id", "formal-low-coverage",
            "--out-dir", str(tmp_path / "out"),
            "--device", "cpu",
            "--bootstrap-samples", "1999",
        ])


def test_formal_gate_checks_160_pass_boundary_before_calibration_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Dataset:
        def require_collection_boundary(self, expected_passes=160):
            assert expected_passes == 160
            raise ValueError("formal role dataset requires complete atomic boundary")

        def open_role(self, *args, **kwargs):
            raise AssertionError("policy_gate role must remain unopened")

    monkeypatch.setattr(gate, "RoleDatasetAccess", lambda *args, **kwargs: Dataset())
    monkeypatch.setattr(
        gate,
        "load_calibrated_ensemble",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("calibration loader must not run")
        ),
    )

    with pytest.raises(SystemExit, match="complete atomic boundary"):
        gate.main([
            "--calibration-dir", str(tmp_path / "calibration"),
            "--policy-dir", str(tmp_path / "policy"),
            "--bundle", str(tmp_path / "bundle.json"),
            "--role-manifest", str(tmp_path / "roles.json"),
            "--ledger", str(tmp_path / "ledger.json"),
            "--run-id", "formal-gate-boundary",
            "--out-dir", str(tmp_path / "out"),
            "--device", "cpu",
        ])


def test_gate_rejects_policy_code_drift_before_opening_gate_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Dataset:
        manifest = {
            "source_collection_complete": True,
            "source_completed_passes": 160,
            "source_requested_passes": 160,
        }

        def require_collection_boundary(self, expected_passes=160):
            assert expected_passes == 160
            return {"source_completed_passes": 160}

        def open_role(self, *args, **kwargs):
            raise AssertionError("policy_gate role must remain unopened")

    dataset = Dataset()
    monkeypatch.setattr(gate, "RoleDatasetAccess", lambda *args, **kwargs: dataset)
    monkeypatch.setattr(gate, "load_calibrated_ensemble", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        gate, "_validated_calibrated_ensemble", lambda value, **kwargs: value
    )
    monkeypatch.setattr(
        gate,
        "recompute_and_verify_formal_policy_selection",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ValueError("v4 policy code artifacts changed")
        ),
    )

    with pytest.raises(SystemExit, match="code artifacts changed"):
        gate.main([
            "--calibration-dir", str(tmp_path / "calibration"),
            "--policy-dir", str(tmp_path / "policy"),
            "--bundle", str(tmp_path / "bundle.json"),
            "--role-manifest", str(tmp_path / "roles.json"),
            "--ledger", str(tmp_path / "ledger.json"),
            "--run-id", "gate-code-drift",
            "--out-dir", str(tmp_path / "out"),
            "--device", "cpu",
        ])


def test_gate_rejects_bundle_drift_before_opening_gate_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Dataset:
        manifest = {
            "source_collection_complete": True,
            "source_completed_passes": 160,
            "source_requested_passes": 160,
        }
        manifest_sha256 = "b" * 64

        def require_collection_boundary(self, expected_passes=160):
            return {"source_completed_passes": 160}

        def open_role(self, *args, **kwargs):
            raise AssertionError("policy_gate role must remain unopened")

    dataset = Dataset()
    monkeypatch.setattr(gate, "RoleDatasetAccess", lambda *args, **kwargs: dataset)
    monkeypatch.setattr(gate, "load_calibrated_ensemble", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        gate, "_validated_calibrated_ensemble", lambda value, **kwargs: value
    )
    monkeypatch.setattr(
        gate,
        "recompute_and_verify_formal_policy_selection",
        lambda *args, **kwargs: {
        "selected_policy": _policy(),
        },
    )
    monkeypatch.setattr(
        gate, "build_verified_bundle_payload", lambda **kwargs: {"expected": True}
    )

    def reject_bundle(path, expected):
        assert expected == {"expected": True}
        raise ValueError("bundle differs from deterministic exporter output")

    monkeypatch.setattr(gate, "verify_exact_bundle", reject_bundle)

    with pytest.raises(SystemExit, match="deterministic exporter output"):
        gate.main([
            "--calibration-dir", str(tmp_path / "calibration"),
            "--policy-dir", str(tmp_path / "policy"),
            "--bundle", str(tmp_path / "bundle.json"),
            "--role-manifest", str(tmp_path / "roles.json"),
            "--ledger", str(tmp_path / "ledger.json"),
            "--run-id", "bundle-drift",
            "--out-dir", str(tmp_path / "out"),
            "--device", "cpu",
        ])
