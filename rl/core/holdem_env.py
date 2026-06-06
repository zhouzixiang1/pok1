"""Gymnasium environment for heads-up No-Limit Texas Hold'em.

Wraps engine/judge.py Holdem state machine into a standard Gym interface
suitable for DQN / Transformer Q-learning training.

Key design decisions:
- 2-player heads-up only
- Discrete action space: fold, check/call, allin, + N raise sizes
- Observation: dict with cards, chips, pot, history features
- Reward: per-hand chip delta (normalized by big blind)
"""

from __future__ import annotations

import enum
import json
import os
import sys
from pathlib import Path
from typing import Any

import gymnasium as gym
import numpy as np
from gymnasium import spaces

# Add engine/ to path
ENGINE_DIR = Path(__file__).resolve().parent.parent.parent / "engine"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from judge import Holdem, Card, Suit, HandType, compare_full_cards, find_max_hand_type

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
NUM_CARDS = 52
NUM_RANKS = 13  # 2..14
NUM_SUITS = 4
INITIAL_CHIPS = 20000
SMALL_BLIND = 50
BIG_BLIND = 100
HANDS_PER_GAME = 50

# Action space: discrete
# 0 = fold, 1 = check/call, 2 = all-in, 3..N = raise actions
NUM_RAISE_BINS = 8  # raise sizes: 0.25x, 0.5x, 0.75x, 1x, 1.5x, 2x, 3x, 5x pot
NUM_ACTIONS = 3 + NUM_RAISE_BINS  # 11 total actions

RAISE_MULTIPLIERS = [0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0]

# Observation dimensions
CARD_VEC_DIM = NUM_CARDS  # one-hot for each card (52)
HISTORY_FEATURES_DIM = 64  # compressed history features
PLAYER_FEATURES_DIM = 16  # chips, pot, position, stage, etc.
OBS_DIM = CARD_VEC_DIM + HISTORY_FEATURES_DIM + PLAYER_FEATURES_DIM  # 132


class HoldemAction(enum.IntEnum):
    FOLD = 0
    CHECK_CALL = 1
    ALL_IN = 2
    RAISE_025 = 3   # 0.25x pot
    RAISE_050 = 4   # 0.5x pot
    RAISE_075 = 5   # 0.75x pot
    RAISE_100 = 6   # 1x pot
    RAISE_150 = 7   # 1.5x pot
    RAISE_200 = 8   # 2x pot
    RAISE_300 = 9   # 3x pot
    RAISE_500 = 10  # 5x pot


def _card_to_int(card: Card) -> int:
    return card.to_int()


def _cards_to_set(cards: list[Card]) -> set[int]:
    return {_card_to_int(c) for c in cards}


