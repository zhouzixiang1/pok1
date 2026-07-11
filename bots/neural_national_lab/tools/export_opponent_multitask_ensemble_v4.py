#!/usr/bin/env python3
"""Export a protected calibrated v4 ensemble and frozen win-first policy."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any

from calibrate_opponent_multitask_v4_ensemble import load_calibrated_ensemble
from export_opponent_multitask_v4 import build_export_payload, write_export
from match_outcome_calibration import validate_calibration_artifact
from opponent_multitask_ensemble_runtime_v3 import _canonical_sha256, _digest
from opponent_multitask_ensemble_runtime_v4 import (
    BUNDLE_SCHEMA,
    ENSEMBLE_FORMAT,
    FORMAL_COLLECTION_PASSES,
    FORMAL_UNCERTAINTY_STD_WEIGHT,
    OpponentMultiTaskEnsembleRuntimeV4,
    RUNTIME_MODULE_FILENAMES,
    calibration_projection_from_artifact,
    calibration_projection_from_bundle,
    calibration_projection_sha256,
)
from opponent_multitask_model_v4 import MODEL_FORMAT
from role_dataset_access import RoleDatasetAccess
from select_opponent_multitask_v4_policy import verify_policy_artifacts
from train_opponent_multitask_v3 import _sha256
from train_opponent_multitask_v4 import load_checkpoint
from win_first_policy_v4 import (
    OUTCOME_AGGREGATION_METHOD,
    normalize_policy,
)


def _finite(value: Any, *, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be finite") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def runtime_module_artifacts() -> dict[str, dict[str, Any]]:
    root = Path(__file__).resolve().parent
    result = {}
    for name in RUNTIME_MODULE_FILENAMES:
        path = root / name
        if not path.is_file():
            raise ValueError(f"v4 runtime module is missing: {name}")
        result[name] = {
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
    return result


def _member_outcome_calibration(
    calibrated: dict[str, Any], member: dict[str, Any], index: int
) -> dict[str, Any]:
    payload = member.get("outcome_calibration")
    if payload is None:
        rows = calibrated.get("outcome_calibrations")
        if isinstance(rows, list) and index < len(rows):
            row = rows[index]
            payload = row.get("payload") if isinstance(row, dict) else None
            if payload is None and isinstance(row, dict):
                payload = row
    if not isinstance(payload, dict):
        raise ValueError("v4 ensemble member has no outcome calibration")
    return validate_calibration_artifact(
        payload,
        checkpoint_sha256=member.get("checkpoint_sha256"),
        model_format=MODEL_FORMAT,
    )


def verify_calibrated_members(
    calibrated: dict[str, Any], *, formal: bool
) -> list[dict[str, Any]]:
    """Recheck member and role provenance before building runtime payloads."""
    members = calibrated.get("members")
    if not isinstance(members, list) or not members:
        raise ValueError("v4 calibrated ensemble has no members")
    if formal and len(members) < 3:
        raise ValueError("formal v4 ensemble export requires at least three seeds")
    checkpoints = [
        _digest(member.get("checkpoint_sha256"), field="member checkpoint")
        for member in members
    ]
    if len(set(checkpoints)) != len(checkpoints):
        raise ValueError("v4 calibrated ensemble reuses a checkpoint")
    seeds = [member.get("seed") for member in members]
    if formal and (
        any(isinstance(seed, bool) or not isinstance(seed, int) for seed in seeds)
        or len(set(seeds)) != len(seeds)
    ):
        raise ValueError("formal v4 ensemble requires distinct integer seeds")

    calibrations = [
        _member_outcome_calibration(calibrated, member, index)
        for index, member in enumerate(members)
    ]
    for member, item in zip(members, calibrations, strict=True):
        if (
            item.get("calibration_role") != "model_calibration"
            or item.get("policy_evidence_used") is not False
            or item.get("member_seed") != member.get("seed")
        ):
            raise ValueError("v4 outcome calibration is not model-role-only")
    ordered_calibrations = calibrated.get("outcome_calibrations")
    if (
        not isinstance(ordered_calibrations, list)
        or len(ordered_calibrations) != len(calibrations)
    ):
        raise ValueError("v4 outcome calibration ordering is missing")
    for index, (member, embedded, ordered) in enumerate(
        zip(members, calibrations, ordered_calibrations, strict=True)
    ):
        validated = validate_calibration_artifact(
            ordered,
            checkpoint_sha256=member.get("checkpoint_sha256"),
            model_format=MODEL_FORMAT,
        )
        if validated["payload_sha256"] != embedded["payload_sha256"]:
            raise ValueError(
                f"v4 outcome calibration ordering changed at member {index}"
            )
    signatures = {
        (
            item.get("run_id"),
            item["role_manifest_sha256"],
            item["model_calibration_artifact_sha256"],
            tuple(item["model_calibration_opponents"]),
            item["source_collection_complete"],
        )
        for item in calibrations
    }
    if len(signatures) != 1:
        raise ValueError("v4 outcome calibrations do not share one role")
    (
        calibration_run_id,
        role_manifest_sha256,
        role_artifact_sha256,
        role_opponents,
        source_complete,
    ) = next(iter(signatures))

    base = calibrated.get("calibration")
    if not isinstance(base, dict):
        raise ValueError("v4 ensemble base calibration is missing")
    if (
        base.get("run_id") != calibration_run_id
        or base.get("role_manifest_sha256") != role_manifest_sha256
        or base.get("calibration_artifact_sha256") != role_artifact_sha256
        or tuple(base.get("opponents") or ()) != role_opponents
    ):
        raise ValueError("v4 value and outcome calibrations use different roles")
    ensemble = calibrated.get("ensemble")
    if (
        not isinstance(ensemble, dict)
        or ensemble.get("role_manifest_sha256") != role_manifest_sha256
    ):
        raise ValueError("v4 ensemble manifest and calibration role disagree")
    if formal and (
        source_complete is not True
        or ensemble.get("source_collection_complete") is not True
        or any(member.get("source_collection_complete") is not True for member in members)
    ):
        raise ValueError("formal v4 ensemble members require complete source data")
    if formal and (
        _finite(
            calibrated.get("uncertainty_std_weight"),
            field="value uncertainty std weight",
        )
        != FORMAL_UNCERTAINTY_STD_WEIGHT
        or _finite(
            calibrated.get("outcome_uncertainty_std_weight"),
            field="outcome uncertainty std weight",
        )
        != FORMAL_UNCERTAINTY_STD_WEIGHT
    ):
        raise ValueError("formal v4 ensemble uncertainty weights must remain 1.0")

    result = []
    for member, calibration in zip(members, calibrations, strict=True):
        normalized = dict(member)
        normalized["outcome_calibration"] = calibration
        result.append(normalized)
    return result


def policy_for_export(
    policy: dict[str, Any], *, formal: bool
) -> dict[str, Any]:
    if formal:
        return dict(policy)
    return {
        **policy,
        "selected_policy": None,
        "selected_policy_sha256": None,
        "selection_passed": False,
    }


def build_bundle_payload(
    member_payloads: list[dict[str, Any]],
    *,
    calibrated: dict[str, Any],
    policy: dict[str, Any],
    source: dict[str, Any],
) -> dict[str, Any]:
    checkpoint_sha256 = [
        _digest(
            member.get("source", {}).get("checkpoint_sha256"),
            field="member checkpoint_sha256",
        )
        for member in member_payloads
    ]
    calibrated_members = calibrated.get("members")
    if (
        not isinstance(calibrated_members, list)
        or len(calibrated_members) != len(member_payloads)
    ):
        raise ValueError("v4 calibrated member ordering is missing")
    member_seed = [member.get("seed") for member in calibrated_members]
    if (
        any(isinstance(seed, bool) or not isinstance(seed, int) for seed in member_seed)
        or len(set(member_seed)) != len(member_seed)
    ):
        raise ValueError("v4 ensemble member seeds must be distinct integers")
    outcome_calibrations = [
        validate_calibration_artifact(
            member.get("outcome_calibration"),
            checkpoint_sha256=checkpoint,
            model_format=MODEL_FORMAT,
        )
        for member, checkpoint in zip(
            member_payloads, checkpoint_sha256, strict=True
        )
    ]
    selected_policy = policy.get("selected_policy")
    normalized_policy = normalize_policy(selected_policy)
    if selected_policy != normalized_policy:
        raise ValueError("v4 selected policy is not canonical")
    selection_passed = policy.get("selection_passed") is True
    if selection_passed != (normalized_policy is not None):
        raise ValueError("v4 selected-policy status is inconsistent")

    role_signature = {
        "run_id": outcome_calibrations[0].get("run_id"),
        "role_manifest_sha256": outcome_calibrations[0][
            "role_manifest_sha256"
        ],
        "model_calibration_artifact_sha256": outcome_calibrations[0][
            "model_calibration_artifact_sha256"
        ],
        "model_calibration_opponents": outcome_calibrations[0][
            "model_calibration_opponents"
        ],
        "source_collection_complete": outcome_calibrations[0][
            "source_collection_complete"
        ],
    }
    outcome_uncertainty = _finite(
        calibrated.get("outcome_uncertainty_std_weight"),
        field="outcome uncertainty std weight",
    )
    if outcome_uncertainty < 0.0:
        raise ValueError("outcome uncertainty std weight must be nonnegative")
    original_calibration = calibrated.get("calibration")
    if not isinstance(original_calibration, dict):
        raise ValueError("v4 original calibration artifact is missing")
    original_projection = calibration_projection_from_artifact(
        original_calibration
    )
    runtime_calibration = {
        "payload_sha256": calibrated["calibration_payload_sha256"],
        "member_seed": member_seed,
        "member_checkpoint_sha256": checkpoint_sha256,
        "lower_quantile": calibrated["lower_quantile"],
        "uncertainty_std_weight": calibrated[
            "uncertainty_std_weight"
        ],
        "clips": calibrated["clips"],
        "offsets": calibrated["offsets"],
        "response_temperature": calibrated["response_temperature"],
        "outcome_aggregation": OUTCOME_AGGREGATION_METHOD,
        "outcome_uncertainty_std_weight": outcome_uncertainty,
        "outcome_calibration_payload_sha256": [
            item["payload_sha256"] for item in outcome_calibrations
        ],
        **role_signature,
        "original_calibration_artifact": original_calibration,
        "original_calibration_file_sha256": calibrated[
            "calibration_file_sha256"
        ],
    }
    runtime_projection = calibration_projection_from_bundle(
        runtime_calibration
    )
    if runtime_projection != original_projection:
        raise ValueError("v4 runtime calibration differs from original artifact")
    projection_sha256 = calibration_projection_sha256(original_projection)
    runtime_calibration["calibration_projection_sha256"] = projection_sha256
    payload = {
        "schema": BUNDLE_SCHEMA,
        "format": ENSEMBLE_FORMAT,
        "members": member_payloads,
        "member_payload_sha256": [
            _canonical_sha256(member) for member in member_payloads
        ],
        "calibration": runtime_calibration,
        "selected_policy": normalized_policy,
        "source": {
            **source,
            "calibration_payload_sha256": original_calibration[
                "payload_sha256"
            ],
            "calibration_file_sha256": calibrated[
                "calibration_file_sha256"
            ],
            "calibration_projection_sha256": projection_sha256,
            "selected_policy_sha256": policy.get("selected_policy_sha256"),
            "policy_selection_passed": selection_passed,
            "deployment_policy_value": False,
            "strength_evidence": False,
        },
        "export_contract": {
            "bundle_tool_sha256": _sha256(Path(__file__).resolve()),
            "runtime_tool_sha256": _sha256(
                Path(
                    sys.modules[
                        "opponent_multitask_ensemble_runtime_v4"
                    ].__file__
                ).resolve()
            ),
            "member_payload_hash": "canonical_json_sha256",
            "outcome_aggregation": OUTCOME_AGGREGATION_METHOD,
            "copied_tool_modules": runtime_module_artifacts(),
        },
        "deployment_policy_value": False,
        "strength_evidence": False,
    }
    OpponentMultiTaskEnsembleRuntimeV4(payload)
    return payload


def canonical_bundle_bytes(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        + b"\n"
    )


def bundle_artifact_binding(
    payload: dict[str, Any], *, raw: bytes | None = None
) -> dict[str, Any]:
    calibration = payload.get("calibration")
    source = payload.get("source")
    member_hashes = payload.get("member_payload_sha256")
    if (
        payload.get("schema") != BUNDLE_SCHEMA
        or payload.get("format") != ENSEMBLE_FORMAT
        or not isinstance(calibration, dict)
        or not isinstance(source, dict)
        or not isinstance(member_hashes, list)
        or not member_hashes
        or not isinstance(
            calibration.get("member_checkpoint_sha256"), list
        )
        or len(calibration["member_checkpoint_sha256"]) != len(member_hashes)
        or not isinstance(
            calibration.get("outcome_calibration_payload_sha256"), list
        )
        or len(calibration["outcome_calibration_payload_sha256"])
        != len(member_hashes)
    ):
        raise ValueError("v4 bundle binding is incomplete")
    canonical = canonical_bundle_bytes(payload)
    if raw is not None and raw != canonical:
        raise ValueError("v4 bundle bytes are not canonical exporter output")
    serialized = canonical if raw is None else raw
    return {
        "bundle_bytes": len(serialized),
        "bundle_sha256": hashlib.sha256(serialized).hexdigest(),
        "member_payload_sha256": [
            _digest(value, field="member payload sha256")
            for value in member_hashes
        ],
        "ensemble_manifest_sha256": _digest(
            source.get("ensemble_manifest_sha256"),
            field="ensemble_manifest_sha256",
        ),
        "member_checkpoint_sha256": [
            _digest(value, field="member checkpoint sha256")
            for value in calibration.get("member_checkpoint_sha256") or []
        ],
        "outcome_calibration_payload_sha256": [
            _digest(value, field="outcome calibration sha256")
            for value in calibration.get(
                "outcome_calibration_payload_sha256"
            ) or []
        ],
    }


def build_verified_bundle_payload(
    *,
    calibrated: dict[str, Any],
    policy: dict[str, Any],
    dataset: RoleDatasetAccess,
    run_id: str,
    formal: bool,
) -> dict[str, Any]:
    """Rebuild the only accepted bundle directly from verified artifacts."""
    members = verify_calibrated_members(calibrated, formal=formal)
    frozen_policy = policy_for_export(policy, formal=formal)
    member_payloads = []
    for member in members:
        checkpoint_path = Path(member["checkpoint_path"]).resolve()
        checkpoint_sha256 = _digest(
            member.get("checkpoint_sha256"), field="member checkpoint"
        )
        if _sha256(checkpoint_path) != checkpoint_sha256:
            raise ValueError("v4 member checkpoint file changed before export")
        model, checkpoint = load_checkpoint(
            checkpoint_path, device="cpu"
        )
        member_payloads.append(build_export_payload(
            model,
            checkpoint,
            checkpoint_sha256=member["checkpoint_sha256"],
            outcome_calibration=member["outcome_calibration"],
        ))
        if _sha256(checkpoint_path) != checkpoint_sha256:
            raise ValueError("v4 member checkpoint file changed during export")
    runtime_context = dataset.runtime_context_contract()
    return build_bundle_payload(
        member_payloads,
        calibrated=calibrated,
        policy=frozen_policy,
        source={
            "run_id": run_id,
            "role_manifest_sha256": dataset.manifest_sha256,
            "ensemble_manifest_sha256": calibrated[
                "ensemble_manifest_sha256"
            ],
            "calibration_artifact_manifest_sha256": calibrated[
                "artifact_manifest_sha256"
            ],
            "calibration_file_sha256": calibrated[
                "calibration_file_sha256"
            ],
            "calibration_report_sha256": calibrated[
                "calibration_report_sha256"
            ],
            "calibration_payload_sha256": calibrated[
                "calibration_payload_sha256"
            ],
            "policy_candidate_sha256": frozen_policy["candidate_sha256"],
            "policy_evaluation_sha256": frozen_policy["evaluation_sha256"],
            "policy_result_sha256": frozen_policy["result_sha256"],
            "policy_artifact_manifest_sha256": frozen_policy[
                "artifact_manifest_sha256"
            ],
            "source_collection_complete": dataset.manifest.get(
                "source_collection_complete"
            ),
            "source_completed_passes": dataset.manifest.get(
                "source_completed_passes"
            ),
            "source_requested_passes": dataset.manifest.get(
                "source_requested_passes"
            ),
            **runtime_context,
        },
    )


def verify_exact_bundle(
    path: Path, expected: dict[str, Any]
) -> tuple[dict[str, Any], bytes, dict[str, Any]]:
    raw = path.resolve().read_bytes()
    expected_raw = canonical_bundle_bytes(expected)
    if raw != expected_raw:
        raise ValueError("v4 bundle does not match deterministic exporter output")
    try:
        actual = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("v4 bundle is not valid JSON") from exc
    if actual != expected:
        raise ValueError("v4 bundle payload changed")
    OpponentMultiTaskEnsembleRuntimeV4(actual)
    return actual, raw, bundle_artifact_binding(actual, raw=raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration-dir", required=True, type=Path)
    parser.add_argument("--policy-dir", required=True, type=Path)
    parser.add_argument("--role-manifest", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--allow-incomplete-smoke", action="store_true")
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args(argv)
    formal = not args.allow_incomplete_smoke
    try:
        dataset = RoleDatasetAccess(
            args.role_manifest,
            ledger_path=args.ledger,
            run_id=args.run_id,
            require_complete=formal,
        )
        if formal:
            dataset.require_collection_boundary(FORMAL_COLLECTION_PASSES)
        calibrated = load_calibrated_ensemble(
            args.calibration_dir,
            dataset=dataset,
            run_id=args.run_id,
            device=args.device,
            formal=formal,
        )
        policy = verify_policy_artifacts(
            args.policy_dir,
            calibrated=calibrated,
            dataset=dataset,
            run_id=args.run_id,
            formal=formal,
        )
        payload = build_verified_bundle_payload(
            calibrated=calibrated,
            policy=policy,
            dataset=dataset,
            run_id=args.run_id,
            formal=formal,
        )
        artifact = write_export(args.output, payload)
        expected_binding = bundle_artifact_binding(payload)
        if (
            artifact["bytes"] != expected_binding["bundle_bytes"]
            or artifact["sha256"] != expected_binding["bundle_sha256"]
        ):
            raise RuntimeError("written v4 bundle bytes changed")
        if OpponentMultiTaskEnsembleRuntimeV4.load(artifact["path"]) is None:
            raise RuntimeError("written v4 ensemble failed strict reload")
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps({
        **artifact,
        "members": len(payload["members"]),
        "selected_policy": payload["selected_policy"],
        "policy_selection_passed": payload["source"][
            "policy_selection_passed"
        ],
        "deployment_policy_value": False,
        "strength_evidence": False,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
