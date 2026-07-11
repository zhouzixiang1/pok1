"""Pure-Python validation and application of v4 outcome calibration."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any


CALIBRATION_SCHEMA = "match_outcome_probability_calibration_v1"
CALIBRATION_METHOD = "global_positive_scale_and_bias_v1"


def _finite(value: Any, *, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be finite") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def _sigmoid(value: float) -> float:
    if value >= 0.0:
        exponent = math.exp(-min(value, 60.0))
        return 1.0 / (1.0 + exponent)
    exponent = math.exp(max(value, -60.0))
    return exponent / (1.0 + exponent)


def calibration_parameters(payload: Any) -> tuple[float, float]:
    if not isinstance(payload, dict):
        raise ValueError("match outcome calibration must be an object")
    if (
        payload.get("schema") != CALIBRATION_SCHEMA
        or payload.get("method") != CALIBRATION_METHOD
    ):
        raise ValueError("unsupported match outcome calibration")
    scale = _finite(payload.get("scale"), field="calibration scale")
    bias = _finite(payload.get("bias"), field="calibration bias")
    if scale <= 0.0:
        raise ValueError("calibration scale must be positive")
    return scale, bias


def _digest(value: Any, *, field: str) -> str:
    digest = str(value or "").strip().lower()
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return digest


def calibration_payload_sha256(payload: Any) -> str:
    """Hash a calibration payload without its self-referential digest."""
    if not isinstance(payload, dict):
        raise ValueError("match outcome calibration must be an object")
    unsigned = dict(payload)
    unsigned.pop("payload_sha256", None)
    try:
        raw = json.dumps(
            unsigned,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError) as exc:
        raise ValueError("match outcome calibration is not canonical JSON") from exc
    return hashlib.sha256(raw).hexdigest()


def validate_calibration_artifact(
    payload: Any,
    *,
    checkpoint_sha256: str | None = None,
    model_format: str | None = None,
) -> dict[str, Any]:
    """Validate provenance before calibration enters an exported runtime."""
    calibration_parameters(payload)
    assert isinstance(payload, dict)
    observed_payload_sha256 = _digest(
        payload.get("payload_sha256"), field="calibration.payload_sha256"
    )
    if calibration_payload_sha256(payload) != observed_payload_sha256:
        raise ValueError("match outcome calibration payload hash changed")
    observed_checkpoint = _digest(
        payload.get("checkpoint_sha256"),
        field="calibration.checkpoint_sha256",
    )
    _digest(
        payload.get("role_manifest_sha256"),
        field="calibration.role_manifest_sha256",
    )
    _digest(
        payload.get("model_calibration_artifact_sha256"),
        field="calibration.model_calibration_artifact_sha256",
    )
    if checkpoint_sha256 is not None and observed_checkpoint != _digest(
        checkpoint_sha256, field="checkpoint_sha256"
    ):
        raise ValueError("match outcome calibration checkpoint does not match model")
    if model_format is not None and payload.get("model_format") != model_format:
        raise ValueError("match outcome calibration model format does not match")
    opponents = payload.get("model_calibration_opponents")
    if (
        not isinstance(opponents, list)
        or not opponents
        or any(not isinstance(name, str) or not name.strip() for name in opponents)
    ):
        raise ValueError("match outcome calibration opponents are invalid")
    if not isinstance(payload.get("source_collection_complete"), bool):
        raise ValueError("match outcome calibration collection state is invalid")
    if (
        payload.get("deployment_policy_value") is not False
        or payload.get("strength_evidence") is not False
    ):
        raise ValueError("match outcome calibration makes unsupported claims")
    return dict(payload)


def apply_calibration(
    logits: list[float], payload: dict[str, Any]
) -> dict[str, list[float]]:
    scale, bias = calibration_parameters(payload)
    raw = [_finite(value, field="outcome logit") for value in logits]
    calibrated = [scale * value + bias for value in raw]
    return {
        "logits": calibrated,
        "probabilities": [_sigmoid(value) for value in calibrated],
    }
