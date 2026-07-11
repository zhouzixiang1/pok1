"""Stdlib-only runtime for v4 value, response, and 70-hand outcome heads."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from match_outcome_calibration import (
    apply_calibration,
    validate_calibration_artifact,
)
import opponent_multitask_runtime_v3 as v3


RUNTIME_FORMAT = "opponent_multitask_stdlib_v4"
MODEL_FORMAT = "opponent_multitask_distributional_outcome_v4"
OUTCOME_HEAD_SCHEMA = "per_action_70_hand_positive_logit_v1"
MATCH_OUTCOME_SCHEMA = "national_70_hand_match_outcome_supervision_v1"
MATCH_OUTCOME_ESTIMAND = (
    "single_decision_70_hand_positive_outcome_uplift_clustered_v1"
)
POSITIVE_OUTCOME_RULE = "net_chips_after_70_hands_gt_zero"


class OpponentMultiTaskRuntimeV4:
    """Strict v4 wrapper that reuses the proven v3 shared forward path."""

    def __init__(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict) or payload.get("format") != RUNTIME_FORMAT:
            raise ValueError("unsupported v4 stdlib model format")
        metadata = payload.get("model_metadata")
        hidden = payload.get("hidden_sizes")
        weights = payload.get("weights")
        if not isinstance(metadata, dict) or not isinstance(hidden, dict):
            raise ValueError("v4 stdlib model metadata is missing")
        if not isinstance(weights, dict):
            raise ValueError("v4 stdlib model weights are missing")
        self.metadata = dict(metadata)
        raw_calibration = payload.get("outcome_calibration")
        if raw_calibration is not None:
            source = payload.get("source")
            if not isinstance(source, dict):
                raise ValueError("v4 calibrated model source is missing")
            self.outcome_calibration = validate_calibration_artifact(
                raw_calibration,
                checkpoint_sha256=source.get("checkpoint_sha256"),
                model_format=MODEL_FORMAT,
            )
        else:
            self.outcome_calibration = None
        self.hidden = {key: int(hidden.get(key, 0)) for key in v3.HIDDEN_KEYS}
        if set(hidden) != set(v3.HIDDEN_KEYS) or min(self.hidden.values()) <= 0:
            raise ValueError("v4 hidden-size contract is invalid")
        self._validate_metadata()

        outcome_shapes = self._outcome_shapes()
        outcome_keys = set(outcome_shapes)
        base_weights = {
            name: value for name, value in weights.items()
            if name not in outcome_keys
        }
        if set(weights) - set(base_weights) != outcome_keys:
            missing = sorted(outcome_keys - set(weights))
            extra = sorted(set(weights) - set(base_weights) - outcome_keys)
            raise ValueError(
                f"v4 outcome weight keys changed: missing={missing} extra={extra}"
            )
        self.outcome_weights: dict[str, Any] = {}
        outcome_parameters = 0
        for name, shape in outcome_shapes.items():
            if len(shape) == 1:
                self.outcome_weights[name] = v3._vector(
                    weights[name], size=shape[0], field=name
                )
                outcome_parameters += shape[0]
            else:
                self.outcome_weights[name] = v3._matrix(
                    weights[name], rows=shape[0], columns=shape[1], field=name
                )
                outcome_parameters += shape[0] * shape[1]
        total_parameters = int(metadata.get("parameters", -1))
        if total_parameters <= outcome_parameters:
            raise ValueError("v4 parameter count is invalid")
        base_metadata = dict(metadata)
        base_metadata["format"] = v3.MODEL_FORMAT
        base_metadata["parameters"] = total_parameters - outcome_parameters
        base_payload = dict(payload)
        base_payload.update({
            "format": v3.RUNTIME_FORMAT,
            "model_metadata": base_metadata,
            "weights": base_weights,
        })
        self.base = v3.OpponentMultiTaskRuntimeV3(base_payload)
        if (
            self.base.metadata["parameters"] + outcome_parameters
            != total_parameters
        ):
            raise ValueError("v4 parameter count changed")

    @classmethod
    def load(cls, path: str | Path) -> "OpponentMultiTaskRuntimeV4 | None":
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            return cls(payload)
        except Exception:
            return None

    def _validate_metadata(self) -> None:
        expected = {
            "format": MODEL_FORMAT,
            "parent_value_format": v3.MODEL_FORMAT,
            "match_outcome_head_schema": OUTCOME_HEAD_SCHEMA,
            "match_outcome_supervision_schema": MATCH_OUTCOME_SCHEMA,
            "match_outcome_estimand": MATCH_OUTCOME_ESTIMAND,
            "match_outcome_hands": 70,
            "match_positive_outcome_rule": POSITIVE_OUTCOME_RULE,
            "match_outcome_actions": list(v3.LABELS),
            "match_outcome_output": "uncalibrated_binary_logits",
        }
        for key, value in expected.items():
            if self.metadata.get(key) != value:
                raise ValueError(f"v4 model metadata changed: {key}")

    def _outcome_shapes(self) -> dict[str, tuple[int, ...]]:
        head = self.hidden["head_hidden"]
        latent = self.hidden["latent"]
        return {
            "match_outcome_head.0.weight": (head, latent),
            "match_outcome_head.0.bias": (head,),
            "match_outcome_head.2.weight": (len(v3.LABELS), head),
            "match_outcome_head.2.bias": (len(v3.LABELS),),
        }

    def _outcome_layer(self, prefix: str, values: list[float]) -> list[float]:
        return v3._linear(
            self.outcome_weights[f"{prefix}.weight"],
            values,
            self.outcome_weights[f"{prefix}.bias"],
        )

    def _value_latent(
        self,
        *,
        state: list[float],
        profile: list[float],
        history: list[list[float]],
        cross_sequence: list[list[float]],
        rule_action: list[float],
        strategy_context: list[float],
    ) -> list[float]:
        common, opponent, interaction = self.base._common(
            state, profile, history, cross_sequence, response=False
        )
        rule_action = v3._vector(
            rule_action, size=len(v3.LABELS), field="rule_action"
        )
        if (
            any(value not in (0.0, 1.0) for value in rule_action)
            or sum(rule_action) != 1.0
        ):
            raise ValueError("rule_action must be one-hot")
        strategy_context = v3._vector(
            strategy_context, size=v3.STRATEGY_DIM, field="strategy_context"
        )
        strategy = self.base._two_layer_relu(
            strategy_context,
            first="strategy_encoder.0",
            second="strategy_encoder.2",
        )
        return self.base._two_layer_relu(
            v3._concat(common, opponent, interaction, rule_action, strategy),
            first="value_fusion.0",
            second="value_fusion.3",
        )

    def predict_match_outcome(self, **inputs: Any) -> dict[str, Any]:
        latent = self._value_latent(**inputs)
        hidden = v3._relu(
            self._outcome_layer("match_outcome_head.0", latent)
        )
        logits = self._outcome_layer("match_outcome_head.2", hidden)
        calibrated = (
            apply_calibration(logits, self.outcome_calibration)
            if self.outcome_calibration is not None
            else {
                "logits": logits,
                "probabilities": [v3._sigmoid(value) for value in logits],
            }
        )
        return {
            "schema": OUTCOME_HEAD_SCHEMA,
            "positive_outcome_rule": POSITIVE_OUTCOME_RULE,
            "calibrated": self.outcome_calibration is not None,
            "raw_logits": logits,
            "logits": calibrated["logits"],
            "probabilities": calibrated["probabilities"],
        }

    def predict_value(self, **inputs: Any):
        return self.base.predict_value(**inputs)

    def predict_response(self, **inputs: Any):
        return self.base.predict_response(**inputs)

    def predict_joint_value(self, **inputs: Any) -> dict[str, Any]:
        return {
            "values": self.predict_value(**inputs),
            "match_outcome": self.predict_match_outcome(**inputs),
        }
