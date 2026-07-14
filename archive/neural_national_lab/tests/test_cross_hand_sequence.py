from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
TOOLS = ROOT / "bots" / "neural_national_lab" / "tools"
sys.path.insert(0, str(TOOLS))

import cross_hand_sequence as features  # noqa: E402


def test_server_and_native_summaries_match_on_public_observations() -> None:
    events = [
        {"type": "action", "hand": 1, "player_idx": 1, "action": "call",
         "amount": 50, "pot": 200, "stage": "preflop"},
        {"type": "stage", "hand": 1, "stage": "flop"},
        {"type": "action", "hand": 1, "player_idx": 1, "action": "raise",
         "amount": 300, "pot": 500, "stage": "flop"},
        {"type": "stage", "hand": 1, "stage": "river"},
    ]
    settlement = {
        "type": "settle", "hand": 1, "is_showdown": True,
        "earnings": [-300, 300], "pot": 500,
    }
    history = [
        {"player_id": 1, "action_type": "call", "round": 0,
         "committed": 50, "pot_after": 200},
        {"player_id": 1, "action_type": "raise", "round": 1,
         "stage_bet": 300, "committed": 300, "pot_after": 500},
    ]

    server = features.summarize_server_hand(
        events, settlement, opponent_player_idx=1
    )
    native = features.summarize_native_hand(
        history, [1, 2, 3, 4, 5], opponent_id=1,
        hero_earned=-300, final_pot=500, showdown=True,
    )

    assert len(server) == features.CROSS_HAND_SEQUENCE_DIM
    assert server == native


def test_server_sequences_include_only_strictly_prior_hands() -> None:
    events = [
        {"type": "action", "hand": 1, "player_idx": 1, "action": "fold",
         "stage": "preflop"},
        {"type": "settle", "hand": 1, "earnings": [50, -50],
         "pot": 150, "is_showdown": False},
        {"type": "action", "hand": 2, "player_idx": 1, "action": "allin",
         "amount": 19900, "pot": 20050, "stage": "preflop"},
        {"type": "settle", "hand": 2, "earnings": [-20000, 20000],
         "pot": 40000, "is_showdown": True},
    ]

    sequences = features.server_sequences_by_hand(
        events, opponent_player_idx=1
    )

    assert sequences[1] == []
    assert len(sequences[2]) == 1
    assert len(sequences[3]) == 2
    assert sequences[2][0][14] == 0.0
    assert sequences[3][1][14] == 1.0


def test_sequence_normalization_rejects_wrong_width_and_clamps() -> None:
    raw = [[2.0] * 15 + [-3.0], [1.0, 2.0], "bad"]

    normalized = features.normalize_cross_hand_sequence(raw)

    assert normalized == [[1.0] * 15 + [-1.0]]
