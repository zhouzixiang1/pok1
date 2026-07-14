from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import sys

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import v4_runtime_budget as budget  # noqa: E402
import win_first_policy_v4 as win_first  # noqa: E402


class _Runtime:
    def __init__(self, *, failure: str | None = None) -> None:
        self.failure = failure
        self.calls = {
            "predict_values": 0,
            "predict_match_outcomes": 0,
            "predict_response": 0,
            "response_signal": 0,
        }
        self.value_inputs: list[dict] = []
        self.response_inputs: list[dict] = []

    def predict_values(self, **inputs):
        self.calls["predict_values"] += 1
        self.value_inputs.append(inputs)
        if self.failure == "exception":
            raise RuntimeError("synthetic inference failure")
        lower = (
            [-10.0] * 6
            if self.failure == "ineligible"
            else [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
        )
        if self.failure == "nan":
            lower[2] = float("nan")
        return {
            field: {
                "mean": list(lower),
                "lower": list(lower),
                "member_mean_std": [0.0] * 6,
            }
            for field in (
                "delta_vs_rule",
                "tail_delta_vs_rule",
                "match_delta_vs_rule",
            )
        }

    def predict_match_outcomes(self, **inputs):
        self.calls["predict_match_outcomes"] += 1
        assert inputs == self.value_inputs[-1]
        return win_first.aggregate_member_probabilities(
            [
                [0.1] * 6
                if self.failure == "ineligible"
                else [0.1, 0.6, 0.65, 0.7, 0.75, 0.8]
            ],
            uncertainty_std_weight=0.0,
        )

    def predict_response(self, **inputs):
        self.calls["predict_response"] += 1
        self.response_inputs.append(inputs)
        return {
            "logits": [0.0] * 5,
            "probabilities": {
                "fold": 0.2,
                "check": 0.2,
                "call": 0.2,
                "raise": 0.2,
                "allin": 0.2,
            },
            "normalized_entropy": 1.0,
            "aggressive_increment_pot_log": 0.0,
            "aggressive_stack_fraction": 0.1,
        }

    def response_signal(self, response, **inputs):
        self.calls["response_signal"] += 1
        assert set(response["probabilities"]) == {
            "fold", "check", "call", "raise", "allin"
        }
        assert set(inputs) == {
            "action", "pot", "hero_stage_bet", "hero_stack", "opponent_stack"
        }
        return 0.0


def _fast_clocks(monkeypatch: pytest.MonkeyPatch, *, cpu_step: int = 10) -> None:
    cpu = iter(range(0, 1_000_000, cpu_step))
    wall = iter(range(0, 2_000_000, 2 * cpu_step))
    monkeypatch.setattr(budget.time, "process_time_ns", lambda: next(cpu))
    monkeypatch.setattr(budget.time, "perf_counter_ns", lambda: next(wall))


def _measure(
    runtime: _Runtime,
    *,
    complete: bool = True,
    bundle_bytes: int = 1234,
) -> dict:
    return budget.measure_runtime_budget(
        runtime,
        bundle_bytes=bundle_bytes,
        bundle_sha256="a" * 64,
        source_collection_complete=complete,
    )


def test_runtime_budget_uses_immutable_full_decision_workload_and_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fast_clocks(monkeypatch)
    runtime = _Runtime()
    selector_calls = []
    original_selector = budget.win_first.select_candidate

    def tracked_selector(*args, **kwargs):
        selector_calls.append((args, kwargs))
        return original_selector(*args, **kwargs)

    monkeypatch.setattr(budget.win_first, "select_candidate", tracked_selector)
    artifact = _measure(runtime)

    rounds = budget.WARMUP_ROUNDS + budget.MEASURED_REPEATS
    assert artifact["runtime_budget_passed"] is True
    assert artifact["formal_runtime_budget_passed"] is True
    assert runtime.calls == {
        "predict_values": rounds,
        "predict_match_outcomes": rounds,
        "predict_response": 5 * rounds,
        "response_signal": 5 * rounds,
    }
    assert len(selector_calls) == rounds
    assert all(call[1]["rule_label_id"] == 0 for call in selector_calls)
    assert all(len(call[0][3]) == 5 for call in selector_calls)

    for inputs in runtime.value_inputs:
        assert len(inputs["state"]) == 81
        assert len(inputs["profile"]) == 12
        assert len(inputs["history"]) == 16
        assert all(len(row) == 24 for row in inputs["history"])
        assert len(inputs["cross_sequence"]) == 32
        assert all(len(row) == 16 for row in inputs["cross_sequence"])
        assert len(inputs["strategy_context"]) == 66
        assert inputs["rule_action"] == [1.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    assert all(
        inputs["legal_action_mask"] == [1, 1, 1, 1, 1]
        and len(inputs["hero_action"]) == 10
        for inputs in runtime.response_inputs
    )
    assert artifact["measurements"]["full_decision_cpu_ns"] == [10] * 7
    assert artifact["measurements"]["full_decision_wall_ns"] == [20] * 7
    assert budget.validate_runtime_budget_artifact(
        artifact,
        bundle_bytes=1234,
        bundle_sha256="a" * 64,
        require_formal=True,
    ) == artifact


def test_ineligible_real_predictions_still_measure_full_fixed_selector_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fast_clocks(monkeypatch)
    runtime = _Runtime(failure="ineligible")
    original_score = budget.win_first.score_candidate
    scores = []

    def tracked_score(*args, **kwargs):
        result = original_score(*args, **kwargs)
        scores.append(result)
        return result

    monkeypatch.setattr(budget.win_first, "score_candidate", tracked_score)
    artifact = _measure(runtime)

    rounds = budget.WARMUP_ROUNDS + budget.MEASURED_REPEATS
    assert artifact["runtime_budget_passed"] is True
    assert runtime.calls["predict_values"] == rounds
    assert runtime.calls["predict_match_outcomes"] == rounds
    assert len(scores) == 5 * rounds
    assert all(score is not None for score in scores)


def test_runtime_budget_constants_cannot_be_weakened_even_with_rehash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fast_clocks(monkeypatch)
    artifact = _measure(_Runtime())
    weakened = copy.deepcopy(artifact)
    weakened["limits"]["max_bundle_bytes"] += 1
    weakened["payload_sha256"] = budget.runtime_budget_payload_sha256(weakened)

    with pytest.raises(ValueError, match="contract changed"):
        budget.validate_runtime_budget_artifact(weakened)


def test_runtime_budget_self_hash_and_bundle_binding_reject_tampering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fast_clocks(monkeypatch)
    artifact = _measure(_Runtime())
    tampered = copy.deepcopy(artifact)
    tampered["bundle"]["bytes"] = 1

    with pytest.raises(ValueError, match="self-hash changed"):
        budget.validate_runtime_budget_artifact(tampered)
    with pytest.raises(ValueError, match="byte count changed"):
        budget.validate_runtime_budget_artifact(artifact, bundle_bytes=999)


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("exception", "synthetic inference failure"),
        ("nan", "non-finite"),
    ],
)
def test_runtime_exception_or_nan_is_a_hashed_failure(
    failure: str,
    message: str,
) -> None:
    artifact = _measure(_Runtime(failure=failure))

    assert artifact["runtime_budget_passed"] is False
    assert artifact["formal_runtime_budget_passed"] is False
    assert artifact["measurements_complete"] is False
    assert message in artifact["errors"][0]
    assert budget.validate_runtime_budget_artifact(artifact) == artifact


