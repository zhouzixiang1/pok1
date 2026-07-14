"""Stdlib-only calibrated ensemble runtime for opponent multi-task v3."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from opponent_multitask_runtime_v3 import (
    LABELS,
    QUANTILE_LEVELS,
    RESPONSE_LABELS,
    VALUE_FIELDS,
    OpponentMultiTaskRuntimeV3,
)


ENSEMBLE_FORMAT = "opponent_multitask_stdlib_ensemble_v3"


def _finite(value: Any, *, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be finite") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def _positive(value: Any, *, field: str) -> float:
    number = _finite(value, field=field)
    if number <= 0.0:
        raise ValueError(f"{field} must be positive")
    return number


def _digest(value: Any, *, field: str) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return result


def _canonical_sha256(value: Any) -> str:
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _softmax(values: list[float], legal: list[int]) -> list[float]:
    allowed = [value for value, valid in zip(values, legal) if valid]
    peak = max(allowed)
    exponents = [
        math.exp(max(-60.0, value - peak)) if legal[index] else 0.0
        for index, value in enumerate(values)
    ]
    total = sum(exponents)
    return [value / total for value in exponents]


def _population_std(values: list[float], mean: float) -> float:
    return math.sqrt(
        sum((value - mean) ** 2 for value in values) / len(values)
    )


class OpponentMultiTaskEnsembleRuntimeV3:
    def __init__(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict) or payload.get("format") != ENSEMBLE_FORMAT:
            raise ValueError("unsupported v3 stdlib ensemble format")
        member_payloads = payload.get("members")
        member_hashes = payload.get("member_payload_sha256")
        if (
            not isinstance(member_payloads, list)
            or not member_payloads
            or not isinstance(member_hashes, list)
            or len(member_hashes) != len(member_payloads)
        ):
            raise ValueError("v3 ensemble members are missing")
        self.members = []
        for index, (member, digest) in enumerate(
            zip(member_payloads, member_hashes)
        ):
            expected = _digest(digest, field=f"member_payload_sha256[{index}]")
            if _canonical_sha256(member) != expected:
                raise ValueError("v3 ensemble member payload changed")
            self.members.append(OpponentMultiTaskRuntimeV3(member))
        first_metadata = self.members[0].metadata
        first_hidden = self.members[0].hidden
        if any(
            member.metadata != first_metadata or member.hidden != first_hidden
            for member in self.members[1:]
        ):
            raise ValueError("v3 ensemble members use different architectures")
        checkpoints = [
            _digest(
                member_payload.get("source", {}).get("checkpoint_sha256"),
                field="member checkpoint_sha256",
            )
            for member_payload in member_payloads
        ]
        if len(set(checkpoints)) != len(checkpoints):
            raise ValueError("v3 ensemble reuses a checkpoint")

        calibration = payload.get("calibration")
        if not isinstance(calibration, dict):
            raise ValueError("v3 ensemble calibration is missing")
        self.calibration_payload_sha256 = _digest(
            calibration.get("payload_sha256"),
            field="calibration.payload_sha256",
        )
        self.lower_quantile = _finite(
            calibration.get("lower_quantile"), field="lower_quantile"
        )
        if self.lower_quantile not in QUANTILE_LEVELS:
            raise ValueError("v3 ensemble lower quantile is unsupported")
        self.lower_index = QUANTILE_LEVELS.index(self.lower_quantile)
        self.uncertainty_std_weight = _finite(
            calibration.get("uncertainty_std_weight"),
            field="uncertainty_std_weight",
        )
        if self.uncertainty_std_weight < 0.0:
            raise ValueError("uncertainty_std_weight must be nonnegative")
        self.response_temperature = _positive(
            calibration.get("response_temperature"),
            field="response_temperature",
        )
        raw_clips = calibration.get("clips")
        raw_offsets = calibration.get("offsets")
        if (
            not isinstance(raw_clips, dict)
            or set(raw_clips) != set(VALUE_FIELDS)
            or not isinstance(raw_offsets, dict)
            or set(raw_offsets) != set(VALUE_FIELDS)
        ):
            raise ValueError("v3 ensemble value calibration is malformed")
        self.clips = {
            field: _positive(raw_clips[field], field=f"{field} clip")
            for field in VALUE_FIELDS
        }
        self.offsets = {}
        for field in VALUE_FIELDS:
            values = raw_offsets[field]
            if not isinstance(values, list) or len(values) != len(LABELS):
                raise ValueError(f"{field} offsets have the wrong dimension")
            self.offsets[field] = [
                _finite(value, field=f"{field} offset") for value in values
            ]
        member_checkpoints = calibration.get("member_checkpoint_sha256")
        if member_checkpoints != checkpoints:
            raise ValueError("v3 ensemble calibration member binding changed")
        self.policy = self._policy(payload.get("selected_policy"))
        source = payload.get("source")
        if (
            not isinstance(source, dict)
            or source.get("deployment_policy_value") is not False
            or source.get("strength_evidence") is not False
            or payload.get("deployment_policy_value") is not False
            or payload.get("strength_evidence") is not False
        ):
            raise ValueError("v3 ensemble carries an invalid evidence claim")
        policy_passed = source.get("policy_selection_passed") is True
        selected_policy_sha256 = source.get("selected_policy_sha256")
        if policy_passed != (self.policy is not None):
            raise ValueError("v3 ensemble selected-policy status is inconsistent")
        if self.policy is None:
            if selected_policy_sha256 is not None:
                raise ValueError("v3 ensemble has a policy hash without a policy")
        elif _canonical_sha256(self.policy) != _digest(
            selected_policy_sha256, field="selected_policy_sha256"
        ):
            raise ValueError("v3 ensemble selected policy changed")

    @classmethod
    def load(
        cls, path: str | Path
    ) -> "OpponentMultiTaskEnsembleRuntimeV3 | None":
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            return cls(payload)
        except Exception:
            return None

    @staticmethod
    def _policy(raw: Any) -> dict[str, Any] | None:
        if raw is None:
            return None
        if not isinstance(raw, dict) or raw.get("use_lower") is not True:
            raise ValueError("selected policy must use calibrated lower values")
        result = {
            "margin": _finite(raw.get("margin"), field="policy.margin"),
            "hand_weight": _finite(
                raw.get("hand_weight"), field="policy.hand_weight"
            ),
            "tail_weight": _finite(
                raw.get("tail_weight", 0.0), field="policy.tail_weight"
            ),
            "match_weight": _finite(
                raw.get("match_weight"), field="policy.match_weight"
            ),
            "response_weight": _finite(
                raw.get("response_weight"), field="policy.response_weight"
            ),
            "use_lower": True,
        }
        if min(
            result["hand_weight"],
            result["tail_weight"],
            result["match_weight"],
            result["response_weight"],
        ) < 0.0:
            raise ValueError("selected policy weights must be nonnegative")
        if abs(
            result["hand_weight"]
            + result["tail_weight"]
            + result["match_weight"]
            - 1.0
        ) > 1.0e-8:
            raise ValueError("selected value-policy weights must sum to one")
        if "min_hand_lcb" in raw:
            result["min_hand_lcb"] = _finite(
                raw["min_hand_lcb"], field="policy.min_hand_lcb"
            )
        if set(raw) != set(result):
            raise ValueError("selected policy has unknown or missing fields")
        return result

    def predict_values(self, **inputs: Any) -> dict[str, dict[str, list[float]]]:
        outputs = [member.predict_value(**inputs) for member in self.members]
        result = {}
        for field in VALUE_FIELDS:
            means = []
            lowers = []
            disagreements = []
            for action in range(len(LABELS)):
                member_means = [
                    output[field]["mean"][action] * self.clips[field]
                    for output in outputs
                ]
                member_lower = [
                    output[field]["quantiles"][action][self.lower_index]
                    * self.clips[field]
                    for output in outputs
                ]
                mean = sum(member_means) / len(member_means)
                disagreement = _population_std(member_means, mean)
                lower = (
                    sum(member_lower) / len(member_lower)
                    - self.uncertainty_std_weight * disagreement
                    + self.offsets[field][action]
                )
                means.append(mean)
                lowers.append(lower)
                disagreements.append(disagreement)
            result[field] = {
                "mean": means,
                "lower": lowers,
                "member_mean_std": disagreements,
            }
        return result

    def predict_response(
        self, *, legal_action_mask: list[float], **inputs: Any
    ) -> dict[str, Any]:
        if (
            not isinstance(legal_action_mask, list)
            or len(legal_action_mask) != len(RESPONSE_LABELS)
        ):
            raise ValueError("response legal mask has the wrong dimension")
        legal = []
        for value in legal_action_mask:
            number = _finite(value, field="response legal mask")
            if number not in (0.0, 1.0):
                raise ValueError("response legal mask must be binary")
            legal.append(int(number))
        if not any(legal):
            raise ValueError("response legal mask must be nonempty")
        outputs = [
            member.predict_response(
                **inputs, legal_action_mask=legal_action_mask
            )
            for member in self.members
        ]
        logits = [
            sum(output["logits"][index] for output in outputs) / len(outputs)
            for index in range(len(RESPONSE_LABELS))
        ]
        scaled = [value / self.response_temperature for value in logits]
        probabilities = _softmax(scaled, legal)
        sizes = [
            sum(output["size"][index] for output in outputs) / len(outputs)
            for index in range(2)
        ]
        entropy = -sum(
            probability * math.log(max(probability, 1.0e-12))
            for probability, valid in zip(probabilities, legal)
            if valid
        )
        legal_count = sum(legal)
        return {
            "logits": logits,
            "probabilities": {
                label: probabilities[index]
                for index, label in enumerate(RESPONSE_LABELS)
            },
            "normalized_entropy": (
                entropy / math.log(legal_count) if legal_count > 1 else 0.0
            ),
            "aggressive_increment_pot_log": sizes[0],
            "aggressive_stack_fraction": sizes[1],
        }

    @staticmethod
    def response_signal(
        response: dict[str, Any],
        *,
        action: int,
        pot: float,
        hero_stage_bet: float,
        hero_stack: float,
        opponent_stack: float,
    ) -> float:
        pot = max(1.0, _finite(pot, field="pot"))
        hero_stage_bet = max(
            0.0, _finite(hero_stage_bet, field="hero_stage_bet")
        )
        hero_stack = max(0.0, _finite(hero_stack, field="hero_stack"))
        opponent_stack = max(
            0.0, _finite(opponent_stack, field="opponent_stack")
        )
        probabilities = response.get("probabilities")
        if not isinstance(probabilities, dict):
            raise ValueError("response probabilities are missing")
        values = {
            label: _finite(probabilities.get(label), field=f"P({label})")
            for label in RESPONSE_LABELS
        }
        if any(value < 0.0 for value in values.values()) or abs(
            sum(values.values()) - 1.0
        ) > 1.0e-6:
            raise ValueError("response probabilities are invalid")
        committed = (
            hero_stack
            if int(action) == -2
            else max(0.0, float(action) - hero_stage_bet)
        )
        fold_gain = values["fold"] * pot
        aggression = values["raise"] + values["allin"]
        predicted_raise = _finite(
            response.get("aggressive_stack_fraction"),
            field="aggressive_stack_fraction",
        ) * opponent_stack
        aggression_risk = aggression * min(
            hero_stack, max(pot, committed, predicted_raise)
        )
        entropy_penalty = 0.25 * _finite(
            response.get("normalized_entropy"), field="normalized_entropy"
        ) * pot
        return fold_gain - aggression_risk - entropy_penalty

    def score_candidate(
        self,
        values: dict[str, dict[str, list[float]]],
        *,
        label_id: int,
        response_signal: float = 0.0,
    ) -> dict[str, float] | None:
        if self.policy is None:
            return None
        label_id = int(label_id)
        if not 0 <= label_id < len(LABELS):
            raise ValueError("candidate label_id is out of range")
        hand = _finite(
            values["delta_vs_rule"]["lower"][label_id], field="hand lower"
        )
        tail = _finite(
            values["tail_delta_vs_rule"]["lower"][label_id], field="tail lower"
        )
        match = _finite(
            values["match_delta_vs_rule"]["lower"][label_id], field="match lower"
        )
        if hand < self.policy.get("min_hand_lcb", -math.inf):
            return None
        response_signal = _finite(response_signal, field="response_signal")
        score = (
            self.policy["hand_weight"] * hand
            + self.policy["tail_weight"] * tail
            + self.policy["match_weight"] * match
            + self.policy["response_weight"] * response_signal
        )
        return {"score": score, "hand": hand, "tail": tail, "match": match}

    def select_candidate(
        self,
        values: dict[str, dict[str, list[float]]],
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if self.policy is None:
            return None
        best = None
        for candidate in candidates:
            scored = self.score_candidate(
                values,
                label_id=int(candidate.get("label_id", -1)),
                response_signal=candidate.get("response_signal", 0.0),
            )
            if scored is None:
                continue
            if best is None or scored["score"] > best[0]:
                best = (scored["score"], dict(candidate), scored)
        if best is None or best[0] <= self.policy["margin"]:
            return None
        return {**best[1], "prediction": best[2]}
