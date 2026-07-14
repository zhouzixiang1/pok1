"""Weighted, legality-aware calibration for frozen multi-task checkpoints."""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
import hashlib
import json
import math
from typing import Any

from multitask_training_data import (
    EVALUATION_WEIGHT_FIELD,
    MODEL_CALIBRATION_ROLE,
    MULTITASK_TRAINING_DATA_SCHEMA,
)
from opponent_response_schema import OPPONENT_ACTION_LABELS


VALUE_LOWER_CALIBRATION_SCHEMA = "weighted_value_lower_calibration_v2"
RESPONSE_TEMPERATURE_SCHEMA = "masked_response_temperature_v2"
MULTITASK_CALIBRATION_ARTIFACT_SCHEMA = "multitask_model_calibration_v1"


def _finite(value: Any, *, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be finite") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def _positive_weight(row: Mapping[str, Any]) -> float:
    value = row.get("weight", row.get(EVALUATION_WEIGHT_FIELD))
    weight = _finite(value, field="calibration weight")
    if weight <= 0.0:
        raise ValueError("calibration weight must be positive")
    return weight


def _effective_sample_size(weights: Sequence[float]) -> float:
    total = sum(weights)
    squared = sum(weight * weight for weight in weights)
    return total * total / squared if total > 0.0 and squared > 0.0 else 0.0


def weighted_quantile(
    values: Sequence[float], weights: Sequence[float], quantile: float
) -> float:
    """Return the deterministic left-continuous weighted empirical quantile."""
    if len(values) != len(weights) or not values:
        raise ValueError("weighted quantile requires equal non-empty inputs")
    q = _finite(quantile, field="quantile")
    if not 0.0 < q < 1.0:
        raise ValueError("quantile must be in (0, 1)")
    pairs = sorted(
        (
            _finite(value, field="quantile value"),
            _finite(weight, field="quantile weight"),
        )
        for value, weight in zip(values, weights)
    )
    if any(weight <= 0.0 for _, weight in pairs):
        raise ValueError("quantile weights must be positive")
    threshold = q * sum(weight for _, weight in pairs)
    cumulative = 0.0
    for value, weight in pairs:
        cumulative += weight
        if cumulative >= threshold:
            return value
    return pairs[-1][0]


def calibrate_value_lower_offsets(
    observations: Sequence[Mapping[str, Any]],
    *,
    value_fields: Sequence[str],
    num_actions: int,
    quantile: float = 0.20,
    min_rows_per_action: int = 20,
    min_ess_per_action: float = 10.0,
) -> dict[str, Any]:
    """Fit weighted residual offsets with an ESS-based global fallback."""
    fields = tuple(str(field) for field in value_fields)
    if not fields or len(set(fields)) != len(fields):
        raise ValueError("value_fields must be unique and non-empty")
    if num_actions < 1 or min_rows_per_action < 1 or min_ess_per_action <= 0.0:
        raise ValueError("invalid value calibration thresholds")
    grouped: dict[str, list[list[tuple[float, float, str]]]] = {
        field: [[] for _ in range(num_actions)] for field in fields
    }
    for row in observations:
        field = str(row.get("field", ""))
        if field not in grouped:
            raise ValueError(f"unsupported value calibration field: {field}")
        try:
            action_id = int(row.get("action_id"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("value calibration action_id must be an integer") from exc
        if not 0 <= action_id < num_actions:
            raise ValueError("value calibration action_id is out of range")
        residual = _finite(row.get("residual"), field="lower residual")
        weight = _positive_weight(row)
        opponent = str(row.get("opponent", "")).strip()
        if not opponent:
            raise ValueError("value calibration row is missing opponent")
        grouped[field][action_id].append((residual, weight, opponent))

    result: dict[str, Any] = {}
    for field in fields:
        global_rows = [item for action_rows in grouped[field] for item in action_rows]
        if global_rows:
            global_offset = weighted_quantile(
                [row[0] for row in global_rows],
                [row[1] for row in global_rows],
                quantile,
            )
        else:
            global_offset = 0.0
        offsets = []
        action_reports = []
        for action_rows in grouped[field]:
            weights = [row[1] for row in action_rows]
            ess = _effective_sample_size(weights)
            use_action = (
                len(action_rows) >= min_rows_per_action
                and ess >= min_ess_per_action
            )
            source = action_rows if use_action else global_rows
            offset = (
                weighted_quantile(
                    [row[0] for row in source],
                    [row[1] for row in source],
                    quantile,
                )
                if source
                else 0.0
            )
            offsets.append(offset)
            action_reports.append({
                "rows": len(action_rows),
                "effective_sample_size": ess,
                "opponents": len({row[2] for row in action_rows}),
                "source": "per_action" if use_action else "global_fallback",
            })
        global_weights = [row[1] for row in global_rows]
        result[field] = {
            "quantile": float(quantile),
            "global_offset": global_offset,
            "offsets": offsets,
            "rows": len(global_rows),
            "effective_sample_size": _effective_sample_size(global_weights),
            "opponents": len({row[2] for row in global_rows}),
            "per_action": action_reports,
        }
    return {
        "schema": VALUE_LOWER_CALIBRATION_SCHEMA,
        "num_actions": int(num_actions),
        "value_fields": list(fields),
        "weight_field": EVALUATION_WEIGHT_FIELD,
        "quantile": float(quantile),
        "min_rows_per_action": int(min_rows_per_action),
        "min_ess_per_action": float(min_ess_per_action),
        "fields": result,
    }


def _masked_row_nll(row: Mapping[str, Any], temperature: float) -> float:
    logits = row["logits"]
    legal = row["legal_action_mask"]
    target = int(row["target"])
    scaled = [float(logits[index]) / temperature for index in range(len(logits))]
    legal_values = [scaled[index] for index, allowed in enumerate(legal) if allowed]
    maximum = max(legal_values)
    denominator = sum(math.exp(value - maximum) for value in legal_values)
    return math.log(denominator) + maximum - scaled[target]


def _temperature_nll(
    rows: Sequence[Mapping[str, Any]], temperature: float
) -> float:
    weighted = [(_masked_row_nll(row, temperature), _positive_weight(row)) for row in rows]
    total_weight = sum(weight for _, weight in weighted)
    return sum(loss * weight for loss, weight in weighted) / total_weight


def calibrate_response_temperature(
    rows: Sequence[Mapping[str, Any]],
    *,
    min_temperature: float = 0.25,
    max_temperature: float = 4.0,
    grid_points: int = 81,
) -> dict[str, Any]:
    """Fit temperature on legal-action masked, opponent-balanced NLL."""
    if not rows:
        return {
            "schema": RESPONSE_TEMPERATURE_SCHEMA,
            "temperature": 1.0,
            "rows": 0,
            "effective_sample_size": 0.0,
            "nll_before": None,
            "nll_after": None,
            "by_opponent": {},
        }
    low = _finite(min_temperature, field="min_temperature")
    high = _finite(max_temperature, field="max_temperature")
    if low <= 0.0 or high < low or grid_points < 3:
        raise ValueError("invalid response temperature grid")
    prepared = []
    legal_counts: Counter[str] = Counter()
    for source in rows:
        logits_raw = source.get("logits")
        legal_raw = source.get("legal_action_mask")
        if (
            not isinstance(logits_raw, Sequence)
            or isinstance(logits_raw, (str, bytes))
            or len(logits_raw) != len(OPPONENT_ACTION_LABELS)
            or not isinstance(legal_raw, Sequence)
            or isinstance(legal_raw, (str, bytes))
            or len(legal_raw) != len(OPPONENT_ACTION_LABELS)
        ):
            raise ValueError("response calibration logits or legal mask has wrong dimension")
        logits = [_finite(value, field="response logit") for value in logits_raw]
        legal = [1 if bool(value) else 0 for value in legal_raw]
        try:
            target = int(source.get("target"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("response calibration target must be an integer") from exc
        if not 0 <= target < len(legal) or not legal[target] or not any(legal):
            raise ValueError("response calibration target is absent or illegal")
        opponent = str(source.get("opponent", "")).strip()
        if not opponent:
            raise ValueError("response calibration row is missing opponent")
        row = {
            "logits": logits,
            "legal_action_mask": legal,
            "target": target,
            "opponent": opponent,
            "weight": _positive_weight(source),
        }
        prepared.append(row)
        for index, allowed in enumerate(legal):
            if allowed:
                legal_counts[OPPONENT_ACTION_LABELS[index]] += 1

    log_low = math.log(low)
    log_high = math.log(high)
    temperatures = [
        math.exp(log_low + (log_high - log_low) * index / (grid_points - 1))
        for index in range(grid_points)
    ]
    temperatures.append(1.0)
    temperatures = sorted(set(temperatures))
    scored = [
        (temperature, _temperature_nll(prepared, temperature))
        for temperature in temperatures
    ]
    best_temperature, best_nll = min(
        scored, key=lambda item: (item[1], abs(math.log(item[0])), item[0])
    )
    before = _temperature_nll(prepared, 1.0)
    by_opponent = {}
    for opponent in sorted({row["opponent"] for row in prepared}):
        subset = [row for row in prepared if row["opponent"] == opponent]
        by_opponent[opponent] = {
            "rows": len(subset),
            "effective_sample_size": _effective_sample_size(
                [row["weight"] for row in subset]
            ),
            "nll_before": _temperature_nll(subset, 1.0),
            "nll_after": _temperature_nll(subset, best_temperature),
        }
    return {
        "schema": RESPONSE_TEMPERATURE_SCHEMA,
        "temperature": best_temperature,
        "rows": len(prepared),
        "effective_sample_size": _effective_sample_size(
            [row["weight"] for row in prepared]
        ),
        "nll_before": before,
        "nll_after": best_nll,
        "grid": {
            "min": low,
            "max": high,
            "points": len(temperatures),
        },
        "legal_action_counts": dict(sorted(legal_counts.items())),
        "by_opponent": by_opponent,
    }


def build_calibration_artifact(
    calibration_phase: Mapping[str, Any],
    *,
    value_lower: Mapping[str, Any],
    response_temperature: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind fitted calibration to one frozen checkpoint and role artifact."""
    roles = calibration_phase.get("roles")
    role = roles.get(MODEL_CALIBRATION_ROLE) if isinstance(roles, Mapping) else None
    if (
        calibration_phase.get("schema") != MULTITASK_TRAINING_DATA_SCHEMA
        or calibration_phase.get("phase") != "model_calibration"
        or calibration_phase.get("opened_roles") != [MODEL_CALIBRATION_ROLE]
        or not isinstance(role, Mapping)
        or value_lower.get("schema") != VALUE_LOWER_CALIBRATION_SCHEMA
        or response_temperature.get("schema") != RESPONSE_TEMPERATURE_SCHEMA
    ):
        raise ValueError("invalid model calibration inputs")
    payload = {
        "schema": MULTITASK_CALIBRATION_ARTIFACT_SCHEMA,
        "run_id": calibration_phase.get("run_id"),
        "role_manifest_sha256": calibration_phase.get("role_manifest_sha256"),
        "checkpoint_sha256": calibration_phase.get("checkpoint_sha256"),
        "calibration_role": MODEL_CALIBRATION_ROLE,
        "calibration_artifact_sha256": role.get("provenance", {}).get(
            "artifact_sha256"
        ),
        "opponents": list(role.get("opponents", [])),
        "value_rows": len(role.get("value", [])),
        "behavior_rows": len(role.get("behavior", [])),
        "weighting": role.get("weighting"),
        "value_lower": dict(value_lower),
        "response_temperature": dict(response_temperature),
        "policy_evidence_used": False,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return {**payload, "payload_sha256": hashlib.sha256(raw).hexdigest()}
