"""Stdlib-only runtime for ``opp_multitask_gru_v1`` JSON models."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from opp_value_runtime import _matvec, _relu, _sigmoid


def _softmax(values: list[float]) -> list[float]:
    if not values:
        return []
    peak = max(values)
    exps = [math.exp(max(-60.0, min(60.0, value - peak))) for value in values]
    total = sum(exps) or 1.0
    return [value / total for value in exps]


class OpponentMultiTaskRuntime:
    def __init__(self, payload: dict[str, Any]) -> None:
        meta = payload.get("meta") or {}
        if meta.get("format") != "opp_multitask_gru_v1":
            raise ValueError("unsupported opponent multitask model format")
        self.meta = meta
        self.weights = payload.get("weights") or {}
        model = meta.get("model") or {}
        self.gru_hidden = int(model.get("gru_hidden", 96))
        self.max_hist = int(model.get("max_hist", 16))
        self.labels = list(meta.get("labels") or [])
        self.response_labels = list(meta.get("opponent_action_labels") or [])
        self.value_fields = list(meta.get("value_fields") or [])
        training = meta.get("training") or {}
        self.clips = dict(training.get("clips") or {})

    @classmethod
    def load(cls, path: str | Path) -> "OpponentMultiTaskRuntime | None":
        try:
            return cls(json.loads(Path(path).read_text(encoding="utf-8")))
        except Exception:
            return None

    def _gru_forward(self, sequence: list[list[float]]) -> list[float]:
        hidden_size = self.gru_hidden
        weight_input = self.weights.get("gru.weight_ih_l0")
        weight_hidden = self.weights.get("gru.weight_hh_l0")
        bias_input = self.weights.get("gru.bias_ih_l0")
        bias_hidden = self.weights.get("gru.bias_hh_l0")
        if not weight_input or not weight_hidden:
            return [0.0] * hidden_size
        hidden = [0.0] * hidden_size
        for features in sequence[:self.max_hist]:
            input_gates = _matvec(weight_input, features, bias_input)
            hidden_gates = _matvec(weight_hidden, hidden, bias_hidden)
            for idx in range(hidden_size):
                reset = _sigmoid(input_gates[idx] + hidden_gates[idx])
                update = _sigmoid(
                    input_gates[hidden_size + idx] + hidden_gates[hidden_size + idx]
                )
                candidate = math.tanh(
                    input_gates[2 * hidden_size + idx]
                    + reset * hidden_gates[2 * hidden_size + idx]
                )
                hidden[idx] = (1.0 - update) * candidate + update * hidden[idx]
        return hidden

    def _linear_stack(
        self,
        features: list[float],
        prefix: str,
        linear_indices: tuple[int, ...],
        *,
        final_relu: bool = False,
    ) -> list[float]:
        output = list(features)
        for position, layer_index in enumerate(linear_indices):
            weight = self.weights.get(f"{prefix}.{layer_index}.weight")
            bias = self.weights.get(f"{prefix}.{layer_index}.bias")
            if weight is None:
                return []
            output = _matvec(weight, output, bias)
            if position < len(linear_indices) - 1 or final_relu:
                output = _relu(output)
        return output

    def encode(
        self,
        state: list[float],
        profile: list[float],
        history: list[list[float]],
        cross_hand: list[float],
    ) -> list[float]:
        history_embedding = self._gru_forward(history) if history else [0.0] * self.gru_hidden
        opponent_embedding = self._linear_stack(
            cross_hand, "opp_encoder", (0, 2), final_relu=True
        )
        if not opponent_embedding:
            return []
        return self._linear_stack(
            list(state) + list(profile) + history_embedding + opponent_embedding,
            "shared",
            (0, 3),
            final_relu=True,
        )

    def predict_values(
        self,
        state: list[float],
        profile: list[float],
        history: list[list[float]],
        cross_hand: list[float],
    ) -> dict[str, dict[str, list[float]]]:
        try:
            latent = self.encode(state, profile, history, cross_hand)
            if not latent:
                return {}
            result = {}
            action_count = len(self.labels)
            for field in self.value_fields:
                raw = self._linear_stack(
                    latent, f"value_heads.{field}", (0, 2)
                )
                if len(raw) != action_count * 2:
                    return {}
                clip = float(self.clips.get(field, 1.0) or 1.0)
                result[field] = {
                    "mean": [value * clip for value in raw[:action_count]],
                    "lower": [value * clip for value in raw[action_count:]],
                }
            return result
        except Exception:
            return {}

    def predict_response(
        self,
        state: list[float],
        profile: list[float],
        history: list[list[float]],
        cross_hand: list[float],
        hero_action: list[float],
    ) -> dict[str, Any]:
        try:
            latent = self.encode(state, profile, history, cross_hand)
            raw = self._linear_stack(
                latent + list(hero_action), "response_head", (0, 2)
            )
            action_count = len(self.response_labels)
            if len(raw) != action_count + 1:
                return {}
            probabilities = _softmax(raw[:action_count])
            return {
                "probabilities": dict(zip(self.response_labels, probabilities)),
                "raise_pot_ratio": 4.0 * _sigmoid(raw[-1]),
            }
        except Exception:
            return {}
