"""Tests for precommit-attempt status injection in _format_checkpoint_info.

Covers the GLOBAL INTERFACE CONTRACT Task B:
  - precommit_attempt == 0 -> no PRECOMMIT STATUS line
  - precommit_attempt in (0, MAX) -> status line with last-result record
  - precommit_attempt >= MAX -> HARD LIMIT line
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))

from orchestrator_context import _format_checkpoint_info
from evolution_infra import MAX_PRECOMMIT_RETRIES


def _render(checkpoint):
    lines = []
    _format_checkpoint_info(checkpoint, lines)
    return "\n".join(lines)


def _base_checkpoint(**overrides):
    cp = {
        "next_v": 145,
        "source_v": 144,
        "stage": "critic_checked",
        "generation_attempt": 0,
    }
    cp.update(overrides)
    return cp


def test_precommit_attempt_zero_no_status():
    out = _render(_base_checkpoint(precommit_attempt=0))
    assert "PRECOMMIT STATUS" not in out
    assert "PRECOMMIT HARD LIMIT" not in out


def test_precommit_attempt_partial_with_gate_result():
    cp = _base_checkpoint(
        precommit_attempt=2,
        gate_results={
            "precommit_eval": {
                "total_wins": 8,
                "total_losses": 11,
                "total_draws": 1,
                "n_opponents": 4,
            }
        },
    )
    out = _render(cp)
    assert f"PRECOMMIT STATUS: 2/{MAX_PRECOMMIT_RETRIES}" in out
    assert "8W-11L-1D vs 4 opps" in out
    assert "SAME result" in out
    # Below the hard limit -> no HARD LIMIT line
    assert "HARD LIMIT" not in out


def test_precommit_attempt_at_hard_limit():
    cp = _base_checkpoint(
        precommit_attempt=MAX_PRECOMMIT_RETRIES,
        gate_results={
            "precommit_eval": {
                "total_wins": 3,
                "total_losses": 12,
                "total_draws": 0,
                "n_opponents": 3,
            }
        },
    )
    out = _render(cp)
    assert f"PRECOMMIT STATUS: {MAX_PRECOMMIT_RETRIES}/{MAX_PRECOMMIT_RETRIES}" in out
    assert "HARD LIMIT" in out


def test_precommit_gate_missing_fields_defensive():
    """If gate_results.precommit_eval exists but lacks the count fields,
    defaults render as 0W-0L-0D and the status line still appears."""
    cp = _base_checkpoint(
        precommit_attempt=1,
        gate_results={"precommit_eval": {}},
    )
    out = _render(cp)
    assert "PRECOMMIT STATUS: 1/" in out
    assert "0W-0L-0D" in out


def test_precommit_gate_derives_n_opponents_from_list():
    """If n_opponents is absent, fall back to len(opponents)."""
    cp = _base_checkpoint(
        precommit_attempt=1,
        gate_results={
            "precommit_eval": {
                "total_wins": 5,
                "total_losses": 5,
                "total_draws": 0,
                "opponents": ["national_v143", "national_v144"],
            }
        },
    )
    out = _render(cp)
    assert "5W-5L-0D vs 2 opps" in out
