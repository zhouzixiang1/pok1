from __future__ import annotations

import copy
from pathlib import Path
import sys

import pytest
import torch


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import export_opponent_multitask_ensemble_v3 as bundle_export  # noqa: E402
import export_opponent_multitask_v3 as member_export  # noqa: E402
import opponent_multitask_ensemble_runtime_v3 as ensemble  # noqa: E402
import opponent_multitask_model_v3 as models  # noqa: E402
from opponent_multitask_batch_v3 import collate_inference_rows  # noqa: E402
from select_opponent_multitask_v3_policy import (  # noqa: E402
    _canonical_sha256,
    _response_summary,
    aggregate_value_predictions,
)


def _value_row() -> dict:
    return {
        "encoded_context_schema": "opponent_multitask_inference_context_v3",
        "response_mode": False,
        "state": [0.1 + index / 1000.0 for index in range(81)],
        "opponent_profile": [0.05 * index for index in range(12)],
        "history": [[0.01 * index for index in range(24)]],
        "cross_hand_sequence": [[0.02 * index for index in range(15)] + [-0.1]],
        "rule_action": [0.0, 0.0, 1.0, 0.0, 0.0, 0.0],
        "strategy_context": [0.01 * (index % 10) for index in range(66)],
        "strategy_context_available": True,
        "legal_action_mask": [0, 0, 1, 1, 0, 1],
        "opponent": "national_v98",
    }


def _response_row() -> dict:
    row = _value_row()
    row.update({
        "response_mode": True,
        "hero_action_features": [
            0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.01, 0.2, 0.01, 0.99,
        ],
        "response_legal_action_mask": [1, 0, 1, 1, 1],
    })
    row.pop("rule_action")
    row.pop("strategy_context")
    return row


def _members() -> tuple[list, list[dict]]:
    models_out = []
    payloads = []
    for seed in (101, 211, 307):
        torch.manual_seed(seed)
        model = models.model_from_scale(
            "small", cross_encoder="deep_set", dropout=0.0
        ).eval()
        models_out.append(model)
        payloads.append(member_export.build_export_payload(
            model,
            {"schema": "test_checkpoint", "code_artifacts": {}},
            checkpoint_sha256=f"{seed:064x}",
        ))
    return models_out, payloads


def _calibrated(models_out: list) -> dict:
    return {
        "models": models_out,
        "calibration_payload_sha256": "b" * 64,
        "lower_quantile": 0.2,
        "uncertainty_std_weight": 1.0,
        "clips": {field: 2000.0 for field in models.VALUE_FIELDS},
        "offsets": {field: [float(index) for index in range(6)]
                    for field in models.VALUE_FIELDS},
        "response_temperature": 0.75,
    }


def _policy() -> dict:
    selected = {
        "margin": 25.0,
        "hand_weight": 0.25,
        "tail_weight": 0.25,
        "match_weight": 0.50,
        "response_weight": 0.10,
        "use_lower": True,
        "min_hand_lcb": 0.0,
    }
    return {
        "selected_policy": selected,
        "selected_policy_sha256": _canonical_sha256(selected),
        "selection_passed": True,
    }


def _bundle() -> tuple[dict, list]:
    torch_models, payloads = _members()
    payload = bundle_export.build_bundle_payload(
        payloads,
        calibrated=_calibrated(torch_models),
        policy=_policy(),
        source={"run_id": "test-run"},
    )
    return payload, torch_models


def test_three_member_calibrated_value_matches_torch_aggregation() -> None:
    payload, torch_models = _bundle()
    stdlib = ensemble.OpponentMultiTaskEnsembleRuntimeV3(payload)
    row = _value_row()

    expected = aggregate_value_predictions(
        torch_models,
        [row],
        clips={field: 2000.0 for field in models.VALUE_FIELDS},
        offsets={field: [float(index) for index in range(6)]
                 for field in models.VALUE_FIELDS},
        lower_quantile=0.2,
        uncertainty_std_weight=1.0,
        batch_size=1,
        device="cpu",
    )[0]
    actual = stdlib.predict_values(
        state=row["state"],
        profile=row["opponent_profile"],
        history=row["history"],
        cross_sequence=row["cross_hand_sequence"],
        rule_action=row["rule_action"],
        strategy_context=row["strategy_context"],
    )

    for field in models.VALUE_FIELDS:
        assert actual[field]["mean"] == pytest.approx(
            expected[field]["mean"], abs=1.0e-3
        )
        assert actual[field]["lower"] == pytest.approx(
            expected[field]["lower"], abs=1.0e-3
        )


