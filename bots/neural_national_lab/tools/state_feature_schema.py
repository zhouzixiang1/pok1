"""Shared state-feature schema contract for training and stdlib runtimes."""
from __future__ import annotations

from typing import Any

from hand_context_features import (
    HAND_CONTEXT_DIM,
    HAND_CONTEXT_SCHEMA,
    encode_hand_context,
)
from decision_context_features import (
    DECISION_CONTEXT_DIM,
    DECISION_CONTEXT_SCHEMA,
    encode_decision_context,
)


LEGACY_STATE_SCHEMA = "legacy48_v1"
HERO_HAND_STATE_SCHEMA = "legacy48_plus_hero_hand_v1"
PUBLIC_DECISION_STATE_SCHEMA = "legacy48_plus_public_decision_v1"
HERO_HAND_PUBLIC_DECISION_STATE_SCHEMA = (
    "legacy48_plus_hero_hand_public_decision_v1"
)
STATE_FEATURE_SCHEMAS = (
    LEGACY_STATE_SCHEMA,
    HERO_HAND_STATE_SCHEMA,
    PUBLIC_DECISION_STATE_SCHEMA,
    HERO_HAND_PUBLIC_DECISION_STATE_SCHEMA,
)
LEGACY_PRIVATE_STATE_INDICES = tuple(range(5, 10))


def extend_state_features(
    base_features: list[float],
    request: dict[str, Any],
    *,
    state: dict[str, Any] | None = None,
    legal_mask: Any = None,
    schema: str,
) -> list[float]:
    base = [float(value) for value in base_features]
    if schema == LEGACY_STATE_SCHEMA:
        return base
    if schema == HERO_HAND_STATE_SCHEMA:
        return base + encode_hand_context(request)
    if schema == PUBLIC_DECISION_STATE_SCHEMA:
        return base + encode_decision_context(request, state, legal_mask)
    if schema == HERO_HAND_PUBLIC_DECISION_STATE_SCHEMA:
        return (
            base
            + encode_hand_context(request)
            + encode_decision_context(request, state, legal_mask)
        )
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
    if schema == PUBLIC_DECISION_STATE_SCHEMA:
        return LEGACY_PRIVATE_STATE_INDICES
    if schema == HERO_HAND_PUBLIC_DECISION_STATE_SCHEMA:
        return LEGACY_PRIVATE_STATE_INDICES + tuple(
            range(base_dim, base_dim + HAND_CONTEXT_DIM)
        )
    raise ValueError(f"unsupported state feature schema: {schema}")


def feature_schema_metadata(*, schema: str, base_dim: int) -> dict[str, Any]:
    private = private_state_indices(schema=schema, base_dim=base_dim)
    hand_dim = (
        HAND_CONTEXT_DIM
        if schema in {
            HERO_HAND_STATE_SCHEMA, HERO_HAND_PUBLIC_DECISION_STATE_SCHEMA
        }
        else 0
    )
    decision_dim = (
        DECISION_CONTEXT_DIM
        if schema in {
            PUBLIC_DECISION_STATE_SCHEMA,
            HERO_HAND_PUBLIC_DECISION_STATE_SCHEMA,
        }
        else 0
    )
    return {
        "schema": schema,
        "base_dim": int(base_dim),
        "hand_context_schema": (
            HAND_CONTEXT_SCHEMA if hand_dim else None
        ),
        "hand_context_dim": hand_dim,
        "decision_context_schema": (
            DECISION_CONTEXT_SCHEMA if decision_dim else None
        ),
        "decision_context_dim": decision_dim,
        "state_dim": int(base_dim) + hand_dim + decision_dim,
        "response_private_state_masked": list(private),
    }
