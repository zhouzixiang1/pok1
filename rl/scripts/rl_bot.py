#!/usr/bin/env python3
"""RL Bot wrapper for POK engine battle system.

This bot loads a trained Q-network checkpoint and plays heads-up NL Hold'em
using the POK subprocess JSON protocol (engine/judge.py).

Usage:
    python rl/scripts/rl_bot.py    # Uses default checkpoint path
    python rl/scripts/rl_bot.py --ckpt rl/checkpoints/best_model.pt

Can be used as a regular bot in engine/battle.py:
    python engine/battle.py bots/bot5/main.py rl/scripts/rl_bot.py -n 50 -v
"""

from __future__ import annotations

import argparse
import json
import sys
import os
from pathlib import Path

# Add project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np

# Lazy imports for torch (heavy)
_torch = None
_model = None
_config = None
_device = None

DEFAULT_CKPT = str(PROJECT_ROOT / "rl" / "checkpoints" / "best_model.pt")


def _load_model(ckpt_path: str):
    """Lazy-load model on first call."""
    global _torch, _model, _config, _device
    if _model is not None:
        return

    import torch
    from rl.core.config import HoldemRLConfig
    from rl.training.trainer import build_model

    _torch = torch
    _device = torch.device("cpu")
    ckpt = torch.load(ckpt_path, map_location=_device, weights_only=False)
    _config = HoldemRLConfig.from_dict(ckpt["config"])
    _model = build_model(_config).to(_device)
    _model.load_state_dict(ckpt["model_state_dict"])
    _model.eval()


def _encode_observation(req: dict) -> tuple[np.ndarray, np.ndarray]:
    """Encode POK judge request into flat observation and legal action mask.

    POK request format:
        {
            "num_players": 2,
            "dealer_id": int,
            "my_id": int,
            "my_chips": int,
            "my_cards": [int, ...],
            "public_cards": [int, ...],
            "history": [{"player_id": int, "action": int, "action_type": str, "round": int}]
        }
    """
    from rl.core.holdem_env import (
        NUM_CARDS, CARD_VEC_DIM, HISTORY_FEATURES_DIM,
        PLAYER_FEATURES_DIM, BIG_BLIND, NUM_ACTIONS, RAISE_MULTIPLIERS,
        INITIAL_CHIPS, HoldemAction,
    )

    my_id = req.get("my_id", 0)
    opp_id = 1 - my_id
    my_chips = req.get("my_chips", INITIAL_CHIPS)
    my_cards = req.get("my_cards", [])
    public_cards = req.get("public_cards", [])
    history = req.get("history", [])
    dealer_id = req.get("dealer_id", 0)

    # Card vector
    card_vec = np.zeros(NUM_CARDS, dtype=np.float32)
    for c in my_cards:
        card_vec[c] = 1.0
    for c in public_cards:
        card_vec[c] = 1.0

    # History features
    hist_features = np.zeros(HISTORY_FEATURES_DIM, dtype=np.float32)
    stage = 0
    if len(public_cards) >= 3:
        stage = 1
    if len(public_cards) >= 4:
        stage = 2
    if len(public_cards) >= 5:
        stage = 3
    hist_features[stage] = 1.0

    # Pot estimate from history
    pot = req.get("pot", BIG_BLIND * 2)
    hist_features[4] = pot / BIG_BLIND / 100.0
    hist_features[5] = pot / INITIAL_CHIPS

    n_raises = sum(1 for h in history if h.get("action_type") == "raise")
    n_calls = sum(1 for h in history if h.get("action_type") in ("call", "check"))
    hist_features[6] = n_raises / 10.0
    hist_features[7] = n_calls / 10.0

    # Recent actions
    for i, h in enumerate(history[-4:]):
        offset = 11 + i * 6
        if offset + 3 < HISTORY_FEATURES_DIM:
            hist_features[offset] = 1.0 if h.get("player_id") == my_id else -1.0
            action_type = h.get("action_type", "")
            if action_type == "fold":
                hist_features[offset + 2] = -1.0
            elif action_type in ("call", "check"):
                hist_features[offset + 2] = 0.0
            elif action_type == "raise":
                hist_features[offset + 2] = 1.0
            elif action_type == "allin":
                hist_features[offset + 2] = 2.0

    # Player features
    player_features = np.zeros(PLAYER_FEATURES_DIM, dtype=np.float32)
    # Chips (approximate - we don't have opponent chips directly)
    player_features[0] = my_chips / INITIAL_CHIPS
    player_features[4] = 1.0 if my_id != dealer_id else 0.0  # is BB
    player_features[7] = stage / 3.0
    player_features[8] = 1.0 if dealer_id == my_id else -1.0

    obs = np.concatenate([card_vec, hist_features, player_features])
    return obs


