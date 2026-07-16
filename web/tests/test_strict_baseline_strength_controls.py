"""Strength-control regressions for the repository-owned strict-v1 baseline.

These tests exercise only active, checked-in system artifacts.  They do not
read or import any retired or external bot implementation.  The assertions are
strategy contracts at the national runtime boundary: calibrated preflop facts,
heads-up line sizing, mathematically safe match closure, position-aware equity
realization, and board-relative opponent-range conditioning.
"""

from __future__ import annotations

from copy import deepcopy
import importlib.util
import itertools
import math
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
        "strict_baseline_strength_controls_policy",
        POLICY_PATH,
    )
    assert spec is not None and spec.loader is not None
    policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy)
    return precompute, policy


def _native_module():
    native = types.ModuleType("strict_baseline_strength_controls_native")
    native.__file__ = str(ROOT / "national_bot.py")
    exec(
        compile(national_native.NATIVE_BOT_TEMPLATE, "national_bot.py", "exec"),
        native.__dict__,
    )
    return native


@pytest.fixture(scope="module")
def modules():
    return _modules()


def _card(rank: int, suit: int) -> dict[str, int]:
    return {"suit": suit, "rank": rank}


def _card_id(precompute, rank: int, suit: int) -> int:
    return precompute.card_id(suit, rank)


def _equity(precompute, high: int, low: int, *, suited: bool) -> float:
    suit_a, suit_b = (0, 0) if suited else (0, 1)
    return precompute.preflop_equity(
        _card_id(precompute, high, suit_a),
        _card_id(precompute, low, suit_b),
    )


def _pair_equity(precompute, rank: int) -> float:
    return precompute.preflop_equity(
        _card_id(precompute, rank, 0),
        _card_id(precompute, rank, 1),
    )


def _runtime_context(
    *,
    street: str,
    hole: list[dict],
    board: list[dict] | None = None,
    history: list[dict] | None = None,
    pot: int,
    hero_bet: int,
    opponent_bet: int,
    is_sb: bool,
    hand_number: int = 63,
    hero_stack: int | None = None,
    opponent_stack: int | None = None,
    hero_net_earned: int = 0,
    decision_id: int = 17,
) -> dict:
    """Build candidate input through the production native runtime template."""

    native = _native_module()
    bot = native.NativeNationalBot("strict-strength-control-runtime")
    history = deepcopy(history or [])
    board = list(board or [])
    try:
        bot._my_id = 0
        bot._opponent_id = 1
        bot._is_sb = bool(is_sb)
        bot._hand_num = int(hand_number)
        bot._stage = street
        bot._my_cards = [
            (int(card["suit"]), int(card["rank"])) for card in hole
        ]
        bot._public_cards = [
            (int(card["suit"]), int(card["rank"])) for card in board
        ]
        bot._history = history
        bot._pot = int(pot)
        bot._my_stage_bet = int(hero_bet)
        bot._opponent_stage_bet = int(opponent_bet)
        bot._my_chips = (
            20000 - int(hero_bet) if hero_stack is None else int(hero_stack)
        )
        bot._opponent_chips = (
            20000 - int(opponent_bet)
            if opponent_stack is None
            else int(opponent_stack)
        )
        bot._opponent_tracker.begin_hand(
            hand_number,
            opponent_is_sb=not is_sb,
        )
        bot._opponent_tracker.net_earned = int(hero_net_earned)
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


def _base_context(
    *,
    street: str = "preflop",
    spot: str = "sb_open",
    hole: list[dict] | None = None,
    board: list[dict] | None = None,
) -> dict:
    board = list(board or [])
    return {
        "schema_version": 1,
        "runtime_version": national_native.NATIONAL_DECISION_RUNTIME_VERSION,
        "decision_id": 17,
        "cards": {
            "hole": list(hole or [_card(12, 0), _card(12, 1)]),
            "board": board,
        },
        "hand": {
            "number": 63,
            "total_hands": 70,
            "remaining_including_current": 8,
            "street": street,
            "acts_first_postflop": False,
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
            "current_street": {"actions": []},
            "line_tags": [],
        },
        "opponent": {"match_result": {"hero_net_earned": 0}},
    }


def _assert_typed(decision: dict, context: dict) -> None:
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


