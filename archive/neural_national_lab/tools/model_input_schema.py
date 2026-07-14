"""Shared row-to-model input contract for training and stdlib inference."""
from __future__ import annotations

from collections.abc import Mapping
import math
from typing import Any

from history_feature_schema import (
    CURRENT_HAND_HISTORY_SCHEMA,
    encode_history_sequence,
    history_feature_metadata,
)
from state_feature_schema import (
    HERO_HAND_PUBLIC_DECISION_STATE_SCHEMA,
    extend_state_features,
    feature_schema_metadata,
    private_state_indices,
)


MODEL_INPUT_SCHEMA = "opponent_multitask_input_v2"
DEFAULT_STATE_SCHEMA = HERO_HAND_PUBLIC_DECISION_STATE_SCHEMA
DEFAULT_HISTORY_SCHEMA = CURRENT_HAND_HISTORY_SCHEMA


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _bounded_vector(values: list[Any], *, field: str) -> list[float]:
    result = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{field} contains a non-numeric value") from exc
        if not math.isfinite(number) or not 0.0 <= number <= 1.0:
            raise ValueError(f"{field} must contain finite values in [0, 1]")
        result.append(number)
    return result


def encode_model_input(
    row: Mapping[str, Any] | None,
    base_state_features: list[Any],
    *,
    max_hist: int = 16,
    response: bool = False,
    state_schema: str = DEFAULT_STATE_SCHEMA,
    history_schema: str = DEFAULT_HISTORY_SCHEMA,
) -> dict[str, Any]:
    if max_hist < 1:
        raise ValueError("max_hist must be positive")
    row_map = _mapping(row)
    request = _mapping(row_map.get("request"))
    state = _mapping(row_map.get("state"))
    base = _bounded_vector(list(base_state_features), field="base_state_features")
    encoded_state = extend_state_features(
        base,
        request,
        state=state,
        legal_mask=row_map.get("legal_mask"),
        schema=state_schema,
    )
    private = private_state_indices(schema=state_schema, base_dim=len(base))
    if response:
        encoded_state = list(encoded_state)
        for index in private:
            encoded_state[index] = 0.0
    encoded_history = encode_history_sequence(
        request,
        request.get("my_id"),
        schema=history_schema,
    )[-max_hist:]
    encoded_state = _bounded_vector(encoded_state, field="state")
    encoded_history = [
        _bounded_vector(event, field="history") for event in encoded_history
    ]
    return {
        "schema": MODEL_INPUT_SCHEMA,
        "state_schema": state_schema,
        "history_schema": history_schema,
        "state": encoded_state,
        "history": encoded_history,
        "response_private_state_masked": list(private),
        "response_mode": bool(response),
    }


def model_input_metadata(
    *,
    base_state_dim: int,
    state_schema: str = DEFAULT_STATE_SCHEMA,
    history_schema: str = DEFAULT_HISTORY_SCHEMA,
) -> dict[str, Any]:
    state = feature_schema_metadata(schema=state_schema, base_dim=base_state_dim)
    history = history_feature_metadata(schema=history_schema)
    return {
        "schema": MODEL_INPUT_SCHEMA,
        "base_state_dim": int(base_state_dim),
        "state": state,
        "history": history,
        "state_dim": state["state_dim"],
        "history_feature_dim": history["history_feature_dim"],
        "response_private_state_masked": state["response_private_state_masked"],
        "strategy_context_schema": None,
        "strategy_context_captured": False,
    }
