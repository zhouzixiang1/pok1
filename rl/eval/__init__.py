"""Evaluation framework for Hold'em RL agents."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import torch

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from rl.core.holdem_env import HoldemEnv


class RandomOpponent:
    """Random action opponent."""

    def __init__(self, seed: int = 42):
        self.rng = np.random.default_rng(seed)

    def __call__(self, obs, game):
        legal = obs["legal_actions"]
        legal_indices = np.where(legal > 0)[0]
        return int(self.rng.choice(legal_indices)) if len(legal_indices) > 0 else 0


class AlwaysCallOpponent:
    """Always check/call opponent (passive)."""

    def __call__(self, obs, game):
        from rl.core.holdem_env import HoldemAction
        legal = obs["legal_actions"]
        if legal[HoldemAction.CHECK_CALL] > 0:
            return HoldemAction.CHECK_CALL
        return 0  # fold if can't check/call


class AggressiveOpponent:
    """Aggressive opponent that raises frequently."""

    def __init__(self, seed: int = 42, raise_prob: float = 0.5):
        self.rng = np.random.default_rng(seed)
        self.raise_prob = raise_prob

    def __call__(self, obs, game):
        from rl.core.holdem_env import HoldemAction
        legal = obs["legal_actions"]

        if self.rng.random() < self.raise_prob:
            # Try to raise
            for action in [HoldemAction.RAISE_100, HoldemAction.RAISE_050,
                          HoldemAction.RAISE_200, HoldemAction.RAISE_075]:
                if legal[action] > 0:
                    return action

        if legal[HoldemAction.CHECK_CALL] > 0:
            return HoldemAction.CHECK_CALL
        return HoldemAction.FOLD


def evaluate(
    model: torch.nn.Module,
    opponent,
    num_games: int = 100,
    seed: int = 42,
    device: str = "cpu",
) -> dict:
    """Evaluate model against an opponent.

    Returns dict with win_rate, avg_reward, etc.
    """
    model.eval()
    env = HoldemEnv(opponent_policy=opponent, seed=seed)

    wins = 0
    losses = 0
    draws = 0
    total_reward = 0.0
    rewards_list = []

    for game_idx in range(num_games):
        obs, info = env.reset(seed=seed + game_idx)
        game_reward = 0.0
        done = env._hand_over  # hand might be over from reset

        while not done:
            flat_obs = env.get_flat_obs(obs)
            legal_mask = obs["legal_actions"]

            with torch.no_grad():
                obs_tensor = torch.FloatTensor(flat_obs).unsqueeze(0).to(device)
                mask_tensor = torch.FloatTensor(legal_mask).unsqueeze(0).to(device)
                q_values = model(obs_tensor, mask_tensor)
                action = q_values.argmax(dim=-1).item()

            obs, reward, terminated, truncated, info = env.step(action)
            game_reward += reward
            done = terminated or truncated

        total_reward += game_reward
        rewards_list.append(game_reward)

        hand_result = info.get("hand_result", {})
        agent_delta = hand_result.get("agent_chips_delta", 0)
        if agent_delta > 0:
            wins += 1
        elif agent_delta < 0:
            losses += 1
        else:
            draws += 1

    total_decided = wins + losses
    win_rate = wins / total_decided if total_decided > 0 else 0.0

    return {
        "win_rate": win_rate,
        "wins": wins,
        "losses": losses,
        "draws": draws,
        "num_games": num_games,
        "avg_reward": total_reward / num_games,
        "std_reward": np.std(rewards_list) if rewards_list else 0.0,
    }
