from __future__ import annotations

from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
import time
import types

from national_native import (
    NATIONAL_DECISION_RUNTIME_VERSION,
    NATIVE_PRECOMPUTE_TEMPLATE,
)


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
    donk = line == "donk"
    return {
        "schema_version": 1,
        "runtime_version": NATIONAL_DECISION_RUNTIME_VERSION,
        "decision_id": 1,
        "cards": {
            "hole": (
                [{"suit": 2, "rank": 7}, {"suit": 0, "rank": 10}]
                if donk
                else [{"suit": 0, "rank": 9}, {"suit": 0, "rank": 10}]
            ),
            "board": (
                [
                    {"suit": 0, "rank": 2},
                    {"suit": 1, "rank": 12},
                    {"suit": 2, "rank": 1},
                ]
                if donk
                else [
                    {"suit": 2, "rank": 8},
                    {"suit": 3, "rank": 5},
                    {"suit": 0, "rank": 2},
                    {"suit": 1, "rank": 4},
                ]
            ),
        },
        "hand": {"number": 1, "street": "flop" if donk else "turn"},
        "betting": {
            "pot": 600,
            "hero_stack": 19700,
            "opponent_stack": 19700,
            "hero_street_bet": 0,
            "opponent_street_bet": 0,
            "to_call": 0,
            "spr": 32.833333,
        },
        "legal": {
            "policy_kinds": ["pass", "fold", "allin", "raise"],
            "min_raise_to": 100,
            "max_raise_to": 19699,
        },
        "line": {
            "can_donk": donk,
            "can_delayed_probe": not donk,
            "line_tags": [
                "donk_opportunity"
            ] if donk else [
                "delayed_probe_opportunity",
                "previous_street_checked_through",
            ],
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
        context["opponent"] = {}
        _baseline, decision = _final_decision(policy, context)
        assert decision["kind"] == "raise"
        assert decision["raise_to"] >= context["legal"]["min_raise_to"]
        assert decision["raise_to"] <= context["legal"]["max_raise_to"]

        control = deepcopy(context)
        flag = "can_donk" if line == "donk" else "can_delayed_probe"
        opportunity_tag = (
            "donk_opportunity"
            if line == "donk"
            else "delayed_probe_opportunity"
        )
        control["line"][flag] = False
        control["line"]["line_tags"] = [
            tag
            for tag in control["line"]["line_tags"]
            if tag != opportunity_tag
        ]
        _control_baseline, control_decision = _final_decision(policy, control)
        assert decision != control_decision


def _final_decision(policy, context):
    baseline = policy.get_baseline_decision(context)
    refinements = list(
        policy.iter_decisions(context, baseline, time.monotonic() + 0.10)
    )
    return baseline, (
        refinements[-1].get("decision", refinements[-1])
        if refinements
        else baseline
    )


def test_refinement_prefers_nested_terminal_response_snapshot():
    policy = _policy()
    context = _context(line="donk")
    caller = deepcopy(context)
    caller["opponent"]["terminal_response"]["fold_to_raise"] = 0.1
    _folder_baseline, folder_decision = _final_decision(policy, context)
    _caller_baseline, caller_decision = _final_decision(policy, caller)
    assert folder_decision["kind"] == caller_decision["kind"] == "raise"
    assert folder_decision["raise_to"] > caller_decision["raise_to"]


def _final_raise(policy, context):
    _baseline, decision = _final_decision(policy, context)
    assert decision["kind"] == "raise"
    assert (
        context["legal"]["min_raise_to"]
        <= decision["raise_to"]
        <= context["legal"]["max_raise_to"]
    )
    return decision["raise_to"]


def _sizing_context():
    context = _context(line="donk")
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
