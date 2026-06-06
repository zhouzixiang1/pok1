"""Transformer Q-Network (DanLM-style TinyLM).

Architecture:
    token_sequence → Token Embedding → Causal Transformer → context vector →
    hand_features → Hand MLP → hand_context →
    [context; hand_context] → Q-Value Head → Q(s,a)

Key differences from MLP Q-Network:
- Input is a tokenized game history sequence, not a flat feature vector
- Causal attention captures temporal dependencies in game play
- Hand features (card counts, action space) processed separately
- Auxiliary Next-Token Prediction task (optional, for representation learning)
"""

from __future__ import annotations

import math

import torch
import torch.nn as nn


class TokenEmbedding(nn.Module):
    """Token embedding with positional encoding."""

    def __init__(self, vocab_size: int, d_model: int, max_seq_len: int):
        super().__init__()
        self.token_emb = nn.Embedding(vocab_size, d_model, padding_idx=0)
        self.pos_emb = nn.Embedding(max_seq_len, d_model)
        self.d_model = d_model
        self.max_seq_len = max_seq_len

    def forward(self, tokens: torch.Tensor) -> torch.Tensor:
        """tokens: (batch, seq_len) int64 tensor."""
        seq_len = tokens.shape[1]
        positions = torch.arange(seq_len, device=tokens.device).unsqueeze(0)
        return self.token_emb(tokens) + self.pos_emb(positions)


class CausalSelfAttention(nn.Module):
    """Multi-head causal self-attention."""

    def __init__(self, d_model: int, n_heads: int, dropout: float = 0.1):
        super().__init__()
        assert d_model % n_heads == 0
        self.d_k = d_model // n_heads
        self.n_heads = n_heads

        self.q_proj = nn.Linear(d_model, d_model)
        self.k_proj = nn.Linear(d_model, d_model)
        self.v_proj = nn.Linear(d_model, d_model)
        self.out_proj = nn.Linear(d_model, d_model)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x: torch.Tensor, mask: torch.Tensor | None = None) -> torch.Tensor:
        B, T, C = x.shape

        q = self.q_proj(x).view(B, T, self.n_heads, self.d_k).transpose(1, 2)
        k = self.k_proj(x).view(B, T, self.n_heads, self.d_k).transpose(1, 2)
        v = self.v_proj(x).view(B, T, self.n_heads, self.d_k).transpose(1, 2)

        attn = torch.matmul(q, k.transpose(-2, -1)) / math.sqrt(self.d_k)

        # Causal mask
        if mask is not None:
            attn = attn.masked_fill(mask.unsqueeze(1).unsqueeze(2) == 0, float('-inf'))
        else:
            causal_mask = torch.triu(torch.ones(T, T, device=x.device), diagonal=1).bool()
            attn = attn.masked_fill(causal_mask, float('-inf'))

        attn = torch.softmax(attn, dim=-1)
        attn = self.dropout(attn)

        out = torch.matmul(attn, v)
        out = out.transpose(1, 2).contiguous().view(B, T, C)
        return self.out_proj(out)


class TransformerBlock(nn.Module):
    """Pre-norm Transformer block."""

    def __init__(self, d_model: int, n_heads: int, d_ff: int, dropout: float = 0.1):
        super().__init__()
        self.ln1 = nn.LayerNorm(d_model)
        self.attn = CausalSelfAttention(d_model, n_heads, dropout)
        self.ln2 = nn.LayerNorm(d_model)
        self.ff = nn.Sequential(
            nn.Linear(d_model, d_ff),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(d_ff, d_model),
            nn.Dropout(dropout),
        )

    def forward(self, x: torch.Tensor, mask: torch.Tensor | None = None) -> torch.Tensor:
        x = x + self.attn(self.ln1(x), mask)
        x = x + self.ff(self.ln2(x))
        return x


