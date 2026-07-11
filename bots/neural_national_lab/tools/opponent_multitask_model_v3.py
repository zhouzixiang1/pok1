"""Opponent-aware distributional multi-task network for the v3 data contract."""
from __future__ import annotations

import math
from typing import Any

import torch
import torch.nn as nn
import torch.nn.functional as F

from cross_hand_sequence import CROSS_HAND_SEQUENCE_DIM, MAX_CROSS_HANDS
from feature_spec import LABELS
from model_input_schema import model_input_metadata
from multitask_training_data import (
    HERO_RESPONSE_ACTION_DIM,
    HERO_RESPONSE_ACTION_SCHEMA,
    MAX_CURRENT_HAND_HISTORY,
    VALUE_FIELDS,
)
from opponent_profile_schema import (
    OPPONENT_PROFILE_DIM,
    OPPONENT_PROFILE_FIELDS,
    OPPONENT_PROFILE_SCHEMA,
)
from opponent_response_schema import OPPONENT_ACTION_LABELS
from strategy_context_schema import STRATEGY_CONTEXT_DIM, STRATEGY_CONTEXT_SCHEMA


MODEL_FORMAT = "opponent_multitask_distributional_v3"
QUANTILE_LEVELS = (0.05, 0.10, 0.20, 0.50)
STATE_DIM = 81
HISTORY_DIM = 24
PROFILE_DIM = OPPONENT_PROFILE_DIM
NUM_ACTIONS = len(LABELS)
RESPONSE_ACTIONS = len(OPPONENT_ACTION_LABELS)
RULE_ACTION_DIM = NUM_ACTIONS
RESPONSE_SIZE_TARGETS = 2


MODEL_SCALES = {
    "small": {
        "state_hidden": 64,
        "profile_hidden": 32,
        "history_hidden": 48,
        "cross_hidden": 48,
        "opponent_hidden": 64,
        "strategy_hidden": 48,
        "fusion_hidden": 128,
        "latent": 64,
        "head_hidden": 64,
    },
    "medium": {
        "state_hidden": 128,
        "profile_hidden": 64,
        "history_hidden": 96,
        "cross_hidden": 96,
        "opponent_hidden": 128,
        "strategy_hidden": 96,
        "fusion_hidden": 256,
        "latent": 128,
        "head_hidden": 128,
    },
    "large": {
        "state_hidden": 256,
        "profile_hidden": 128,
        "history_hidden": 192,
        "cross_hidden": 192,
        "opponent_hidden": 256,
        "strategy_hidden": 192,
        "fusion_hidden": 512,
        "latent": 256,
        "head_hidden": 256,
    },
}


class CrossHandEncoder(nn.Module):
    """Exportable Deep Sets or recurrent encoder over completed prior hands."""

    def __init__(
        self,
        hidden: int,
        *,
        encoder: str = "deep_set",
        moe_experts: int = 4,
    ) -> None:
        super().__init__()
        if hidden < 1:
            raise ValueError("cross-hand hidden size must be positive")
        if encoder not in {"none", "deep_set", "gru", "gru_moe"}:
            raise ValueError(f"unsupported cross-hand encoder: {encoder}")
        if encoder == "gru_moe" and moe_experts < 2:
            raise ValueError("GRU MoE requires at least two experts")
        self.hidden = int(hidden)
        self.encoder = str(encoder)
        self.moe_experts = int(moe_experts)
        self.item_encoder = (
            nn.Sequential(
                nn.Linear(CROSS_HAND_SEQUENCE_DIM, hidden),
                nn.ReLU(),
                nn.Linear(hidden, hidden),
                nn.ReLU(),
            )
            if encoder == "deep_set"
            else None
        )
        self.set_fusion = (
            nn.Sequential(
                nn.Linear(hidden * 2, hidden),
                nn.ReLU(),
            )
            if encoder == "deep_set"
            else None
        )
        self.gru = (
            nn.GRU(CROSS_HAND_SEQUENCE_DIM, hidden, batch_first=True)
            if encoder in {"gru", "gru_moe"}
            else None
        )
        self.gate = (
            nn.Linear(hidden, moe_experts) if encoder == "gru_moe" else None
        )
        self.experts = (
            nn.ModuleList([
                nn.Sequential(
                    nn.Linear(hidden, hidden),
                    nn.ReLU(),
                    nn.Linear(hidden, hidden),
                    nn.ReLU(),
                )
                for _ in range(moe_experts)
            ])
            if encoder == "gru_moe"
            else None
        )

    def forward(
        self, sequence: torch.Tensor, lengths: torch.Tensor
    ) -> torch.Tensor:
        batch = sequence.size(0)
        present = (lengths > 0).float().unsqueeze(1)
        if self.encoder == "none" or sequence.size(1) == 0:
            return torch.zeros(batch, self.hidden, device=sequence.device)
        lengths = lengths.clamp(min=0, max=sequence.size(1))
        if self.item_encoder is not None:
            encoded = self.item_encoder(sequence)
            positions = torch.arange(sequence.size(1), device=sequence.device)
            mask = positions.unsqueeze(0) < lengths.unsqueeze(1)
            float_mask = mask.float().unsqueeze(2)
            mean = (encoded * float_mask).sum(dim=1) / lengths.clamp(
                min=1
            ).float().unsqueeze(1)
            maximum = encoded.masked_fill(~mask.unsqueeze(2), -torch.inf).max(dim=1).values
            maximum = torch.where(present.bool(), maximum, torch.zeros_like(maximum))
            return self.set_fusion(torch.cat([mean, maximum], dim=1)) * present
        packed = nn.utils.rnn.pack_padded_sequence(
            sequence,
            lengths.clamp(min=1).to("cpu"),
            batch_first=True,
            enforce_sorted=False,
        )
        _, final = self.gru(packed)
        embedding = final.squeeze(0) * present
        if self.gate is not None:
            weights = torch.softmax(self.gate(embedding), dim=1)
            experts = torch.stack(
                [expert(embedding) for expert in self.experts], dim=1
            )
            embedding = (experts * weights.unsqueeze(2)).sum(dim=1) * present
        return embedding