class HoldemEnv(gym.Env):
    """Heads-up No-Limit Texas Hold'em Gymnasium environment.

    Each episode = one hand (not a full game of 50 hands).
    Reward = normalized chip delta for the agent.

    The agent always plays as player 0. Opponent can be:
    - A callable policy function
    - A random policy
    - Another Q-network
    """

    metadata = {"render_modes": []}

    def __init__(
        self,
        opponent_policy: Any = None,
        initial_chips: int = INITIAL_CHIPS,
        small_blind: int = SMALL_BLIND,
        big_blind: int = BIG_BLIND,
        seed: int | None = None,
    ):
        super().__init__()

        self.initial_chips = initial_chips
        self.small_blind = small_blind
        self.big_blind = big_blind
        self.opponent_policy = opponent_policy
        self._rng = np.random.default_rng(seed)

        # Action space: discrete NUM_ACTIONS
        self.action_space = spaces.Discrete(NUM_ACTIONS)

        # Observation space: dict
        self.observation_space = spaces.Dict({
            "card_vec": spaces.Box(0, 1, shape=(CARD_VEC_DIM,), dtype=np.float32),
            "history": spaces.Box(-1, 1, shape=(HISTORY_FEATURES_DIM,), dtype=np.float32),
            "player": spaces.Box(-10, 10, shape=(PLAYER_FEATURES_DIM,), dtype=np.float32),
            "legal_actions": spaces.Box(0, 1, shape=(NUM_ACTIONS,), dtype=np.float32),
        })

        self._game: Holdem | None = None
        self._deck: list[int] = []
        self._agent_idx: int = 0
        self._opponent_idx: int = 1
        self._step_count: int = 0
        self._hand_history: list[dict] = []
        self._chips_before: int = 0
        self._hand_over: bool = False
        self._winners: list[int] | None = None

    def reset(self, *, seed: int | None = None, options: dict | None = None) -> tuple:
        super().reset(seed=seed)
        rng_seed = seed if seed is not None else int(self._rng.integers(2**31))
        self._rng = np.random.default_rng(rng_seed)

        # Random dealer position (0 or 1)
        dealer_idx = int(self._rng.integers(2))
        self._agent_idx = 0
        self._opponent_idx = 1

        # Create game
        self._game = Holdem(
            [self.initial_chips, self.initial_chips],
            dealer_idx=dealer_idx,
            small_blind=self.small_blind,
            big_blind=self.big_blind,
        )

        # Set deterministic deck
        deck = list(range(NUM_CARDS))
        self._rng.shuffle(deck)
        self._deck = deck
        self._game.set_deck_array(deck)

        # Deal cards and blinds
        self._game.deal_cards_and_blind()
        self._chips_before = self._game.player_chips[self._agent_idx]
        self._hand_history = []
        self._step_count = 0
        self._hand_over = False
        self._winners = None

        # If opponent goes first, execute opponent action
        if self._game.round_idx == self._opponent_idx:
            self._execute_opponent()

        # If hand ended during initial opponent action (e.g. opponent fold after our all-in blind),
        # we still need to return. The caller should check terminal state.
        if self._hand_over and self._winners is not None:
            # Return terminal observation
            obs = self._get_obs()
            info = self._get_info()
            info["hand_result"] = {"winners": self._winners, "note": "hand_over_during_reset"}
            return obs, info

        obs = self._get_obs()
        info = self._get_info()
        return obs, info

    def step(self, action: int) -> tuple:
        """Execute one agent action.

        Returns (obs, reward, terminated, truncated, info).
        """
        if self._game is None:
            raise RuntimeError("Call reset() first")
        if self._hand_over:
            raise RuntimeError("Hand is already over, call reset()")

        self._step_count += 1

        # Convert discrete action to Holdem action
        holdem_action = self._discrete_to_holdem(action)

        # Execute agent action
        result = self._game.player_action(holdem_action)
        self._hand_history.append({
            "player": self._agent_idx,
            "action": action,
            "holdem_action": holdem_action,
            "stage": self._game.round,
        })

        # Check if hand is over
        if result is not None and len(result) > 0:
            return self._finish_hand(result)

        # Opponent's turn
        if self._game.round_idx == self._opponent_idx:
            self._execute_opponent()
            # Check if hand ended after opponent action
            if self._hand_over and self._winners is not None:
                return self._finish_hand(self._winners)

        obs = self._get_obs()
        reward = 0.0
        terminated = False
        truncated = False
        info = self._get_info()

        return obs, reward, terminated, truncated, info

    def _execute_opponent(self):
        """Execute opponent action(s) until it's agent's turn or hand ends."""
        max_steps = 20  # safety limit
        for _ in range(max_steps):
            if self._game is None or self._hand_over:
                return
            if self._game.round_idx == self._agent_idx:
                return

            # Get opponent action
            if self.opponent_policy is not None:
                obs = self._get_obs_for_player(self._opponent_idx)
                opp_action = self.opponent_policy(obs, self._game)
            else:
                opp_action = self._random_opponent_action()

            holdem_action = self._discrete_to_holdem_for_player(
                opp_action, self._opponent_idx
            )
            result = self._game.player_action(holdem_action)
            self._hand_history.append({
                "player": self._opponent_idx,
                "action": opp_action,
                "holdem_action": holdem_action,
                "stage": self._game.round if self._game else -1,
            })

            if result is not None and len(result) > 0:
                self._hand_over = True
                self._winners = result
                return

    def _random_opponent_action(self) -> int:
        """Random legal opponent action."""
        legal = self._get_legal_actions(self._opponent_idx)
        legal_indices = np.where(legal > 0)[0]
        if len(legal_indices) == 0:
            return HoldemAction.FOLD
        return int(self._rng.choice(legal_indices))

    def _discrete_to_holdem(self, action: int) -> int:
        """Convert discrete action to Holdem action for agent."""
        return self._discrete_to_holdem_for_player(action, self._agent_idx)

    def _discrete_to_holdem_for_player(self, action: int, player_idx: int) -> int:
        """Convert discrete action index to Holdem action value."""
        game = self._game
        current_bet = game.round_player_bet[player_idx]
        to_call = game.round_bet - current_bet
        chips = game.player_chips[player_idx]

        if action == HoldemAction.FOLD:
            return Holdem.FOLD
        elif action == HoldemAction.CHECK_CALL:
            if to_call <= 0:
                return Holdem.CALL  # check
            elif to_call >= chips:
                return Holdem.ALLIN
            else:
                return Holdem.CALL
        elif action == HoldemAction.ALL_IN:
            return Holdem.ALLIN
        else:
            # Raise action
            raise_idx = action - 3  # 0..7
            multiplier = RAISE_MULTIPLIERS[raise_idx]

            # Calculate raise-to amount
            pot_after_call = game.pot + to_call
            raise_amount = max(int(pot_after_call * multiplier), game.big_blind)
            raise_to = game.round_bet + raise_amount

            # Clamp to valid range
            min_raise_to = game.last_raise_to * 2
            raise_to = max(raise_to, min_raise_to)
            max_raise_to = current_bet + chips
            raise_to = min(raise_to, max_raise_to)

            if raise_to <= game.round_bet:
                # Can't raise this small, fall back to call
                return Holdem.CALL if to_call > 0 else Holdem.CALL
            if raise_to >= current_bet + chips:
                return Holdem.ALLIN

            return raise_to

    def _finish_hand(self, winners: list[int]) -> tuple:
        """Hand is over. Calculate reward and return terminal obs."""
        self._hand_over = True
        game = self._game
        final_chips = game.get_player_final_chips(winners)
        agent_chips_after = final_chips[self._agent_idx]

        # Reward = normalized chip delta
        reward = (agent_chips_after - self._chips_before) / self.big_blind

        obs = self._get_obs()
        info = self._get_info()
        info["hand_result"] = {
            "winners": winners,
            "agent_chips_delta": agent_chips_after - self._chips_before,
            "final_chips": final_chips,
        }

        terminated = True
        truncated = False

        return obs, reward, terminated, truncated, info

    def _get_legal_actions(self, player_idx: int) -> np.ndarray:
        """Get legal action mask for a player."""
        legal = np.zeros(NUM_ACTIONS, dtype=np.float32)
        game = self._game

        if game is None:
            legal[HoldemAction.FOLD] = 1.0
            return legal

        current_bet = game.round_player_bet[player_idx]
        to_call = game.round_bet - current_bet
        chips = game.player_chips[player_idx]

        # Fold is always legal (though usually suboptimal when check is available)
        legal[HoldemAction.FOLD] = 1.0

        # Check/call
        if to_call <= 0:
            legal[HoldemAction.CHECK_CALL] = 1.0  # check
        elif to_call <= chips:
            legal[HoldemAction.CHECK_CALL] = 1.0  # call
        elif chips > 0:
            legal[HoldemAction.CHECK_CALL] = 1.0  # call (short stack)

        # All-in
        if chips > 0 and not game.allin_occurred:
            legal[HoldemAction.ALL_IN] = 1.0

        # Raises
        if chips > 0 and not game.allin_occurred:
            min_raise_to = game.last_raise_to * 2
            max_raise_to = current_bet + chips

            if min_raise_to <= max_raise_to:
                for i, mult in enumerate(RAISE_MULTIPLIERS):
                    pot_after_call = game.pot + to_call
                    raise_amount = max(int(pot_after_call * mult), game.big_blind)
                    raise_to = game.round_bet + raise_amount
                    raise_to = max(raise_to, min_raise_to)
                    raise_to = min(raise_to, max_raise_to)

                    if raise_to > game.round_bet and raise_to < current_bet + chips:
                        legal[3 + i] = 1.0

        # If only fold is legal and check is available, enable check
        if np.sum(legal) == 1 and to_call <= 0:
            legal[HoldemAction.CHECK_CALL] = 1.0

        return legal

    def _get_obs(self) -> dict:
        """Get observation dict for the agent."""
        return self._get_obs_for_player(self._agent_idx)

    def _get_obs_for_player(self, player_idx: int) -> dict:
        """Get observation for a specific player."""
        game = self._game
        if game is None:
            return self._build_empty_obs()

        # Card vector: own cards + public cards
        card_vec = np.zeros(CARD_VEC_DIM, dtype=np.float32)
        for card in game.player_cards[player_idx]:
            card_vec[card.to_int()] = 1.0
        for card in game.public_cards:
            card_vec[card.to_int()] = 1.0

        # History features
        history = self._encode_history(player_idx)

        # Player features
        player = self._encode_player_features(player_idx)

        # Legal actions
        legal = self._get_legal_actions(player_idx)

        return {
            "card_vec": card_vec,
            "history": history,
            "player": player,
            "legal_actions": legal,
        }

    def _encode_history(self, player_idx: int) -> np.ndarray:
        """Encode action history into fixed-size feature vector."""
        features = np.zeros(HISTORY_FEATURES_DIM, dtype=np.float32)
        game = self._game

        # Stage one-hot (4 stages)
        features[game.round] = 1.0

        # Round bet and pot (normalized)
        features[4] = game.round_bet / self.big_blind / 100.0
        features[5] = game.pot / self.initial_chips

        # Number of raises and calls this hand
        n_raises = sum(1 for h in game.history if h.get("action_type") == "raise")
        n_calls = sum(1 for h in game.history if h.get("action_type") == "call")
        n_folds = sum(1 for h in game.history if h.get("action_type") == "fold")
        features[6] = n_raises / 10.0
        features[7] = n_calls / 10.0
        features[8] = n_folds / 10.0

        # Last raise size (normalized)
        raises = [h for h in game.history if h.get("action_type") == "raise"]
        if raises:
            last_raise = raises[-1].get("action", 0)
            features[9] = last_raise / self.big_blind / 50.0

        # Opponent aggression (raises / total actions)
        opp_actions = [h for h in game.history if h.get("player_id") == (1 - player_idx)]
        if opp_actions:
            opp_raises = sum(1 for h in opp_actions if h.get("action_type") == "raise")
            features[10] = opp_raises / max(len(opp_actions), 1)

        # Per-stage action encoding (compact)
        for i, h in enumerate(game.history[-8:]):
            offset = 11 + i * 6
            if offset + 5 < HISTORY_FEATURES_DIM:
                is_agent = 1.0 if h.get("player_id") == player_idx else -1.0
                features[offset] = is_agent
                features[offset + 1] = h.get("round", 0) / 3.0
                action_type = h.get("action_type", "")
                if action_type == "fold":
                    features[offset + 2] = -1.0
                elif action_type == "call":
                    features[offset + 2] = 0.0
                elif action_type == "raise":
                    features[offset + 2] = 1.0
                elif action_type == "check":
                    features[offset + 2] = 0.0
                elif action_type == "allin":
                    features[offset + 2] = 2.0
                features[offset + 3] = h.get("action", 0) / self.big_blind / 50.0

        return features

    def _encode_player_features(self, player_idx: int) -> np.ndarray:
        """Encode player-specific features."""
        game = self._game
        features = np.zeros(PLAYER_FEATURES_DIM, dtype=np.float32)
        opp_idx = 1 - player_idx

        # Chips (normalized)
        features[0] = game.player_chips[player_idx] / self.initial_chips
        features[1] = game.player_chips[opp_idx] / self.initial_chips

        # Chip ratio
        total = game.player_chips[player_idx] + game.player_chips[opp_idx]
        if total > 0:
            features[2] = game.player_chips[player_idx] / total

        # Pot (normalized)
        features[3] = game.pot / self.initial_chips

        # Position (0=SB/dealer, 1=BB)
        features[4] = 1.0 if player_idx != game.dealer_idx else 0.0

        # To call (normalized)
        to_call = game.round_bet - game.round_player_bet[player_idx]
        features[5] = to_call / self.big_blind / 10.0

        # Pot odds
        if to_call > 0:
            features[6] = to_call / (game.pot + to_call)

        # Stage
        features[7] = game.round / 3.0

        # Dealer
        features[8] = 1.0 if game.dealer_idx == player_idx else -1.0

        # Round player bet (normalized)
        features[9] = game.round_player_bet[player_idx] / self.big_blind / 50.0
        features[10] = game.round_player_bet[opp_idx] / self.big_blind / 50.0

        # All-in flag
        features[11] = 1.0 if game.allin_occurred else 0.0

        # Last raise to (normalized)
        features[12] = game.last_raise_to / self.big_blind / 50.0

        # Opponent folded
        features[13] = 1.0 if game.round_player_bet[opp_idx] < 0 else 0.0

        # Cards strength (simple: number of cards available)
        n_own = len(game.player_cards[player_idx])
        n_public = len(game.public_cards)
        features[14] = (n_own + n_public) / 7.0

        # Round actions left
        features[15] = game.round_action_left / 4.0

        return features

    def _build_empty_obs(self) -> dict:
        return {
            "card_vec": np.zeros(CARD_VEC_DIM, dtype=np.float32),
            "history": np.zeros(HISTORY_FEATURES_DIM, dtype=np.float32),
            "player": np.zeros(PLAYER_FEATURES_DIM, dtype=np.float32),
            "legal_actions": np.zeros(NUM_ACTIONS, dtype=np.float32),
        }

    def _get_info(self) -> dict:
        if self._game is None:
            return {}
        return {
            "stage": self._game.round,
            "pot": self._game.pot,
            "chips": list(self._game.player_chips),
            "step_count": self._step_count,
        }

    def get_flat_obs(self, obs: dict | None = None) -> np.ndarray:
        """Flatten observation dict into a single vector (for MLP input)."""
        if obs is None:
            obs = self._get_obs()
        return np.concatenate([
            obs["card_vec"],
            obs["history"],
            obs["player"],
        ])

    def render(self):
        pass
