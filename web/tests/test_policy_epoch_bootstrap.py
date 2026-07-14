from __future__ import annotations

from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
import time
import types

from national_native import NATIVE_PRECOMPUTE_TEMPLATE


ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / "web" / "core" / "bootstrap_assets" / "strict_v1" / "policy.py"


def _policy():
    precompute = types.ModuleType("precompute")
    exec(compile(NATIVE_PRECOMPUTE_TEMPLATE, "precompute.py", "exec"), precompute.__dict__)
    sys.modules["precompute"] = precompute
    spec = importlib.util.spec_from_file_location("strict_v1_policy_test", POLICY_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _context(*, line: str) -> dict:
    return {
        "cards": {
            "hole": [{"suit": 0, "rank": 12}, {"suit": 1, "rank": 8}],
            "board": [{"suit": 2, "rank": 10}, {"suit": 0, "rank": 5}, {"suit": 3, "rank": 2}],
        },
        "betting": {
            "pot": 600,
            "hero_street_bet": 100,
            "opponent_street_bet": 100,
            "to_call": 0,
        },
        "legal": {
            "policy_kinds": ["pass", "fold", "raise"],
            "min_raise_to": 400,
            "max_raise_to": 20000,
        },
        "line": {
            "can_donk": line == "donk",
            "can_delayed_probe": line == "delayed_probe",
        },
        # The nested terminal projection is authoritative; the contradictory
        # flat value exists only to catch accidental compatibility reads.
        "opponent": {
            "confidence": 0.0,
            "fold_to_raise": 0.0,
            "terminal_response": {
                "confidence": 0.8,
                "fold_to_raise": 0.9,
            },
        },
        "deadline": {"refinement_budget_ms": 500},
    }


def test_donk_and_delayed_probe_are_reachable_typed_raise_intents():
    policy = _policy()
    for line in ("donk", "delayed_probe"):
        context = _context(line=line)
        decision = policy.get_baseline_decision(context)
        assert decision["kind"] == "raise"
        assert decision["raise_to"] >= context["legal"]["min_raise_to"]
        # Correct field is hero_street_bet; the sizing must not be based on a
        # retired my_stage_bet alias.
        assert decision["raise_to"] >= 400


def test_refinement_prefers_nested_terminal_response_snapshot():
    policy = _policy()
    context = _context(line="donk")
    baseline = policy.get_baseline_decision(context)
    refinements = list(
        policy.iter_decisions(context, baseline, time.monotonic() + 0.08)
    )
    assert refinements
    decision = refinements[-1].get("decision", refinements[-1])
    assert decision["kind"] == "raise"
    assert decision["raise_to"] > baseline["raise_to"]


def _final_raise(policy, context):
    baseline = policy.get_baseline_decision(context)
    refinements = list(
        policy.iter_decisions(context, baseline, time.monotonic() + 0.08)
    )
    decision = (
        refinements[-1].get("decision", refinements[-1])
        if refinements
        else baseline
    )
    assert decision["kind"] == "raise"
    assert (
        context["legal"]["min_raise_to"]
        <= decision["raise_to"]
        <= context["legal"]["max_raise_to"]
    )
    return decision["raise_to"]


def _sizing_context():
    context = _context(line="donk")
    context["betting"].update({
        "hero_street_bet": 0,
        "opponent_street_bet": 0,
    })
    context["legal"]["min_raise_to"] = 100
    return context


def test_action_terminal_and_guarded_showdown_signals_each_change_raise_to():
    policy = _policy()
    pairs = []

    aggressive = _sizing_context()
    aggressive["opponent"] = {
        "adaptation_weight": 0.52,
        "rates": {"aggression": 0.85},
    }
    passive = deepcopy(aggressive)
    passive["opponent"]["rates"]["aggression"] = 0.12
    pairs.append((aggressive, passive))

    folder = _sizing_context()
    folder["opponent"] = {
        "terminal_response": {
            "adaptation_weight": 0.4875,
            "fold_to_raise": 0.82,
        },
    }
    caller = deepcopy(folder)
    caller["opponent"]["terminal_response"]["fold_to_raise"] = 0.18
    pairs.append((folder, caller))

    tight = _sizing_context()
    tight["opponent"] = {
        "showdown_range": {
            "adaptation_weight": 0.35,
            "selection_scope": "reached_showdown_only",
            "selection_bias_guard": "reach_rate_discount_and_capped_influence",
            "tightness": 0.72,
        },
    }
    loose = deepcopy(tight)
    loose["opponent"]["showdown_range"]["tightness"] = 0.18
    pairs.append((tight, loose))

    for left, right in pairs:
        assert _final_raise(policy, left) != _final_raise(policy, right)


def test_showdown_signal_requires_system_selection_bias_guard():
    policy = _policy()
    tight = _sizing_context()
    tight["opponent"] = {
        "showdown_range": {
            "adaptation_weight": 0.65,
            "selection_scope": "unconditional",
            "selection_bias_guard": "missing",
            "tightness": 0.95,
        },
    }
    loose = deepcopy(tight)
    loose["opponent"]["showdown_range"]["tightness"] = 0.05

    assert _final_raise(policy, tight) == _final_raise(policy, loose)