def _compute_legal_actions(req: dict) -> np.ndarray:
    """Compute legal action mask from request state."""
    from rl.core.holdem_env import HoldemAction, RAISE_MULTIPLIERS, BIG_BLIND, NUM_ACTIONS

    legal = np.zeros(NUM_ACTIONS, dtype=np.float32)

    # Always can fold
    legal[HoldemAction.FOLD] = 1.0

    # Check/call is almost always legal
    legal[HoldemAction.CHECK_CALL] = 1.0

    # All-in
    my_chips = req.get("my_chips", 0)
    if my_chips > 0:
        legal[HoldemAction.ALL_IN] = 1.0

    # Raises — simplified, allow most raises
    if my_chips > BIG_BLIND:
        for i in range(len(RAISE_MULTIPLIERS)):
            legal[3 + i] = 1.0

    return legal


def _action_to_pok(action: int, req: dict) -> int:
    """Convert discrete action to POK protocol action.

    POK protocol:
        0 = call/check
        -1 = fold
        -2 = allin
        >0 = raise-to-total amount
    """
    from rl.core.holdem_env import HoldemAction, RAISE_MULTIPLIERS, BIG_BLIND
    from engine.judge import Holdem

    if action == HoldemAction.FOLD:
        return -1
    elif action == HoldemAction.CHECK_CALL:
        return 0
    elif action == HoldemAction.ALL_IN:
        return -2
    else:
        # Raise action
        raise_idx = action - 3
        multiplier = RAISE_MULTIPLIERS[raise_idx]

        # Estimate pot from history
        history = req.get("history", [])
        pot = BIG_BLIND * 2  # minimum pot
        for h in history:
            act = h.get("action", 0)
            if act > 0:  # raise
                pot += act

        raise_amount = max(int(pot * multiplier), BIG_BLIND)

        # Get current round_bet from last_raise_to in req
        round_raise = req.get("round_raise", BIG_BLIND)
        min_raise_to = round_raise * 2
        raise_to = max(round_raise + raise_amount, min_raise_to)

        # Clamp to chips
        my_chips = req.get("my_chips", 0)
        my_bet = req.get("round_player_bet", [0, 0])
        if isinstance(my_bet, list) and len(my_bet) > req.get("my_id", 0):
            current_bet = my_bet[req.get("my_id", 0)]
        else:
            current_bet = 0

        max_raise = current_bet + my_chips
        raise_to = min(raise_to, max_raise)

        if raise_to >= max_raise:
            return -2  # all-in instead

        return raise_to


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ckpt", type=str, default=os.environ.get("RL_CKPT", DEFAULT_CKPT))
    args, _ = parser.parse_known_args()

    payload = json.loads(input())
    requests = payload["requests"]
    req = dict(requests[-1])

    _load_model(args.ckpt)

    obs = _encode_observation(req)
    legal_mask = _compute_legal_actions(req)

    obs_tensor = _torch.FloatTensor(obs).unsqueeze(0).to(_device)
    mask_tensor = _torch.FloatTensor(legal_mask).unsqueeze(0).to(_device)

    with _torch.no_grad():
        q_values = _model(obs_tensor, mask_tensor)
        action = q_values.argmax(dim=-1).item()

    pok_action = _action_to_pok(action, req)
    print(json.dumps({"response": pok_action}))


if __name__ == "__main__":
    main()
