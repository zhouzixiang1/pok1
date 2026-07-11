#!/usr/bin/env python3
"""Export one frozen v4 checkpoint to its stdlib outcome-aware runtime."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

import export_opponent_multitask_v3 as v3_export
from opponent_multitask_runtime_v4 import (
    OpponentMultiTaskRuntimeV4,
    RUNTIME_FORMAT,
)
from train_opponent_multitask_v4 import load_checkpoint


EXPORT_SCHEMA = "opponent_multitask_stdlib_export_v2_outcome"


def build_export_payload(
    model: Any,
    checkpoint: dict[str, Any],
    *,
    checkpoint_sha256: str,
) -> dict[str, Any]:
    checkpoint_sha256 = v3_export._digest(
        checkpoint_sha256, field="checkpoint_sha256"
    )
    weights = {}
    for name, tensor in sorted(model.state_dict().items()):
        if tensor.ndim not in (1, 2):
            raise ValueError(f"runtime export does not support tensor rank: {name}")
        weights[name] = tensor.detach().cpu().tolist()
    payload = {
        "schema": EXPORT_SCHEMA,
        "format": RUNTIME_FORMAT,
        "model_metadata": model.metadata(),
        "hidden_sizes": dict(model.config),
        "weights": weights,
        "source": {
            "checkpoint_sha256": checkpoint_sha256,
            "checkpoint_schema": checkpoint.get("schema"),
            "role_manifest_sha256": checkpoint.get("role_manifest_sha256"),
            "training_artifact_sha256": checkpoint.get(
                "training_artifact_sha256"
            ),
            "code_artifacts": v3_export._code_contract(
                checkpoint.get("code_artifacts")
            ),
        },
        "export_contract": {
            "dropout_mode": "evaluation_disabled",
            "numeric_weights": "float32_values_as_json_numbers",
            "runtime_tool_sha256": v3_export._sha256(
                Path(
                    sys.modules["opponent_multitask_runtime_v4"].__file__
                ).resolve()
            ),
            "export_tool_sha256": v3_export._sha256(Path(__file__).resolve()),
        },
        "deployment_policy_value": False,
        "strength_evidence": False,
    }
    OpponentMultiTaskRuntimeV4(payload)
    return payload


def write_export(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    return v3_export.write_export(path, payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    checkpoint_path = args.checkpoint.resolve()
    try:
        checkpoint_sha256 = v3_export._sha256(checkpoint_path)
        model, checkpoint = load_checkpoint(checkpoint_path, device="cpu")
        payload = build_export_payload(
            model,
            checkpoint,
            checkpoint_sha256=checkpoint_sha256,
        )
        artifact = write_export(args.output, payload)
        loaded = OpponentMultiTaskRuntimeV4.load(artifact["path"])
        if loaded is None:
            raise RuntimeError("written v4 stdlib model failed strict reload")
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps({
        **artifact,
        "parameters": payload["model_metadata"]["parameters"],
        "cross_encoder": payload["model_metadata"]["cross_encoder"],
        "match_outcome_head_schema": payload["model_metadata"][
            "match_outcome_head_schema"
        ],
        "deployment_policy_value": False,
        "strength_evidence": False,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
