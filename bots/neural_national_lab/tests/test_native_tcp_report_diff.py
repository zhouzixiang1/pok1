from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from bots.neural_national_lab.tools.v4_native_strength_runtime import (
    native_strength_runtime_contract,
)


ROOT = Path(__file__).resolve().parents[3]
TOOL = ROOT / "bots" / "neural_national_lab" / "tools" / "native_tcp_report_diff.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("native_tcp_report_diff", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(opponent: str, match_idx: int, net: int, *, seed: int = 10) -> dict:
    return {
        "opponent": opponent,
        "match_idx": match_idx,
        "deck_seed_base": seed,
        "bot_seed_base": 1000 + match_idx,
        "hands_played": 4,
        "leg": "paired",
        "net_chips": net,
        "hand_net_chips": [net // 2, net - net // 2],
        "passed_compliance": True,
        "issues": [],
    }


def test_diff_validates_pair_metadata_and_emits_deterministic_ci() -> None:
    tool = _load_tool()
    candidate = {"rows": [_row("a", 0, 40), _row("b", 0, -10)]}
    baseline = {"rows": [_row("a", 0, 0), _row("b", 0, 0)]}

    rows = tool._diff_rows(candidate, baseline)
    first = tool._summary(
        rows,
        candidate,
        baseline,
        2,
        bootstrap_samples=500,
        bootstrap_seed=7,
    )
    second = tool._summary(
        rows,
        candidate,
        baseline,
        2,
        bootstrap_samples=500,
        bootstrap_seed=7,
    )

    assert first["combined"]["sum"] == 30
    assert first["combined"]["delta_per_hand"] == 3.75
    assert first["combined"]["bootstrap_mean_paired_chips"] == second["combined"]["bootstrap_mean_paired_chips"]
    assert first["combined"]["stratified_bootstrap_mean_paired_chips"]["groups"] == 2


def test_diff_rejects_seed_mismatch() -> None:
    tool = _load_tool()
    candidate = {"rows": [_row("a", 0, 10, seed=10)]}
    baseline = {"rows": [_row("a", 0, 0, seed=11)]}

    with pytest.raises(SystemExit, match="deck_seed_base"):
        tool._diff_rows(candidate, baseline)


def test_diff_rejects_duplicate_row_keys_instead_of_overwriting() -> None:
    tool = _load_tool()
    duplicate = _row("a", 0, 10)
    candidate = {"rows": [duplicate, dict(duplicate)]}
    baseline = {"rows": [_row("a", 0, 0)]}

    with pytest.raises(SystemExit, match="duplicate row key"):
        tool._diff_rows(candidate, baseline)


def _strength_row(opponent: str, match_idx: int, net: int, seed: int) -> dict:
    bot_seed = 2_000 + match_idx * 10

    def native(seed_value: int) -> dict:
        return {
            "returncode": 0,
            "bot_seed": seed_value,
            "decision_trace": [],
            "process_failures": 0,
            "json_response_stdout": 0,
        }

    forward_net = net // 2
    swapped_net = net - forward_net
    legs = []
    for leg_name, leg_net in (("forward", forward_net), ("swapped", swapped_net)):
        forward = leg_name == "forward"
        legs.append({
            "candidate": "candidate_v4",
            "opponent": opponent,
            "opponent_path": "opponent",
            "match_idx": match_idx,
            "deck_seed_base": seed,
            "bot_seed_base": bot_seed,
            "hands_played": 70,
            "leg": leg_name,
            "net_chips": leg_net,
            "hand_net_chips": [0] * 69 + [leg_net],
            "passed_compliance": True,
            "wrapper_used": False,
            "issues": [],
            "candidate_illegal": 0,
            "candidate_timeouts": 0,
            "opponent_illegal": 0,
            "opponent_timeouts": 0,
            "adapter_actions_candidate": 0,
            "adapter_actions_opponent": 0,
            "candidate_native": native(bot_seed if forward else bot_seed + 1),
            "opponent_native": native(bot_seed + 1 if forward else bot_seed),
        })
    return {
        "candidate": "candidate_v4",
        "opponent": opponent,
        "opponent_path": "opponent",
        "match_idx": match_idx,
        "deck_seed_base": seed,
        "bot_seed_base": bot_seed,
        "hands_played": 140,
        "leg": "paired",
        "net_chips": net,
        "hand_net_chips": [0] * 69 + [net],
        "legs": legs,
        "passed_compliance": True,
        "wrapper_used": False,
        "issues": [],
        "candidate_illegal": 0,
        "candidate_timeouts": 0,
        "opponent_illegal": 0,
        "opponent_timeouts": 0,
        "adapter_actions_candidate": 0,
        "adapter_actions_opponent": 0,
    }


def _strength_report(rows: list[dict], candidate: str) -> dict:
    seeds = [row["deck_seed_base"] for row in rows]
    return {
        "format": "native_tcp_evaluation_v2",
        "execution_mode": "native_tcp",
        "runtime_contract": native_strength_runtime_contract(),
        "candidate_ablation": {
            "schema": "opponent_multitask_v4_native_ablation_v1",
            "mode": "full",
            "candidate_env_overrides": {
                "POK_V4_DISABLE": None,
                "POK_V4_DISABLE_CROSS_HAND": None,
                "POK_V4_DISABLE_OUTCOME_UNCERTAINTY_MATCH": None,
            },
            "opponent_env_overrides": {
                "POK_V4_DISABLE": None,
                "POK_V4_DISABLE_CROSS_HAND": None,
                "POK_V4_DISABLE_OUTCOME_UNCERTAINTY_MATCH": None,
            },
            "diagnostic_only": False,
            "eligible_as_strength_evidence": True,
            "protected_data_read": False,
            "policy_roles_opened": [],
            "deployment_policy_value": False,
            "strength_evidence": False,
        },
        "candidate_path": candidate,
        "opponent_paths": ["opponent"],
        "hands_per_match": 70,
        "seeds": seeds,
        "actual_deck_seed_bases": seeds,
        "deck_seed_scheme": "opponent_disjoint_match_blocks_v1",
        "opponent_seed_stride": 10_000_000,
        "bot_seed_base": 2_000,
        "bot_seed_stride": 10,
        "outcome_bootstrap_samples": 2_000,
        "outcome_bootstrap_seed": 20_260_711,
        "workers": 1,
        "paired": True,
        "requires_native_opponents": True,
        "legacy_debug_wrapper_enabled": False,
        "wrapper_used": False,
        "execution_artifacts": {
            "candidate": {
                "path": candidate,
                "sha256_before": "a" * 64,
                "sha256_after": "a" * 64,
                "stable": True,
            },
            "opponents": [{
                "path": "opponent",
                "sha256_before": "b" * 64,
                "sha256_after": "b" * 64,
                "stable": True,
            }],
        },
        "trace_decisions": False,
        "force": {"hand": None, "decision": None, "action": None},
        "strength_evidence": {
            "schema": "native_tcp_strength_evidence_v2_outcome_first",
            "criterion": "net_chips_after_70_hands_gt_zero",
            "requested": True,
            "execution_contract_passed": True,
            "outcome_gate_passed": True,
            "passed": True,
            "request_errors": [],
            "result_errors": [],
            "statistical_errors": [],
        },
        "seventy_hand_outcomes": {
            "criterion": "net_chips_after_70_hands_gt_zero",
            "combined": {"win_rate_evidence_passed": True},
            "opponents": {},
        },
        "rows": rows,
    }


def test_strength_diff_accepts_independent_complete_reports() -> None:
    tool = _load_tool()
    candidate_rows = [
        _strength_row("a", index, net, seed)
        for index, (net, seed) in enumerate(((100, 1_000), (200, 1_080), (300, 1_160)))
    ]
    baseline_rows = [
        _strength_row("a", index, net, seed)
        for index, (net, seed) in enumerate(((20, 1_000), (40, 1_080), (60, 1_160)))
    ]

    rows = tool._diff_rows(
        _strength_report(candidate_rows, "candidate"),
        _strength_report(baseline_rows, "baseline"),
        require_strength=True,
    )
    summary = tool._summary(
        rows,
        _strength_report(candidate_rows, "candidate"),
        _strength_report(baseline_rows, "baseline"),
        2,
        bootstrap_samples=100,
        bootstrap_seed=7,
    )

    assert len(rows) == 3
    assert summary["combined"]["leave_one_block_out"]["blocks"] == 3
    assert summary["combined"]["leave_one_block_out"]["negative_estimates"] == 0


def test_chip_wins_cannot_hide_degraded_primary_outcome_direction() -> None:
    tool = _load_tool()
    seeds = [1_000 + 80 * index for index in range(12)]
    candidate_rows = [
        _strength_row("a", index, -200 if index == 11 else 2_000, seed)
        for index, seed in enumerate(seeds)
    ]
    baseline_rows = [
        _strength_row("a", index, 4, seed)
        for index, seed in enumerate(seeds)
    ]
    candidate = _strength_report(candidate_rows, "candidate")
    baseline = _strength_report(baseline_rows, "baseline")

    rows = tool._diff_rows(candidate, baseline, require_strength=True)
    summary = tool._summary(
        rows,
        candidate,
        baseline,
        2,
        bootstrap_samples=2_000,
        bootstrap_seed=17,
    )

    primary = summary["primary_outcome_diagnostic"]
    assert primary["candidate_positive_rate"] < primary["baseline_positive_rate"]
    assert primary["direction"] == "degraded"
    assert summary["combined"]["sum"] > 0
    assert summary["chip_delta_role"] == "secondary_only_cannot_override_outcome_direction"
    assert summary["diagnostic_only"] is True
    assert summary["format"] == "native_tcp_report_diff_v3_diagnostic"
    assert summary["strength_evidence"] is False
    assert summary["native_strength_evidence"] is False
    assert summary["deployment_eligible"] is False


def test_strength_diff_rejects_overlapping_deck_windows() -> None:
    tool = _load_tool()
    rows = [
        _strength_row("a", index, 0, seed)
        for index, seed in enumerate((7_000, 7_001, 7_002))
    ]
    report = _strength_report(rows, "candidate")

    with pytest.raises(SystemExit, match="overlapping_deck_windows"):
        tool._diff_rows(report, report, require_strength=True)


def test_strength_diff_rejects_legacy_compliance_only_receipt() -> None:
    tool = _load_tool()
    rows = [
        _strength_row("a", index, 20, seed)
        for index, seed in enumerate((1_000, 1_080, 1_160))
    ]
    report = _strength_report(rows, "candidate")
    report["strength_evidence"] = {"passed": True}

    with pytest.raises(SystemExit, match="schema_mismatch"):
        tool._diff_rows(report, report, require_strength=True)


def test_strength_diff_recomputes_and_rejects_all_loss_forged_receipt() -> None:
    tool = _load_tool()
    rows = [
        _strength_row("a", index, -20, seed)
        for index, seed in enumerate((1_000, 1_080, 1_160))
    ]
    report = _strength_report(rows, "candidate")

    with pytest.raises(SystemExit, match="recomputed_ordinary"):
        tool._diff_rows(report, report, require_strength=True)


def test_strength_diff_requires_reported_outcome_summary() -> None:
    tool = _load_tool()
    rows = [
        _strength_row("a", index, 20, seed)
        for index, seed in enumerate((1_000, 1_080, 1_160))
    ]
    report = _strength_report(rows, "candidate")
    report.pop("seventy_hand_outcomes")

    with pytest.raises(SystemExit, match="reported_seventy_hand"):
        tool._diff_rows(report, report, require_strength=True)


def test_report_loader_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    tool = _load_tool()
    path = tmp_path / "duplicate.json"
    path.write_text('{"format":"a","format":"b"}', encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate JSON key"):
        tool._load(path)
