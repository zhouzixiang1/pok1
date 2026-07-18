"""Causal clean-room regressions for the LLL-informed strict v143 policy.

The external reference is never imported or read here.  These tests bind the
active runtime's heads-up decision-context vocabulary to the checked-in strict
policy and prove the independently implemented strategy directions.
"""

from __future__ import annotations

import ast
from copy import deepcopy
import importlib.util
import itertools
from pathlib import Path
import sys
import time
import types

import pytest

import national_native
from national_native import NATIVE_PRECOMPUTE_TEMPLATE


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
        "lll_clean_room_strict_policy_test",
        POLICY_PATH,
    )
    assert spec is not None and spec.loader is not None
    policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy)
    return precompute, policy


def _native_module():
    native = types.ModuleType("lll_clean_room_native_template_test")
    native.__file__ = str(ROOT / "national_bot.py")
    exec(
        compile(national_native.NATIVE_BOT_TEMPLATE, "national_bot.py", "exec"),
        native.__dict__,
    )
    return native


@pytest.fixture(scope="module")
def modules():
    return _modules()


def _card(rank, suit):
    return {"suit": suit, "rank": rank}


def _context(*, street="preflop", spot="sb_open", hole=None, board=None):
    board = list(board or [])
    return {
        "schema_version": 1,
        "runtime_version": national_native.NATIONAL_DECISION_RUNTIME_VERSION,
        "decision_id": 17,
        "cards": {
            "hole": list(hole or [_card(12, 0), _card(10, 1)]),
            "board": board,
        },
        "hand": {
            "number": 63,
            "total_hands": 70,
            "remaining_including_current": 8,
            "street": street,
        },
        "betting": {
            "pot": 1000,
            "hero_stack": 19000,
            "opponent_stack": 19000,
            "effective_stack": 19000,
            "hero_street_bet": 0,
            "opponent_street_bet": 400,
            "to_call": 400,
            "spr": 19.0,
        },
        "legal": {
            "policy_kinds": ["fold", "pass", "raise", "allin"],
            "min_raise_to": 800,
            "max_raise_to": 19000,
        },
        "line": {
            "preflop_spot": spot,
            "hero_in_position_postflop": True,
            "can_donk": False,
            "can_delayed_probe": False,
        },
        "opponent": {
            "match_result": {"hero_net_earned": 0},
        },
    }


def _runtime_context(
    *,
    street,
    hole,
    board,
    history,
    pot,
    hero_bet,
    opponent_bet,
    is_sb=True,
    decision_id=17,
):
    """Build strategy input through the active system runtime producer."""

    native = _native_module()
    bot = native.NativeNationalBot("lll-clean-room-runtime-context")
    try:
        bot._my_id = 0
        bot._opponent_id = 1
        bot._is_sb = bool(is_sb)
        bot._hand_num = 63
        bot._stage = street
        bot._my_cards = [
            (int(card["suit"]), int(card["rank"])) for card in hole
        ]
        bot._public_cards = [
            (int(card["suit"]), int(card["rank"])) for card in board
        ]
        bot._history = deepcopy(history)
        bot._pot = int(pot)
        bot._my_stage_bet = int(hero_bet)
        bot._opponent_stage_bet = int(opponent_bet)
        bot._my_chips = 20000 - int(hero_bet)
        bot._opponent_chips = 20000 - int(opponent_bet)
        bot._opponent_tracker.begin_hand(63, opponent_is_sb=not is_sb)
        for record in history:
            if record.get("player_id") == bot._opponent_id:
                bot._opponent_tracker.observe_action(
                    "opponent",
                    street,
                    str(record.get("action_type") or ""),
                    amount=record.get("stage_bet"),
                    committed=record.get("committed"),
                )
        now = time.monotonic()
        return bot._build_decision_context(
            decision_id=decision_id,
            hard_deadline=now + 2.0,
            refinement_deadline=now + 1.8,
        )
    finally:
        bot.close()


def _assert_typed(decision, context):
    kinds = set(context["legal"]["policy_kinds"])
    assert isinstance(decision, dict)
    assert decision.get("kind") in kinds
    if decision["kind"] == "raise":
        assert set(decision) == {"kind", "raise_to"}
        assert isinstance(decision["raise_to"], int)
        assert not isinstance(decision["raise_to"], bool)
        assert context["legal"]["min_raise_to"] <= decision["raise_to"]
        assert decision["raise_to"] <= context["legal"]["max_raise_to"]
    else:
        assert set(decision) == {"kind"}


