"""Leakage-safe data assembly for the next multi-task trainer."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
from typing import Any

from feature_spec import LABELS, label_action
from model_input_schema import encode_model_input, model_input_metadata
from opponent_response_schema import (
    OPPONENT_ACTION_LABELS,
    OPPONENT_RESPONSE_SCHEMA,
    annotate_response_rows,
    response_schema_metadata,
)
from sampling_weights import attach_training_row_weights, opponent_label
from strategy_context_schema import (
    STRATEGY_CONTEXT_DIM,
    STRATEGY_CONTEXT_SCHEMA,
    strategy_context_metadata,
)


MULTITASK_TRAINING_DATA_SCHEMA = "multitask_role_training_data_v1"
MODEL_DEVELOPMENT_ROLES = ("train", "early_stop", "model_calibration")
MODEL_TRAINING_ROLES = ("train", "early_stop")
MODEL_CALIBRATION_ROLE = "model_calibration"
POLICY_ROLES = ("policy_selection", "policy_gate")
FROZEN_CHECKPOINT_SCHEMA = "frozen_model_checkpoint_v1"
VALUE_WEIGHTING = "opponent_balanced_sampling_ipw"
BEHAVIOR_WEIGHTING = "opponent_balanced"
TRAIN_WEIGHT_FIELD = "_training_loss_weight"
EVALUATION_WEIGHT_FIELD = "_evaluation_metric_weight"
HERO_RESPONSE_ACTION_SCHEMA = "hero_response_action_v2"
HERO_RESPONSE_ACTION_FIELDS = (
    *(f"hero_{label}" for label in LABELS),
    "hero_commit_fraction",
    "hero_commit_pot_ratio_norm",
    "opponent_to_call_fraction",
    "hero_stack_after_fraction",
)
HERO_RESPONSE_ACTION_DIM = len(HERO_RESPONSE_ACTION_FIELDS)
INITIAL_CHIPS = 20_000.0


def _finite(value: Any, *, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be finite") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def _bounded_vector(values: Sequence[Any], *, field: str) -> list[float]:
    result = []
    for value in values:
        number = _finite(value, field=field)
        if not 0.0 <= number <= 1.0:
            raise ValueError(f"{field} values must be in [0, 1]")
        result.append(number)
    return result


def _model_weight_rows(
    rows: list[dict[str, Any]], *, role: str, modality: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    scheme = VALUE_WEIGHTING if modality == "value" else BEHAVIOR_WEIGHTING
    weighted, report = attach_training_row_weights(
        rows, scheme=scheme, modality=modality
    )
    if role != "train":
        for row in weighted:
            row[EVALUATION_WEIGHT_FIELD] = row.pop(TRAIN_WEIGHT_FIELD)
        report = dict(report)
        report["row_weight_field"] = EVALUATION_WEIGHT_FIELD
        report["used_for_gradient_updates"] = False
    else:
        report = dict(report)
        report["row_weight_field"] = TRAIN_WEIGHT_FIELD
        report["used_for_gradient_updates"] = True
    return weighted, report


def _prepare_model_role(dataset: Any, role: str) -> dict[str, Any]:
    if role not in MODEL_DEVELOPMENT_ROLES:
        raise ValueError(f"role is not available to model training: {role}")
    opened = dataset.open_role(role)
    if opened.get("role") != role:
        raise RuntimeError(f"dataset returned the wrong role: {opened.get('role')}")
    value_source = opened.get("value")
    behavior_source = opened.get("behavior")
    if not isinstance(value_source, list) or not isinstance(behavior_source, list):
        raise RuntimeError(f"dataset role has invalid row containers: {role}")
    value_rows = [dict(row) for row in value_source]
    behavior_rows = annotate_response_rows(
        [dict(row) for row in behavior_source], strict=True
    )
    if any(
        row.get("response_schema") != OPPONENT_RESPONSE_SCHEMA
        or row.get("response_target_mask") != 1
        for row in behavior_rows
    ):
        raise RuntimeError(f"role contains incomplete response supervision: {role}")

    value_rows, value_weighting = _model_weight_rows(
        value_rows, role=role, modality="value"
    )
    behavior_rows, behavior_weighting = _model_weight_rows(
        behavior_rows, role=role, modality="behavior"
    )
    opponents = sorted({
        *(opponent_label(row) for row in value_rows),
        *(opponent_label(row) for row in behavior_rows),
    })
    expected_opponents = sorted(str(name) for name in opened.get("opponents", []))
    if opponents != expected_opponents:
        raise RuntimeError(f"prepared role opponent coverage changed: {role}")
    return {
        "role": role,
        "opponents": opponents,
        "value": value_rows,
        "behavior": behavior_rows,
        "weighting": {
            "value": value_weighting,
            "behavior": behavior_weighting,
        },
        "provenance": {
            "artifact_sha256": opened.get("artifact_sha256"),
            "manifest_sha256": opened.get("manifest_sha256"),
            "candidate_sha256": opened.get("candidate_sha256"),
        },
    }


def _manifest_sha256(dataset: Any) -> str:
    digest = str(getattr(dataset, "manifest_sha256", ""))
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("dataset manifest_sha256 must be a lowercase SHA-256 digest")
    return digest


def _checkpoint_authorization(
    dataset: Any,
    training_phase: Mapping[str, Any],
    authorization: Mapping[str, Any],
) -> str:
    if not isinstance(authorization, Mapping):
        raise ValueError("model calibration requires a frozen checkpoint authorization")
    if not isinstance(training_phase, Mapping):
        raise ValueError("model calibration requires a prepared training phase")
    digest = str(authorization.get("checkpoint_sha256", ""))
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("checkpoint_sha256 must be a lowercase SHA-256 digest")
    phase_roles = training_phase.get("roles")
    if (
        training_phase.get("schema") != MULTITASK_TRAINING_DATA_SCHEMA
        or training_phase.get("phase") != "training"
        or training_phase.get("run_id") != getattr(dataset, "run_id", None)
        or training_phase.get("role_manifest_sha256") != _manifest_sha256(dataset)
        or training_phase.get("opened_roles") != list(MODEL_TRAINING_ROLES)
        or not isinstance(phase_roles, Mapping)
        or set(phase_roles) != set(MODEL_TRAINING_ROLES)
    ):
        raise ValueError("checkpoint authorization is not bound to this training run")
    training_artifacts = {
        role: phase_roles[role]["provenance"]["artifact_sha256"]
        for role in MODEL_TRAINING_ROLES
    }
    if (
        authorization.get("schema") != FROZEN_CHECKPOINT_SCHEMA
        or authorization.get("frozen") is not True
        or authorization.get("early_stop_complete") is not True
        or authorization.get("run_id") != getattr(dataset, "run_id", None)
        or authorization.get("role_manifest_sha256") != _manifest_sha256(dataset)
        or authorization.get("training_roles") != list(MODEL_TRAINING_ROLES)
        or authorization.get("training_artifact_sha256") != training_artifacts
    ):
        raise ValueError("checkpoint authorization is not bound to this training run")
    return digest


def prepare_training_phase(dataset: Any) -> dict[str, Any]:
    """Open gradient training and early-stop roles, but not calibration."""
    roles = {
        role: _prepare_model_role(dataset, role)
        for role in MODEL_TRAINING_ROLES
    }
    return {
        "schema": MULTITASK_TRAINING_DATA_SCHEMA,
        "phase": "training",
        "run_id": getattr(dataset, "run_id", None),
        "role_manifest_sha256": _manifest_sha256(dataset),
        "opened_roles": list(MODEL_TRAINING_ROLES),
        "roles": roles,
    }


def prepare_model_calibration(
    dataset: Any,
    training_phase: Mapping[str, Any],
    authorization: Mapping[str, Any],
) -> dict[str, Any]:
    """Open calibration only after a run-bound early-stop checkpoint freezes."""
    checkpoint_sha256 = _checkpoint_authorization(
        dataset, training_phase, authorization
    )
    role = _prepare_model_role(dataset, MODEL_CALIBRATION_ROLE)
    return {
        "schema": MULTITASK_TRAINING_DATA_SCHEMA,
        "phase": "model_calibration",
        "run_id": getattr(dataset, "run_id", None),
        "role_manifest_sha256": _manifest_sha256(dataset),
        "checkpoint_sha256": checkpoint_sha256,
        "opened_roles": [MODEL_CALIBRATION_ROLE],
        "roles": {MODEL_CALIBRATION_ROLE: role},
    }


def combine_model_development(
    training: Mapping[str, Any], calibration: Mapping[str, Any]
) -> dict[str, Any]:
    """Combine two already-opened phases without granting any new access."""
    if (
        training.get("schema") != MULTITASK_TRAINING_DATA_SCHEMA
        or training.get("phase") != "training"
        or calibration.get("schema") != MULTITASK_TRAINING_DATA_SCHEMA
        or calibration.get("phase") != "model_calibration"
        or training.get("run_id") != calibration.get("run_id")
        or training.get("role_manifest_sha256")
        != calibration.get("role_manifest_sha256")
    ):
        raise ValueError("training and calibration phases are not compatible")
    roles = {**training["roles"], **calibration["roles"]}
    seen: dict[str, str] = {}
    for role, payload in roles.items():
        for opponent in payload["opponents"]:
            if opponent in seen:
                raise RuntimeError(
                    f"opponent appears in multiple model roles: "
                    f"{opponent} ({seen[opponent]}, {role})"
                )
            seen[opponent] = role
    manifest_sha256 = str(training["role_manifest_sha256"])
    if any(
        payload["provenance"]["manifest_sha256"] != manifest_sha256
        for payload in roles.values()
    ):
        raise RuntimeError("model roles came from different role manifests")
    return {
        "schema": MULTITASK_TRAINING_DATA_SCHEMA,
        "run_id": training.get("run_id"),
        "role_manifest_sha256": manifest_sha256,
        "checkpoint_sha256": calibration["checkpoint_sha256"],
        "opened_roles": list(MODEL_DEVELOPMENT_ROLES),
        "policy_roles_opened": False,
        "roles": roles,
        "contracts": training_data_metadata(),
    }


def _hero_response_action_features(row: Mapping[str, Any]) -> list[float]:
    request = row.get("request") if isinstance(row.get("request"), Mapping) else {}
    context = (
        row.get("response_context")
        if isinstance(row.get("response_context"), Mapping)
        else {}
    )
    try:
        label_id = int(row.get("hero_action_label_id"))
    except (TypeError, ValueError):
        label_id = label_action(int(row.get("hero_action", 0) or 0), dict(request))
    if not 0 <= label_id < len(LABELS):
        raise ValueError("hero action label is out of range")
    one_hot = [1.0 if index == label_id else 0.0 for index in range(len(LABELS))]
    commit = max(0.0, _finite(context.get("hero_commit", 0.0), field="hero_commit"))
    pot_before_response = max(
        1.0,
        _finite(
            context.get("pot_before_response", request.get("pot", 150.0)),
            field="pot_before_response",
        ),
    )
    pot_before_hero = max(1.0, pot_before_response - commit)
    opponent_to_call = max(
        0.0,
        _finite(context.get("opponent_to_call", 0.0), field="opponent_to_call"),
    )
    hero_stack_after = max(
        0.0,
        _finite(context.get("hero_stack_after", 0.0), field="hero_stack_after"),
    )
    return one_hot + [
        min(1.0, commit / INITIAL_CHIPS),
        min(1.0, commit / pot_before_hero / 4.0),
        min(1.0, opponent_to_call / INITIAL_CHIPS),
        min(1.0, hero_stack_after / INITIAL_CHIPS),
    ]


def encode_prepared_row(
    row: Mapping[str, Any], *, response: bool, max_hist: int = 16
) -> dict[str, Any]:
    """Encode a prepared role row without importing the legacy trainer."""
    base = row.get("state_features", row.get("features"))
    if not isinstance(base, Sequence) or isinstance(base, (str, bytes)):
        raise ValueError("row is missing state_features")
    encoded = encode_model_input(
        row, list(base), max_hist=max_hist, response=response
    )
    weight_field = (
        TRAIN_WEIGHT_FIELD if TRAIN_WEIGHT_FIELD in row else EVALUATION_WEIGHT_FIELD
    )
    if weight_field not in row:
        raise ValueError("prepared row is missing its role weight")
    encoded["row_weight"] = _finite(row[weight_field], field=weight_field)
    encoded["row_weight_field"] = weight_field
    encoded["opponent"] = opponent_label(dict(row))

    if response:
        prepared = (
            dict(row)
            if row.get("response_schema") == OPPONENT_RESPONSE_SCHEMA
            else annotate_response_rows([dict(row)], strict=True)[0]
        )
        legal = prepared.get("response_legal_action_mask")
        if not isinstance(legal, Sequence) or len(legal) != len(OPPONENT_ACTION_LABELS):
            raise ValueError("response legal-action mask has the wrong dimension")
        legal_mask = [1 if bool(value) else 0 for value in legal]
        target = int(prepared.get("opponent_action_label_id", -1))
        target_mask = int(prepared.get("response_target_mask", 0) or 0)
        if target_mask != 1 or not 0 <= target < len(legal_mask) or not legal_mask[target]:
            raise ValueError("response target is absent or illegal")
        encoded.update({
            "response_schema": OPPONENT_RESPONSE_SCHEMA,
            "response_target": target,
            "response_target_mask": target_mask,
            "response_legal_action_mask": legal_mask,
            "response_amount_target": _finite(
                prepared.get("response_amount_target", 0.0),
                field="response_amount_target",
            ),
            "response_amount_target_mask": int(
                prepared.get("response_amount_target_mask", 0) or 0
            ),
            "hero_action_schema": HERO_RESPONSE_ACTION_SCHEMA,
            "hero_action_features": _hero_response_action_features(prepared),
            "strategy_context": [],
            "strategy_context_schema": None,
        })
    else:
        raw_strategy = row.get("strategy_context_features")
        if raw_strategy is None:
            strategy = []
            strategy_schema = None
        else:
            if not isinstance(raw_strategy, Sequence) or isinstance(
                raw_strategy, (str, bytes)
            ):
                raise ValueError("strategy context must be a numeric sequence")
            strategy = _bounded_vector(raw_strategy, field="strategy_context")
            if len(strategy) != STRATEGY_CONTEXT_DIM:
                raise ValueError("strategy context has the wrong dimension")
            strategy_schema = str(
                row.get("strategy_context_schema") or STRATEGY_CONTEXT_SCHEMA
            )
            if strategy_schema != STRATEGY_CONTEXT_SCHEMA:
                raise ValueError("unsupported strategy context schema")
        encoded.update({
            "strategy_context": strategy,
            "strategy_context_schema": strategy_schema,
            "strategy_context_available": bool(strategy),
        })
    return encoded


def training_data_metadata() -> dict[str, Any]:
    return {
        "schema": MULTITASK_TRAINING_DATA_SCHEMA,
        "model_development_roles": list(MODEL_DEVELOPMENT_ROLES),
        "model_training_roles": list(MODEL_TRAINING_ROLES),
        "model_calibration_role": MODEL_CALIBRATION_ROLE,
        "frozen_checkpoint_schema": FROZEN_CHECKPOINT_SCHEMA,
        "policy_roles_forbidden": list(POLICY_ROLES),
        "value_weighting": VALUE_WEIGHTING,
        "behavior_weighting": BEHAVIOR_WEIGHTING,
        "train_weight_field": TRAIN_WEIGHT_FIELD,
        "evaluation_weight_field": EVALUATION_WEIGHT_FIELD,
        "model_input": model_input_metadata(base_state_dim=48),
        "opponent_response": response_schema_metadata(),
        "hero_response_action": {
            "schema": HERO_RESPONSE_ACTION_SCHEMA,
            "dim": HERO_RESPONSE_ACTION_DIM,
            "fields": list(HERO_RESPONSE_ACTION_FIELDS),
        },
        "strategy_context": strategy_context_metadata(),
    }
