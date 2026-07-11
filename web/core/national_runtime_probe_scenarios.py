"""Deterministic local states used by the national runtime architecture probe."""

from __future__ import annotations

import hashlib
import json


RUNTIME_PROBE_SCENARIO_VERSION = 3

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
                "player_id": 1,
                "action": 300,
                "action_type": "raise",
                "stage_bet": 300,
                "committed": 250,
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
                "round": 0,
                "player_id": 0,
                "action": 300,
                "action_type": "raise",
                "stage_bet": 300,
                "committed": 250,
            },
            {
                "round": 0,
                "player_id": 1,
                "action": 0,
                "action_type": "call",
                "stage_bet": 300,
                "committed": 200,
            },
            {
                "round": 1,
                "player_id": 1,
                "action": 350,
                "action_type": "raise",
                "stage_bet": 350,
                "committed": 350,
            }
        ],
        "pot": 950,
        "my_stage_bet": 0,
        "opponent_stage_bet": 350,
    },
    {
        "id": "flop_donk_vs_opponent_pfr",
        "stage": "flop",
        "is_sb": False,
        "my_cards": [44, 40],
        "public_cards": [32, 21, 6],
        "history": [
            {
                "round": 0,
                "player_id": 1,
                "action": 300,
                "action_type": "raise",
                "stage_bet": 300,
                "committed": 250,
            },
            {
                "round": 0,
                "player_id": 0,
                "action": 0,
                "action_type": "call",
                "stage_bet": 300,
                "committed": 200,
            },
        ],
        "pot": 600,
        "my_stage_bet": 0,
        "opponent_stage_bet": 0,
        "expected_hand_runtime": {
            "preflop_aggressor": "opponent",
            "preflop_spot": "bb_vs_raise",
            "can_donk": True,
            "can_delayed_probe": False,
        },
    },
    {
        "id": "flop_donk_control_hero_pfr",
        "stage": "flop",
        "is_sb": False,
        "my_cards": [44, 40],
        "public_cards": [32, 21, 6],
        "history": [
            {
                "round": 0,
                "player_id": 1,
                "action": 0,
                "action_type": "call",
                "stage_bet": 100,
                "committed": 50,
            },
            {
                "round": 0,
                "player_id": 0,
                "action": 300,
                "action_type": "raise",
                "stage_bet": 300,
                "committed": 200,
            },
            {
                "round": 0,
                "player_id": 1,
                "action": 0,
                "action_type": "call",
                "stage_bet": 300,
                "committed": 200,
            },
        ],
        "pot": 600,
        "my_stage_bet": 0,
        "opponent_stage_bet": 0,
        "expected_hand_runtime": {
            "preflop_aggressor": "hero",
            "preflop_spot": "bb_vs_limp",
            "can_donk": False,
            "can_delayed_probe": False,
        },
    },
    {
        "id": "turn_responding_to_check",
        "stage": "turn",
        "is_sb": True,
        "my_cards": [48, 28],
        "public_cards": [49, 25, 10, 4],
        "history": [
            {
                "round": 0,
                "player_id": 0,
                "action": 300,
                "action_type": "raise",
                "stage_bet": 300,
                "committed": 250,
            },
            {
                "round": 0,
                "player_id": 1,
                "action": 0,
                "action_type": "call",
                "stage_bet": 300,
                "committed": 200,
            },
            {
                "round": 1,
                "player_id": 1,
                "action": 0,
                "action_type": "check",
            },
            {
                "round": 1,
                "player_id": 0,
                "action": 0,
                "action_type": "call",
                "committed": 0,
            },
            {
                "round": 2,
                "player_id": 1,
                "action": 0,
                "action_type": "check",
            }
        ],
        "pot": 600,
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
                "round": 0,
                "player_id": 0,
                "action": 300,
                "action_type": "raise",
                "stage_bet": 300,
                "committed": 250,
            },
            {
                "round": 0,
                "player_id": 1,
                "action": 0,
                "action_type": "call",
                "stage_bet": 300,
                "committed": 200,
            },
            {
                "round": 1,
                "player_id": 1,
                "action": 0,
                "action_type": "check",
            },
            {
                "round": 1,
                "player_id": 0,
                "action": 0,
                "action_type": "call",
                "committed": 0,
            },
            {
                "round": 2,
                "player_id": 1,
                "action": 3200,
                "action_type": "raise",
                "stage_bet": 3200,
                "committed": 3200,
            },
            {
                "round": 2,
                "player_id": 0,
                "action": 0,
                "action_type": "call",
                "stage_bet": 3200,
                "committed": 3200,
            },
            {
                "round": 3,
                "player_id": 1,
                "action": 4200,
                "action_type": "raise",
                "stage_bet": 4200,
                "committed": 4200,
            }
        ],
        "pot": 11200,
        "my_stage_bet": 0,
        "opponent_stage_bet": 4200,
    },
    {
        "id": "turn_delayed_probe_vs_opponent_pfr",
        "stage": "turn",
        "is_sb": False,
        "my_cards": [44, 40],
        "public_cards": [32, 21, 6, 13],
        "history": [
            {
                "round": 0,
                "player_id": 1,
                "action": 300,
                "action_type": "raise",
                "stage_bet": 300,
                "committed": 250,
            },
            {
                "round": 0,
                "player_id": 0,
                "action": 0,
                "action_type": "call",
                "stage_bet": 300,
                "committed": 200,
            },
            {
                "round": 1,
                "player_id": 0,
                "action": 0,
                "action_type": "check",
            },
            {
                "round": 1,
                "player_id": 1,
                "action": 0,
                "action_type": "call",
                "committed": 0,
            },
        ],
        "pot": 600,
        "my_stage_bet": 0,
        "opponent_stage_bet": 0,
        "expected_hand_runtime": {
            "preflop_aggressor": "opponent",
            "preflop_spot": "bb_vs_raise",
            "can_donk": False,
            "can_delayed_probe": True,
        },
    },
    {
        "id": "turn_delayed_probe_control_hero_pfr",
        "stage": "turn",
        "is_sb": False,
        "my_cards": [44, 40],
        "public_cards": [32, 21, 6, 13],
        "history": [
            {
                "round": 0,
                "player_id": 1,
                "action": 0,
                "action_type": "call",
                "stage_bet": 100,
                "committed": 50,
            },
            {
                "round": 0,
                "player_id": 0,
                "action": 300,
                "action_type": "raise",
                "stage_bet": 300,
                "committed": 200,
            },
            {
                "round": 0,
                "player_id": 1,
                "action": 0,
                "action_type": "call",
                "stage_bet": 300,
                "committed": 200,
            },
            {
                "round": 1,
                "player_id": 0,
                "action": 0,
                "action_type": "check",
            },
            {
                "round": 1,
                "player_id": 1,
                "action": 0,
                "action_type": "call",
                "committed": 0,
            },
        ],
        "pot": 600,
        "my_stage_bet": 0,
        "opponent_stage_bet": 0,
        "expected_hand_runtime": {
            "preflop_aggressor": "hero",
            "preflop_spot": "bb_vs_limp",
            "can_donk": False,
            "can_delayed_probe": False,
        },
    },
)


