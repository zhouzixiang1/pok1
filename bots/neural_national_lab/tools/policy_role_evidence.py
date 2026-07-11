"""Ledger-backed offline policy selection and gate evidence contracts."""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any

from match_outcome_schema import MATCH_OUTCOME_ESTIMAND
from role_dataset_access import (
    POLICY_OFFLINE_ESTIMAND,
    POLICY_SELECTION_RESULT_SCHEMA,
)


POLICY_SELECTION_PHASE_SCHEMA = "policy_selection_phase_v1"
POLICY_GATE_RESULT_SCHEMA = "policy_gate_result_v2"
DEFAULT_THRESHOLDS = {
    "min_overrides": 12,
    "min_override_clusters": 8,
    "min_overrides_per_opponent": 4,
    "min_cluster_ci_lower": 0.0,
    "min_opponent_stratified_ci_lower": 0.0,
    "min_match_outcome_coverage": 1.0,
    "min_match_positive_rate_ci_lower": 0.5,
    "min_match_positive_uplift_ci_lower": 0.0,
    "min_opponent_match_positive_rate": 0.5,
}


def _digest(value: Any, *, field: str) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return result


def _canonical_sha256(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _finite(value: Any, *, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be finite") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def open_policy_selection(
    dataset: Any,
    *,
    candidate_sha256: str,
    calibration_payload_sha256: str,
) -> dict[str, Any]:
    candidate = _digest(candidate_sha256, field="candidate_sha256")
    calibration = _digest(
        calibration_payload_sha256, field="calibration_payload_sha256"
    )
    opened = dataset.open_role(
        "policy_selection", candidate_sha256=candidate
    )
    return {
        "schema": POLICY_SELECTION_PHASE_SCHEMA,
        "run_id": dataset.run_id,
        "candidate_sha256": candidate,
        "role_manifest_sha256": dataset.manifest_sha256,
        "calibration_payload_sha256": calibration,
        "policy_selection_artifact_sha256": opened["artifact_sha256"],
        "opponents": list(opened["opponents"]),
        "value": opened["value"],
        "behavior": opened["behavior"],
    }


def _thresholds(overrides: Mapping[str, Any] | None) -> dict[str, float | int]:
    result = dict(DEFAULT_THRESHOLDS)
    updates = dict(overrides or {})
    unknown = set(updates) - set(DEFAULT_THRESHOLDS)
    if unknown:
        raise ValueError(f"unknown policy evidence thresholds: {sorted(unknown)}")
    result.update(updates)
    integer_fields = (
        "min_overrides", "min_override_clusters", "min_overrides_per_opponent"
    )
    for field in integer_fields:
        value = result[field]
        if isinstance(value, bool) or int(value) != value or int(value) < 1:
            raise ValueError(f"{field} must be a positive integer")
        result[field] = int(value)
    for field in (
        "min_cluster_ci_lower",
        "min_opponent_stratified_ci_lower",
        "min_match_outcome_coverage",
        "min_match_positive_rate_ci_lower",
        "min_match_positive_uplift_ci_lower",
        "min_opponent_match_positive_rate",
    ):
        result[field] = _finite(result[field], field=field)
    if result["min_match_outcome_coverage"] != 1.0:
        raise ValueError("min_match_outcome_coverage must remain 1.0")
    if not 0.5 <= result["min_match_positive_rate_ci_lower"] <= 1.0:
        raise ValueError("min_match_positive_rate_ci_lower cannot be weakened")
    if not 0.0 <= result["min_match_positive_uplift_ci_lower"] <= 1.0:
        raise ValueError("min_match_positive_uplift_ci_lower cannot be weakened")
    if not 0.5 <= result["min_opponent_match_positive_rate"] <= 1.0:
        raise ValueError("min_opponent_match_positive_rate cannot be weakened")
    return result


def _gate_errors(
    evaluation: Mapping[str, Any], thresholds: Mapping[str, Any]
) -> list[str]:
    errors = []
    if evaluation.get("match_outcome_estimand") != MATCH_OUTCOME_ESTIMAND:
        errors.append("match_outcome_estimand_missing")
    coverage = thresholds["min_match_outcome_coverage"]
    if _finite(
        evaluation.get("match_outcome_row_coverage", 0.0),
        field="match outcome row coverage",
    ) < coverage:
        errors.append(f"match_outcome_row_coverage<{coverage}")
    if _finite(
        evaluation.get("match_outcome_cluster_coverage", 0.0),
        field="match outcome cluster coverage",
    ) < coverage:
        errors.append(f"match_outcome_cluster_coverage<{coverage}")
    for field in (
        "match_positive_rate_cluster_bootstrap_ci",
        "match_positive_rate_opponent_stratified_cluster_ci",
    ):
        lower = _finite(
            (evaluation.get(field) or {}).get("lower", 0.0),
            field=f"{field} lower",
        )
        threshold = thresholds["min_match_positive_rate_ci_lower"]
        if lower <= threshold:
            errors.append(f"{field}_lower<={threshold}")
    for field in (
        "match_positive_uplift_cluster_bootstrap_ci",
        "match_positive_uplift_opponent_stratified_cluster_ci",
    ):
        lower = _finite(
            (evaluation.get(field) or {}).get("lower", 0.0),
            field=f"{field} lower",
        )
        threshold = thresholds["min_match_positive_uplift_ci_lower"]
        if lower < threshold:
            errors.append(f"{field}_lower<{threshold}")
    selected = evaluation.get("selected_policy")
    if not isinstance(selected, Mapping) or not selected:
        errors.append("selected_policy_missing")
    overrides = int(evaluation.get("overrides", 0) or 0)
    clusters = int(evaluation.get("override_clusters", 0) or 0)
    if overrides < thresholds["min_overrides"]:
        errors.append(f"overrides<{thresholds['min_overrides']}")
    if clusters < thresholds["min_override_clusters"]:
        errors.append(f"override_clusters<{thresholds['min_override_clusters']}")
    ordinary = _finite(
        (evaluation.get("match_cluster_bootstrap_mean_ci") or {}).get("lower", 0.0),
        field="cluster CI lower",
    )
    stratified = _finite(
        (evaluation.get("match_opponent_stratified_cluster_ci") or {}).get(
            "lower", 0.0
        ),
        field="opponent-stratified CI lower",
    )
    if ordinary <= thresholds["min_cluster_ci_lower"]:
        errors.append(
            f"cluster_ci_lower<={thresholds['min_cluster_ci_lower']}"
        )
    if stratified <= thresholds["min_opponent_stratified_ci_lower"]:
        errors.append(
            "opponent_stratified_cluster_ci_lower<="
            f"{thresholds['min_opponent_stratified_ci_lower']}"
        )
    by_opponent = evaluation.get("by_opponent")
    if not isinstance(by_opponent, Mapping) or not by_opponent:
        errors.append("per_opponent_evidence_missing")
    else:
        for opponent, raw in sorted(by_opponent.items()):
            row = raw if isinstance(raw, Mapping) else {}
            if int(row.get("overrides", 0) or 0) < thresholds[
                "min_overrides_per_opponent"
            ]:
                errors.append(
                    f"{opponent}:overrides<"
                    f"{thresholds['min_overrides_per_opponent']}"
                )
            if _finite(row.get("mean", 0.0), field=f"{opponent}.mean") < 0.0:
                errors.append(f"{opponent}:negative_mean")
            if int(row.get("match_outcome_clusters", 0) or 0) < 1:
                errors.append(f"{opponent}:match_outcome_clusters<1")
            positive_rate = _finite(
                row.get("match_positive_rate", 0.0),
                field=f"{opponent}.match_positive_rate",
            )
            if positive_rate < thresholds["min_opponent_match_positive_rate"]:
                errors.append(
                    f"{opponent}:match_positive_rate<"
                    f"{thresholds['min_opponent_match_positive_rate']}"
                )
            if _finite(
                row.get("match_positive_uplift_mean", 0.0),
                field=f"{opponent}.match_positive_uplift_mean",
            ) < 0.0:
                errors.append(f"{opponent}:match_positive_uplift_mean<0")
    return errors


def build_policy_selection_result(
    phase: Mapping[str, Any],
    evaluation: Mapping[str, Any],
    *,
    thresholds: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if (
        phase.get("schema") != POLICY_SELECTION_PHASE_SCHEMA
        or evaluation.get("offline_estimand") != POLICY_OFFLINE_ESTIMAND
        or evaluation.get("match_outcome_estimand") != MATCH_OUTCOME_ESTIMAND
        or evaluation.get("deployment_policy_value") is not False
        or evaluation.get("strength_evidence") is not False
    ):
        raise ValueError("invalid offline policy selection evidence")
    limits = _thresholds(thresholds)
    errors = _gate_errors(evaluation, limits)
    selected = evaluation.get("selected_policy")
    selected_sha256 = _canonical_sha256(selected) if isinstance(selected, Mapping) else None
    result = {
        "schema": POLICY_SELECTION_RESULT_SCHEMA,
        "passed": not errors,
        "errors": errors,
        "run_id": phase.get("run_id"),
        "candidate_sha256": _digest(
            phase.get("candidate_sha256"), field="candidate_sha256"
        ),
        "role_manifest_sha256": _digest(
            phase.get("role_manifest_sha256"), field="role_manifest_sha256"
        ),
        "policy_selection_artifact_sha256": _digest(
            phase.get("policy_selection_artifact_sha256"),
            field="policy_selection_artifact_sha256",
        ),
        "calibration_payload_sha256": _digest(
            phase.get("calibration_payload_sha256"),
            field="calibration_payload_sha256",
        ),
        "evaluation_report_sha256": _canonical_sha256(evaluation),
        "selected_policy_sha256": selected_sha256,
        "offline_estimand": POLICY_OFFLINE_ESTIMAND,
        "match_outcome_estimand": MATCH_OUTCOME_ESTIMAND,
        "deployment_policy_value": False,
        "strength_evidence": False,
        "policy_gate_opened": False,
        "thresholds": limits,
        "summary": {
            key: evaluation.get(key)
            for key in (
                "overrides",
                "override_clusters",
                "match_cluster_bootstrap_mean_ci",
                "match_opponent_stratified_cluster_ci",
                "match_positive_rate_cluster_bootstrap_ci",
                "match_positive_rate_opponent_stratified_cluster_ci",
                "match_positive_uplift_cluster_bootstrap_ci",
                "match_positive_uplift_opponent_stratified_cluster_ci",
                "match_outcome_row_coverage",
                "match_outcome_cluster_coverage",
                "by_opponent",
            )
        },
    }
    if result["passed"] and selected_sha256 is None:
        raise RuntimeError("passing selection result has no selected policy")
    return result


def write_selection_result(path: Path, result: Mapping[str, Any]) -> str:
    if result.get("schema") != POLICY_SELECTION_RESULT_SCHEMA:
        raise ValueError("invalid policy selection result schema")
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(result, indent=2, sort_keys=True).encode()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return hashlib.sha256(raw).hexdigest()


def open_policy_gate(
    dataset: Any,
    *,
    candidate_sha256: str,
    selection_result_path: Path,
) -> dict[str, Any]:
    candidate = _digest(candidate_sha256, field="candidate_sha256")
    opened = dataset.open_role(
        "policy_gate",
        candidate_sha256=candidate,
        prerequisite_report=selection_result_path,
    )
    return {
        "schema": "policy_gate_phase_v1",
        "run_id": dataset.run_id,
        "candidate_sha256": candidate,
        "role_manifest_sha256": dataset.manifest_sha256,
        "policy_gate_artifact_sha256": opened["artifact_sha256"],
        "selection_result_sha256": opened["prerequisite_sha256"],
        "opponents": list(opened["opponents"]),
        "value": opened["value"],
        "behavior": opened["behavior"],
        "deployment_policy_value": False,
        "strength_evidence": False,
    }


def build_policy_gate_result(
    phase: Mapping[str, Any],
    evaluation: Mapping[str, Any],
    *,
    thresholds: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if (
        phase.get("schema") != "policy_gate_phase_v1"
        or phase.get("deployment_policy_value") is not False
        or phase.get("strength_evidence") is not False
        or evaluation.get("offline_estimand") != POLICY_OFFLINE_ESTIMAND
        or evaluation.get("match_outcome_estimand") != MATCH_OUTCOME_ESTIMAND
        or evaluation.get("policy_search_performed") is not False
        or evaluation.get("source_collection_complete") is not True
        or evaluation.get("deployment_policy_value") is not False
        or evaluation.get("strength_evidence") is not False
        or evaluation.get("config") != evaluation.get("selected_policy")
    ):
        raise ValueError("invalid offline policy gate evidence")
    limits = _thresholds(thresholds)
    errors = _gate_errors(evaluation, limits)
    selected = evaluation.get("selected_policy")
    selected_sha256 = (
        _canonical_sha256(selected) if isinstance(selected, Mapping) else None
    )
    result = {
        "schema": POLICY_GATE_RESULT_SCHEMA,
        "passed": not errors,
        "errors": errors,
        "run_id": phase.get("run_id"),
        "candidate_sha256": _digest(
            phase.get("candidate_sha256"), field="candidate_sha256"
        ),
        "role_manifest_sha256": _digest(
            phase.get("role_manifest_sha256"), field="role_manifest_sha256"
        ),
        "policy_gate_artifact_sha256": _digest(
            phase.get("policy_gate_artifact_sha256"),
            field="policy_gate_artifact_sha256",
        ),
        "selection_result_sha256": _digest(
            phase.get("selection_result_sha256"),
            field="selection_result_sha256",
        ),
        "evaluation_report_sha256": _canonical_sha256(evaluation),
        "selected_policy_sha256": selected_sha256,
        "offline_estimand": POLICY_OFFLINE_ESTIMAND,
        "match_outcome_estimand": MATCH_OUTCOME_ESTIMAND,
        "deployment_policy_value": False,
        "strength_evidence": False,
        "native_candidate_build_authorized": not errors,
        "thresholds": limits,
        "summary": {
            key: evaluation.get(key)
            for key in (
                "overrides",
                "override_clusters",
                "match_cluster_bootstrap_mean_ci",
                "match_opponent_stratified_cluster_ci",
                "match_positive_rate_cluster_bootstrap_ci",
                "match_positive_rate_opponent_stratified_cluster_ci",
                "match_positive_uplift_cluster_bootstrap_ci",
                "match_positive_uplift_opponent_stratified_cluster_ci",
                "match_outcome_row_coverage",
                "match_outcome_cluster_coverage",
                "by_opponent",
            )
        },
    }
    if result["passed"] and selected_sha256 is None:
        raise RuntimeError("passing policy gate has no selected policy")
    return result


def write_policy_gate_result(path: Path, result: Mapping[str, Any]) -> str:
    if result.get("schema") != POLICY_GATE_RESULT_SCHEMA:
        raise ValueError("invalid policy gate result schema")
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(result, indent=2, sort_keys=True).encode()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return hashlib.sha256(raw).hexdigest()