class OpponentAwareMultiTaskNetV3(nn.Module):
    def __init__(
        self,
        *,
        scale: str = "medium",
        cross_encoder: str = "deep_set",
        moe_experts: int = 4,
        dropout: float = 0.10,
    ) -> None:
        super().__init__()
        if scale not in MODEL_SCALES:
            raise ValueError(f"unsupported model scale: {scale}")
        if not 0.0 <= dropout < 1.0:
            raise ValueError("dropout must be in [0, 1)")
        config = dict(MODEL_SCALES[scale])
        self.scale = scale
        self.cross_encoder_name = cross_encoder
        self.moe_experts = int(moe_experts)
        self.dropout_rate = float(dropout)
        self.config = config
        self.response_private_indices = tuple(
            model_input_metadata(base_state_dim=48)[
                "response_private_state_masked"
            ]
        )

        self.state_encoder = nn.Sequential(
            nn.Linear(STATE_DIM, config["state_hidden"]),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(config["state_hidden"], config["state_hidden"]),
            nn.ReLU(),
        )
        self.profile_encoder = nn.Sequential(
            nn.Linear(PROFILE_DIM, config["profile_hidden"]),
            nn.ReLU(),
            nn.Linear(config["profile_hidden"], config["profile_hidden"]),
            nn.ReLU(),
        )
        self.history_gru = nn.GRU(
            HISTORY_DIM, config["history_hidden"], batch_first=True
        )
        self.cross_encoder = CrossHandEncoder(
            config["cross_hidden"],
            encoder=cross_encoder,
            moe_experts=moe_experts,
        )
        self.opponent_fusion = nn.Sequential(
            nn.Linear(
                config["profile_hidden"] + config["cross_hidden"],
                config["opponent_hidden"],
            ),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(config["opponent_hidden"], config["opponent_hidden"]),
            nn.ReLU(),
        )
        common_dim = config["state_hidden"] + config["history_hidden"]
        self.opponent_interaction = nn.Linear(
            config["opponent_hidden"], common_dim
        )
        self.strategy_encoder = nn.Sequential(
            nn.Linear(STRATEGY_CONTEXT_DIM, config["strategy_hidden"]),
            nn.ReLU(),
            nn.Linear(config["strategy_hidden"], config["strategy_hidden"]),
            nn.ReLU(),
        )
        value_input = (
            common_dim * 2
            + config["opponent_hidden"]
            + RULE_ACTION_DIM
            + config["strategy_hidden"]
        )
        response_input = (
            common_dim * 2
            + config["opponent_hidden"]
            + HERO_RESPONSE_ACTION_DIM
        )
        self.value_fusion = nn.Sequential(
            nn.Linear(value_input, config["fusion_hidden"]),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(config["fusion_hidden"], config["latent"]),
            nn.ReLU(),
        )
        self.response_fusion = nn.Sequential(
            nn.Linear(response_input, config["fusion_hidden"]),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(config["fusion_hidden"], config["latent"]),
            nn.ReLU(),
        )
        value_width = NUM_ACTIONS * (1 + len(QUANTILE_LEVELS))
        self.value_heads = nn.ModuleDict({
            field: nn.Sequential(
                nn.Linear(config["latent"], config["head_hidden"]),
                nn.ReLU(),
                nn.Linear(config["head_hidden"], value_width),
            )
            for field in VALUE_FIELDS
        })
        self.response_head = nn.Sequential(
            nn.Linear(config["latent"], config["head_hidden"]),
            nn.ReLU(),
            nn.Linear(
                config["head_hidden"],
                RESPONSE_ACTIONS + RESPONSE_SIZE_TARGETS,
            ),
        )

    def _history_embedding(
        self, history: torch.Tensor, lengths: torch.Tensor
    ) -> torch.Tensor:
        batch = history.size(0)
        if history.size(1) == 0:
            return torch.zeros(
                batch, self.config["history_hidden"], device=history.device
            )
        packed = nn.utils.rnn.pack_padded_sequence(
            history,
            lengths.clamp(min=1, max=history.size(1)).to("cpu"),
            batch_first=True,
            enforce_sorted=False,
        )
        _, final = self.history_gru(packed)
        return final.squeeze(0) * (lengths > 0).float().unsqueeze(1)

    def _common_context(
        self,
        state: torch.Tensor,
        profile: torch.Tensor,
        history: torch.Tensor,
        history_lengths: torch.Tensor,
        cross_sequence: torch.Tensor,
        cross_lengths: torch.Tensor,
        *,
        response: bool,
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        if response and self.response_private_indices:
            state = state.clone()
            state[:, list(self.response_private_indices)] = 0.0
        state_embedding = self.state_encoder(state)
        history_embedding = self._history_embedding(history, history_lengths)
        common = torch.cat([state_embedding, history_embedding], dim=1)
        opponent = self.opponent_fusion(torch.cat([
            self.profile_encoder(profile),
            self.cross_encoder(cross_sequence, cross_lengths),
        ], dim=1))
        interaction = common * torch.tanh(self.opponent_interaction(opponent))
        return common, opponent, interaction

    def forward_value(
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
    ) -> dict[str, dict[str, torch.Tensor]]:
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
        latent = self.value_fusion(torch.cat([
            common, opponent, interaction, rule_action, strategy
        ], dim=1))
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

    def forward_response(
        self,
        *,
        state: torch.Tensor,
        profile: torch.Tensor,
        history: torch.Tensor,
        history_lengths: torch.Tensor,
        cross_sequence: torch.Tensor,
        cross_lengths: torch.Tensor,
        hero_action: torch.Tensor,
        legal_action_mask: torch.Tensor | None = None,
    ) -> dict[str, torch.Tensor]:
        common, opponent, interaction = self._common_context(
            state,
            profile,
            history,
            history_lengths,
            cross_sequence,
            cross_lengths,
            response=True,
        )
        latent = self.response_fusion(torch.cat([
            common, opponent, interaction, hero_action
        ], dim=1))
        raw = self.response_head(latent)
        logits = raw[:, :RESPONSE_ACTIONS]
        if legal_action_mask is not None:
            logits = masked_response_logits(logits, legal_action_mask)
        return {
            "logits": logits,
            "size": torch.sigmoid(raw[:, RESPONSE_ACTIONS:]),
        }

    def metadata(self) -> dict[str, Any]:
        return {
            "format": MODEL_FORMAT,
            "scale": self.scale,
            "cross_encoder": self.cross_encoder_name,
            "moe_experts": self.moe_experts,
            "dropout": self.dropout_rate,
            "parameters": sum(parameter.numel() for parameter in self.parameters()),
            "state_dim": STATE_DIM,
            "history_dim": HISTORY_DIM,
            "max_current_hand_history": MAX_CURRENT_HAND_HISTORY,
            "profile_dim": PROFILE_DIM,
            "opponent_profile_schema": OPPONENT_PROFILE_SCHEMA,
            "opponent_profile_fields": list(OPPONENT_PROFILE_FIELDS),
            "cross_hand_sequence_dim": CROSS_HAND_SEQUENCE_DIM,
            "max_cross_hands": MAX_CROSS_HANDS,
            "strategy_context_schema": STRATEGY_CONTEXT_SCHEMA,
            "strategy_context_dim": STRATEGY_CONTEXT_DIM,
            "strategy_context_value_head_only": True,
            "response_private_state_masked": list(self.response_private_indices),
            "hero_action_schema": HERO_RESPONSE_ACTION_SCHEMA,
            "hero_action_dim": HERO_RESPONSE_ACTION_DIM,
            "value_fields": list(VALUE_FIELDS),
            "labels": list(LABELS),
            "quantile_levels": list(QUANTILE_LEVELS),
            "opponent_action_labels": list(OPPONENT_ACTION_LABELS),
            "response_size_targets": [
                "aggressive_increment_pot_log",
                "aggressive_stack_fraction",
            ],
            "stdlib_export_operations": [
                "linear", "relu", "gru", "softplus", "sigmoid",
                "masked_mean", "masked_max",
            ],
        }


def masked_response_logits(
    logits: torch.Tensor, legal_action_mask: torch.Tensor
) -> torch.Tensor:
    if logits.shape != legal_action_mask.shape:
        raise ValueError("response logits and legal mask shapes differ")
    legal = legal_action_mask > 0
    if not bool(legal.any(dim=1).all()):
        raise ValueError("every response row requires at least one legal action")
    return logits.masked_fill(~legal, -1.0e9)


def model_from_scale(
    scale: str, *, cross_encoder: str = "deep_set", dropout: float = 0.10
) -> OpponentAwareMultiTaskNetV3:
    return OpponentAwareMultiTaskNetV3(
        scale=scale, cross_encoder=cross_encoder, dropout=dropout
    )
