from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
from typing import Callable

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import summarize_v4_native_ablations as summary  # noqa: E402
from v4_native_strength_runtime import native_strength_runtime_contract  # noqa: E402


OPPONENTS = (("opp_a", "/bots/opp_a"), ("opp_b", "/bots/opp_b"))
SEEDS = (1_000, 1_100, 1_200)
OPPONENT_SEED_STRIDE = 10_000
BOT_SEED_BASE = 5_000
BOT_SEED_STRIDE = 10


def _ablation_contract(mode: str) -> dict:
    return {
        "schema": summary.ABLATION_SCHEMA,
        "mode": mode,
        "candidate_env_overrides": dict(summary.MODE_ENV_OVERRIDES[mode]),
        "opponent_env_overrides": dict(summary.OPPONENT_ENV_OVERRIDES),
        "diagnostic_only": mode != "full",
        "eligible_as_strength_evidence": mode == "full",
        "protected_data_read": False,
        "policy_roles_opened": [],
        "deployment_policy_value": False,
        "strength_evidence": False,
    }


def _native_process(seed: int) -> dict:
    return {
        "returncode": 0,
        "bot_seed": seed,
        "stdout_tail": "",
        "stderr_tail": "",
        "decision_trace": [],
        "process_failures": 0,
        "json_response_stdout": 0,
    }


def _zero_counters() -> dict:
    return {field: 0 for field in summary.ZERO_COUNTER_FIELDS}


def _default_value(mode: str, opponent_idx: int, match_idx: int, leg_idx: int) -> int:
    serial = opponent_idx * 6 + match_idx * 2 + leg_idx
    if mode == "full":
        return 20 if serial % 3 else -20
    if mode == "neural_off":
        return 20 if serial % 2 else -20
    if mode == "cross_hand_off":
        return 20 if serial % 4 == 0 else -20
    return 20 if serial % 3 else -20


def _report(
    mode: str,
    *,
    values: dict[tuple[str, int, str], int] | None = None,
) -> dict:
    rows = []
    actual_deck_seeds = []
    for opponent_idx, (opponent, opponent_path) in enumerate(OPPONENTS):
        for match_idx, base_seed in enumerate(SEEDS):
            deck_seed = base_seed + opponent_idx * OPPONENT_SEED_STRIDE
            bot_seed = (
                BOT_SEED_BASE
                + match_idx * BOT_SEED_STRIDE
                + opponent_idx * 100_000
            )
            actual_deck_seeds.append(deck_seed)
            legs = []
            for leg_idx, leg_name in enumerate(summary.LEG_NAMES):
                net_chips = (
                    values[(opponent, match_idx, leg_name)]
                    if values is not None
                    else _default_value(mode, opponent_idx, match_idx, leg_idx)
                )
                legs.append(
                    {
                        "candidate": "candidate_v4",
                        "opponent": opponent,
                        "opponent_path": opponent_path,
                        "match_idx": match_idx,
                        "leg": leg_name,
                        "deck_seed_base": deck_seed,
                        "bot_seed_base": bot_seed,
                        "hands_played": 70,
                        "net_chips": net_chips,
                        "hand_net_chips": [net_chips, *([0] * 69)],
                        "passed_compliance": True,
                        "wrapper_used": False,
                        "issues": [],
                        **_zero_counters(),
                        "candidate_native": _native_process(
                            bot_seed if leg_name == "forward" else bot_seed + 1
                        ),
                        "opponent_native": _native_process(
                            bot_seed + 1 if leg_name == "forward" else bot_seed
                        ),
                    }
                )
            paired_hands = [
                legs[0]["hand_net_chips"][index]
                + legs[1]["hand_net_chips"][index]
                for index in range(70)
            ]
            rows.append(
                {
                    "candidate": "candidate_v4",
                    "opponent": opponent,
                    "opponent_path": opponent_path,
                    "match_idx": match_idx,
                    "leg": "paired",
                    "deck_seed_base": deck_seed,
                    "bot_seed_base": bot_seed,
                    "hands_played": 140,
                    "net_chips": sum(leg["net_chips"] for leg in legs),
                    "hand_net_chips": paired_hands,
                    "passed_compliance": True,
                    "wrapper_used": False,
                    "issues": [],
                    **_zero_counters(),
                    "legs": legs,
                }
            )
    candidate_digest = "a" * 64
    opponent_digests = ("b" * 64, "c" * 64)
    return {
        "format": "native_tcp_evaluation_v2",
        "execution_mode": "native_tcp",
        "candidate_ablation": _ablation_contract(mode),
        "runtime_contract": native_strength_runtime_contract(),
        "candidate_path": "/bots/candidate_v4",
        "opponent_paths": [path for _, path in OPPONENTS],
        "hands_per_match": 70,
        "seeds": list(SEEDS),
        "deck_seed_scheme": "opponent_disjoint_match_blocks_v1",
        "opponent_seed_stride": OPPONENT_SEED_STRIDE,
        "actual_deck_seed_bases": sorted(actual_deck_seeds),
        "execution_artifacts": {
            "candidate": {
                "path": "/bots/candidate_v4",
                "sha256_before": candidate_digest,
                "sha256_after": candidate_digest,
                "stable": True,
            },
            "opponents": [
                {
                    "path": path,
                    "sha256_before": opponent_digests[index],
                    "sha256_after": opponent_digests[index],
                    "stable": True,
                }
                for index, (_, path) in enumerate(OPPONENTS)
            ],
        },
        "workers": 4,
        "paired": True,
        "requires_native_opponents": True,
        "legacy_debug_wrapper_enabled": False,
        "wrapper_used": False,
        "bot_seed_base": BOT_SEED_BASE,
        "bot_seed_stride": BOT_SEED_STRIDE,
        "trace_decisions": False,
        "force": {"hand": None, "decision": None, "action": None},
        "rows": rows,
        "strength_evidence": {
            "schema": "native_tcp_strength_evidence_v2_outcome_first",
            "criterion": "net_chips_after_70_hands_gt_zero",
            "requested": False,
            "execution_contract_passed": False,
            "outcome_gate_passed": False,
            "passed": False,
            "request_errors": [],
            "result_errors": [],
            "statistical_errors": [],
        },
    }


