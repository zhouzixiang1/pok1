"""Shared state-feature schema contract for training and stdlib runtimes."""
from __future__ import annotations

from typing import Any

from hand_context_features import (
    HAND_CONTEXT_DIM,
    HAND_CONTEXT_SCHEMA,
    encode_hand_context,
)


LEGACY_STATE_SCHEMA = "legacy48_v1"
HERO_HAND_STATE_SCHEMA = "legacy48_plus_hero_hand_v1"
STATE_FEATURE_SCHEMAS = (LEGACY_STATE_SCHEMA, HERO_HAND_STATE_SCHEMA)
LEGACY_PRIVATE_STATE_INDICES = tuple(range(5, 10))


def extend_state_features(
    base_features: list[float],
    request: dict[str, Any],
    *,
    schema: str,
) -> list[float]:
    base = [float(value) for value in base_features]
    if schema == LEGACY_STATE_SCHEMA:
        return base
    if schema == HERO_HAND_STATE_SCHEMA:
        return base + encode_hand_context(request)
    raise ValueError(f"unsupported state feature schema: {schema}")


def private_state_indices(*, schema: str, base_dim: int) -> tuple[int, ...]:
    if base_dim <= max(LEGACY_PRIVATE_STATE_INDICES):
        raise ValueError("base state dimension does not contain private card features")
    if schema == LEGACY_STATE_SCHEMA:
        return LEGACY_PRIVATE_STATE_INDICES
    if schema == HERO_HAND_STATE_SCHEMA:
        return LEGACY_PRIVATE_STATE_INDICES + tuple(
            range(base_dim, base_dim + HAND_CONTEXT_DIM)
        )
    raise ValueError(f"unsupported state feature schema: {schema}")


def feature_schema_metadata(*, schema: str, base_dim: int) -> dict[str, Any]:
    private = private_state_indices(schema=schema, base_dim=base_dim)
    hand_dim = HAND_CONTEXT_DIM if schema == HERO_HAND_STATE_SCHEMA else 0
    return {
        "schema": schema,
        "base_dim": int(base_dim),
        "hand_context_schema": (
            HAND_CONTEXT_SCHEMA if schema == HERO_HAND_STATE_SCHEMA else None
        ),
        "hand_context_dim": hand_dim,
        "state_dim": int(base_dim) + hand_dim,
        "response_private_state_masked": list(private),
    }
