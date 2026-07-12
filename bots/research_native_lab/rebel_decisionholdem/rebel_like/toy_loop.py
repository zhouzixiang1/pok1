"""Deterministic A1 PBS -> action -> Bayes update -> value-label toy loop."""

from __future__ import annotations

from random import Random

from ..common_runtime.kuhn import (
    CARDS,
    Deal,
    StrategyProfile,
    current_player,
    is_terminal,
    legal_actions,
    terminal_utility,
)
from .pbs import KuhnPublicBeliefState


def fixture_policy() -> StrategyProfile:
    """Return a fixed non-trained policy that makes belief updates observable."""

    profile: StrategyProfile = {}
    root_bet = {0: 0.25, 1: 0.50, 2: 0.75}
    facing_bet_call = {0: 0.10, 1: 0.50, 2: 0.90}
    after_check_bet = {0: 0.20, 1: 0.50, 2: 0.80}
    for card in CARDS:
        profile[(0, card, "")] = {
            "check": 1.0 - root_bet[card],
            "bet": root_bet[card],
        }
        profile[(1, card, "check")] = {
            "check": 1.0 - after_check_bet[card],
            "bet": after_check_bet[card],
        }
        profile[(1, card, "bet")] = {
            "fold": 1.0 - facing_bet_call[card],
            "call": facing_bet_call[card],
        }
        profile[(0, card, "check-bet")] = {
            "fold": 1.0 - facing_bet_call[card],
            "call": facing_bet_call[card],
        }
    return profile


def _sample_action(probabilities: dict[str, float], actions: tuple[str, ...], rng: Random) -> str:
    threshold = rng.random()
    cumulative = 0.0
    for action in actions:
        cumulative += probabilities[action]
        if threshold < cumulative:
            return action
    return actions[-1]


def run_toy_selfplay(deal: Deal = (0, 2), seed: int = 7) -> dict[str, object]:
    """Run a complete toy hand and retain every exact PBS transition."""

    if deal[0] == deal[1] or any(card not in CARDS for card in deal):
        raise ValueError(f"invalid Kuhn deal: {deal}")
    rng = Random(seed)
    profile = fixture_policy()
    pbs = KuhnPublicBeliefState.initial()
    trace: list[dict[str, object]] = []

    while not is_terminal(pbs.history):
        actor = current_player(pbs.history)
        actions = legal_actions(pbs.history)
        probabilities = profile[(actor, deal[actor], pbs.history)]
        action = _sample_action(probabilities, actions, rng)
        card_policy = {
            card: profile[(actor, card, pbs.history)] for card in CARDS
        }
        before = pbs
        action_probability = before.action_probability(action, card_policy)
        pbs = before.observe(action, card_policy)
        trace.append(
            {
                "actor": actor,
                "action": action,
                "public_action_probability": action_probability,
                "before": before.snapshot(),
                "after": pbs.snapshot(),
            }
        )

    return {
        "route": "A1-rebel-like-toy-pbs",
        "fidelity": "paper-faithful clean-room PBS update; no learned value/search",
        "seed": seed,
        "deal": list(deal),
        "trace": trace,
        "terminal_history": pbs.history,
        "utility": [
            terminal_utility(deal, pbs.history, 0),
            terminal_utility(deal, pbs.history, 1),
        ],
        "terminal_pbs": pbs.snapshot(),
    }
