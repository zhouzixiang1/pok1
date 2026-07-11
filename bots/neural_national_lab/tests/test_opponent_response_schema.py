from __future__ import annotations

from pathlib import Path
import sys

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import opponent_response_schema as schema  # noqa: E402


def _row(
    *,
    stage: str = "preflop",
    dealer_id: int = 0,
    hero_action: int = 200,
    opponent_action: str | None = "call",
    opponent_amount: int = 100,
    history: list[dict] | None = None,
    my_bet: int = 50,
    opponent_bet: int = 100,
    my_chips: int = 19_950,
    opponent_chips: int = 19_900,
    pot: int = 150,
    to_call: int = 50,
) -> dict:
    row = {
        "stage": stage,
        "hero_action": hero_action,
        "request": {
            "my_id": 0,
            "dealer_id": dealer_id,
            "my_chips": my_chips,
            "opponent_chips": opponent_chips,
            "my_stage_bet": my_bet,
            "opponent_stage_bet": opponent_bet,
            "pot": pot,
            "to_call": to_call,
            "history": list(history or []),
            "public_cards": [] if stage == "preflop" else [0, 4, 8],
        },
        "state": {
            "round": {"preflop": 0, "flop": 1, "turn": 2, "river": 3}[stage],
            "pot": pot,
            "to_call": to_call,
        },
    }
    if opponent_action is not None:
        row["opponent_action"] = opponent_action
        row["opponent_action_amount"] = opponent_amount
    return row


def test_raise_response_uses_increment_not_raise_to_total() -> None:
    row = _row(
        hero_action=300,
        opponent_action="raise",
        opponent_amount=700,
    )

    enriched = schema.annotate_response_row(row)

    assert enriched["response_legal_action_mask"] == [1, 0, 1, 1, 1]
    assert enriched["response_raise_to_total"] == 700
    assert enriched["response_aggressive_increment"] == 600
    assert enriched["response_context"]["pot_before_response"] == 400
    assert enriched["response_aggressive_increment_pot_ratio"] == pytest.approx(1.5)
    assert enriched["response_aggressive_stack_fraction"] == pytest.approx(600 / 19_900)
    assert enriched["response_amount_target_mask"] == 1


def test_allin_response_uses_remaining_stack_increment() -> None:
    row = _row(
        hero_action=300,
        opponent_action="allin",
        opponent_amount=19_900,
    )

    enriched = schema.annotate_response_row(row)

    assert enriched["response_raise_to_total"] == 20_000
    assert enriched["response_aggressive_increment"] == 19_900
    assert enriched["response_aggressive_stack_fraction"] == 1.0
    assert 0.0 < enriched["response_amount_target"] < 1.0


def test_small_blind_limp_allows_big_blind_check_but_not_call() -> None:
    row = _row(hero_action=0, opponent_action="check", opponent_amount=0)

    enriched = schema.annotate_response_row(row)

    assert enriched["response_context"]["hero_action_type"] == "call"
    assert enriched["response_legal_actions"] == ["fold", "check", "raise", "allin"]


def test_postflop_first_check_requires_call_as_pass_not_check() -> None:
    row = _row(
        stage="flop",
        dealer_id=1,
        hero_action=0,
        opponent_action="call",
        opponent_amount=0,
        my_bet=0,
        opponent_bet=0,
        my_chips=20_000,
        opponent_chips=20_000,
        pot=200,
        to_call=0,
    )

    enriched = schema.annotate_response_row(row)

    assert enriched["response_context"]["hero_action_type"] == "check"
    assert enriched["response_legal_actions"] == ["fold", "call", "raise", "allin"]


def test_call_that_closes_street_is_explicitly_not_a_response_target() -> None:
    row = _row(
        stage="flop",
        dealer_id=0,
        hero_action=0,
        opponent_action=None,
        opponent_amount=0,
        history=[{"round": 1, "player_id": 1, "action_type": "check"}],
        my_bet=0,
        opponent_bet=0,
        my_chips=20_000,
        opponent_chips=20_000,
        pot=200,
        to_call=0,
    )

    enriched = schema.annotate_response_row(row)

    assert enriched["response_eligible"] is False
    assert enriched["response_target_mask"] == 0
    assert enriched["response_outcome"] == "call_closed_street"
    assert enriched["response_legal_action_mask"] == [0, 0, 0, 0, 0]


def test_call_after_hero_allin_is_masked_to_fold_or_call() -> None:
    row = _row(
        hero_action=-2,
        opponent_action="call",
        opponent_amount=19_900,
    )

    enriched = schema.annotate_response_row(row)

    assert enriched["response_legal_actions"] == ["fold", "call"]
    assert enriched["response_amount_target_mask"] == 0


def test_missing_required_or_illegal_response_is_rejected() -> None:
    with pytest.raises(ValueError, match="missing opponent action"):
        schema.annotate_response_row(_row(opponent_action=None))

    with pytest.raises(ValueError, match="illegal"):
        schema.annotate_response_row(_row(opponent_action="check"))


def test_metadata_documents_national_wire_amount_semantics() -> None:
    metadata = schema.response_schema_metadata()

    assert metadata["raise_wire_semantics"] == "raise_to_stage_total"
    assert metadata["allin_wire_semantics"] == "remaining_stack_increment"
    assert metadata["public_only"] is True


def test_current_response_rows_are_idempotent_and_canonical() -> None:
    enriched = schema.annotate_response_row(_row())

    assert schema.annotate_response_rows([enriched]) == [enriched]
    schema.validate_response_row(enriched)

    enriched["response_legal_action_mask"][2] = 0
    with pytest.raises(ValueError, match="canonical validation"):
        schema.validate_response_row(enriched)

    closed = schema.annotate_response_row(
        _row(
            stage="flop",
            dealer_id=0,
            hero_action=0,
            opponent_action=None,
            history=[{"round": 1, "player_id": 1, "action_type": "check"}],
            my_bet=0,
            opponent_bet=0,
            pot=200,
            to_call=0,
        )
    )
    assert schema.annotate_response_rows([closed]) == [closed]
    schema.validate_response_row(closed)


def test_population_accounts_for_observed_and_closed_decisions() -> None:
    observed = schema.annotate_response_row(_row())
    observed.update({"hand": 1, "hand_decision_index": 0, "decision_serial": 0})
    closed = _row(hero_action=-1, opponent_action=None)
    decisions = [
        {
            "hand": 1,
            "stage": "preflop",
            "hand_decision_index": 0,
            "decision_serial": 0,
            "final_action": 200,
            "request": observed["request"],
            "state": observed["state"],
        },
        {
            "hand": 2,
            "stage": "preflop",
            "hand_decision_index": 0,
            "decision_serial": 1,
            "final_action": -1,
            "request": closed["request"],
            "state": closed["state"],
        },
    ]

    summary = schema.summarize_response_population(decisions, [observed])

    assert summary["decisions"] == 2
    assert summary["response_expected"] == 1
    assert summary["response_observed"] == 1
    assert summary["response_not_expected"] == 1
    assert summary["not_expected_by_reason"] == {"hero_fold_settled": 1}


def test_population_rejects_missing_required_response() -> None:
    row = _row()
    decision = {
        "hand": 1,
        "stage": "preflop",
        "hand_decision_index": 0,
        "decision_serial": 0,
        "final_action": 200,
        "request": row["request"],
        "state": row["state"],
    }

    with pytest.raises(ValueError, match="population mismatch"):
        schema.summarize_response_population([decision], [])
