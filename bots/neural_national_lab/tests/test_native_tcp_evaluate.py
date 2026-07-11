from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[3]
TOOL = (
    ROOT
    / "bots"
    / "neural_national_lab"
    / "tools"
    / "native_tcp_evaluate.py"
)


def _load_tool():
    spec = importlib.util.spec_from_file_location("native_tcp_evaluate", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _args(**overrides):
    values = {
        "seeds": "",
        "seed_base": 1_000,
        "seed_stride": None,
        "matches": 3,
        "hands": 70,
        "paired": True,
        "allow_generated_opponent_entry": False,
        "bot_seed_base": 2_000,
        "bot_seed_stride": 10,
        "workers": 4,
        "force_hand": None,
        "force_decision": None,
        "force_action": None,
        "opponent_seed_stride": 10_000_000,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_default_seed_plan_uses_nonoverlapping_match_blocks() -> None:
    tool = _load_tool()
    args = _args()
    seeds = tool._seeds(args)

    assert seeds == [1_000, 1_080, 1_160]
    actual = [
        tool._opponent_deck_seed(seed, opponent, args.opponent_seed_stride)
        for opponent in range(2)
        for seed in seeds
    ]
    assert tool._seed_window_overlaps(actual, hands=70) == []
    assert tool._strength_request_errors(
        args, seeds, opponent_count=2
    ) == []


def test_strength_request_rejects_overlapping_explicit_seeds() -> None:
    tool = _load_tool()
    args = _args(seeds="7000,7001,7002")
    seeds = tool._seeds(args)

    errors = tool._strength_request_errors(args, seeds, opponent_count=1)

    assert any(error.startswith("overlapping_deck_windows:") for error in errors)


def test_strength_result_rejects_short_or_noncompliant_rows() -> None:
    tool = _load_tool()
    payload = {
        "format": "native_tcp_evaluation_v2",
        "execution_mode": "native_tcp",
        "paired": True,
        "requires_native_opponents": True,
        "legacy_debug_wrapper_enabled": False,
        "wrapper_used": False,
        "execution_artifacts": {
            "candidate": {
                "path": "candidate",
                "sha256_before": "a" * 64,
                "sha256_after": "a" * 64,
                "stable": True,
            },
            "opponents": [{
                "path": "opponent",
                "sha256_before": "b" * 64,
                "sha256_after": "b" * 64,
                "stable": True,
            }],
        },
        "rows": [{
            "leg": "paired",
            "hands_played": 138,
            "hand_net_chips": [0] * 69,
            "passed_compliance": False,
            "wrapper_used": True,
            "issues": ["timeout"],
            "candidate_illegal": 1,
            "candidate_timeouts": 1,
            "opponent_illegal": 0,
            "opponent_timeouts": 0,
            "adapter_actions_candidate": 0,
            "adapter_actions_opponent": 0,
            "deck_seed_base": 1_000,
            "legs": [],
        }],
    }

    errors = tool._strength_result_errors(
        payload, expected_rows=1, hands_per_leg=70
    )

    assert "row[0]:short_match" in errors
    assert "row[0]:incomplete_hand_vector" in errors
    assert "row[0]:compliance_failed" in errors
    assert "row[0]:wrapper_used" in errors
    assert "row[0]:candidate_illegal" in errors
