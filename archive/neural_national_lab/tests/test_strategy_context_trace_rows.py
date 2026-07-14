from __future__ import annotations

from pathlib import Path
import sys

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import strategy_context_trace_rows as trace_rows  # noqa: E402


def _decision(*, action: int = 200, value: float = 0.25) -> dict:
    return {
        "type": "decision",
        "hand": 7,
        "hand_decision_index": 2,
        "decision_serial": 19,
        "final_action": action,
        "strategy_context": {
            "schema": "v140_strategy_context_v1",
            "dim": 66,
            "available": True,
            "features": [value] * 66,
            "raw": {"weighted_win_rate": 0.61, "range_summary": [0.1] * 6},
        },
    }


def _row(*, action: int = 200) -> dict:
    return {
        "hand": 7,
        "hand_decision_index": 2,
        "decision_serial": 19,
        "rule_final": action,
    }


def test_attach_binds_context_to_exact_decision_and_action() -> None:
    rows, report = trace_rows.attach_strategy_context([_row()], [_decision()])

    assert len(rows[0]["strategy_context_features"]) == 66
    assert len(rows[0]["strategy_context_sha256"]) == 64
    assert rows[0]["strategy_context_value_head_only"] is True
    assert rows[0]["strategy_context_response_head_allowed"] is False
    assert report == {
        "schema": "strategy_context_trace_join_v1",
        "rows": 1,
        "trace_decisions": 1,
        "unique_contexts": 1,
        "strategy_context_schema": "v140_strategy_context_v1",
        "strategy_context_dim": 66,
        "value_head_only": True,
        "response_head_allowed": False,
    }


def test_attach_rejects_action_or_decision_mismatch() -> None:
    with pytest.raises(ValueError, match="rule action disagrees"):
        trace_rows.attach_strategy_context([_row(action=300)], [_decision()])
    bad = _row()
    bad["decision_serial"] = 20
    with pytest.raises(ValueError, match="no matching"):
        trace_rows.attach_strategy_context([bad], [_decision()])


@pytest.mark.parametrize("value", [float("nan"), -0.1, 1.1, "bad"])
def test_attach_rejects_malformed_strategy_features(value) -> None:
    with pytest.raises(ValueError, match="strategy context feature"):
        trace_rows.attach_strategy_context(
            [_row()], [_decision(value=value)]
        )


def test_attach_rejects_duplicate_trace_keys() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        trace_rows.attach_strategy_context(
            [_row()], [_decision(), _decision()]
        )
