from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import textwrap
from types import SimpleNamespace
import sys

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import build_opponent_multitask_v3_native_candidate as builder  # noqa: E402


def _write(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifacts(tmp_path: Path, *, passed: bool = True) -> tuple[Path, Path, dict]:
    gate = tmp_path / "gate"
    gate.mkdir()
    selected = {
        "margin": 25.0,
        "hand_weight": 0.25,
        "tail_weight": 0.25,
        "match_weight": 0.5,
        "response_weight": 0.1,
        "use_lower": True,
        "min_hand_lcb": 0.0,
    }
    selected_sha = builder._canonical_sha256(selected)
    candidate_sha = "a" * 64
    selection_sha = "b" * 64
    role_manifest_sha = "d" * 64
    evaluation = {
        "schema": builder.GATE_EVALUATION_SCHEMA,
        "config": selected,
        "selected_policy": selected,
        "source_collection_complete": True,
        "policy_search_performed": False,
        "deployment_policy_value": False,
        "strength_evidence": False,
        "offline_estimand": builder.POLICY_OFFLINE_ESTIMAND,
        "match_outcome_estimand": builder.MATCH_OUTCOME_ESTIMAND,
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
        "by_opponent": {
            "national_v1": {
                "match_outcome_clusters": 8,
                "match_positive_rate": 0.7,
                "match_positive_uplift_mean": 0.1,
            },
            "national_v2": {
                "match_outcome_clusters": 8,
                "match_positive_rate": 0.7,
                "match_positive_uplift_mean": 0.1,
            },
        },
    }
    result = {
        "schema": builder.GATE_RESULT_SCHEMA,
        "run_id": "test-run",
        "passed": passed,
        "errors": [] if passed else ["negative_ci"],
        "native_candidate_build_authorized": passed,
        "candidate_sha256": candidate_sha,
        "selection_result_sha256": selection_sha,
        "role_manifest_sha256": role_manifest_sha,
        "selected_policy_sha256": selected_sha,
        "evaluation_report_sha256": builder._canonical_sha256(evaluation),
        "deployment_policy_value": False,
        "strength_evidence": False,
        "offline_estimand": builder.POLICY_OFFLINE_ESTIMAND,
        "match_outcome_estimand": builder.MATCH_OUTCOME_ESTIMAND,
        "thresholds": {
            "min_match_outcome_coverage": 1.0,
            "min_match_positive_rate_ci_lower": 0.5,
            "min_match_positive_uplift_ci_lower": 0.0,
            "min_opponent_match_positive_rate": 0.5,
        },
    }
    _write(gate / "policy_gate_evaluation.json", evaluation)
    _write(gate / "policy_gate_result.json", result)
    report = {
        "schema": builder.GATE_REPORT_SCHEMA,
        "run_id": "test-run",
        "role_manifest_sha256": role_manifest_sha,
        "gate_passed": passed,
        "gate_errors": [] if passed else ["negative_ci"],
        "native_candidate_build_authorized": passed,
        "gate_result_sha256": _sha(gate / "policy_gate_result.json"),
        "selected_policy_sha256": selected_sha,
        "candidate_sha256": candidate_sha,
        "selection_result_sha256": selection_sha,
        "source_collection_complete": True,
        "policy_gate_opponents": ["national_v1", "national_v2"],
        "deployment_policy_value": False,
        "strength_evidence": False,
    }
    _write(gate / "policy_gate_report.json", report)
    names = (
        "policy_gate_evaluation.json",
        "policy_gate_result.json",
        "policy_gate_report.json",
    )
    artifact = {
        "schema": builder.GATE_ARTIFACT_SCHEMA,
        "run_id": "test-run",
        "candidate_sha256": candidate_sha,
        "native_candidate_build_authorized": passed,
        "deployment_policy_value": False,
        "strength_evidence": False,
        "files": {
            name: {
                "bytes": (gate / name).stat().st_size,
                "sha256": _sha(gate / name),
            }
            for name in names
        },
    }
    _write(gate / "artifact_manifest.json", artifact)
    bundle = {
        "schema": builder.BUNDLE_SCHEMA,
        "format": builder.ENSEMBLE_FORMAT,
        "selected_policy": selected,
        "source": {
            "run_id": "test-run",
            "role_manifest_sha256": role_manifest_sha,
            "selected_policy_sha256": selected_sha,
            "policy_selection_passed": True,
            "source_collection_complete": True,
            "policy_candidate_sha256": candidate_sha,
            "policy_result_sha256": selection_sha,
            "deployment_policy_value": False,
            "strength_evidence": False,
        },
        "deployment_policy_value": False,
        "strength_evidence": False,
    }
    bundle_path = tmp_path / "bundle.json"
    _write(bundle_path, bundle)
    return gate, bundle_path, selected


def test_failed_policy_gate_cannot_authorize_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, bundle, selected = _artifacts(tmp_path, passed=False)
    monkeypatch.setattr(
        builder.OpponentMultiTaskEnsembleRuntimeV3,
        "load",
        lambda path: SimpleNamespace(policy=selected),
    )

    with pytest.raises(ValueError, match="does not authorize"):
        builder.verify_build_authorization(gate, bundle)


def test_builder_independently_rejects_win_first_evidence_below_floor(
    tmp_path: Path,
) -> None:
    gate, _bundle, _selected = _artifacts(tmp_path, passed=True)
    evaluation = json.loads(
        (gate / "policy_gate_evaluation.json").read_text(encoding="utf-8")
    )
    result = json.loads(
        (gate / "policy_gate_result.json").read_text(encoding="utf-8")
    )
    evaluation["match_positive_rate_cluster_bootstrap_ci"]["lower"] = 0.49

    with pytest.raises(ValueError, match="positive-rate evidence"):
        builder._verify_win_first_evidence(evaluation, result)


def test_passing_gate_builds_merged_native_candidate_without_old_version_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, bundle, selected = _artifacts(tmp_path, passed=True)
    monkeypatch.setattr(
        builder.OpponentMultiTaskEnsembleRuntimeV3,
        "load",
        lambda path: SimpleNamespace(policy=selected),
    )
    output = tmp_path / "v999_test_native_tcp"
    strategy_before = builder._directory_sha256(builder.DEFAULT_STRATEGY_DONOR)
    transport_before = builder._directory_sha256(builder.DEFAULT_TRANSPORT_DONOR)

    manifest = builder.build_candidate(
        strategy_donor=builder.DEFAULT_STRATEGY_DONOR,
        transport_donor=builder.DEFAULT_TRANSPORT_DONOR,
        bundle_path=bundle,
        gate_dir=gate,
        output=output,
    )

    assert output.is_dir()
    assert manifest["runtime_contract"]["native_tcp"] is True
    assert manifest["runtime_contract"]["adapter"] is False
    assert manifest["native_strength_evidence"] is False
    assert (output / "national_bot.py").is_file()
    assert (output / "v3_ensemble_bundle.json").is_file()
    assert (output / "V3_BUILD_MANIFEST.json").is_file()
    assert not (output / "TRACE_VERSION.md").exists()
    assert not (output / "trace_manifest.json").exists()
    native = (output / "national_bot.py").read_text(encoding="utf-8")
    assert "def _recv_messages(" in native
    assert "consume_strategy_context" in native
    assert "safe_rule_action" in native
    assert "sanitize_stage_total" in native
    assert "self.v3_policy.advise" in native
    response = (output / "opponent_response_schema.py").read_text(encoding="utf-8")
    assert "import national_validator as _VALIDATOR" in response
    assert "parents[3]" not in response
    smoke = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
                from national_bot import NativeNationalBot

                request = {
                    "my_chips": 19950,
                    "opponent_chips": 19900,
                    "my_stage_bet": 50,
                    "opponent_stage_bet": 100,
                    "pot": 150,
                    "to_call": 50,
                }
                state = {
                    "round": 0,
                    "round_bet": 100,
                    "round_raise": 100,
                    "min_raise_action": 151,
                    "my_round_bet": 50,
                    "to_call": 50,
                    "pot": 150,
                    "opponent_allin": False,
                }

                bot = NativeNationalBot("Smoke", "upper")
                assert bot.v3_policy is None
                bot._request = lambda: dict(request)
                bot.get_action = lambda req, requests: 151
                bot.consume_strategy_context = lambda: {}
                bot.reconstruct_state = lambda req: dict(state)
                bot.apply_neural_advice = lambda req, state, action: action
                bot.sanitize_action = lambda action, state, chips: 201

                class BrokenPolicy:
                    last_decision = None

                    def advise(self, *args):
                        raise RuntimeError("broken neural policy")

                bot.v3_policy = BrokenPolicy()
                assert bot._strategy_action(0) == 201

                def broken_sanitize(*args):
                    raise RuntimeError("broken sanitizer")

                bot.v3_policy = None
                bot.sanitize_action = broken_sanitize
                assert bot._strategy_action(1) == 0
            """),
        ],
        cwd=output,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert smoke.returncode == 0, smoke.stderr
    assert builder._directory_sha256(builder.DEFAULT_STRATEGY_DONOR) == strategy_before
    assert builder._directory_sha256(builder.DEFAULT_TRANSPORT_DONOR) == transport_before


def test_policy_or_bundle_hash_drift_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, bundle_path, selected = _artifacts(tmp_path, passed=True)
    payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    payload["source"]["selected_policy_sha256"] = "c" * 64
    _write(bundle_path, payload)
    monkeypatch.setattr(
        builder.OpponentMultiTaskEnsembleRuntimeV3,
        "load",
        lambda path: SimpleNamespace(policy=selected),
    )

    with pytest.raises(ValueError, match="not bound"):
        builder.verify_build_authorization(gate, bundle_path)


def test_cli_output_must_be_a_new_version_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="formal output"):
        builder._formal_output(tmp_path / "candidate")
