"""Stdlib-only runtime for opponent multi-task GRU JSON models."""
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
        self.format = str(meta.get("format", ""))
        if self.format not in {"opp_multitask_gru_v1", "opp_multitask_gru_v2"}:
            raise ValueError("unsupported opponent multitask model format")
        self.meta = meta
        self.weights = payload.get("weights") or {}
        model = meta.get("model") or {}
        self.gru_hidden = int(model.get("gru_hidden", 96))
        self.max_hist = int(model.get("max_hist", 16))
        self.cross_sequence_hidden = int(model.get("cross_sequence_hidden", 0))
        self.max_cross_hands = int(model.get("max_cross_hands", 32))
        self.cross_sequence_encoder = str(
            model.get("cross_sequence_encoder", "gru")
        )
        self.cross_transformer_heads = int(
            model.get("cross_transformer_heads", 4)
        )
        self.cross_moe_experts = int(model.get("cross_moe_experts", 4))
        if self.cross_sequence_encoder not in {
            "none", "gru", "gru_moe", "deep_set", "transformer"
        }:
            raise ValueError("unsupported cross-hand sequence encoder")
        if self.cross_sequence_encoder == "none" and self.cross_sequence_hidden:
            raise ValueError("none cross-hand encoder must have zero hidden size")
        if self.cross_sequence_encoder == "transformer" and (
            self.cross_transformer_heads <= 0
            or self.cross_sequence_hidden <= 0
            or self.cross_sequence_hidden % self.cross_transformer_heads
        ):
            raise ValueError("invalid cross-hand transformer dimensions")
        if self.cross_sequence_encoder == "gru_moe" and (
            self.cross_sequence_hidden <= 0 or self.cross_moe_experts < 2
        ):
            raise ValueError("invalid cross-hand GRU MoE dimensions")
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

    def _gru_forward(
        self,
        sequence: list[list[float]],
        *,
        prefix: str,
        hidden_size: int,
        max_steps: int,
    ) -> list[float]:
        weight_input = self.weights.get(f"{prefix}.weight_ih_l0")
        weight_hidden = self.weights.get(f"{prefix}.weight_hh_l0")
        bias_input = self.weights.get(f"{prefix}.bias_ih_l0")
        bias_hidden = self.weights.get(f"{prefix}.bias_hh_l0")
        if not weight_input or not weight_hidden:
            return [0.0] * hidden_size
        hidden = [0.0] * hidden_size
        for features in sequence[-max(0, int(max_steps)):]:
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

    def _context_features(
        self,
        state: list[float],
        profile: list[float],
        history: list[list[float]],
        cross_hand: list[float],
        cross_sequence: list[list[float]] | None,
    ) -> list[float]:
        history_embedding = self._gru_forward(
            history,
            prefix="gru",
            hidden_size=self.gru_hidden,
            max_steps=self.max_hist,
        ) if history else [0.0] * self.gru_hidden
        opponent_embedding = self._linear_stack(
            cross_hand, "opp_encoder", (0, 2), final_relu=True
        )
        if not opponent_embedding:
            return []
        context = list(state) + list(profile) + history_embedding + opponent_embedding
        if self.cross_sequence_hidden > 0:
            if self.cross_sequence_encoder == "deep_set":
                sequence_embedding = self._deep_set_forward(cross_sequence or [])
            elif self.cross_sequence_encoder == "transformer":
                sequence_embedding = self._transformer_forward(
                    cross_sequence or []
                )
            else:
                sequence_embedding = self._gru_forward(
                    cross_sequence or [],
                    prefix="cross_gru",
                    hidden_size=self.cross_sequence_hidden,
                    max_steps=self.max_cross_hands,
                ) if cross_sequence else [0.0] * self.cross_sequence_hidden
                if (
                    cross_sequence
                    and self.cross_sequence_encoder == "gru_moe"
                ):
                    sequence_embedding = self._moe_forward(sequence_embedding)
            context += sequence_embedding
        return context

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

    def _deep_set_forward(
        self, sequence: list[list[float]]
    ) -> list[float]:
        encoded = [
            self._linear_stack(
                row, "cross_set_encoder", (0, 2), final_relu=True
            )
            for row in sequence[-self.max_cross_hands:]
        ]
        if not encoded or any(not row for row in encoded):
            return [0.0] * self.cross_sequence_hidden
        return [
            sum(row[index] for row in encoded) / len(encoded)
            for index in range(self.cross_sequence_hidden)
        ]

    def _moe_forward(self, embedding: list[float]) -> list[float]:
        gate = _softmax(_matvec(
            self.weights.get("cross_moe_gate.weight"),
            embedding,
            self.weights.get("cross_moe_gate.bias"),
        ))
        experts = [
            self._linear_stack(
                embedding,
                f"cross_moe_expert_layers.{index}",
                (0, 2),
                final_relu=True,
            )
            for index in range(self.cross_moe_experts)
        ]
        if len(gate) != len(experts) or any(not expert for expert in experts):
            return [0.0] * self.cross_sequence_hidden
        return [
            sum(gate[index] * experts[index][feature] for index in range(len(gate)))
            for feature in range(self.cross_sequence_hidden)
        ]

    def _layer_norm(self, values: list[float], prefix: str) -> list[float]:
        weight = self.weights.get(f"{prefix}.weight")
        bias = self.weights.get(f"{prefix}.bias")
        if not values or weight is None or bias is None:
            return []
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        scale = 1.0 / math.sqrt(variance + 1e-5)
        return [
            (value - mean) * scale * float(weight[index]) + float(bias[index])
            for index, value in enumerate(values)
        ]

    def _transformer_forward(
        self, sequence: list[list[float]]
    ) -> list[float]:
        sequence = sequence[-self.max_cross_hands:]
        if not sequence:
            return [0.0] * self.cross_sequence_hidden
        position = self.weights.get("cross_transformer.position")
        if not position:
            return [0.0] * self.cross_sequence_hidden
        hidden = self.cross_sequence_hidden
        heads = self.cross_transformer_heads
        if hidden <= 0 or heads <= 0 or hidden % heads:
            return [0.0] * max(0, hidden)
        head_dim = hidden // heads
        values = []
        for index, row in enumerate(sequence):
            projected = _matvec(
                self.weights.get("cross_transformer.input_proj.weight"),
                row,
                self.weights.get("cross_transformer.input_proj.bias"),
            )
            values.append([
                value + float(position[index][feature])
                for feature, value in enumerate(projected)
            ])
        query = [
            _matvec(
                self.weights.get("cross_transformer.q_proj.weight"), row,
                self.weights.get("cross_transformer.q_proj.bias"),
            )
            for row in values
        ]
        key = [
            _matvec(
                self.weights.get("cross_transformer.k_proj.weight"), row,
                self.weights.get("cross_transformer.k_proj.bias"),
            )
            for row in values
        ]
        projected_value = [
            _matvec(
                self.weights.get("cross_transformer.v_proj.weight"), row,
                self.weights.get("cross_transformer.v_proj.bias"),
            )
            for row in values
        ]
        attended_rows = []
        divisor = math.sqrt(head_dim)
        for row_index in range(len(values)):
            attended = []
            for head in range(heads):
                start = head * head_dim
                stop = start + head_dim
                scores = [
                    sum(
                        query[row_index][feature] * key[key_index][feature]
                        for feature in range(start, stop)
                    ) / divisor
                    for key_index in range(len(values))
                ]
                probabilities = _softmax(scores)
                attended.extend([
                    sum(
                        probabilities[key_index]
                        * projected_value[key_index][feature]
                        for key_index in range(len(values))
                    )
                    for feature in range(start, stop)
                ])
            attended_rows.append(attended)
        output_rows = []
        for residual, attended in zip(values, attended_rows):
            projected = _matvec(
                self.weights.get("cross_transformer.out_proj.weight"),
                attended,
                self.weights.get("cross_transformer.out_proj.bias"),
            )
            normalized = self._layer_norm(
                [left + right for left, right in zip(residual, projected)],
                "cross_transformer.norm1",
            )
            feed_forward = _relu(_matvec(
                self.weights.get("cross_transformer.ff.0.weight"),
                normalized,
                self.weights.get("cross_transformer.ff.0.bias"),
            ))
            feed_forward = _matvec(
                self.weights.get("cross_transformer.ff.2.weight"),
                feed_forward,
                self.weights.get("cross_transformer.ff.2.bias"),
            )
            output_rows.append(self._layer_norm(
                [left + right for left, right in zip(normalized, feed_forward)],
                "cross_transformer.norm2",
            ))
        return output_rows[-1] if output_rows and output_rows[-1] else [
            0.0
        ] * hidden

    def encode(
        self,
        state: list[float],
        profile: list[float],
        history: list[list[float]],
        cross_hand: list[float],
        rule_label_id: int,
        cross_sequence: list[list[float]] | None = None,
    ) -> list[float]:
        context = self._context_features(
            state, profile, history, cross_hand, cross_sequence
        )
        if not context:
            return []
        rule_action = [
            1.0 if index == max(0, min(len(self.labels) - 1, int(rule_label_id))) else 0.0
            for index in range(len(self.labels))
        ]
        return self._linear_stack(
            context + rule_action,
            "shared",
            (0, 3),
            final_relu=True,
        )

    def encode_response(
        self,
        state: list[float],
        profile: list[float],
        history: list[list[float]],
        cross_hand: list[float],
        cross_sequence: list[list[float]] | None = None,
    ) -> list[float]:
        context = self._context_features(
            state, profile, history, cross_hand, cross_sequence
        )
        if not context:
            return []
        return self._linear_stack(
            context,
            "response_shared",
            (0, 3),
            final_relu=True,
        )

    def predict_values(
        self,
        state: list[float],
        profile: list[float],
        history: list[list[float]],
        cross_hand: list[float],
        rule_label_id: int,
        cross_sequence: list[list[float]] | None = None,
    ) -> dict[str, dict[str, list[float]]]:
        try:
            latent = self.encode(
                state, profile, history, cross_hand, rule_label_id, cross_sequence
            )
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
                means = [value * clip for value in raw[:action_count]]
                uncalibrated_lower = [
                    value * clip for value in raw[action_count:]
                ]
                calibration = (self.meta.get("lower_calibration") or {}).get(field) or {}
                offsets = calibration.get("offsets") or [0.0] * action_count
                lowers = [
                    lower + float(offsets[index])
                    for index, lower in enumerate(uncalibrated_lower)
                ]
                safe_rule_id = max(
                    0, min(action_count - 1, int(rule_label_id))
                )
                means[safe_rule_id] = 0.0
                lowers[safe_rule_id] = 0.0
                result[field] = {
                    "mean": means,
                    "lower": lowers,
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
        cross_sequence: list[list[float]] | None = None,
    ) -> dict[str, Any]:
        try:
            public_state = list(state)
            for index in self.meta.get("response_private_state_masked") or []:
                if 0 <= int(index) < len(public_state):
                    public_state[int(index)] = 0.0
            latent = self.encode_response(
                public_state, profile, history, cross_hand, cross_sequence
            )
            raw = self._linear_stack(
                latent + list(hero_action), "response_head", (0, 2)
            )
            action_count = len(self.response_labels)
            if len(raw) != action_count + 1:
                return {}
            response_calibration = self.meta.get("response_calibration") or {}
            temperature = max(
                1e-6, float(response_calibration.get("temperature", 1.0) or 1.0)
            )
            probabilities = _softmax(
                [value / temperature for value in raw[:action_count]]
            )
            return {
                "probabilities": dict(zip(self.response_labels, probabilities)),
                "raise_pot_ratio": 4.0 * _sigmoid(raw[-1]),
            }
        except Exception:
            return {}
