"""Replay buffer for DQN training.

Supports:
- Uniform sampling
- Prioritized experience replay (optional)
- Multiple weight-version diversity (from DanLM)
"""

from __future__ import annotations

import numpy as np


class ReplayBuffer:
    """Circular replay buffer with optional PER."""

    def __init__(
        self,
        capacity: int = 100_000,
        obs_dim: int = 132,
        num_actions: int = 11,
        use_per: bool = False,
        alpha: float = 0.6,
        beta: float = 0.4,
    ):
        self.capacity = capacity
        self.obs_dim = obs_dim
        self.num_actions = num_actions
        self.use_per = use_per
        self.alpha = alpha
        self.beta = beta

        # Pre-allocate arrays
        self.obs = np.zeros((capacity, obs_dim), dtype=np.float32)
        self.actions = np.zeros(capacity, dtype=np.int64)
        self.rewards = np.zeros(capacity, dtype=np.float32)
        self.next_obs = np.zeros((capacity, obs_dim), dtype=np.float32)
        self.dones = np.zeros(capacity, dtype=np.float32)
        self.legal_masks = np.zeros((capacity, num_actions), dtype=np.float32)
        self.next_legal_masks = np.zeros((capacity, num_actions), dtype=np.float32)

        # For token-based observations (Transformer)
        self.tokens = np.zeros((capacity, 128), dtype=np.int64)
        self.next_tokens = np.zeros((capacity, 128), dtype=np.int64)

        self.pos = 0
        self.size = 0

        # PER priorities
        if use_per:
            self.priorities = np.zeros(capacity, dtype=np.float32)
            self.max_priority = 1.0

    def push(
        self,
        obs: np.ndarray,
        action: int,
        reward: float,
        next_obs: np.ndarray,
        done: bool,
        legal_mask: np.ndarray,
        next_legal_mask: np.ndarray,
        tokens: np.ndarray | None = None,
        next_tokens: np.ndarray | None = None,
    ):
        """Add one transition to buffer."""
        idx = self.pos

        self.obs[idx] = obs
        self.actions[idx] = action
        self.rewards[idx] = reward
        self.next_obs[idx] = next_obs
        self.dones[idx] = done
        self.legal_masks[idx] = legal_mask
        self.next_legal_masks[idx] = next_legal_mask

        if tokens is not None:
            self.tokens[idx] = tokens
        if next_tokens is not None:
            self.next_tokens[idx] = next_tokens

        if self.use_per:
            self.priorities[idx] = self.max_priority

        self.pos = (self.pos + 1) % self.capacity
        self.size = min(self.size + 1, self.capacity)

    def sample(self, batch_size: int) -> dict:
        """Sample a batch of transitions."""
        if self.use_per:
            return self._sample_per(batch_size)
        return self._sample_uniform(batch_size)

    def _sample_uniform(self, batch_size: int) -> dict:
        indices = np.random.randint(0, self.size, size=batch_size)
        return self._get_batch(indices)

    def _sample_per(self, batch_size: int) -> dict:
        priorities = self.priorities[:self.size] ** self.alpha
        probs = priorities / priorities.sum()
        indices = np.random.choice(self.size, size=batch_size, p=probs, replace=False)

        # Importance sampling weights
        total = self.size
        weights = (total * probs[indices]) ** (-self.beta)
        weights = weights / weights.max()

        batch = self._get_batch(indices)
        batch["weights"] = weights.astype(np.float32)
        batch["indices"] = indices
        return batch

    def _get_batch(self, indices: np.ndarray) -> dict:
        return {
            "obs": self.obs[indices],
            "actions": self.actions[indices],
            "rewards": self.rewards[indices],
            "next_obs": self.next_obs[indices],
            "dones": self.dones[indices],
            "legal_masks": self.legal_masks[indices],
            "next_legal_masks": self.next_legal_masks[indices],
            "tokens": self.tokens[indices],
            "next_tokens": self.next_tokens[indices],
        }

    def update_priorities(self, indices: np.ndarray, priorities: np.ndarray):
        """Update PER priorities."""
        if self.use_per:
            self.priorities[indices] = priorities + 1e-6
            self.max_priority = max(self.max_priority, priorities.max())

    def __len__(self) -> int:
        return self.size

    def is_ready(self, batch_size: int) -> bool:
        return self.size >= batch_size
