from __future__ import annotations

import copy
from pathlib import Path
import sys

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import native_tcp_strategy_context_probe as probe  # noqa: E402


def _decision() -> dict:
    return {
        "type": "decision",
        "hand": 3,
        "hand_decision_index": 1,
        "decision_serial": 8,
        "final_action": 200,
        "strategy_context": {
            "schema": "v140_strategy_context_v1",
            "dim": 66,
            "available": True,
            "features": [0.25] * 66,
            "raw": {"weighted_win_rate": 0.6, "range_summary": [0.1] * 6},
        },
    }


def _payload() -> dict:
    return {
        "execution_mode": "native_tcp_counterfactual",
        "baseline_net_chips": 400,
        "rows": [{
            "hand": 3,
            "hand_decision_index": 1,
            "decision_serial": 8,
            "rule_final": 200,
        }],
        "behavior_rows": [{"opponent_action": "call"}],
    }


def _trace() -> dict:
    return {
        "format": "native_tcp_evaluation_v2",
        "rows": [{
            "leg": "forward",
            "passed_compliance": True,
            "candidate_illegal": 0,
            "candidate_timeouts": 0,
            "wrapper_used": False,
            "issues": [],
            "net_chips": 400,
            "deck_seed_base": 5_000_000,
            "bot_seed_base": 1_000_000,
            "hands_played": 70,
            "candidate_native": {"decision_trace": [_decision()]},
        }],
    }


def test_enrich_payload_adds_context_only_to_value_rows() -> None:
    enriched = probe.enrich_payload(_payload(), _trace())

    assert enriched["execution_mode"] == (
        "native_tcp_counterfactual_strategy_context"
    )
    assert len(enriched["rows"][0]["strategy_context_features"]) == 66
    assert "strategy_context_features" not in enriched["behavior_rows"][0]
    assert enriched["strategy_context_join"]["response_head_allowed"] is False
    assert len(enriched["strategy_context_replay"]["trace_sha256"]) == 64


def test_enrich_payload_rejects_replay_drift_or_protocol_failure() -> None:
    drifted = _trace()
    drifted["rows"][0]["net_chips"] = 401
    with pytest.raises(ValueError, match="changed baseline"):
        probe.enrich_payload(_payload(), drifted)

    illegal = _trace()
    illegal["rows"][0]["candidate_illegal"] = 1
    with pytest.raises(ValueError, match="native compliance"):
        probe.enrich_payload(_payload(), illegal)


def test_enrich_payload_does_not_mutate_source_payloads() -> None:
    payload = _payload()
    trace = _trace()
    original_payload = copy.deepcopy(payload)
    original_trace = copy.deepcopy(trace)

    probe.enrich_payload(payload, trace)

    assert payload == original_payload
    assert trace == original_trace