def test_cpu_timeout_fails_and_wall_time_is_diagnostic_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cpu_values = iter([0, budget.MAX_FULL_DECISION_CPU_NS + 1])
    wall_values = iter([0, budget.MAX_FULL_DECISION_CPU_NS * 100])
    monkeypatch.setattr(
        budget.time, "process_time_ns", lambda: next(cpu_values)
    )
    monkeypatch.setattr(
        budget.time, "perf_counter_ns", lambda: next(wall_values)
    )

    artifact = _measure(_Runtime())

    assert artifact["measurements"]["full_decision_cpu_ns"] == [
        budget.MAX_FULL_DECISION_CPU_NS + 1
    ]
    assert artifact["measurements"]["full_decision_wall_ns"] == [
        budget.MAX_FULL_DECISION_CPU_NS * 100
    ]
    assert "exceeds immutable limit" in artifact["errors"][0]
    assert artifact["runtime_budget_passed"] is False


def test_large_bundle_fails_before_any_inference() -> None:
    runtime = _Runtime()
    artifact = _measure(
        runtime,
        bundle_bytes=budget.MAX_BUNDLE_BYTES + 1,
    )

    assert artifact["runtime_budget_passed"] is False
    assert artifact["formal_runtime_budget_passed"] is False
    assert runtime.calls == {
        "predict_values": 0,
        "predict_match_outcomes": 0,
        "predict_response": 0,
        "response_signal": 0,
    }


