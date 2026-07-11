"""Opponent-aware v4 network with an explicit 70-hand outcome head."""
from __future__ import annotations

from typing import Any

import torch
import torch.nn as nn
import torch.nn.functional as F

from feature_spec import LABELS
from match_outcome_schema import (
    MATCH_OUTCOME_ESTIMAND,
    MATCH_OUTCOME_SCHEMA,
    NATIONAL_MATCH_HANDS,
    POSITIVE_OUTCOME_RULE,
)
from opponent_multitask_model_v3 import (
    MODEL_FORMAT as PARENT_MODEL_FORMAT,
    MODEL_SCALES,
    NUM_ACTIONS,
    QUANTILE_LEVELS,
    VALUE_FIELDS,
    OpponentAwareMultiTaskNetV3,
)


MODEL_FORMAT = "opponent_multitask_distributional_outcome_v4"
OUTCOME_HEAD_SCHEMA = "per_action_70_hand_positive_logit_v1"


class OpponentAwareMultiTaskNetV4(OpponentAwareMultiTaskNetV3):
    """Add absolute match-win logits without changing any v3 head semantics."""

    def __init__(
        self,
        *,
        scale: str = "medium",
        cross_encoder: str = "deep_set",
        moe_experts: int = 4,
        dropout: float = 0.10,
    ) -> None:
        super().__init__(
            scale=scale,
            cross_encoder=cross_encoder,
            moe_experts=moe_experts,
            dropout=dropout,
        )
        self.match_outcome_head = nn.Sequential(
            nn.Linear(self.config["latent"], self.config["head_hidden"]),
            nn.ReLU(),
            nn.Linear(self.config["head_hidden"], NUM_ACTIONS),
        )

    def _value_latent(
        self,
        *,
        state: torch.Tensor,
        profile: torch.Tensor,
        history: torch.Tensor,
        history_lengths: torch.Tensor,
        cross_sequence: torch.Tensor,
        cross_lengths: torch.Tensor,
        rule_action: torch.Tensor,
        strategy_context: torch.Tensor,
    ) -> torch.Tensor:
        common, opponent, interaction = self._common_context(
            state,
            profile,
            history,
            history_lengths,
            cross_sequence,
            cross_lengths,
            response=False,
        )
        strategy = self.strategy_encoder(strategy_context)
        return self.value_fusion(torch.cat([
            common, opponent, interaction, rule_action, strategy
        ], dim=1))

    def _distributional_values(
        self, latent: torch.Tensor
    ) -> dict[str, dict[str, torch.Tensor]]:
        result = {}
        for field, head in self.value_heads.items():
            raw = head(latent)
            mean = raw[:, :NUM_ACTIONS]
            quantile_raw = raw[:, NUM_ACTIONS:].reshape(
                raw.size(0), NUM_ACTIONS, len(QUANTILE_LEVELS)
            )
            first = quantile_raw[:, :, :1]
            increments = F.softplus(quantile_raw[:, :, 1:])
            quantiles = torch.cat([
                first,
                first + torch.cumsum(increments, dim=2),
            ], dim=2)
            result[field] = {"mean": mean, "quantiles": quantiles}
        return result

    def forward_joint_value(
        self,
        *,
        state: torch.Tensor,
        profile: torch.Tensor,
        history: torch.Tensor,
        history_lengths: torch.Tensor,
        cross_sequence: torch.Tensor,
        cross_lengths: torch.Tensor,
        rule_action: torch.Tensor,
        strategy_context: torch.Tensor,
    ) -> dict[str, Any]:
        latent = self._value_latent(
            state=state,
            profile=profile,
            history=history,
            history_lengths=history_lengths,
            cross_sequence=cross_sequence,
            cross_lengths=cross_lengths,
            rule_action=rule_action,
            strategy_context=strategy_context,
        )
        return {
            "values": self._distributional_values(latent),
            "match_positive_logits": self.match_outcome_head(latent),
        }

    def forward_value(self, **inputs: torch.Tensor):
        """Retain the v3 value API for calibration and compatibility tools."""
        return self.forward_joint_value(**inputs)["values"]

    def forward_match_outcome(self, **inputs: torch.Tensor) -> torch.Tensor:
        latent = self._value_latent(**inputs)
        return self.match_outcome_head(latent)

    def metadata(self) -> dict[str, Any]:
        metadata = super().metadata()
        metadata.update({
            "format": MODEL_FORMAT,
            "parent_value_format": PARENT_MODEL_FORMAT,
            "match_outcome_head_schema": OUTCOME_HEAD_SCHEMA,
            "match_outcome_supervision_schema": MATCH_OUTCOME_SCHEMA,
            "match_outcome_estimand": MATCH_OUTCOME_ESTIMAND,
            "match_outcome_hands": NATIONAL_MATCH_HANDS,
            "match_positive_outcome_rule": POSITIVE_OUTCOME_RULE,
            "match_outcome_actions": list(LABELS),
            "match_outcome_output": "uncalibrated_binary_logits",
            "parameters": sum(
                parameter.numel() for parameter in self.parameters()
            ),
        })
        return metadata


def model_from_scale(
    scale: str,
    *,
    cross_encoder: str = "deep_set",
    moe_experts: int = 4,
    dropout: float = 0.10,
) -> OpponentAwareMultiTaskNetV4:
    if scale not in MODEL_SCALES:
        raise ValueError(f"unsupported model scale: {scale}")
    return OpponentAwareMultiTaskNetV4(
        scale=scale,
        cross_encoder=cross_encoder,
        moe_experts=moe_experts,
        dropout=dropout,
    )