def _reports() -> dict[str, dict]:
    return {mode: _report(mode) for mode in summary.ABLATION_MODES}


def _encode(report: dict, *, allow_nan: bool = False) -> bytes:
    return (
        json.dumps(
            report,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=allow_nan,
        )
        + "\n"
    ).encode()


def _inputs(reports: dict[str, dict], modes=None):
    selected = summary.ABLATION_MODES if modes is None else modes
    return [(f"{mode}.json", _encode(reports[mode])) for mode in selected]


def _duplicate_forward_leg(report: dict) -> None:
    leg = report["rows"][0]["legs"][1]
    bot_seed = report["rows"][0]["bot_seed_base"]
    leg["leg"] = "forward"
    leg["candidate_native"]["bot_seed"] = bot_seed
    leg["opponent_native"]["bot_seed"] = bot_seed + 1


def test_happy_path_is_deterministic_self_hashed_and_input_bound() -> None:
    reports = _reports()
    inputs = _inputs(reports)

    first = summary.summarize_native_ablation_reports(
        inputs, bootstrap_samples=500, bootstrap_seed=17
    )
    second = summary.summarize_native_ablation_reports(
        list(reversed(inputs)), bootstrap_samples=500, bootstrap_seed=17
    )

    assert first == second
    assert summary.validate_summary_artifact(first, inputs) == first
    assert first["payload_sha256"] == summary.summary_payload_sha256(first)
    assert first["protected_data_read"] is False
    assert first["policy_roles_opened"] == []
    assert first["deployment_policy_value"] is False
    assert first["deployment_eligible"] is False
    assert first["strength_evidence"] is False
    assert first["native_strength_evidence"] is False
    assert first["official_exe_accepted"] is False
    assert first["formal_release_evidence"] is False
    assert first["diagnostic_only"] is True
    assert first["eligible_as_strength_evidence"] is False
    assert first["input_reports"] == [
        {
            "mode": mode,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        for mode, (_, raw) in zip(summary.ABLATION_MODES, inputs)
    ]
    for comparison in first["comparisons"].values():
        primary = comparison["primary"]
        secondary = comparison["secondary"]
        assert primary["priority"] == 1
        assert primary["clusters"] == 6
        assert primary["paired_70_hand_legs"] == 12
        assert primary["ordinary_cluster_bootstrap_ci"]["clusters"] == 6
        assert (
            primary["equal_opponent_stratified_cluster_bootstrap_ci"][
                "opponents"
            ]
            == 2
        )
        assert secondary["priority"] == 2
        assert secondary["used_for_primary_direction_or_ordering"] is False
        assert secondary["ordinary_cluster_bootstrap_ci"]["clusters"] == 6
        assert (
            secondary["equal_opponent_stratified_cluster_bootstrap_ci"][
                "opponents"
            ]
            == 2
        )


def test_missing_or_duplicate_modes_are_rejected() -> None:
    reports = _reports()
    with pytest.raises(ValueError, match="exactly 4 reports"):
        summary.summarize_native_ablation_reports(
            _inputs(reports, summary.ABLATION_MODES[:-1]), bootstrap_samples=10
        )

    duplicate_inputs = _inputs(reports, summary.ABLATION_MODES[:-1])
    duplicate_inputs.append(("duplicate.json", _encode(reports["neural_off"])))
    with pytest.raises(ValueError, match="duplicate ablation mode"):
        summary.summarize_native_ablation_reports(
            duplicate_inputs, bootstrap_samples=10
        )


def test_candidate_ablation_schema_and_environment_are_exact() -> None:
    reports = _reports()
    reports["neural_off"]["candidate_ablation"]["candidate_env_overrides"][
        "POK_V4_DISABLE"
    ] = None

    with pytest.raises(ValueError, match="candidate env does not match mode"):
        summary.summarize_native_ablation_reports(
            _inputs(reports), bootstrap_samples=10
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("deployment_eligible", True),
        ("native_strength_evidence", True),
        ("official_exe_accepted", True),
        ("protected_data_read", True),
        ("policy_roles_opened", ["policy_gate"]),
    ),
)
def test_positive_input_authority_claims_are_rejected(
    field: str, value: object
) -> None:
    reports = _reports()
    reports["neural_off"][field] = value

    with pytest.raises(ValueError, match=field):
        summary.summarize_native_ablation_reports(
            _inputs(reports), bootstrap_samples=10
        )


