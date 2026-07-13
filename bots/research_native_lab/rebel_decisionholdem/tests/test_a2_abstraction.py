from __future__ import annotations

from itertools import combinations

import pytest

from ..decisionholdem_like.a2_runtime import (
    ACTION_IDS,
    ActionContext,
    ActionSpec,
    legal_action_specs,
    hand_bucket,
    map_observed_raise_to,
    normalize_action_specs,
    postflop_bucket,
    preflop_class,
    tcp_card_id,
)
from sever.engine.validator import validate_action


def test_preflop_abstraction_has_exactly_169_order_independent_classes() -> None:
    classes = {preflop_class(cards) for cards in combinations(range(52), 2)}
    assert len(classes) == 169
    assert {"AA", "AKs", "AKo", "72s", "72o"} <= classes
    cards = (tcp_card_id(0, 12), tcp_card_id(0, 11))
    assert preflop_class(cards) == preflop_class(tuple(reversed(cards))) == "AKs"


def test_postflop_bucket_uses_real_five_to_seven_card_rank_categories() -> None:
    private = (tcp_card_id(0, 12), tcp_card_id(1, 12))
    flop = (tcp_card_id(2, 12), tcp_card_id(0, 0), tcp_card_id(1, 3))
    assert postflop_bucket(private, flop) == "made:trips"
    turn = flop + (tcp_card_id(3, 7),)
    assert postflop_bucket(private, turn) == "made:trips"
    with pytest.raises(ValueError, match="unique"):
        postflop_bucket(private, flop + (private[0],))
    with pytest.raises(ValueError, match="empty board"):
        hand_bucket("preflop", private, flop)


def test_action_abstraction_exposes_all_required_base_actions() -> None:
    context = ActionContext(
        street="flop",
        pot=1_000,
        hero_bet=0,
        opponent_bet=0,
        hero_chips=20_000,
        is_small_blind=False,
        hero_action_count=0,
    )
    specs = legal_action_specs(context)
    assert tuple(spec.action_id for spec in specs) == ACTION_IDS
    assert tuple(spec.wire_action for spec in specs) == (
        "fold",
        "check",
        "raise 100",
        "raise 500",
        "raise 1000",
        "raise 1500",
        "allin",
    )


def test_duplicate_or_illegal_action_specs_are_normalized_deterministically() -> None:
    context = ActionContext(
        street="flop",
        pot=100,
        hero_bet=0,
        opponent_bet=0,
        hero_chips=150,
        is_small_blind=False,
        hero_action_count=0,
    )
    specs = legal_action_specs(context)
    assert tuple(spec.action_id for spec in specs) == (
        "fold",
        "check_call",
        "exact_min",
        "allin",
    )
    assert len({spec.wire_action for spec in specs}) == len(specs)

    malformed = normalize_action_specs(
        (
            ActionSpec("fold", "fold"),
            ActionSpec("fold", "call"),
            ActionSpec("1p", "raise  100", 100),
            ActionSpec("0.5p", "raise 150", 150),
        ),
        context,
    )
    assert malformed == (ActionSpec("fold", "fold"),)
    assert normalize_action_specs((ActionSpec("fold", "call"),), context) == (
        ActionSpec("fold", "fold"),
    )


def test_exact_double_raise_boundary_and_allin_guard_are_preserved() -> None:
    context = ActionContext(
        street="preflop",
        pot=1_000,
        hero_bet=400,
        opponent_bet=400,
        hero_chips=19_600,
        is_small_blind=True,
        hero_action_count=1,
        stage_actions=(("raise", 200), ("raise", 400)),
    )
    minimum = next(
        spec for spec in legal_action_specs(context) if spec.action_id == "exact_min"
    )
    assert minimum.wire_action == "raise 800"

    allin_context = ActionContext(
        street="turn",
        pot=20_000,
        hero_bet=1_000,
        opponent_bet=20_000,
        hero_chips=19_000,
        is_small_blind=True,
        hero_action_count=0,
        stage_actions=(("allin", None),),
        opponent_allin=True,
    )
    assert tuple(spec.action_id for spec in legal_action_specs(allin_context)) == (
        "fold",
        "check_call",
    )


def test_off_tree_mapping_is_explicitly_nearest_only_not_safe_resolving() -> None:
    context = ActionContext(
        street="flop",
        pot=1_000,
        hero_bet=0,
        opponent_bet=0,
        hero_chips=20_000,
        is_small_blind=False,
        hero_action_count=0,
    )
    mapped = map_observed_raise_to(730, context)
    assert mapped.mapped_action_id == "0.5p"
    assert not mapped.exact
    assert mapped.fidelity == "nearest-action-translation-only-not-safe-resolve"
    exact = map_observed_raise_to(1_000, context)
    assert exact.mapped_action_id == "1p"
    assert exact.exact


@pytest.mark.parametrize(
    "context",
    (
        ActionContext("preflop", 150, 50, 100, 19_950, True, 0),
        ActionContext(
            "preflop",
            200,
            100,
            100,
            19_900,
            False,
            0,
            (("call", None),),
        ),
        ActionContext(
            "preflop",
            300,
            100,
            200,
            19_900,
            False,
            0,
            (("raise", 200),),
        ),
        ActionContext("flop", 400, 0, 0, 19_800, False, 0),
        ActionContext(
            "flop",
            400,
            0,
            0,
            19_800,
            True,
            0,
            (("check", None),),
            True,
        ),
        ActionContext(
            "turn",
            1_200,
            0,
            400,
            19_400,
            True,
            0,
            (("raise", 400),),
        ),
        ActionContext(
            "river",
            20_000,
            1_000,
            20_000,
            19_000,
            True,
            0,
            (("allin", None),),
            False,
            True,
        ),
    ),
)
def test_every_projected_action_passes_the_national_validator(context: ActionContext) -> None:
    state = {
        "stage": context.street,
        "actions": list(context.stage_actions),
        "player_chips": context.hero_chips,
        "player_bet": context.hero_bet,
        "opponent_bet": context.opponent_bet,
        "is_small_blind": context.is_small_blind,
        "is_big_blind": not context.is_small_blind,
        "allin_occurred": context.opponent_allin,
        "player_action_count": context.hero_action_count,
    }
    for spec in legal_action_specs(context):
        if spec.wire_action.startswith("raise "):
            action_type = "raise"
            amount = int(spec.wire_action.split(" ", 1)[1])
        else:
            action_type = spec.wire_action
            amount = None
        valid, reason = validate_action(action_type, amount, state)
        assert valid, (context, spec, reason)