def test_all_169_preflop_equities_are_finite_ordered_and_suit_sensitive(modules):
    precompute, _policy = modules
    values = precompute.PREFLOP_CLASS_EQUITY

    assert len(values) == 169
    assert all(isinstance(value, float) and math.isfinite(value) for value in values)
    assert all(0.0 < value < 1.0 for value in values)
    assert [_pair_equity(precompute, rank) for rank in range(13)] == sorted(
        _pair_equity(precompute, rank) for rank in range(13)
    )
    for high in range(1, 13):
        for low in range(high):
            assert _equity(precompute, high, low, suited=True) > _equity(
                precompute,
                high,
                low,
                suited=False,
            )


def test_seeded_heads_up_equity_oracle_anchors_are_exact(modules):
    precompute, _policy = modules
    anchors = {
        "A2o": (_equity(precompute, 12, 0, suited=False), 0.551247),
        "K2o": (_equity(precompute, 11, 0, suited=False), 0.501106),
        "76o": (_equity(precompute, 5, 4, suited=False), 0.423744),
        "76s": (_equity(precompute, 5, 4, suited=True), 0.454697),
        "A2s": (_equity(precompute, 12, 0, suited=True), 0.572975),
        "AKs": (_equity(precompute, 12, 11, suited=True), 0.668808),
        "AKo": (_equity(precompute, 12, 11, suited=False), 0.652756),
        "22": (_pair_equity(precompute, 0), 0.505562),
        "AA": (_pair_equity(precompute, 12), 0.853325),
    }
    for name, (actual, expected) in anchors.items():
        assert actual == pytest.approx(expected, abs=0.0000005), name
    assert anchors["A2o"][0] > anchors["K2o"][0] > anchors["76o"][0]


def _preflop_cases(hole: list[dict]) -> list[tuple]:
    return [
        ("sb_open", True, [], 150, 50, 100, (225, 300)),
        (
            "bb_vs_limp",
            False,
            [{
                "round": 0,
                "street": "preflop",
                "player_id": 1,
                "action_type": "call",
                "committed": 50,
                "stage_bet": 100,
            }],
            200,
            100,
            100,
            (325, 450),
        ),
        (
            "bb_vs_raise",
            False,
            [{
                "round": 0,
                "street": "preflop",
                "player_id": 1,
                "action_type": "raise",
                "committed": 150,
                "stage_bet": 200,
            }],
            300,
            100,
            200,
            (650, 900),
        ),
        (
            "sb_vs_reraise",
            True,
            [
                {
                    "round": 0,
                    "street": "preflop",
                    "player_id": 0,
                    "action_type": "raise",
                    "committed": 150,
                    "stage_bet": 200,
                },
                {
                    "round": 0,
                    "street": "preflop",
                    "player_id": 1,
                    "action_type": "raise",
                    "committed": 300,
                    "stage_bet": 400,
                },
            ],
            600,
            200,
            400,
            (900, 1200),
        ),
    ]


def test_real_runtime_a2o_opens_while_76o_does_not(modules):
    _precompute, policy = modules
    decisions = {}
    for name, hole in {
        "A2o": [_card(12, 0), _card(0, 1)],
        "76o": [_card(5, 0), _card(4, 1)],
    }.items():
        context = _runtime_context(
            street="preflop",
            hole=hole,
            pot=150,
            hero_bet=50,
            opponent_bet=100,
            is_sb=True,
        )
        assert context["line"]["preflop_spot"] == "sb_open"
        decisions[name] = policy.get_baseline_decision(context)
        _assert_typed(decisions[name], context)

    assert decisions["A2o"]["kind"] == "raise"
    assert decisions["76o"]["kind"] != "raise"


@pytest.mark.parametrize(
    (
        "expected_spot",
        "is_sb",
        "history",
        "pot",
        "hero_bet",
        "opponent_bet",
        "band",
    ),
    _preflop_cases([_card(12, 0), _card(12, 1)]),
)
def test_four_real_runtime_preflop_lines_use_heads_up_sizing_bands(
    modules,
    expected_spot,
    is_sb,
    history,
    pot,
    hero_bet,
    opponent_bet,
    band,
):
    _precompute, policy = modules
    aces = [_card(12, 0), _card(12, 1)]
    context = _runtime_context(
        street="preflop",
        hole=aces,
        history=history,
        pot=pot,
        hero_bet=hero_bet,
        opponent_bet=opponent_bet,
        is_sb=is_sb,
    )
    assert context["line"]["preflop_spot"] == expected_spot
    decision = policy.get_baseline_decision(context)
    _assert_typed(decision, context)
    assert decision["kind"] == "raise", expected_spot
    assert band[0] <= decision["raise_to"] <= band[1], expected_spot