def test_runtime_classifier_and_policy_share_six_line_states_but_four_actions(modules):
    _precompute, policy = modules
    native = _native_module()
    bot = native.NativeNationalBot("lll-clean-room-context")
    try:
        bot._my_id = 0
        bot._opponent_id = 1
        cases = [
            (True, [], "sb_open"),
            (True, [{"round": 0, "player_id": 0, "action_type": "call"}], "sb_limp"),
            (
                True,
                [
                    {"round": 0, "player_id": 0, "action_type": "raise"},
                    {"round": 0, "player_id": 1, "action_type": "raise"},
                ],
                "sb_vs_reraise",
            ),
            (False, [], "bb_option"),
            (False, [{"round": 0, "player_id": 1, "action_type": "call"}], "bb_vs_limp"),
            (False, [{"round": 0, "player_id": 1, "action_type": "raise"}], "bb_vs_raise"),
        ]
        observed = set()
        for is_sb, history, expected in cases:
            bot._is_sb = is_sb
            bot._history = list(history)
            observed.add(bot._preflop_line()[1])
            assert bot._preflop_line()[1] == expected
        assert observed == set(policy._HEADS_UP_PREFLOP_EQUITY_DELTA)
        assert policy._HEADS_UP_ACTIONABLE_PREFLOP_SPOTS == {
            "sb_open", "sb_vs_reraise", "bb_vs_limp", "bb_vs_raise",
        }
        assert policy._HEADS_UP_LINE_STATE_ONLY_SPOTS == {"sb_limp", "bb_option"}
        assert observed == (
            policy._HEADS_UP_ACTIONABLE_PREFLOP_SPOTS
            | policy._HEADS_UP_LINE_STATE_ONLY_SPOTS
        )
        assert not observed & {"btn", "co", "utg", "hj", "mp"}
    finally:
        bot.close()


def test_runtime_decision_context_produces_the_four_actionable_preflop_spots(
    modules,
):
    _precompute, policy = modules
    hole = [_card(12, 0), _card(10, 1)]
    cases = [
        (True, [], 150, 50, 100, "sb_open"),
        (
            False,
            [{
                "round": 0, "street": "preflop", "player_id": 1,
                "action_type": "call", "committed": 50, "stage_bet": 100,
            }],
            200, 100, 100, "bb_vs_limp",
        ),
        (
            False,
            [{
                "round": 0, "street": "preflop", "player_id": 1,
                "action_type": "raise", "committed": 150, "stage_bet": 200,
            }],
            300, 100, 200, "bb_vs_raise",
        ),
        (
            True,
            [
                {
                    "round": 0, "street": "preflop", "player_id": 0,
                    "action_type": "raise", "committed": 150, "stage_bet": 200,
                },
                {
                    "round": 0, "street": "preflop", "player_id": 1,
                    "action_type": "raise", "committed": 300, "stage_bet": 400,
                },
            ],
            600, 200, 400, "sb_vs_reraise",
        ),
    ]
    observed = set()
    for is_sb, history, pot, hero_bet, opponent_bet, expected in cases:
        context = _runtime_context(
            street="preflop",
            hole=hole,
            board=[],
            history=history,
            pot=pot,
            hero_bet=hero_bet,
            opponent_bet=opponent_bet,
            is_sb=is_sb,
        )
        assert context["line"]["preflop_spot"] == expected
        observed.add(expected)
        _assert_typed(policy.get_baseline_decision(context), context)
    assert observed == policy._HEADS_UP_ACTIONABLE_PREFLOP_SPOTS
    assert not observed & policy._HEADS_UP_LINE_STATE_ONLY_SPOTS


def test_line_state_only_spots_are_explicit_neutral_consumers(modules):
    _precompute, policy = modules
    for spot in policy._HEADS_UP_LINE_STATE_ONLY_SPOTS:
        context = _context(spot=spot)
        assert policy._preflop_spot_adjustment(context) == {
            "equity_delta": 0.0,
            "sizing_delta": 0.0,
        }


def test_checked_in_policy_has_no_retired_opponent_adjustment_helper():
    tree = ast.parse(POLICY_PATH.read_text(encoding="utf-8"))
    top_level_definitions = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_opponent_adjustments" not in top_level_definitions