def test_input_strength_claim_is_rejected() -> None:
    reports = _reports()
    reports["neural_off"]["strength_evidence"].update(
        requested=True, passed=True
    )

    with pytest.raises(ValueError, match="non-full ablation must not claim"):
        summary.summarize_native_ablation_reports(
            _inputs(reports), bootstrap_samples=10
        )


def test_clean_requested_full_strength_report_is_accepted_as_diagnostic_input() -> None:
    reports = _reports()
    reports["full"]["strength_evidence"].update(
        requested=True,
        execution_contract_passed=True,
        outcome_gate_passed=True,
        passed=True,
    )

    artifact = summary.summarize_native_ablation_reports(
        _inputs(reports), bootstrap_samples=10
    )

    assert artifact["diagnostic_only"] is True
    assert artifact["eligible_as_strength_evidence"] is False
    assert artifact["strength_evidence"] is False


def test_failed_requested_full_strength_report_remains_valid_diagnostic_input() -> None:
    reports = _reports()
    reports["full"]["strength_evidence"].update(
        requested=True,
        execution_contract_passed=True,
        outcome_gate_passed=False,
        passed=False,
        statistical_errors=["ordinary_positive_rate_lcb_not_above_half"],
    )

    artifact = summary.summarize_native_ablation_reports(
        _inputs(reports), bootstrap_samples=10
    )

    assert artifact["diagnostic_only"] is True
    assert artifact["strength_evidence"] is False


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda report: report["execution_artifacts"]["candidate"].update(
                sha256_before="d" * 64,
                sha256_after="d" * 64,
            ),
            "drifted from full",
        ),
        (
            lambda report: report["rows"][0].update(deck_seed_base=9_999),
            "deck_seed_base does not match",
        ),
        (
            lambda report: report["rows"][0].update(bot_seed_base=9_999),
            "bot_seed_base does not match",
        ),
        (
            _duplicate_forward_leg,
            "duplicate 'forward' leg",
        ),
        (
            lambda report: report["rows"][0]["legs"].pop(),
            "exactly two legs",
        ),
    ],
)
def test_artifact_seed_and_leg_drift_are_rejected(
    mutation: Callable[[dict], object], message: str
) -> None:
    reports = _reports()
    mutation(reports["cross_hand_off"])

    with pytest.raises(ValueError, match=message):
        summary.summarize_native_ablation_reports(
            _inputs(reports), bootstrap_samples=10
        )


