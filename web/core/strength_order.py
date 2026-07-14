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
PRECOMMIT_PARENT_MIN_SAMPLES = 4
PRECOMMIT_PARENT_MAX_SCORE = 0.40
PRECOMMIT_AGGREGATE_MIN_SAMPLES = 8
PRECOMMIT_AGGREGATE_MIN_LOSS_MARGIN = 2
# Strict-policy precommit has no telemetry-only opponent class.  Every ordinary
# published-bot matchup is a gate and strength sample; the first-strict control
# uses explicit typed admission flags instead of a magic reason string.
NON_STRENGTH_MATCHUP_REASONS = frozenset()


def is_precommit_gate_matchup(matchup: dict[str, Any]) -> bool:
    """Whether complete samples from this matchup may decide precommit.

    The first-strict system control is a hard local regression authority but
    deliberately not a strength/rating authority.  Telemetry probes remain
    excluded from both channels.
    """

    if "precommit_gate_admitted" in matchup:
        return matchup.get("precommit_gate_admitted") is True
    return str(matchup.get("reason") or "") not in NON_STRENGTH_MATCHUP_REASONS


def is_strength_matchup(matchup: dict[str, Any]) -> bool:
    if not is_precommit_gate_matchup(matchup):
        return False
    if "strength_admitted" in matchup:
        return matchup.get("strength_admitted") is True
    if matchup.get("strength_authoritative") is False:
        return False
    return True


def match_score(wins: int | float, draws: int | float, games: int | float) -> float | None:
    """Return outcome points per match, with every draw worth exactly 0.5."""

    try:
        wins = float(wins)
        draws = float(draws)
        games = float(games)
    except (TypeError, ValueError):
        return None
    if games <= 0 or wins < 0 or draws < 0 or wins + draws > games:
        return None
    return (wins + 0.5 * draws) / games


def summarize_match_outcomes(wins: int, losses: int, draws: int) -> dict[str, Any]:
    """Summarize W/L/D using the national match scoring contract.

    A draw is worth half a point.  Counts are deliberately kept separate from
    chip amounts so callers cannot accidentally turn a large pot into multiple
    rating observations or treat a zero-chip match as a loss.
    """

    counts = tuple(int(value) for value in (wins, losses, draws))
    if any(value < 0 for value in counts):
        raise ValueError("match outcome counts must be non-negative")
    wins, losses, draws = counts
    samples = wins + losses + draws
    points = wins + 0.5 * draws
    return {
        "wins": wins,
        "losses": losses,
        "draws": draws,
        "samples": samples,
        "points": points,
        "primary_match_score": points / samples if samples else None,
        "win_loss_margin": wins - losses,
    }


def precommit_outcome_blockers(
    matchups: Iterable[dict[str, Any]],
    *,
    parent_label: str = "",
    aggregate_reason: str = "aggregate_native_regression",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Apply the production precommit gate to complete-match outcomes.

    The parent gate catches a clear matchup collapse after at least four
    samples.  The aggregate gate keeps the established two-match loss-margin
    tolerance after at least eight samples.  Both operate exclusively on W/L/D
    points; net-chip magnitude is intentionally not accepted as an argument.
    """

    blockers: list[dict[str, Any]] = []
    total_wins = total_losses = total_draws = 0
    per_matchup: list[dict[str, Any]] = []
    for matchup in matchups:
        if not is_precommit_gate_matchup(matchup):
            continue
        summary = summarize_match_outcomes(
            matchup.get("wins", 0),
            matchup.get("losses", 0),
            matchup.get("draws", 0),
        )
        opponent = str(matchup.get("opponent") or "")
        per_matchup.append({"opponent": opponent, **summary})
        total_wins += summary["wins"]
        total_losses += summary["losses"]
        total_draws += summary["draws"]
        if (
            parent_label
            and opponent == parent_label
            and summary["samples"] >= PRECOMMIT_PARENT_MIN_SAMPLES
            and summary["primary_match_score"] <= PRECOMMIT_PARENT_MAX_SCORE
        ):
            blockers.append({
                "reason": "lost_to_parent",
                "opponent": opponent,
                "details": (
                    f"70-hand outcomes {summary['wins']}W-{summary['losses']}L-"
                    f"{summary['draws']}D score={summary['primary_match_score']:.3f}; "
                    f"block at score<={PRECOMMIT_PARENT_MAX_SCORE:.2f} with "
                    f"n>={PRECOMMIT_PARENT_MIN_SAMPLES}."
                ),
            })

    aggregate = summarize_match_outcomes(total_wins, total_losses, total_draws)
    if (
        aggregate["samples"] >= PRECOMMIT_AGGREGATE_MIN_SAMPLES
        and aggregate["losses"] >= (
            aggregate["wins"] + PRECOMMIT_AGGREGATE_MIN_LOSS_MARGIN
        )
    ):
        blockers.append({
            "reason": aggregate_reason,
            "details": (
                f"Aggregate 70-hand outcomes {aggregate['wins']}W-"
                f"{aggregate['losses']}L-{aggregate['draws']}D "
                f"score={aggregate['primary_match_score']:.3f}; loss margin "
                f">={PRECOMMIT_AGGREGATE_MIN_LOSS_MARGIN} with "
                f"n>={PRECOMMIT_AGGREGATE_MIN_SAMPLES}."
            ),
        })
    return blockers, {
        **aggregate,
        "per_matchup": per_matchup,
        "parent_min_samples": PRECOMMIT_PARENT_MIN_SAMPLES,
        "parent_max_score": PRECOMMIT_PARENT_MAX_SCORE,
        "aggregate_min_samples": PRECOMMIT_AGGREGATE_MIN_SAMPLES,
        "aggregate_min_loss_margin": PRECOMMIT_AGGREGATE_MIN_LOSS_MARGIN,
        "gate_basis": "complete_70_hand_match_outcomes",
        "draw_score": 0.5,
    }


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
