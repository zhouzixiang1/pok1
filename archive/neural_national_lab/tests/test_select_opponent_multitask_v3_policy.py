from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest
import torch


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import select_opponent_multitask_v3_policy as policy  # noqa: E402


class _ValueModel:
    def __init__(self, *, mean: float, q20: float) -> None:
        self.mean = mean
        self.q20 = q20

    def eval(self):
        return self

    def forward_value(self, **inputs):
        batch = inputs["state"].shape[0]
        result = {}
        for field in policy.VALUE_FIELDS:
            quantiles = torch.zeros(batch, 6, 4)
            quantiles[:, :, 2] = self.q20
            result[field] = {
                "mean": torch.full((batch, 6), self.mean),
                "quantiles": quantiles,
            }
        return result


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
        "legal_action_mask": [0, 0, 1, 1, 0, 1],
        "opponent": "national_v98",
    }


def test_value_aggregation_uses_member_uncertainty_and_offsets() -> None:
    models = [
        _ValueModel(mean=1.0, q20=0.5),
        _ValueModel(mean=3.0, q20=1.5),
    ]
    clips = {field: 10.0 for field in policy.VALUE_FIELDS}
    offsets = {field: [2.0] * 6 for field in policy.VALUE_FIELDS}

    result = policy.aggregate_value_predictions(
        models,
        [_inference_row()],
        clips=clips,
        offsets=offsets,
        lower_quantile=0.2,
        uncertainty_std_weight=1.0,
        batch_size=8,
        device="cpu",
    )

    # mean=(10+30)/2=20; lower=(5+15)/2-std(10,30)+offset(2)=2.
    assert result[0]["match_delta_vs_rule"]["mean"] == [20.0] * 6
    assert result[0]["match_delta_vs_rule"]["lower"] == [2.0] * 6


def test_response_summary_masks_illegal_action_and_sizes_risk() -> None:
    summary = policy._response_summary(
        torch.tensor([0.0, 100.0, 0.0, 0.0, 0.0]),
        torch.tensor([0.25, 0.50]),
        torch.tensor([1.0, 0.0, 1.0, 1.0, 1.0]),
        temperature=1.0,
    )

    assert summary["probabilities"]["check"] == 0.0
    assert sum(summary["probabilities"].values()) == pytest.approx(1.0)
    source = {
        "request": {
            "pot": 200,
            "my_chips": 20_000,
            "opponent_chips": 20_000,
            "my_stage_bet": 0,
        },
        "state": {"pot": 200, "my_round_bet": 0},
    }
    smaller = dict(summary, aggressive_stack_fraction=0.10)
    larger = dict(summary, aggressive_stack_fraction=0.90)
    assert policy._response_signal(larger, source, 200) < policy._response_signal(
        smaller, source, 200
    )


def test_incomplete_policy_evaluation_cannot_select_candidate() -> None:
    selected = {
        "config": {"margin": 25.0, "use_lower": True},
        "overrides": 20,
        "override_clusters": 10,
        "match_cluster_bootstrap_mean_ci": {
            "lower": 1.0, "mean": 2.0, "upper": 3.0,
        },
        "match_opponent_stratified_cluster_ci": {
            "lower": 1.0, "mean": 2.0, "upper": 3.0,
        },
        "by_opponent": {"national_v98": {"overrides": 20, "mean": 2.0}},
    }

    evaluation = policy.policy_evaluation(
        {"selected": selected, "grid": [selected], "selection_failure": None},
        incomplete_smoke=True,
    )

    assert evaluation["selected_policy"] is None
    assert evaluation["provisional_selected_policy"] == selected["config"]
    assert evaluation["source_collection_complete"] is False
    assert evaluation["deployment_policy_value"] is False
    assert evaluation["strength_evidence"] is False


