#!/usr/bin/env python3
"""Evaluate one frozen v4 win-first policy on its protected gate role."""
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

from evaluate_multitask_offline_policy import _evaluate_config
from export_opponent_multitask_ensemble_v4 import (
    build_verified_bundle_payload,
    verify_exact_bundle,
)
from match_outcome_schema import MATCH_OUTCOME_ESTIMAND
from policy_role_evidence import (
    build_bootstrap_contract,
    build_policy_gate_result,
    open_policy_gate,
    write_policy_gate_result,
)
from role_dataset_access import POLICY_OFFLINE_ESTIMAND_V4, RoleDatasetAccess
import select_opponent_multitask_v3_policy as v3
from v4_native_build_contract import current_native_build_contract
from select_opponent_multitask_v4_policy import (
    EVIDENCE_CONTRACT,
    FORMAL_COLLECTION_PASSES,
    FORMAL_MIN_BOOTSTRAP_SAMPLES,
    FORMAL_MIN_OVERRIDE_CLUSTERS,
    FORMAL_MIN_OVERRIDES,
    FORMAL_MIN_OVERRIDES_PER_OPPONENT,
    _validated_calibrated_ensemble,
    load_calibrated_ensemble,
    normalize_selected_policy,
    prepare_policy_rows,
    recompute_and_verify_formal_policy_selection,
    select_win_first_candidate,
    selector_code_artifacts,
)


GATE_EVALUATION_SCHEMA = "opponent_multitask_v4_policy_gate_evaluation_v1"
GATE_REPORT_SCHEMA = "opponent_multitask_v4_policy_gate_report_v1"
GATE_ARTIFACT_SCHEMA = "opponent_multitask_v4_policy_gate_artifacts_v1"


def gate_code_artifacts() -> dict[str, dict[str, Any]]:
    result = selector_code_artifacts()
    path = Path(__file__).resolve()
    result["gate"] = {
        "bytes": path.stat().st_size,
        "sha256": v3._sha256(path),
    }
    bundle_exporter = Path(
        sys.modules["export_opponent_multitask_ensemble_v4"].__file__
    ).resolve()
    result["bundle_exporter"] = {
        "bytes": bundle_exporter.stat().st_size,
        "sha256": v3._sha256(bundle_exporter),
    }
    contract_helper = Path(
        sys.modules["v4_native_build_contract"].__file__
    ).resolve()
    result["native_build_contract"] = {
        "bytes": contract_helper.stat().st_size,
        "sha256": v3._sha256(contract_helper),
    }
    return dict(sorted(result.items()))


def validate_gate_probability_domain(evaluation: dict[str, Any]) -> None:
    for field, lower, upper in (
        ("match_positive_rate", 0.0, 1.0),
        ("rule_match_positive_rate", 0.0, 1.0),
        ("match_positive_uplift_mean", -1.0, 1.0),
    ):
        value = evaluation.get(field)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not lower <= float(value) <= upper
        ):
            raise ValueError(f"v4 policy gate {field} is outside its domain")
    if not math.isclose(
        float(evaluation["match_positive_uplift_mean"]),
        float(evaluation["match_positive_rate"])
        - float(evaluation["rule_match_positive_rate"]),
        rel_tol=0.0,
        abs_tol=1.0e-12,
    ):
        raise ValueError("v4 policy gate positive uplift is inconsistent")
    for field, lower, upper in (
        ("match_positive_rate_cluster_bootstrap_ci", 0.0, 1.0),
        (
            "match_positive_rate_opponent_stratified_cluster_ci",
            0.0,
            1.0,
        ),
        ("rule_match_positive_rate_cluster_bootstrap_ci", 0.0, 1.0),
        (
            "rule_match_positive_rate_opponent_stratified_cluster_ci",
            0.0,
            1.0,
        ),
        ("match_positive_uplift_cluster_bootstrap_ci", -1.0, 1.0),
        (
            "match_positive_uplift_opponent_stratified_cluster_ci",
            -1.0,
            1.0,
        ),
    ):
        interval = evaluation.get(field)
        if not isinstance(interval, dict) or set(interval) != {
            "lower", "mean", "upper"
        }:
            raise ValueError(f"v4 policy gate {field} is invalid")
        values = [interval[key] for key in ("lower", "mean", "upper")]
        if (
            any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not lower <= float(value) <= upper
                for value in values
            )
            or not values[0] <= values[1] <= values[2]
        ):
            raise ValueError(f"v4 policy gate {field} is outside its domain")
    ordinary_means = {
        "match_positive_rate_cluster_bootstrap_ci": evaluation[
            "match_positive_rate"
        ],
        "rule_match_positive_rate_cluster_bootstrap_ci": evaluation[
            "rule_match_positive_rate"
        ],
        "match_positive_uplift_cluster_bootstrap_ci": evaluation[
            "match_positive_uplift_mean"
        ],
    }
    for field, expected in ordinary_means.items():
        if not math.isclose(
            float(evaluation[field]["mean"]),
            float(expected),
            rel_tol=0.0,
            abs_tol=1.0e-12,
        ):
            raise ValueError(f"v4 policy gate {field} mean is inconsistent")
    by_opponent = evaluation.get("by_opponent")
    if not isinstance(by_opponent, dict) or not by_opponent:
        raise ValueError("v4 policy gate per-opponent evidence is missing")
    for opponent, row in by_opponent.items():
        if not isinstance(row, dict):
            raise ValueError(f"v4 policy gate opponent row is invalid: {opponent}")
        rate = row.get("match_positive_rate")
        rule_rate = row.get("rule_match_positive_rate")
        uplift = row.get("match_positive_uplift_mean")
        if (
            isinstance(rate, bool)
            or not isinstance(rate, (int, float))
            or not 0.0 <= float(rate) <= 1.0
            or isinstance(rule_rate, bool)
            or not isinstance(rule_rate, (int, float))
            or not 0.0 <= float(rule_rate) <= 1.0
            or isinstance(uplift, bool)
            or not isinstance(uplift, (int, float))
            or not -1.0 <= float(uplift) <= 1.0
            or not math.isclose(
                float(uplift),
                float(rate) - float(rule_rate),
                rel_tol=0.0,
                abs_tol=1.0e-12,
            )
        ):
            raise ValueError(
                f"v4 policy gate opponent probability is invalid: {opponent}"
            )