def test_weak_hands_do_not_raise_or_jam_in_any_actionable_preflop_line(modules):
    _precompute, policy = modules
    weak = [_card(1, 0), _card(0, 1)]
    for expected_spot, is_sb, history, pot, hero_bet, opponent_bet, _band in (
        _preflop_cases(weak)
    ):
        context = _runtime_context(
            street="preflop",
            hole=weak,
            history=history,
            pot=pot,
            hero_bet=hero_bet,
            opponent_bet=opponent_bet,
            is_sb=is_sb,
        )
        assert context["line"]["preflop_spot"] == expected_spot
        decision = policy.get_baseline_decision(context)
        _assert_typed(decision, context)
        assert decision["kind"] not in {"raise", "allin"}, expected_spot


def test_all_four_shallow_preflop_lines_use_allin_token_at_exact_stack_target(
    modules,
):
    _precompute, policy = modules
    aces = [_card(12, 0), _card(12, 1)]
    weak = [_card(1, 0), _card(0, 1)]
    shallow_stacks = {
        "sb_open": 200,
        "bb_vs_limp": 250,
        "bb_vs_raise": 600,
        "sb_vs_reraise": 800,
    }
    for expected_spot, is_sb, history, pot, hero_bet, opponent_bet, _band in (
        _preflop_cases(aces)
    ):
        strong_context = _runtime_context(
            street="preflop",
            hole=aces,
            history=history,
            pot=pot,
            hero_bet=hero_bet,
            opponent_bet=opponent_bet,
            hero_stack=shallow_stacks[expected_spot],
            is_sb=is_sb,
        )
        legal = strong_context["legal"]
        hero_total = hero_bet + shallow_stacks[expected_spot]
        assert strong_context["line"]["preflop_spot"] == expected_spot
        assert {"raise", "allin"} <= set(legal["policy_kinds"])
        assert legal["max_raise_to"] == hero_total - 1
        assert policy._raise_intent(strong_context, 1.25) == {"kind": "allin"}
        assert policy.get_baseline_decision(strong_context) == {"kind": "allin"}

        weak_context = deepcopy(strong_context)
        weak_context["cards"]["hole"] = weak
        weak_decision = policy.get_baseline_decision(weak_context)
        _assert_typed(weak_decision, weak_context)
        assert weak_decision["kind"] not in {"raise", "allin"}, expected_spot


def test_allin_only_ultrashort_preflop_lines_jam_aces_without_weak_leak(
    modules,
):
    _precompute, policy = modules
    aces = [_card(12, 0), _card(12, 1)]
    weak = [_card(1, 0), _card(0, 1)]
    ultrashort_stacks = {
        "sb_open": 100,
        "bb_vs_limp": 100,
        "bb_vs_raise": 250,
        "sb_vs_reraise": 350,
    }
    for expected_spot, is_sb, history, pot, hero_bet, opponent_bet, _band in (
        _preflop_cases(aces)
    ):
        context = _runtime_context(
            street="preflop",
            hole=aces,
            history=history,
            pot=pot,
            hero_bet=hero_bet,
            opponent_bet=opponent_bet,
            hero_stack=ultrashort_stacks[expected_spot],
            is_sb=is_sb,
        )
        assert context["line"]["preflop_spot"] == expected_spot
        assert "allin" in context["legal"]["policy_kinds"]
        assert "raise" not in context["legal"]["policy_kinds"]
        assert context["legal"]["min_raise_to"] is None
        assert context["legal"]["max_raise_to"] is None
        assert policy._raise_intent(context, 1.25) == {"kind": "allin"}
        assert policy.get_baseline_decision(context) == {"kind": "allin"}

        weak_context = deepcopy(context)
        weak_context["cards"]["hole"] = weak
        weak_decision = policy.get_baseline_decision(weak_context)
        _assert_typed(weak_decision, weak_context)
        assert weak_decision["kind"] != "allin", expected_spot


