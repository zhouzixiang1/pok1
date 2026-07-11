from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
from typing import Callable

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import evaluate_v4_native_strength_verdict as verdict  # noqa: E402
import summarize_v4_native_ablations as ablations  # noqa: E402
from v4_native_strength_runtime import native_strength_runtime_contract  # noqa: E402


OPPONENTS = tuple(
    (f"opp_{index}", f"/snapshots/opp_{index}") for index in range(4)
)
SEEDS = tuple(10_000 + 100 * index for index in range(10))
OPPONENT_SEED_STRIDE = 10_000_000
BOT_SEED_BASE = 50_000
BOT_SEED_STRIDE = 10
CANDIDATE_PATH = "/snapshots/candidate"
CANDIDATE_DIGEST = "a" * 64


ValueFn = Callable[[int, int, int], int]


@pytest.fixture(autouse=True)
def _stub_live_pool_plan_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep synthetic fixtures local while proving the public validator is used."""

    def validate(raw: bytes, require_snapshots: bool = True) -> dict:
        assert type(raw) is bytes
        assert require_snapshots is True
        return json.loads(raw)

    monkeypatch.setattr(
        verdict, "validate_v4_native_strength_pool_plan_bytes", validate
    )


def _ablation_contract(mode: str) -> dict:
    return {
        "schema": ablations.ABLATION_SCHEMA,
        "mode": mode,
        "candidate_env_overrides": dict(ablations.MODE_ENV_OVERRIDES[mode]),
        "opponent_env_overrides": dict(ablations.OPPONENT_ENV_OVERRIDES),
        "diagnostic_only": mode != "full",
        "eligible_as_strength_evidence": mode == "full",
        "protected_data_read": False,
        "policy_roles_opened": [],
        "deployment_policy_value": False,
        "strength_evidence": False,
    }


def _strength_contract(mode: str) -> dict:
    full = mode == "full"
    return {
        "schema": ablations.EVALUATOR_STRENGTH_SCHEMA,
        "criterion": "net_chips_after_70_hands_gt_zero",
        "requested": full,
        "execution_contract_passed": full,
        "outcome_gate_passed": full,
        "passed": full,
        "request_errors": [],
        "result_errors": [],
        "statistical_errors": [],
    }


def _native(seed: int) -> dict:
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
    return {field: 0 for field in ablations.ZERO_COUNTER_FIELDS}


def _report(mode: str, value: ValueFn) -> dict:
    rows = []
    actual_seeds = []
    for opponent_index, (opponent, opponent_path) in enumerate(OPPONENTS):
        for match_index, base_seed in enumerate(SEEDS):
            deck_seed = base_seed + opponent_index * OPPONENT_SEED_STRIDE
            bot_seed = (
                BOT_SEED_BASE
                + match_index * BOT_SEED_STRIDE
                + opponent_index * verdict.BOT_OPPONENT_SEED_STRIDE
            )
            actual_seeds.append(deck_seed)
            legs = []
            for leg_index, leg_name in enumerate(ablations.LEG_NAMES):
                net_chips = int(value(opponent_index, match_index, leg_index))
                forward = leg_name == "forward"
                legs.append(
                    {
                        "candidate": "candidate",
                        "opponent": opponent,
                        "opponent_path": opponent_path,
                        "match_idx": match_index,
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
                        "candidate_native": _native(
                            bot_seed if forward else bot_seed + 1
                        ),
                        "opponent_native": _native(
                            bot_seed + 1 if forward else bot_seed
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
                    "candidate": "candidate",
                    "opponent": opponent,
                    "opponent_path": opponent_path,
                    "match_idx": match_index,
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
    opponent_digests = [f"{index + 1:x}" * 64 for index in range(len(OPPONENTS))]
    return {
        "format": "native_tcp_evaluation_v2",
        "execution_mode": "native_tcp",
        "candidate_ablation": _ablation_contract(mode),
        "runtime_contract": native_strength_runtime_contract(),
        "candidate_path": CANDIDATE_PATH,
        "opponent_paths": [path for _, path in OPPONENTS],
        "hands_per_match": 70,
        "seeds": list(SEEDS),
        "deck_seed_scheme": "opponent_disjoint_match_blocks_v1",
        "opponent_seed_stride": OPPONENT_SEED_STRIDE,
        "actual_deck_seed_bases": sorted(actual_seeds),
        "execution_artifacts": {
            "candidate": {
                "path": CANDIDATE_PATH,
                "sha256_before": CANDIDATE_DIGEST,
                "sha256_after": CANDIDATE_DIGEST,
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
        "outcome_bootstrap_samples": 2_000,
        "outcome_bootstrap_seed": 17,
        "trace_decisions": False,
        "force": {"hand": None, "decision": None, "action": None},
        "strength_evidence": _strength_contract(mode),
        "rows": rows,
    }


def _candidate_artifact() -> dict:
    return {
        "label": "candidate",
        "source_path": "/source/candidate",
        "source_directory_sha256": CANDIDATE_DIGEST,
        "snapshot_path": CANDIDATE_PATH,
        "snapshot_directory_sha256": CANDIDATE_DIGEST,
        "native_entry": "national_bot.py",
    }


def _opponent_artifact(index: int, label: str, path: str) -> dict:
    digest = f"{index + 1:x}" * 64
    return {
        "label": label,
        "version": index + 1,
        "source_path": f"/source/{label}",
        "source_directory_sha256": digest,
        "snapshot_path": path,
        "snapshot_directory_sha256": digest,
        "native_entry": "national_bot.py",
        "completion_tag": f"national-bot-v{index + 1}",
        "tag_object": "b" * 40,
        "tag_commit": "c" * 40,
        "tag_tree_oid": "d" * 40,
        "tag_directory_sha256": digest,
        "execution_matches_completion_tag": True,
    }


def _plan() -> dict:
    plan = {
        "schema": verdict.POOL_PLAN_SCHEMA,
        "repository": {"head": "e" * 40},
        "lifecycle": {"eligible": True},
        "ratings_snapshot": {"raw_sha256": "f" * 64},
        "candidate_artifact": _candidate_artifact(),
        "opponent_artifacts": [
            _opponent_artifact(index, label, path)
            for index, (label, path) in enumerate(OPPONENTS)
        ],
        "seeds": list(SEEDS),
        "actual_deck_seed_bases": sorted(
            seed + opponent_index * OPPONENT_SEED_STRIDE
            for opponent_index in range(len(OPPONENTS))
            for seed in SEEDS
        ),
        "deck_seed_scheme": "opponent_disjoint_match_blocks_v1",
        "opponent_seed_stride": OPPONENT_SEED_STRIDE,
        "bot_seed_base": BOT_SEED_BASE,
        "bot_seed_stride": BOT_SEED_STRIDE,
        "bot_opponent_seed_stride": verdict.BOT_OPPONENT_SEED_STRIDE,
        "hands_per_leg": 70,
        "paired": True,
        "minimum_seed_blocks_per_opponent": 3,
        "workers": 4,
        "runtime_contract": native_strength_runtime_contract(),
        "bootstrap_samples": 2_000,
        "bootstrap_seed": 17,
        "selection": {"method": "test"},
        "code_artifacts": {"freeze_tool": "1" * 64},
        "protected_data_read": False,
        "policy_roles_opened": [],
        "held_out_read": False,
        "policy_selection_opened": False,
        "policy_gate_opened": False,
        "deployment_policy_value": False,
        "deployment_eligible": False,
        "strength_evidence": False,
        "native_strength_evidence": False,
        "official_exe_accepted": False,
        "formal_release_evidence": False,
    }
    plan["payload_sha256"] = verdict.pool_plan_payload_sha256(plan)
    return plan


def _encode(payload: dict) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _inputs(
    candidate_value: ValueFn | None = None,
    baseline_value: ValueFn | None = None,
) -> tuple[bytes, bytes, bytes]:
    candidate_fn = candidate_value or (lambda _o, _m, _l: 1_000)
    baseline_fn = baseline_value or (lambda _o, _m, _l: 0)
    return (
        _encode(_plan()),
        _encode(_report("full", candidate_fn)),
        _encode(_report("neural_off", baseline_fn)),
    )


def _evaluate(inputs: tuple[bytes, bytes, bytes]) -> dict:
    return verdict.evaluate_v4_native_strength_verdict(
        *inputs, bootstrap_samples=2_000, bootstrap_seed=17
    )


def test_passing_verdict_is_outcome_first_false_authority_and_raw_replay_bound() -> None:
    inputs = _inputs()
    artifact = _evaluate(inputs)

    assert artifact["primary_outcome"]["passed"] is True
    assert artifact["secondary_paired_ev"]["passed"] is True
    assert artifact["candidate_evaluator_outcome_receipt_passed"] is True
    assert artifact["development_classic_pool_verdict_passed"] is True
    secondary = artifact["secondary_paired_ev"]
    assert secondary["candidate_direct_ev_chips_per_hand"] == 14.285714286
    assert secondary["baseline_direct_ev_chips_per_hand"] == 0.0
    assert all(
        row["candidate_direct_ev_chips_per_hand"] == 14.285714286
        and row["baseline_direct_ev_chips_per_hand"] == 0.0
        for row in secondary["opponents"].values()
    )
    assert artifact["strength_evidence"] is False
    assert artifact["native_strength_evidence"] is False
    assert artifact["deployment_eligible"] is False
    assert artifact["official_exe_accepted"] is False
    assert artifact["formal_release_evidence"] is False
    assert verdict.validate_v4_native_strength_verdict(
        artifact,
        pool_plan_raw=inputs[0],
        candidate_report_raw=inputs[1],
        baseline_report_raw=inputs[2],
    ) == artifact


def test_mechanically_valid_losing_candidate_fails_primary_and_cannot_be_rescued() -> None:
    inputs = _inputs(candidate_value=lambda _o, _m, _l: -1)
    artifact = _evaluate(inputs)

    assert artifact["primary_outcome"]["ordinary_lcb_passed"] is False
    assert artifact["primary_outcome"]["stratified_lcb_passed"] is False
    assert artifact["primary_outcome"]["passed"] is False
    assert artifact["secondary_paired_ev"]["passed"] is False
    assert artifact["development_classic_pool_verdict_passed"] is False


def test_failed_candidate_outcome_receipt_cannot_be_overridden_by_passing_rows() -> None:
    plan_raw, candidate_raw, baseline_raw = _inputs()
    candidate = json.loads(candidate_raw)
    candidate["strength_evidence"].update(
        outcome_gate_passed=False,
        passed=False,
        statistical_errors=["ordinary_positive_rate_lcb_not_above_half"],
    )

    artifact = _evaluate((plan_raw, _encode(candidate), baseline_raw))

    assert artifact["primary_outcome"]["passed"] is True
    assert artifact["secondary_paired_ev"]["passed"] is True
    assert artifact["candidate_evaluator_outcome_receipt_passed"] is False
    assert artifact["development_classic_pool_verdict_passed"] is False


def test_candidate_execution_receipt_must_have_passed_cleanly() -> None:
    plan_raw, candidate_raw, baseline_raw = _inputs()
    candidate = json.loads(candidate_raw)
    candidate["strength_evidence"].update(
        execution_contract_passed=False,
        outcome_gate_passed=False,
        passed=False,
        result_errors=["row[0]:short_match"],
    )

    with pytest.raises(ValueError, match="execution receipt must have passed"):
        _evaluate((plan_raw, _encode(candidate), baseline_raw))


def test_one_positive_rate_nemesis_fails_even_when_combined_lcbs_pass() -> None:
    def candidate(opponent: int, match: int, _leg: int) -> int:
        if opponent == len(OPPONENTS) - 1:
            return 1_000 if match < 4 else -1_000
        return 1_000

    artifact = _evaluate(_inputs(candidate_value=candidate, baseline_value=lambda *_: -2_000))

    primary = artifact["primary_outcome"]
    assert primary["ordinary_lcb_passed"] is True
    assert primary["stratified_lcb_passed"] is True
    assert primary["failed_opponents"] == [OPPONENTS[-1][0]]
    assert primary["passed"] is False
    assert artifact["secondary_paired_ev"]["failed_direct_ev_opponents"] == [
        OPPONENTS[-1][0]
    ]
    assert artifact["development_classic_pool_verdict_passed"] is False


def test_paired_ev_ci_crossing_zero_fails_despite_positive_point_and_target() -> None:
    def baseline(_opponent: int, match: int, _leg: int) -> int:
        delta = -19_000 if match == 0 else 3_000
        return 1_000 - delta

    artifact = _evaluate(_inputs(baseline_value=baseline))
    secondary = artifact["secondary_paired_ev"]

    assert secondary["point_target_passed"] is True
    assert (
        secondary["ordinary_lcb_passed"] is False
        or secondary["stratified_lcb_passed"] is False
    )
    assert secondary["passed"] is False
    assert artifact["development_classic_pool_verdict_passed"] is False


def test_positive_ev_cis_below_five_chips_per_hand_fail_target() -> None:
    artifact = _evaluate(_inputs(baseline_value=lambda _o, _m, _l: 720))
    secondary = artifact["secondary_paired_ev"]

    assert secondary["ordinary_lcb_passed"] is True
    assert secondary["stratified_lcb_passed"] is True
    assert secondary["point_estimate_chips_per_hand"] == 4.0
    assert secondary["point_target_passed"] is False
    assert secondary["passed"] is False


def test_negative_per_opponent_paired_ev_is_a_nemesis_failure() -> None:
    def baseline(opponent: int, _match: int, _leg: int) -> int:
        return 1_070 if opponent == len(OPPONENTS) - 1 else -400

    artifact = _evaluate(_inputs(baseline_value=baseline))
    secondary = artifact["secondary_paired_ev"]

    assert secondary["ordinary_lcb_passed"] is True
    assert secondary["stratified_lcb_passed"] is True
    assert secondary["point_target_passed"] is True
    assert secondary["failed_direct_ev_opponents"] == []
    assert secondary["failed_delta_opponents"] == [OPPONENTS[-1][0]]
    assert secondary["opponent_nemesis_passed"] is False
    assert secondary["passed"] is False


def test_outcome_uplift_is_reported_but_does_not_replace_absolute_gate() -> None:
    artifact = _evaluate(_inputs(baseline_value=lambda _o, _m, _l: 500))

    uplift = artifact["outcome_uplift_diagnostic"]
    assert uplift["ordinary_cluster_bootstrap_ci"]["estimate"] == 0.0
    assert uplift["used_for_verdict"] is False
    assert artifact["development_classic_pool_verdict_passed"] is True


def test_negative_candidate_direct_ev_is_a_separate_nemesis_failure() -> None:
    def candidate(opponent: int, match: int, _leg: int) -> int:
        if opponent == len(OPPONENTS) - 1:
            return 1 if match < 5 else -10_000
        return 1_000

    artifact = _evaluate(
        _inputs(candidate_value=candidate, baseline_value=lambda *_: -20_000)
    )
    secondary = artifact["secondary_paired_ev"]

    assert secondary["failed_direct_ev_opponents"] == [OPPONENTS[-1][0]]
    assert secondary["failed_delta_opponents"] == []
    assert secondary["opponent_nemesis_passed"] is False
    assert secondary["passed"] is False


def test_plan_report_drift_and_wrong_modes_are_rejected() -> None:
    plan_raw, candidate_raw, baseline_raw = _inputs()
    drifted_plan = json.loads(plan_raw)
    drifted_plan["seeds"][0] += 1
    drifted_plan["payload_sha256"] = verdict.pool_plan_payload_sha256(drifted_plan)
    with pytest.raises(ValueError, match="seeds.*drifted|seeds do not"):
        _evaluate((_encode(drifted_plan), candidate_raw, baseline_raw))

    wrong_baseline = json.loads(baseline_raw)
    wrong_baseline["candidate_ablation"] = _ablation_contract("cross_hand_off")
    with pytest.raises(ValueError, match="baseline report must use neural_off"):
        _evaluate((plan_raw, candidate_raw, _encode(wrong_baseline)))


def test_resigned_statistics_identity_or_ci_cannot_bypass_raw_replay() -> None:
    inputs = _inputs()
    artifact = _evaluate(inputs)
    tampered = copy.deepcopy(artifact)
    tampered["primary_outcome"]["ordinary_cluster_bootstrap_ci"]["low"] = 0.99
    tampered["secondary_paired_ev"]["point_estimate_chips_per_hand"] = 999.0
    tampered["execution_identity"]["candidate"]["sha256"] = "f" * 64
    tampered["payload_sha256"] = verdict.verdict_payload_sha256(tampered)

    with pytest.raises(ValueError, match="does not match its raw inputs"):
        verdict.validate_v4_native_strength_verdict(
            tampered,
            pool_plan_raw=inputs[0],
            candidate_report_raw=inputs[1],
            baseline_report_raw=inputs[2],
        )


def test_duplicate_rows_and_too_few_bootstrap_samples_fail_closed() -> None:
    plan_raw, candidate_raw, baseline_raw = _inputs()
    candidate = json.loads(candidate_raw)
    candidate["rows"].append(copy.deepcopy(candidate["rows"][0]))
    with pytest.raises(ValueError, match="row count"):
        verdict.evaluate_v4_native_strength_verdict(
            plan_raw,
            _encode(candidate),
            baseline_raw,
            bootstrap_samples=2_000,
            bootstrap_seed=17,
        )
    with pytest.raises(ValueError, match="bootstrap_samples"):
        verdict.evaluate_v4_native_strength_verdict(
            plan_raw,
            candidate_raw,
            baseline_raw,
            bootstrap_samples=1_999,
            bootstrap_seed=17,
        )

    with pytest.raises(ValueError, match="differs from the frozen pool plan"):
        verdict.evaluate_v4_native_strength_verdict(
            plan_raw,
            candidate_raw,
            baseline_raw,
            bootstrap_samples=2_000,
            bootstrap_seed=18,
        )
    with pytest.raises(ValueError, match="bootstrap_samples"):
        verdict.evaluate_v4_native_strength_verdict(
            plan_raw,
            candidate_raw,
            baseline_raw,
            bootstrap_samples=verdict.MAX_BOOTSTRAP_SAMPLES + 1,
            bootstrap_seed=17,
        )


def test_atomic_cli_output_write_leaves_no_partial_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "verdict.json"
    verdict._write_text_atomic(output, '{"complete":true}\n')
    assert output.read_text(encoding="utf-8") == '{"complete":true}\n'
    assert list(tmp_path.glob(".verdict.json.*.tmp")) == []

    failed = tmp_path / "failed.json"

    def fail_replace(source, destination):
        raise OSError("publish failed")

    monkeypatch.setattr(verdict.os, "replace", fail_replace)
    with pytest.raises(OSError, match="publish failed"):
        verdict._write_text_atomic(failed, "partial")
    assert not failed.exists()
    assert list(tmp_path.glob(".failed.json.*.tmp")) == []


def test_cli_returns_two_after_publishing_a_failed_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "verdict.json"
    monkeypatch.setattr(verdict, "_read", lambda path: b"raw")
    monkeypatch.setattr(
        verdict,
        "evaluate_v4_native_strength_verdict",
        lambda *args, **kwargs: {
            "development_classic_pool_verdict_passed": False,
            "strength_evidence": False,
        },
    )

    result = verdict.main(
        [
            "--pool-plan",
            str(tmp_path / "plan.json"),
            "--candidate-report",
            str(tmp_path / "candidate.json"),
            "--baseline-report",
            str(tmp_path / "baseline.json"),
            "--output",
            str(output),
        ]
    )

    assert result == 2
    assert json.loads(output.read_text(encoding="utf-8"))[
        "development_classic_pool_verdict_passed"
    ] is False
