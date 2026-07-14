"""Programmatic A2 small-game validation entry point."""

from __future__ import annotations

from ..common_runtime.evaluation import exploitability, nash_conv
from ..common_runtime.kuhn import uniform_strategy
from .linear_cfr import LinearCFR
from .resolving import CoinTossResolveGame


def run_validation(iterations: int = 10_000) -> dict[str, object]:
    solver = LinearCFR()
    uniform = uniform_strategy()
    initial = {
        "nash_conv": nash_conv(uniform),
        "exploitability": exploitability(uniform),
    }
    solver.train(iterations)
    trained = solver.average_strategy()
    coin_toss = CoinTossResolveGame()
    return {
        "route": "A2-decisionholdem-like-small-game",
        "fidelity": {
            "linear_cfr": (
                "paper-faithful clean-room toy LCFR; unresolved DecisionHoldem "
                "LCFR-vs-MCCFR blueprint conflict"
            ),
            "safe_resolve": (
                "source-shaped Coin Toss unsafe-reach plus simplified Sell-payoff "
                "constraint; not full Resolve and not DecisionHoldem"
            ),
        },
        "iterations": solver.iterations_completed,
        "checkpoint_sha256": solver.checkpoint_digest(),
        "uniform": initial,
        "trained": {
            "nash_conv": nash_conv(trained),
            "exploitability": exploitability(trained),
        },
        "plain_resolve": coin_toss.plain_resolve().to_dict(),
        "safe_resolve": coin_toss.safe_resolve().to_dict(),
    }
