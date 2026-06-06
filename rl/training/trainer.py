"""DMC training loop for Hold'em RL.

Architecture inspired by DanLM:
- Actor processes play games and collect (s, a, r, s') transitions
- Learner samples from replay buffer and trains Q-network
- Cycle-based: collect N/k samples → train S steps → sync

Actor parallelism via ProcessPoolExecutor:
- Each actor runs in a separate CPU process
- Model weights serialized via state_dict → rebuild in subprocess
- GPU reserved exclusively for Learner
"""

from __future__ import annotations

import logging
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from rl.core.config import HoldemRLConfig
from rl.core.holdem_env import HoldemEnv, NUM_ACTIONS, OBS_DIM
from rl.models.q_network import MLPQNetwork
from rl.models.transformer import TransformerQNetwork
from rl.training.replay_buffer import ReplayBuffer

log = logging.getLogger("holdem_rl.trainer")


# ---------------------------------------------------------------------------
# Model builders (shared between main process and workers)
# ---------------------------------------------------------------------------

def build_model(config: HoldemRLConfig) -> nn.Module:
    """Build Q-network based on config."""
    if config.architecture == "transformer":
        return TransformerQNetwork(
            vocab_size=config.vocab_size,
            max_seq_len=config.max_seq_len,
            d_model=config.d_model,
            n_heads=config.n_heads,
            n_layers=config.n_layers,
            d_ff=config.d_ff,
            hand_input_dim=config.input_dim,
            num_actions=NUM_ACTIONS,
            dueling=config.dueling,
        )
    else:
        return MLPQNetwork(
            input_dim=config.input_dim,
            num_actions=NUM_ACTIONS,
            hidden_sizes=config.hidden_sizes,
            dueling=config.dueling,
            dropout=config.dropout,
        )


def build_target_model(model: nn.Module) -> nn.Module:
    """Create a copy of the model for target network."""
    import copy
    return copy.deepcopy(model)


# ---------------------------------------------------------------------------
# Worker function — runs in subprocess, must be top-level for pickle
# ---------------------------------------------------------------------------

def _worker_collect(
    actor_id: int,
    num_hands: int,
    epsilon: float,
    state_dict: dict,
    config_dict: dict,
) -> list[dict]:
    """Collect transitions in a subprocess.

    Rebuilds model from state_dict, runs num_hands self-play hands on CPU.
    Returns list of transition dicts with numpy arrays.
    """
    config = HoldemRLConfig.from_dict(config_dict)

    # Rebuild model on CPU in subprocess
    model = build_model(config)
    model.load_state_dict(state_dict)
    model.eval()

    env = HoldemEnv(seed=actor_id * 1000)
    rng = np.random.default_rng(actor_id * 42)

    transitions = []

    for _ in range(num_hands):
        obs, info = env.reset()
        done = False

        if env._hand_over:
            continue

        while not done:
            flat_obs = env.get_flat_obs(obs)
            legal_mask = obs["legal_actions"]

            if rng.random() < epsilon:
                legal_indices = np.where(legal_mask > 0)[0]
                action = int(rng.choice(legal_indices)) if len(legal_indices) > 0 else 0
            else:
                with torch.no_grad():
                    obs_tensor = torch.FloatTensor(flat_obs).unsqueeze(0)
                    mask_tensor = torch.FloatTensor(legal_mask).unsqueeze(0)
                    q_values = model(obs_tensor, mask_tensor)
                    action = q_values.argmax(dim=-1).item()

            next_obs, reward, terminated, truncated, info = env.step(action)
            next_flat_obs = env.get_flat_obs(next_obs)
            next_legal = next_obs["legal_actions"]

            transitions.append({
                "obs": flat_obs,
                "action": action,
                "reward": float(np.clip(reward * config.reward_scale,
                                        -config.reward_clip, config.reward_clip)),
                "next_obs": next_flat_obs,
                "done": bool(terminated or truncated),
                "legal_mask": legal_mask,
                "next_legal_mask": next_legal,
            })

            obs = next_obs
            done = terminated or truncated

    return transitions


# ---------------------------------------------------------------------------
# Single-process Actor (for num_actors=1 or debugging)
# ---------------------------------------------------------------------------

