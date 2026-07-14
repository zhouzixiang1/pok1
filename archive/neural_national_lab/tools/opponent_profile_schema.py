"""Versioned opponent-profile features shared by training and inference."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
from typing import Any


OPPONENT_PROFILE_SCHEMA = "opponent_profile_features_v1"
OPPONENT_PROFILE_FIELDS = (
    "confidence",
    "actions_total_norm",
    "fold_rate",
    "call_rate",
    "check_rate",
    "raise_rate",
    "allin_rate",
    "aggression",
    "preflop_actions_norm",
    "preflop_raise_rate",
    "postflop_actions_norm",
    "postflop_raise_rate",
)
OPPONENT_PROFILE_DIM = len(OPPONENT_PROFILE_FIELDS)


def _unit(value: Any, *, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be finite and in [0, 1]") from exc
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError(f"{field} must be finite and in [0, 1]")
    return number


def _profile_mappings(row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    profiles = []
    profile = row.get("opponent_profile")
    if isinstance(profile, Mapping):
        profiles.append(profile)
    request = row.get("request")
    if isinstance(request, Mapping):
        profile = request.get("opponent_profile")
        if isinstance(profile, Mapping):
            profiles.append(profile)
    return profiles


def encode_opponent_profile(row: Mapping[str, Any]) -> list[float]:
    """Return the canonical 12-d profile and reject collector/runtime drift."""
    derived = [
        [
            _unit(profile.get(name, 0.0), field=f"opponent_profile.{name}")
            for name in OPPONENT_PROFILE_FIELDS
        ]
        for profile in _profile_mappings(row)
    ]
    if len(derived) > 1 and any(
        abs(first - second) > 1.0e-8
        for first, second in zip(derived[0], derived[1], strict=True)
    ):
        raise ValueError("row and request opponent profiles disagree")
    expected = derived[0] if derived else None
    raw = row.get("opponent_profile_features")
    if raw is None:
        return expected if expected is not None else [0.0] * OPPONENT_PROFILE_DIM
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError("opponent_profile_features must be a numeric sequence")
    if len(raw) != OPPONENT_PROFILE_DIM:
        raise ValueError("opponent_profile_features has the wrong dimension")
    encoded = [
        _unit(value, field=f"opponent_profile_features[{index}]")
        for index, value in enumerate(raw)
    ]
    if expected is not None and any(
        abs(actual - derived) > 1.0e-8
        for actual, derived in zip(encoded, expected, strict=True)
    ):
        raise ValueError(
            "opponent_profile_features disagrees with the request profile"
        )
    return encoded


def opponent_profile_metadata() -> dict[str, Any]:
    return {
        "schema": OPPONENT_PROFILE_SCHEMA,
        "dim": OPPONENT_PROFILE_DIM,
        "fields": list(OPPONENT_PROFILE_FIELDS),
        "range": [0.0, 1.0],
        "missing_profile": "all_zero_unknown",
    }
