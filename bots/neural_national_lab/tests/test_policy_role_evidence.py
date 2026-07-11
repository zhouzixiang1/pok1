from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import policy_role_evidence as evidence  # noqa: E402


CANDIDATE = "a" * 64


class _Dataset:
    run_id = "run-1"
    manifest_sha256 = "b" * 64

    def __init__(self) -> None:
        self.calls = []

    def open_role(self, role: str, **kwargs) -> dict:
        self.calls.append((role, kwargs))
        return {
            "artifact_sha256": ("c" if role == "policy_selection" else "d") * 64,
            "prerequisite_sha256": "e" * 64 if role == "policy_gate" else None,
            "prerequisite_calibration_payload_sha256": (
                "f" * 64 if role == "policy_gate" else None
            ),
            "opponents": ["national_v98" if role == "policy_selection" else "national_v57"],
            "value": [{"role": role}],
            "behavior": [{"role": role}],
        }


def _evaluation(*, mean: float = 10.0, lower: float = 1.0) -> dict:
    return {
        "offline_estimand": evidence.POLICY_OFFLINE_ESTIMAND,
        "match_outcome_estimand": evidence.MATCH_OUTCOME_ESTIMAND,
        "deployment_policy_value": False,
        "strength_evidence": False,
        "selected_policy": {"margin": 0.5, "use_lower": True},
        "overrides": 12,
        "override_clusters": 8,
        "match_cluster_bootstrap_mean_ci": {"lower": lower, "upper": 20.0},
        "match_opponent_stratified_cluster_ci": {"lower": lower, "upper": 20.0},
        "match_outcome_row_coverage": 1.0,
        "match_outcome_cluster_coverage": 1.0,
        "match_positive_rate_cluster_bootstrap_ci": {
            "lower": 0.6, "mean": 0.7, "upper": 0.8,
        },
        "match_positive_rate_opponent_stratified_cluster_ci": {
            "lower": 0.6, "mean": 0.7, "upper": 0.8,
        },
        "match_positive_uplift_cluster_bootstrap_ci": {
            "lower": 0.0, "mean": 0.1, "upper": 0.2,
        },
        "match_positive_uplift_opponent_stratified_cluster_ci": {
            "lower": 0.0, "mean": 0.1, "upper": 0.2,
        },
        "by_opponent": {"national_v98": {
            "overrides": 12,
            "mean": mean,
            "match_outcome_clusters": 8,
            "match_positive_rate": 0.7,
            "match_positive_uplift_mean": 0.1,
        }},
    }


def _gate_evaluation(*, mean: float = 10.0, lower: float = 1.0) -> dict:
    result = _evaluation(mean=mean, lower=lower)
    result.update({
        "config": result["selected_policy"],
        "policy_search_performed": False,
        "source_collection_complete": True,
    })
    return result


def test_selection_phase_opens_only_selection_role() -> None:
    dataset = _Dataset()

    phase = evidence.open_policy_selection(
        dataset,
        candidate_sha256=CANDIDATE,
        calibration_payload_sha256="f" * 64,
    )

    assert dataset.calls == [(
        "policy_selection", {"candidate_sha256": CANDIDATE}
    )]
    assert phase["policy_selection_artifact_sha256"] == "c" * 64


def test_passing_selection_result_is_bound_and_explicitly_not_strength() -> None:
    dataset = _Dataset()
    phase = evidence.open_policy_selection(
        dataset,
        candidate_sha256=CANDIDATE,
        calibration_payload_sha256="f" * 64,
    )

    result = evidence.build_policy_selection_result(phase, _evaluation())

    assert result["passed"] is True
    assert result["deployment_policy_value"] is False
    assert result["strength_evidence"] is False
    assert result["policy_gate_opened"] is False
    assert len(result["evaluation_report_sha256"]) == 64
    assert len(result["selected_policy_sha256"]) == 64


@pytest.mark.parametrize(
    ("mean", "lower", "error"),
    [
        (-1.0, 1.0, "negative_mean"),
        (10.0, 0.0, "cluster_ci_lower<=0.0"),
    ],
)
def test_selection_rejects_negative_opponent_or_nonpositive_ci(
    mean: float, lower: float, error: str
) -> None:
    dataset = _Dataset()
    phase = evidence.open_policy_selection(
        dataset,
        candidate_sha256=CANDIDATE,
        calibration_payload_sha256="f" * 64,
    )

    result = evidence.build_policy_selection_result(
        phase, _evaluation(mean=mean, lower=lower)
    )

    assert result["passed"] is False
    assert any(error in item for item in result["errors"])


def test_selection_rejects_chip_positive_policy_below_match_win_floor() -> None:
    dataset = _Dataset()
    phase = evidence.open_policy_selection(
        dataset,
        candidate_sha256=CANDIDATE,
        calibration_payload_sha256="f" * 64,
    )
    evaluation = _evaluation(mean=1_000.0, lower=500.0)
    evaluation["match_positive_rate_cluster_bootstrap_ci"]["lower"] = 0.49

    result = evidence.build_policy_selection_result(phase, evaluation)

    assert result["passed"] is False
    assert (
        "match_positive_rate_cluster_bootstrap_ci_lower<=0.5"
        in result["errors"]
    )