@pytest.mark.parametrize("value", (-20_000, 20_000))
def test_physical_hand_chip_boundaries_are_accepted(value: int) -> None:
    values = {
        (opponent, match_idx, leg): value
        for opponent, _ in OPPONENTS
        for match_idx in range(len(SEEDS))
        for leg in summary.LEG_NAMES
    }
    report = _report("full", values=values)

    validated = summary.validate_native_ablation_report_bytes(
        _encode(report), source="physical_boundary"
    )

    assert validated.clusters[(OPPONENTS[0][0], 0)]["legs"]["forward"][
        "hand_net_chips"
    ][0] == value
    assert report["rows"][0]["hand_net_chips"][0] == 2 * value


@pytest.mark.parametrize("value", (-20_001, 20_001))
def test_leg_hand_chip_value_outside_stack_is_rejected(value: int) -> None:
    report = _report("neural_off")
    leg = report["rows"][0]["legs"][0]
    leg["hand_net_chips"] = [value, *([0] * 69)]
    leg["net_chips"] = value

    with pytest.raises(ValueError, match=r"hand_net_chips\[0\].*exceeds 20000"):
        summary.validate_native_ablation_report_bytes(
            _encode(report), source="leg_overflow"
        )


@pytest.mark.parametrize("value", (-40_001, 40_001))
def test_paired_hand_chip_value_outside_two_seat_sum_is_rejected(
    value: int,
) -> None:
    report = _report("neural_off")
    row = report["rows"][0]
    row["hand_net_chips"] = [value, *([0] * 69)]
    row["net_chips"] = value

    with pytest.raises(ValueError, match=r"hand_net_chips\[0\].*exceeds 40000"):
        summary.validate_native_ablation_report_bytes(
            _encode(report), source="paired_overflow"
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda report: report.update(hands_per_match=69), "hands_per_match must be 70"),
        (lambda report: report.update(paired=False), "paired must be true"),
        (lambda report: report.update(workers=5), "workers must be in"),
        (lambda report: report.update(wrapper_used=True), "wrapper_used must be false"),
        (
            lambda report: report["force"].update(action=1),
            "forced actions are forbidden",
        ),
        (
            lambda report: report["rows"][0].update(passed_compliance=False),
            "passed_compliance must be true",
        ),
        (
            lambda report: report["rows"][0].update(issues=["bad"]),
            "issues must be an empty list",
        ),
        (
            lambda report: report["rows"][0].update(candidate_illegal=1),
            "candidate_illegal must be zero",
        ),
        (
            lambda report: report["rows"][0].update(candidate_timeouts=1),
            "candidate_timeouts must be zero",
        ),
        (
            lambda report: report["rows"][0].update(adapter_actions_candidate=1),
            "adapter_actions_candidate must be zero",
        ),
        (
            lambda report: report["rows"][0]["legs"][0]["candidate_native"].update(
                process_failures=1
            ),
            "process_failures must be zero",
        ),
        (
            lambda report: report["rows"][0]["legs"][0]["candidate_native"].update(
                bot_seed=123
            ),
            "bot_seed drifted",
        ),
        (
            lambda report: report["rows"][0]["legs"][0]["opponent_native"].update(
                returncode=1
            ),
            "returncode must be zero",
        ),
    ],
)
def test_non70_nonpaired_or_noncompliant_input_is_rejected(
    mutation: Callable[[dict], object], message: str
) -> None:
    reports = _reports()
    mutation(reports["neural_off"])

    with pytest.raises(ValueError, match=message):
        summary.summarize_native_ablation_reports(
            _inputs(reports), bootstrap_samples=10
        )


