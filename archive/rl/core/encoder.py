"""State/action encoders for Hold'em RL.

Two encoding versions:
- v0: Flat hand-crafted features (132-dim) for MLP
- v1: Token sequence for Transformer
"""

from __future__ import annotations

import numpy as np

from rl.core.holdem_env import (
    NUM_CARDS, NUM_ACTIONS, OBS_DIM, CARD_VEC_DIM,
    HISTORY_FEATURES_DIM, PLAYER_FEATURES_DIM, BIG_BLIND,
)
from rl.core.tokenizer import encode_token_sequence, CARD_TOKEN_OFFSET


def encode_observation_flat(obs: dict) -> np.ndarray:
    """Flatten observation dict into a single vector for MLP input."""
    return np.concatenate([
        obs["card_vec"],
        obs["history"],
        obs["player"],
    ])


def encode_observation_tokens(
    obs: dict,
    game_state: dict | None = None,
) -> np.ndarray:
    """Encode observation as a token sequence for Transformer input.

    Requires game_state dict with:
        - hand_cards: list[int] (agent's hole cards as card_int)
        - public_cards: list[int] (community cards)
        - history: list[dict] (action history)
        - pot, my_chips, opp_chips, stage, big_blind
    """
    if game_state is None:
        # Fallback: build minimal game state from obs
        # This is lossy — prefer passing game_state directly
        game_state = {
            "hand_cards": _extract_hand_cards(obs),
            "public_cards": _extract_public_cards(obs),
            "history": [],
            "pot": int(obs["player"][3] * 20000),  # approximate
            "my_chips": int(obs["player"][0] * 20000),
            "opp_chips": int(obs["player"][1] * 20000),
            "stage": int(obs["history"][0] * 3) if obs["history"][0] > 0 else 0,
            "big_blind": BIG_BLIND,
        }

    return encode_token_sequence(
        hand_cards=game_state["hand_cards"],
        public_cards=game_state["public_cards"],
        history=game_state.get("history", []),
        pot=game_state["pot"],
        my_chips=game_state["my_chips"],
        opp_chips=game_state["opp_chips"],
        stage=game_state["stage"],
        big_blind=game_state["big_blind"],
    )


def _extract_hand_cards(obs: dict) -> list[int]:
    """Extract hand card integers from card_vec."""
    card_vec = obs["card_vec"]
    return [i for i in range(NUM_CARDS) if card_vec[i] > 0]


def _extract_public_cards(obs: dict) -> list[int]:
    """Extract public card integers from card_vec (all non-hole cards)."""
    # This is a lossy approximation — in practice, the env should provide
    # the split between hand and public cards
    return []
