"""Tokenizer for Hold'em game history.

Inspired by DanLM's approach: tokenize game history as a sequence of discrete
tokens, enabling Transformer-based Q-learning.

Vocabulary design (~50 tokens):
- Card tokens: [CARD 2H..AS] = 52 tokens (card_int directly)
- Action tokens: FOLD, CHECK, CALL, RAISE, ALLIN = 5 tokens
- Stage tokens: PREFLOP, FLOP, TURN, RIVER = 4 tokens
- Special tokens: PAD, START, SEP, AGENT, OPPONENT = 5 tokens
- Raise size bins: 8 tokens (matching RAISE_MULTIPLIERS)
- Player tokens: P0, P1 = 2 tokens

Total vocab: ~76 tokens

Token sequence format per hand:
    [START] [STAGE] [P0_cards] [P1_hidden] [SEP]
    [P0] [ACTION] [RAISE_SIZE?] [SEP]
    [P1] [ACTION] [RAISE_SIZE?] [SEP]
    ...
    [STAGE] [COMMUNITY_CARDS] [SEP]
    ...
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

import numpy as np

# ---------------------------------------------------------------------------
# Token vocabulary
# ---------------------------------------------------------------------------
VOCAB_SIZE = 80

# Special tokens (0-7)
PAD = 0
START = 1
SEP = 2
AGENT = 3       # marks agent's turn
OPPONENT = 4    # marks opponent's turn
MASK = 5        # masked position (for variable-length padding)
UNKNOWN = 6
END = 7

# Stage tokens (8-11)
PREFLOP = 8
FLOP = 9
TURN = 10
RIVER = 11

# Action tokens (12-19)
FOLD = 12
CHECK = 13
CALL = 14
RAISE = 15
ALLIN = 16

# Raise size bins (20-27)
RAISE_BIN_025 = 20
RAISE_BIN_050 = 21
RAISE_BIN_075 = 22
RAISE_BIN_100 = 23
RAISE_BIN_150 = 24
RAISE_BIN_200 = 25
RAISE_BIN_300 = 26
RAISE_BIN_500 = 27

# Player tokens (28-29)
PLAYER_0 = 28
PLAYER_1 = 29

# Card tokens (30-81): card_int 0..51 maps to token 30+card_int
CARD_TOKEN_OFFSET = 30

# Pot size tokens (82-89) — bucketed
POT_SMALL = 82    # < 5 BB
POT_MEDIUM = 83   # 5-15 BB
POT_LARGE = 84    # 15-30 BB
POT_HUGE = 85     # > 30 BB

# Chip advantage tokens (86-89)
CHIP_BEHIND = 86    # < 0.8x opponent
CHIP_EVEN = 87      # 0.8-1.2x
CHIP_AHEAD = 88     # 1.2-2x
CHIP_DOMINANT = 89  # > 2x

# Reverse mapping for debugging
TOKEN_NAMES = {
    PAD: "PAD", START: "START", SEP: "SEP", AGENT: "AGENT",
    OPPONENT: "OPPONENT", MASK: "MASK", UNKNOWN: "UNK", END: "END",
    PREFLOP: "PREFLOP", FLOP: "FLOP", TURN: "TURN", RIVER: "RIVER",
    FOLD: "FOLD", CHECK: "CHECK", CALL: "CALL", RAISE: "RAISE",
    ALLIN: "ALLIN",
    RAISE_BIN_025: "R025", RAISE_BIN_050: "R050", RAISE_BIN_075: "R075",
    RAISE_BIN_100: "R100", RAISE_BIN_150: "R150", RAISE_BIN_200: "R200",
    RAISE_BIN_300: "R300", RAISE_BIN_500: "R500",
    PLAYER_0: "P0", PLAYER_1: "P1",
    POT_SMALL: "POT_S", POT_MEDIUM: "POT_M", POT_LARGE: "POT_L", POT_HUGE: "POT_H",
    CHIP_BEHIND: "CHP_B", CHIP_EVEN: "CHP_E", CHIP_AHEAD: "CHP_A",
    CHIP_DOMINANT: "CHP_D",
}


def card_to_token(card_int: int) -> int:
    return CARD_TOKEN_OFFSET + card_int


def token_to_card(token: int) -> int | None:
    if CARD_TOKEN_OFFSET <= token < CARD_TOKEN_OFFSET + 52:
        return token - CARD_TOKEN_OFFSET
    return None


def stage_to_token(stage: int) -> int:
    return [PREFLOP, FLOP, TURN, RIVER][stage]


def action_type_to_token(action_type: str) -> int:
    return {
        "fold": FOLD, "check": CHECK, "call": CALL,
        "raise": RAISE, "allin": ALLIN,
    }.get(action_type, UNKNOWN)


def raise_to_bin(raise_amount: int, pot: int, big_blind: int) -> int:
    """Map a raise amount to a bin token."""
    if pot <= 0:
        pot = big_blind
    ratio = raise_amount / pot
    if ratio <= 0.375:
        return RAISE_BIN_025
    elif ratio <= 0.625:
        return RAISE_BIN_050
    elif ratio <= 0.875:
        return RAISE_BIN_075
    elif ratio <= 1.25:
        return RAISE_BIN_100
    elif ratio <= 1.75:
        return RAISE_BIN_150
    elif ratio <= 2.5:
        return RAISE_BIN_200
    elif ratio <= 4.0:
        return RAISE_BIN_300
    else:
        return RAISE_BIN_500


def pot_to_token(pot: int, big_blind: int) -> int:
    bb_ratio = pot / big_blind
    if bb_ratio < 5:
        return POT_SMALL
    elif bb_ratio < 15:
        return POT_MEDIUM
    elif bb_ratio < 30:
        return POT_LARGE
    else:
        return POT_HUGE


def chips_to_token(my_chips: int, opp_chips: int) -> int:
    if opp_chips <= 0:
        return CHIP_DOMINANT
    ratio = my_chips / opp_chips
    if ratio < 0.8:
        return CHIP_BEHIND
    elif ratio <= 1.2:
        return CHIP_EVEN
    elif ratio <= 2.0:
        return CHIP_AHEAD
    else:
        return CHIP_DOMINANT


def encode_token_sequence(
    hand_cards: list[int],       # agent's hole cards (as card_int)
    public_cards: list[int],     # community cards
    history: list[dict],         # action history from Holdem
    pot: int,
    my_chips: int,
    opp_chips: int,
    stage: int,
    big_blind: int,
    max_len: int = 128,
) -> np.ndarray:
    """Encode a game state into a fixed-length token sequence.

    Format:
        [START] [STAGE] [POT_SIZE] [CHIP_STATUS] [AGENT] [CARD] [CARD] [SEP]
        For each action in history:
            [PLAYER] [ACTION] [RAISE_BIN?] [SEP]
        [STAGE] [COMMUNITY_CARDS...] [SEP] (for each stage transition)
        [PAD] ... (padding to max_len)
    """
    tokens = [START, stage_to_token(stage), pot_to_token(pot, big_blind),
              chips_to_token(my_chips, opp_chips)]

    # Agent's hole cards
    tokens.append(AGENT)
    for c in sorted(hand_cards):
        tokens.append(card_to_token(c))
    tokens.append(SEP)

    # Encode action history
    for h in history:
        pid = h.get("player_id", 0)
        tokens.append(PLAYER_0 if pid == 0 else PLAYER_1)
        tokens.append(action_type_to_token(h.get("action_type", "")))

        # Add raise bin for raise actions
        if h.get("action_type") == "raise":
            raise_amt = h.get("action", 0)
            tokens.append(raise_to_bin(raise_amt, pot, big_blind))

        tokens.append(SEP)

    # Encode community cards per stage
    if public_cards:
        for s in range(stage + 1):
            stage_cards = _get_stage_cards(public_cards, s)
            if stage_cards:
                tokens.append(stage_to_token(s))
                for c in sorted(stage_cards):
                    tokens.append(card_to_token(c))
                tokens.append(SEP)

    # Truncate or pad
    if len(tokens) > max_len:
        tokens = tokens[:max_len]
    else:
        tokens = tokens + [PAD] * (max_len - len(tokens))

    return np.array(tokens, dtype=np.int64)


def _get_stage_cards(public_cards: list[int], stage: int) -> list[int]:
    """Get the cards that appeared at a given stage."""
    if stage == 0:  # preflop — no community cards yet
        return []
    elif stage == 1:  # flop — first 3 cards
        return public_cards[:3]
    elif stage == 2:  # turn — 4th card
        return public_cards[3:4] if len(public_cards) >= 4 else []
    elif stage == 3:  # river — 5th card
        return public_cards[4:5] if len(public_cards) >= 5 else []
    return []
