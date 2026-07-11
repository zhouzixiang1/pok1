#!/usr/bin/env python3
"""Export calibrated v3 members and an approved offline policy as one bundle."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from export_opponent_multitask_v3 import build_export_payload, write_export
from opponent_multitask_ensemble_runtime_v3 import (
    ENSEMBLE_FORMAT,
    OpponentMultiTaskEnsembleRuntimeV3,
)
from role_dataset_access import POLICY_SELECTION_RESULT_SCHEMA, RoleDatasetAccess
from select_opponent_multitask_v3_policy import (
    POLICY_ARTIFACT_SCHEMA,
    POLICY_CANDIDATE_SCHEMA,
    POLICY_EVALUATION_SCHEMA,
    POLICY_REPORT_SCHEMA,
    _canonical_sha256,
    _load_json,
    _sha256,
    _verify_file_contracts,
    load_calibrated_ensemble,
)
from train_opponent_multitask_v3 import load_checkpoint


BUNDLE_SCHEMA = "opponent_multitask_stdlib_ensemble_export_v1"


def verify_policy_artifacts(
    policy_dir: Path,
    *,
    calibrated: dict[str, Any],
    dataset: RoleDatasetAccess,
    run_id: str,
    formal: bool,
) -> dict[str, Any]:
    root = policy_dir.resolve()
    artifact = _load_json(root / "artifact_manifest.json", field="policy artifacts")
    if (
        artifact.get("schema") != POLICY_ARTIFACT_SCHEMA
        or artifact.get("run_id") != run_id
        or artifact.get("policy_gate_opened") is not False
        or artifact.get("deployment_policy_value") is not False
        or artifact.get("strength_evidence") is not False
    ):
        raise ValueError("invalid policy artifact manifest")
    _verify_file_contracts(root, artifact)
    candidate_path = root / "candidate_manifest.json"
    evaluation_path = root / "policy_evaluation.json"
    result_path = root / "policy_selection_result.json"
    report_path = root / "policy_selection_report.json"
    candidate = _load_json(candidate_path, field="policy candidate")
    evaluation = _load_json(evaluation_path, field="policy evaluation")
    result = _load_json(result_path, field="policy selection result")
    report = _load_json(report_path, field="policy selection report")
    candidate_sha256 = _sha256(candidate_path)
    result_file_sha256 = _sha256(result_path)
    selected_policy = evaluation.get("selected_policy")
    selected_policy_sha256 = (
        _canonical_sha256(selected_policy)
        if isinstance(selected_policy, dict)
        else None
    )
    if (
        candidate.get("schema") != POLICY_CANDIDATE_SCHEMA
        or candidate.get("run_id") != run_id
        or candidate.get("role_manifest_sha256") != dataset.manifest_sha256
        or candidate.get("ensemble_manifest_sha256")
        != calibrated["ensemble_manifest_sha256"]
        or candidate.get("calibration_payload_sha256")
        != calibrated["calibration_payload_sha256"]
        or candidate.get("deployment_policy_value") is not False
        or candidate.get("strength_evidence") is not False
        or artifact.get("candidate_sha256") != candidate_sha256
        or evaluation.get("schema") != POLICY_EVALUATION_SCHEMA
        or evaluation.get("deployment_policy_value") is not False
        or evaluation.get("strength_evidence") is not False
        or result.get("schema") != POLICY_SELECTION_RESULT_SCHEMA
        or result.get("run_id") != run_id
        or result.get("candidate_sha256") != candidate_sha256
        or result.get("role_manifest_sha256") != dataset.manifest_sha256
        or result.get("calibration_payload_sha256")
        != calibrated["calibration_payload_sha256"]
        or result.get("evaluation_report_sha256")
        != _canonical_sha256(evaluation)
        or result.get("selected_policy_sha256") != selected_policy_sha256
        or result.get("policy_gate_opened") is not False
        or result.get("deployment_policy_value") is not False
        or result.get("strength_evidence") is not False
        or report.get("schema") != POLICY_REPORT_SCHEMA
        or report.get("run_id") != run_id
        or report.get("candidate_sha256") != candidate_sha256
        or report.get("selection_result_sha256") != result_file_sha256
        or report.get("policy_gate_opened") is not False
        or report.get("deployment_policy_value") is not False
        or report.get("strength_evidence") is not False
    ):
        raise ValueError("policy artifact bindings are invalid")
    if formal:
        if (
            result.get("passed") is not True
            or result.get("formal_selection") is not True
            or result.get("source_collection_complete") is not True
            or report.get("selection_passed") is not True
            or report.get("incomplete_smoke") is not False
            or report.get("source_collection_complete") is not True
            or not isinstance(selected_policy, dict)
        ):
            raise ValueError("formal bundle requires a passing policy selection")
    else:
        selected_policy = None
        selected_policy_sha256 = None
    return {
        "root": root,
        "candidate_sha256": candidate_sha256,
        "evaluation_sha256": _sha256(evaluation_path),
        "result_sha256": result_file_sha256,
        "artifact_manifest_sha256": _sha256(root / "artifact_manifest.json"),
        "selected_policy": selected_policy,
        "selected_policy_sha256": selected_policy_sha256,
        "selection_passed": bool(formal and result.get("passed") is True),
    }


def build_bundle_payload(
    member_payloads: list[dict[str, Any]],
    *,
    calibrated: dict[str, Any],
    policy: dict[str, Any],
    source: dict[str, Any],
) -> dict[str, Any]:
    checkpoint_sha256 = [
        member["source"]["checkpoint_sha256"] for member in member_payloads
    ]
    payload = {
        "schema": BUNDLE_SCHEMA,
        "format": ENSEMBLE_FORMAT,
        "members": member_payloads,
        "member_payload_sha256": [
            _canonical_sha256(member) for member in member_payloads
        ],
        "calibration": {
            "payload_sha256": calibrated["calibration_payload_sha256"],
            "member_checkpoint_sha256": checkpoint_sha256,
            "lower_quantile": calibrated["lower_quantile"],
            "uncertainty_std_weight": calibrated["uncertainty_std_weight"],
            "clips": calibrated["clips"],
            "offsets": calibrated["offsets"],
            "response_temperature": calibrated["response_temperature"],
        },
        "selected_policy": policy.get("selected_policy"),
        "source": {
            **source,
            "selected_policy_sha256": policy.get("selected_policy_sha256"),
            "policy_selection_passed": policy.get("selection_passed", False),
            "deployment_policy_value": False,
            "strength_evidence": False,
        },
        "export_contract": {
            "bundle_tool_sha256": _sha256(Path(__file__).resolve()),
            "runtime_tool_sha256": _sha256(
                Path(
                    sys.modules["opponent_multitask_ensemble_runtime_v3"].__file__
                ).resolve()
            ),
            "member_payload_hash": "canonical_json_sha256",
        },
        "deployment_policy_value": False,
        "strength_evidence": False,
    }
    OpponentMultiTaskEnsembleRuntimeV3(payload)
    return payload


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
    try:
        dataset = RoleDatasetAccess(
            args.role_manifest,
            ledger_path=args.ledger,
            run_id=args.run_id,
            require_complete=not args.allow_incomplete_smoke,
        )
        calibrated = load_calibrated_ensemble(
            args.calibration_dir,
            dataset=dataset,
            run_id=args.run_id,
            device=args.device,
            formal=not args.allow_incomplete_smoke,
        )
        policy = verify_policy_artifacts(
            args.policy_dir,
            calibrated=calibrated,
            dataset=dataset,
            run_id=args.run_id,
            formal=not args.allow_incomplete_smoke,
        )
        member_payloads = []
        for member in calibrated["members"]:
            model, checkpoint = load_checkpoint(
                Path(member["checkpoint_path"]), device="cpu"
            )
            member_payloads.append(build_export_payload(
                model,
                checkpoint,
                checkpoint_sha256=member["checkpoint_sha256"],
            ))
        payload = build_bundle_payload(
            member_payloads,
            calibrated=calibrated,
            policy=policy,
            source={
                "run_id": args.run_id,
                "role_manifest_sha256": dataset.manifest_sha256,
                "ensemble_manifest_sha256": calibrated[
                    "ensemble_manifest_sha256"
                ],
                "calibration_payload_sha256": calibrated[
                    "calibration_payload_sha256"
                ],
                "policy_candidate_sha256": policy["candidate_sha256"],
                "policy_evaluation_sha256": policy["evaluation_sha256"],
                "policy_result_sha256": policy["result_sha256"],
                "policy_artifact_manifest_sha256": policy[
                    "artifact_manifest_sha256"
                ],
                "source_collection_complete": dataset.manifest.get(
                    "source_collection_complete"
                ),
            },
        )
        artifact = write_export(args.output, payload)
        if OpponentMultiTaskEnsembleRuntimeV3.load(artifact["path"]) is None:
            raise RuntimeError("written v3 ensemble failed strict reload")
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps({
        **artifact,
        "members": len(member_payloads),
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