def test_real_heads_up_spots_reach_distinct_baseline_actions(modules):
    _precompute, policy = modules
    opened = _context(spot="sb_open")
    opened["betting"].update({"pot": 150, "to_call": 50})
    opened["legal"].update({"min_raise_to": 200, "max_raise_to": 20000})
    defended = deepcopy(opened)
    defended["line"]["preflop_spot"] = "bb_vs_raise"
    defended["betting"].update({"pot": 600, "to_call": 400})
    defended["legal"]["min_raise_to"] = 1000

    open_decision = policy.get_baseline_decision(opened)
    defend_decision = policy.get_baseline_decision(defended)

    _assert_typed(open_decision, opened)
    _assert_typed(defend_decision, defended)
    assert open_decision["kind"] == "raise"
    assert defend_decision["kind"] != "raise"
    assert policy._preflop_spot_adjustment(
        _context(spot="unknown")
    ) == {"equity_delta": 0.0, "sizing_delta": 0.0}


def test_late_match_pressure_chases_deficit_and_protects_lead(modules, monkeypatch):
    _precompute, policy = modules
    neutral = _context(street="flop", board=[
        _card(11, 0), _card(6, 1), _card(0, 2),
    ])
    neutral["legal"]["policy_kinds"] = ["fold", "pass"]
    behind = deepcopy(neutral)
    ahead = deepcopy(neutral)
    behind["opponent"]["match_result"]["hero_net_earned"] = -3000
    ahead["opponent"]["match_result"]["hero_net_earned"] = 3000

    behind_profile = policy._match_pressure_adjustment(behind)
    neutral_profile = policy._match_pressure_adjustment(neutral)
    ahead_profile = policy._match_pressure_adjustment(ahead)
    assert behind_profile["equity_delta"] > neutral_profile["equity_delta"]
    assert neutral_profile["equity_delta"] > ahead_profile["equity_delta"]
    assert behind_profile["sizing_delta"] > 0.0 > ahead_profile["sizing_delta"]

    monkeypatch.setattr(policy, "_baseline_equity", lambda _context: 0.27)
    assert policy.get_baseline_decision(behind)["kind"] == "pass"
    assert policy.get_baseline_decision(ahead)["kind"] == "fold"

    early = deepcopy(behind)
    early["hand"]["remaining_including_current"] = 50
    assert policy._match_pressure_adjustment(early) == {
        "equity_delta": 0.0,
        "sizing_delta": 0.0,
        "protect": 0.0,
        "chase": 0.0,
    }


def test_match_pressure_cannot_fold_nuts_or_turn_air_into_a_jam(modules):
    _precompute, policy = modules
    board = [_card(11, 0), _card(7, 0), _card(3, 0), _card(1, 0)]
    nuts = _context(
        street="turn",
        hole=[_card(12, 0), _card(10, 1)],
        board=board,
    )
    nuts["opponent"]["match_result"]["hero_net_earned"] = 10000
    nuts["hand"]["remaining_including_current"] = 1
    air = _context(
        street="turn",
        hole=[_card(5, 1), _card(4, 2)],
        board=board,
    )
    air["opponent"]["match_result"]["hero_net_earned"] = -10000
    air["hand"]["remaining_including_current"] = 1
    assert policy.get_baseline_decision(nuts)["kind"] != "fold"
    assert policy.get_baseline_decision(air)["kind"] != "allin"


def test_board_risk_distinguishes_weak_hand_from_hole_backed_flush(modules):
    _precompute, policy = modules
    board = [_card(11, 0), _card(8, 0), _card(4, 0), _card(1, 0)]
    weak = _context(
        street="turn",
        hole=[_card(10, 1), _card(9, 2)],
        board=board,
    )
    strong = _context(
        street="turn",
        hole=[_card(12, 0), _card(9, 2)],
        board=board,
    )
    weak_profile = policy._board_adjustment(weak)
    strong_profile = policy._board_adjustment(strong)
    assert weak_profile["danger"] == strong_profile["danger"] > 0.0
    assert weak_profile["made_tier"] in {"air", "weak"}
    assert strong_profile["made_tier"] == "nut_like"
    assert weak_profile["equity_delta"] < strong_profile["equity_delta"]
    assert weak_profile["sizing_delta"] < strong_profile["sizing_delta"]