def test_shallow_stack_uses_exact_allin_token_without_weak_hand_leak(modules):
    _precompute, policy = modules
    history = [
        {
            "round": 0,
            "street": "preflop",
            "player_id": 0,
            "action_type": "raise",
            "committed": 3950,
            "stage_bet": 4000,
        },
        {
            "round": 0,
            "street": "preflop",
            "player_id": 1,
            "action_type": "raise",
            "committed": 7900,
            "stage_bet": 8000,
        },
    ]
    decisions = {}
    for name, hole in {
        "AA": [_card(12, 0), _card(12, 1)],
        "72o": [_card(5, 0), _card(0, 1)],
    }.items():
        context = _runtime_context(
            street="preflop",
            hole=hole,
            history=history,
            pot=16000,
            hero_bet=4000,
            opponent_bet=8000,
            is_sb=True,
        )
        assert context["line"]["preflop_spot"] == "sb_vs_reraise"
        assert context["betting"]["spr"] == 0.75
        decisions[name] = policy.get_baseline_decision(context)
    assert decisions["AA"] == {"kind": "allin"}
    assert decisions["72o"]["kind"] != "allin"


def test_preflop_line_sizing_controls_are_neutral_postflop(modules):
    _precompute, policy = modules
    context = _base_context(
        street="flop",
        hole=[_card(12, 0), _card(11, 1)],
        board=[_card(10, 2), _card(7, 3), _card(2, 0)],
    )
    decisions = []
    for spot in ("sb_open", "bb_vs_limp", "bb_vs_raise", "sb_vs_reraise"):
        candidate = deepcopy(context)
        candidate["line"]["preflop_spot"] = spot
        assert policy._preflop_spot_adjustment(candidate) == {
            "equity_delta": 0.0,
            "sizing_delta": 0.0,
        }
        decisions.append(policy.get_baseline_decision(candidate))
    assert decisions[1:] == decisions[:-1]


@pytest.mark.parametrize(
    (
        "hand_number",
        "is_sb",
        "hero_stack",
        "strict_lead",
        "boundary_lead",
        "current_exposure",
        "future_blinds",
        "loss_bound",
    ),
    [
        (70, True, 19950, 51, 50, 50, 0, 50),
        (70, False, 19900, 101, 100, 100, 0, 100),
        (69, True, 19950, 151, 150, 50, 100, 150),
        (70, True, 19500, 501, 500, 500, 0, 500),
    ],
)
def test_runtime_match_control_proves_only_strict_forced_fold_wins(
    hand_number,
    is_sb,
    hero_stack,
    strict_lead,
    boundary_lead,
    current_exposure,
    future_blinds,
    loss_bound,
):
    def produced(lead: int) -> dict:
        return _runtime_context(
            street="preflop",
            hole=[_card(12, 0), _card(12, 1)],
            pot=20000 - hero_stack + (100 if is_sb else 50),
            hero_bet=20000 - hero_stack,
            opponent_bet=100 if is_sb else 50,
            is_sb=is_sb,
            hand_number=hand_number,
            hero_stack=hero_stack,
            hero_net_earned=lead,
        )["hand"]["match_control"]

    locked = produced(strict_lead)
    boundary = produced(boundary_lead)
    expected_position = "small_blind" if is_sb else "big_blind"
    assert locked == {
        "schema_version": 1,
        "initial_chips": 20_000,
        "small_blind": 50,
        "big_blind": 100,
        "current_position": expected_position,
        "current_exposure": current_exposure,
        "future_forced_blinds": future_blinds,
        "forced_fold_loss_bound": loss_bound,
        "hero_net_earned": strict_lead,
        "fold_locks_win": True,
    }
    assert boundary == {
        **locked,
        "hero_net_earned": boundary_lead,
        "fold_locks_win": False,
    }


def _locked_context() -> dict:
    locked = _base_context()
    locked["hand"].update({
        "number": 70,
        "total_hands": 70,
        "remaining_including_current": 1,
        "position": "small_blind",
    })
    locked["line"]["position"] = "small_blind"
    locked["betting"]["hero_stack"] = 19_500
    locked["opponent"]["match_result"]["hero_net_earned"] = 501
    locked["hand"]["match_control"] = {
        "schema_version": 1,
        "initial_chips": 20_000,
        "small_blind": 50,
        "big_blind": 100,
        "current_position": "small_blind",
        "current_exposure": 500,
        "future_forced_blinds": 0,
        "forced_fold_loss_bound": 500,
        "hero_net_earned": 501,
        "fold_locks_win": True,
    }
    return locked


