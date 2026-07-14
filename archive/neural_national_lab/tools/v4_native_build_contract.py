#!/usr/bin/env python3
"""Hash-bound native integration inputs shared by the v4 gate and builder."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
TOOLS = Path(__file__).resolve().parent
VERSIONS = ROOT / "bots" / "neural_national_lab" / "versions"
TRANSPORT_NAME = "v151_national_v150_temporal_multitask_shadow_tcp"
TRANSPORT_DIR = VERSIONS / TRANSPORT_NAME
CONTRACT_SCHEMA = "opponent_multitask_v4_native_build_contract_v1"
EXPECTED_TRANSPORT_SHA256 = (
    "0e7d3f42e2cc82417cef96b2c902a58a646373ce15e80aceb4d1b441c554749c"
)
EXPECTED_TRANSPORT_BOT_SHA256 = (
    "8bf7003cb8bd38b3cd8d1bdc3f5c2d169bb94ebe61f02e0adf21ccdda7a7ebd2"
)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _file_contract(raw: bytes) -> dict[str, Any]:
    return {"bytes": len(raw), "sha256": _sha256(raw)}


def _transport_snapshot() -> tuple[dict[str, Any], bytes]:
    digest = hashlib.sha256()
    total_bytes = 0
    files = 0
    national_bot = None
    for path in sorted(
        item for item in TRANSPORT_DIR.rglob("*")
        if item.is_file()
        and "__pycache__" not in item.parts
        and item.suffix.lower() != ".pyc"
    ):
        raw = path.read_bytes()
        relative = str(path.relative_to(TRANSPORT_DIR))
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(raw)
        digest.update(b"\0")
        total_bytes += len(raw)
        files += 1
        if relative == "national_bot.py":
            national_bot = raw
    if national_bot is None:
        raise ValueError("fixed v151 transport national_bot.py is missing")
    directory_sha256 = digest.hexdigest()
    if (
        directory_sha256 != EXPECTED_TRANSPORT_SHA256
        or _sha256(national_bot) != EXPECTED_TRANSPORT_BOT_SHA256
    ):
        raise ValueError("fixed v151 transport snapshot changed")
    return {
        "name": TRANSPORT_NAME,
        "files": files,
        "bytes": total_bytes,
        "sha256": directory_sha256,
        "national_bot.py": _file_contract(national_bot),
    }, national_bot


def snapshot_native_build_inputs(
) -> tuple[dict[str, Any], dict[str, bytes]]:
    paths = {
        "v4_builder": TOOLS / "build_opponent_multitask_v4_native_candidate.py",
        "v3_patch_helper": TOOLS / "build_opponent_multitask_v3_native_candidate.py",
        "national_validator_source": ROOT / "sever" / "engine" / "validator.py",
        "opponent_response_schema_source": TOOLS / "opponent_response_schema.py",
        "contract_helper": Path(__file__).resolve(),
    }
    raw = {name: path.read_bytes() for name, path in paths.items()}
    transport, transport_bot = _transport_snapshot()
    contract = {
        "schema": CONTRACT_SCHEMA,
        "artifacts": {
            name: _file_contract(payload) for name, payload in sorted(raw.items())
        },
        "transport": transport,
    }
    sources = {
        "transport_national_bot": transport_bot,
        "national_validator": raw["national_validator_source"],
        "opponent_response_schema": raw["opponent_response_schema_source"],
    }
    return contract, sources


def current_native_build_contract() -> dict[str, Any]:
    return snapshot_native_build_inputs()[0]