def test_four_flush_board_distinguishes_low_flush_from_the_nut_flush(modules):
    _precompute, policy = modules
    board = [_card(11, 0), _card(8, 0), _card(4, 0), _card(1, 0)]
    low = _context(
        street="turn",
        hole=[_card(0, 0), _card(7, 2)],
        board=board,
    )
    nut = _context(
        street="turn",
        hole=[_card(12, 0), _card(7, 2)],
        board=board,
    )

    low_profile = policy._board_adjustment(low)
    nut_profile = policy._board_adjustment(nut)
    assert low_profile["made_tier"] == "weak"
    assert nut_profile["made_tier"] == "nut_like"
    assert policy._baseline_equity(low) < policy._baseline_equity(nut)
    assert low_profile["sizing_delta"] < nut_profile["sizing_delta"]


def test_public_two_pair_does_not_become_unconditional_strong_value(modules):
    _precompute, policy = modules
    board = [
        _card(12, 0), _card(12, 1), _card(11, 2), _card(11, 3), _card(0, 0),
    ]
    plays_board = _context(
        street="river",
        hole=[_card(1, 1), _card(1, 2)],
        board=board,
    )
    improves = _context(
        street="river",
        hole=[_card(10, 1), _card(1, 2)],
        board=board,
    )
    assert policy._board_adjustment(plays_board)["made_tier"] == "weak"
    assert policy._board_adjustment(improves)["made_tier"] == "strong"
    assert policy._baseline_equity(plays_board) < policy._baseline_equity(improves)


def test_public_quads_are_shared_not_private_nut_authority(modules):
    _precompute, policy = modules
    board = [
        _card(12, 0), _card(12, 1), _card(12, 2), _card(12, 3), _card(11, 0),
    ]
    context = _context(
        street="river",
        hole=[_card(0, 1), _card(1, 2)],
        board=board,
    )
    profile = policy._board_adjustment(context)
    assert profile["made_tier"] == "shared"
    assert policy._baseline_equity(context) == pytest.approx(0.5, abs=0.02)
    assert policy.get_baseline_decision(context)["kind"] != "raise"


def _exact_river_equity_from_frozen_posterior(policy, precompute, context):
    """Independently derive the bounded policy's completed river posterior."""

    cards = context["cards"]
    hole = policy._card_ids(cards["hole"])
    board = policy._card_ids(cards["board"])
    hero_rank = precompute.evaluate_seven((*hole, *board))
    posterior = policy._opponent_posterior(context)
    points = 0.0
    total = 0.0
    for opponent_hole in itertools.combinations(
        precompute.deck_without((*hole, *board)),
        2,
    ):
        opponent_rank = precompute.evaluate_seven((*opponent_hole, *board))
        point = (
            1.0 if hero_rank > opponent_rank
            else 0.5 if hero_rank == opponent_rank
            else 0.0
        )
        weight = policy._opponent_sample_weight(
            posterior,
            opponent_hole,
            board,
        )
        points += weight * point
        total += weight
    return points / total