def test_policy_folds_aces_when_match_is_mathematically_locked(modules):
    _precompute, policy = modules
    assert policy.get_baseline_decision(_locked_context()) == {"kind": "fold"}


def test_locked_match_starts_no_refinement_work(modules):
    _precompute, policy = modules
    assert list(
        policy.iter_decisions(
            _locked_context(),
            {"kind": "fold"},
            time.monotonic() + 0.10,
        )
    ) == []


def test_equal_loss_bound_is_not_a_mathematical_lock(modules):
    _precompute, policy = modules
    boundary = _locked_context()
    boundary["opponent"]["match_result"]["hero_net_earned"] = 500
    boundary["hand"]["match_control"]["hero_net_earned"] = 500
    boundary["hand"]["match_control"]["fold_locks_win"] = False
    assert policy.get_baseline_decision(boundary)["kind"] != "fold"


@pytest.mark.parametrize(
    "malformed",
    [
        None,
        [],
        {"schema_version": 2, "fold_locks_win": True},
        {
            "schema_version": 1,
            "current_exposure": float("nan"),
            "future_forced_blinds": 0,
            "forced_fold_loss_bound": 500,
            "fold_locks_win": True,
        },
        {
            "schema_version": 1,
            "current_exposure": 500,
            "future_forced_blinds": 0,
            "forced_fold_loss_bound": float("inf"),
            "fold_locks_win": True,
        },
    ],
)
def test_malformed_match_control_is_neutral(modules, malformed):
    _precompute, policy = modules
    neutral = _base_context()
    expected = policy.get_baseline_decision(neutral)
    candidate = deepcopy(neutral)
    candidate["hand"]["match_control"] = malformed
    assert policy.get_baseline_decision(candidate) == expected


def _positional_call_context(
    *,
    in_position: bool,
    street: str,
    action: str,
    call_closes_allin_runout: bool | None = False,
) -> dict:
    context = _base_context(
        street=street,
        hole=[_card(11, 0), _card(10, 1)],
        board=(
            [_card(12, 2), _card(6, 3), _card(0, 0)]
            if street == "flop"
            else [
                _card(12, 2),
                _card(6, 3),
                _card(0, 0),
                _card(4, 1),
                _card(2, 2),
            ]
        ),
    )
    context["betting"].update({
        "pot": 1000,
        "hero_stack": 19500,
        "opponent_stack": 19500,
        "effective_stack": 19500,
        "hero_street_bet": 0,
        "opponent_street_bet": 500,
        "to_call": 500,
        "spr": 19.5,
    })
    if call_closes_allin_runout is not None:
        context["betting"]["call_closes_allin_runout"] = (
            call_closes_allin_runout
        )
    context["legal"] = {
        "policy_kinds": ["fold", "pass"],
        "min_raise_to": None,
        "max_raise_to": None,
    }
    context["line"].update({
        "hero_in_position_postflop": in_position,
        "current_street": {
            "actions": [{"actor": "opponent", "action": action}],
        },
    })
    context["hand"]["acts_first_postflop"] = not in_position
    return context


def test_runtime_produces_consistent_heads_up_postflop_position_facts():
    board = [_card(12, 2), _card(6, 3), _card(0, 0)]
    for is_sb, expected_in_position in ((True, True), (False, False)):
        context = _runtime_context(
            street="flop",
            hole=[_card(11, 0), _card(10, 1)],
            board=board,
            pot=1000,
            hero_bet=0,
            opponent_bet=500,
            is_sb=is_sb,
        )
        assert context["line"]["hero_in_position_postflop"] is (
            expected_in_position
        )
        assert context["hand"]["acts_first_postflop"] is (
            not expected_in_position
        )


def test_position_changes_only_marginal_future_street_equity_realization(modules):
    _precompute, policy = modules
    ip = _positional_call_context(
        in_position=True,
        street="flop",
        action="raise",
    )
    oop = _positional_call_context(
        in_position=False,
        street="flop",
        action="raise",
    )
    assert policy._decision_from_equity(ip, 0.36, 1.0, 4096) == {
        "kind": "pass"
    }
    assert policy._decision_from_equity(oop, 0.36, 1.0, 4096) == {
        "kind": "fold"
    }


