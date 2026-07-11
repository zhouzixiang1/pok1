"""Authoritative ordering contract for national-bot strength.

One sample is one complete 70-hand national match.  Its primary outcome is the
sign of final net chips; chip magnitude is retained only as a secondary signal.
Official-EXE evidence is deliberately absent because it has compliance, not
strength, authority.
"""

from __future__ import annotations

import math
from typing import Any, Iterable


STRENGTH_ORDER_SCHEMA_VERSION = 1
NATIONAL_STRENGTH_HANDS = 70
NATIONAL_STRENGTH_SAMPLE_UNIT = "70_hand_match"


def summarize_70_hand_net_chips(samples: Iterable[int | float]) -> dict[str, Any]:
    values = [int(value) for value in samples]
    wins = sum(1 for value in values if value > 0)
    losses = sum(1 for value in values if value < 0)
    draws = len(values) - wins - losses
    total = sum(values)
    count = len(values)
    return {
        "schema_version": STRENGTH_ORDER_SCHEMA_VERSION,
        "sample_unit": NATIONAL_STRENGTH_SAMPLE_UNIT,
        "hands_per_sample": NATIONAL_STRENGTH_HANDS,
        "samples": count,
        "positive_matches": wins,
        "negative_matches": losses,
        "zero_matches": draws,
        "primary_match_score": (
            (wins + 0.5 * draws) / count
            if count
            else None
        ),
        "secondary_net_chips_total": total,
        "secondary_net_chips_mean": (total / count if count else None),
    }


def strength_order_key(row: dict[str, Any]) -> tuple[float, ...]:
    """Return the lexicographic order used by the dashboard and evolution.

    ``selection_score`` is the confidence-discounted, opponent-adjusted score
    derived exclusively from positive/negative/zero 70-hand outcomes.  Net-chip
    magnitude follows it and therefore can only break equal primary strength.
    """

    primary = row.get("selection_score", row.get("leaderboard_score"))
    secondary = row.get("secondary_net_chips_mean")
    return (
        float(primary) if primary is not None else -math.inf,
        float(secondary) if secondary is not None else -math.inf,
        float(row.get("leaderboard_score"))
        if row.get("leaderboard_score") is not None
        else -math.inf,
        float(row.get("h2h_weighted_wr"))
        if row.get("h2h_weighted_wr") is not None
        else -math.inf,
        float(row.get("conservative_rating"))
        if row.get("conservative_rating") is not None
        else -math.inf,
        float(row.get("rating")) if row.get("rating") is not None else -math.inf,
    )
