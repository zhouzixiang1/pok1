#!/usr/bin/env python3
"""Export one frozen v3 Torch checkpoint to the stdlib runtime format."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any

from opponent_multitask_runtime_v3 import (
    OpponentMultiTaskRuntimeV3,
    RUNTIME_FORMAT,
)
from train_opponent_multitask_v3 import load_checkpoint


EXPORT_SCHEMA = "opponent_multitask_stdlib_export_v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest(value: Any, *, field: str) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return result


def _code_contract(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}
    result = {}
    for name, details in sorted(raw.items()):
        if not isinstance(details, dict):
            raise ValueError("checkpoint code artifact is malformed")
        result[str(name)] = {
            "bytes": int(details.get("bytes", -1)),
            "sha256": _digest(
                details.get("sha256"), field=f"code_artifacts.{name}.sha256"
            ),
        }
        if result[str(name)]["bytes"] < 1:
            raise ValueError("checkpoint code artifact byte count is invalid")
    return result


def build_export_payload(
    model: Any,
    checkpoint: dict[str, Any],
    *,
    checkpoint_sha256: str,
) -> dict[str, Any]:
    """Build a path-independent deterministic runtime payload."""
    checkpoint_sha256 = _digest(
        checkpoint_sha256, field="checkpoint_sha256"
    )
    metadata = model.metadata()
    hidden = dict(getattr(model, "config", {}))
    state = model.state_dict()
    weights = {}
    for name, tensor in sorted(state.items()):
        if tensor.ndim not in (1, 2):
            raise ValueError(f"runtime export does not support tensor rank: {name}")
        weights[name] = tensor.detach().cpu().tolist()
    payload = {
        "schema": EXPORT_SCHEMA,
        "format": RUNTIME_FORMAT,
        "model_metadata": metadata,
        "hidden_sizes": hidden,
        "weights": weights,
        "source": {
            "checkpoint_sha256": checkpoint_sha256,
            "checkpoint_schema": checkpoint.get("schema"),
            "role_manifest_sha256": checkpoint.get("role_manifest_sha256"),
            "training_artifact_sha256": checkpoint.get(
                "training_artifact_sha256"
            ),
            "code_artifacts": _code_contract(checkpoint.get("code_artifacts")),
        },
        "export_contract": {
            "dropout_mode": "evaluation_disabled",
            "numeric_weights": "float32_values_as_json_numbers",
            "runtime_tool_sha256": _sha256(
                Path(sys.modules["opponent_multitask_runtime_v3"].__file__).resolve()
            ),
            "export_tool_sha256": _sha256(Path(__file__).resolve()),
        },
        "deployment_policy_value": False,
        "strength_evidence": False,
    }
    OpponentMultiTaskRuntimeV3(payload)
    return payload


def write_export(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    path = path.resolve()
    if path.exists():
        raise ValueError(f"output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    if temporary.exists():
        raise ValueError(f"temporary output already exists: {temporary}")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(
                payload,
                handle,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    checkpoint_path = args.checkpoint.resolve()
    try:
        checkpoint_sha256 = _sha256(checkpoint_path)
        model, checkpoint = load_checkpoint(checkpoint_path, device="cpu")
        payload = build_export_payload(
            model,
            checkpoint,
            checkpoint_sha256=checkpoint_sha256,
        )
        artifact = write_export(args.output, payload)
        loaded = OpponentMultiTaskRuntimeV3.load(artifact["path"])
        if loaded is None:
            raise RuntimeError("written stdlib model failed strict reload")
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps({
        **artifact,
        "parameters": payload["model_metadata"]["parameters"],
        "cross_encoder": payload["model_metadata"]["cross_encoder"],
        "deployment_policy_value": False,
        "strength_evidence": False,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
