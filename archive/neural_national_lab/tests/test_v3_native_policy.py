from __future__ import annotations

from pathlib import Path
import sys


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import v3_native_policy as policy  # noqa: E402


def _request() -> dict:
    profile = {
        "confidence": 0.5,
        "actions_total_norm": 0.25,
        "fold_rate": 0.2,
        "call_rate": 0.3,
        "check_rate": 0.2,
        "raise_rate": 0.2,
        "allin_rate": 0.1,
        "aggression": 0.3,
        "preflop_actions_norm": 0.2,
        "preflop_raise_rate": 0.25,
        "postflop_actions_norm": 0.1,
        "postflop_raise_rate": 0.2,
    }
    return {
        "num_players": 2,
        "dealer_id": 0,
        "my_id": 0,
        "my_chips": 19_950,
        "opponent_chips": 19_900,
        "my_cards": [0, 5],
        "public_cards": [],
        "history": [],
        "hand": 0,
        "max_hand": 70,
        "remaining_hands": 70,
        "total_win_chips": [0, 0],
        "opponent_showdowns": [],
        "cross_hand_sequence": [],
        "opponent_profile": profile,
        "my_stage_bet": 50,
        "opponent_stage_bet": 100,
        "pot": 150,
        "to_call": 50,
        "opponent_allin": False,
    }


def _state() -> dict:
    return {
        "round": 0,
        "round_bet": 100,
        "round_raise": 100,
        "min_raise_action": 151,
        "my_round_bet": 50,
        "to_call": 50,
        "pot": 150,
        "stacks": [19_950, 19_900],
        "opponent_allin": False,
    }


class _Runtime:
    policy = {
        "margin": 1.0,
        "hand_weight": 0.25,
        "tail_weight": 0.25,
        "match_weight": 0.5,
        "response_weight": 0.1,
        "use_lower": True,
    }

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.value_inputs = None
        self.response_inputs = []

    def predict_values(self, **inputs):
        if self.fail:
            raise RuntimeError("inference failed")
        self.value_inputs = inputs
        values = [0.0] * 6
        values[3] = 100.0
        return {
            field: {
                "mean": list(values),
                "lower": list(values),
                "member_mean_std": [0.0] * 6,
            }
            for field in (
                "delta_vs_rule", "tail_delta_vs_rule", "match_delta_vs_rule"
            )
        }

    def predict_response(self, **inputs):
        self.response_inputs.append(inputs)
        return {
            "probabilities": {
                "fold": 0.5,
                "check": 0.0,
                "call": 0.4,
                "raise": 0.1,
                "allin": 0.0,
            },
            "normalized_entropy": 0.5,
            "aggressive_increment_pot_log": 0.2,
            "aggressive_stack_fraction": 0.1,
        }

    @staticmethod
    def response_signal(response, **kwargs):
        return 5.0

    @staticmethod
    def select_candidate(values, candidates):
        selected = next(item for item in candidates if item["label_id"] == 3)
        return {**selected, "prediction": {"score": 100.0}}


def test_candidate_actions_match_collector_labels_and_stage_totals() -> None:
    candidates = policy.candidate_actions(_request(), _state(), 0)

    assert [item["label_id"] for item in candidates] == [0, 3, 4, 5]
    assert len({item["label_id"] for item in candidates}) == len(candidates)
    assert all(
        item["action"] in {-2, -1, 0} or item["action"] > _state()["round_bet"]
        for item in candidates
    )


def test_native_policy_uses_observable_context_and_returns_safe_total() -> None:
    runtime = _Runtime()
    native = policy.NativeV3Policy(runtime)
    captured = {
        "schema": policy.STRATEGY_CONTEXT_SCHEMA,
        "dim": policy.STRATEGY_CONTEXT_DIM,
        "available": True,
        "features": [0.25] * policy.STRATEGY_CONTEXT_DIM,
        "raw": {"weighted_win_rate": 0.6},
    }

    action = native.advise(_request(), _state(), 0, captured)

    assert action > _state()["round_bet"]
    assert runtime.value_inputs is not None
    assert len(runtime.value_inputs["state"]) == 81
    assert len(runtime.value_inputs["profile"]) == 12
    assert len(runtime.value_inputs["rule_action"]) == 6
    assert len(runtime.value_inputs["strategy_context"]) == 66
    assert runtime.value_inputs["strategy_context"] == [0.25] * 66
    assert runtime.response_inputs
    assert len(runtime.response_inputs[0]["hero_action"]) == 10
    assert native.last_decision["used"] is True


def test_native_policy_inference_failure_returns_sanitized_rule() -> None:
    native = policy.NativeV3Policy(_Runtime(fail=True))

    assert native.advise(_request(), _state(), 0, {}) == 0
    assert native.last_decision["used"] is False
    assert "inference failed" in native.last_decision["error"]


def test_stage_total_sanitizer_never_reinterprets_total_as_delta() -> None:
    state = _state()

    assert policy.sanitize_stage_total(201, state, 19_950) == 201
    assert policy.sanitize_stage_total(100, state, 19_950) == 0
    assert policy.sanitize_stage_total(50_000, state, 19_950) == -2