def test_win_first_thresholds_cannot_be_weakened() -> None:
    dataset = _Dataset()
    phase = evidence.open_policy_selection(
        dataset,
        candidate_sha256=CANDIDATE,
        calibration_payload_sha256="f" * 64,
    )

    with pytest.raises(ValueError, match="cannot be weakened"):
        evidence.build_policy_selection_result(
            phase,
            _evaluation(),
            thresholds={"min_match_positive_rate_ci_lower": 0.49},
        )


def test_selection_rejects_claim_that_offline_uplift_is_strength() -> None:
    dataset = _Dataset()
    phase = evidence.open_policy_selection(
        dataset,
        candidate_sha256=CANDIDATE,
        calibration_payload_sha256="f" * 64,
    )
    evaluation = _evaluation()
    evaluation["strength_evidence"] = True

    with pytest.raises(ValueError, match="invalid offline"):
        evidence.build_policy_selection_result(phase, evaluation)


def test_selection_rejects_unknown_threshold_override() -> None:
    dataset = _Dataset()
    phase = evidence.open_policy_selection(
        dataset,
        candidate_sha256=CANDIDATE,
        calibration_payload_sha256="f" * 64,
    )

    with pytest.raises(ValueError, match="unknown policy evidence thresholds"):
        evidence.build_policy_selection_result(
            phase, _evaluation(), thresholds={"unreviewed_gate": 0}
        )


def test_result_write_then_gate_open_preserves_candidate_binding(
    tmp_path: Path,
) -> None:
    dataset = _Dataset()
    phase = evidence.open_policy_selection(
        dataset,
        candidate_sha256=CANDIDATE,
        calibration_payload_sha256="f" * 64,
    )
    result = evidence.build_policy_selection_result(phase, _evaluation())
    path = tmp_path / "selection.json"

    digest = evidence.write_selection_result(path, result)
    gate = evidence.open_policy_gate(
        dataset,
        candidate_sha256=CANDIDATE,
        selection_result_path=path,
    )

    assert json.loads(path.read_text()) == result
    assert len(digest) == 64
    assert dataset.calls[-1] == (
        "policy_gate",
        {
            "candidate_sha256": CANDIDATE,
            "prerequisite_report": path,
            "prerequisite_schema": evidence.POLICY_SELECTION_RESULT_SCHEMA,
            "prerequisite_offline_estimand": evidence.POLICY_OFFLINE_ESTIMAND,
        },
    )
    assert gate["deployment_policy_value"] is False
    assert gate["strength_evidence"] is False


def test_policy_gate_result_binds_fixed_policy_and_authorizes_only_build(
    tmp_path: Path,
) -> None:
    dataset = _Dataset()
    selection_path = tmp_path / "selection.json"
    selection_path.write_text("{}")
    phase = evidence.open_policy_gate(
        dataset,
        candidate_sha256=CANDIDATE,
        selection_result_path=selection_path,
    )

    result = evidence.build_policy_gate_result(phase, _gate_evaluation())
    path = tmp_path / "gate.json"
    digest = evidence.write_policy_gate_result(path, result)

    assert result["passed"] is True
    assert result["native_candidate_build_authorized"] is True
    assert result["deployment_policy_value"] is False
    assert result["strength_evidence"] is False
    assert result["selection_result_sha256"] == "e" * 64
    assert result["calibration_payload_sha256"] == "f" * 64
    assert len(result["selected_policy_sha256"]) == 64
    assert len(digest) == 64


def test_failed_policy_gate_does_not_authorize_native_candidate() -> None:
    dataset = _Dataset()
    phase = evidence.open_policy_gate(
        dataset,
        candidate_sha256=CANDIDATE,
        selection_result_path=Path("selection.json"),
    )

    result = evidence.build_policy_gate_result(
        phase, _gate_evaluation(mean=-1.0)
    )

    assert result["passed"] is False
    assert result["native_candidate_build_authorized"] is False
    assert any("negative_mean" in error for error in result["errors"])


def test_policy_gate_rejects_grid_search_or_policy_substitution() -> None:
    dataset = _Dataset()
    phase = evidence.open_policy_gate(
        dataset,
        candidate_sha256=CANDIDATE,
        selection_result_path=Path("selection.json"),
    )
    searched = _gate_evaluation()
    searched["policy_search_performed"] = True
    with pytest.raises(ValueError, match="invalid offline policy gate"):
        evidence.build_policy_gate_result(phase, searched)

    substituted = _gate_evaluation()
    substituted["config"] = {"margin": 999.0}
    with pytest.raises(ValueError, match="invalid offline policy gate"):
        evidence.build_policy_gate_result(phase, substituted)
