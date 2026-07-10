"""Stdlib-only uncertainty aggregation for opponent multi-task models."""
from __future__ import annotations

import math
from pathlib import Path
import statistics
from typing import Any

from opp_multitask_runtime import OpponentMultiTaskRuntime


class OpponentMultiTaskEnsemble:
    def __init__(
        self,
        members: list[OpponentMultiTaskRuntime],
        *,
        std_multiplier: float = 1.0,
    ) -> None:
        if not members:
            raise ValueError("opponent ensemble requires at least one model")
        self.members = members
        self.std_multiplier = max(0.0, float(std_multiplier))
        self.labels = list(members[0].labels)
        self.response_labels = list(members[0].response_labels)
        self.value_fields = list(members[0].value_fields)
        self.max_hist = max(member.max_hist for member in members)
        self.max_cross_hands = max(
            int(getattr(member, "max_cross_hands", 32)) for member in members
        )
        for member in members[1:]:
            if list(member.labels) != self.labels:
                raise ValueError("ensemble action labels differ")
            if list(member.response_labels) != self.response_labels:
                raise ValueError("ensemble response labels differ")
            if list(member.value_fields) != self.value_fields:
                raise ValueError("ensemble value fields differ")

    @classmethod
    def load(
        cls,
        paths: list[str | Path],
        *,
        std_multiplier: float = 1.0,
    ) -> "OpponentMultiTaskEnsemble | None":
        members = []
        for path in paths:
            member = OpponentMultiTaskRuntime.load(path)
            if member is None:
                return None
            members.append(member)
        try:
            return cls(members, std_multiplier=std_multiplier)
        except Exception:
            return None

    def predict_values(
        self,
        state: list[float],
        profile: list[float],
        history: list[list[float]],
        cross_hand: list[float],
        rule_label_id: int,
        cross_sequence: list[list[float]] | None = None,
    ) -> dict[str, dict[str, list[float]]]:
        predictions = [
            member.predict_values(
                state, profile, history, cross_hand, rule_label_id, cross_sequence
            )
            for member in self.members
        ]
        if any(not prediction for prediction in predictions):
            return {}
        result = {}
        rule_label_id = max(0, min(len(self.labels) - 1, int(rule_label_id)))
        for field in self.value_fields:
            means = [prediction[field]["mean"] for prediction in predictions]
            lowers = [prediction[field]["lower"] for prediction in predictions]
            aggregate_mean = []
            aggregate_std = []
            aggregate_member_lower = []
            aggregate_lcb = []
            for action_id in range(len(self.labels)):
                action_means = [values[action_id] for values in means]
                action_lowers = [values[action_id] for values in lowers]
                mean = statistics.fmean(action_means)
                std = statistics.pstdev(action_means)
                member_lower = min(action_lowers)
                lcb = min(member_lower, mean - self.std_multiplier * std)
                if action_id == rule_label_id:
                    mean = std = member_lower = lcb = 0.0
                aggregate_mean.append(mean)
                aggregate_std.append(std)
                aggregate_member_lower.append(member_lower)
                aggregate_lcb.append(lcb)
            result[field] = {
                "mean": aggregate_mean,
                "std": aggregate_std,
                "member_lower": aggregate_member_lower,
                "lower": aggregate_lcb,
            }
        return result

    def predict_response(
        self,
        state: list[float],
        profile: list[float],
        history: list[list[float]],
        cross_hand: list[float],
        hero_action: list[float],
        cross_sequence: list[list[float]] | None = None,
    ) -> dict[str, Any]:
        predictions = [
            member.predict_response(
                state, profile, history, cross_hand, hero_action, cross_sequence
            )
            for member in self.members
        ]
        if any(not prediction for prediction in predictions):
            return {}
        probabilities = {}
        probability_std = {}
        for label in self.response_labels:
            values = [prediction["probabilities"][label] for prediction in predictions]
            probabilities[label] = statistics.fmean(values)
            probability_std[label] = statistics.pstdev(values)
        entropy = -sum(
            probability * math.log(max(probability, 1e-12))
            for probability in probabilities.values()
        )
        max_entropy = math.log(max(1, len(self.response_labels)))
        raise_ratios = [prediction["raise_pot_ratio"] for prediction in predictions]
        return {
            "probabilities": probabilities,
            "probability_std": probability_std,
            "normalized_entropy": entropy / max_entropy if max_entropy else 0.0,
            "raise_pot_ratio": statistics.fmean(raise_ratios),
            "raise_pot_ratio_std": statistics.pstdev(raise_ratios),
            "members": len(predictions),
        }