def test_position_does_not_fold_strong_future_street_equity(modules):
    _precompute, policy = modules
    ip = _positional_call_context(
        in_position=True,
        street="flop",
        action="raise",
    )
    oop = _positional_call_context(
        in_position=False,
        street="flop",
        action="raise",
    )
    for context in (ip, oop):
        assert policy._decision_from_equity(
            context,
            0.70,
            1.0,
            4096,
        ) == {"kind": "pass"}


def test_position_is_irrelevant_when_no_future_equity_can_be_realized(modules):
    _precompute, policy = modules
    river_ip = _positional_call_context(
        in_position=True,
        street="river",
        action="raise",
    )
    river_oop = _positional_call_context(
        in_position=False,
        street="river",
        action="raise",
    )
    assert policy._decision_from_equity(
        river_ip, 0.34, 1.0, 4096
    ) == policy._decision_from_equity(river_oop, 0.34, 1.0, 4096)

    allin_ip = _positional_call_context(
        in_position=True,
        street="flop",
        action="raise",
        call_closes_allin_runout=True,
    )
    allin_oop = _positional_call_context(
        in_position=False,
        street="flop",
        action="raise",
        call_closes_allin_runout=True,
    )
    assert policy._decision_from_equity(
        allin_ip, 0.34, 1.0, 4096
    ) == policy._decision_from_equity(allin_oop, 0.34, 1.0, 4096)


def test_allin_closure_uses_only_the_runtime_boolean_not_action_text(modules):
    _precompute, policy = modules
    spoofed_ip = _positional_call_context(
        in_position=True,
        street="flop",
        action="allin",
        call_closes_allin_runout=False,
    )
    spoofed_oop = _positional_call_context(
        in_position=False,
        street="flop",
        action="allin",
        call_closes_allin_runout=False,
    )
    assert policy._decision_from_equity(
        spoofed_ip, 0.36, 1.0, 4096
    ) == {"kind": "pass"}
    assert policy._decision_from_equity(
        spoofed_oop, 0.36, 1.0, 4096
    ) == {"kind": "fold"}

    missing_ip = _positional_call_context(
        in_position=True,
        street="flop",
        action="raise",
        call_closes_allin_runout=None,
    )
    missing_oop = _positional_call_context(
        in_position=False,
        street="flop",
        action="raise",
        call_closes_allin_runout=None,
    )
    assert policy._decision_from_equity(
        missing_ip, 0.34, 1.0, 4096
    ) == policy._decision_from_equity(missing_oop, 0.34, 1.0, 4096)


def _river_pressure_context(action: str) -> dict:
    context = _base_context(
        street="river",
        hole=[_card(4, 2), _card(0, 2)],  # 6d2d
        board=[
            _card(5, 0),  # 7s
            _card(1, 1),  # 3h
            _card(5, 1),  # 7h
            _card(3, 3),  # 5c
            _card(0, 0),  # 2s
        ],
    )
    if action == "raise":
        context["betting"].update({
            "pot": 3800,
            "hero_street_bet": 0,
            "opponent_street_bet": 1800,
            "to_call": 1800,
        })
    else:
        context["betting"].update({
            "pot": 2000,
            "hero_street_bet": 0,
            "opponent_street_bet": 0,
            "to_call": 0,
        })
    context["line"]["current_street"] = {
        "actions": [{"actor": "opponent", "action": action}],
    }
    return context


def _board_relative_weight(policy, posterior, opponent_hole, current_board):
    try:
        return policy._opponent_sample_weight(
            posterior,
            opponent_hole,
            current_board,
        )
    except TypeError as exc:
        pytest.fail(
            "opponent range weight must consume the current public board: "
            f"{exc}"
        )


def test_preflop_pressure_still_upweights_premium_over_trash(modules):
    precompute, policy = modules
    ace_ace = (
        _card_id(precompute, 12, 0),
        _card_id(precompute, 12, 1),
    )
    seven_deuce = (
        _card_id(precompute, 5, 0),
        _card_id(precompute, 0, 1),
    )
    neutral = {
        "bucket_multipliers": {},
        "wide_range_tilt": 0.0,
        "preflop_line_strength_tilt": 0.0,
        "current_action_strength_tilt": 0.0,
    }
    pressured = {
        **neutral,
        "preflop_line_strength_tilt": 0.55,
        "current_action_strength_tilt": 0.35,
    }
    neutral_ratio = policy._opponent_sample_weight(
        neutral, ace_ace, ()
    ) / policy._opponent_sample_weight(neutral, seven_deuce, ())
    pressured_ratio = policy._opponent_sample_weight(
        pressured, ace_ace, ()
    ) / policy._opponent_sample_weight(pressured, seven_deuce, ())
    assert pressured_ratio > neutral_ratio