def test_bounded_river_baseline_defers_full_enumeration_to_refinement(
    modules,
    monkeypatch,
):
    """A river baseline keeps defensive signal without visiting all 990 holes."""

    precompute, policy = modules
    two_pair_board = [
        _card(12, 0), _card(12, 1), _card(11, 2), _card(11, 3), _card(0, 0),
    ]
    crushed = _context(
        street="river",
        hole=[_card(1, 1), _card(1, 2)],
        board=two_pair_board,
    )

    draw_counts = []
    evaluation_calls = []
    original_draw = precompute.deterministic_draw
    original_evaluate = precompute.evaluate_seven

    def counted_draw(deck, count, state):
        draw_counts.append(count)
        return original_draw(deck, count, state)

    def counted_evaluate(cards):
        evaluation_calls.append(tuple(cards))
        return original_evaluate(cards)

    monkeypatch.setattr(precompute, "deterministic_draw", counted_draw)
    monkeypatch.setattr(precompute, "evaluate_seven", counted_evaluate)
    started = time.perf_counter()
    crushed_baseline = policy.get_baseline_decision(crushed)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.10
    assert draw_counts == [2] * policy.BASELINE_RIVER_SAMPLES
    assert len(evaluation_calls) == 2 * policy.BASELINE_RIVER_SAMPLES
    assert crushed_baseline == {"kind": "fold"}

    straight_board = [
        _card(0, 0), _card(1, 1), _card(2, 2), _card(3, 3), _card(4, 0),
    ]
    shared = _context(
        street="river",
        hole=[_card(11, 1), _card(10, 2)],
        board=straight_board,
    )
    shared["betting"].update({
        "pot": 1000,
        "opponent_street_bet": 900,
        "to_call": 900,
    })
    shared["legal"]["min_raise_to"] = 1800
    draw_counts.clear()
    evaluation_calls.clear()
    shared_baseline = policy.get_baseline_decision(shared)
    assert draw_counts == [2] * policy.BASELINE_RIVER_SAMPLES
    assert len(evaluation_calls) == 2 * policy.BASELINE_RIVER_SAMPLES
    assert shared_baseline == {"kind": "fold"}

    # The full river posterior is still consumed before a normal refinement
    # deadline.  Public-board / dominated spots must end as folds after the
    # fixed C(45, 2) enumeration, not by blocking the baseline publication.
    for context, baseline in (
        (crushed, crushed_baseline),
        (shared, shared_baseline),
    ):
        exact_equity = _exact_river_equity_from_frozen_posterior(
            policy,
            precompute,
            context,
        )
        decisions = []
        original_decision = policy._decision_from_equity

        def capture_decision(*args, **kwargs):
            decisions.append((args[1], args[2], args[3]))
            return original_decision(*args, **kwargs)

        monkeypatch.setattr(policy, "_decision_from_equity", capture_decision)
        rows = list(policy.iter_decisions(
            context,
            baseline,
            time.monotonic() + 3.0,
        ))
        monkeypatch.setattr(policy, "_decision_from_equity", original_decision)
        assert rows
        assert rows[-1]["complete"] is True
        assert rows[-1]["sample_count"] == 990
        assert decisions[-1][0] == pytest.approx(exact_equity)
        assert decisions[-1][1:] == (1.0, 990)
        assert rows[-1]["decision"] == original_decision(
            context,
            exact_equity,
            confidence=1.0,
            samples=990,
        ) == {"kind": "fold"}


def test_bounded_turn_baseline_handles_public_two_pair_and_quads(modules):
    _precompute, policy = modules
    public_two_pair = [
        _card(12, 0), _card(12, 1), _card(11, 2), _card(11, 3),
    ]
    low_two_pair = _context(
        street="turn",
        hole=[_card(1, 1), _card(1, 2)],
        board=public_two_pair,
    )
    high_two_pair = _context(
        street="turn",
        hole=[_card(10, 1), _card(1, 2)],
        board=public_two_pair,
    )
    low_equity = policy._baseline_equity(low_two_pair)
    high_equity = policy._baseline_equity(high_two_pair)
    assert low_equity < 0.27
    assert high_equity > low_equity + 0.15
    assert policy.get_baseline_decision(low_two_pair) == {"kind": "fold"}
    assert policy.get_baseline_decision(high_two_pair)["kind"] != "fold"

    public_quads = [
        _card(12, 0), _card(12, 1), _card(12, 2), _card(12, 3),
    ]
    low_quads = _context(
        street="turn",
        hole=[_card(0, 1), _card(1, 2)],
        board=public_quads,
    )
    high_quads = _context(
        street="turn",
        hole=[_card(11, 1), _card(10, 2)],
        board=public_quads,
    )
    started = time.perf_counter()
    low_quads_equity = policy._baseline_equity(low_quads)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.25
    assert low_quads_equity < 0.27
    assert policy._baseline_equity(high_quads) > 0.75
    assert policy.get_baseline_decision(low_quads) == {"kind": "fold"}
    assert policy.get_baseline_decision(high_quads)["kind"] != "fold"


