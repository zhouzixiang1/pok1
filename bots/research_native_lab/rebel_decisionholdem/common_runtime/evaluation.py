"""Exact Kuhn policy evaluation, deterministic best response and exploitability."""

from __future__ import annotations

from itertools import product

from .kuhn import (
    Deal,
    InfoSet,
    StrategyProfile,
    current_player,
    infosets_for_player,
    is_terminal,
    legal_actions,
    next_history,
    ordered_deals,
    terminal_utility,
    validate_strategy,
)


def deal_expected_utility(
    deal: Deal,
    history: str,
    profile: StrategyProfile,
    *,
    player: int = 0,
) -> float:
    """Evaluate one fixed private-card deal from an arbitrary public history."""

    if is_terminal(history):
        return terminal_utility(deal, history, player)
    actor = current_player(history)
    key = (actor, deal[actor], history)
    probabilities = profile[key]
    return sum(
        probabilities[action]
        * deal_expected_utility(
            deal, next_history(history, action), profile, player=player
        )
        for action in legal_actions(history)
    )


def expected_utility(profile: StrategyProfile, *, player: int = 0) -> float:
    validate_strategy(profile)
    deals = ordered_deals()
    return sum(
        deal_expected_utility(deal, "", profile, player=player) for deal in deals
    ) / len(deals)


def _pure_best_response_profiles(
    profile: StrategyProfile, player: int
):
    infosets = infosets_for_player(player)
    action_spaces = [legal_actions(key[2]) for key in infosets]
    for choices in product(*action_spaces):
        candidate = {
            key: dict(probabilities) for key, probabilities in profile.items()
        }
        for key, chosen_action in zip(infosets, choices, strict=True):
            candidate[key] = {
                action: float(action == chosen_action)
                for action in legal_actions(key[2])
            }
        yield candidate


def best_response_value(profile: StrategyProfile, player: int) -> float:
    """Return exact BR payoff by enumerating all deterministic Kuhn policies."""

    validate_strategy(profile)
    if player not in (0, 1):
        raise ValueError(f"invalid player: {player}")
    return max(
        expected_utility(candidate, player=player)
        for candidate in _pure_best_response_profiles(profile, player)
    )


def nash_conv(profile: StrategyProfile) -> float:
    """Return two-player zero-sum NashConv (sum of deviation incentives)."""

    return best_response_value(profile, 0) + best_response_value(profile, 1)


def exploitability(profile: StrategyProfile) -> float:
    """Return the conventional average per-player exploitability."""

    return nash_conv(profile) / 2.0