def test_three_member_temperature_response_matches_torch() -> None:
    payload, torch_models = _bundle()
    stdlib = ensemble.OpponentMultiTaskEnsembleRuntimeV3(payload)
    row = _response_row()
    batch = collate_inference_rows([row], response=True)
    with torch.no_grad():
        outputs = [
            model.forward_response(**batch["inputs"]) for model in torch_models
        ]
    logits = torch.stack([output["logits"] for output in outputs]).mean(0)[0]
    sizes = torch.stack([output["size"] for output in outputs]).mean(0)[0]
    expected = _response_summary(
        logits,
        sizes,
        batch["inputs"]["legal_action_mask"][0],
        temperature=0.75,
    )
    actual = stdlib.predict_response(
        state=row["state"],
        profile=row["opponent_profile"],
        history=row["history"],
        cross_sequence=row["cross_hand_sequence"],
        hero_action=row["hero_action_features"],
        legal_action_mask=row["response_legal_action_mask"],
    )

    assert actual["probabilities"] == pytest.approx(
        expected["probabilities"], abs=1.0e-6
    )
    assert actual["normalized_entropy"] == pytest.approx(
        expected["normalized_entropy"], abs=1.0e-6
    )
    assert actual["aggressive_stack_fraction"] == pytest.approx(
        expected["aggressive_stack_fraction"], abs=1.0e-6
    )


def test_selected_policy_scores_lcb_and_respects_margin() -> None:
    payload, _ = _bundle()
    stdlib = ensemble.OpponentMultiTaskEnsembleRuntimeV3(payload)
    values = {
        field: {
            "mean": [0.0] * 6,
            "lower": [0.0, 0.0, 100.0, 50.0, -20.0, 10.0],
        }
        for field in models.VALUE_FIELDS
    }

    selected = stdlib.select_candidate(values, [
        {"label_id": 2, "action": 200, "response_signal": 0.0},
        {"label_id": 3, "action": 400, "response_signal": 100.0},
    ])

    assert selected is not None
    assert selected["label_id"] == 2
    assert selected["prediction"]["score"] == 100.0

    stdlib.policy["margin"] = 100.0
    assert stdlib.select_candidate(values, [
        {"label_id": 2, "action": 200, "response_signal": 0.0}
    ]) is None


def test_bundle_rejects_member_or_policy_binding_drift() -> None:
    payload, _ = _bundle()
    malformed = copy.deepcopy(payload)
    malformed["members"][0]["weights"]["state_encoder.0.bias"][0] += 1.0
    with pytest.raises(ValueError, match="member payload changed"):
        ensemble.OpponentMultiTaskEnsembleRuntimeV3(malformed)

    malformed = copy.deepcopy(payload)
    malformed["selected_policy"]["hand_weight"] = 0.5
    with pytest.raises(ValueError, match="sum to one"):
        ensemble.OpponentMultiTaskEnsembleRuntimeV3(malformed)

    malformed = copy.deepcopy(payload)
    malformed["source"]["policy_selection_passed"] = False
    with pytest.raises(ValueError, match="status is inconsistent"):
        ensemble.OpponentMultiTaskEnsembleRuntimeV3(malformed)


def test_calibration_only_bundle_cannot_select_action(tmp_path: Path) -> None:
    torch_models, payloads = _members()
    payload = bundle_export.build_bundle_payload(
        payloads,
        calibrated=_calibrated(torch_models),
        policy={
            "selected_policy": None,
            "selected_policy_sha256": None,
            "selection_passed": False,
        },
        source={"run_id": "smoke"},
    )
    path = tmp_path / "bundle.json"
    member_export.write_export(path, payload)
    stdlib = ensemble.OpponentMultiTaskEnsembleRuntimeV3.load(path)

    assert stdlib is not None
    assert stdlib.policy is None
    assert stdlib.select_candidate({}, []) is None