def _weighted_current_board_percentile(precompute, policy, context: dict) -> float:
    hole = tuple(
        _card_id(precompute, card["rank"], card["suit"])
        for card in context["cards"]["hole"]
    )
    board = tuple(
        _card_id(precompute, card["rank"], card["suit"])
        for card in context["cards"]["board"]
    )
    combos = list(itertools.combinations(precompute.deck_without((*hole, *board)), 2))
    ranks = [precompute.evaluate_seven((*opponent_hole, *board)) for opponent_hole in combos]
    ordered = {rank: index for index, rank in enumerate(sorted(set(ranks)))}
    denominator = max(1, len(ordered) - 1)
    posterior = policy._opponent_posterior(context)
    weighted = 0.0
    weight_total = 0.0
    for opponent_hole, rank in zip(combos, ranks):
        weight = _board_relative_weight(
            policy,
            posterior,
            opponent_hole,
            board,
        )
        weighted += weight * ordered[rank] / denominator
        weight_total += weight
    return weighted / weight_total


def test_raise_conditioning_cannot_improve_hero_equity_on_fixed_river(modules):
    _precompute, policy = modules
    checked = _river_pressure_context("check")
    raised = _river_pressure_context("raise")
    checked_equity = policy._baseline_equity(checked)
    raised_equity = policy._baseline_equity(raised)

    assert math.isfinite(checked_equity) and math.isfinite(raised_equity)
    # The tactical audit's frozen example produced approximately 0.524494
    # versus 0.562265 before correction because it treated stronger preflop
    # classes as stronger on every board.  A raise-conditioned current range
    # may tie the checked range, but it cannot make this hero stronger.
    assert raised_equity <= checked_equity


def test_raise_weights_stronger_hands_on_the_current_public_board(modules):
    precompute, policy = modules
    checked_strength = _weighted_current_board_percentile(
        precompute,
        policy,
        _river_pressure_context("check"),
    )
    raised_strength = _weighted_current_board_percentile(
        precompute,
        policy,
        _river_pressure_context("raise"),
    )
    assert raised_strength > checked_strength


def test_rollout_range_weights_use_current_board_not_future_runout(
    modules,
    monkeypatch,
):
    _precompute, policy = modules
    context = _base_context(
        street="flop",
        hole=[_card(4, 2), _card(0, 2)],
        board=[_card(5, 0), _card(1, 1), _card(5, 1)],
    )
    current_board = tuple(
        policy._card_ids(context["cards"]["board"])
    )
    original = policy._opponent_sample_weight
    observed_boards = []

    def observe(*args):
        observed_boards.append(args[2] if len(args) >= 3 else None)
        return original(*args)

    monkeypatch.setattr(policy, "_opponent_sample_weight", observe)
    assert policy._baseline_equity(context) is not None
    assert observed_boards
    assert all(
        board is not None and tuple(board) == current_board
        for board in observed_boards
    )


@pytest.mark.parametrize(
    "malformed_board",
    [
        (0, 0, 1),
        (-1, 4, 8),
        (0, 4, 52),
        ("bad", 4, 8),
        {0, 4, 8},
    ],
)
def test_malformed_current_board_is_neutral_for_range_weight(
    modules,
    malformed_board,
):
    precompute, policy = modules
    posterior = {
        "bucket_multipliers": {},
        "wide_range_tilt": 0.0,
        "preflop_line_strength_tilt": 0.0,
        "current_action_strength_tilt": 1.0,
    }
    opponent_holes = [
        (_card_id(precompute, 12, 0), _card_id(precompute, 12, 1)),
        (_card_id(precompute, 6, 0), _card_id(precompute, 1, 1)),
    ]
    assert [
        _board_relative_weight(
            policy,
            posterior,
            opponent_hole,
            malformed_board,
        )
        for opponent_hole in opponent_holes
    ] == [1.0, 1.0]
