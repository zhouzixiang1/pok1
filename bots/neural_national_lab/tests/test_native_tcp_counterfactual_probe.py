from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
TOOL = ROOT / "bots" / "neural_national_lab" / "tools" / "native_tcp_counterfactual_probe.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("native_tcp_counterfactual_probe", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_uniform_decision_sampling_spreads_across_window() -> None:
    tool = _load_tool()
    eligible = [{"hand": hand} for hand in range(1, 11)]

    assert [row["hand"] for row in tool._sample_decisions(eligible, 3, "uniform")] == [2, 6, 9]
    assert [row["hand"] for row in tool._sample_decisions(eligible, 3, "first")] == [1, 2, 3]


def test_rotating_alternatives_cover_every_non_rule_action_class() -> None:
    tool = _load_tool()
    row = {
        "final_action": 0,
        "request": {
            "my_chips": 19950,
            "my_stage_bet": 50,
            "pot": 150,
            "to_call": 50,
        },
        "state": {
            "my_round_bet": 50,
            "to_call": 50,
            "pot": 150,
            "min_raise_action": 100,
        },
    }

    alternatives = tool._legal_alternatives(row)
    labels = {tool._label_id(action, row["request"]) for action in alternatives}
    rotated_labels = {
        tool._label_id(action, row["request"])
        for rotation in range(len(alternatives))
        for action in tool._rotate_alternatives(alternatives, 2, rotation)
    }

    assert labels == {0, 2, 3, 4, 5}
    assert rotated_labels == labels


def test_force_confirmation_requires_matching_forced_trace() -> None:
    tool = _load_tool()
    result = {
        "per_player": {
            "Candidate": {
                "native": {
                    "decision_trace": [{
                        "type": "decision",
                        "hand": 4,
                        "hand_decision_index": 2,
                        "final_action": 500,
                        "forced": True,
                    }]
                }
            }
        }
    }

    assert tool._force_confirmed(result, "Candidate", hand=4, decision_index=2, action=500)
    assert not tool._force_confirmed(result, "Candidate", hand=4, decision_index=2, action=600)


def test_behavior_rows_use_immediate_post_action_event() -> None:
    tool = _load_tool()
    decision = {
        "type": "decision",
        "hand": 1,
        "hand_decision_index": 0,
        "decision_serial": 0,
        "final_action": 200,
        "request": {
            "num_players": 2,
            "dealer_id": 0,
            "my_id": 0,
            "my_chips": 19950,
            "my_cards": [0, 4],
            "public_cards": [],
            "history": [],
            "hand": 0,
            "max_hand": 70,
            "opponent_profile": {},
        },
        "state": {"pot": 150, "to_call": 50},
    }
    baseline = {
        "bot_b": "Opponent",
        "deck_seed_base": 10,
        "bot_seed_base": 20,
        "events": [
            {"type": "action", "player_idx": 0, "action": "raise", "amount": 200,
             "stage": "preflop", "hand": 1},
            {"type": "action", "player_idx": 1, "action": "fold",
             "stage": "preflop", "hand": 1},
            {"type": "settle", "hand": 1},
        ],
    }

    rows = tool._behavior_response_rows(baseline, [decision])

    assert len(rows) == 1
    assert rows[0]["hero_action"] == 200
    assert rows[0]["opponent_action"] == "fold"
    assert rows[0]["opponent_action_label_id"] == 0
    assert rows[0]["cross_hand_sequence_schema"] == "public_opponent_hand_v1"
    assert rows[0]["cross_hand_sequence"] == []