class DMCActor:
    """In-process actor that collects transitions without spawning processes."""

    def __init__(self, model: nn.Module, config: HoldemRLConfig, actor_id: int = 0):
        self.model = model
        self.config = config
        self.actor_id = actor_id
        self.env = HoldemEnv(seed=actor_id * 1000)
        self.rng = np.random.default_rng(actor_id * 42)

    def collect(self, num_hands: int, epsilon: float) -> list[dict]:
        self.model.eval()
        transitions = []

        for _ in range(num_hands):
            obs, info = self.env.reset()
            done = False

            if self.env._hand_over:
                continue

            while not done:
                flat_obs = self.env.get_flat_obs(obs)
                legal_mask = obs["legal_actions"]

                if self.rng.random() < epsilon:
                    legal_indices = np.where(legal_mask > 0)[0]
                    action = int(self.rng.choice(legal_indices)) if len(legal_indices) > 0 else 0
                else:
                    with torch.no_grad():
                        obs_tensor = torch.FloatTensor(flat_obs).unsqueeze(0)
                        mask_tensor = torch.FloatTensor(legal_mask).unsqueeze(0)
                        q_values = self.model(obs_tensor, mask_tensor)
                        action = q_values.argmax(dim=-1).item()

                next_obs, reward, terminated, truncated, info = self.env.step(action)
                next_flat_obs = self.env.get_flat_obs(next_obs)
                next_legal = next_obs["legal_actions"]

                transitions.append({
                    "obs": flat_obs,
                    "action": action,
                    "reward": float(np.clip(reward * self.config.reward_scale,
                                            -self.config.reward_clip, self.config.reward_clip)),
                    "next_obs": next_flat_obs,
                    "done": bool(terminated or truncated),
                    "legal_mask": legal_mask,
                    "next_legal_mask": next_legal,
                })

                obs = next_obs
                done = terminated or truncated

        return transitions


# ---------------------------------------------------------------------------
# DMC Trainer
# ---------------------------------------------------------------------------

