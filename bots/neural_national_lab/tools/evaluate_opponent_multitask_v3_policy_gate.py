#!/usr/bin/env python3
"""Evaluate one frozen v3 policy on its protected opponent-disjoint gate."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import sys
from typing import Any

import torch

from evaluate_multitask_offline_policy import OFFLINE_ESTIMAND, _evaluate_config
from export_opponent_multitask_ensemble_v3 import verify_policy_artifacts
from policy_role_evidence import (
    build_policy_gate_result,
    open_policy_gate,
    write_policy_gate_result,
)
from role_dataset_access import RoleDatasetAccess
from select_opponent_multitask_v3_policy import (
    _sha256,
    load_calibrated_ensemble,
    prepare_policy_rows,
)


GATE_EVALUATION_SCHEMA = "opponent_multitask_v3_policy_gate_evaluation_v2"
GATE_REPORT_SCHEMA = "opponent_multitask_v3_policy_gate_report_v2"
GATE_ARTIFACT_SCHEMA = "opponent_multitask_v3_policy_gate_artifacts_v2"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def evaluate_fixed_policy(
    prepared_rows: list[dict[str, Any]],
    selected_policy: dict[str, Any],
    *,
    bootstrap_samples: int,
    bootstrap_seed: int,
) -> dict[str, Any]:
    result = _evaluate_config(
        prepared_rows,
        dict(selected_policy),
        bootstrap_samples=bootstrap_samples,
        bootstrap_seed=bootstrap_seed,
    )
    result.update({
        "schema": GATE_EVALUATION_SCHEMA,
        "offline_estimand": OFFLINE_ESTIMAND,
        "selected_policy": dict(selected_policy),
        "policy_search_performed": False,
        "source_collection_complete": True,
        "deployment_policy_value": False,
        "strength_evidence": False,
    })
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration-dir", required=True, type=Path)
    parser.add_argument("--policy-dir", required=True, type=Path)
    parser.add_argument("--role-manifest", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--bootstrap-samples", type=int, default=2000)
    parser.add_argument("--bootstrap-seed", type=int, default=20260712)
    parser.add_argument("--min-overrides", type=int, default=12)
    parser.add_argument("--min-override-clusters", type=int, default=8)
    parser.add_argument("--min-overrides-per-opponent", type=int, default=4)
    parser.add_argument("--min-ci-lower", type=float, default=0.0)
    parser.add_argument(
        "--min-match-positive-rate-ci-lower", type=float, default=0.5
    )
    parser.add_argument(
        "--min-match-positive-uplift-ci-lower", type=float, default=0.0
    )
    parser.add_argument(
        "--min-opponent-match-positive-rate", type=float, default=0.5
    )
    args = parser.parse_args(argv)
    if (
        min(
            args.batch_size,
            args.bootstrap_samples,
            args.min_overrides,
            args.min_override_clusters,
            args.min_overrides_per_opponent,
        ) < 1
        or not math.isfinite(args.min_ci_lower)
    ):
        raise SystemExit("invalid policy gate thresholds")
    if (
        not 0.5 <= args.min_match_positive_rate_ci_lower <= 1.0
        or not 0.0 <= args.min_match_positive_uplift_ci_lower <= 1.0
        or not 0.5 <= args.min_opponent_match_positive_rate <= 1.0
    ):
        raise SystemExit("win-first thresholds cannot be weakened")
    if str(args.device).startswith("cuda") and not torch.cuda.is_available():
        raise SystemExit("CUDA was requested but is unavailable")

    try:
        dataset = RoleDatasetAccess(
            args.role_manifest,
            ledger_path=args.ledger,
            run_id=args.run_id,
            require_complete=True,
        )
        calibrated = load_calibrated_ensemble(
            args.calibration_dir,
            dataset=dataset,
            run_id=args.run_id,
            device=args.device,
            formal=True,
        )
        policy = verify_policy_artifacts(
            args.policy_dir,
            calibrated=calibrated,
            dataset=dataset,
            run_id=args.run_id,
            formal=True,
        )
        selected_policy = policy["selected_policy"]
        if not isinstance(selected_policy, dict):
            raise ValueError("policy selection did not freeze a policy")
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    out_dir = args.out_dir.resolve()
    if out_dir.exists():
        raise SystemExit(f"output directory already exists: {out_dir}")
    temporary = out_dir.with_name(f".{out_dir.name}.tmp-{os.getpid()}")
    if temporary.exists():
        raise SystemExit(f"temporary output directory already exists: {temporary}")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.mkdir()
    try:
        selection_result_path = (
            policy["root"] / "policy_selection_result.json"
        )
        phase = open_policy_gate(
            dataset,
            candidate_sha256=policy["candidate_sha256"],
            selection_result_path=selection_result_path,
        )
        prepared_rows, preparation = prepare_policy_rows(
            phase["value"],
            calibrated,
            batch_size=args.batch_size,
            device=args.device,
        )
        evaluation = evaluate_fixed_policy(
            prepared_rows,
            selected_policy,
            bootstrap_samples=args.bootstrap_samples,
            bootstrap_seed=args.bootstrap_seed,
        )
        evaluation["preparation"] = preparation
        evaluation_path = temporary / "policy_gate_evaluation.json"
        _write_json(evaluation_path, evaluation)
        thresholds = {
            "min_overrides": args.min_overrides,
            "min_override_clusters": args.min_override_clusters,
            "min_overrides_per_opponent": args.min_overrides_per_opponent,
            "min_cluster_ci_lower": args.min_ci_lower,
            "min_opponent_stratified_ci_lower": args.min_ci_lower,
            "min_match_positive_rate_ci_lower": (
                args.min_match_positive_rate_ci_lower
            ),
            "min_match_positive_uplift_ci_lower": (
                args.min_match_positive_uplift_ci_lower
            ),
            "min_opponent_match_positive_rate": (
                args.min_opponent_match_positive_rate
            ),
        }
        result = build_policy_gate_result(
            phase, evaluation, thresholds=thresholds
        )
        result_path = temporary / "policy_gate_result.json"
        result_sha256 = write_policy_gate_result(result_path, result)
        report = {
            "schema": GATE_REPORT_SCHEMA,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "run_id": args.run_id,
            "command": [sys.executable, *sys.argv],
            "role_manifest": str(args.role_manifest.resolve()),
            "role_manifest_sha256": dataset.manifest_sha256,
            "candidate_sha256": policy["candidate_sha256"],
            "selection_result_sha256": phase["selection_result_sha256"],
            "selected_policy_sha256": policy["selected_policy_sha256"],
            "policy_gate_artifact_sha256": phase[
                "policy_gate_artifact_sha256"
            ],
            "opened_roles": ["policy_gate"],
            "policy_gate_opponents": phase["opponents"],
            "policy_gate_value_rows": len(phase["value"]),
            "policy_gate_behavior_rows": len(phase["behavior"]),
            "preparation": preparation,
            "policy_search_performed": False,
            "gate_passed": result["passed"],
            "gate_errors": result["errors"],
            "native_candidate_build_authorized": result[
                "native_candidate_build_authorized"
            ],
            "gate_result_sha256": result_sha256,
            "source_collection_complete": True,
            "deployment_policy_value": False,
            "strength_evidence": False,
            "native_tcp_evaluated": False,
        }
        report_path = temporary / "policy_gate_report.json"
        _write_json(report_path, report)
        files = (
            "policy_gate_evaluation.json",
            "policy_gate_result.json",
            "policy_gate_report.json",
        )
        _write_json(temporary / "artifact_manifest.json", {
            "schema": GATE_ARTIFACT_SCHEMA,
            "run_id": args.run_id,
            "files": {
                name: {
                    "bytes": (temporary / name).stat().st_size,
                    "sha256": _sha256(temporary / name),
                }
                for name in files
            },
            "candidate_sha256": policy["candidate_sha256"],
            "native_candidate_build_authorized": result[
                "native_candidate_build_authorized"
            ],
            "deployment_policy_value": False,
            "strength_evidence": False,
        })
        temporary.replace(out_dir)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    print(json.dumps({
        "out_dir": str(out_dir),
        "candidate_sha256": policy["candidate_sha256"],
        "prepared_rows": preparation["prepared_rows"],
        "gate_passed": result["passed"],
        "native_candidate_build_authorized": result[
            "native_candidate_build_authorized"
        ],
        "strength_evidence": False,
    }, indent=2, sort_keys=True))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