def test_raise_ev_uses_both_players_exact_marginal_commitments(modules):
    _precompute, policy = modules
    context = _context(street="flop", board=[
        _card(11, 0), _card(6, 1), _card(0, 2),
    ])
    context["betting"].update({
        "pot": 1000,
        "hero_street_bet": 0,
        "opponent_street_bet": 400,
        "hero_stack": 19000,
        "opponent_stack": 19000,
        "to_call": 400,
    })
    commitments = policy._matched_showdown_commitments(context, 800)
    assert commitments == {
        "contest_pot": 1000.0,
        "hero_extra": 800.0,
        "opponent_extra": 400.0,
    }
    assert policy._called_showdown_ev(context, 800, 0.5) == 300.0
    # The retired symmetric formula would have invented another 400 chips of
    # opponent contribution and returned 500 here.
    assert policy._called_showdown_ev(context, 800, 0.5) != (
        0.5 * (1000 + 2 * 800) - 800
    )

    covered = deepcopy(context)
    covered["betting"]["opponent_stack"] = 600
    covered_commitments = policy._matched_showdown_commitments(covered, 19000)
    assert covered_commitments["hero_extra"] == 1000.0
    assert covered_commitments["opponent_extra"] == 600.0
    assert policy._called_showdown_ev(covered, 19000, 0.5) == 300.0


def test_short_stack_call_excludes_unmatched_opponent_chips(modules, monkeypatch):
    _precompute, policy = modules
    context = _context(street="flop", board=[])
    context["betting"].update({
        "pot": 2000,
        "hero_stack": 600,
        "opponent_stack": 19000,
        "hero_street_bet": 0,
        "opponent_street_bet": 1000,
        "to_call": 1000,
        "spr": 0.3,
    })
    context["legal"]["policy_kinds"] = ["fold", "pass"]
    commitments = policy._matched_showdown_commitments(context, 1000)
    assert commitments == {
        "contest_pot": 1600.0,
        "hero_extra": 600.0,
        "opponent_extra": 0.0,
    }
    assert policy._called_showdown_ev(context, 1000, 0.30) == pytest.approx(60.0)
    assert policy._effective_call_pot_odds(context) == pytest.approx(600 / 2200)
    assert policy._decision_from_equity(
        context, 0.30, confidence=1.0, samples=4096
    ) == {"kind": "pass"}

    monkeypatch.setattr(policy, "_baseline_equity", lambda _context: 0.30)
    assert policy.get_baseline_decision(context) == {"kind": "pass"}
    monkeypatch.setattr(policy, "_baseline_equity", lambda _context: 0.20)
    assert policy.get_baseline_decision(context) == {"kind": "fold"}


def test_current_public_pressure_conditions_the_sampled_range(modules):
    precompute, policy = modules
    hole = [_card(10, 1), _card(9, 2)]
    board = [
        _card(11, 0), _card(8, 1), _card(5, 2), _card(3, 3), _card(1, 0),
    ]
    checked = _runtime_context(
        street="river",
        hole=hole,
        board=board,
        history=[{
            "round": 3,
            "street": "river",
            "player_id": 1,
            "action_type": "check",
            "committed": 0,
            "stage_bet": 0,
        }],
        pot=2000,
        hero_bet=0,
        opponent_bet=0,
    )
    raised = _runtime_context(
        street="river",
        hole=hole,
        board=board,
        history=[{
            "round": 3,
            "street": "river",
            "player_id": 1,
            "action_type": "raise",
            "committed": 1800,
            "stage_bet": 1800,
        }],
        pot=3800,
        hero_bet=0,
        opponent_bet=1800,
    )

    assert checked["line"]["current_street"]["actions"][-1]["actor"] == "opponent"
    assert checked["line"]["current_street"]["actions"][-1]["action"] == "check"
    assert raised["line"]["current_street"]["actions"][-1]["action"] == "raise"
    assert raised["betting"]["to_call"] == 1800

    checked_posterior = policy._opponent_posterior(checked)
    raised_posterior = policy._opponent_posterior(raised)
    assert raised_posterior["line_strength_tilt"] > checked_posterior[
        "line_strength_tilt"
    ]
    assert raised_posterior["range_strength"] < checked_posterior[
        "range_strength"
    ]
    aces = (
        precompute.card_id(0, 12),
        precompute.card_id(1, 12),
    )
    trash = (
        precompute.card_id(0, 5),
        precompute.card_id(1, 0),
    )
    raised_ratio = (
        policy._opponent_sample_weight(raised_posterior, aces)
        / policy._opponent_sample_weight(raised_posterior, trash)
    )
    checked_ratio = (
        policy._opponent_sample_weight(checked_posterior, aces)
        / policy._opponent_sample_weight(checked_posterior, trash)
    )
    assert raised_ratio > checked_ratio


