from __future__ import annotations

from pathlib import Path
import sys


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import evaluate_opponent_multitask_v3_policy_gate as gate  # noqa: E402
import policy_role_evidence as evidence  # noqa: E402


def _policy() -> dict:
    return {
        "margin": 25.0,
        "hand_weight": 0.25,
        "tail_weight": 0.25,
        "match_weight": 0.50,
        "response_weight": 0.0,
        "use_lower": True,
        "min_hand_lcb": 0.0,
    }


def _rows(*, negative_second: bool = False) -> list[dict]:
    rows = []
    for opponent_index, opponent in enumerate(("national_v57", "national_v66")):
        for cluster in range(4):
            for decision in range(2):
                observed = -100.0 if negative_second and opponent_index else 100.0
                values = {
                    field: {
                        "mean": [0.0, 0.0, 100.0, 0.0, 0.0, 0.0],
                        "lower": [0.0, 0.0, 100.0, 0.0, 0.0, 0.0],
                    }
                    for field in (
                        "delta_vs_rule", "tail_delta_vs_rule",
                        "match_delta_vs_rule",
                    )
                }
                rows.append({
                    "source_row_index": len(rows),
                    "opponent": opponent,
                    "cluster": f"{opponent}|{cluster}",
                    "rule_id": 1,
                    "sampling_weight": 1.0,
                    "decision": {"decision": decision},
                    "values": values,
                    "candidates": [{
                        "label_id": 2,
                        "label": "raise_half",
                        "action": 200,
                        "response_signal": 0.0,
                        "hand_delta": observed,
                        "tail_delta": observed,
                        "match_delta": observed,
                    }],
                })
    return rows


def _phase() -> dict:
    return {
        "schema": "policy_gate_phase_v1",
        "run_id": "gate-run",
        "candidate_sha256": "a" * 64,
        "role_manifest_sha256": "b" * 64,
        "policy_gate_artifact_sha256": "c" * 64,
        "selection_result_sha256": "d" * 64,
        "deployment_policy_value": False,
        "strength_evidence": False,
    }


def test_fixed_policy_gate_performs_no_grid_search_and_can_pass() -> None:
    evaluation = gate.evaluate_fixed_policy(
        _rows(),
        _policy(),
        bootstrap_samples=100,
        bootstrap_seed=7,
    )
    result = evidence.build_policy_gate_result(_phase(), evaluation)

    assert evaluation["config"] == _policy()
    assert evaluation["selected_policy"] == _policy()
    assert evaluation["policy_search_performed"] is False
    assert evaluation["overrides"] == 16
    assert evaluation["override_clusters"] == 8
    assert result["passed"] is True
    assert result["native_candidate_build_authorized"] is True
    assert result["strength_evidence"] is False


def test_fixed_policy_gate_rejects_negative_opponent_mean() -> None:
    evaluation = gate.evaluate_fixed_policy(
        _rows(negative_second=True),
        _policy(),
        bootstrap_samples=100,
        bootstrap_seed=11,
    )
    result = evidence.build_policy_gate_result(_phase(), evaluation)

    assert result["passed"] is False
    assert result["native_candidate_build_authorized"] is False
    assert "national_v66:negative_mean" in result["errors"]
