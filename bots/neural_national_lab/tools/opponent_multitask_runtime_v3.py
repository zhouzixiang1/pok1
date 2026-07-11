"""Stdlib-only runtime for exported opponent-aware multi-task v3 models."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


RUNTIME_FORMAT = "opponent_multitask_stdlib_v3"
MODEL_FORMAT = "opponent_multitask_distributional_v3"
LABELS = ("fold", "call", "raise_half", "raise_pot", "raise_2pot", "allin")
RESPONSE_LABELS = ("fold", "check", "call", "raise", "allin")
VALUE_FIELDS = ("delta_vs_rule", "tail_delta_vs_rule", "match_delta_vs_rule")
QUANTILE_LEVELS = (0.05, 0.10, 0.20, 0.50)
STATE_DIM = 81
PROFILE_DIM = 12
HISTORY_DIM = 24
CROSS_DIM = 16
STRATEGY_DIM = 66
HERO_ACTION_DIM = 10
MAX_HISTORY = 16
MAX_CROSS_HANDS = 32
PRIVATE_STATE_INDICES = tuple(range(5, 10)) + tuple(range(48, 66))
HIDDEN_KEYS = (
    "state_hidden",
    "profile_hidden",
    "history_hidden",
    "cross_hidden",
    "opponent_hidden",
    "strategy_hidden",
    "fusion_hidden",
    "latent",
    "head_hidden",
)


def _finite(value: Any, *, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be finite") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def _vector(value: Any, *, size: int, field: str) -> list[float]:
    if not isinstance(value, list) or len(value) != size:
        raise ValueError(f"{field} has the wrong vector shape")
    return [_finite(item, field=field) for item in value]


def _matrix(value: Any, *, rows: int, columns: int, field: str) -> list[list[float]]:
    if not isinstance(value, list) or len(value) != rows:
        raise ValueError(f"{field} has the wrong matrix shape")
    return [
        _vector(row, size=columns, field=field)
        for row in value
    ]


def _linear(
    weights: list[list[float]], values: list[float], bias: list[float]
) -> list[float]:
    result = []
    for row, offset in zip(weights, bias):
        total = offset
        for weight, value in zip(row, values):
            total += weight * value
        result.append(total)
    return result


def _relu(values: list[float]) -> list[float]:
    return [value if value > 0.0 else 0.0 for value in values]


def _sigmoid(value: float) -> float:
    if value >= 0.0:
        exp_value = math.exp(-min(value, 60.0))
        return 1.0 / (1.0 + exp_value)
    exp_value = math.exp(max(value, -60.0))
    return exp_value / (1.0 + exp_value)


def _softplus(value: float) -> float:
    if value > 20.0:
        return value
    if value < -20.0:
        return math.exp(value)
    return math.log1p(math.exp(value))


def _softmax(values: list[float]) -> list[float]:
    if not values:
        return []
    peak = max(values)
    exponents = [math.exp(max(-60.0, value - peak)) for value in values]
    total = sum(exponents)
    return [value / total for value in exponents]


def _concat(*parts: list[float]) -> list[float]:
    return [value for part in parts for value in part]


class OpponentMultiTaskRuntimeV3:
    """Validated pure-Python forward pass for one frozen v3 member."""

    def __init__(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict) or payload.get("format") != RUNTIME_FORMAT:
            raise ValueError("unsupported v3 stdlib model format")
        metadata = payload.get("model_metadata")
        hidden = payload.get("hidden_sizes")
        weights = payload.get("weights")
        if not isinstance(metadata, dict) or not isinstance(hidden, dict):
            raise ValueError("v3 stdlib model metadata is missing")
        if not isinstance(weights, dict):
            raise ValueError("v3 stdlib model weights are missing")
        self.metadata = dict(metadata)
        self.hidden = {
            key: int(hidden.get(key, 0)) for key in HIDDEN_KEYS
        }
        if set(hidden) != set(HIDDEN_KEYS) or min(self.hidden.values()) <= 0:
            raise ValueError("v3 hidden-size contract is invalid")
        self.cross_encoder = str(metadata.get("cross_encoder", ""))
        self.moe_experts = int(metadata.get("moe_experts", 0))
        if self.cross_encoder not in {"none", "deep_set", "gru", "gru_moe"}:
            raise ValueError("unsupported v3 cross-hand encoder")
        if self.cross_encoder == "gru_moe" and self.moe_experts < 2:
            raise ValueError("GRU MoE requires at least two experts")
        self._validate_metadata()
        expected = self._expected_shapes()
        if set(weights) != set(expected):
            missing = sorted(set(expected) - set(weights))
            extra = sorted(set(weights) - set(expected))
            raise ValueError(f"v3 weight keys changed: missing={missing} extra={extra}")
        self.weights: dict[str, Any] = {}
        parameter_count = 0
        for name, shape in expected.items():
            if len(shape) == 1:
                self.weights[name] = _vector(
                    weights[name], size=shape[0], field=name
                )
                parameter_count += shape[0]
            else:
                self.weights[name] = _matrix(
                    weights[name], rows=shape[0], columns=shape[1], field=name
                )
                parameter_count += shape[0] * shape[1]
        if parameter_count != int(metadata.get("parameters", -1)):
            raise ValueError("v3 parameter count changed")

    @classmethod
    def load(cls, path: str | Path) -> "OpponentMultiTaskRuntimeV3 | None":
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            return cls(payload)
        except Exception:
            return None

    def _validate_metadata(self) -> None:
        expected = {
            "format": MODEL_FORMAT,
            "state_dim": STATE_DIM,
            "history_dim": HISTORY_DIM,
            "max_current_hand_history": MAX_HISTORY,
            "profile_dim": PROFILE_DIM,
            "cross_hand_sequence_dim": CROSS_DIM,
            "max_cross_hands": MAX_CROSS_HANDS,
            "strategy_context_dim": STRATEGY_DIM,
            "strategy_context_value_head_only": True,
            "hero_action_dim": HERO_ACTION_DIM,
            "labels": list(LABELS),
            "quantile_levels": list(QUANTILE_LEVELS),
            "opponent_action_labels": list(RESPONSE_LABELS),
            "value_fields": list(VALUE_FIELDS),
            "response_private_state_masked": list(PRIVATE_STATE_INDICES),
        }
        for key, value in expected.items():
            if self.metadata.get(key) != value:
                raise ValueError(f"v3 model metadata changed: {key}")
        dropout = _finite(self.metadata.get("dropout"), field="dropout")
        if not 0.0 <= dropout < 1.0:
            raise ValueError("v3 dropout contract is invalid")

    def _expected_shapes(self) -> dict[str, tuple[int, ...]]:
        h = self.hidden
        common = h["state_hidden"] + h["history_hidden"]
        shapes: dict[str, tuple[int, ...]] = {}

        def linear(prefix: str, output: int, input_size: int) -> None:
            shapes[f"{prefix}.weight"] = (output, input_size)
            shapes[f"{prefix}.bias"] = (output,)

        def gru(prefix: str, hidden: int, input_size: int) -> None:
            shapes[f"{prefix}.weight_ih_l0"] = (3 * hidden, input_size)
            shapes[f"{prefix}.weight_hh_l0"] = (3 * hidden, hidden)
            shapes[f"{prefix}.bias_ih_l0"] = (3 * hidden,)
            shapes[f"{prefix}.bias_hh_l0"] = (3 * hidden,)

        linear("state_encoder.0", h["state_hidden"], STATE_DIM)
        linear("state_encoder.3", h["state_hidden"], h["state_hidden"])
        linear("profile_encoder.0", h["profile_hidden"], PROFILE_DIM)
        linear("profile_encoder.2", h["profile_hidden"], h["profile_hidden"])
        gru("history_gru", h["history_hidden"], HISTORY_DIM)
        if self.cross_encoder == "deep_set":
            linear("cross_encoder.item_encoder.0", h["cross_hidden"], CROSS_DIM)
            linear(
                "cross_encoder.item_encoder.2",
                h["cross_hidden"],
                h["cross_hidden"],
            )
            linear(
                "cross_encoder.set_fusion.0",
                h["cross_hidden"],
                2 * h["cross_hidden"],
            )
        elif self.cross_encoder in {"gru", "gru_moe"}:
            gru("cross_encoder.gru", h["cross_hidden"], CROSS_DIM)
            if self.cross_encoder == "gru_moe":
                linear("cross_encoder.gate", self.moe_experts, h["cross_hidden"])
                for index in range(self.moe_experts):
                    linear(
                        f"cross_encoder.experts.{index}.0",
                        h["cross_hidden"],
                        h["cross_hidden"],
                    )
                    linear(
                        f"cross_encoder.experts.{index}.2",
                        h["cross_hidden"],
                        h["cross_hidden"],
                    )
        linear(
            "opponent_fusion.0",
            h["opponent_hidden"],
            h["profile_hidden"] + h["cross_hidden"],
        )
        linear(
            "opponent_fusion.3", h["opponent_hidden"], h["opponent_hidden"]
        )
        linear("opponent_interaction", common, h["opponent_hidden"])
        linear("strategy_encoder.0", h["strategy_hidden"], STRATEGY_DIM)
        linear(
            "strategy_encoder.2", h["strategy_hidden"], h["strategy_hidden"]
        )
        linear(
            "value_fusion.0",
            h["fusion_hidden"],
            2 * common + h["opponent_hidden"] + len(LABELS)
            + h["strategy_hidden"],
        )
        linear("value_fusion.3", h["latent"], h["fusion_hidden"])
        linear(
            "response_fusion.0",
            h["fusion_hidden"],
            2 * common + h["opponent_hidden"] + HERO_ACTION_DIM,
        )
        linear("response_fusion.3", h["latent"], h["fusion_hidden"])
        for field in VALUE_FIELDS:
            linear(
                f"value_heads.{field}.0", h["head_hidden"], h["latent"]
            )
            linear(
                f"value_heads.{field}.2",
                len(LABELS) * (1 + len(QUANTILE_LEVELS)),
                h["head_hidden"],
            )
        linear("response_head.0", h["head_hidden"], h["latent"])
        linear(
            "response_head.2",
            len(RESPONSE_LABELS) + 2,
            h["head_hidden"],
        )
        return shapes

    def _layer(self, prefix: str, values: list[float]) -> list[float]:
        return _linear(
            self.weights[f"{prefix}.weight"],
            values,
            self.weights[f"{prefix}.bias"],
        )

    def _two_layer_relu(
        self, values: list[float], *, first: str, second: str
    ) -> list[float]:
        return _relu(self._layer(second, _relu(self._layer(first, values))))

    def _gru(
        self, sequence: list[list[float]], *, prefix: str, hidden_size: int
    ) -> list[float]:
        hidden = [0.0] * hidden_size
        weight_ih = self.weights[f"{prefix}.weight_ih_l0"]
        weight_hh = self.weights[f"{prefix}.weight_hh_l0"]
        bias_ih = self.weights[f"{prefix}.bias_ih_l0"]
        bias_hh = self.weights[f"{prefix}.bias_hh_l0"]
        for features in sequence:
            input_gates = _linear(weight_ih, features, bias_ih)
            hidden_gates = _linear(weight_hh, hidden, bias_hh)
            updated = [0.0] * hidden_size
            for index in range(hidden_size):
                reset = _sigmoid(input_gates[index] + hidden_gates[index])
                update = _sigmoid(
                    input_gates[hidden_size + index]
                    + hidden_gates[hidden_size + index]
                )
                candidate = math.tanh(
                    input_gates[2 * hidden_size + index]
                    + reset * hidden_gates[2 * hidden_size + index]
                )
                updated[index] = (1.0 - update) * candidate + update * hidden[index]
            hidden = updated
        return hidden

    def _sequence(
        self,
        value: Any,
        *,
        feature_size: int,
        max_length: int,
        field: str,
    ) -> list[list[float]]:
        if not isinstance(value, list) or len(value) > max_length:
            raise ValueError(f"{field} has the wrong sequence shape")
        return [
            _vector(row, size=feature_size, field=field) for row in value
        ]

    def _cross_embedding(self, sequence: list[list[float]]) -> list[float]:
        size = self.hidden["cross_hidden"]
        if not sequence or self.cross_encoder == "none":
            return [0.0] * size
        if self.cross_encoder == "deep_set":
            encoded = [
                self._two_layer_relu(
                    row,
                    first="cross_encoder.item_encoder.0",
                    second="cross_encoder.item_encoder.2",
                )
                for row in sequence
            ]
            mean = [
                sum(row[index] for row in encoded) / len(encoded)
                for index in range(size)
            ]
            maximum = [max(row[index] for row in encoded) for index in range(size)]
            return _relu(
                self._layer("cross_encoder.set_fusion.0", mean + maximum)
            )
        embedding = self._gru(
            sequence, prefix="cross_encoder.gru", hidden_size=size
        )
        if self.cross_encoder == "gru_moe":
            gates = _softmax(self._layer("cross_encoder.gate", embedding))
            experts = [
                self._two_layer_relu(
                    embedding,
                    first=f"cross_encoder.experts.{index}.0",
                    second=f"cross_encoder.experts.{index}.2",
                )
                for index in range(self.moe_experts)
            ]
            embedding = [
                sum(gates[expert] * experts[expert][index]
                    for expert in range(self.moe_experts))
                for index in range(size)
            ]
        return embedding

    def _common(
        self,
        state: list[float],
        profile: list[float],
        history: list[list[float]],
        cross_sequence: list[list[float]],
        *,
        response: bool,
    ) -> tuple[list[float], list[float], list[float]]:
        state = _vector(state, size=STATE_DIM, field="state")
        profile = _vector(profile, size=PROFILE_DIM, field="profile")
        history = self._sequence(
            history,
            feature_size=HISTORY_DIM,
            max_length=MAX_HISTORY,
            field="history",
        )
        cross_sequence = self._sequence(
            cross_sequence,
            feature_size=CROSS_DIM,
            max_length=MAX_CROSS_HANDS,
            field="cross_sequence",
        )
        if response:
            state = list(state)
            for index in PRIVATE_STATE_INDICES:
                state[index] = 0.0
        state_embedding = self._two_layer_relu(
            state, first="state_encoder.0", second="state_encoder.3"
        )
        history_embedding = (
            self._gru(
                history,
                prefix="history_gru",
                hidden_size=self.hidden["history_hidden"],
            )
            if history
            else [0.0] * self.hidden["history_hidden"]
        )
        common = state_embedding + history_embedding
        profile_embedding = self._two_layer_relu(
            profile, first="profile_encoder.0", second="profile_encoder.2"
        )
        opponent = self._two_layer_relu(
            profile_embedding + self._cross_embedding(cross_sequence),
            first="opponent_fusion.0",
            second="opponent_fusion.3",
        )
        modulation = [
            math.tanh(value)
            for value in self._layer("opponent_interaction", opponent)
        ]
        interaction = [
            value * modulation[index] for index, value in enumerate(common)
        ]
        return common, opponent, interaction

    def predict_value(
        self,
        *,
        state: list[float],
        profile: list[float],
        history: list[list[float]],
        cross_sequence: list[list[float]],
        rule_action: list[float],
        strategy_context: list[float],
    ) -> dict[str, dict[str, list[Any]]]:
        common, opponent, interaction = self._common(
            state, profile, history, cross_sequence, response=False
        )
        rule_action = _vector(
            rule_action, size=len(LABELS), field="rule_action"
        )
        if (
            any(value not in (0.0, 1.0) for value in rule_action)
            or sum(rule_action) != 1.0
        ):
            raise ValueError("rule_action must be one-hot")
        strategy_context = _vector(
            strategy_context, size=STRATEGY_DIM, field="strategy_context"
        )
        strategy = self._two_layer_relu(
            strategy_context,
            first="strategy_encoder.0",
            second="strategy_encoder.2",
        )
        latent = self._two_layer_relu(
            _concat(common, opponent, interaction, rule_action, strategy),
            first="value_fusion.0",
            second="value_fusion.3",
        )
        result = {}
        action_count = len(LABELS)
        quantile_count = len(QUANTILE_LEVELS)
        for field in VALUE_FIELDS:
            hidden = _relu(self._layer(f"value_heads.{field}.0", latent))
            raw = self._layer(f"value_heads.{field}.2", hidden)
            means = raw[:action_count]
            quantile_raw = raw[action_count:]
            quantiles = []
            for action in range(action_count):
                start = action * quantile_count
                first = quantile_raw[start]
                row = [first]
                total = first
                for value in quantile_raw[start + 1:start + quantile_count]:
                    total += _softplus(value)
                    row.append(total)
                quantiles.append(row)
            result[field] = {"mean": means, "quantiles": quantiles}
        return result

    def predict_response(
        self,
        *,
        state: list[float],
        profile: list[float],
        history: list[list[float]],
        cross_sequence: list[list[float]],
        hero_action: list[float],
        legal_action_mask: list[float] | None = None,
    ) -> dict[str, list[float]]:
        common, opponent, interaction = self._common(
            state, profile, history, cross_sequence, response=True
        )
        hero_action = _vector(
            hero_action, size=HERO_ACTION_DIM, field="hero_action"
        )
        latent = self._two_layer_relu(
            _concat(common, opponent, interaction, hero_action),
            first="response_fusion.0",
            second="response_fusion.3",
        )
        hidden = _relu(self._layer("response_head.0", latent))
        raw = self._layer("response_head.2", hidden)
        logits = raw[:len(RESPONSE_LABELS)]
        if legal_action_mask is not None:
            legal = _vector(
                legal_action_mask,
                size=len(RESPONSE_LABELS),
                field="legal_action_mask",
            )
            if any(value not in (0.0, 1.0) for value in legal) or not any(legal):
                raise ValueError("legal_action_mask must be nonempty and binary")
            logits = [
                value if legal[index] else -1.0e9
                for index, value in enumerate(logits)
            ]
        return {
            "logits": logits,
            "size": [_sigmoid(value) for value in raw[len(RESPONSE_LABELS):]],
        }