class DMCTrainer:
    """DMC training loop coordinator.

    Cycle-based training:
    1. Actors collect N/k transitions (parallel via ProcessPoolExecutor)
    2. Learner trains S gradient steps on GPU
    3. Sync target network

    When num_actors=1, uses in-process DMCActor (no subprocess overhead).
    When num_actors>1, spawns subprocess workers for true parallelism.
    """

    def __init__(self, config: HoldemRLConfig):
        self.config = config
        self.device = torch.device(config.device if torch.cuda.is_available() else "cpu")

        # Build model
        self.model = build_model(config).to(self.device)
        self.target_model = build_target_model(self.model).to(self.device)
        self.target_model.load_state_dict(self.model.state_dict())

        # Optimizer
        self.optimizer = torch.optim.Adam(
            self.model.parameters(),
            lr=config.lr,
            weight_decay=config.weight_decay,
        )

        # Replay buffer
        self.buffer = ReplayBuffer(
            capacity=config.replay_buffer_size,
            obs_dim=config.input_dim,
            num_actions=NUM_ACTIONS,
        )

        # Multiprocessing pool (lazy init)
        self._pool: ProcessPoolExecutor | None = None

        # Training state
        self.total_steps = 0
        self.cycle = 0
        self.best_eval_reward = float('-inf')
        self._last_loss = 0.0
        self._last_collect_time = 0.0

        log.info(f"DMCTrainer initialized: arch={config.architecture}, "
                 f"device={self.device}, buffer={config.replay_buffer_size}, "
                 f"actors={config.num_actors}")

    def _get_pool(self) -> ProcessPoolExecutor:
        """Lazy-init the process pool."""
        if self._pool is None:
            n = self.config.num_actors
            log.info(f"Starting ProcessPoolExecutor with {n} workers")
            self._pool = ProcessPoolExecutor(max_workers=n)
        return self._pool

    def train_cycle(self):
        """Execute one training cycle."""
        config = self.config
        epsilon = self._get_epsilon()
        hands_per_actor = config.budget_per_cycle // max(config.num_actors, 1)

        # Phase 1: Collect data
        t0 = time.time()

        if config.num_actors <= 1:
            # Single process — no subprocess overhead
            actor = DMCActor(self.model, config, actor_id=0)
            all_transitions = actor.collect(hands_per_actor, epsilon)
        else:
            # Multi-process parallel collection
            pool = self._get_pool()
            state_dict = {k: v.cpu() for k, v in self.model.state_dict().items()}
            config_dict = config.to_dict()

            futures = {}
            for actor_id in range(config.num_actors):
                future = pool.submit(
                    _worker_collect,
                    actor_id,
                    hands_per_actor,
                    epsilon,
                    state_dict,
                    config_dict,
                )
                futures[future] = actor_id

            all_transitions = []
            for future in as_completed(futures):
                actor_id = futures[future]
                try:
                    transitions = future.result()
                    all_transitions.extend(transitions)
                except Exception as e:
                    log.warning(f"Actor {actor_id} failed: {e}")

        self._last_collect_time = time.time() - t0
        log.debug(f"Collected {len(all_transitions)} transitions in {self._last_collect_time:.1f}s")

        # Push to buffer
        for t in all_transitions:
            self.buffer.push(
                obs=t["obs"],
                action=t["action"],
                reward=t["reward"],
                next_obs=t["next_obs"],
                done=t["done"],
                legal_mask=t["legal_mask"],
                next_legal_mask=t["next_legal_mask"],
            )

        # Phase 2: Train
        if self.buffer.is_ready(config.batch_size):
            for _ in range(config.train_steps_per_cycle):
                self._train_step()

        self.cycle += 1

    def _train_step(self):
        """One gradient step."""
        config = self.config
        self.model.train()

        batch = self.buffer.sample(config.batch_size)

        obs = torch.FloatTensor(batch["obs"]).to(self.device)
        actions = torch.LongTensor(batch["actions"]).to(self.device).unsqueeze(1)
        rewards = torch.FloatTensor(batch["rewards"]).to(self.device)
        next_obs = torch.FloatTensor(batch["next_obs"]).to(self.device)
        dones = torch.FloatTensor(batch["dones"]).to(self.device)
        legal_masks = torch.FloatTensor(batch["legal_masks"]).to(self.device)
        next_legal_masks = torch.FloatTensor(batch["next_legal_masks"]).to(self.device)

        # Current Q-values
        q_values = self.model(obs, legal_masks)
        q_values = q_values.gather(1, actions).squeeze(1)

        # Target Q-values
        with torch.no_grad():
            if config.double_dqn:
                next_q_online = self.model(next_obs, next_legal_masks)
                next_actions = next_q_online.argmax(dim=1, keepdim=True)
                next_q_target = self.target_model(next_obs, next_legal_masks)
                target_q = next_q_target.gather(1, next_actions).squeeze(1)
            else:
                next_q = self.target_model(next_obs, next_legal_masks)
                target_q = next_q.max(dim=1)[0]

            target = rewards + (1 - dones) * 0.99 * target_q

        # Loss
        loss = nn.SmoothL1Loss()(q_values, target)
        self._last_loss = loss.item()

        # Gradient step
        self.optimizer.zero_grad()
        loss.backward()
        if config.grad_clip > 0:
            nn.utils.clip_grad_norm_(self.model.parameters(), config.grad_clip)
        self.optimizer.step()

        self.total_steps += 1

        # Update target network
        if config.target_update_tau > 0 and self.total_steps % 100 == 0:
            self._soft_update_target()
        elif self.total_steps % config.target_update_freq == 0:
            self.target_model.load_state_dict(self.model.state_dict())

    def _soft_update_target(self):
        tau = self.config.target_update_tau
        for target_param, param in zip(self.target_model.parameters(), self.model.parameters()):
            target_param.data.copy_(tau * param.data + (1 - tau) * target_param.data)

    def _get_epsilon(self) -> float:
        """Linear epsilon decay."""
        config = self.config
        progress = min(self.total_steps / config.eps_decay_steps, 1.0)
        return config.eps_start + (config.eps_end - config.eps_start) * progress

    def shutdown(self):
        """Clean up process pool."""
        if self._pool is not None:
            log.info("Shutting down process pool")
            self._pool.shutdown(wait=False)
            self._pool = None

    def save_checkpoint(self, path: str):
        """Save model checkpoint."""
        torch.save({
            "model_state_dict": self.model.state_dict(),
            "target_model_state_dict": self.target_model.state_dict(),
            "optimizer_state_dict": self.optimizer.state_dict(),
            "config": self.config.to_dict(),
            "total_steps": self.total_steps,
            "cycle": self.cycle,
            "best_eval_reward": self.best_eval_reward,
        }, path)
        log.info(f"Checkpoint saved: {path} (cycle={self.cycle}, steps={self.total_steps})")

    def load_checkpoint(self, path: str):
        """Load model checkpoint."""
        ckpt = torch.load(path, map_location=self.device, weights_only=False)
        self.model.load_state_dict(ckpt["model_state_dict"])
        self.target_model.load_state_dict(ckpt["target_model_state_dict"])
        self.optimizer.load_state_dict(ckpt["optimizer_state_dict"])
        self.total_steps = ckpt.get("total_steps", 0)
        self.cycle = ckpt.get("cycle", 0)
        self.best_eval_reward = ckpt.get("best_eval_reward", float('-inf'))
        log.info(f"Checkpoint loaded: {path} (cycle={self.cycle}, steps={self.total_steps})")