class HandMLP(nn.Module):
    """Process hand features (card vector + player features) into context."""

    def __init__(self, input_dim: int, d_model: int):
        super().__init__()
        self.mlp = nn.Sequential(
            nn.Linear(input_dim, d_model * 2),
            nn.ReLU(),
            nn.Linear(d_model * 2, d_model),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.mlp(x)


class TransformerQNetwork(nn.Module):
    """Transformer-based Q-Network (DanLM style).

    Two input streams:
    1. Token sequence → TinyLM encoder → context vector
    2. Hand features (card_vec + player_features) → HandMLP → hand context

    Combined → Q-Value Head → Q(s,a)
    """

    def __init__(
        self,
        vocab_size: int = 80,
        max_seq_len: int = 128,
        d_model: int = 128,
        n_heads: int = 4,
        n_layers: int = 4,
        d_ff: int = 512,
        hand_input_dim: int = 132,
        num_actions: int = 11,
        dropout: float = 0.1,
        dueling: bool = True,
    ):
        super().__init__()
        self.d_model = d_model
        self.num_actions = num_actions
        self.dueling = dueling

        # Token embedding + positional encoding
        self.embedding = TokenEmbedding(vocab_size, d_model, max_seq_len)

        # Transformer encoder
        self.transformer_blocks = nn.ModuleList([
            TransformerBlock(d_model, n_heads, d_ff, dropout)
            for _ in range(n_layers)
        ])
        self.ln_f = nn.LayerNorm(d_model)

        # Hand feature MLP
        self.hand_mlp = HandMLP(hand_input_dim, d_model)

        # Q-Value head (takes [transformer_context; hand_context])
        combined_dim = d_model * 2
        if dueling:
            self.value_head = nn.Sequential(
                nn.Linear(combined_dim, 256),
                nn.ReLU(),
                nn.Linear(256, 1),
            )
            self.advantage_head = nn.Sequential(
                nn.Linear(combined_dim, 256),
                nn.ReLU(),
                nn.Linear(256, num_actions),
            )
        else:
            self.q_head = nn.Sequential(
                nn.Linear(combined_dim, 256),
                nn.ReLU(),
                nn.Linear(256, num_actions),
            )

        # Next-token prediction head (auxiliary task)
        self.ntp_head = nn.Linear(d_model, vocab_size)

        self._init_weights()

    def _init_weights(self):
        for module in self.modules():
            if isinstance(module, nn.Linear):
                nn.init.kaiming_normal_(module.weight, nonlinearity='relu')
                if module.bias is not None:
                    nn.init.zeros_(module.bias)
            elif isinstance(module, nn.Embedding):
                nn.init.normal_(module.weight, mean=0.0, std=0.02)

    def forward(
        self,
        tokens: torch.Tensor,
        hand_features: torch.Tensor,
        legal_mask: torch.Tensor | None = None,
        token_mask: torch.Tensor | None = None,
    ) -> torch.Tensor:
        """Forward pass.

        Args:
            tokens: (batch, seq_len) tokenized game history
            hand_features: (batch, hand_input_dim) flat card+player features
            legal_mask: (batch, num_actions) legal action mask
            token_mask: (batch, seq_len) padding mask (1=valid, 0=pad)

        Returns:
            (batch, num_actions) Q-values
        """
        # Token encoding
        x = self.embedding(tokens)  # (batch, seq_len, d_model)

        # Transformer
        for block in self.transformer_blocks:
            x = block(x, token_mask)

        x = self.ln_f(x)

        # Pool: use last non-padded token or mean
        if token_mask is not None:
            # Weighted mean over valid tokens
            mask_expanded = token_mask.unsqueeze(-1).float()
            context = (x * mask_expanded).sum(dim=1) / mask_expanded.sum(dim=1).clamp(min=1)
        else:
            context = x.mean(dim=1)  # (batch, d_model)

        # Hand features
        hand_context = self.hand_mlp(hand_features)  # (batch, d_model)

        # Combine
        combined = torch.cat([context, hand_context], dim=-1)  # (batch, 2*d_model)

        # Q-values
        if self.dueling:
            value = self.value_head(combined)
            advantage = self.advantage_head(combined)
            q_values = value + advantage - advantage.mean(dim=-1, keepdim=True)
        else:
            q_values = self.q_head(combined)

        if legal_mask is not None:
            q_values = q_values.masked_fill(legal_mask == 0, float('-inf'))

        return q_values

    def forward_with_ntp(
        self,
        tokens: torch.Tensor,
        hand_features: torch.Tensor,
        legal_mask: torch.Tensor | None = None,
        token_mask: torch.Tensor | None = None,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """Forward pass with auxiliary next-token prediction.

        Returns:
            (q_values, ntp_logits)
        """
        # Token encoding
        x = self.embedding(tokens)

        for block in self.transformer_blocks:
            x = block(x, token_mask)

        x = self.ln_f(x)

        # NTP logits from hidden states
        ntp_logits = self.ntp_head(x)  # (batch, seq_len, vocab_size)

        # Pool for Q-value
        if token_mask is not None:
            mask_expanded = token_mask.unsqueeze(-1).float()
            context = (x * mask_expanded).sum(dim=1) / mask_expanded.sum(dim=1).clamp(min=1)
        else:
            context = x.mean(dim=1)

        hand_context = self.hand_mlp(hand_features)
        combined = torch.cat([context, hand_context], dim=-1)

        if self.dueling:
            value = self.value_head(combined)
            advantage = self.advantage_head(combined)
            q_values = value + advantage - advantage.mean(dim=-1, keepdim=True)
        else:
            q_values = self.q_head(combined)

        if legal_mask is not None:
            q_values = q_values.masked_fill(legal_mask == 0, float('-inf'))

        return q_values, ntp_logits

    def get_action(
        self,
        tokens: torch.Tensor,
        hand_features: torch.Tensor,
        legal_mask: torch.Tensor,
        token_mask: torch.Tensor | None = None,
        epsilon: float = 0.0,
    ) -> torch.Tensor:
        """Select action using epsilon-greedy."""
        batch_size = tokens.shape[0]
        q_values = self.forward(tokens, hand_features, legal_mask, token_mask)

        greedy_actions = q_values.argmax(dim=-1)

        if epsilon > 0:
            random_actions = torch.zeros_like(greedy_actions)
            for i in range(batch_size):
                legal_indices = torch.where(legal_mask[i] > 0)[0]
                if len(legal_indices) > 0:
                    rand_idx = torch.randint(len(legal_indices), (1,), device=tokens.device)
                    random_actions[i] = legal_indices[rand_idx]
                else:
                    random_actions[i] = 0

            explore_mask = torch.rand(batch_size, device=tokens.device) < epsilon
            actions = torch.where(explore_mask, random_actions, greedy_actions)
        else:
            actions = greedy_actions

        return actions
