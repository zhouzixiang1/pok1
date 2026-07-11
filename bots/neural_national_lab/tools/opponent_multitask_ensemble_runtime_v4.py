"""Stdlib-only v4 ensemble with win-first outcome uncertainty scoring."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import opponent_multitask_ensemble_runtime_v3 as v3_ensemble
import opponent_multitask_runtime_v3 as v3_runtime
from opponent_multitask_runtime_v4 import OpponentMultiTaskRuntimeV4
from win_first_policy_v4 import (
    OUTCOME_AGGREGATION_METHOD,
    aggregate_member_probabilities,
    normalize_policy,
    select_candidate,
)


ENSEMBLE_FORMAT = "opponent_multitask_stdlib_ensemble_v4"
BUNDLE_SCHEMA = "opponent_multitask_stdlib_ensemble_export_v2_outcome"
ORIGINAL_CALIBRATION_SCHEMA = "opponent_multitask_v4_ensemble_calibration_v1"
CALIBRATION_PROJECTION_SCHEMA = "opponent_multitask_v4_runtime_calibration_v1"
FORMAL_COLLECTION_PASSES = 160
FORMAL_UNCERTAINTY_STD_WEIGHT = 1.0
CHECKPOINT_SCHEMA = "opponent_multitask_torch_checkpoint_v4"
STRATEGY_CONTEXT_RUNTIME_MODE = "zero_vector_training_aligned_v1"
RUNTIME_MODULE_FILENAMES = (
    "feature_spec.py",
    "decision_context_features.py",
    "hand_context_features.py",
    "history_feature_schema.py",
    "state_feature_schema.py",
    "model_input_schema.py",
    "opponent_profile_schema.py",
    "cross_hand_sequence.py",
    "strategy_context_schema.py",
    "match_outcome_calibration.py",
    "opponent_multitask_runtime_v3.py",
    "opponent_multitask_runtime_v4.py",
    "opponent_multitask_ensemble_runtime_v3.py",
    "opponent_multitask_ensemble_runtime_v4.py",
    "win_first_policy_v4.py",
    "v4_runtime_budget.py",
    "v3_native_policy.py",
    "v4_native_policy.py",
)


def _normalized_calibration_projection(raw: dict[str, Any]) -> dict[str, Any]:
    member_seed = raw.get("member_seed")
    checkpoints = raw.get("member_checkpoint_sha256")
    outcome_hashes = raw.get("outcome_calibration_payload_sha256")
    if (
        not isinstance(member_seed, list)
        or not member_seed
        or any(isinstance(seed, bool) or not isinstance(seed, int) for seed in member_seed)
        or len(set(member_seed)) != len(member_seed)
        or not isinstance(checkpoints, list)
        or len(checkpoints) != len(member_seed)
        or not isinstance(outcome_hashes, list)
        or len(outcome_hashes) != len(member_seed)
    ):
        raise ValueError("v4 calibration member projection is invalid")
    normalized_checkpoints = [
        v3_ensemble._digest(value, field="calibration member checkpoint")
        for value in checkpoints
    ]
    normalized_outcome_hashes = [
        v3_ensemble._digest(value, field="outcome calibration payload")
        for value in outcome_hashes
    ]
    clips = raw.get("clips")
    offsets = raw.get("offsets")
    if (
        not isinstance(clips, dict)
        or set(clips) != set(v3_runtime.VALUE_FIELDS)
        or not isinstance(offsets, dict)
        or set(offsets) != set(v3_runtime.VALUE_FIELDS)
    ):
        raise ValueError("v4 calibration value projection is invalid")
    normalized_clips = {
        field: v3_ensemble._positive(clips[field], field=f"{field} clip")
        for field in v3_runtime.VALUE_FIELDS
    }
    normalized_offsets = {}
    for field in v3_runtime.VALUE_FIELDS:
        values = offsets[field]
        if not isinstance(values, list) or len(values) != len(v3_runtime.LABELS):
            raise ValueError("v4 calibration offsets have the wrong dimension")
        normalized_offsets[field] = [
            v3_ensemble._finite(value, field=f"{field} offset")
            for value in values
        ]
    lower_quantile = v3_ensemble._finite(
        raw.get("lower_quantile"), field="lower_quantile"
    )
    if lower_quantile not in v3_runtime.QUANTILE_LEVELS:
        raise ValueError("v4 calibration lower quantile is unsupported")
    value_uncertainty = v3_ensemble._finite(
        raw.get("uncertainty_std_weight"), field="uncertainty_std_weight"
    )
    outcome_uncertainty = v3_ensemble._finite(
        raw.get("outcome_uncertainty_std_weight"),
        field="outcome_uncertainty_std_weight",
    )
    if min(value_uncertainty, outcome_uncertainty) < 0.0:
        raise ValueError("v4 calibration uncertainty must be nonnegative")
    response_temperature = v3_ensemble._positive(
        raw.get("response_temperature"), field="response_temperature"
    )
    run_id = str(raw.get("run_id") or "").strip()
    opponents = raw.get("model_calibration_opponents")
    if (
        not run_id
        or not isinstance(opponents, list)
        or not opponents
        or any(not isinstance(name, str) or not name.strip() for name in opponents)
        or not isinstance(raw.get("source_collection_complete"), bool)
    ):
        raise ValueError("v4 calibration role projection is invalid")
    if raw.get("outcome_aggregation") != OUTCOME_AGGREGATION_METHOD:
        raise ValueError("v4 outcome aggregation contract changed")
    return {
        "schema": CALIBRATION_PROJECTION_SCHEMA,
        "member_seed": list(member_seed),
        "member_checkpoint_sha256": normalized_checkpoints,
        "lower_quantile": lower_quantile,
        "uncertainty_std_weight": value_uncertainty,
        "clips": normalized_clips,
        "offsets": normalized_offsets,
        "response_temperature": response_temperature,
        "outcome_aggregation": OUTCOME_AGGREGATION_METHOD,
        "outcome_uncertainty_std_weight": outcome_uncertainty,
        "outcome_calibration_payload_sha256": normalized_outcome_hashes,
        "run_id": run_id,
        "role_manifest_sha256": v3_ensemble._digest(
            raw.get("role_manifest_sha256"), field="role_manifest_sha256"
        ),
        "model_calibration_artifact_sha256": v3_ensemble._digest(
            raw.get("model_calibration_artifact_sha256"),
            field="model_calibration_artifact_sha256",
        ),
        "model_calibration_opponents": list(opponents),
        "source_collection_complete": raw["source_collection_complete"],
    }


def calibration_projection_from_artifact(
    artifact: Any,
) -> dict[str, Any]:
    if not isinstance(artifact, dict):
        raise ValueError("v4 original calibration artifact is missing")
    unsigned = dict(artifact)
    observed_sha256 = v3_ensemble._digest(
        unsigned.pop("payload_sha256", None),
        field="original calibration payload_sha256",
    )
    if v3_ensemble._canonical_sha256(unsigned) != observed_sha256:
        raise ValueError("v4 original calibration payload changed")
    ensemble = artifact.get("ensemble")
    value_lower = artifact.get("value_lower")
    response = artifact.get("response_temperature")
    fields = value_lower.get("fields") if isinstance(value_lower, dict) else None
    members = ensemble.get("members") if isinstance(ensemble, dict) else None
    if (
        artifact.get("schema") != ORIGINAL_CALIBRATION_SCHEMA
        or artifact.get("calibration_role") != "model_calibration"
        or artifact.get("policy_evidence_used") is not False
        or artifact.get("deployment_policy_value") is not False
        or artifact.get("strength_evidence") is not False
        or not isinstance(ensemble, dict)
        or not isinstance(value_lower, dict)
        or not isinstance(fields, dict)
        or not isinstance(response, dict)
        or not isinstance(members, list)
    ):
        raise ValueError("v4 original calibration artifact is invalid")
    return _normalized_calibration_projection({
        "member_seed": [member.get("seed") for member in members],
        "member_checkpoint_sha256": [
            member.get("checkpoint_sha256") for member in members
        ],
        "lower_quantile": ensemble.get("lower_quantile"),
        "uncertainty_std_weight": ensemble.get("uncertainty_std_weight"),
        "clips": value_lower.get("target_clips"),
        "offsets": {
            field: (fields.get(field) or {}).get("offsets")
            for field in v3_runtime.VALUE_FIELDS
        },
        "response_temperature": response.get("temperature"),
        "outcome_aggregation": ensemble.get("outcome_aggregation"),
        "outcome_uncertainty_std_weight": ensemble.get(
            "outcome_uncertainty_std_weight"
        ),
        "outcome_calibration_payload_sha256": ensemble.get(
            "outcome_calibration_payload_sha256"
        ),
        "run_id": artifact.get("run_id"),
        "role_manifest_sha256": artifact.get("role_manifest_sha256"),
        "model_calibration_artifact_sha256": artifact.get(
            "calibration_artifact_sha256"
        ),
        "model_calibration_opponents": artifact.get("opponents"),
        "source_collection_complete": artifact.get(
            "source_collection_complete"
        ),
    })


def calibration_projection_from_bundle(
    calibration: dict[str, Any],
) -> dict[str, Any]:
    return _normalized_calibration_projection(calibration)


def calibration_projection_sha256(projection: dict[str, Any]) -> str:
    return v3_ensemble._canonical_sha256(projection)


def validate_calibration_binding(
    calibration: dict[str, Any], source: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(calibration, dict) or not isinstance(source, dict):
        raise ValueError("v4 calibration binding is missing")
    artifact = calibration.get("original_calibration_artifact")
    from_artifact = calibration_projection_from_artifact(artifact)
    from_bundle = calibration_projection_from_bundle(calibration)
    if from_artifact != from_bundle:
        raise ValueError("v4 runtime calibration differs from original artifact")
    projection_sha256 = calibration_projection_sha256(from_artifact)
    observed_projection = v3_ensemble._digest(
        calibration.get("calibration_projection_sha256"),
        field="calibration_projection_sha256",
    )
    original_payload_sha256 = v3_ensemble._digest(
        artifact.get("payload_sha256") if isinstance(artifact, dict) else None,
        field="original calibration payload_sha256",
    )
    original_file_sha256 = v3_ensemble._digest(
        calibration.get("original_calibration_file_sha256"),
        field="original calibration file_sha256",
    )
    if (
        observed_projection != projection_sha256
        or calibration.get("payload_sha256") != original_payload_sha256
        or source.get("calibration_projection_sha256") != projection_sha256
        or source.get("calibration_payload_sha256") != original_payload_sha256
        or source.get("calibration_file_sha256") != original_file_sha256
    ):
        raise ValueError("v4 calibration source binding changed")
    return from_artifact


def _base_member_payload(
    payload: dict[str, Any], runtime: OpponentMultiTaskRuntimeV4
) -> dict[str, Any]:
    outcome_keys = set(runtime.outcome_weights)
    weights = payload.get("weights")
    if not isinstance(weights, dict) or not outcome_keys < set(weights):
        raise ValueError("v4 ensemble member outcome weights are missing")
    result = dict(payload)
    result.update({
        "format": v3_runtime.RUNTIME_FORMAT,
        "model_metadata": dict(runtime.base.metadata),
        "weights": {
            name: value for name, value in weights.items()
            if name not in outcome_keys
        },
    })
    result.pop("outcome_calibration", None)
    return result


def _chip_policy(policy: dict[str, Any] | None) -> dict[str, Any] | None:
    if policy is None:
        return None
    return {
        "margin": policy["chip_margin"],
        "hand_weight": policy["hand_weight"],
        "tail_weight": policy["tail_weight"],
        "match_weight": policy["match_weight"],
        "response_weight": policy["response_weight"],
        "use_lower": True,
        "min_hand_lcb": policy["min_hand_lcb"],
    }


class OpponentMultiTaskEnsembleRuntimeV4:
    def __init__(self, payload: dict[str, Any]) -> None:
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != BUNDLE_SCHEMA
            or payload.get("format") != ENSEMBLE_FORMAT
        ):
            raise ValueError("unsupported v4 stdlib ensemble format")
        member_payloads = payload.get("members")
        member_hashes = payload.get("member_payload_sha256")
        if (
            not isinstance(member_payloads, list)
            or not member_payloads
            or not isinstance(member_hashes, list)
            or len(member_hashes) != len(member_payloads)
        ):
            raise ValueError("v4 ensemble members are missing")
        self.members = []
        for index, (member, digest) in enumerate(zip(member_payloads, member_hashes)):
            expected = v3_ensemble._digest(
                digest, field=f"member_payload_sha256[{index}]"
            )
            if v3_ensemble._canonical_sha256(member) != expected:
                raise ValueError("v4 ensemble member payload changed")
            runtime = OpponentMultiTaskRuntimeV4(member)
            if runtime.outcome_calibration is None:
                raise ValueError("v4 ensemble member outcome is uncalibrated")
            self.members.append(runtime)
        calibration = payload.get("calibration")
        if not isinstance(calibration, dict):
            raise ValueError("v4 ensemble calibration is missing")
        source = payload.get("source")
        if not isinstance(source, dict):
            raise ValueError("v4 ensemble source is missing")
        self.calibration_projection = validate_calibration_binding(
            calibration, source
        )
        if calibration.get("outcome_aggregation") != OUTCOME_AGGREGATION_METHOD:
            raise ValueError("v4 outcome aggregation contract changed")
        member_seeds = calibration.get("member_seed")
        if (
            not isinstance(member_seeds, list)
            or len(member_seeds) != len(self.members)
            or any(
                isinstance(seed, bool) or not isinstance(seed, int)
                for seed in member_seeds
            )
            or len(set(member_seeds)) != len(member_seeds)
        ):
            raise ValueError("v4 ensemble member seed binding changed")
        self.member_seeds = list(member_seeds)
        for member_payload, member, seed in zip(
            member_payloads, self.members, self.member_seeds, strict=True
        ):
            member_source = member_payload.get("source")
            if (
                not isinstance(member_source, dict)
                or member_source.get("checkpoint_schema") != CHECKPOINT_SCHEMA
                or member_source.get("role_manifest_sha256")
                != member.outcome_calibration.get("role_manifest_sha256")
                or member_source.get("source_collection_complete")
                is not member.outcome_calibration.get(
                    "source_collection_complete"
                )
                or member.outcome_calibration.get("calibration_role")
                != "model_calibration"
                or member.outcome_calibration.get("policy_evidence_used")
                is not False
                or member.outcome_calibration.get("member_seed") != seed
            ):
                raise ValueError("v4 outcome calibration is not model-role-only")
            training = member_source.get("training_artifact_sha256")
            code = member_source.get("code_artifacts")
            if (
                not isinstance(training, dict)
                or set(training) != {"train", "early_stop"}
                or not isinstance(code, dict)
                or not code
            ):
                raise ValueError("v4 member checkpoint provenance is incomplete")
            for role, digest in training.items():
                v3_ensemble._digest(
                    digest, field=f"{role} training artifact sha256"
                )
            for name, contract in code.items():
                if (
                    not isinstance(contract, dict)
                    or set(contract) != {"bytes", "sha256"}
                    or isinstance(contract.get("bytes"), bool)
                    or not isinstance(contract.get("bytes"), int)
                    or contract["bytes"] < 1
                ):
                    raise ValueError("v4 member code provenance is invalid")
                v3_ensemble._digest(
                    contract.get("sha256"),
                    field=f"{name} code artifact sha256",
                )
        self.outcome_uncertainty_std_weight = v3_ensemble._finite(
            calibration.get("outcome_uncertainty_std_weight"),
            field="outcome_uncertainty_std_weight",
        )
        if self.outcome_uncertainty_std_weight < 0.0:
            raise ValueError("outcome uncertainty std weight must be nonnegative")
        outcome_hashes = calibration.get("outcome_calibration_payload_sha256")
        observed_hashes = [
            member.outcome_calibration["payload_sha256"]
            for member in self.members
        ]
        if outcome_hashes != observed_hashes:
            raise ValueError("v4 outcome calibration member binding changed")
        signatures = {
            (
                member.outcome_calibration.get("run_id"),
                member.outcome_calibration["role_manifest_sha256"],
                member.outcome_calibration[
                    "model_calibration_artifact_sha256"
                ],
                tuple(member.outcome_calibration["model_calibration_opponents"]),
                member.outcome_calibration["source_collection_complete"],
            )
            for member in self.members
        }
        if len(signatures) != 1:
            raise ValueError("v4 members use different outcome calibration roles")
        outcome_role = next(iter(signatures))
        expected_role = (
            calibration.get("run_id"),
            calibration.get("role_manifest_sha256"),
            calibration.get("model_calibration_artifact_sha256"),
            tuple(calibration.get("model_calibration_opponents") or ()),
            calibration.get("source_collection_complete"),
        )
        if outcome_role != expected_role:
            raise ValueError("v4 value and outcome calibration roles differ")

        self.policy = normalize_policy(payload.get("selected_policy"))
        if (
            source.get("deployment_policy_value") is not False
            or source.get("strength_evidence") is not False
            or payload.get("deployment_policy_value") is not False
            or payload.get("strength_evidence") is not False
        ):
            raise ValueError("v4 ensemble carries an invalid evidence claim")
        policy_passed = source.get("policy_selection_passed") is True
        if policy_passed != (self.policy is not None):
            raise ValueError("v4 ensemble selected-policy status is inconsistent")
        selected_policy_sha256 = source.get("selected_policy_sha256")
        if self.policy is None:
            if selected_policy_sha256 is not None:
                raise ValueError("v4 ensemble has a policy hash without a policy")
        elif v3_ensemble._canonical_sha256(self.policy) != v3_ensemble._digest(
            selected_policy_sha256, field="selected_policy_sha256"
        ):
            raise ValueError("v4 ensemble selected policy changed")
        candidate_snapshot = source.get("candidate_snapshot")
        if (
            source.get("run_id") != calibration.get("run_id")
            or source.get("role_manifest_sha256")
            != calibration.get("role_manifest_sha256")
            or source.get("strategy_context_runtime_mode")
            != STRATEGY_CONTEXT_RUNTIME_MODE
            or not isinstance(candidate_snapshot, dict)
            or set(candidate_snapshot) != {"name", "sha256"}
            or not str(candidate_snapshot.get("name") or "").strip()
        ):
            raise ValueError("v4 bundle source and calibration role differ")
        v3_ensemble._digest(
            candidate_snapshot.get("sha256"),
            field="candidate snapshot sha256",
        )
        self.strategy_context_runtime_mode = STRATEGY_CONTEXT_RUNTIME_MODE
        if policy_passed and (
            source.get("source_collection_complete") is not True
            or calibration.get("source_collection_complete") is not True
            or len(self.member_seeds) < 3
            or source.get("source_completed_passes") != FORMAL_COLLECTION_PASSES
            or source.get("source_requested_passes") != FORMAL_COLLECTION_PASSES
            or self.calibration_projection["uncertainty_std_weight"]
            != FORMAL_UNCERTAINTY_STD_WEIGHT
            or self.calibration_projection["outcome_uncertainty_std_weight"]
            != FORMAL_UNCERTAINTY_STD_WEIGHT
        ):
            raise ValueError(
                "v4 selected policy requires the complete formal calibration boundary"
            )

        base_members = [
            _base_member_payload(member, runtime)
            for member, runtime in zip(member_payloads, self.members)
        ]
        chip_policy = _chip_policy(self.policy)
        base_source = {
            "selected_policy_sha256": (
                v3_ensemble._canonical_sha256(chip_policy)
                if chip_policy is not None else None
            ),
            "policy_selection_passed": chip_policy is not None,
            "deployment_policy_value": False,
            "strength_evidence": False,
        }
        base_payload = {
            "format": v3_ensemble.ENSEMBLE_FORMAT,
            "members": base_members,
            "member_payload_sha256": [
                v3_ensemble._canonical_sha256(member) for member in base_members
            ],
            "calibration": {
                key: calibration[key]
                for key in (
                    "payload_sha256",
                    "member_checkpoint_sha256",
                    "lower_quantile",
                    "uncertainty_std_weight",
                    "clips",
                    "offsets",
                    "response_temperature",
                )
            },
            "selected_policy": chip_policy,
            "source": base_source,
            "deployment_policy_value": False,
            "strength_evidence": False,
        }
        self.value_response = v3_ensemble.OpponentMultiTaskEnsembleRuntimeV3(
            base_payload
        )

    @classmethod
    def load(
        cls, path: str | Path
    ) -> "OpponentMultiTaskEnsembleRuntimeV4 | None":
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            return cls(payload)
        except Exception:
            return None

    def predict_values(self, **inputs: Any) -> dict[str, dict[str, list[float]]]:
        return self.value_response.predict_values(**inputs)

    def predict_response(self, **inputs: Any) -> dict[str, Any]:
        return self.value_response.predict_response(**inputs)

    response_signal = staticmethod(v3_ensemble.OpponentMultiTaskEnsembleRuntimeV3.response_signal)

    def predict_match_outcomes(self, **inputs: Any) -> dict[str, Any]:
        outputs = [
            member.predict_match_outcome(**inputs) for member in self.members
        ]
        if any(output.get("calibrated") is not True for output in outputs):
            raise ValueError("v4 ensemble outcome prediction is uncalibrated")
        return aggregate_member_probabilities(
            [list(output["probabilities"]) for output in outputs],
            uncertainty_std_weight=self.outcome_uncertainty_std_weight,
        )

    def select_candidate(
        self,
        values: dict[str, dict[str, list[float]]],
        outcomes: dict[str, Any],
        candidates: list[dict[str, Any]],
        *,
        rule_label_id: int,
    ) -> dict[str, Any] | None:
        return select_candidate(
            self.policy,
            outcomes,
            values,
            candidates,
            rule_label_id=rule_label_id,
        )
