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
    POLICY_OFFLINE_ESTIMAND_V4,
    POLICY_SELECTION_RESULT_SCHEMA,
    POLICY_SELECTION_RESULT_SCHEMA_V4,
)


POLICY_SELECTION_PHASE_SCHEMA = "policy_selection_phase_v1"
POLICY_GATE_RESULT_SCHEMA = "policy_gate_result_v2"
POLICY_SELECTION_PHASE_SCHEMA_V4 = "policy_selection_phase_v2_win_first_v4"
POLICY_GATE_PHASE_SCHEMA_V4 = "policy_gate_phase_v2_win_first_v4"
POLICY_GATE_RESULT_SCHEMA_V4 = "policy_gate_result_v3_win_first_v4"
BOOTSTRAP_CONTRACT_SCHEMA = "observed_70_hand_match_cluster_bootstrap_v1"
POLICY_EVIDENCE_CONTRACTS = {
    "v3": {
        "selection_phase_schema": POLICY_SELECTION_PHASE_SCHEMA,
        "selection_result_schema": POLICY_SELECTION_RESULT_SCHEMA,
        "gate_phase_schema": "policy_gate_phase_v1",
        "gate_result_schema": POLICY_GATE_RESULT_SCHEMA,
        "offline_estimand": POLICY_OFFLINE_ESTIMAND,
    },
    "v4": {
        "selection_phase_schema": POLICY_SELECTION_PHASE_SCHEMA_V4,
        "selection_result_schema": POLICY_SELECTION_RESULT_SCHEMA_V4,
        "gate_phase_schema": POLICY_GATE_PHASE_SCHEMA_V4,
        "gate_result_schema": POLICY_GATE_RESULT_SCHEMA_V4,
        "offline_estimand": POLICY_OFFLINE_ESTIMAND_V4,
    },
}
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
V4_SELECTION_EXTRA_DEFAULT_THRESHOLDS = {
    "min_selection_clusters": 8,
    "min_override_hand_mean": 0.0,
    "bootstrap_samples": 2000,
}
EVIDENCE_SUMMARY_FIELDS = (
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
    "bootstrap_contract",
)
V4_SELECTION_SUMMARY_FIELDS = (
    *EVIDENCE_SUMMARY_FIELDS,
    "rows",
    "match_outcome_rows",
    "match_outcome_clusters",
    "match_clusters",
    "override_hand_mean",
)


def _contract(name: str) -> dict[str, str]:
    try:
        return POLICY_EVIDENCE_CONTRACTS[str(name)]
    except KeyError as exc:
        raise ValueError(f"unsupported policy evidence contract: {name}") from exc


def build_bootstrap_contract(*, samples: int, seed: int) -> dict[str, Any]:
    for value, field in ((samples, "samples"), (seed, "seed")):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"bootstrap {field} must be an integer")
    if samples < 1:
        raise ValueError("bootstrap samples must be positive")
    return {
        "schema": BOOTSTRAP_CONTRACT_SCHEMA,
        "samples": samples,
        "seed": seed,
        "observed_70_hand_match_clusters": True,
        "ordinary": True,
        "opponent_stratified": True,
    }