def test_incomplete_collection_can_never_be_formal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fast_clocks(monkeypatch)
    artifact = _measure(_Runtime(), complete=False)

    assert artifact["runtime_budget_passed"] is True
    assert artifact["source_collection_complete"] is False
    assert artifact["formal_runtime_budget_passed"] is False
    with pytest.raises(ValueError, match="not formal-eligible"):
        budget.validate_runtime_budget_artifact(artifact, require_formal=True)


def test_wall_diagnostic_does_not_decide_eligibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cpu = iter(value for _ in range(7) for value in (0, 1))
    huge = budget.MAX_FULL_DECISION_CPU_NS * 10
    wall = iter(value for _ in range(7) for value in (0, huge))
    monkeypatch.setattr(budget.time, "process_time_ns", lambda: next(cpu))
    monkeypatch.setattr(budget.time, "perf_counter_ns", lambda: next(wall))

    artifact = _measure(_Runtime())

    assert artifact["measurements"]["max_full_decision_cpu_ns"] == 1
    assert artifact["measurements"]["max_full_decision_wall_ns"] == huge
    assert artifact["runtime_budget_passed"] is True


def _identity_only_bundle(path: Path) -> Path:
    payload = {
        "schema": "not-a-runtime-bundle",
        "format": "not-a-runtime-format",
        "calibration": {},
        "source": {},
        "export_contract": {},
        "member_payload_sha256": ["b" * 64],
    }
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    return path


def _assert_worker_imported_local_runtime(artifact: dict) -> None:
    assert artifact["runtime_budget_passed"] is False
    assert artifact["measurements_complete"] is False
    assert artifact["errors"] == [
        "ValueError: unsupported v4 stdlib ensemble format"
    ]
    assert "subprocess failed" not in artifact["errors"][0]


def test_isolated_subprocess_loads_modules_from_tools_directory(
    tmp_path: Path,
) -> None:
    bundle = _identity_only_bundle(tmp_path / "bundle.json")

    artifact = budget.measure_bundle_runtime_budget_subprocess(
        bundle,
        source_collection_complete=False,
    )

    _assert_worker_imported_local_runtime(artifact)


def test_isolated_subprocess_preserves_exact_bound_formal_failure(
    tmp_path: Path,
) -> None:
    bundle = _identity_only_bundle(tmp_path / "bundle.json")

    artifact = budget.measure_bundle_runtime_budget_subprocess(
        bundle,
        source_collection_complete=True,
    )

    _assert_worker_imported_local_runtime(artifact)
    assert artifact["source_collection_complete"] is True
    assert artifact["formal_runtime_budget_passed"] is False
    assert not artifact["errors"][0].startswith(
        "invalid runtime budget subprocess result"
    )
    assert budget.validate_runtime_budget_artifact(
        artifact, require_formal=False
    ) == artifact
    with pytest.raises(ValueError, match="not formal-eligible"):
        budget.validate_runtime_budget_artifact(
            artifact, require_formal=True
        )


def test_isolated_subprocess_loads_modules_from_candidate_directory(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    for name in (
        "v4_runtime_budget.py",
        "win_first_policy_v4.py",
        "feature_spec.py",
        "match_outcome_calibration.py",
        "opponent_multitask_runtime_v3.py",
        "opponent_multitask_runtime_v4.py",
        "opponent_multitask_ensemble_runtime_v3.py",
        "opponent_multitask_ensemble_runtime_v4.py",
    ):
        shutil.copy2(TOOLS / name, candidate / name)
    bundle = _identity_only_bundle(candidate / "v4_ensemble_bundle.json")

    artifact = budget.measure_bundle_runtime_budget_subprocess(
        bundle,
        source_collection_complete=False,
        worker_script=candidate / "v4_runtime_budget.py",
    )

    _assert_worker_imported_local_runtime(artifact)
