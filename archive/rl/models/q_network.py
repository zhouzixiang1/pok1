"""MLP Q-Network (DanZero-style).

Architecture:
    flat_obs (132-dim) → [512, 1024, 512, 1024, 512] → Q-values (11-dim)

Supports:
- Dueling DQN: V(s) + A(s,a) decomposition
- Double DQN: separate target network
"""

from __future__ import annotations

import torch
import torch.nn as nn


class MLPQNetwork(nn.Module):
    """MLP Q-Network with optional Dueling architecture."""

    def __init__(
        self,
        input_dim: int = 132,
        num_actions: int = 11,
        hidden_sizes: tuple[int, ...] = (512, 1024, 512, 1024, 512),
        dueling: bool = True,
        dropout: float = 0.0,
    ):
        super().__init__()
        self.input_dim = input_dim
        self.num_actions = num_actions
        self.dueling = dueling

        # Build shared backbone
        layers = []
        prev_dim = input_dim
        for h in hidden_sizes:
            layers.append(nn.Linear(prev_dim, h))
            layers.append(nn.ReLU())
            if dropout > 0:
                layers.append(nn.Dropout(dropout))
            prev_dim = h
        self.backbone = nn.Sequential(*layers)

        if dueling:
            # Value stream
            self.value_head = nn.Sequential(
                nn.Linear(prev_dim, 256),
                nn.ReLU(),
                nn.Linear(256, 1),
            )
            # Advantage stream
            self.advantage_head = nn.Sequential(
                nn.Linear(prev_dim, 256),
                nn.ReLU(),
                nn.Linear(256, num_actions),
            )
        else:
            self.q_head = nn.Sequential(
                nn.Linear(prev_dim, 256),
                nn.ReLU(),
                nn.Linear(256, num_actions),
            )

        self._init_weights()

    def _init_weights(self):
        for module in self.modules():
            if isinstance(module, nn.Linear):
                nn.init.kaiming_normal_(module.weight, nonlinearity='relu')
                if module.bias is not None:
                    nn.init.zeros_(module.bias)

    def forward(self, obs: torch.Tensor, legal_mask: torch.Tensor | None = None) -> torch.Tensor:
        """Forward pass.

        Args:
            obs: (batch, input_dim) flat observation
            legal_mask: (batch, num_actions) legal action mask (1=legal, 0=illegal)

        Returns:
            (batch, num_actions) Q-values. Illegal actions get -inf.
        """
        features = self.backbone(obs)

        if self.dueling:
            value = self.value_head(features)       # (batch, 1)
            advantage = self.advantage_head(features) # (batch, num_actions)
            q_values = value + advantage - advantage.mean(dim=-1, keepdim=True)
        else:
            q_values = self.q_head(features)

        # Mask illegal actions
        if legal_mask is not None:
            q_values = q_values.masked_fill(legal_mask == 0, float('-inf'))

        return q_values

    def get_q_values(self, obs: torch.Tensor, legal_mask: torch.Tensor | None = None) -> torch.Tensor:
        """Get Q-values (alias for forward)."""
        return self.forward(obs, legal_mask)

    def get_action(self, obs: torch.Tensor, legal_mask: torch.Tensor, epsilon: float = 0.0) -> torch.Tensor:
        """Select action using epsilon-greedy.

        Args:
            obs: (batch, input_dim)
            legal_mask: (batch, num_actions)
            epsilon: exploration rate

        Returns:
            (batch,) selected action indices
        """
        batch_size = obs.shape[0]
        q_values = self.forward(obs, legal_mask)

        # Epsilon-greedy
        greedy_actions = q_values.argmax(dim=-1)

        if epsilon > 0:
            # Random exploration among legal actions
            random_actions = torch.zeros_like(greedy_actions)
            for i in range(batch_size):
                legal_indices = torch.where(legal_mask[i] > 0)[0]
                if len(legal_indices) > 0:
                    rand_idx = torch.randint(len(legal_indices), (1,), device=obs.device)
                    random_actions[i] = legal_indices[rand_idx]
                else:
                    random_actions[i] = 0

            explore_mask = torch.rand(batch_size, device=obs.device) < epsilon
            actions = torch.where(explore_mask, random_actions, greedy_actions)
        else:
            actions = greedy_actions

        return actions
