"""Content-bound game identities for route-B checkpoints and shards."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .strict_io import read_regular_bytes


GAME_IDENTITY_SCHEMA = "route-b-game-identity-v1"


def canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def payload_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def file_sha256(path: str | Path) -> str:
    """Hash one stable O_NOFOLLOW regular-file descriptor."""

    return hashlib.sha256(read_regular_bytes(path)).hexdigest()


def require_sha256(value: object, context: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{context} must be an exact string")
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{context} must be 64 lowercase SHA-256 hex digits")
    return value


def game_identity_sha256(game: object) -> str:
    """Read an exact identity exposed by a versioned game implementation."""

    identity = getattr(game, "identity_sha256", None)
    if not callable(identity):
        raise TypeError(
            "solver games must expose identity_sha256() bound to complete semantics"
        )
    return require_sha256(identity(), "game identity")
