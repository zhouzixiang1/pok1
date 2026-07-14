from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[3]
TOOL = (
    ROOT
    / "bots"
    / "neural_national_lab"
    / "tools"
    / "native_tcp_evaluate.py"
)


def _load_tool():
    spec = importlib.util.spec_from_file_location("native_tcp_evaluate", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_direct_cli_help_resolves_repository_imports() -> None:
    result = subprocess.run(
        [sys.executable, str(TOOL), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _args(**overrides):
    values = {
        "seeds": "",
        "seed_base": 1_000,
        "seed_stride": None,
        "matches": 3,
        "hands": 70,
        "paired": True,
        "allow_generated_opponent_entry": False,
        "bot_seed_base": 2_000,
        "bot_seed_stride": 10,
        "workers": 4,
        "force_hand": None,
        "force_decision": None,
        "force_action": None,
        "trace_decisions": False,
        "opponent_seed_stride": 10_000_000,
        "candidate_ablation": "full",
        "strength_evidence": False,
        "outcome_bootstrap_samples": 20_000,
        "outcome_bootstrap_seed": 20_260_712,
        "timeout_sec": 90.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_default_seed_plan_uses_nonoverlapping_match_blocks() -> None:
    tool = _load_tool()
    args = _args()
    seeds = tool._seeds(args)

    assert seeds == [1_000, 1_080, 1_160]
    actual = [
        tool._opponent_deck_seed(seed, opponent, args.opponent_seed_stride)
        for opponent in range(2)
        for seed in seeds
    ]
    assert tool._seed_window_overlaps(actual, hands=70) == []
    assert tool._strength_request_errors(
        args, seeds, opponent_count=2
    ) == []


@pytest.mark.parametrize(
    ("mode", "enabled_env", "diagnostic_only"),
    (
        ("full", None, False),
        ("neural_off", "POK_V4_DISABLE", True),
        ("cross_hand_off", "POK_V4_DISABLE_CROSS_HAND", True),
        (
            "outcome_uncertainty_match_off",
            "POK_V4_DISABLE_OUTCOME_UNCERTAINTY_MATCH",
            True,
        ),
    ),
)
def test_candidate_ablation_contract_is_exact(
    mode: str, enabled_env: str | None, diagnostic_only: bool
) -> None:
    tool = _load_tool()
    candidate_env = {
        "POK_V4_DISABLE": None,
        "POK_V4_DISABLE_CROSS_HAND": None,
        "POK_V4_DISABLE_OUTCOME_UNCERTAINTY_MATCH": None,
    }
    if enabled_env is not None:
        candidate_env[enabled_env] = "1"
    expected = {
        "schema": "opponent_multitask_v4_native_ablation_v1",
        "mode": mode,
        "candidate_env_overrides": candidate_env,
        "opponent_env_overrides": {
            "POK_V4_DISABLE": None,
            "POK_V4_DISABLE_CROSS_HAND": None,
            "POK_V4_DISABLE_OUTCOME_UNCERTAINTY_MATCH": None,
        },
        "diagnostic_only": diagnostic_only,
        "eligible_as_strength_evidence": not diagnostic_only,
        "protected_data_read": False,
        "policy_roles_opened": [],
        "deployment_policy_value": False,
        "strength_evidence": False,
    }

    contract = tool._candidate_ablation_contract(mode)

    assert contract == expected
    assert tool._candidate_ablation_contract_errors(contract) == []


def test_candidate_ablation_overrides_clear_parent_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = _load_tool()
    for name in tool.CANDIDATE_ABLATION_ENV_NAMES:
        monkeypatch.setenv(name, "parent-value")
    contract = tool._candidate_ablation_contract("cross_hand_off")

    candidate_environment = os.environ.copy()
    for name, value in contract["candidate_env_overrides"].items():
        if value is None:
            candidate_environment.pop(name, None)
        else:
            candidate_environment[name] = value
    opponent_environment = os.environ.copy()
    for name, value in contract["opponent_env_overrides"].items():
        if value is None:
            opponent_environment.pop(name, None)
        else:
            opponent_environment[name] = value

    assert candidate_environment.get("POK_V4_DISABLE_CROSS_HAND") == "1"
    assert "POK_V4_DISABLE" not in candidate_environment
    assert (
        "POK_V4_DISABLE_OUTCOME_UNCERTAINTY_MATCH"
        not in candidate_environment
    )
    assert all(
        name not in opponent_environment
        for name in tool.CANDIDATE_ABLATION_ENV_NAMES
    )
    assert all(
        os.environ[name] == "parent-value"
        for name in tool.CANDIDATE_ABLATION_ENV_NAMES
    )


def test_process_overrides_clear_parent_force_trace_and_legacy_ablations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = _load_tool()
    inherited = (
        *tool.EVALUATION_CONTROL_ENV_NAMES,
        *tool.LEGACY_NEURAL_ABLATION_ENV_NAMES,
        *tool.CANDIDATE_ABLATION_ENV_NAMES,
    )
    for name in inherited:
        monkeypatch.setenv(name, "parent-value")
    for name in tool.RUNTIME_ENVIRONMENT_OVERRIDES:
        monkeypatch.setenv(name, "parent-contamination")
    args = _args(trace_decisions=False)
    contract = tool._candidate_ablation_contract("cross_hand_off")

    candidate = tool._native_process_env_overrides(
        args, contract, candidate=True
    )
    opponent = tool._native_process_env_overrides(
        args, contract, candidate=False
    )

    assert candidate["POK_V4_DISABLE_CROSS_HAND"] == "1"
    assert all(
        candidate[name] is None
        for name in inherited
        if name != "POK_V4_DISABLE_CROSS_HAND"
    )
    assert all(opponent[name] is None for name in inherited)
    assert {
        name: candidate[name] for name in tool.RUNTIME_ENVIRONMENT_OVERRIDES
    } == tool.RUNTIME_ENVIRONMENT_OVERRIDES
    assert {
        name: opponent[name] for name in tool.RUNTIME_ENVIRONMENT_OVERRIDES
    } == tool.RUNTIME_ENVIRONMENT_OVERRIDES
    assert all(os.environ[name] == "parent-value" for name in inherited)


def test_process_overrides_bind_explicit_force_and_trace() -> None:
    tool = _load_tool()
    args = _args(
        trace_decisions=True,
        force_hand=3,
        force_decision=4,
        force_action=-1,
    )
    contract = tool._candidate_ablation_contract("full")

    for candidate in (True, False):
        overrides = tool._native_process_env_overrides(
            args, contract, candidate=candidate
        )
        assert overrides["POK_TRACE_DECISIONS"] == "1"
        assert overrides["POK_FORCE_HAND"] == "3"
        assert overrides["POK_FORCE_DECISION"] == "4"
        assert overrides["POK_FORCE_ACTION"] == "-1"


def test_atomic_report_write_publishes_complete_json_and_cleans_temp(
    tmp_path: Path,
) -> None:
    tool = _load_tool()
    output = tmp_path / "report.json"

    tool._atomic_write_json(output, {"complete": True, "value": 7})

    assert json.loads(output.read_text(encoding="utf-8")) == {
        "complete": True,
        "value": 7,
    }
    assert list(tmp_path.glob(".report.json.tmp-*")) == []


def test_atomic_report_write_failure_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tool = _load_tool()
    output = tmp_path / "report.json"

    def fail_replace(source, destination):
        raise OSError("publish failed")

    monkeypatch.setattr(tool.os, "replace", fail_replace)
    with pytest.raises(OSError, match="publish failed"):
        tool._atomic_write_json(output, {"complete": True})

    assert not output.exists()
    assert list(tmp_path.glob(".report.json.tmp-*")) == []


def test_candidate_ablation_is_applied_only_to_candidate_in_both_seats(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = _load_tool()
    candidate = tmp_path / "candidate"
    opponent = tmp_path / "opponent"
    candidate.mkdir()
    opponent.mkdir()
    calls: list[dict] = []

    async def fake_pair(bot_a, bot_b, hands, **kwargs):
        label_a = Path(bot_a).name
        label_b = Path(bot_b).name
        calls.append({"bot_a": label_a, "bot_b": label_b, **kwargs})

        def player() -> dict:
            return {
                "illegal_actions": 0,
                "timeouts": 0,
                "adapter": {"actions_sent": 0},
                "native": {},
                "runtime_telemetry": {},
            }

        return {
            "bot_a": label_a,
            "bot_b": label_b,
            "bot_seed_base": kwargs.get("bot_seed_base"),
            "hands_played": hands,
            "net_chips_a": 10,
            "net_chips_b": -10,
            "settlements": [{"earnings": [10, -10]} for _ in range(hands)],
            "passed_compliance": True,
            "wrapper_used": False,
            "issues": [],
            "per_player": {label_a: player(), label_b: player()},
        }

    monkeypatch.setattr(tool, "run_native_tcp_pair", fake_pair)
    args = _args(
        candidate=str(candidate),
        opponent=[str(opponent)],
        candidate_ablation="outcome_uncertainty_match_off",
        hands=1,
        matches=1,
        seed_base=100,
        workers=1,
        timeout_sec=1.0,
        print_rows=False,
        trace_decisions=False,
        outcome_bootstrap_samples=10,
        outcome_bootstrap_seed=7,
    )

    payload = asyncio.run(tool._run(args))

    candidate_env = tool._native_process_env_overrides(
        args, payload["candidate_ablation"], candidate=True
    )
    opponent_env = tool._native_process_env_overrides(
        args, payload["candidate_ablation"], candidate=False
    )
    assert [(call["bot_a"], call["bot_b"]) for call in calls] == [
        ("candidate", "opponent"),
        ("opponent", "candidate"),
    ]
    assert calls[0]["bot_a_env_overrides"] == candidate_env
    assert calls[0]["bot_b_env_overrides"] == opponent_env
    assert calls[1]["bot_a_env_overrides"] == opponent_env
    assert calls[1]["bot_b_env_overrides"] == candidate_env
    assert all(call["sanitize_parent_environment"] is True for call in calls)
    assert payload["runtime_contract"] == tool.native_strength_runtime_contract(1.0)


def test_unknown_candidate_ablation_is_rejected_by_contract_and_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = _load_tool()
    with pytest.raises(ValueError, match="unknown candidate ablation mode"):
        tool._candidate_ablation_contract("unknown")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(TOOL),
            "--candidate",
            "candidate",
            "--opponent",
            "opponent",
            "--candidate-ablation",
            "unknown",
        ],
    )
    with pytest.raises(SystemExit) as exc_info:
        tool.main()
    assert exc_info.value.code == 2


def test_non_full_candidate_ablation_rejects_strength_and_legacy_wrapper() -> None:
    tool = _load_tool()
    strength_args = _args(
        candidate_ablation="neural_off", strength_evidence=True
    )
    strength_errors = tool._strength_request_errors(
        strength_args, tool._seeds(strength_args), opponent_count=1
    )
    assert "candidate_ablation_is_diagnostic_only" in strength_errors

    wrapper_errors = tool._candidate_ablation_request_errors(
        _args(
            candidate_ablation="cross_hand_off",
            allow_generated_opponent_entry=True,
        )
    )
    assert wrapper_errors == ["candidate_ablation_requires_native_opponents"]


def test_strength_request_rejects_overlapping_explicit_seeds() -> None:
    tool = _load_tool()
    args = _args(seeds="7000,7001,7002")
    seeds = tool._seeds(args)

    errors = tool._strength_request_errors(args, seeds, opponent_count=1)

    assert any(error.startswith("overlapping_deck_windows:") for error in errors)


def test_strength_request_rejects_bot_seed_window_overlap() -> None:
    tool = _load_tool()
    args = _args(bot_seed_stride=1)

    errors = tool._strength_request_errors(
        args, tool._seeds(args), opponent_count=1
    )

    assert "per_player_bot_seed_window_overlap" in errors


def test_strength_request_rejects_tiny_outcome_bootstrap() -> None:
    tool = _load_tool()
    args = _args(outcome_bootstrap_samples=1_999)

    errors = tool._strength_request_errors(
        args, tool._seeds(args), opponent_count=1
    )

    assert "outcome_bootstrap_samples_must_be_at_least_2000" in errors


def test_strength_request_rejects_nonfrozen_match_timeout() -> None:
    tool = _load_tool()
    args = _args(timeout_sec=1.0)

    errors = tool._strength_request_errors(
        args, tool._seeds(args), opponent_count=1
    )

    assert "match_timeout_must_equal_frozen_default" in errors

    trace_args = _args(trace_decisions=True)
    trace_errors = tool._strength_request_errors(
        trace_args, tool._seeds(trace_args), opponent_count=1
    )
    assert "decision_trace_forbidden" in trace_errors


def _outcome_payload(
    *, ordinary_low: float, stratified_low: float, opponent_rates: dict[str, float]
) -> dict:
    passed = bool(
        ordinary_low > 0.5
        and stratified_low > 0.5
        and all(rate >= 0.5 for rate in opponent_rates.values())
    )
    return {
        "seventy_hand_outcomes": {
            "criterion": "net_chips_after_70_hands_gt_zero",
            "combined": {
                "cluster_bootstrap_positive_rate_ci": {"low": ordinary_low},
                "opponent_stratified_cluster_bootstrap_positive_rate_ci": {
                    "low": stratified_low
                },
                "win_rate_evidence_passed": passed,
            },
            "opponents": {
                name: {"positive_rate": rate}
                for name, rate in opponent_rates.items()
            },
        }
    }


def test_strength_outcome_gate_requires_both_lcbs_and_each_opponent() -> None:
    tool = _load_tool()

    assert tool._strength_outcome_errors(
        _outcome_payload(
            ordinary_low=0.51,
            stratified_low=0.52,
            opponent_rates={"a": 0.5, "b": 0.75},
        )
    ) == []

    errors = tool._strength_outcome_errors(
        _outcome_payload(
            ordinary_low=0.5,
            stratified_low=0.49,
            opponent_rates={"a": 0.49, "b": 1.0},
        )
    )
    assert "ordinary_positive_rate_lcb_not_above_half" in errors
    assert "opponent_stratified_positive_rate_lcb_not_above_half" in errors
    assert "opponent:a:positive_rate_below_half" in errors


def test_mechanically_compliant_all_loss_summary_fails_strength_outcome() -> None:
    tool = _load_tool()
    errors = tool._strength_outcome_errors(
        _outcome_payload(
            ordinary_low=0.0,
            stratified_low=0.0,
            opponent_rates={"a": 0.0},
        )
    )

    assert errors
    assert all("missing" not in error for error in errors)

    evidence = tool._strength_evidence_payload(
        requested=True,
        request_errors=[],
        result_errors=[],
        payload=_outcome_payload(
            ordinary_low=0.0,
            stratified_low=0.0,
            opponent_rates={"a": 0.0},
        ),
    )
    assert evidence == {
        "schema": "native_tcp_strength_evidence_v2_outcome_first",
        "criterion": "net_chips_after_70_hands_gt_zero",
        "requested": True,
        "execution_contract_passed": True,
        "outcome_gate_passed": False,
        "passed": False,
        "request_errors": [],
        "result_errors": [],
        "statistical_errors": errors,
    }


def test_unrequested_strength_evidence_is_explicitly_false() -> None:
    tool = _load_tool()
    evidence = tool._strength_evidence_payload(
        requested=False,
        request_errors=[],
        result_errors=[],
        payload={},
    )

    assert evidence["requested"] is False
    assert evidence["execution_contract_passed"] is False
    assert evidence["outcome_gate_passed"] is False
    assert evidence["passed"] is False
    assert evidence["statistical_errors"] == []


def test_strength_result_rejects_short_or_noncompliant_rows() -> None:
    tool = _load_tool()
    payload = {
        "format": "native_tcp_evaluation_v2",
        "execution_mode": "native_tcp",
        "candidate_ablation": tool._candidate_ablation_contract("full"),
        "runtime_contract": tool.native_strength_runtime_contract(),
        "paired": True,
        "requires_native_opponents": True,
        "legacy_debug_wrapper_enabled": False,
        "wrapper_used": False,
        "execution_artifacts": {
            "candidate": {
                "path": "candidate",
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
        "rows": [{
            "leg": "paired",
            "hands_played": 138,
            "hand_net_chips": [0] * 69,
            "passed_compliance": False,
            "wrapper_used": True,
            "issues": ["timeout"],
            "candidate_illegal": 1,
            "candidate_timeouts": 1,
            "opponent_illegal": 0,
            "opponent_timeouts": 0,
            "adapter_actions_candidate": 0,
            "adapter_actions_opponent": 0,
            "deck_seed_base": 1_000,
            "legs": [],
        }],
    }

    errors = tool._strength_result_errors(
        payload, expected_rows=1, hands_per_leg=70
    )

    assert "row[0]:short_match" in errors
    assert "row[0]:incomplete_hand_vector" in errors
    assert "row[0]:compliance_failed" in errors
    assert "row[0]:wrapper_used" in errors
    assert "row[0]:candidate_illegal" in errors


def _paired_outcome_row(
    opponent: str, match_idx: int, forward: int, swapped: int
) -> dict:
    def leg(name: str, value: int) -> dict:
        return {
            "opponent": opponent,
            "match_idx": match_idx,
            "leg": name,
            "hands_played": 70,
            "net_chips": value,
            "passed_compliance": True,
            "issues": [],
        }

    return {
        "opponent": opponent,
        "match_idx": match_idx,
        "leg": "paired",
        "hands_played": 140,
        "net_chips": forward + swapped,
        "passed_compliance": True,
        "issues": [],
        "candidate_illegal": 0,
        "candidate_timeouts": 0,
        "opponent_illegal": 0,
        "opponent_timeouts": 0,
        "adapter_actions_candidate": 0,
        "adapter_actions_opponent": 0,
        "legs": [leg("forward", forward), leg("swapped", swapped)],
    }


def test_summary_prioritizes_complete_70_hand_match_outcomes() -> None:
    tool = _load_tool()
    rows = [
        _paired_outcome_row("a", 0, 100, -50),
        _paired_outcome_row("a", 1, 200, 10),
        _paired_outcome_row("b", 0, -10, -20),
        _paired_outcome_row("b", 1, 30, 40),
    ]

    first = tool._summary(
        rows, outcome_bootstrap_samples=500, outcome_bootstrap_seed=7
    )
    second = tool._summary(
        rows, outcome_bootstrap_samples=500, outcome_bootstrap_seed=7
    )
    primary = first["seventy_hand_outcomes"]

    assert primary["priority"] == 1
    assert primary["criterion"] == "net_chips_after_70_hands_gt_zero"
    assert primary["combined"]["matches_70_hand"] == 8
    assert primary["combined"]["wins"] == 5
    assert primary["combined"]["losses"] == 3
    assert primary["combined"]["positive_rate"] == 0.625
    assert primary["combined"]["mean_net_chips_per_match"] == 37.5
    assert primary["opponents"]["a"]["positive_rate"] == 0.75
    assert primary["opponents"]["b"]["positive_rate"] == 0.5
    assert first["combined"]["unit"] == "paired_seed_block"
    assert (
        primary["combined"]["cluster_bootstrap_positive_rate_ci"]
        == second["seventy_hand_outcomes"]["combined"][
            "cluster_bootstrap_positive_rate_ci"
        ]
    )