def evaluate_fixed_policy(
    prepared_rows: list[dict[str, Any]],
    selected_policy: dict[str, Any],
    *,
    bootstrap_samples: int,
    bootstrap_seed: int,
) -> dict[str, Any]:
    normalized = normalize_selected_policy(selected_policy)
    if normalized is None or normalized != selected_policy:
        raise ValueError("policy gate requires one exact normalized v4 policy")
    result = _evaluate_config(
        prepared_rows,
        normalized,
        bootstrap_samples=bootstrap_samples,
        bootstrap_seed=bootstrap_seed,
        candidate_selector=select_win_first_candidate,
    )
    validate_gate_probability_domain(result)
    result.update({
        "schema": GATE_EVALUATION_SCHEMA,
        "estimand": POLICY_OFFLINE_ESTIMAND_V4,
        "offline_estimand": POLICY_OFFLINE_ESTIMAND_V4,
        "match_outcome_estimand": MATCH_OUTCOME_ESTIMAND,
        "selected_policy": dict(normalized),
        "config": dict(normalized),
        "policy_search_performed": False,
        "source_collection_complete": True,
        "deployment_policy_value": False,
        "strength_evidence": False,
        "bootstrap_contract": build_bootstrap_contract(
            samples=bootstrap_samples, seed=bootstrap_seed
        ),
        "native_build_contract": current_native_build_contract(),
        "code_artifacts": gate_code_artifacts(),
    })
    return result