LINE_SCENARIO_PAIRS = (
    {
        "dimension": "donk",
        "positive": "flop_donk_vs_opponent_pfr",
        "negative": "flop_donk_control_hero_pfr",
        "flag": "can_donk",
    },
    {
        "dimension": "delayed_probe",
        "positive": "turn_delayed_probe_vs_opponent_pfr",
        "negative": "turn_delayed_probe_control_hero_pfr",
        "flag": "can_delayed_probe",
    },
)

ACTION_PROFILE_SCENARIO_IDS = (
    "preflop_sb_premium",
    "preflop_bb_facing_raise",
    "flop_top_pair_facing_bet",
    "turn_responding_to_check",
    "river_facing_large_bet",
)

TERMINAL_RESPONSE_SCENARIO_IDS = (
    "preflop_sb_premium",
    "preflop_bb_facing_raise",
    "river_facing_large_bet",
)

SHOWDOWN_RANGE_SCENARIO_IDS = (
    "preflop_bb_facing_raise",
    "flop_top_pair_facing_bet",
    "river_facing_large_bet",
)


def scenario_bank_digest() -> str:
    payload = {
        "version": RUNTIME_PROBE_SCENARIO_VERSION,
        "scenarios": DECISION_SCENARIOS,
        "line_pairs": LINE_SCENARIO_PAIRS,
        "action_profile_ids": ACTION_PROFILE_SCENARIO_IDS,
        "terminal_response_ids": TERMINAL_RESPONSE_SCENARIO_IDS,
        "showdown_range_ids": SHOWDOWN_RANGE_SCENARIO_IDS,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


RUNTIME_PROBE_SCENARIO_DIGEST = scenario_bank_digest()