class _Dataset:
    manifest_sha256 = "a" * 64
    manifest = {"source_collection_complete": False}
    outputs = {
        "cf_model_calibration.jsonl": {"sha256": "b" * 64},
        "opponent_actions_model_calibration.jsonl": {"sha256": "c" * 64},
    }


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _calibration_fixture(root: Path) -> tuple[Path, dict]:
    root.mkdir()
    checkpoint = "d" * 64
    ensemble = {
        "schema": policy.ENSEMBLE_MANIFEST_SCHEMA,
        "role_manifest_sha256": _Dataset.manifest_sha256,
        "selected_configuration": {"scale": "small", "encoder": "none"},
        "members": [{
            "seed": 101,
            "output_dir": str(root / "member"),
            "checkpoint_sha256": checkpoint,
        }],
        "source_collection_complete": False,
        "strength_evidence": False,
    }
    ensemble_path = root / "ensemble_checkpoint_manifest.json"
    _write_json(ensemble_path, ensemble)
    ensemble_sha = hashlib.sha256(ensemble_path.read_bytes()).hexdigest()
    role_contract = {
        "cf_model_calibration.jsonl": "b" * 64,
        "opponent_actions_model_calibration.jsonl": "c" * 64,
    }
    role_sha = hashlib.sha256(
        json.dumps(role_contract, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    fields = {
        field: {"offsets": [0.0] * 6} for field in policy.VALUE_FIELDS
    }
    calibration = {
        "schema": policy.ENSEMBLE_CALIBRATION_SCHEMA,
        "run_id": "run-1",
        "role_manifest_sha256": _Dataset.manifest_sha256,
        "checkpoint_sha256": ensemble_sha,
        "calibration_artifact_sha256": role_sha,
        "policy_evidence_used": False,
        "deployment_policy_value": False,
        "strength_evidence": False,
        "ensemble": {
            "manifest_sha256": ensemble_sha,
            "members": [{"seed": 101, "checkpoint_sha256": checkpoint}],
            "value_lower_aggregation": (
                "mean_member_quantile_minus_mean_value_std"
            ),
            "response_aggregation": "mean_member_logits_then_temperature",
            "lower_quantile": 0.2,
            "uncertainty_std_weight": 1.0,
        },
        "value_lower": {
            "fields": fields,
            "target_clips": {field: 2000.0 for field in policy.VALUE_FIELDS},
            "target_preprocessing": "symmetric_clip_before_residual",
        },
        "response_temperature": {"temperature": 1.0},
    }
    calibration["payload_sha256"] = policy._canonical_sha256(calibration)
    _write_json(root / "calibration.json", calibration)
    report = {
        "schema": policy.CALIBRATION_REPORT_SCHEMA,
        "run_id": "run-1",
        "role_manifest_sha256": _Dataset.manifest_sha256,
        "ensemble_manifest_sha256": ensemble_sha,
        "calibration_payload_sha256": calibration["payload_sha256"],
        "member_checkpoint_sha256": [checkpoint],
        "opened_roles": ["train", "early_stop", "model_calibration"],
        "policy_roles_opened": False,
        "deployment_policy_value": False,
        "strength_evidence": False,
        "source_collection_complete": False,
        "formal_selection": False,
        "incomplete_smoke": True,
    }
    _write_json(root / "calibration_report.json", report)
    _write_json(root / "checkpoint_authorization.json", {"run_id": "run-1"})
    names = (
        "ensemble_checkpoint_manifest.json",
        "calibration.json",
        "calibration_report.json",
        "checkpoint_authorization.json",
    )
    artifact = {
        "schema": policy.CALIBRATION_ARTIFACT_MANIFEST_SCHEMA,
        "run_id": "run-1",
        "deployment_policy_value": False,
        "strength_evidence": False,
        "files": {
            name: {
                "bytes": (root / name).stat().st_size,
                "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest(),
            }
            for name in names
        },
    }
    _write_json(root / "artifact_manifest.json", artifact)
    return root, calibration


def test_calibration_loader_checks_payload_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, calibration = _calibration_fixture(tmp_path / "calibration")
    monkeypatch.setattr(policy, "verify_members", lambda *args, **kwargs: [{
        "seed": 101,
        "checkpoint_sha256": "d" * 64,
        "model": object(),
    }])

    loaded = policy.load_calibrated_ensemble(
        root,
        dataset=_Dataset(),
        run_id="run-1",
        device="cpu",
        formal=False,
    )
    assert loaded["calibration_payload_sha256"] == calibration["payload_sha256"]

    with pytest.raises(ValueError, match="complete ensemble"):
        policy.load_calibrated_ensemble(
            root,
            dataset=_Dataset(),
            run_id="run-1",
            device="cpu",
            formal=True,
        )

    calibration["response_temperature"]["temperature"] = 2.0
    _write_json(root / "calibration.json", calibration)
    artifact = json.loads((root / "artifact_manifest.json").read_text())
    contract = artifact["files"]["calibration.json"]
    contract["bytes"] = (root / "calibration.json").stat().st_size
    contract["sha256"] = hashlib.sha256(
        (root / "calibration.json").read_bytes()
    ).hexdigest()
    _write_json(root / "artifact_manifest.json", artifact)

    with pytest.raises(ValueError, match="bindings are invalid"):
        policy.load_calibrated_ensemble(
            root,
            dataset=_Dataset(),
            run_id="run-1",
            device="cpu",
            formal=False,
        )