def test_primary_outcome_sign_is_not_overwritten_by_secondary_chips() -> None:
    keys = [
        (opponent, match_idx, leg)
        for opponent, _ in OPPONENTS
        for match_idx in range(len(SEEDS))
        for leg in summary.LEG_NAMES
    ]
    full_values = {
        key: (1 if index < 7 else -10_000) for index, key in enumerate(keys)
    }
    ablation_values = {
        key: (0 if index < 7 else 10_000) for index, key in enumerate(keys)
    }
    reports = _reports()
    reports["full"] = _report("full", values=full_values)
    reports["neural_off"] = _report("neural_off", values=ablation_values)

    artifact = summary.summarize_native_ablation_reports(
        _inputs(reports), bootstrap_samples=500, bootstrap_seed=31
    )
    comparison = artifact["comparisons"]["neural_off"]

    assert comparison["primary"]["paired_uplift_mean"] > 0
    assert comparison["primary"]["direction"] == "full_better"
    assert comparison["secondary"]["net_chips_delta_per_hand"] < 0
    assert comparison["primary"]["priority"] == 1
    assert comparison["secondary"]["priority"] == 2
    assert comparison["secondary"]["used_for_primary_direction_or_ordering"] is False


def test_overlapping_per_player_bot_seed_windows_are_rejected() -> None:
    reports = _reports()
    for report in reports.values():
        report["bot_seed_stride"] = 1
        for row in report["rows"]:
            opponent_idx = next(
                index
                for index, (name, _) in enumerate(OPPONENTS)
                if name == row["opponent"]
            )
            bot_seed = (
                BOT_SEED_BASE + row["match_idx"] + opponent_idx * 100_000
            )
            row["bot_seed_base"] = bot_seed
            for leg in row["legs"]:
                leg["bot_seed_base"] = bot_seed
                forward = leg["leg"] == "forward"
                leg["candidate_native"]["bot_seed"] = (
                    bot_seed if forward else bot_seed + 1
                )
                leg["opponent_native"]["bot_seed"] = (
                    bot_seed + 1 if forward else bot_seed
                )

    with pytest.raises(ValueError, match="bot seed windows overlap"):
        summary.summarize_native_ablation_reports(
            _inputs(reports), bootstrap_samples=10
        )


def test_summary_tampering_and_nonfinite_json_are_rejected() -> None:
    reports = _reports()
    inputs = _inputs(reports)
    artifact = summary.summarize_native_ablation_reports(
        inputs, bootstrap_samples=10
    )
    tampered = copy.deepcopy(artifact)
    tampered["comparisons"]["neural_off"]["primary"]["paired_uplift_mean"] = 1.0
    with pytest.raises(ValueError, match="self-hash changed"):
        summary.validate_summary_artifact(tampered, inputs)

    nonfinite_artifact = copy.deepcopy(artifact)
    nonfinite_artifact["comparisons"]["neural_off"]["primary"][
        "paired_uplift_mean"
    ] = float("nan")
    with pytest.raises(ValueError, match="non-finite"):
        summary.validate_summary_artifact(nonfinite_artifact, inputs)

    resigned_extra_authority = copy.deepcopy(artifact)
    resigned_extra_authority["official_release_accepted"] = True
    resigned_extra_authority["payload_sha256"] = summary.summary_payload_sha256(
        resigned_extra_authority
    )
    with pytest.raises(ValueError, match="keys differ"):
        summary.validate_summary_artifact(resigned_extra_authority, inputs)

    resigned_statistics = copy.deepcopy(artifact)
    resigned_statistics["comparisons"]["neural_off"]["primary"].update(
        paired_uplift_mean=1.0,
        direction="full_better",
    )
    resigned_statistics["payload_sha256"] = summary.summary_payload_sha256(
        resigned_statistics
    )
    with pytest.raises(ValueError, match="does not match its raw input reports"):
        summary.validate_summary_artifact(resigned_statistics, inputs)

    with pytest.raises(ValueError, match="raw native ablation reports are required"):
        summary.validate_summary_artifact(artifact)

    reports["neural_off"]["rows"][0]["net_chips"] = float("nan")
    raw_inputs = [
        (
            f"{mode}.json",
            _encode(reports[mode], allow_nan=True),
        )
        for mode in summary.ABLATION_MODES
    ]
    with pytest.raises(ValueError, match="non-finite JSON constant"):
        summary.summarize_native_ablation_reports(
            raw_inputs, bootstrap_samples=10
        )
