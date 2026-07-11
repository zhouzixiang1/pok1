"""Strict supervision for the primary 70-hand national match outcome."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
from typing import Any

from feature_spec import LABELS


MATCH_OUTCOME_SCHEMA = "national_70_hand_match_outcome_supervision_v1"
MATCH_OUTCOME_ESTIMAND = (
    "single_decision_70_hand_positive_outcome_uplift_clustered_v1"
)
NATIONAL_MATCH_HANDS = 70
POSITIVE_OUTCOME_RULE = "net_chips_after_70_hands_gt_zero"


def _finite(value: Any, *, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be finite") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def _integer(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    try:
        exact = float(value) == float(number)
    except (TypeError, ValueError, OverflowError):
        exact = False
    if not exact:
        raise ValueError(f"{field} must be an integer")
    return number


def _binary_vector(value: Any, *, field: str) -> list[int]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{field} must be a binary sequence")
    if len(value) != len(LABELS):
        raise ValueError(f"{field} has the wrong dimension")
    result = []
    for item in value:
        number = _integer(item, field=field)
        if number not in (0, 1):
            raise ValueError(f"{field} must be binary")
        result.append(number)
    return result


def _sequence(value: Any, *, field: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{field} must be a sequence")
    if len(value) != len(LABELS):
        raise ValueError(f"{field} has the wrong dimension")
    return value


def _is_close(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=0.0, abs_tol=1e-6)


def _outcome_markers_present(row: Mapping[str, Any]) -> bool:
    return any(
        field in row
        for field in (
            "_collection_hands",
            "baseline_match_net_chips",
            "match_action_values",
        )
    )


def derive_match_outcome_supervision(
    row: Mapping[str, Any], *, required: bool = False
) -> dict[str, Any] | None:
    """Return exact absolute and binary outcome targets for one 70-hand row.

    Older short-match rows remain readable when ``required`` is false. A formal
    win-first pipeline must pass ``required=True`` so partial or non-70-hand
    evidence fails closed.
    """
    if not _outcome_markers_present(row):
        if required:
            raise ValueError("70-hand match outcome supervision is missing")
        return None

    try:
        hands = _integer(row.get("_collection_hands"), field="_collection_hands")
    except ValueError:
        if required:
            raise
        return None
    if hands != NATIONAL_MATCH_HANDS:
        if required:
            raise ValueError(
                f"match outcome supervision requires {NATIONAL_MATCH_HANDS} hands"
            )
        return None

    baseline = _finite(
        row.get("baseline_match_net_chips"),
        field="baseline_match_net_chips",
    )
    legal = _binary_vector(row.get("legal_mask"), field="legal_mask")
    rule_label_id = _integer(row.get("rule_label_id"), field="rule_label_id")
    if not 0 <= rule_label_id < len(LABELS) or not legal[rule_label_id]:
        raise ValueError("rule action is absent or illegal")

    deltas = _sequence(row.get("match_delta_vs_rule"), field="match_delta_vs_rule")
    absolute = _sequence(row.get("match_action_values"), field="match_action_values")
    masks = row.get("target_masks")
    masks = masks if isinstance(masks, Mapping) else {}
    mask = _binary_vector(
        masks.get("match_delta_vs_rule", row.get("target_mask")),
        field="target_masks.match_delta_vs_rule",
    )
    if any(observed and not legal[index] for index, observed in enumerate(mask)):
        raise ValueError("match outcome observes an illegal action")

    net_targets = [0.0] * len(LABELS)
    positive_targets = [0] * len(LABELS)
    baseline_positive = int(baseline > 0.0)
    for index, observed in enumerate(mask):
        delta_value = deltas[index]
        absolute_value = absolute[index]
        if not observed:
            if delta_value is not None or absolute_value is not None:
                raise ValueError("match outcome has a target outside its mask")
            continue
        delta = _finite(delta_value, field=f"match_delta_vs_rule[{index}]")
        net = _finite(absolute_value, field=f"match_action_values[{index}]")
        if not _is_close(net, baseline + delta):
            raise ValueError("absolute match target disagrees with match delta")
        net_targets[index] = net
        positive_targets[index] = int(net > 0.0)

    if not mask[rule_label_id]:
        raise ValueError("rule action has no match outcome target")
    if not _is_close(net_targets[rule_label_id], baseline):
        raise ValueError("rule absolute match target differs from baseline")
    if not _is_close(
        _finite(
            deltas[rule_label_id],
            field=f"match_delta_vs_rule[{rule_label_id}]",
        ),
        0.0,
    ):
        raise ValueError("rule match delta must be zero")

    for probe_index, probe in enumerate(row.get("probes") or []):
        if not isinstance(probe, Mapping):
            raise ValueError("probe must be an object")
        if probe.get("status") != "ok" or probe.get("force_confirmed") is not True:
            continue
        label = str(probe.get("forced_label") or "")
        if label not in LABELS:
            raise ValueError("confirmed probe has an unknown forced label")
        label_id = LABELS.index(label)
        if not mask[label_id]:
            raise ValueError("confirmed probe has no match outcome target")
        forced = _finite(
            probe.get("forced_match_net_chips"),
            field=f"probes[{probe_index}].forced_match_net_chips",
        )
        delta = _finite(
            probe.get("match_delta_vs_rule"),
            field=f"probes[{probe_index}].match_delta_vs_rule",
        )
        if not _is_close(forced, net_targets[label_id]):
            raise ValueError("confirmed probe disagrees with absolute match target")
        if not _is_close(forced, baseline + delta):
            raise ValueError("confirmed probe disagrees with match delta")

    return {
        "schema": MATCH_OUTCOME_SCHEMA,
        "estimand": MATCH_OUTCOME_ESTIMAND,
        "hands": NATIONAL_MATCH_HANDS,
        "positive_outcome_rule": POSITIVE_OUTCOME_RULE,
        "baseline_match_net_chips": baseline,
        "baseline_match_positive": baseline_positive,
        "match_net_chips_targets": net_targets,
        "match_positive_targets": positive_targets,
        "match_positive_uplift_targets": [
            value - baseline_positive if mask[index] else 0
            for index, value in enumerate(positive_targets)
        ],
        "target_mask": mask,
    }


def policy_outcome_context(supervision: Mapping[str, Any]) -> dict[str, Any]:
    if supervision.get("schema") != MATCH_OUTCOME_SCHEMA:
        raise ValueError("unsupported match outcome supervision")
    baseline_positive = _integer(
        supervision.get("baseline_match_positive"),
        field="baseline_match_positive",
    )
    baseline_net = _finite(
        supervision.get("baseline_match_net_chips"),
        field="baseline_match_net_chips",
    )
    if baseline_positive not in (0, 1) or baseline_positive != int(
        baseline_net > 0.0
    ):
        raise ValueError("baseline match outcome is inconsistent")
    return {
        "schema": MATCH_OUTCOME_SCHEMA,
        "estimand": MATCH_OUTCOME_ESTIMAND,
        "hands": NATIONAL_MATCH_HANDS,
        "positive_outcome_rule": POSITIVE_OUTCOME_RULE,
        "baseline_match_net_chips": baseline_net,
        "baseline_match_positive": baseline_positive,
    }


def candidate_outcome(
    supervision: Mapping[str, Any], label_id: int
) -> dict[str, Any]:
    if supervision.get("schema") != MATCH_OUTCOME_SCHEMA:
        raise ValueError("unsupported match outcome supervision")
    label_id = _integer(label_id, field="label_id")
    if not 0 <= label_id < len(LABELS):
        raise ValueError("label_id is out of range")
    mask = _binary_vector(supervision.get("target_mask"), field="target_mask")
    if not mask[label_id]:
        raise ValueError("candidate has no match outcome target")
    net = _finite(
        supervision["match_net_chips_targets"][label_id],
        field="forced_match_net_chips",
    )
    positive = _integer(
        supervision["match_positive_targets"][label_id],
        field="forced_match_positive",
    )
    uplift = _integer(
        supervision["match_positive_uplift_targets"][label_id],
        field="match_positive_uplift",
    )
    if positive not in (0, 1) or uplift not in (-1, 0, 1):
        raise ValueError("candidate match outcome targets are invalid")
    if positive != int(net > 0.0):
        raise ValueError("candidate match outcome targets are inconsistent")
    return {
        "match_outcome_schema": MATCH_OUTCOME_SCHEMA,
        "forced_match_net_chips": net,
        "forced_match_positive": positive,
        "match_positive_uplift": uplift,
    }


def match_outcome_metadata() -> dict[str, Any]:
    return {
        "schema": MATCH_OUTCOME_SCHEMA,
        "estimand": MATCH_OUTCOME_ESTIMAND,
        "hands": NATIONAL_MATCH_HANDS,
        "positive_outcome_rule": POSITIVE_OUTCOME_RULE,
        "optional_for_v3": True,
        "required_for_win_first_policy_evidence": True,
    }