def recompute_bound_fixed_gate(
    *,
    dataset: RoleDatasetAccess,
    calibrated: dict[str, Any],
    selected_policy: dict[str, Any],
    candidate_sha256: str,
    selection_result_path: Path,
    bundle_binding: dict[str, Any],
    batch_size: int,
    device: str,
    bootstrap_samples: int,
    bootstrap_seed: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Open protected gate rows and deterministically rebuild its evaluation."""
    phase = open_policy_gate(
        dataset,
        candidate_sha256=candidate_sha256,
        selection_result_path=selection_result_path,
        contract=EVIDENCE_CONTRACT,
    )
    prepared_rows, preparation = prepare_policy_rows(
        phase["value"],
        calibrated,
        batch_size=batch_size,
        device=device,
    )
    evaluation = evaluate_fixed_policy(
        prepared_rows,
        selected_policy,
        bootstrap_samples=bootstrap_samples,
        bootstrap_seed=bootstrap_seed,
    )
    if set(evaluation.get("by_opponent") or {}) != set(phase["opponents"]):
        raise ValueError("v4 policy gate opponent coverage changed")
    evaluation.update({
        "preparation": preparation,
        "inference_contract": {
            "device": str(device),
            "batch_size": batch_size,
        },
        **bundle_binding,
        **dataset.runtime_context_contract(),
        "policy_gate_artifact_sha256": phase[
            "policy_gate_artifact_sha256"
        ],
        "policy_gate_opponents": list(phase["opponents"]),
    })
    return phase, evaluation


def build_bound_gate_result(
    *,
    phase: dict[str, Any],
    evaluation: dict[str, Any],
    thresholds: dict[str, Any],
    bundle_binding: dict[str, Any],
    dataset: RoleDatasetAccess,
) -> dict[str, Any]:
    result = build_policy_gate_result(
        phase,
        evaluation,
        thresholds=thresholds,
        contract=EVIDENCE_CONTRACT,
    )
    result.update(bundle_binding)
    result.update(dataset.runtime_context_contract())
    result["native_build_contract"] = evaluation["native_build_contract"]
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calibration-dir", required=True, type=Path)
    parser.add_argument("--policy-dir", required=True, type=Path)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--role-manifest", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--bootstrap-samples", type=int, default=2000)
    parser.add_argument("--bootstrap-seed", type=int, default=20260714)
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
        or args.min_ci_lower < 0.0
    ):
        raise SystemExit("invalid v4 policy gate thresholds")
    if (
        not 0.5 <= args.min_match_positive_rate_ci_lower <= 1.0
        or not 0.0 <= args.min_match_positive_uplift_ci_lower <= 1.0
        or not 0.5 <= args.min_opponent_match_positive_rate <= 1.0
    ):
        raise SystemExit("win-first evidence thresholds cannot be weakened")
    if (
        args.min_overrides < FORMAL_MIN_OVERRIDES
        or args.min_override_clusters < FORMAL_MIN_OVERRIDE_CLUSTERS
        or args.min_overrides_per_opponent
        < FORMAL_MIN_OVERRIDES_PER_OPPONENT
        or args.bootstrap_samples < FORMAL_MIN_BOOTSTRAP_SAMPLES
    ):
        raise SystemExit("formal v4 policy gate coverage cannot be weakened")
    if str(args.device).startswith("cuda") and not torch.cuda.is_available():
        raise SystemExit("CUDA was requested but is unavailable")

    try:
        dataset = RoleDatasetAccess(
            args.role_manifest,
            ledger_path=args.ledger,
            run_id=args.run_id,
            require_complete=True,
        )
        collection_boundary = dataset.require_collection_boundary(
            FORMAL_COLLECTION_PASSES
        )
        calibrated = _validated_calibrated_ensemble(
            load_calibrated_ensemble(
                args.calibration_dir,
                dataset=dataset,
                run_id=args.run_id,
                device=args.device,
                formal=True,
            ),
            dataset=dataset,
            run_id=args.run_id,
            formal=True,
        )
        policy = recompute_and_verify_formal_policy_selection(
            args.policy_dir,
            calibrated=calibrated,
            dataset=dataset,
            run_id=args.run_id,
            device=str(args.device),
            batch_size=args.batch_size,
        )
        selected_policy = policy["selected_policy"]
        if not isinstance(selected_policy, dict):
            raise ValueError("v4 policy selection did not freeze a policy")
        expected_bundle = build_verified_bundle_payload(
            calibrated=calibrated,
            policy=policy,
            dataset=dataset,
            run_id=args.run_id,
            formal=True,
        )
        _bundle, _bundle_raw, bundle_binding = verify_exact_bundle(
            args.bundle, expected_bundle
        )
        runtime_context = dataset.runtime_context_contract()
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
        phase, evaluation = recompute_bound_fixed_gate(
            dataset=dataset,
            calibrated=calibrated,
            selected_policy=selected_policy,
            candidate_sha256=policy["candidate_sha256"],
            selection_result_path=(
                policy["root"] / "policy_selection_result.json"
            ),
            bundle_binding=bundle_binding,
            batch_size=args.batch_size,
            device=str(args.device),
            bootstrap_samples=args.bootstrap_samples,
            bootstrap_seed=args.bootstrap_seed,
        )
        preparation = evaluation["preparation"]
        evaluation_path = temporary / "policy_gate_evaluation.json"
        v3._write_json(evaluation_path, evaluation)
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
        result = build_bound_gate_result(
            phase=phase,
            evaluation=evaluation,
            thresholds=thresholds,
            bundle_binding=bundle_binding,
            dataset=dataset,
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
            "calibration_payload_sha256": phase[
                "calibration_payload_sha256"
            ],
            "selected_policy_sha256": policy["selected_policy_sha256"],
            "policy_gate_artifact_sha256": phase[
                "policy_gate_artifact_sha256"
            ],
            **bundle_binding,
            **runtime_context,
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
            "collection_boundary": collection_boundary,
            "code_artifacts": evaluation["code_artifacts"],
            "native_build_contract": evaluation["native_build_contract"],
            "deployment_policy_value": False,
            "strength_evidence": False,
            "native_tcp_evaluated": False,
        }
        v3._write_json(temporary / "policy_gate_report.json", report)
        files = (
            "policy_gate_evaluation.json",
            "policy_gate_result.json",
            "policy_gate_report.json",
        )
        v3._write_json(temporary / "artifact_manifest.json", {
            "schema": GATE_ARTIFACT_SCHEMA,
            "run_id": args.run_id,
            "files": {
                name: {
                    "bytes": (temporary / name).stat().st_size,
                    "sha256": v3._sha256(temporary / name),
                }
                for name in files
            },
            "candidate_sha256": policy["candidate_sha256"],
            "policy_gate_artifact_sha256": phase[
                "policy_gate_artifact_sha256"
            ],
            **bundle_binding,
            **runtime_context,
            "code_artifacts": evaluation["code_artifacts"],
            "native_build_contract": evaluation["native_build_contract"],
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
        "deployment_policy_value": False,
        "strength_evidence": False,
    }, indent=2, sort_keys=True))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
