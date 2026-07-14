from __future__ import annotations

import math
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]
TOOLS = ROOT / "bots" / "neural_national_lab" / "tools"
sys.path.insert(0, str(TOOLS))

import history_feature_schema as history_schema  # noqa: E402


def _raise_event(player_id: int, *, pot_after: int = 900) -> dict[str, object]:
    return {
        "player_id": player_id,
        "stage": "flop",
        "round": 1,
        "action_type": "raise",
        "action": 400,
        "stage_bet": 400,
        "committed": 400,
        "pot_after": pot_after,
        "chips_after": 19_600,
    }


def test_actor_swap_that_collides_in_legacy15_differs_in_new_schema() -> None:
    hero_event = _raise_event(0)
    opponent_event = _raise_event(1)

    legacy_hero = history_schema.encode_history_sequence(
        {"pot": 2_500, "history": [hero_event]},
        0,
        schema=history_schema.LEGACY_HISTORY_SCHEMA,
    )
    legacy_opponent = history_schema.encode_history_sequence(
        {"pot": 2_500, "history": [opponent_event]},
        0,
        schema=history_schema.LEGACY_HISTORY_SCHEMA,
    )
    current_hero = history_schema.encode_history_sequence([hero_event], 0)
    current_opponent = history_schema.encode_history_sequence([opponent_event], 0)

    assert legacy_hero == legacy_opponent
    assert current_hero != current_opponent
    assert current_hero[0][history_schema.ACTOR_FEATURE_SLICE] == [1.0, 0.0, 0.0]
    assert current_opponent[0][history_schema.ACTOR_FEATURE_SLICE] == [0.0, 1.0, 0.0]


def test_same_action_uses_each_events_cumulative_pot() -> None:
    small_pot = _raise_event(1, pot_after=600)
    large_pot = _raise_event(1, pot_after=1_200)
    rows = history_schema.encode_history_sequence(
        {"pot": 39_000, "history": [small_pot, large_pot]}, 0
    )
    index = history_schema.HISTORY_FEATURE_INDEX

    assert rows[0][index["pot_after_norm"]] == 600 / 40_000
    assert rows[1][index["pot_after_norm"]] == 1_200 / 40_000
    assert rows[0][index["wager_to_pot_after"]] == 400 / 600
    assert rows[1][index["wager_to_pot_after"]] == 400 / 1_200
    assert rows[0] != rows[1]

    # A decision-time pot must not leak backward into actor-aware event rows.
    other_request_pot = history_schema.encode_history_sequence(
        {"pot": 1, "history": [small_pot, large_pot]}, 0
    )
    assert rows == other_request_pot


def test_actor_encoding_converts_between_hero_and_opponent_perspectives() -> None:
    event = _raise_event(0)
    hero_view = history_schema.encode_history_sequence([event], my_id=0)[0]
    opponent_view = history_schema.encode_history_sequence([event], my_id=1)[0]
    unknown_view = history_schema.encode_history_sequence(
        [{"actor_id": 9, "action_type": "check", "round": 2}], my_id=0
    )[0]

    actor = history_schema.ACTOR_FEATURE_SLICE
    assert hero_view[actor] == [1.0, 0.0, 0.0]
    assert opponent_view[actor] == [0.0, 1.0, 0.0]
    assert unknown_view[actor] == [0.0, 0.0, 1.0]
    assert hero_view[history_schema.STREET_FEATURE_SLICE] == opponent_view[
        history_schema.STREET_FEATURE_SLICE
    ]
    assert hero_view[history_schema.ACTION_FEATURE_SLICE] == opponent_view[
        history_schema.ACTION_FEATURE_SLICE
    ]


def test_rows_have_declared_dimension_and_bounds() -> None:
    history = [
        {
            **_raise_event(0),
            "amount": 10**9,
            "stage_bet": 10**9,
            "committed": 10**9,
            "pot_after": 10**9,
            "chips_after": 10**9,
            "effective_stack_after": 9_000,
        },
        {
            "player_id": "bad",
            "round": 99,
            "action_type": "bad",
            "pot_after": float("nan"),
            "chips_after": float("inf"),
        },
        None,
    ]

    rows = history_schema.encode_history_sequence(history, my_id=0)

    assert len(rows) == len(history)
    assert history_schema.HISTORY_FEATURE_DIM == len(
        history_schema.HISTORY_FEATURE_NAMES
    )
    assert (
        len(history_schema.HISTORY_FEATURE_BOUNDS)
        == history_schema.HISTORY_FEATURE_DIM
    )
    for row in rows:
        assert len(row) == history_schema.HISTORY_FEATURE_DIM
        assert all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in row)
        assert sum(row[history_schema.ACTOR_FEATURE_SLICE]) == 1.0
        assert sum(row[history_schema.STREET_FEATURE_SLICE]) == 1.0
        assert sum(row[history_schema.ACTION_FEATURE_SLICE]) == 1.0


def test_missing_fields_are_encoded_without_using_request_state() -> None:
    rows = history_schema.encode_history_sequence(
        {"pot": 12_345, "chips": [10_000, 10_000], "history": [{}, None]},
        my_id=None,
    )
    index = history_schema.HISTORY_FEATURE_INDEX

    assert len(rows) == 2
    for row in rows:
        assert row[history_schema.ACTOR_FEATURE_SLICE] == [0.0, 0.0, 1.0]
        assert row[history_schema.STREET_FEATURE_SLICE] == [0.0, 0.0, 0.0, 0.0, 1.0]
        assert row[history_schema.ACTION_FEATURE_SLICE] == [
            0.0, 0.0, 0.0, 0.0, 0.0, 1.0
        ]
        assert row[index["pot_after_norm"]] == 0.0
        assert row[index["pot_after_known"]] == 0.0
        assert row[index["stack_after_known"]] == 0.0
    assert history_schema.encode_history_sequence({}, my_id=0) == []
    assert history_schema.encode_history_sequence(None, my_id=0) == []


def test_event_stack_context_and_schema_metadata_are_explicit() -> None:
    event = {
        **_raise_event(1),
        "committed": 1_000,
        "chips_after": 15_000,
        "effective_stack_after": 9_000,
    }
    row = history_schema.encode_history_sequence([event], my_id=0)[0]
    index = history_schema.HISTORY_FEATURE_INDEX
    metadata = history_schema.history_feature_metadata()

    assert row[index["chips_after_norm"]] == 15_000 / 20_000
    assert row[index["effective_stack_norm"]] == 9_000 / 20_000
    assert row[index["wager_to_effective_stack_before"]] == 1_000 / 10_000
    assert metadata["schema"] == history_schema.CURRENT_HAND_HISTORY_SCHEMA
    assert metadata["history_feature_dim"] == history_schema.HISTORY_FEATURE_DIM
    assert metadata["uses_event_pot_after"] is True
    assert all(bounds == [0.0, 1.0] for bounds in metadata["feature_bounds"])


def test_unknown_schema_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported history feature schema"):
        history_schema.encode_history_sequence([], 0, schema="unknown")
