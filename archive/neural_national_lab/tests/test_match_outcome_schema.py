from __future__ import annotations

import copy
from pathlib import Path
import sys

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import match_outcome_schema as outcome  # noqa: E402


def _row() -> dict:
    mask = [0, 0, 1, 1, 0, 1]
    return {
        "_collection_hands": 70,
        "legal_mask": mask,
        "rule_label_id": 2,
        "baseline_match_net_chips": -100.0,
        "match_delta_vs_rule": [
            None, None, 0.0, 150.0, None, -50.0,
        ],
        "match_action_values": [
            None, None, -100.0, 50.0, None, -150.0,
        ],
        "target_masks": {"match_delta_vs_rule": mask},
        "probes": [{
            "status": "ok",
            "force_confirmed": True,
            "forced_label": "raise_pot",
            "forced_match_net_chips": 50.0,
            "match_delta_vs_rule": 150.0,
        }],
    }


def test_derives_strict_positive_outcome_and_uplift_targets() -> None:
    supervision = outcome.derive_match_outcome_supervision(_row(), required=True)

    assert supervision is not None
    assert supervision["schema"] == outcome.MATCH_OUTCOME_SCHEMA
    assert supervision["baseline_match_positive"] == 0
    assert supervision["match_positive_targets"] == [0, 0, 0, 1, 0, 0]
    assert supervision["match_positive_uplift_targets"] == [0, 0, 0, 1, 0, 0]
    assert outcome.candidate_outcome(supervision, 3) == {
        "match_outcome_schema": outcome.MATCH_OUTCOME_SCHEMA,
        "forced_match_net_chips": 50.0,
        "forced_match_positive": 1,
        "match_positive_uplift": 1,
    }


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda row: row["match_action_values"].__setitem__(3, 49.0),
            "absolute match target disagrees",
        ),
        (
            lambda row: row["probes"][0].__setitem__(
                "forced_match_net_chips", 49.0
            ),
            "confirmed probe disagrees",
        ),
        (
            lambda row: row.__setitem__("_collection_hands", 69),
            "requires 70 hands",
        ),
    ],
)
def test_rejects_inconsistent_or_non_70_hand_evidence(mutation, message: str) -> None:
    row = copy.deepcopy(_row())
    mutation(row)

    with pytest.raises(ValueError, match=message):
        outcome.derive_match_outcome_supervision(row, required=True)


def test_old_short_match_is_ignored_only_in_optional_v3_mode() -> None:
    row = _row()
    row["_collection_hands"] = 2

    assert outcome.derive_match_outcome_supervision(row, required=False) is None
    with pytest.raises(ValueError, match="requires 70 hands"):
        outcome.derive_match_outcome_supervision(row, required=True)