def _validate_bootstrap_contract(evaluation: Mapping[str, Any]) -> dict[str, Any]:
    raw = evaluation.get("bootstrap_contract")
    if not isinstance(raw, Mapping) or set(raw) != {
        "schema",
        "samples",
        "seed",
        "observed_70_hand_match_clusters",
        "ordinary",
        "opponent_stratified",
    }:
        raise ValueError("v4 policy evidence bootstrap contract is missing")
    normalized = build_bootstrap_contract(
        samples=raw.get("samples"), seed=raw.get("seed")
    )
    if dict(raw) != normalized:
        raise ValueError("v4 policy evidence bootstrap contract changed")
    return normalized


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
    contract: str = "v3",
) -> dict[str, Any]:
    evidence_contract = _contract(contract)
    candidate = _digest(candidate_sha256, field="candidate_sha256")
    calibration = _digest(
        calibration_payload_sha256, field="calibration_payload_sha256"
    )
    opened = dataset.open_role(
        "policy_selection", candidate_sha256=candidate
    )
    return {
        "schema": evidence_contract["selection_phase_schema"],
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


def _selection_thresholds(
    overrides: Mapping[str, Any] | None, *, contract: str
) -> dict[str, float | int]:
    if contract != "v4":
        return _thresholds(overrides)
    updates = dict(overrides or {})
    allowed = set(DEFAULT_THRESHOLDS) | set(V4_SELECTION_EXTRA_DEFAULT_THRESHOLDS)
    unknown = set(updates) - allowed
    if unknown:
        raise ValueError(f"unknown policy evidence thresholds: {sorted(unknown)}")
    base = _thresholds({
        key: value for key, value in updates.items() if key in DEFAULT_THRESHOLDS
    })
    extras = dict(V4_SELECTION_EXTRA_DEFAULT_THRESHOLDS)
    extras.update({
        key: value
        for key, value in updates.items()
        if key in V4_SELECTION_EXTRA_DEFAULT_THRESHOLDS
    })
    for field in ("min_selection_clusters", "bootstrap_samples"):
        value = extras[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{field} must be a positive integer")
    extras["min_override_hand_mean"] = _finite(
        extras["min_override_hand_mean"], field="min_override_hand_mean"
    )
    return {**base, **extras}


def _summary_projection(
    evaluation: Mapping[str, Any], *, contract: str, selection: bool
) -> dict[str, Any]:
    fields = (
        V4_SELECTION_SUMMARY_FIELDS
        if contract == "v4" and selection
        else EVIDENCE_SUMMARY_FIELDS
    )
    return {key: evaluation.get(key) for key in fields}


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


def _v4_selection_extra_errors(
    evaluation: Mapping[str, Any], thresholds: Mapping[str, Any]
) -> list[str]:
    errors = []
    if int(evaluation.get("match_clusters", 0) or 0) < thresholds[
        "min_selection_clusters"
    ]:
        errors.append(
            f"selection_clusters<{thresholds['min_selection_clusters']}"
        )
    if _finite(
        evaluation.get("override_hand_mean", 0.0), field="override_hand_mean"
    ) < thresholds["min_override_hand_mean"]:
        errors.append(
            f"override_hand_mean<{thresholds['min_override_hand_mean']}"
        )
    return errors


def _validated_ci(
    evaluation: Mapping[str, Any],
    field: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> dict[str, float]:
    raw = evaluation.get(field)
    if not isinstance(raw, Mapping) or set(raw) != {"lower", "mean", "upper"}:
        raise ValueError(f"formal v4 selection {field} is invalid")
    result = {
        key: _finite(raw.get(key), field=f"{field}.{key}")
        for key in ("lower", "mean", "upper")
    }
    if not result["lower"] <= result["mean"] <= result["upper"]:
        raise ValueError(f"formal v4 selection {field} is not ordered")
    if minimum is not None and result["lower"] < minimum:
        raise ValueError(f"formal v4 selection {field} is below its domain")
    if maximum is not None and result["upper"] > maximum:
        raise ValueError(f"formal v4 selection {field} is above its domain")
    return result


def _nonnegative_integer(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


def verify_formal_v4_selection_evidence(
    evaluation: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    expected_opponents: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Independently recompute every protected selection acceptance check."""
    if (
        evaluation.get("offline_estimand") != POLICY_OFFLINE_ESTIMAND_V4
        or evaluation.get("match_outcome_estimand") != MATCH_OUTCOME_ESTIMAND
        or evaluation.get("source_collection_complete") is not True
        or evaluation.get("deployment_policy_value") is not False
        or evaluation.get("strength_evidence") is not False
        or result.get("schema") != POLICY_SELECTION_RESULT_SCHEMA_V4
        or result.get("offline_estimand") != POLICY_OFFLINE_ESTIMAND_V4
        or result.get("match_outcome_estimand") != MATCH_OUTCOME_ESTIMAND
        or result.get("passed") is not True
        or result.get("errors") != []
        or result.get("formal_selection") is not True
        or result.get("source_collection_complete") is not True
        or result.get("policy_gate_opened") is not False
        or result.get("deployment_policy_value") is not False
        or result.get("strength_evidence") is not False
        or result.get("evaluation_report_sha256")
        != _canonical_sha256(evaluation)
    ):
        raise ValueError("formal v4 selection evidence status is invalid")
    limits = _selection_thresholds(result.get("thresholds"), contract="v4")
    if dict(result.get("thresholds") or {}) != limits:
        raise ValueError("formal v4 selection thresholds are not canonical")
    if (
        limits["min_overrides"] < 12
        or limits["min_selection_clusters"] < 8
        or limits["min_override_clusters"] < 8
        or limits["min_overrides_per_opponent"] < 4
        or limits["bootstrap_samples"] < 2000
        or limits["min_override_hand_mean"] < 0.0
        or limits["min_cluster_ci_lower"] < 0.0
        or limits["min_opponent_stratified_ci_lower"] < 0.0
    ):
        raise ValueError("formal v4 selection thresholds were weakened")
    bootstrap = _validate_bootstrap_contract(evaluation)
    if (
        bootstrap["samples"] < 2000
        or bootstrap["samples"] != limits["bootstrap_samples"]
    ):
        raise ValueError("formal v4 selection bootstrap coverage is insufficient")
    expected_summary = _summary_projection(
        evaluation, contract="v4", selection=True
    )
    if result.get("summary") != expected_summary:
        raise ValueError("formal v4 selection summary changed")
    for field in (
        "match_cluster_bootstrap_mean_ci",
        "match_opponent_stratified_cluster_ci",
    ):
        _validated_ci(evaluation, field)
    positive_rate = _finite(
        evaluation.get("match_positive_rate"), field="match_positive_rate"
    )
    rule_positive_rate = _finite(
        evaluation.get("rule_match_positive_rate"),
        field="rule_match_positive_rate",
    )
    positive_uplift = _finite(
        evaluation.get("match_positive_uplift_mean"),
        field="match_positive_uplift_mean",
    )
    if (
        not 0.0 <= positive_rate <= 1.0
        or not 0.0 <= rule_positive_rate <= 1.0
        or not -1.0 <= positive_uplift <= 1.0
        or not math.isclose(
            positive_uplift,
            positive_rate - rule_positive_rate,
            rel_tol=0.0,
            abs_tol=1.0e-12,
        )
    ):
        raise ValueError("formal v4 selection outcome means are outside domain")
    for field in (
        "match_positive_rate_cluster_bootstrap_ci",
        "match_positive_rate_opponent_stratified_cluster_ci",
        "rule_match_positive_rate_cluster_bootstrap_ci",
        "rule_match_positive_rate_opponent_stratified_cluster_ci",
    ):
        ci = _validated_ci(evaluation, field, minimum=0.0, maximum=1.0)
        if "opponent_stratified" not in field:
            expected = (
                rule_positive_rate if field.startswith("rule_") else positive_rate
            )
            if not math.isclose(
                ci["mean"], expected, rel_tol=0.0, abs_tol=1.0e-12
            ):
                raise ValueError(f"formal v4 selection {field} mean changed")
    for field in (
        "match_positive_uplift_cluster_bootstrap_ci",
        "match_positive_uplift_opponent_stratified_cluster_ci",
    ):
        ci = _validated_ci(evaluation, field, minimum=-1.0, maximum=1.0)
        if "opponent_stratified" not in field and not math.isclose(
            ci["mean"], positive_uplift, rel_tol=0.0, abs_tol=1.0e-12
        ):
            raise ValueError(f"formal v4 selection {field} mean changed")
    rows = _nonnegative_integer(evaluation.get("rows"), field="rows")
    overrides = _nonnegative_integer(
        evaluation.get("overrides"), field="overrides"
    )
    outcome_rows = _nonnegative_integer(
        evaluation.get("match_outcome_rows"), field="match_outcome_rows"
    )
    match_clusters = _nonnegative_integer(
        evaluation.get("match_clusters"), field="match_clusters"
    )
    override_clusters = _nonnegative_integer(
        evaluation.get("override_clusters"), field="override_clusters"
    )
    outcome_clusters = _nonnegative_integer(
        evaluation.get("match_outcome_clusters"),
        field="match_outcome_clusters",
    )
    if (
        _finite(
            evaluation.get("match_outcome_row_coverage"),
            field="match_outcome_row_coverage",
        ) != 1.0
        or _finite(
            evaluation.get("match_outcome_cluster_coverage"),
            field="match_outcome_cluster_coverage",
        ) != 1.0
        or outcome_rows != rows
        or outcome_clusters != match_clusters
        or overrides > rows
        or override_clusters > match_clusters
        or _nonnegative_integer(
            evaluation.get("grid_size"), field="grid_size"
        ) < 1
        or evaluation.get("selection_failure") is not None
        or evaluation.get("config") != evaluation.get("selected_policy")
        or match_clusters < limits["min_selection_clusters"]
        or _finite(
            evaluation.get("override_hand_mean"), field="override_hand_mean"
        ) < limits["min_override_hand_mean"]
    ):
        raise ValueError("formal v4 selection coverage is insufficient")
    for field in (
        "override_rate",
        "weighted_override_rate",
        "negative_override_rate",
    ):
        rate = _finite(evaluation.get(field), field=field)
        if not 0.0 <= rate <= 1.0:
            raise ValueError(f"formal v4 selection {field} is outside [0, 1]")
    flip_counts = evaluation.get("match_positive_flip_counts")
    expected_flip_fields = {
        "negative_to_positive",
        "positive_to_negative",
        "unchanged_positive",
        "unchanged_nonpositive",
    }
    if not isinstance(flip_counts, Mapping) or set(flip_counts) != expected_flip_fields:
        raise ValueError("formal v4 selection outcome flip counts are invalid")
    if sum(
        _nonnegative_integer(value, field=f"match_positive_flip_counts.{field}")
        for field, value in flip_counts.items()
    ) != outcome_rows:
        raise ValueError("formal v4 selection outcome flip totals changed")
    overrides_by_action = evaluation.get("overrides_by_action")
    if not isinstance(overrides_by_action, Mapping) or sum(
        _nonnegative_integer(value, field=f"overrides_by_action.{field}")
        for field, value in overrides_by_action.items()
    ) != overrides:
        raise ValueError("formal v4 selection action override totals changed")
    errors = _gate_errors(evaluation, limits)
    if errors:
        raise ValueError(f"formal v4 selection evidence failed: {errors}")
    by_opponent = evaluation.get("by_opponent")
    if not isinstance(by_opponent, Mapping) or not by_opponent:
        raise ValueError("formal v4 selection opponent evidence is missing")
    if expected_opponents is not None and set(by_opponent) != set(expected_opponents):
        raise ValueError("formal v4 selection opponent coverage changed")
    opponent_rows = 0
    opponent_clusters = 0
    opponent_overrides = 0
    opponent_override_clusters = 0
    opponent_negative_overrides = 0
    for opponent, raw in by_opponent.items():
        if not isinstance(raw, Mapping):
            raise ValueError(f"formal v4 selection opponent is invalid: {opponent}")
        for field in (
            "mean",
            "match_positive_rate",
            "rule_match_positive_rate",
            "match_positive_uplift_mean",
        ):
            _finite(raw.get(field), field=f"{opponent}.{field}")
        opponent_positive_rate = float(raw["match_positive_rate"])
        opponent_rule_positive_rate = float(raw["rule_match_positive_rate"])
        opponent_uplift = float(raw["match_positive_uplift_mean"])
        if (
            not 0.0 <= opponent_positive_rate <= 1.0
            or not 0.0 <= opponent_rule_positive_rate <= 1.0
            or not -1.0 <= opponent_uplift <= 1.0
            or not math.isclose(
                opponent_uplift,
                opponent_positive_rate - opponent_rule_positive_rate,
                rel_tol=0.0,
                abs_tol=1.0e-12,
            )
        ):
            raise ValueError(
                f"formal v4 selection opponent outcome domain failed: {opponent}"
            )
        current_rows = _nonnegative_integer(
            raw.get("rows"), field=f"{opponent}.rows"
        )
        current_clusters = _nonnegative_integer(
            raw.get("clusters"), field=f"{opponent}.clusters"
        )
        current_overrides = _nonnegative_integer(
            raw.get("overrides"), field=f"{opponent}.overrides"
        )
        current_override_clusters = _nonnegative_integer(
            raw.get("override_clusters"),
            field=f"{opponent}.override_clusters",
        )
        current_negative_overrides = _nonnegative_integer(
            raw.get("negative_overrides"),
            field=f"{opponent}.negative_overrides",
        )
        opponent_outcome_clusters = _nonnegative_integer(
            raw.get("match_outcome_clusters"),
            field=f"{opponent}.match_outcome_clusters",
        )
        if (
            current_rows < 1
            or current_clusters < 1
            or current_overrides > current_rows
            or current_override_clusters > current_clusters
            or current_negative_overrides > current_overrides
            or opponent_outcome_clusters != current_clusters
        ):
            raise ValueError(
                f"formal v4 selection opponent coverage failed: {opponent}"
            )
        opponent_rows += current_rows
        opponent_clusters += current_clusters
        opponent_overrides += current_overrides
        opponent_override_clusters += current_override_clusters
        opponent_negative_overrides += current_negative_overrides
    if (
        opponent_rows != rows
        or opponent_clusters != match_clusters
        or opponent_overrides != overrides
        or opponent_override_clusters != override_clusters
        or opponent_negative_overrides > overrides
    ):
        raise ValueError("formal v4 selection opponent totals changed")
    expected_override_names = sorted(
        opponent
        for opponent, raw in by_opponent.items()
        if int(raw["overrides"]) > 0
    )
    if (
        _nonnegative_integer(
            evaluation.get("override_opponents"), field="override_opponents"
        ) != len(expected_override_names)
        or evaluation.get("override_opponent_names") != expected_override_names
    ):
        raise ValueError("formal v4 selection override opponent coverage changed")
    return {
        "thresholds": limits,
        "bootstrap_contract": bootstrap,
        "summary": expected_summary,
    }


def build_policy_selection_result(
    phase: Mapping[str, Any],
    evaluation: Mapping[str, Any],
    *,
    thresholds: Mapping[str, Any] | None = None,
    contract: str = "v3",
) -> dict[str, Any]:
    evidence_contract = _contract(contract)
    if (
        phase.get("schema") != evidence_contract["selection_phase_schema"]
        or evaluation.get("offline_estimand")
        != evidence_contract["offline_estimand"]
        or evaluation.get("match_outcome_estimand") != MATCH_OUTCOME_ESTIMAND
        or evaluation.get("deployment_policy_value") is not False
        or evaluation.get("strength_evidence") is not False
    ):
        raise ValueError("invalid offline policy selection evidence")
    limits = _selection_thresholds(thresholds, contract=contract)
    if contract == "v4" and min(
        limits["min_cluster_ci_lower"],
        limits["min_opponent_stratified_ci_lower"],
    ) < 0.0:
        raise ValueError("v4 policy evidence CI floors cannot be negative")
    if contract == "v4":
        _validate_bootstrap_contract(evaluation)
    errors = _gate_errors(evaluation, limits)
    if contract == "v4":
        errors.extend(_v4_selection_extra_errors(evaluation, limits))
    selected = evaluation.get("selected_policy")
    selected_sha256 = _canonical_sha256(selected) if isinstance(selected, Mapping) else None
    result = {
        "schema": evidence_contract["selection_result_schema"],
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
        "offline_estimand": evidence_contract["offline_estimand"],
        "match_outcome_estimand": MATCH_OUTCOME_ESTIMAND,
        "deployment_policy_value": False,
        "strength_evidence": False,
        "policy_gate_opened": False,
        "thresholds": limits,
        "summary": _summary_projection(
            evaluation, contract=contract, selection=True
        ),
    }
    if result["passed"] and selected_sha256 is None:
        raise RuntimeError("passing selection result has no selected policy")
    return result


def write_selection_result(path: Path, result: Mapping[str, Any]) -> str:
    if result.get("schema") not in {
        contract["selection_result_schema"]
        for contract in POLICY_EVIDENCE_CONTRACTS.values()
    }:
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
    contract: str = "v3",
) -> dict[str, Any]:
    evidence_contract = _contract(contract)
    candidate = _digest(candidate_sha256, field="candidate_sha256")
    role_kwargs = {
        "candidate_sha256": candidate,
        "prerequisite_report": selection_result_path,
        "prerequisite_schema": evidence_contract[
            "selection_result_schema"
        ],
        "prerequisite_offline_estimand": evidence_contract[
            "offline_estimand"
        ],
    }
    opened = dataset.open_role("policy_gate", **role_kwargs)
    return {
        "schema": evidence_contract["gate_phase_schema"],
        "run_id": dataset.run_id,
        "candidate_sha256": candidate,
        "role_manifest_sha256": dataset.manifest_sha256,
        "policy_gate_artifact_sha256": opened["artifact_sha256"],
        "selection_result_sha256": opened["prerequisite_sha256"],
        "calibration_payload_sha256": opened[
            "prerequisite_calibration_payload_sha256"
        ],
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
    contract: str = "v3",
) -> dict[str, Any]:
    evidence_contract = _contract(contract)
    if (
        phase.get("schema") != evidence_contract["gate_phase_schema"]
        or phase.get("deployment_policy_value") is not False
        or phase.get("strength_evidence") is not False
        or evaluation.get("offline_estimand")
        != evidence_contract["offline_estimand"]
        or evaluation.get("match_outcome_estimand") != MATCH_OUTCOME_ESTIMAND
        or evaluation.get("policy_search_performed") is not False
        or evaluation.get("source_collection_complete") is not True
        or evaluation.get("deployment_policy_value") is not False
        or evaluation.get("strength_evidence") is not False
        or evaluation.get("config") != evaluation.get("selected_policy")
    ):
        raise ValueError("invalid offline policy gate evidence")
    limits = _thresholds(thresholds)
    if contract == "v4" and min(
        limits["min_cluster_ci_lower"],
        limits["min_opponent_stratified_ci_lower"],
    ) < 0.0:
        raise ValueError("v4 policy evidence CI floors cannot be negative")
    if contract == "v4":
        _validate_bootstrap_contract(evaluation)
    errors = _gate_errors(evaluation, limits)
    selected = evaluation.get("selected_policy")
    selected_sha256 = (
        _canonical_sha256(selected) if isinstance(selected, Mapping) else None
    )
    result = {
        "schema": evidence_contract["gate_result_schema"],
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
        "calibration_payload_sha256": _digest(
            phase.get("calibration_payload_sha256"),
            field="calibration_payload_sha256",
        ),
        "evaluation_report_sha256": _canonical_sha256(evaluation),
        "selected_policy_sha256": selected_sha256,
        "offline_estimand": evidence_contract["offline_estimand"],
        "match_outcome_estimand": MATCH_OUTCOME_ESTIMAND,
        "deployment_policy_value": False,
        "strength_evidence": False,
        "native_candidate_build_authorized": not errors,
        "thresholds": limits,
        "summary": _summary_projection(
            evaluation, contract=contract, selection=False
        ),
    }
    if result["passed"] and selected_sha256 is None:
        raise RuntimeError("passing policy gate has no selected policy")
    return result


def write_policy_gate_result(path: Path, result: Mapping[str, Any]) -> str:
    if result.get("schema") not in {
        contract["gate_result_schema"]
        for contract in POLICY_EVIDENCE_CONTRACTS.values()
    }:
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