def test_air_never_uses_a_fixed_bluff_and_draw_bluffs_are_deterministically_mixed(
    modules,
):
    _precompute, policy = modules
    air = _context(
        street="flop",
        hole=[_card(5, 1), _card(0, 2)],
        board=[_card(11, 0), _card(8, 1), _card(3, 2)],
    )
    air["betting"].update({"to_call": 0, "opponent_street_bet": 0})
    air["line"].update({
        "responding_to_check": False,
        "line_tags": [],
        "current_street": {"actions": []},
    })
    for decision_id in range(64):
        candidate = deepcopy(air)
        candidate["decision_id"] = decision_id
        assert policy._polarized_raise_fraction(candidate, 0.30) is None
        decision = policy._decision_from_equity(
            candidate, 0.30, confidence=1.0, samples=4096
        )
        assert decision["kind"] != "raise"

    draw = deepcopy(air)
    draw["cards"] = {
        "hole": [_card(12, 0), _card(9, 2)],
        "board": [_card(11, 0), _card(8, 0), _card(4, 0)],
    }
    mixed = []
    for decision_id in range(128):
        candidate = deepcopy(draw)
        candidate["decision_id"] = decision_id
        mixed.append(policy._polarized_raise_fraction(candidate, 0.30))
    bluff_count = sum(value is not None for value in mixed)
    assert 10 <= bluff_count <= 55
    assert mixed == [
        policy._polarized_raise_fraction(
            {**deepcopy(draw), "decision_id": decision_id}, 0.30
        )
        for decision_id in range(128)
    ]

    structural_air = deepcopy(air)
    structural_air["line"]["can_donk"] = True
    structural = []
    for decision_id in range(128):
        candidate = deepcopy(structural_air)
        candidate["decision_id"] = decision_id
        structural.append(policy._polarized_raise_fraction(candidate, 0.30))
    structural_count = sum(value is not None for value in structural)
    assert 2 <= structural_count <= 25
    assert structural_count < 128

    river = deepcopy(draw)
    river["hand"]["street"] = "river"
    river["cards"]["board"].extend([_card(6, 2), _card(2, 1)])
    assert policy._polarized_raise_fraction(river, 0.30) is None


def test_shallow_spr_aces_reach_the_preflop_allin_branch(modules):
    _precompute, policy = modules
    history = [
        {
            "round": 0, "street": "preflop", "player_id": 0,
            "action_type": "raise", "committed": 3950, "stage_bet": 4000,
        },
        {
            "round": 0, "street": "preflop", "player_id": 1,
            "action_type": "raise", "committed": 7900, "stage_bet": 8000,
        },
    ]
    aces = _runtime_context(
        street="preflop",
        hole=[_card(12, 0), _card(12, 1)],
        board=[],
        history=history,
        pot=16000,
        hero_bet=4000,
        opponent_bet=8000,
        is_sb=True,
    )
    assert aces["line"]["preflop_spot"] == "sb_vs_reraise"
    assert aces["line"]["current_street"]["actions"][-1]["action"] == "raise"
    assert aces["betting"]["spr"] == 0.75
    assert policy.get_baseline_decision(aces) == {"kind": "allin"}

    weak = _runtime_context(
        street="preflop",
        hole=[_card(5, 0), _card(0, 1)],
        board=[],
        history=history,
        pot=16000,
        hero_bet=4000,
        opponent_bet=8000,
        is_sb=True,
    )
    assert policy.get_baseline_decision(weak)["kind"] != "allin"


def test_draw_bonus_requires_hole_card_participation_and_stops_on_river(modules):
    precompute, policy = modules

    def cid(rank, suit):
        return precompute.card_id(suit, rank)

    board_only_four_flush = tuple(cid(rank, 0) for rank in (11, 8, 4, 1))
    no_spade_hole = (cid(12, 1), cid(3, 2))
    assert policy._draw_bonus(no_spade_hole, board_only_four_flush) == 0.0

    flop_three_spades = tuple(cid(rank, 0) for rank in (11, 8, 4))
    one_spade_hole = (cid(12, 0), cid(9, 2))
    assert policy._draw_bonus(one_spade_hole, flop_three_spades) > 0.0
    river = (*flop_three_spades, cid(6, 3), cid(2, 1))
    assert policy._draw_bonus(one_spade_hole, river) == 0.0

    board_only_four_straight = tuple(cid(rank, suit) for rank, suit in (
        (4, 0), (5, 1), (6, 2), (7, 3),
    ))
    unrelated_hole = (cid(11, 0), cid(1, 1))
    assert policy._draw_bonus(unrelated_hole, board_only_four_straight) == 0.0


