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
        if not isinstance(payload, dict) or payload.get("format") != ENSEMBLE_FORMAT:
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
        if calibration.get("outcome_aggregation") != OUTCOME_AGGREGATION_METHOD:
            raise ValueError("v4 outcome aggregation contract changed")
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

        self.policy = normalize_policy(payload.get("selected_policy"))
        source = payload.get("source")
        if (
            not isinstance(source, dict)
            or source.get("deployment_policy_value") is not False
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
