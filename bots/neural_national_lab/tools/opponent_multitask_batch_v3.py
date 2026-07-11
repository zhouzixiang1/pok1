"""Strict tensor collation for opponent-aware multi-task model v3."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
from typing import Any

import torch

from cross_hand_sequence import CROSS_HAND_SEQUENCE_DIM, MAX_CROSS_HANDS
from feature_spec import LABELS
from model_input_schema import model_input_metadata
from multitask_training_data import (
    ENCODED_CONTEXT_SCHEMA,
    ENCODED_ROW_SCHEMA,
    HERO_RESPONSE_ACTION_DIM,
    MAX_CURRENT_HAND_HISTORY,
    VALUE_FIELDS,
)
from opponent_profile_schema import OPPONENT_PROFILE_DIM
from opponent_response_schema import OPPONENT_ACTION_LABELS
from strategy_context_schema import STRATEGY_CONTEXT_DIM


MODEL_BATCH_SCHEMA = "opponent_multitask_tensor_batch_v3"
MODEL_INFERENCE_BATCH_SCHEMA = "opponent_multitask_inference_batch_v3"
_MODEL_INPUT = model_input_metadata(base_state_dim=48)
STATE_DIM = int(_MODEL_INPUT["state_dim"])
HISTORY_DIM = int(_MODEL_INPUT["history_feature_dim"])


def _number(value: Any, *, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be finite") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def _vector(value: Any, *, field: str, dimension: int) -> list[float]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{field} must be a numeric sequence")
    if len(value) != dimension:
        raise ValueError(f"{field} has the wrong dimension")
    return [_number(item, field=field) for item in value]


def _unit_vector(value: Any, *, field: str, dimension: int) -> list[float]:
    result = _vector(value, field=field, dimension=dimension)
    if any(not 0.0 <= item <= 1.0 for item in result):
        raise ValueError(f"{field} must contain values in [0, 1]")
    return result


def _binary_vector(value: Any, *, field: str, dimension: int) -> list[float]:
    result = _unit_vector(value, field=field, dimension=dimension)
    if any(item not in (0.0, 1.0) for item in result):
        raise ValueError(f"{field} must be binary")
    return result


def _encoded_rows(
    rows: Sequence[Mapping[str, Any]], *, response: bool, supervised: bool
) -> list[Mapping[str, Any]]:
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)) or not rows:
        raise ValueError("a model batch requires at least one encoded row")
    result = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"batch row {index} is not a mapping")
        if row.get("encoded_context_schema") != ENCODED_CONTEXT_SCHEMA:
            raise ValueError(f"batch row {index} has the wrong context schema")
        if supervised and row.get("encoded_row_schema") != ENCODED_ROW_SCHEMA:
            raise ValueError(f"batch row {index} has the wrong encoded schema")
        if bool(row.get("response_mode")) != bool(response):
            raise ValueError(f"batch row {index} has the wrong task mode")
        result.append(row)
    return result


def _common_inputs(
    encoded: Sequence[Mapping[str, Any]], *, device: torch.device | str
) -> dict[str, torch.Tensor]:
    state = torch.tensor(
        [
            _unit_vector(row.get("state"), field="state", dimension=STATE_DIM)
            for row in encoded
        ],
        dtype=torch.float32,
        device=device,
    )
    profile = torch.tensor(
        [
            _unit_vector(
                row.get("opponent_profile"),
                field="opponent_profile",
                dimension=OPPONENT_PROFILE_DIM,
            )
            for row in encoded
        ],
        dtype=torch.float32,
        device=device,
    )
    history, history_lengths = _sequence_tensor(
        encoded,
        key="history",
        feature_dim=HISTORY_DIM,
        max_length=MAX_CURRENT_HAND_HISTORY,
        device=device,
    )
    cross_sequence, cross_lengths = _sequence_tensor(
        encoded,
        key="cross_hand_sequence",
        feature_dim=CROSS_HAND_SEQUENCE_DIM,
        max_length=MAX_CROSS_HANDS,
        device=device,
    )
    return {
        "state": state,
        "profile": profile,
        "history": history,
        "history_lengths": history_lengths,
        "cross_sequence": cross_sequence,
        "cross_lengths": cross_lengths,
    }


def _value_inputs(
    encoded: Sequence[Mapping[str, Any]], *, device: torch.device | str
) -> tuple[dict[str, torch.Tensor], torch.Tensor]:
    inputs = {
        "rule_action": torch.tensor(
            [
                _binary_vector(
                    row.get("rule_action"),
                    field="rule_action",
                    dimension=len(LABELS),
                )
                for row in encoded
            ],
            dtype=torch.float32,
            device=device,
        ),
        "strategy_context": torch.tensor(
            [
                _unit_vector(
                    row.get("strategy_context"),
                    field="strategy_context",
                    dimension=STRATEGY_CONTEXT_DIM,
                )
                for row in encoded
            ],
            dtype=torch.float32,
            device=device,
        ),
    }
    if not bool((inputs["rule_action"].sum(dim=1) == 1.0).all()):
        raise ValueError("rule_action must be one-hot")
    legal = torch.tensor(
        [
            _binary_vector(
                row.get("legal_action_mask"),
                field="legal_action_mask",
                dimension=len(LABELS),
            )
            for row in encoded
        ],
        dtype=torch.float32,
        device=device,
    )
    if not bool((legal.sum(dim=1) > 0.0).all()):
        raise ValueError("every value row requires at least one legal action")
    if not bool(((inputs["rule_action"] * legal).sum(dim=1) == 1.0).all()):
        raise ValueError("rule_action must be legal")
    return inputs, legal


def _response_inputs(
    encoded: Sequence[Mapping[str, Any]], *, device: torch.device | str
) -> tuple[dict[str, torch.Tensor], torch.Tensor]:
    legal = torch.tensor(
        [
            _binary_vector(
                row.get("response_legal_action_mask"),
                field="response_legal_action_mask",
                dimension=len(OPPONENT_ACTION_LABELS),
            )
            for row in encoded
        ],
        dtype=torch.float32,
        device=device,
    )
    if not bool((legal.sum(dim=1) > 0.0).all()):
        raise ValueError("every response row requires at least one legal action")
    return {
        "hero_action": torch.tensor(
            [
                _unit_vector(
                    row.get("hero_action_features"),
                    field="hero_action_features",
                    dimension=HERO_RESPONSE_ACTION_DIM,
                )
                for row in encoded
            ],
            dtype=torch.float32,
            device=device,
        ),
        "legal_action_mask": legal,
    }, legal


def _sequence_tensor(
    rows: Sequence[Mapping[str, Any]],
    *,
    key: str,
    feature_dim: int,
    max_length: int,
    device: torch.device | str,
) -> tuple[torch.Tensor, torch.Tensor]:
    sequences: list[list[list[float]]] = []
    for row_index, row in enumerate(rows):
        raw = row.get(key)
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise ValueError(f"{key} row {row_index} must be a sequence")
        if len(raw) > max_length:
            raise ValueError(f"{key} row {row_index} exceeds its maximum length")
        sequence = [
            _vector(
                event,
                field=f"{key}[{row_index}][{event_index}]",
                dimension=feature_dim,
            )
            for event_index, event in enumerate(raw)
        ]
        sequences.append(sequence)
    lengths = torch.tensor(
        [len(sequence) for sequence in sequences],
        dtype=torch.long,
        device=device,
    )
    width = max(1, max((len(sequence) for sequence in sequences), default=0))
    padded = torch.zeros(
        len(sequences), width, feature_dim, dtype=torch.float32, device=device
    )
    for index, sequence in enumerate(sequences):
        if sequence:
            padded[index, : len(sequence)] = torch.tensor(
                sequence, dtype=torch.float32, device=device
            )
    return padded, lengths


def collate_encoded_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    response: bool,
    device: torch.device | str = "cpu",
) -> dict[str, Any]:
    """Build model kwargs and masked supervision without legacy trainer state."""
    encoded = _encoded_rows(rows, response=response, supervised=True)
    inputs = _common_inputs(encoded, device=device)
    weights = torch.tensor(
        [_number(row.get("row_weight"), field="row_weight") for row in encoded],
        dtype=torch.float32,
        device=device,
    )
    if not bool((weights > 0.0).all()):
        raise ValueError("every model row requires a positive weight")

    if response:
        response_inputs, legal = _response_inputs(encoded, device=device)
        target = torch.tensor(
            [int(row.get("response_target", -1)) for row in encoded],
            dtype=torch.long,
            device=device,
        )
        if not bool(
            ((target >= 0) & (target < len(OPPONENT_ACTION_LABELS))).all()
        ):
            raise ValueError("response target is out of range")
        if not bool((legal.gather(1, target.unsqueeze(1)) > 0.0).all()):
            raise ValueError("response target is illegal")
        inputs.update(response_inputs)
        supervision = {
            "target": target,
            "size_targets": torch.tensor(
                [
                    _unit_vector(
                        row.get("response_size_targets"),
                        field="response_size_targets",
                        dimension=2,
                    )
                    for row in encoded
                ],
                dtype=torch.float32,
                device=device,
            ),
            "size_target_mask": torch.tensor(
                [
                    _binary_vector(
                        row.get("response_size_target_mask"),
                        field="response_size_target_mask",
                        dimension=2,
                    )
                    for row in encoded
                ],
                dtype=torch.float32,
                device=device,
            ),
            "row_weight": weights,
        }
    else:
        value_inputs, value_legal = _value_inputs(encoded, device=device)
        inputs.update(value_inputs)
        target_payload: dict[str, torch.Tensor] = {}
        mask_payload: dict[str, torch.Tensor] = {}
        for field in VALUE_FIELDS:
            target_payload[field] = torch.tensor(
                [
                    _vector(
                        row.get("value_targets", {}).get(field),
                        field=f"value_targets.{field}",
                        dimension=len(LABELS),
                    )
                    for row in encoded
                ],
                dtype=torch.float32,
                device=device,
            )
            mask_payload[field] = torch.tensor(
                [
                    _binary_vector(
                        row.get("value_target_masks", {}).get(field),
                        field=f"value_target_masks.{field}",
                        dimension=len(LABELS),
                    )
                    for row in encoded
                ],
                dtype=torch.float32,
                device=device,
            )
        if any(bool((mask > value_legal).any()) for mask in mask_payload.values()):
            raise ValueError("value target masks must be subsets of legal actions")
        supervision = {
            "targets": target_payload,
            "target_masks": mask_payload,
            "legal_action_mask": value_legal,
            "strategy_context_available": torch.tensor(
                [bool(row.get("strategy_context_available")) for row in encoded],
                dtype=torch.bool,
                device=device,
            ),
            "row_weight": weights,
        }
    return {
        "schema": MODEL_BATCH_SCHEMA,
        "response_mode": bool(response),
        "rows": len(encoded),
        "inputs": inputs,
        "supervision": supervision,
        "opponents": [str(row.get("opponent")) for row in encoded],
    }


def collate_inference_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    response: bool,
    device: torch.device | str = "cpu",
) -> dict[str, Any]:
    """Build model inputs without accepting weights or supervision targets."""
    encoded = _encoded_rows(rows, response=response, supervised=False)
    inputs = _common_inputs(encoded, device=device)
    if response:
        task_inputs, legal = _response_inputs(encoded, device=device)
        inputs.update(task_inputs)
        legal_key = "response_legal_action_mask"
    else:
        task_inputs, legal = _value_inputs(encoded, device=device)
        inputs.update(task_inputs)
        legal_key = "legal_action_mask"
    return {
        "schema": MODEL_INFERENCE_BATCH_SCHEMA,
        "response_mode": bool(response),
        "rows": len(encoded),
        "inputs": inputs,
        legal_key: legal,
        "strategy_context_available": (
            None if response else torch.tensor(
                [bool(row.get("strategy_context_available")) for row in encoded],
                dtype=torch.bool,
                device=device,
            )
        ),
        "opponents": [str(row.get("opponent")) for row in encoded],
    }