@pytest.mark.parametrize(
    "bad_value",
    [None, "bad", [], float("nan"), float("inf"), float("-inf")],
)
def test_malformed_match_fields_are_independently_neutral(modules, bad_value):
    _precompute, policy = modules
    context = _context(street="flop", board=[
        _card(11, 0), _card(6, 1), _card(0, 2),
    ])
    context["hand"]["remaining_including_current"] = bad_value
    assert policy._match_pressure_adjustment(context)["equity_delta"] == 0.0

    context = _context(street="flop", board=[
        _card(11, 0), _card(6, 1), _card(0, 2),
    ])
    context["opponent"]["match_result"]["hero_net_earned"] = bad_value
    assert policy._match_pressure_adjustment(context)["equity_delta"] == 0.0


def test_malformed_pressure_and_bluff_containers_fail_neutral(modules):
    _precompute, policy = modules
    context = _context(
        street="flop",
        hole=[_card(5, 1), _card(0, 2)],
        board=[_card(11, 0), _card(8, 1), _card(3, 2)],
    )
    context["betting"].update({"to_call": 0, "opponent_street_bet": 0})
    context["line"] = {
        "current_street": ["invalid"],
        "line_tags": [{}],
        "can_donk": False,
        "can_delayed_probe": False,
    }
    context["opponent"] = {
        "raw_street_actions": ["invalid"],
        "rates": ["invalid"],
        "terminal_response": ["invalid"],
        "showdown_range": ["invalid"],
    }
    assert policy._current_pressure_adjustment(context) == {
        "range_tilt": 0.0,
        "preflop_range_tilt": 0.0,
        "current_action_tilt": 0.0,
        "equity_delta": 0.0,
    }
    assert policy._bluff_allowed(context, 0.30) is False
    posterior = policy._opponent_posterior(context)
    assert posterior["line_strength_tilt"] == 0.0
    assert all(
        isinstance(posterior[field], float)
        for field in ("range_strength", "raise_fraction", "fold_to_raise")
    )

def test_baseline_refinements_and_wire_are_typed_and_legally_bounded(modules):
    _precompute, policy = modules
    contexts = [
        _context(spot="sb_open"),
        _context(spot="bb_vs_raise"),
        _context(street="flop", board=[
            _card(11, 0), _card(6, 1), _card(0, 2),
        ]),
    ]
    for context in contexts:
        baseline = policy.get_baseline_decision(context)
        _assert_typed(baseline, context)
        deadline = time.monotonic() + 0.01
        for refinement in policy.iter_decisions(context, baseline, deadline):
            decision = refinement.get("decision", refinement)
            _assert_typed(decision, context)

        native = _native_module()
        bot = native.NativeNationalBot("lll-clean-room-wire")
        try:
            bot._legal_policy_state = lambda: context["legal"]
            bot._socket_safe_fallback_decision = lambda: {"kind": "fold"}
            bot._pass_wire_kind = lambda: "call"
            wire, action, amount = bot._decision_to_tcp(baseline)
            assert "\n" not in wire and "\r" not in wire
            assert action in {"fold", "call", "check", "raise", "allin"}
            if action == "raise":
                assert wire == f"raise {amount}"
        finally:
            bot.close()

    no_raise = _context(spot="sb_open")
    no_raise["legal"]["policy_kinds"] = ["fold", "pass"]
    assert policy._raise_intent(no_raise, 0.5) is None
    _assert_typed(policy.get_baseline_decision(no_raise), no_raise)


@pytest.mark.parametrize("deadline", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_deadline_starts_no_refinement_work(modules, monkeypatch, deadline):
    precompute, policy = modules
    context = _context(street="flop", board=[
        _card(11, 0), _card(6, 1), _card(0, 2),
    ])
    baseline = policy.get_baseline_decision(context)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("nonfinite deadline reached expensive precompute")

    monkeypatch.setattr(precompute, "deck_without", forbidden)
    assert list(policy.iter_decisions(context, baseline, deadline)) == []
