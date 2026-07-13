"""Exact value, deterministic best response and exploitability for Leduc."""

from __future__ import annotations

from collections import defaultdict
from typing import Mapping

from .leduc import (
    LeducDeal,
    LeducInfoSet,
    LeducState,
    LeducStrategy,
    apply_action,
    information_set,
    initial_state,
    legal_actions,
    ordered_deals,
    terminal_utility,
    validate_strategy,
)


def _profile_value(
    state: LeducState,
    deal: LeducDeal,
    profile: LeducStrategy,
    player: int,
) -> float:
    if state.terminal:
        return terminal_utility(state, deal, player)
    key = information_set(state, deal)
    return sum(
        profile[key][action]
        * _profile_value(apply_action(state, action), deal, profile, player)
        for action in legal_actions(state)
    )


def expected_utility(profile: LeducStrategy, player: int = 0) -> float:
    validate_strategy(profile)
    if player not in (0, 1):
        raise ValueError(f"invalid player: {player}")
    chance = 1.0 / len(ordered_deals())
    return sum(
        chance * _profile_value(initial_state(), deal, profile, player)
        for deal in ordered_deals()
    )


def _collect_response_nodes(
    opponent_profile: LeducStrategy,
    responding_player: int,
) -> dict[LeducInfoSet, list[tuple[LeducDeal, LeducState, float]]]:
    nodes: dict[LeducInfoSet, list[tuple[LeducDeal, LeducState, float]]] = defaultdict(list)
    chance = 1.0 / len(ordered_deals())

    def visit(
        state: LeducState,
        deal: LeducDeal,
        opponent_reach: float,
    ) -> None:
        if state.terminal:
            return
        key = information_set(state, deal)
        if state.actor == responding_player:
            nodes[key].append((deal, state, chance * opponent_reach))
            for action in legal_actions(state):
                visit(apply_action(state, action), deal, opponent_reach)
            return
        for action in legal_actions(state):
            probability = opponent_profile[key][action]
            if probability > 0.0:
                visit(
                    apply_action(state, action),
                    deal,
                    opponent_reach * probability,
                )

    for deal in ordered_deals():
        visit(initial_state(), deal, 1.0)
    return dict(nodes)


def _response_continuation(
    state: LeducState,
    deal: LeducDeal,
    responding_player: int,
    opponent_profile: LeducStrategy,
    response_policy: Mapping[LeducInfoSet, str],
) -> float:
    if state.terminal:
        return terminal_utility(state, deal, responding_player)
    key = information_set(state, deal)
    if state.actor == responding_player:
        try:
            action = response_policy[key]
        except KeyError as exc:
            raise RuntimeError(
                f"deeper best-response action not solved for {key}"
            ) from exc
        return _response_continuation(
            apply_action(state, action),
            deal,
            responding_player,
            opponent_profile,
            response_policy,
        )
    value = 0.0
    for action in legal_actions(state):
        probability = opponent_profile[key][action]
        # Do not descend through a zero-reach opponent branch.  Apart from
        # avoiding needless work, this is required for exact sparse-profile
        # best responses: a responding-player infoset below such a branch is
        # intentionally absent from ``response_policy``.
        if probability <= 0.0:
            continue
        value += probability * _response_continuation(
            apply_action(state, action),
            deal,
            responding_player,
            opponent_profile,
            response_policy,
        )
    return value


def best_response_policy(
    opponent_profile: LeducStrategy,
    responding_player: int,
) -> dict[LeducInfoSet, str]:
    validate_strategy(opponent_profile)
    if responding_player not in (0, 1):
        raise ValueError(f"invalid responding player: {responding_player}")
    nodes = _collect_response_nodes(opponent_profile, responding_player)
    response: dict[LeducInfoSet, str] = {}
    ordered = sorted(
        nodes,
        key=lambda key: max(state.depth for _, state, _ in nodes[key]),
        reverse=True,
    )
    for key in ordered:
        actions = legal_actions(nodes[key][0][1])
        values: list[tuple[float, str]] = []
        for action in actions:
            value = sum(
                weight
                * _response_continuation(
                    apply_action(state, action),
                    deal,
                    responding_player,
                    opponent_profile,
                    response,
                )
                for deal, state, weight in nodes[key]
            )
            values.append((value, action))
        # Legal-action order supplies a deterministic tie break.
        best_value = max(value for value, _ in values)
        response[key] = next(
            action for value, action in values if abs(value - best_value) <= 1e-15
        )
    return response


def best_response_value(
    opponent_profile: LeducStrategy,
    responding_player: int,
) -> float:
    response = best_response_policy(opponent_profile, responding_player)
    chance = 1.0 / len(ordered_deals())
    return sum(
        chance
        * _response_continuation(
            initial_state(),
            deal,
            responding_player,
            opponent_profile,
            response,
        )
        for deal in ordered_deals()
    )


def nash_conv(profile: LeducStrategy) -> float:
    value0 = expected_utility(profile, 0)
    value1 = -value0
    improvement0 = best_response_value(profile, 0) - value0
    improvement1 = best_response_value(profile, 1) - value1
    result = improvement0 + improvement1
    if result < -1e-10:
        raise RuntimeError(f"negative exact Leduc NashConv: {result}")
    return max(0.0, result)


def exploitability(profile: LeducStrategy) -> float:
    return nash_conv(profile) / 2.0
