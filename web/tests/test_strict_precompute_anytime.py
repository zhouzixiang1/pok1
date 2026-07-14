from __future__ import annotations

import importlib.util
import random
from pathlib import Path
import sys
import time
import types

import pytest

from national_native import (
    NATIVE_BOT_TEMPLATE,
    NATIVE_PRECOMPUTE_TEMPLATE,
)
from sever.engine.deck import Card
from sever.engine.evaluator import best_hand as sever_best_hand
from sever.engine.evaluator import evaluate_hand as sever_evaluate_hand


ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / "web/core/bootstrap_assets/strict_v1/policy.py"


def _modules():
    precompute = types.ModuleType("precompute")
    exec(
        compile(NATIVE_PRECOMPUTE_TEMPLATE, "precompute.py", "exec"),
        precompute.__dict__,
    )
    sys.modules["precompute"] = precompute
    spec = importlib.util.spec_from_file_location(
        "strict_anytime_policy_test", POLICY_PATH
    )
    assert spec is not None and spec.loader is not None
    policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy)
    native = types.ModuleType("strict_native_template_test")
    native.__file__ = str(ROOT / "national_bot.py")
    exec(compile(NATIVE_BOT_TEMPLATE, "national_bot.py", "exec"), native.__dict__)
    return precompute, policy, native


@pytest.fixture(scope="module")
def modules():
    return _modules()


