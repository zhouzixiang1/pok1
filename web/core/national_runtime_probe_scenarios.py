"""Deterministic local states used by the national runtime architecture probe."""

from __future__ import annotations

import hashlib
import json


RUNTIME_PROBE_SCENARIO_VERSION = 2

DECISION_SCENARIOS = (
    {
        "id": "preflop_sb_premium",
        "stage": "preflop",
        "is_sb": True,
        "my_cards": [48, 45],
        "public_cards": [],
        "history": [],
        "pot": 150,
        "my_stage_bet": 50,
        "opponent_stage_bet": 100,
    },
    {
        "id": "preflop_bb_facing_raise",
        "stage": "preflop",
        "is_sb": False,
        "my_cards": [44, 36],
        "public_cards": [],
        "history": [
            {
                "round": 0,
                "player_id": 0,
                "action": 300,
                "action_type": "raise",
                "stage_bet": 300,
            }
        ],
        "pot": 400,
        "my_stage_bet": 100,
        "opponent_stage_bet": 300,
    },
    {
        "id": "flop_top_pair_facing_bet",
        "stage": "flop",
        "is_sb": True,
        "my_cards": [48, 32],
        "public_cards": [49, 25, 10],
        "history": [
            {
                "round": 1,
                "player_id": 1,
                "action": 350,
                "action_type": "raise",
                "stage_bet": 350,
            }
        ],
        "pot": 900,
        "my_stage_bet": 0,
        "opponent_stage_bet": 350,
    },
    {
        "id": "flop_first_to_act_no_bet",
        "stage": "flop",
        "is_sb": False,
        "my_cards": [44, 40],
        "public_cards": [32, 21, 6],
        "history": [],
        "pot": 600,
        "my_stage_bet": 0,
        "opponent_stage_bet": 0,
    },
    {
        "id": "turn_responding_to_check",
        "stage": "turn",
        "is_sb": True,
        "my_cards": [48, 28],
        "public_cards": [49, 25, 10, 4],
        "history": [
            {
                "round": 2,
                "player_id": 1,
                "action": 0,
                "action_type": "check",
            }
        ],
        "pot": 1200,
        "my_stage_bet": 0,
        "opponent_stage_bet": 0,
    },
    {
        "id": "river_facing_large_bet",
        "stage": "river",
        "is_sb": True,
        "my_cards": [36, 20],
        "public_cards": [49, 25, 10, 4, 31],
        "history": [
            {
                "round": 3,
                "player_id": 1,
                "action": 4200,
                "action_type": "raise",
                "stage_bet": 4200,
            }
        ],
        "pot": 7000,
        "my_stage_bet": 0,
        "opponent_stage_bet": 4200,
    },
)


def scenario_bank_digest() -> str:
    payload = {
        "version": RUNTIME_PROBE_SCENARIO_VERSION,
        "scenarios": DECISION_SCENARIOS,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


RUNTIME_PROBE_SCENARIO_DIGEST = scenario_bank_digest()
