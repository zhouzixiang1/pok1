"""National-platform evaluation backend for evolution gates.

This module evaluates existing bot directories through the national GameEngine
in process. It is intentionally separate from ``national_acceptance``: acceptance
answers "is this protocol-clean?", while this module answers "can this candidate
survive the final precommit performance gate under national 70-hand rules?".
"""

from __future__ import annotations

import statistics
from pathlib import Path
from typing import Any

from eval_stats import paired_bootstrap_ci
from .national_acceptance import _critical_adapter_issues, resolve_bot, run_pair
from strength_order import is_strength_matchup, precommit_outcome_blockers


def _mean(values: list[int]) -> float | None:
    return (sum(values) / len(values)) if values else None


def _rounded(value: float | None) -> float | None:
    return round(value, 1) if value is not None else None


def _ci(values: list[int]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    lo, hi = paired_bootstrap_ci(values)
    return lo, hi


def _candidate_compliance_issues(result: dict[str, Any], candidate_label: str, *, strict: bool) -> list[str]:
    per_player = result.get("per_player", {})
    pdata = per_player.get(candidate_label, {})
    issues: list[str] = []
    illegal = int(pdata.get("illegal_actions", 0) or 0)
    timeouts = int(pdata.get("timeouts", 0) or 0)
    if illegal:
        issues.append(f"illegal_actions={illegal}")
    if timeouts:
        issues.append(f"timeouts={timeouts}")
    for detail in _critical_adapter_issues(dict(pdata.get("adapter", {}) or {}), strict=strict):
        issues.append(detail)
    return issues


def _opponent_compliance_issues(result: dict[str, Any], candidate_label: str, *, strict: bool) -> list[str]:
    per_player = result.get("per_player", {})
    issues: list[str] = []
    for label, pdata in per_player.items():
        if label == candidate_label:
            continue
        illegal = int(pdata.get("illegal_actions", 0) or 0)
        timeouts = int(pdata.get("timeouts", 0) or 0)
        parts = []
        if illegal:
            parts.append(f"illegal_actions={illegal}")
        if timeouts:
            parts.append(f"timeouts={timeouts}")
        parts.extend(_critical_adapter_issues(dict(pdata.get("adapter", {}) or {}), strict=strict))
        if parts:
            issues.append(f"{label}: " + "; ".join(parts))
    return issues


async def run_national_precommit(
    candidate_token: str | Path,
    opponents: list[dict[str, Any]],
    *,
    hands: int = 70,
    matches_per_opponent: int = 1,
    strict: bool = True,
    parent_label: str = "",
    deck_seed_base: int | None = 91_000,
) -> dict[str, Any]:
    """Run national 70-hand precommit matchups for one candidate.

    ``opponents`` uses the same shape as ``tool_eval._select_precommit_opponents``
    plus an optional ``path`` key. The candidate is always bot A, so positive net
    chips mean candidate advantage.
    """
    candidate = resolve_bot(candidate_token)
    hands = int(hands)
    if hands != 70:
        raise ValueError(
            f"national precommit strength samples must contain exactly 70 hands; got {hands}"
        )
    matches_per_opponent = max(1, int(matches_per_opponent))

    matchups: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    aggregate_net_chips: list[int] = []
    total_wins = 0
    total_losses = 0
    total_draws = 0
    resolved_opponents: list[dict[str, Any]] = []

    if not opponents:
        blockers.append({
            "reason": "national_no_opponents",
            "details": "National precommit requires at least one resolved opponent.",
        })

    for opp_index, item in enumerate(opponents):
        reason = str(item.get("reason") or "precommit")
        strength_authoritative = is_strength_matchup(item)
        token = item.get("path") or item.get("token") or item.get("name")
        opponent = resolve_bot(token)
        resolved_opponents.append({
            "name": item.get("name") or opponent.label,
            "reason": reason,
            "path": str(opponent.path),
        })

        samples: list[int] = []
        repeats: list[dict[str, Any]] = []
        candidate_issues: list[str] = []
        opponent_issues: list[str] = []
        hands_played_total = 0

        for repeat in range(matches_per_opponent):
            seed = None
            if deck_seed_base is not None:
                seed = int(deck_seed_base) + (opp_index * 100_000) + (repeat * 1_000)
            result = await run_pair(candidate, opponent, hands, strict=strict, deck_seed_base=seed)
            net = int(result.get("net_chips_a", 0) or 0)
            hands_played = int(result.get("hands_played", 0) or 0)
            hands_played_total += hands_played
            c_issues = _candidate_compliance_issues(result, candidate.label, strict=strict)
            o_issues = _opponent_compliance_issues(result, candidate.label, strict=strict)
            complete = hands_played == hands
            compliance_passed = bool(result.get("passed_compliance", False))
            sample_valid = complete and compliance_passed and not c_issues and not o_issues
            strength_admitted = strength_authoritative and sample_valid
            if sample_valid:
                samples.append(net)
            if strength_admitted:
                aggregate_net_chips.append(net)
            candidate_issues.extend(c_issues)
            opponent_issues.extend(o_issues)
            repeats.append({
                "repeat": repeat + 1,
                "deck_seed_base": seed,
                "hands_played": hands_played,
                "net_chips": net,
                "candidate_issues": c_issues,
                "opponent_issues": o_issues,
                "complete": complete,
                "passed_compliance": compliance_passed,
                "sample_valid": sample_valid,
                "strength_admitted": strength_admitted,
                "raw": result,
            })

        wins = sum(1 for value in samples if value > 0)
        losses = sum(1 for value in samples if value < 0)
        draws = sum(1 for value in samples if value == 0)
        if strength_authoritative:
            total_wins += wins
            total_losses += losses
            total_draws += draws
        mean = _mean(samples)
        ci_lo, ci_hi = _ci(samples)

        matchup = {
            "opponent": item.get("name") or opponent.label,
            "reason": reason,
            "strength_authoritative": strength_authoritative,
            "protocol": "national",
            "hands_per_match": hands,
            "matches": matches_per_opponent,
            "wins": wins,
            "losses": losses,
            "draws": draws,
            "n_played": len(samples),
            "samples_expected": matches_per_opponent,
            "hands_played_total": hands_played_total,
            "net_chips": samples,
            "net_chips_mean": _rounded(mean),
            "net_chip_ci": [_rounded(ci_lo), _rounded(ci_hi)],
            "candidate_compliance_issues": candidate_issues,
            "opponent_compliance_issues": opponent_issues,
            "repeats": repeats,
        }
        matchups.append(matchup)

        if strength_authoritative and candidate_issues:
            blockers.append({
                "reason": "national_candidate_compliance",
                "opponent": matchup["opponent"],
                "details": "; ".join(candidate_issues[:5]),
            })
        if strength_authoritative and opponent_issues:
            blockers.append({
                "reason": "national_opponent_compliance",
                "opponent": matchup["opponent"],
                "details": "; ".join(opponent_issues[:5]),
            })
        if strength_authoritative and any(not row["complete"] for row in repeats):
            blockers.append({
                "reason": "national_incomplete_match",
                "opponent": matchup["opponent"],
                "details": f"{hands_played_total}/{hands * matches_per_opponent} hands completed",
            })
        if strength_authoritative and len(samples) != matches_per_opponent:
            blockers.append({
                "reason": "national_strength_sample_shortfall",
                "opponent": matchup["opponent"],
                "details": f"{len(samples)}/{matches_per_opponent} complete compliant 70-hand samples admitted",
            })

    agg_mean = _mean(aggregate_net_chips)
    agg_ci_lower, agg_ci_upper = _ci(aggregate_net_chips)
    if not aggregate_net_chips:
        blockers.append({
            "reason": "national_no_samples",
            "details": "National precommit produced zero completed match samples.",
        })
    outcome_blockers, outcome_gate = precommit_outcome_blockers(
        matchups,
        parent_label=parent_label,
        aggregate_reason="aggregate_national_regression",
    )
    blockers.extend(outcome_blockers)

    paired_payload = {
        "protocol": "national",
        "hands_per_match": hands,
        "matches_per_opponent": matches_per_opponent,
        "aggregate_ci_lower": _rounded(agg_ci_lower),
        "aggregate_ci_upper": _rounded(agg_ci_upper),
        "aggregate_threshold": None,
        "aggregate_gate_bound": outcome_gate.get("primary_match_score"),
        "aggregate_gate_rule": "complete_70_hand_wld_loss_margin",
        "outcome_gate": outcome_gate,
        "net_chips_samples": len(aggregate_net_chips),
        "gate_degraded": len(aggregate_net_chips) < 2,
        "net_chips_mean": _rounded(agg_mean),
        "net_chips_std": (
            round(statistics.pstdev(aggregate_net_chips), 1)
            if len(aggregate_net_chips) > 1 else None
        ),
        "net_chips_min": min(aggregate_net_chips) if aggregate_net_chips else None,
        "net_chips_max": max(aggregate_net_chips) if aggregate_net_chips else None,
        "secondary_net_chip_ci": [_rounded(agg_ci_lower), _rounded(agg_ci_upper)],
    }

    return {
        "evaluation_protocol": "national",
        "candidate": candidate.label,
        "candidate_path": str(candidate.path),
        "opponents": resolved_opponents,
        "matchups": matchups,
        "total_wins": total_wins,
        "total_losses": total_losses,
        "total_draws": total_draws,
        "aggregate_net_chips": aggregate_net_chips,
        "paired_bootstrap": paired_payload,
        "blockers": blockers,
        "passed": not blockers,
    }