def _server_cards(ids):
    return [Card(card % 4, card // 4) for card in ids]


def _cid(rank, suit):
    ranks = "23456789TJQKA"
    return ranks.index(rank) * 4 + suit


@pytest.mark.parametrize(
    ("cards", "category"),
    [
        ((("A", 0), ("K", 0), ("Q", 0), ("J", 0), ("T", 0)), 9),
        ((("A", 0), ("A", 1), ("A", 2), ("A", 3), ("2", 0)), 8),
        ((("K", 0), ("K", 1), ("K", 2), ("3", 0), ("3", 1)), 7),
        ((("A", 1), ("J", 1), ("8", 1), ("5", 1), ("2", 1)), 6),
        ((("A", 0), ("2", 1), ("3", 2), ("4", 3), ("5", 0)), 5),
        ((("Q", 0), ("Q", 1), ("Q", 2), ("7", 3), ("2", 0)), 4),
        ((("J", 0), ("J", 1), ("4", 2), ("4", 3), ("A", 0)), 3),
        ((("T", 0), ("T", 1), ("A", 2), ("7", 3), ("2", 0)), 2),
        ((("A", 0), ("J", 1), ("8", 2), ("5", 3), ("2", 0)), 1),
    ],
)
def test_known_five_card_categories(modules, cards, category):
    precompute, _policy, _native = modules
    ids = tuple(_cid(rank, suit) for rank, suit in cards)
    assert precompute.evaluate_five(ids)[0] == category
    assert precompute.evaluate_five(ids) == sever_evaluate_hand(_server_cards(ids))


def test_random_five_and_seven_card_ranks_match_sever(modules):
    precompute, _policy, _native = modules
    rng = random.Random(20260714)
    deck = list(range(52))
    for _index in range(400):
        five = tuple(rng.sample(deck, 5))
        seven = tuple(rng.sample(deck, 7))
        assert precompute.evaluate_five(five) == sever_evaluate_hand(
            _server_cards(five)
        )
        assert precompute.evaluate_seven(seven) == sever_best_hand(
            _server_cards(seven)
        )[0]


def test_precompute_has_complete_compact_tables_and_deck_tools(modules):
    precompute, _policy, _native = modules
    manifest = precompute.PRECOMPUTE_MANIFEST
    assert len(precompute.HOLE_COMBO_FACTS) == 1326
    assert len(precompute.PREFLOP_CLASS_EQUITY) == 169
    assert len(precompute.STRAIGHT_HIGH_BY_MASK) == 8192
    assert len(precompute.FIVE_OF_SEVEN_INDICES) == 21
    assert manifest["hole_class_entries"] == 169
    counts = {}
    for bucket in precompute.HOLE_BUCKET_BY_COMBO.values():
        counts[bucket] = counts.get(bucket, 0) + 1
    assert counts == {
        "premium_pair": 30,
        "small_pair": 48,
        "ace_broadway": 64,
        "broadway": 96,
        "suited_connector": 64,
        "suited_ace": 32,
        "offsuit_ace": 96,
        "suited_other": 176,
        "offsuit_other": 720,
    }
    draw_a = precompute.deterministic_draw(precompute.FULL_DECK, 7, 12345)
    draw_b = precompute.deterministic_draw(precompute.FULL_DECK, 7, 12345)
    assert draw_a == draw_b
    assert len(set(draw_a[0])) == 7


def _context(*, donk=False):
    return {
        "schema_version": 1,
        "decision_id": 19,
        "cards": {
            "hole": [{"suit": 0, "rank": 12}, {"suit": 1, "rank": 11}],
            "board": (
                [{"suit": 2, "rank": 8}, {"suit": 3, "rank": 5}, {"suit": 0, "rank": 2}]
                if donk
                else []
            ),
        },
        "hand": {"number": 7, "street": "flop" if donk else "preflop"},
        "betting": {
            "pot": 1000,
            "hero_stack": 19000,
            "opponent_stack": 19000,
            "effective_stack": 19000,
            "hero_street_bet": 100,
            "opponent_street_bet": 500,
            "to_call": 0 if donk else 400,
            "spr": 19.0,
        },
        "legal": {
            "policy_kinds": ["fold", "pass", "raise", "allin"],
            "min_raise_to": 100 if donk else 1000,
            "max_raise_to": 19099,
        },
        "line": {
            "can_donk": donk,
            "can_delayed_probe": False,
        },
        "opponent": {
            "adaptation_weight": 0.52,
            "rates": {"aggression": 0.7},
            "terminal_response": {
                "adaptation_weight": 0.4875,
                "fold_to_raise": 0.82,
                "fold_to_jam": 0.75,
            },
            "showdown_range": {
                "adaptation_weight": 0.30,
                "selection_scope": "reached_showdown_only",
                "selection_bias_guard": "reach_rate_discount_and_capped_influence",
                "bucket_priors": {
                    "premium_pair": 30 / 1326,
                    "offsuit_other": 720 / 1326,
                },
                "bucket_rates": {
                    "premium_pair": 0.06,
                    "offsuit_other": 0.48,
                },
                "tightness": 0.35,
            },
        },
        "deadline": {"refinement_budget_ms": 100},
    }


def _wire(native, context, decision):
    bot = native.NativeNationalBot("strict-anytime-test")
    try:
        bot._legal_policy_state = lambda: context["legal"]
        bot._socket_safe_fallback_decision = lambda: {"kind": "fold"}
        bot._pass_wire_kind = lambda: "call"
        return bot._decision_to_tcp(decision)[0]
    finally:
        bot.close()


def _final(policy, context, budget):
    baseline = policy.get_baseline_decision(context)
    decision = baseline
    refinements = []
    deadline = time.monotonic() + budget
    for item in policy.iter_decisions(context, baseline, deadline):
        refinements.append(item)
        decision = item.get("decision", item)
    return baseline, decision, refinements


def test_fast_baseline_and_same_shape_table_values_reach_wire(modules):
    precompute, policy, native = modules
    context = _context()
    original = precompute.PREFLOP_CLASS_EQUITY
    started = time.perf_counter()
    try:
        precompute.PREFLOP_CLASS_EQUITY = tuple(0.05 for _ in original)
        low = policy.get_baseline_decision(context)
        precompute.PREFLOP_CLASS_EQUITY = tuple(0.95 for _ in original)
        high = policy.get_baseline_decision(context)
    finally:
        precompute.PREFLOP_CLASS_EQUITY = original
    assert time.perf_counter() - started < 0.25
    assert len(original) == 169
    assert low == {"kind": "fold"}
    assert high["kind"] == "raise"
    assert _wire(native, context, low) == "fold"
    assert _wire(native, context, high).startswith("raise ")


def test_absolute_deadline_bounds_work_and_budget_changes_final_wire(modules):
    _precompute, policy, native = modules
    context = _context(donk=True)
    baseline = policy.get_baseline_decision(context)
    started = time.monotonic()
    assert list(policy.iter_decisions(context, baseline, started - 1.0)) == []
    assert time.monotonic() - started < 0.05

    short_baseline, short_final, short_rows = _final(policy, context, 0.0)
    before = time.monotonic()
    long_baseline, long_final, long_rows = _final(policy, context, 0.12)
    elapsed = time.monotonic() - before
    assert short_baseline == long_baseline
    assert short_rows == []
    assert long_rows
    assert max(row.get("sample_count", 0) for row in long_rows) > 0
    assert elapsed < 0.18
    assert long_final != short_final
    assert _wire(native, context, long_final) != _wire(native, context, short_final)


def test_longer_budget_performs_more_deterministic_samples(modules):
    _precompute, policy, _native = modules
    context = _context(donk=True)
    _base, _short, short_rows = _final(policy, context, 0.035)
    _base, _long, long_rows = _final(policy, context, 0.14)
    short_samples = max((row.get("sample_count", 0) for row in short_rows), default=0)
    long_samples = max((row.get("sample_count", 0) for row in long_rows), default=0)
    assert short_samples > 0
    assert long_samples > short_samples


def test_showdown_bucket_weights_require_exact_selection_guard(modules):
    _precompute, policy, _native = modules
    guarded = _context(donk=True)
    unguarded = _context(donk=True)
    unguarded["opponent"]["showdown_range"]["selection_scope"] = "unconditional"
    unguarded["opponent"]["showdown_range"]["selection_bias_guard"] = "missing"

    guarded_projection = policy._opponent_posterior(guarded)
    unguarded_projection = policy._opponent_posterior(unguarded)
    assert guarded_projection["showdown_guarded"] is True
    assert guarded_projection["bucket_multipliers"]
    assert unguarded_projection["showdown_guarded"] is False
    assert unguarded_projection["showdown_weight"] == 0.0
    assert unguarded_projection["bucket_multipliers"] == {}


def test_terminal_river_overcall_changes_typed_and_wire_intent(modules):
    _precompute, policy, native = modules
    low_overcall = _context()
    low_overcall["hand"]["street"] = "river"
    low_overcall["betting"].update({"pot": 1000, "to_call": 1000})
    low_overcall["legal"]["min_raise_to"] = 2000
    low_overcall["opponent"]["terminal_response"].update({
        "adaptation_weight": 0.65,
        "fold_to_raise": 0.60,
        "fold_to_jam": 0.50,
        "river_overcall": 0.10,
    })
    high_overcall = {
        **low_overcall,
        "opponent": {
            **low_overcall["opponent"],
            "terminal_response": {
                **low_overcall["opponent"]["terminal_response"],
                "river_overcall": 0.90,
            },
        },
    }
    bluff = policy._decision_from_equity(
        low_overcall, 0.25, 0.95, 10_000
    )
    disciplined = policy._decision_from_equity(
        high_overcall, 0.25, 0.95, 10_000
    )
    assert bluff["kind"] == "raise"
    assert disciplined == {"kind": "fold"}
    assert _wire(native, low_overcall, bluff).startswith("raise ")
    assert _wire(native, high_overcall, disciplined) == "fold"
