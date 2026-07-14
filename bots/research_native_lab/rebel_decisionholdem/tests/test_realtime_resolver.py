"""Tests for the A2 depth-limited real-time resolver."""

from __future__ import annotations

import pytest

from bots.research_native_lab.common_contracts.actions import Action, ActionKind
from bots.research_native_lab.common_contracts.national_state import NationalGameState
from bots.research_native_lab.rebel_decisionholdem.decisionholdem_like.realtime_resolver import (
    RESOLVER_SCHEMA,
    ResolveConfig,
    resolve_public_state,
    should_resolve,
)
from bots.research_native_lab.rebel_decisionholdem.decisionholdem_like.hunl_abstraction import (
    information_abstraction,
    abstract_actions,
)


def test_resolver_produces_valid_strategy_distribution():
    """Resolver output is a valid probability distribution over actions."""
    state = NationalGameState.new_hand(1, small_blind=0, hole_cards=((0, 4), (8, 12)))
    abstraction = information_abstraction(state, state.actor if state.actor is not None else 0)
    action_specs = abstract_actions(state)
    actions = [spec.action_id for spec in action_specs]
    blueprint = {a: 1.0 / len(actions) for a in actions}

    result = resolve_public_state(
        state,
        config=ResolveConfig(iterations=10, depth=1, seed=42),
        blueprint_strategy=blueprint,
        abstraction=abstraction,
    )

    assert result.schema == RESOLVER_SCHEMA
    assert len(result.resolved_strategy) == len(actions)
    assert all(p >= 0.0 for p in result.resolved_strategy)
    assert abs(sum(result.resolved_strategy) - 1.0) < 1e-6


def test_resolver_safe_method_runs_without_error():
    """Safe resolve with exploitability floor executes without error."""
    state = NationalGameState.new_hand(1, small_blind=0, hole_cards=((0, 4), (8, 12)))
    abstraction = information_abstraction(state, state.actor if state.actor is not None else 0)
    action_specs = abstract_actions(state)
    actions = [spec.action_id for spec in action_specs]
    blueprint = {a: 1.0 / len(actions) for a in actions}

    result = resolve_public_state(
        state,
        config=ResolveConfig(
            iterations=5, depth=1, seed=42, method="safe",
            exploitability_floor_margin=2.0,
        ),
        blueprint_strategy=blueprint,
        abstraction=abstraction,
    )

    assert result.method == "safe"
    assert abs(sum(result.resolved_strategy) - 1.0) < 1e-6


def test_should_resolve_returns_false_preflop_and_low_time():
    """Resolver is not triggered preflop or with insufficient time."""
    preflop = NationalGameState.new_hand(1, small_blind=0)
    assert not should_resolve(preflop, {}, time_budget_ms=5000.0)

    # Even with time, preflop doesn't trigger
    assert not should_resolve(preflop, {}, time_budget_ms=5000.0)


def test_resolver_rejects_invalid_config():
    """Invalid configuration parameters are rejected."""
    with pytest.raises(ValueError, match="iterations"):
        ResolveConfig(iterations=0).validated()
    with pytest.raises(ValueError, match="depth"):
        ResolveConfig(depth=0).validated()
    with pytest.raises(ValueError, match="method"):
        ResolveConfig(method="unknown").validated()
