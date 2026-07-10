#!/usr/bin/env python3
"""Train a match-aware value and opponent-response network.

The model shares a state, current-hand GRU, and cross-hand opponent encoder.
Counterfactual rows supervise hand/tail/match mean and lower-quantile values;
baseline action traces supervise the opponent's immediate response. The export
is a deterministic JSON artifact intended for a stdlib-only native runtime.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
import os
from pathlib import Path
import random
import sys
from typing import Any

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F


TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from cross_hand_sequence import (  # noqa: E402
    CROSS_HAND_SEQUENCE_DIM,
    CROSS_HAND_SEQUENCE_SCHEMA,
    MAX_CROSS_HANDS,
    normalize_cross_hand_sequence,
)
from feature_spec import LABELS  # noqa: E402
from train_opponent_value_net import (  # noqa: E402
    CROSS_HAND_DIM,
    HIST_FEAT_DIM,
    build_sample,
    collate,
    load_jsonl,
)


VALUE_FIELDS = ("delta_vs_rule", "tail_delta_vs_rule", "match_delta_vs_rule")
VALUE_REFERENCE = "rule_relative_zero_v1"
OPPONENT_ACTION_LABELS = ("fold", "check", "call", "raise", "allin")
NUM_ACTIONS = len(LABELS)
HERO_ACTION_DIM = NUM_ACTIONS + 4
RULE_ACTION_DIM = NUM_ACTIONS
PRIVATE_STATE_INDICES = tuple(range(5, 10))


def _target_vector(row: dict[str, Any], field: str) -> list[float]:
    raw = row.get(field)
    masks = row.get("target_masks") or {}
    mask = masks.get(field) if isinstance(masks, dict) else None
    mask = mask or row.get("target_mask") or row.get("legal_mask")
    if not isinstance(raw, list) or len(raw) != NUM_ACTIONS:
        return [float("nan")] * NUM_ACTIONS
    out = []
    for idx, value in enumerate(raw):
        observed = not isinstance(mask, list) or idx >= len(mask) or bool(mask[idx])
        if value is None or not observed:
            out.append(float("nan"))
        else:
            try:
                out.append(float(value))
            except (TypeError, ValueError):
                out.append(float("nan"))
    return out


def _attach_cross_hand_sequence(
    sample: dict[str, Any], row: dict[str, Any]
) -> None:
    request = row.get("request") or {}
    raw = row.get("cross_hand_sequence")
    if raw is None and isinstance(request, dict):
        raw = request.get("cross_hand_sequence")
    sample["cross_hand_sequence"] = normalize_cross_hand_sequence(raw)


def build_value_sample(row: dict[str, Any], *, max_hist: int) -> dict[str, Any]:
    sample = build_sample(row, max_hist=max_hist, target_field="delta_vs_rule")
    _attach_cross_hand_sequence(sample, row)
    sample["loss_weight"] = float(row.get("_training_loss_weight", 1.0))
    sample["value_targets"] = {
        field: _target_vector(row, field) for field in VALUE_FIELDS
    }
    return sample


def _hero_action_features(row: dict[str, Any]) -> list[float]:
    label_id = int(row.get("hero_action_label_id", 1) or 0)
    label_id = max(0, min(NUM_ACTIONS - 1, label_id))
    one_hot = [1.0 if idx == label_id else 0.0 for idx in range(NUM_ACTIONS)]
    action = float(row.get("hero_action", 0) or 0)
    state = row.get("state") or {}
    request = row.get("request") or {}
    pot = max(1.0, float(state.get("pot", request.get("pot", 150)) or 150))
    to_call = max(0.0, float(state.get("to_call", request.get("to_call", 0)) or 0))
    stack = max(0.0, float(request.get("my_chips", 20000) or 20000))
    amount = stack if action == -2 else max(0.0, action)
    return one_hot + [
        min(1.0, amount / 20000.0),
        min(4.0, amount / pot) / 4.0,
        min(1.0, to_call / 20000.0),
        min(1.0, stack / 20000.0),
    ]


def build_behavior_sample(row: dict[str, Any], *, max_hist: int) -> dict[str, Any] | None:
    try:
        target = int(row["opponent_action_label_id"])
    except (KeyError, TypeError, ValueError):
        return None
    if target < 0 or target >= len(OPPONENT_ACTION_LABELS):
        return None
    sample = build_sample(row, max_hist=max_hist, target_field="delta_vs_rule")
    _attach_cross_hand_sequence(sample, row)
    sample["state"] = list(sample["state"])
    for index in PRIVATE_STATE_INDICES:
        sample["state"][index] = 0.0
    sample["hero_action_features"] = _hero_action_features(row)
    sample["response_target"] = target
    sample["loss_weight"] = float(row.get("_training_loss_weight", 1.0))
    sample["response_amount"] = min(
        1.0, max(0.0, float(row.get("opponent_action_pot_ratio", 0.0) or 0.0) / 4.0)
    )
    return sample


class CrossHandTransformer(nn.Module):
    """Small public-history encoder with explicit exportable operations."""

    def __init__(self, hidden: int, heads: int, max_hands: int) -> None:
        super().__init__()
        if hidden <= 0 or heads <= 0 or hidden % heads:
            raise ValueError("transformer hidden size must be divisible by heads")
        self.hidden = int(hidden)
        self.heads = int(heads)
        self.head_dim = self.hidden // self.heads
        self.max_hands = int(max_hands)
        self.input_proj = nn.Linear(CROSS_HAND_SEQUENCE_DIM, self.hidden)
        self.position = nn.Parameter(torch.zeros(self.max_hands, self.hidden))
        nn.init.normal_(self.position, mean=0.0, std=0.02)
        self.q_proj = nn.Linear(self.hidden, self.hidden)
        self.k_proj = nn.Linear(self.hidden, self.hidden)
        self.v_proj = nn.Linear(self.hidden, self.hidden)
        self.out_proj = nn.Linear(self.hidden, self.hidden)
        self.norm1 = nn.LayerNorm(self.hidden)
        self.ff = nn.Sequential(
            nn.Linear(self.hidden, self.hidden * 2),
            nn.ReLU(),
            nn.Linear(self.hidden * 2, self.hidden),
        )
        self.norm2 = nn.LayerNorm(self.hidden)

    def forward(
        self, sequence: torch.Tensor, lengths: torch.Tensor
    ) -> torch.Tensor:
        batch, width, _ = sequence.shape
        width = min(width, self.max_hands)
        sequence = sequence[:, :width]
        lengths = lengths.clamp(min=0, max=width)
        x = self.input_proj(sequence) + self.position[:width].unsqueeze(0)

        def split_heads(value: torch.Tensor) -> torch.Tensor:
            return value.reshape(
                batch, width, self.heads, self.head_dim
            ).transpose(1, 2)

        query = split_heads(self.q_proj(x))
        key = split_heads(self.k_proj(x))
        value = split_heads(self.v_proj(x))
        scores = torch.matmul(query, key.transpose(-1, -2)) / math.sqrt(
            self.head_dim
        )
        key_padding = torch.arange(width, device=sequence.device).unsqueeze(0) >= (
            lengths.unsqueeze(1)
        )
        scores = scores.masked_fill(key_padding[:, None, None, :], -1e4)
        attention = torch.softmax(scores, dim=-1)
        attended = torch.matmul(attention, value).transpose(1, 2).reshape(
            batch, width, self.hidden
        )
        x = self.norm1(x + self.out_proj(attended))
        x = self.norm2(x + self.ff(x))
        final_index = (lengths - 1).clamp(min=0)
        embedding = x[torch.arange(batch, device=x.device), final_index]
        return embedding * (lengths > 0).float().unsqueeze(1)


class OpponentAwareMultiTaskNet(nn.Module):
    def __init__(
        self,
        state_dim: int,
        profile_dim: int,
        *,
        gru_hidden: int,
        hidden: int,
        latent: int,
        cross_hidden: int,
        head_hidden: int,
        dropout: float,
        cross_sequence_hidden: int = 0,
        cross_sequence_encoder: str = "gru",
        cross_transformer_heads: int = 4,
        cross_moe_experts: int = 4,
    ) -> None:
        super().__init__()
        self.gru = nn.GRU(HIST_FEAT_DIM, gru_hidden, batch_first=True)
        encoder = str(cross_sequence_encoder)
        if encoder not in {
            "none", "gru", "gru_moe", "deep_set", "transformer"
        }:
            raise ValueError(f"unsupported cross-hand sequence encoder: {encoder}")
        if encoder == "gru_moe" and int(cross_moe_experts) < 2:
            raise ValueError("GRU MoE requires at least two experts")
        self.cross_sequence_encoder = encoder
        self.cross_moe_expert_count = int(cross_moe_experts)
        self.cross_sequence_hidden = (
            0 if encoder == "none" else max(1, int(cross_sequence_hidden))
        )
        self.cross_gru = (
            nn.GRU(
                CROSS_HAND_SEQUENCE_DIM,
                self.cross_sequence_hidden,
                batch_first=True,
            )
            if encoder in {"gru", "gru_moe"} and self.cross_sequence_hidden > 0
            else None
        )
        self.cross_moe_gate = (
            nn.Linear(self.cross_sequence_hidden, self.cross_moe_expert_count)
            if encoder == "gru_moe"
            else None
        )
        self.cross_moe_expert_layers = (
            nn.ModuleList([
                nn.Sequential(
                    nn.Linear(
                        self.cross_sequence_hidden, self.cross_sequence_hidden
                    ),
                    nn.ReLU(),
                    nn.Linear(
                        self.cross_sequence_hidden, self.cross_sequence_hidden
                    ),
                    nn.ReLU(),
                )
                for _ in range(self.cross_moe_expert_count)
            ])
            if encoder == "gru_moe"
            else None
        )
        self.cross_set_encoder = (
            nn.Sequential(
                nn.Linear(CROSS_HAND_SEQUENCE_DIM, self.cross_sequence_hidden),
                nn.ReLU(),
                nn.Linear(self.cross_sequence_hidden, self.cross_sequence_hidden),
                nn.ReLU(),
            )
            if encoder == "deep_set"
            else None
        )
        self.cross_transformer = (
            CrossHandTransformer(
                self.cross_sequence_hidden,
                int(cross_transformer_heads),
                MAX_CROSS_HANDS,
            )
            if encoder == "transformer"
            else None
        )
        self.opponent_dropout = nn.Dropout(dropout)
        self.opp_encoder = nn.Sequential(
            nn.Linear(CROSS_HAND_DIM, cross_hidden),
            nn.ReLU(),
            nn.Linear(cross_hidden, cross_hidden),
            nn.ReLU(),
        )
        context_dim = (
            state_dim
            + profile_dim
            + gru_hidden
            + cross_hidden
            + self.cross_sequence_hidden
        )
        self.shared = nn.Sequential(
            nn.Linear(context_dim + RULE_ACTION_DIM, hidden),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(hidden, latent),
            nn.ReLU(),
        )
        self.response_shared = nn.Sequential(
            nn.Linear(context_dim, hidden),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(hidden, latent),
            nn.ReLU(),
        )
        self.value_heads = nn.ModuleDict({
            field: nn.Sequential(
                nn.Linear(latent, head_hidden),
                nn.ReLU(),
                nn.Linear(head_hidden, NUM_ACTIONS * 2),
            )
            for field in VALUE_FIELDS
        })
        self.response_head = nn.Sequential(
            nn.Linear(latent + HERO_ACTION_DIM, head_hidden),
            nn.ReLU(),
            nn.Linear(head_hidden, len(OPPONENT_ACTION_LABELS) + 1),
        )

    def encode(
        self,
        state: torch.Tensor,
        profile: torch.Tensor,
        history: torch.Tensor,
        lengths: torch.Tensor,
        cross_hand: torch.Tensor,
        *,
        response: bool = False,
        rule_action: torch.Tensor | None = None,
        cross_sequence: torch.Tensor | None = None,
        cross_lengths: torch.Tensor | None = None,
    ) -> torch.Tensor:
        if history.size(1) > 0 and bool((lengths > 0).any()):
            packed = nn.utils.rnn.pack_padded_sequence(
                history,
                lengths.clamp(min=1).to("cpu"),
                batch_first=True,
                enforce_sorted=False,
            )
            _, final = self.gru(packed)
            history_embedding = final.squeeze(0) * (lengths > 0).float().unsqueeze(1)
        else:
            history_embedding = torch.zeros(
                state.size(0), self.gru.hidden_size, device=state.device
            )
        profile = self.opponent_dropout(profile)
        opponent_embedding = self.opp_encoder(self.opponent_dropout(cross_hand))
        context_parts = [state, profile, history_embedding, opponent_embedding]
        if self.cross_sequence_hidden > 0:
            if cross_sequence is None or cross_lengths is None:
                cross_embedding = torch.zeros(
                    state.size(0), self.cross_sequence_hidden, device=state.device
                )
            elif self.cross_gru is not None and bool((cross_lengths > 0).any()):
                packed_cross = nn.utils.rnn.pack_padded_sequence(
                    cross_sequence,
                    cross_lengths.clamp(min=1).to("cpu"),
                    batch_first=True,
                    enforce_sorted=False,
                )
                _, cross_final = self.cross_gru(packed_cross)
                cross_embedding = cross_final.squeeze(0) * (
                    cross_lengths > 0
                ).float().unsqueeze(1)
                if self.cross_moe_gate is not None:
                    gate = torch.softmax(
                        self.cross_moe_gate(cross_embedding), dim=1
                    )
                    experts = torch.stack([
                        expert(cross_embedding)
                        for expert in self.cross_moe_expert_layers
                    ], dim=1)
                    cross_embedding = (
                        experts * gate.unsqueeze(2)
                    ).sum(dim=1) * (
                        cross_lengths > 0
                    ).float().unsqueeze(1)
            elif self.cross_set_encoder is not None:
                encoded = self.cross_set_encoder(cross_sequence)
                mask = (
                    torch.arange(cross_sequence.size(1), device=state.device)
                    .unsqueeze(0) < cross_lengths.unsqueeze(1)
                ).float().unsqueeze(2)
                cross_embedding = (encoded * mask).sum(dim=1) / (
                    cross_lengths.clamp(min=1).float().unsqueeze(1)
                )
                cross_embedding = cross_embedding * (
                    cross_lengths > 0
                ).float().unsqueeze(1)
            elif self.cross_transformer is not None:
                cross_embedding = self.cross_transformer(
                    cross_sequence, cross_lengths
                )
            else:
                cross_embedding = torch.zeros(
                    state.size(0), self.cross_sequence_hidden, device=state.device
                )
            context_parts.append(cross_embedding)
        context = torch.cat(context_parts, dim=1)
        if response:
            return self.response_shared(context)
        if rule_action is None:
            rule_action = torch.zeros(
                state.size(0), RULE_ACTION_DIM, device=state.device
            )
        return self.shared(torch.cat([context, rule_action], dim=1))

    def value(self, latent: torch.Tensor) -> dict[str, torch.Tensor]:
        return {field: head(latent) for field, head in self.value_heads.items()}

    def response(self, latent: torch.Tensor, hero_action: torch.Tensor) -> torch.Tensor:
        return self.response_head(torch.cat([latent, hero_action], dim=1))


def _context_tensors(samples: list[dict[str, Any]], *, max_hist: int, device: str):
    state, profile, cross, history, lengths, *_ = collate(
        samples, max_hist=max_hist, device=device
    )
    if cross is None:
        cross = torch.zeros(len(samples), CROSS_HAND_DIM, device=device)
    sequences = [
        normalize_cross_hand_sequence(sample.get("cross_hand_sequence"))
        for sample in samples
    ]
    cross_lengths = torch.tensor(
        [len(sequence) for sequence in sequences], dtype=torch.long, device=device
    )
    width = max(1, max((len(sequence) for sequence in sequences), default=0))
    cross_sequence = torch.zeros(
        len(samples), width, CROSS_HAND_SEQUENCE_DIM,
        dtype=torch.float32, device=device,
    )
    for index, sequence in enumerate(sequences):
        if sequence:
            cross_sequence[index, :len(sequence)] = torch.tensor(
                sequence, dtype=torch.float32, device=device
            )
    return (
        state, profile, history, lengths, cross,
        cross_sequence, cross_lengths,
    )


def _value_batch_tensors(samples: list[dict[str, Any]], device: str):
    return {
        field: torch.tensor(
            [sample["value_targets"][field] for sample in samples],
            dtype=torch.float32,
            device=device,
        )
        for field in VALUE_FIELDS
    }


def _rule_ids_tensor(samples: list[dict[str, Any]], device: str) -> torch.Tensor:
    return torch.tensor(
        [int(sample.get("rule_id", 1) or 0) for sample in samples],
        dtype=torch.long,
        device=device,
    ).clamp(min=0, max=NUM_ACTIONS - 1)


def _rule_action_tensor(samples: list[dict[str, Any]], device: str) -> torch.Tensor:
    labels = _rule_ids_tensor(samples, device)
    return F.one_hot(labels, num_classes=NUM_ACTIONS).float()


def _loss_weight_tensor(
    samples: list[dict[str, Any]], device: str
) -> torch.Tensor:
    return torch.tensor(
        [max(0.0, float(sample.get("loss_weight", 1.0))) for sample in samples],
        dtype=torch.float32,
        device=device,
    )


def _weighted_mean(values: torch.Tensor, weights: torch.Tensor) -> torch.Tensor:
    return (values * weights).sum() / weights.sum().clamp(min=1e-6)


def _value_loss(
    output: torch.Tensor,
    target: torch.Tensor,
    *,
    clip: float,
    quantile: float,
    row_weights: torch.Tensor | None = None,
) -> tuple[torch.Tensor, int]:
    mean, lower = output[:, :NUM_ACTIONS], output[:, NUM_ACTIONS:]
    valid = ~torch.isnan(target)
    if not bool(valid.any()):
        return output.sum() * 0.0, 0
    normalized = torch.clamp(target, -clip, clip) / clip
    clean = torch.where(valid, normalized, torch.zeros_like(normalized))
    if row_weights is None:
        row_weights = torch.ones(target.shape[0], device=target.device)
    weights = row_weights.unsqueeze(1).expand_as(target)[valid]
    mean_loss = _weighted_mean(
        F.smooth_l1_loss(mean[valid], clean[valid], reduction="none"),
        weights,
    )
    error = clean[valid] - lower[valid]
    quantile_loss = _weighted_mean(
        torch.maximum(quantile * error, (quantile - 1.0) * error),
        weights,
    )
    order_penalty = _weighted_mean(F.relu(lower[valid] - mean[valid]), weights)
    return mean_loss + quantile_loss + 0.1 * order_penalty, int(valid.sum().item())


def _ranking_statistics(
    samples: list[dict[str, Any]], *, field: str, margin: float
) -> dict[str, Any]:
    action_counts = [0] * NUM_ACTIONS
    weighted_action_counts = [0.0] * NUM_ACTIONS
    positive = negative = 0
    weighted_positive = weighted_negative = 0.0
    for sample in samples:
        sample_weight = max(0.0, float(sample.get("loss_weight", 1.0)))
        rule_id = int(sample.get("rule_id", -1) or 0)
        target = sample["value_targets"][field]
        for action, value in enumerate(target):
            if action == rule_id or math.isnan(value) or abs(value) <= margin:
                continue
            action_counts[action] += 1
            weighted_action_counts[action] += sample_weight
            if value > 0:
                positive += 1
                weighted_positive += sample_weight
            else:
                negative += 1
                weighted_negative += sample_weight
    present = sum(count > 0 for count in weighted_action_counts)
    weighted_total = sum(weighted_action_counts)
    action_weights = [
        math.sqrt(weighted_total / (present * count)) if count and present else 0.0
        for count in weighted_action_counts
    ]
    positive_weight = (
        weighted_negative / weighted_positive if weighted_positive else 1.0
    )
    return {
        "field": field,
        "margin": float(margin),
        "samples": sum(action_counts),
        "weighted_samples": float(weighted_total),
        "positive": positive,
        "negative": negative,
        "weighted_positive": float(weighted_positive),
        "weighted_negative": float(weighted_negative),
        "positive_weight": float(positive_weight),
        "per_action_samples": action_counts,
        "weighted_per_action_samples": weighted_action_counts,
        "per_action_weights": action_weights,
    }


def _pairwise_ranking_loss(
    output: torch.Tensor,
    target: torch.Tensor,
    rule_ids: torch.Tensor,
    *,
    margin: float,
    temperature: float,
    positive_weight: torch.Tensor,
    action_weights: torch.Tensor,
    row_weights: torch.Tensor | None = None,
    head: str = "mean",
) -> tuple[torch.Tensor, int]:
    if head == "mean":
        values = output[:, :NUM_ACTIONS]
    elif head == "lower":
        values = output[:, NUM_ACTIONS:]
    else:
        raise ValueError(f"unknown ranking head: {head}")
    valid = ~torch.isnan(target)
    valid &= target.abs() > margin
    action_ids = torch.arange(NUM_ACTIONS, device=target.device).unsqueeze(0)
    valid &= action_ids != rule_ids.unsqueeze(1)
    if not bool(valid.any()):
        return output.sum() * 0.0, 0
    # Every target is already candidate minus rule. Runtime uses the same zero
    # reference and never consumes the model's rule-action output.
    logits = values / max(1e-6, float(temperature))
    labels = (target > 0).float()
    raw = F.binary_cross_entropy_with_logits(
        logits[valid],
        labels[valid],
        pos_weight=positive_weight,
        reduction="none",
    )
    if row_weights is None:
        row_weights = torch.ones(target.shape[0], device=target.device)
    weights = (
        action_weights.unsqueeze(0).expand_as(target)
        * row_weights.unsqueeze(1)
    )[valid]
    loss = (raw * weights).sum() / weights.sum().clamp(min=1e-6)
    return loss, int(valid.sum().item())


def _chunks(order: list[int], batch_size: int) -> list[list[int]]:
    return [order[start:start + batch_size] for start in range(0, len(order), batch_size)]


def _opponents(rows: list[dict[str, Any]]) -> set[str]:
    return {
        str(row.get("_opponent_label") or row.get("opponent"))
        for row in rows
        if row.get("_opponent_label") or row.get("opponent")
    }


def _manifest(path: str | None, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not path:
        return None
    return {
        "path": str(Path(path).resolve()),
        "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        "rows": len(rows),
        "opponents": sorted(_opponents(rows)),
    }


def _assert_disjoint(split_rows: dict[str, list[dict[str, Any]]]) -> dict[str, list[str]]:
    split_opponents = {name: _opponents(rows) for name, rows in split_rows.items()}
    names = list(split_opponents)
    for left_idx, left in enumerate(names):
        for right in names[left_idx + 1:]:
            overlap = split_opponents[left] & split_opponents[right]
            if overlap:
                raise SystemExit(
                    f"opponent leakage between {left} and {right}: {sorted(overlap)}"
                )
    return {name: sorted(values) for name, values in split_opponents.items()}


def _match_cluster_key(row: dict[str, Any]) -> tuple[str, int, int]:
    opponent = str(row.get("_opponent_label") or row.get("opponent") or "")
    try:
        deck_seed = int(row["deck_seed_base"])
        bot_seed = int(row["bot_seed_base"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("training row is missing a match-cluster seed") from exc
    if not opponent:
        raise ValueError("training row is missing an opponent label")
    return opponent, deck_seed, bot_seed


def _attach_training_row_weights(
    rows: list[dict[str, Any]], *, scheme: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if scheme not in {"uniform", "opponent_balanced"}:
        raise ValueError(f"unknown training row weighting scheme: {scheme}")
    copied = [dict(row) for row in rows]
    if not copied:
        return copied, {
            "scheme": scheme,
            "rows": 0,
            "opponents": 0,
            "clusters": 0,
            "min_row_weight": None,
            "max_row_weight": None,
        }
    groups: dict[str, list[int]] = defaultdict(list)
    clusters: dict[str, set[tuple[str, int, int]]] = defaultdict(set)
    for index, row in enumerate(copied):
        key = _match_cluster_key(row)
        groups[key[0]].append(index)
        clusters[key[0]].add(key)
    raw_weights = [1.0] * len(copied)
    if scheme == "opponent_balanced":
        for indices in groups.values():
            row_weight = 1.0 / len(indices)
            for index in indices:
                raw_weights[index] = row_weight
    scale = len(raw_weights) / sum(raw_weights)
    weights = [weight * scale for weight in raw_weights]
    for row, weight in zip(copied, weights):
        row["_training_loss_weight"] = float(weight)
    per_opponent = {}
    for opponent, indices in sorted(groups.items()):
        per_opponent[opponent] = {
            "rows": len(indices),
            "clusters": len(clusters[opponent]),
            "total_weight": float(sum(weights[index] for index in indices)),
        }
    return copied, {
        "scheme": scheme,
        "rows": len(copied),
        "opponents": len(groups),
        "clusters": sum(len(values) for values in clusters.values()),
        "min_row_weight": float(min(weights)),
        "max_row_weight": float(max(weights)),
        "mean_row_weight": float(sum(weights) / len(weights)),
        "per_opponent": per_opponent,
    }


def _stratified_cluster_bootstrap(
    value_rows: list[dict[str, Any]],
    behavior_rows: list[dict[str, Any]],
    *,
    seed: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    def group(rows: list[dict[str, Any]]):
        grouped: dict[tuple[str, int, int], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[_match_cluster_key(row)].append(row)
        return dict(grouped)

    value_groups = group(value_rows)
    behavior_groups = group(behavior_rows)
    if set(value_groups) != set(behavior_groups):
        value_only = sorted(set(value_groups) - set(behavior_groups))
        behavior_only = sorted(set(behavior_groups) - set(value_groups))
        raise ValueError(
            "value/behavior match clusters differ: "
            f"value_only={value_only} behavior_only={behavior_only}"
        )
    by_opponent: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
    for key in value_groups:
        by_opponent[key[0]].append(key)
    rng = random.Random(int(seed))
    multiplicities: Counter[tuple[str, int, int]] = Counter()
    per_opponent = {}
    for opponent in sorted(by_opponent):
        keys = sorted(by_opponent[opponent])
        for _ in keys:
            multiplicities[keys[rng.randrange(len(keys))]] += 1
        per_opponent[opponent] = {
            "source_clusters": len(keys),
            "sampled_draws": len(keys),
            "unique_sampled_clusters": sum(
                multiplicities[key] > 0 for key in keys
            ),
        }

    def expand(groups: dict[tuple[str, int, int], list[dict[str, Any]]]):
        expanded = []
        for key in sorted(groups):
            for _ in range(multiplicities[key]):
                expanded.extend(groups[key])
        return expanded

    sampled_value = expand(value_groups)
    sampled_behavior = expand(behavior_groups)
    report = {
        "enabled": True,
        "scheme": "opponent_stratified_match_cluster_v1",
        "seed": int(seed),
        "source_clusters": len(value_groups),
        "sampled_draws": sum(multiplicities.values()),
        "unique_sampled_clusters": sum(
            multiplicity > 0 for multiplicity in multiplicities.values()
        ),
        "omitted_clusters": len(value_groups) - len(multiplicities),
        "source_value_rows": len(value_rows),
        "source_behavior_rows": len(behavior_rows),
        "effective_value_rows": len(sampled_value),
        "effective_behavior_rows": len(sampled_behavior),
        "per_opponent": per_opponent,
        "clusters": [
            {
                "opponent": key[0],
                "deck_seed_base": key[1],
                "bot_seed_base": key[2],
                "multiplicity": multiplicities[key],
            }
            for key in sorted(value_groups)
        ],
    }
    return sampled_value, sampled_behavior, report


def _direction_report(counts: dict[str, Any]) -> dict[str, Any]:
    class_accuracies = [
        counts[f"{label}_correct"] / counts[label]
        for label in ("positive", "negative")
        if counts[label]
    ]
    return {
        "direction_samples": counts["total"],
        "direction_accuracy": (
            counts["correct"] / counts["total"] if counts["total"] else None
        ),
        "direction_balanced_accuracy": (
            float(np.mean(class_accuracies)) if class_accuracies else None
        ),
        "target_positive_rate": (
            counts["positive"] / counts["total"] if counts["total"] else None
        ),
        "predicted_positive_rate": (
            counts["predicted_positive"] / counts["total"]
            if counts["total"] else None
        ),
        "direction_per_action": {
            LABELS[action]: {
                "samples": row["total"],
                "accuracy": (
                    row["correct"] / row["total"] if row["total"] else None
                ),
            }
            for action, row in enumerate(counts["actions"])
        },
    }


def _new_direction_counts() -> dict[str, Any]:
    return {
        "total": 0,
        "correct": 0,
        "positive": 0,
        "negative": 0,
        "positive_correct": 0,
        "negative_correct": 0,
        "predicted_positive": 0,
        "actions": [
            {"total": 0, "correct": 0} for _ in range(NUM_ACTIONS)
        ],
    }


def _update_direction_counts(
    counts: dict[str, Any],
    *,
    predicted_positive: torch.Tensor,
    target_positive: torch.Tensor,
    valid: torch.Tensor,
) -> None:
    correct = predicted_positive == target_positive
    counts["total"] += int(valid.sum().item())
    counts["correct"] += int((correct & valid).sum().item())
    counts["positive"] += int((target_positive & valid).sum().item())
    counts["negative"] += int(((~target_positive) & valid).sum().item())
    counts["positive_correct"] += int(
        (correct & target_positive & valid).sum().item()
    )
    counts["negative_correct"] += int(
        (correct & (~target_positive) & valid).sum().item()
    )
    counts["predicted_positive"] += int(
        (predicted_positive & valid).sum().item()
    )
    for action in range(NUM_ACTIONS):
        action_valid = valid[:, action]
        counts["actions"][action]["total"] += int(action_valid.sum().item())
        counts["actions"][action]["correct"] += int(
            (correct[:, action] & action_valid).sum().item()
        )


def _evaluate(
    model: OpponentAwareMultiTaskNet,
    value_samples: list[dict[str, Any]],
    behavior_samples: list[dict[str, Any]],
    *,
    clips: dict[str, float],
    max_hist: int,
    batch_size: int,
    device: str,
    lower_calibration: dict[str, Any] | None = None,
    response_temperature: float = 1.0,
    ranking_margin: float = 100.0,
    direction_score_weight: float = 0.5,
    lower_direction_score_weight: float = 0.5,
) -> dict[str, Any]:
    model.eval()
    abs_errors = {field: [] for field in VALUE_FIELDS}
    lower_coverage = {field: [] for field in VALUE_FIELDS}
    direction_counts = {
        field: _new_direction_counts() for field in VALUE_FIELDS
    }
    lower_direction_counts = {
        field: _new_direction_counts() for field in VALUE_FIELDS
    }
    response_total = response_correct = 0
    class_total = [0] * len(OPPONENT_ACTION_LABELS)
    class_correct = [0] * len(OPPONENT_ACTION_LABELS)
    response_nll = 0.0
    amount_errors = []
    with torch.no_grad():
        for indices in _chunks(list(range(len(value_samples))), batch_size):
            batch = [value_samples[idx] for idx in indices]
            (
                state, profile, history, lengths, cross,
                cross_sequence, cross_lengths,
            ) = _context_tensors(
                batch, max_hist=max_hist, device=device
            )
            outputs = model.value(model.encode(
                state,
                profile,
                history,
                lengths,
                cross,
                rule_action=_rule_action_tensor(batch, device),
                cross_sequence=cross_sequence,
                cross_lengths=cross_lengths,
            ))
            targets = _value_batch_tensors(batch, device)
            for field in VALUE_FIELDS:
                target = targets[field]
                valid = ~torch.isnan(target)
                for row_index, sample in enumerate(batch):
                    rule_id = int(sample.get("rule_id", -1) or 0)
                    if 0 <= rule_id < NUM_ACTIONS:
                        valid[row_index, rule_id] = False
                if not bool(valid.any()):
                    continue
                clipped = torch.clamp(target, -clips[field], clips[field])
                mean = outputs[field][:, :NUM_ACTIONS] * clips[field]
                lower = outputs[field][:, NUM_ACTIONS:] * clips[field]
                calibration = (lower_calibration or {}).get(field) or {}
                offsets = calibration.get("offsets") or [0.0] * NUM_ACTIONS
                offset_tensor = torch.tensor(
                    offsets, dtype=torch.float32, device=device
                ).unsqueeze(0)
                lower = lower + offset_tensor
                abs_errors[field].extend((mean[valid] - clipped[valid]).abs().cpu().tolist())
                lower_coverage[field].extend((clipped[valid] <= lower[valid]).cpu().tolist())
                predicted_positive = mean > 0
                lower_predicted_positive = lower > 0
                target_positive = target > 0
                direction_valid = valid & (target.abs() > ranking_margin)
                _update_direction_counts(
                    direction_counts[field],
                    predicted_positive=predicted_positive,
                    target_positive=target_positive,
                    valid=direction_valid,
                )
                _update_direction_counts(
                    lower_direction_counts[field],
                    predicted_positive=lower_predicted_positive,
                    target_positive=target_positive,
                    valid=direction_valid,
                )
        for indices in _chunks(list(range(len(behavior_samples))), batch_size):
            batch = [behavior_samples[idx] for idx in indices]
            (
                state, profile, history, lengths, cross,
                cross_sequence, cross_lengths,
            ) = _context_tensors(
                batch, max_hist=max_hist, device=device
            )
            latent = model.encode(
                state, profile, history, lengths, cross, response=True,
                cross_sequence=cross_sequence, cross_lengths=cross_lengths,
            )
            hero = torch.tensor(
                [sample["hero_action_features"] for sample in batch],
                dtype=torch.float32,
                device=device,
            )
            target = torch.tensor(
                [sample["response_target"] for sample in batch],
                dtype=torch.long,
                device=device,
            )
            output = model.response(latent, hero)
            logits = output[:, :len(OPPONENT_ACTION_LABELS)] / max(
                1e-6, float(response_temperature)
            )
            predicted = logits.argmax(dim=1)
            response_nll += float(F.cross_entropy(logits, target, reduction="sum").item())
            response_total += len(batch)
            response_correct += int((predicted == target).sum().item())
            for label in range(len(OPPONENT_ACTION_LABELS)):
                label_mask = target == label
                count = int(label_mask.sum().item())
                class_total[label] += count
                class_correct[label] += int(((predicted == target) & label_mask).sum().item())
            amount_mask = (target == OPPONENT_ACTION_LABELS.index("raise")) | (
                target == OPPONENT_ACTION_LABELS.index("allin")
            )
            if bool(amount_mask.any()):
                amount_target = torch.tensor(
                    [sample["response_amount"] for sample in batch],
                    dtype=torch.float32,
                    device=device,
                )
                amount_pred = torch.sigmoid(output[:, -1])
                amount_errors.extend(
                    (amount_pred[amount_mask] - amount_target[amount_mask]).abs().cpu().tolist()
                )
    per_class = {
        label: {
            "rows": class_total[idx],
            "accuracy": class_correct[idx] / class_total[idx] if class_total[idx] else None,
        }
        for idx, label in enumerate(OPPONENT_ACTION_LABELS)
    }
    present_accuracy = [
        class_correct[idx] / class_total[idx]
        for idx in range(len(class_total))
        if class_total[idx]
    ]
    result = {
        "value": {
            field: {
                "mae": float(np.mean(abs_errors[field])) if abs_errors[field] else None,
                "samples": len(abs_errors[field]),
                "lower_quantile_coverage": float(np.mean(lower_coverage[field]))
                if lower_coverage[field] else None,
                **_direction_report(direction_counts[field]),
                **{
                    f"lower_{key}": value
                    for key, value in _direction_report(
                        lower_direction_counts[field]
                    ).items()
                },
            }
            for field in VALUE_FIELDS
        },
        "response": {
            "rows": response_total,
            "accuracy": response_correct / response_total if response_total else None,
            "balanced_accuracy": float(np.mean(present_accuracy)) if present_accuracy else None,
            "nll": response_nll / response_total if response_total else None,
            "raise_amount_mae": float(np.mean(amount_errors)) if amount_errors else None,
            "per_class": per_class,
        },
    }
    hand_mae = result["value"]["delta_vs_rule"]["mae"]
    tail_mae = result["value"]["tail_delta_vs_rule"]["mae"]
    match_mae = result["value"]["match_delta_vs_rule"]["mae"]
    balanced = result["response"]["balanced_accuracy"]
    match_direction = result["value"]["match_delta_vs_rule"][
        "direction_balanced_accuracy"
    ]
    lower_match_direction = result["value"]["match_delta_vs_rule"][
        "lower_direction_balanced_accuracy"
    ]
    score = 0.0
    score += hand_mae / clips["delta_vs_rule"] if hand_mae is not None else 2.0
    score += 0.5 * tail_mae / clips["tail_delta_vs_rule"] if tail_mae is not None else 1.0
    score += 0.5 * match_mae / clips["match_delta_vs_rule"] if match_mae is not None else 1.0
    score += 1.0 - balanced if balanced is not None else 1.0
    score += direction_score_weight * (
        1.0 - match_direction if match_direction is not None else 1.0
    )
    score += lower_direction_score_weight * (
        1.0 - lower_match_direction
        if lower_match_direction is not None else 1.0
    )
    result["selection_score"] = float(score)
    return result


def _calibrate_lower_bounds(
    model: OpponentAwareMultiTaskNet,
    samples: list[dict[str, Any]],
    *,
    clips: dict[str, float],
    quantile: float,
    min_per_action: int,
    max_hist: int,
    batch_size: int,
    device: str,
) -> dict[str, Any]:
    residuals = {
        field: [[] for _ in range(NUM_ACTIONS)] for field in VALUE_FIELDS
    }
    model.eval()
    with torch.no_grad():
        for indices in _chunks(list(range(len(samples))), batch_size):
            batch = [samples[index] for index in indices]
            (
                state, profile, history, lengths, cross,
                cross_sequence, cross_lengths,
            ) = _context_tensors(
                batch, max_hist=max_hist, device=device
            )
            outputs = model.value(model.encode(
                state,
                profile,
                history,
                lengths,
                cross,
                rule_action=_rule_action_tensor(batch, device),
                cross_sequence=cross_sequence,
                cross_lengths=cross_lengths,
            ))
            targets = _value_batch_tensors(batch, device)
            for field in VALUE_FIELDS:
                target = torch.clamp(targets[field], -clips[field], clips[field])
                lower = outputs[field][:, NUM_ACTIONS:] * clips[field]
                valid = ~torch.isnan(target)
                for row_index, sample in enumerate(batch):
                    rule_id = int(sample.get("rule_id", -1) or 0)
                    for action_id in range(NUM_ACTIONS):
                        if action_id == rule_id or not bool(valid[row_index, action_id]):
                            continue
                        residuals[field][action_id].append(
                            float((target[row_index, action_id] - lower[row_index, action_id]).item())
                        )
    calibration = {}
    for field in VALUE_FIELDS:
        global_values = [
            value for action_values in residuals[field] for value in action_values
        ]
        global_offset = (
            float(np.quantile(global_values, quantile)) if global_values else 0.0
        )
        offsets = []
        for action_values in residuals[field]:
            values = action_values if len(action_values) >= min_per_action else global_values
            offsets.append(
                float(np.quantile(values, quantile)) if values else 0.0
            )
        calibration[field] = {
            "quantile": float(quantile),
            "global_offset": global_offset,
            "offsets": offsets,
            "samples": len(global_values),
            "per_action_samples": [len(values) for values in residuals[field]],
            "min_per_action": int(min_per_action),
        }
    return calibration


def _calibrate_response_temperature(
    model: OpponentAwareMultiTaskNet,
    samples: list[dict[str, Any]],
    *,
    max_hist: int,
    batch_size: int,
    device: str,
) -> dict[str, Any]:
    logits_rows = []
    targets = []
    model.eval()
    with torch.no_grad():
        for indices in _chunks(list(range(len(samples))), batch_size):
            batch = [samples[index] for index in indices]
            (
                state, profile, history, lengths, cross,
                cross_sequence, cross_lengths,
            ) = _context_tensors(
                batch, max_hist=max_hist, device=device
            )
            latent = model.encode(
                state, profile, history, lengths, cross, response=True,
                cross_sequence=cross_sequence, cross_lengths=cross_lengths,
            )
            hero = torch.tensor(
                [sample["hero_action_features"] for sample in batch],
                dtype=torch.float32,
                device=device,
            )
            output = model.response(latent, hero)
            logits_rows.extend(
                output[:, :len(OPPONENT_ACTION_LABELS)].cpu().tolist()
            )
            targets.extend(int(sample["response_target"]) for sample in batch)
    if not logits_rows:
        return {"temperature": 1.0, "rows": 0, "nll_before": None, "nll_after": None}
    logits_array = np.asarray(logits_rows, dtype=np.float64)
    target_array = np.asarray(targets, dtype=np.int64)

    def nll(temperature: float) -> float:
        scaled = logits_array / float(temperature)
        scaled -= scaled.max(axis=1, keepdims=True)
        log_denom = np.log(np.exp(scaled).sum(axis=1))
        return float(np.mean(log_denom - scaled[np.arange(len(scaled)), target_array]))

    temperatures = np.geomspace(0.25, 4.0, 81)
    best_temperature = min(temperatures, key=nll)
    return {
        "temperature": float(best_temperature),
        "rows": len(targets),
        "nll_before": nll(1.0),
        "nll_after": nll(float(best_temperature)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for split in ("train", "val", "held-out"):
        required = split != "held-out"
        parser.add_argument(f"--value-{split}", required=required)
        parser.add_argument(f"--behavior-{split}", required=required)
    parser.add_argument("--value-calibration")
    parser.add_argument("--behavior-calibration")
    parser.add_argument("--out", required=True)
    parser.add_argument("--hidden", type=int, default=256)
    parser.add_argument("--latent", type=int, default=128)
    parser.add_argument("--gru-hidden", type=int, default=96)
    parser.add_argument("--cross-hidden", type=int, default=64)
    parser.add_argument("--cross-sequence-hidden", type=int, default=64)
    parser.add_argument(
        "--cross-sequence-encoder",
        choices=("none", "gru", "gru_moe", "deep_set", "transformer"),
        default="gru",
    )
    parser.add_argument("--cross-transformer-heads", type=int, default=4)
    parser.add_argument("--cross-moe-experts", type=int, default=4)
    parser.add_argument("--head-hidden", type=int, default=128)
    parser.add_argument("--dropout", type=float, default=0.1)
    parser.add_argument("--max-hist", type=int, default=16)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--patience", type=int, default=15)
    parser.add_argument("--lr", type=float, default=5e-4)
    parser.add_argument("--weight-decay", type=float, default=1e-5)
    parser.add_argument("--behavior-weight", type=float, default=1.0)
    parser.add_argument("--max-behavior-batches-per-value", type=int, default=4)
    parser.add_argument("--hand-clip", type=float, default=5000.0)
    parser.add_argument("--tail-clip", type=float, default=2000.0)
    parser.add_argument("--match-clip", type=float, default=2000.0)
    parser.add_argument("--lower-quantile", type=float, default=0.2)
    parser.add_argument("--match-ranking-weight", type=float, default=0.5)
    parser.add_argument("--match-lower-ranking-weight", type=float, default=0.0)
    parser.add_argument("--ranking-margin", type=float, default=100.0)
    parser.add_argument("--ranking-temperature", type=float, default=0.1)
    parser.add_argument("--direction-score-weight", type=float, default=0.5)
    parser.add_argument("--lower-direction-score-weight", type=float, default=0.5)
    parser.add_argument("--min-calibration-per-action", type=int, default=20)
    parser.add_argument("--cluster-bootstrap", action="store_true")
    parser.add_argument(
        "--training-row-weighting",
        choices=("uniform", "opponent_balanced"),
        default="uniform",
    )
    parser.add_argument("--require-cross-hand-sequence", action="store_true")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args(argv)
    if (
        args.match_ranking_weight < 0
        or args.match_lower_ranking_weight < 0
        or args.direction_score_weight < 0
        or args.lower_direction_score_weight < 0
    ):
        raise SystemExit("ranking and direction score weights must be non-negative")
    if args.ranking_margin < 0 or args.ranking_temperature <= 0:
        raise SystemExit("ranking margin must be non-negative and temperature positive")

    os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(args.seed)
    torch.use_deterministic_algorithms(True, warn_only=True)
    if hasattr(torch.backends, "cudnn"):
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True
    device = args.device if not str(args.device).startswith("cuda") or torch.cuda.is_available() else "cpu"

    paths = {
        "value_train": args.value_train,
        "value_val": args.value_val,
        "value_held_out": args.value_held_out,
        "value_calibration": args.value_calibration,
        "behavior_train": args.behavior_train,
        "behavior_val": args.behavior_val,
        "behavior_held_out": args.behavior_held_out,
        "behavior_calibration": args.behavior_calibration,
    }
    source_raw = {
        name: load_jsonl(path) if path else [] for name, path in paths.items()
    }
    raw = {name: list(rows) for name, rows in source_raw.items()}
    cluster_bootstrap = {
        "enabled": False,
        "scheme": "opponent_stratified_match_cluster_v1",
        "seed": int(args.seed),
    }
    if args.cluster_bootstrap:
        try:
            (
                raw["value_train"],
                raw["behavior_train"],
                cluster_bootstrap,
            ) = _stratified_cluster_bootstrap(
                source_raw["value_train"],
                source_raw["behavior_train"],
                seed=args.seed,
            )
        except ValueError as exc:
            raise SystemExit(f"cluster bootstrap failed: {exc}") from exc
    training_row_weighting = {}
    for name in ("value_train", "behavior_train"):
        try:
            raw[name], training_row_weighting[name] = (
                _attach_training_row_weights(
                    raw[name], scheme=args.training_row_weighting
                )
            )
        except ValueError as exc:
            raise SystemExit(f"training row weighting failed: {exc}") from exc
    if args.require_cross_hand_sequence:
        missing = {
            name: sum(row.get("cross_hand_sequence") is None for row in rows)
            for name, rows in raw.items()
        }
        missing = {name: count for name, count in missing.items() if count}
        if missing:
            raise SystemExit(f"missing required cross-hand sequences: {missing}")
    split_rows = {
        "train": raw["value_train"] + raw["behavior_train"],
        "val": raw["value_val"] + raw["behavior_val"],
        "calibration": raw["value_calibration"] + raw["behavior_calibration"],
        "held_out": raw["value_held_out"] + raw["behavior_held_out"],
    }
    split_opponents = _assert_disjoint(split_rows)
    value = {
        split: [build_value_sample(row, max_hist=args.max_hist) for row in raw[f"value_{split}"]]
        for split in ("train", "val", "calibration", "held_out")
    }
    behavior = {}
    for split in ("train", "val", "calibration", "held_out"):
        samples = [
            build_behavior_sample(row, max_hist=args.max_hist)
            for row in raw[f"behavior_{split}"]
        ]
        behavior[split] = [sample for sample in samples if sample is not None]
    if not value["train"] or not behavior["train"]:
        raise SystemExit("training requires both counterfactual value and behavior rows")
    response_counts = np.bincount(
        [sample["response_target"] for sample in behavior["train"]],
        minlength=len(OPPONENT_ACTION_LABELS),
    )
    weighted_response_counts = np.bincount(
        [sample["response_target"] for sample in behavior["train"]],
        weights=[sample["loss_weight"] for sample in behavior["train"]],
        minlength=len(OPPONENT_ACTION_LABELS),
    )
    response_weights = np.zeros(len(OPPONENT_ACTION_LABELS), dtype=np.float32)
    present = weighted_response_counts > 0
    response_weights[present] = np.sqrt(
        weighted_response_counts.sum()
        / (present.sum() * weighted_response_counts[present])
    )
    response_weight_tensor = torch.tensor(
        response_weights, dtype=torch.float32, device=device
    )

    state_dim = len(value["train"][0]["state"])
    profile_dim = len(value["train"][0]["profile"])
    model = OpponentAwareMultiTaskNet(
        state_dim,
        profile_dim,
        gru_hidden=args.gru_hidden,
        hidden=args.hidden,
        latent=args.latent,
        cross_hidden=args.cross_hidden,
        head_hidden=args.head_hidden,
        dropout=args.dropout,
        cross_sequence_hidden=args.cross_sequence_hidden,
        cross_sequence_encoder=args.cross_sequence_encoder,
        cross_transformer_heads=args.cross_transformer_heads,
        cross_moe_experts=args.cross_moe_experts,
    ).to(device)
    optimizer = torch.optim.AdamW(
        model.parameters(), lr=args.lr, weight_decay=args.weight_decay
    )
    clips = {
        "delta_vs_rule": float(args.hand_clip),
        "tail_delta_vs_rule": float(args.tail_clip),
        "match_delta_vs_rule": float(args.match_clip),
    }
    field_weights = {
        "delta_vs_rule": 1.0,
        "tail_delta_vs_rule": 0.5,
        "match_delta_vs_rule": 0.5,
    }
    ranking_stats = _ranking_statistics(
        value["train"],
        field="match_delta_vs_rule",
        margin=args.ranking_margin,
    )
    ranking_positive_weight = torch.tensor(
        ranking_stats["positive_weight"], dtype=torch.float32, device=device
    )
    ranking_action_weights = torch.tensor(
        ranking_stats["per_action_weights"], dtype=torch.float32, device=device
    )
    best_score = float("inf")
    best_epoch = 0
    best_state = None
    epochs_without_improvement = 0
    rng = random.Random(args.seed)
    print(
        f"[multitask] device={device} params={sum(p.numel() for p in model.parameters())} "
        f"value={len(value['train'])}/{len(value['val'])}/{len(value['calibration'])}/{len(value['held_out'])} "
        f"behavior={len(behavior['train'])}/{len(behavior['val'])}/{len(behavior['calibration'])}/{len(behavior['held_out'])}",
        flush=True,
    )
    print(f"[multitask] match_ranking={ranking_stats}", flush=True)
    print(
        "[multitask] cluster_bootstrap="
        + str({
            key: cluster_bootstrap.get(key)
            for key in (
                "enabled", "scheme", "seed", "source_clusters",
                "sampled_draws", "unique_sampled_clusters",
                "effective_value_rows", "effective_behavior_rows",
            )
            if key in cluster_bootstrap
        }),
        flush=True,
    )
    print(
        "[multitask] training_row_weighting="
        + str({
            name: {
                key: report.get(key)
                for key in (
                    "scheme", "rows", "opponents", "clusters",
                    "min_row_weight", "max_row_weight",
                )
            }
            for name, report in training_row_weighting.items()
        }),
        flush=True,
    )

    for epoch in range(1, args.epochs + 1):
        model.train()
        value_order = list(range(len(value["train"])))
        behavior_order = list(range(len(behavior["train"])))
        rng.shuffle(value_order)
        rng.shuffle(behavior_order)
        value_batches = _chunks(value_order, args.batch_size)
        behavior_batches = _chunks(behavior_order, args.batch_size)
        max_behavior_batches = max(
            1, len(value_batches) * max(1, args.max_behavior_batches_per_value)
        )
        behavior_batches = behavior_batches[:max_behavior_batches]
        steps = max(len(value_batches), len(behavior_batches))
        epoch_loss = 0.0
        for step in range(steps):
            losses = []
            if value_batches:
                indices = value_batches[step % len(value_batches)]
                batch = [value["train"][idx] for idx in indices]
                (
                    state, profile, history, lengths, cross,
                    cross_sequence, cross_lengths,
                ) = _context_tensors(
                    batch, max_hist=args.max_hist, device=device
                )
                outputs = model.value(model.encode(
                    state,
                    profile,
                    history,
                    lengths,
                    cross,
                    rule_action=_rule_action_tensor(batch, device),
                    cross_sequence=cross_sequence,
                    cross_lengths=cross_lengths,
                ))
                targets = _value_batch_tensors(batch, device)
                rule_ids = _rule_ids_tensor(batch, device)
                loss_weights = _loss_weight_tensor(batch, device)
                value_loss = torch.zeros((), device=device)
                for field in VALUE_FIELDS:
                    field_loss, _ = _value_loss(
                        outputs[field],
                        targets[field],
                        clip=clips[field],
                        quantile=args.lower_quantile,
                        row_weights=loss_weights,
                    )
                    value_loss = value_loss + field_weights[field] * field_loss
                ranking_loss, _ = _pairwise_ranking_loss(
                    outputs["match_delta_vs_rule"],
                    targets["match_delta_vs_rule"],
                    rule_ids,
                    margin=args.ranking_margin,
                    temperature=args.ranking_temperature,
                    positive_weight=ranking_positive_weight,
                    action_weights=ranking_action_weights,
                    row_weights=loss_weights,
                )
                lower_ranking_loss, _ = _pairwise_ranking_loss(
                    outputs["match_delta_vs_rule"],
                    targets["match_delta_vs_rule"],
                    rule_ids,
                    margin=args.ranking_margin,
                    temperature=args.ranking_temperature,
                    positive_weight=ranking_positive_weight,
                    action_weights=ranking_action_weights,
                    row_weights=loss_weights,
                    head="lower",
                )
                value_loss = (
                    value_loss
                    + args.match_ranking_weight * ranking_loss
                    + args.match_lower_ranking_weight * lower_ranking_loss
                )
                losses.append(value_loss)
            if behavior_batches:
                indices = behavior_batches[step % len(behavior_batches)]
                batch = [behavior["train"][idx] for idx in indices]
                (
                    state, profile, history, lengths, cross,
                    cross_sequence, cross_lengths,
                ) = _context_tensors(
                    batch, max_hist=args.max_hist, device=device
                )
                latent = model.encode(
                    state, profile, history, lengths, cross, response=True,
                    cross_sequence=cross_sequence, cross_lengths=cross_lengths,
                )
                hero = torch.tensor(
                    [sample["hero_action_features"] for sample in batch],
                    dtype=torch.float32,
                    device=device,
                )
                target = torch.tensor(
                    [sample["response_target"] for sample in batch],
                    dtype=torch.long,
                    device=device,
                )
                output = model.response(latent, hero)
                loss_weights = _loss_weight_tensor(batch, device)
                behavior_loss = _weighted_mean(F.cross_entropy(
                    output[:, :len(OPPONENT_ACTION_LABELS)],
                    target,
                    weight=response_weight_tensor,
                    reduction="none",
                ), loss_weights)
                amount_mask = (target == OPPONENT_ACTION_LABELS.index("raise")) | (
                    target == OPPONENT_ACTION_LABELS.index("allin")
                )
                if bool(amount_mask.any()):
                    amount_target = torch.tensor(
                        [sample["response_amount"] for sample in batch],
                        dtype=torch.float32,
                        device=device,
                    )
                    behavior_loss = behavior_loss + 0.25 * _weighted_mean(
                        F.smooth_l1_loss(
                            torch.sigmoid(output[amount_mask, -1]),
                            amount_target[amount_mask],
                            reduction="none",
                        ),
                        loss_weights[amount_mask],
                    )
                losses.append(args.behavior_weight * behavior_loss)
            loss = sum(losses)
            optimizer.zero_grad()
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            epoch_loss += float(loss.item())

        validation = _evaluate(
            model,
            value["val"],
            behavior["val"],
            clips=clips,
            max_hist=args.max_hist,
            batch_size=args.batch_size,
            device=device,
            ranking_margin=args.ranking_margin,
            direction_score_weight=args.direction_score_weight,
            lower_direction_score_weight=args.lower_direction_score_weight,
        )
        score = float(validation["selection_score"])
        improved = score < best_score
        if improved:
            best_score = score
            best_epoch = epoch
            best_state = {
                key: tensor.detach().cpu().clone()
                for key, tensor in model.state_dict().items()
            }
            epochs_without_improvement = 0
        else:
            epochs_without_improvement += 1
        if epoch == 1 or epoch % 5 == 0 or improved:
            print(
                f"[multitask] ep={epoch} loss={epoch_loss/max(1, steps):.5f} "
                f"val_score={score:.5f} response_bal_acc="
                f"{validation['response']['balanced_accuracy']}"
                " match_dir_bal_acc="
                f"{validation['value']['match_delta_vs_rule']['direction_balanced_accuracy']}"
                " lower_match_dir_bal_acc="
                f"{validation['value']['match_delta_vs_rule']['lower_direction_balanced_accuracy']}"
                f"{' *best' if improved else ''}",
                flush=True,
            )
        if epochs_without_improvement >= args.patience:
            break

    if best_state is None:
        raise SystemExit("training produced no validation checkpoint")
    model.load_state_dict(best_state)
    validation = _evaluate(
        model, value["val"], behavior["val"], clips=clips,
        max_hist=args.max_hist, batch_size=args.batch_size, device=device,
        ranking_margin=args.ranking_margin,
        direction_score_weight=args.direction_score_weight,
        lower_direction_score_weight=args.lower_direction_score_weight,
    )
    lower_calibration = _calibrate_lower_bounds(
        model,
        value["calibration"],
        clips=clips,
        quantile=args.lower_quantile,
        min_per_action=args.min_calibration_per_action,
        max_hist=args.max_hist,
        batch_size=args.batch_size,
        device=device,
    ) if value["calibration"] else {
        field: {
            "quantile": float(args.lower_quantile),
            "global_offset": 0.0,
            "offsets": [0.0] * NUM_ACTIONS,
            "samples": 0,
            "per_action_samples": [0] * NUM_ACTIONS,
            "min_per_action": int(args.min_calibration_per_action),
        }
        for field in VALUE_FIELDS
    }
    response_calibration = _calibrate_response_temperature(
        model,
        behavior["calibration"],
        max_hist=args.max_hist,
        batch_size=args.batch_size,
        device=device,
    )
    calibration_evaluation = _evaluate(
        model,
        value["calibration"],
        behavior["calibration"],
        clips=clips,
        max_hist=args.max_hist,
        batch_size=args.batch_size,
        device=device,
        lower_calibration=lower_calibration,
        response_temperature=response_calibration["temperature"],
        ranking_margin=args.ranking_margin,
        direction_score_weight=args.direction_score_weight,
        lower_direction_score_weight=args.lower_direction_score_weight,
    ) if value["calibration"] or behavior["calibration"] else None
    held_out = _evaluate(
        model, value["held_out"], behavior["held_out"], clips=clips,
        max_hist=args.max_hist, batch_size=args.batch_size, device=device,
        lower_calibration=lower_calibration,
        response_temperature=response_calibration["temperature"],
        ranking_margin=args.ranking_margin,
        direction_score_weight=args.direction_score_weight,
        lower_direction_score_weight=args.lower_direction_score_weight,
    ) if value["held_out"] or behavior["held_out"] else None
    manifests = {
        name: _manifest(path, source_raw[name]) for name, path in paths.items()
    }
    payload = {
        "meta": {
            "format": "opp_multitask_gru_v2",
            "labels": list(LABELS),
            "opponent_action_labels": list(OPPONENT_ACTION_LABELS),
            "value_fields": list(VALUE_FIELDS),
            "value_reference": VALUE_REFERENCE,
            "state_dim": state_dim,
            "profile_dim": profile_dim,
            "hist_feat_dim": HIST_FEAT_DIM,
            "cross_hand_dim": CROSS_HAND_DIM,
            "cross_hand_sequence_dim": CROSS_HAND_SEQUENCE_DIM,
            "cross_hand_sequence_schema": CROSS_HAND_SEQUENCE_SCHEMA,
            "hero_action_dim": HERO_ACTION_DIM,
            "rule_action_dim": RULE_ACTION_DIM,
            "response_private_state_masked": list(PRIVATE_STATE_INDICES),
            "response_encoder": "separate_public_v1",
            "lower_calibration": lower_calibration,
            "response_calibration": response_calibration,
            "model": {
                "hidden": args.hidden,
                "latent": args.latent,
                "gru_hidden": args.gru_hidden,
                "cross_hidden": args.cross_hidden,
                "cross_sequence_hidden": model.cross_sequence_hidden,
                "cross_sequence_hidden_requested": args.cross_sequence_hidden,
                "cross_sequence_encoder": args.cross_sequence_encoder,
                "cross_transformer_heads": args.cross_transformer_heads,
                "cross_moe_experts": args.cross_moe_experts,
                "head_hidden": args.head_hidden,
                "dropout": args.dropout,
                "max_hist": args.max_hist,
                "max_cross_hands": MAX_CROSS_HANDS,
                "parameters": sum(parameter.numel() for parameter in model.parameters()),
            },
            "training": {
                "seed": args.seed,
                "epochs_requested": args.epochs,
                "best_epoch": best_epoch,
                "patience": args.patience,
                "batch_size": args.batch_size,
                "learning_rate": args.lr,
                "weight_decay": args.weight_decay,
                "lower_quantile": args.lower_quantile,
                "match_ranking_weight": args.match_ranking_weight,
                "match_lower_ranking_weight": args.match_lower_ranking_weight,
                "ranking_margin": args.ranking_margin,
                "ranking_temperature": args.ranking_temperature,
                "direction_score_weight": args.direction_score_weight,
                "lower_direction_score_weight": (
                    args.lower_direction_score_weight
                ),
                "ranking_statistics": ranking_stats,
                "ranking_reference": VALUE_REFERENCE,
                "cluster_bootstrap_enabled": bool(args.cluster_bootstrap),
                "cluster_bootstrap": cluster_bootstrap,
                "training_row_weighting": args.training_row_weighting,
                "training_row_weighting_report": training_row_weighting,
                "effective_rows": {
                    name: len(rows) for name, rows in raw.items()
                },
                "trainer_sha256": hashlib.sha256(
                    Path(__file__).read_bytes()
                ).hexdigest(),
                "required_cross_hand_sequence": bool(
                    args.require_cross_hand_sequence
                ),
                "clips": clips,
                "device_requested": args.device,
                "device_effective": str(device),
                "torch_version": torch.__version__,
                "numpy_version": np.__version__,
                "split_opponents": split_opponents,
                "response_class_counts": dict(zip(
                    OPPONENT_ACTION_LABELS,
                    [int(value) for value in response_counts],
                )),
                "weighted_response_class_counts": dict(zip(
                    OPPONENT_ACTION_LABELS,
                    [float(value) for value in weighted_response_counts],
                )),
                "response_class_weights": dict(zip(
                    OPPONENT_ACTION_LABELS,
                    [float(value) for value in response_weights],
                )),
                "data": manifests,
            },
            "validation": validation,
            "calibration": calibration_evaluation,
            "held_out": held_out,
        },
        "weights": {
            key: tensor.detach().cpu().tolist()
            for key, tensor in model.state_dict().items()
        },
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    print(
        f"[multitask] wrote={out} best_epoch={best_epoch} "
        f"val_score={validation['selection_score']:.5f}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
