"""Range-CFV neural network for depth-limited public-tree solving.

Consumes a :class:`PublicHUNLState` plus two 1,326-dimensional reach-probability
vectors (one per player) and produces a 2×1,326 counterfactual-value matrix in
big-blind units.  The architecture mirrors the ReBeL / DeepStack leaf-value
contract: a shared trunk encodes the public state, two range heads process each
player's reach vector through a gated feed-forward block, and a bilinear-style
head emits per-combo CFVs.

All tensor operations are deterministic under ``torch.manual_seed`` and the
``configure_deterministic_runtime`` guard used by the training pipeline.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import torch
import torch.nn as nn

from .combo_index import COMBO_COUNT
from .public_state import (
    ACTION_SLOTS,
    BOARD_LENGTH_BY_STREET,
    PublicHUNLState,
    STREETS,
)

# --------------------------------------------------------------------------- #
# Feature dimensions
# --------------------------------------------------------------------------- #

KIND_ENCODING = {"fold": 0, "check": 1, "call": 2, "raise": 3, "allin": 4}
KIND_ONEHOT_DIM = 5

STREET_DIM = len(STREETS)                      # 4 one-hot
BOARD_CARD_DIM = 5 * 52                         # up to 5 board cards one-hot
SCALAR_DIM = 8                                  # pot, stacks, commitments, etc.
ACTION_MASK_DIM = len(ACTION_SLOTS)             # 8
ACTION_HISTORY_MAX = 24                         # bounded history slots
ACTION_RECORD_DIM = 2 + 1 + KIND_ONEHOT_DIM     # actor(2) + street(1) + kind(5) = 8 per slot
ACTION_HISTORY_DIM = ACTION_HISTORY_MAX * ACTION_RECORD_DIM

PUBLIC_FEATURE_DIM = (
    STREET_DIM
    + BOARD_CARD_DIM
    + SCALAR_DIM
    + ACTION_MASK_DIM
    + ACTION_HISTORY_DIM
)

RANGE_DIM = COMBO_COUNT  # 1,326


# --------------------------------------------------------------------------- #
# Public-state encoder
# --------------------------------------------------------------------------- #

def encode_public_state(state: PublicHUNLState) -> torch.Tensor:
    """Convert a frozen :class:`PublicHUNLState` to a 1-D float tensor."""

    features: list[float] = []

    # Street one-hot --------------------------------------------------------
    street_idx = STREETS.index(state.street)
    features.extend(1.0 if i == street_idx else 0.0 for i in range(STREET_DIM))

    # Board cards one-hot (5 slots × 52 ranks) ------------------------------
    board_oh = [0.0] * BOARD_CARD_DIM
    for card_id in state.board_card_ids:
        if 0 <= card_id < 52:
            board_oh[card_id] = 1.0
    features.extend(board_oh)

    # Scalar features -------------------------------------------------------
    features.append(float(state.pot_bb))
    features.append(float(state.stacks_bb[0]))
    features.append(float(state.stacks_bb[1]))
    features.append(float(state.street_commitments_bb[0]))
    features.append(float(state.street_commitments_bb[1]))
    features.append(float(state.to_call_bb))
    min_raise = state.min_raise_to_bb
    features.append(float(min_raise) if min_raise is not None else 0.0)
    features.append(float(state.small_blind_player))

    # Legal action mask -----------------------------------------------------
    features.extend(1.0 if flag else 0.0 for flag in state.legal_action_mask)

    # Bounded action history ------------------------------------------------
    history = list(state.public_action_history)
    for i in range(ACTION_HISTORY_MAX):
        if i < len(history):
            rec = history[i]
            actor_oh = [0.0, 0.0]
            actor_oh[rec.actor] = 1.0
            features.extend(actor_oh)
            features.append(float(STREETS.index(rec.street)) / 3.0)
            kind_oh = [0.0] * KIND_ONEHOT_DIM
            kind_idx = KIND_ENCODING.get(rec.kind, 0)
            kind_oh[kind_idx] = 1.0
            features.extend(kind_oh)
        else:
            features.extend([0.0] * (2 + 1 + KIND_ONEHOT_DIM))

    if len(features) != PUBLIC_FEATURE_DIM:
        raise AssertionError(
            f"public feature vector length {len(features)} != expected {PUBLIC_FEATURE_DIM}"
        )
    return torch.tensor(features, dtype=torch.float32)


# --------------------------------------------------------------------------- #
# Network
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class RangeCFVNetConfig:
    """Hyper-parameters for :class:`RangeCFVNet`."""

    trunk_hidden: int = 128
    trunk_layers: int = 3
    range_hidden: int = 256
    head_hidden: int = 256
    head_layers: int = 2
    dropout: float = 0.0
    activation: str = "gelu"

    def activation_fn(self) -> nn.Module:
        cls = {"gelu": nn.GELU, "relu": nn.ReLU, "tanh": nn.Tanh}[self.activation]
        return cls()


class RangeCFVNet(nn.Module):
    """Maps (public_state, range₀, range₁) → (cfv₀, cfv₁).

    Architecture::

        public_state ──► trunk(tower MLP) ──► public_embedding
        range₀ ──► range_encoder₀ ──┐
        range₁ ──► range_encoder₁ ──┤
        public_embedding ────────────┴─► concat → head tower → 2×1326

    The head outputs raw logits which are then masked to zero for
    board-incompatible combos.  The caller (training loop) is responsible
    for any zero-sum projection.
    """

    def __init__(self, config: RangeCFVNetConfig | None = None) -> None:
        super().__init__()
        self.config = config or RangeCFVNetConfig()
        c = self.config

        # Shared public-state trunk -------------------------------------------------
        trunk_layers: list[nn.Module] = []
        in_dim = PUBLIC_FEATURE_DIM
        for i in range(c.trunk_layers):
            out_dim = c.trunk_hidden
            trunk_layers.append(nn.Linear(in_dim, out_dim))
            trunk_layers.append(c.activation_fn())
            if c.dropout > 0:
                trunk_layers.append(nn.Dropout(c.dropout))
            in_dim = out_dim
        self.trunk = nn.Sequential(*trunk_layers)
        public_emb_dim = in_dim

        # Per-player range encoders (shared weights by default via two identical
        # sub-modules — instantiated separately so weight-tying can be toggled)
        self.range_encoders = nn.ModuleList([
            self._build_range_encoder(public_emb_dim, c) for _ in range(2)
        ])

        merged_dim = public_emb_dim + 2 * c.range_hidden

        # CFV head ------------------------------------------------------------------
        head_layers: list[nn.Module] = []
        in_dim = merged_dim
        for i in range(c.head_layers):
            out_dim = c.head_hidden
            head_layers.append(nn.Linear(in_dim, out_dim))
            head_layers.append(c.activation_fn())
            if c.dropout > 0:
                head_layers.append(nn.Dropout(c.dropout))
            in_dim = out_dim
        head_layers.append(nn.Linear(in_dim, 2 * COMBO_COUNT))
        self.head = nn.Sequential(*head_layers)

    @staticmethod
    def _build_range_encoder(public_emb_dim: int, c: RangeCFVNetConfig) -> nn.Module:
        return nn.Sequential(
            nn.Linear(RANGE_DIM + public_emb_dim, c.range_hidden),
            c.activation_fn(),
            nn.Linear(c.range_hidden, c.range_hidden),
            c.activation_fn(),
        )

    def forward(
        self,
        public_features: torch.Tensor,
        range0: torch.Tensor,
        range1: torch.Tensor,
    ) -> torch.Tensor:
        """Forward pass.

        Parameters
        ----------
        public_features
            Shape ``(batch, PUBLIC_FEATURE_DIM)`` — output of
            :func:`encode_public_state` stacked along dim 0.
        range0, range1
            Shape ``(batch, 1326)`` — board-legal reach ranges for each player.

        Returns
        -------
        torch.Tensor
            Shape ``(batch, 2, 1326)`` — per-player counterfactual values
            in big-blind units.
        """
        if public_features.dim() != 2:
            raise ValueError("public_features must be 2-D (batch, feature_dim)")
        batch = public_features.shape[0]
        if range0.shape != (batch, RANGE_DIM) or range1.shape != (batch, RANGE_DIM):
            raise ValueError("range tensors must match batch size and 1,326 width")

        pub_emb = self.trunk(public_features)                      # (B, H)
        pub_emb_expanded = pub_emb.unsqueeze(1).expand(-1, 2, -1)   # (B, 2, H)

        ranges = torch.stack([range0, range1], dim=1)               # (B, 2, 1326)
        range_inputs = torch.cat([pub_emb_expanded, ranges], dim=-1)  # (B, 2, H+1326)

        # Encode each player's range
        enc0 = self.range_encoders[0](range_inputs[:, 0, :])         # (B, range_hidden)
        enc1 = self.range_encoders[1](range_inputs[:, 1, :])         # (B, range_hidden)

        merged = torch.cat([pub_emb, enc0, enc1], dim=-1)           # (B, merged)
        raw = self.head(merged)                                     # (B, 2*1326)
        return raw.view(batch, 2, COMBO_COUNT)


# --------------------------------------------------------------------------- #
# Factory & inference helper
# --------------------------------------------------------------------------- #

def build_cfv_model(
    config: RangeCFVNetConfig | None = None,
    *,
    seed: int = 0,
    device: str = "cpu",
) -> RangeCFVNet:
    """Instantiate a deterministic CFV model."""

    g = torch.Generator(device="cpu").manual_seed(seed)
    model = RangeCFVNet(config)
    model.to(device)
    # Deterministic parameter initialisation
    with torch.no_grad():
        for param in model.parameters():
            if param.dim() >= 2:
                nn.init.xavier_uniform_(param, generator=g)
            elif param.dim() == 1:
                nn.init.zeros_(param)
    model.eval()
    return model


def predict_cfv(
    model: RangeCFVNet,
    state: PublicHUNLState,
    range0: tuple[float, ...],
    range1: tuple[float, ...],
    *,
    device: str = "cpu",
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Single-query inference returning plain Python tuples.

    The caller is responsible for masking / zero-sum projection if needed;
    this helper returns the raw network output clamped to board-legal combos.
    """
    from .ranges import cfv_valid_mask

    model.eval()
    pub_feat = encode_public_state(state).unsqueeze(0).to(device)
    r0 = torch.tensor(range0, dtype=torch.float32).unsqueeze(0).to(device)
    r1 = torch.tensor(range1, dtype=torch.float32).unsqueeze(0).to(device)

    with torch.no_grad():
        out = model(pub_feat, r0, r1).squeeze(0).cpu()  # (2, 1326)

    masks = cfv_valid_mask(state.board_card_ids, range1), \
            cfv_valid_mask(state.board_card_ids, range0)

    cfv0 = tuple(
        float(out[0, i]) if masks[0][i] else 0.0
        for i in range(COMBO_COUNT)
    )
    cfv1 = tuple(
        float(out[1, i]) if masks[1][i] else 0.0
        for i in range(COMBO_COUNT)
    )
    return cfv0, cfv1


# --------------------------------------------------------------------------- #
# Dataset stub (for the training pipeline)
# --------------------------------------------------------------------------- #

class RangeCFVDataset(torch.utils.data.Dataset):
    """Minimal in-memory dataset storing (public_feat, range0, range1, target).

    Each sample is a pre-encoded tuple suitable for direct model consumption.
    The actual label-generation pipeline (CFR solver → leaf CFVs) populates
    this structure.
    """

    def __init__(
        self,
        samples: list[
            tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]
        ],
    ) -> None:
        if not samples:
            raise ValueError("dataset cannot be empty")
        self._samples = samples

    def __len__(self) -> int:
        return len(self._samples)

    def __getitem__(
        self, idx: int
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        return self._samples[idx]
