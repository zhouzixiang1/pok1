"""Exact Coin Toss plain/safe resolving validation oracle.

DecisionHoldem's paper states that its diverse-opponent real-time resolver is
safe but defers the algorithmic details, and the public repository ships the
real-time engine only as ``AlascasiaHoldem.so``.  This module therefore does
*not* claim to reproduce that resolver.  It implements the alternative-payoff
constraint in Brown and Sandholm's public Coin Toss example so the route can
falsify unsafe subgame replacement before any HUNL implementation is attempted.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class ResolveCertificate:
    method: str
    guess_heads_probability: float
    play_values: tuple[float, float]
    alternative_payoffs: tuple[float, float]
    safety_margins: tuple[float, float]
    opponent_best_response_value: float
    blueprint_floor_value: float
    exploitability_delta: float
    safe: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CoinTossResolveGame:
    """The NIPS 2017 Coin Toss example with H/T alternative payoffs."""

    alternative_payoffs: tuple[float, float] = (0.5, -0.5)
    type_prior: tuple[float, float] = (0.5, 0.5)

    def __post_init__(self) -> None:
        if any(probability < 0.0 for probability in self.type_prior):
            raise ValueError("type prior must be non-negative")
        if abs(sum(self.type_prior) - 1.0) > 1e-12:
            raise ValueError("type prior must sum to one")

    @staticmethod
    def play_values(guess_heads_probability: float) -> tuple[float, float]:
        q = guess_heads_probability
        if q < 0.0 or q > 1.0:
            raise ValueError("guess probability must be in [0, 1]")
        # Opponent payoff: +1 when guessed incorrectly and -1 when correct.
        return 1.0 - 2.0 * q, 2.0 * q - 1.0

    @property
    def blueprint_floor_value(self) -> float:
        return sum(
            probability * payoff
            for probability, payoff in zip(
                self.type_prior, self.alternative_payoffs, strict=True
            )
        )

    def opponent_best_response_value(self, guess_heads_probability: float) -> float:
        play = self.play_values(guess_heads_probability)
        return sum(
            probability * max(alternative, play_value)
            for probability, alternative, play_value in zip(
                self.type_prior,
                self.alternative_payoffs,
                play,
                strict=True,
            )
        )

    def _certificate(self, method: str, q: float) -> ResolveCertificate:
        play = self.play_values(q)
        margins = tuple(
            alternative - play_value
            for alternative, play_value in zip(
                self.alternative_payoffs, play, strict=True
            )
        )
        best_response = self.opponent_best_response_value(q)
        delta = best_response - self.blueprint_floor_value
        return ResolveCertificate(
            method=method,
            guess_heads_probability=q,
            play_values=play,
            alternative_payoffs=self.alternative_payoffs,
            safety_margins=(margins[0], margins[1]),
            opponent_best_response_value=best_response,
            blueprint_floor_value=self.blueprint_floor_value,
            exploitability_delta=delta,
            safe=all(margin >= -1e-12 for margin in margins),
        )

    def plain_resolve(self) -> ResolveCertificate:
        """Solve only the isolated Play subgame, ignoring outside alternatives."""

        heads_prior, tails_prior = self.type_prior
        slope = 2.0 * (tails_prior - heads_prior)
        if slope > 0.0:
            q = 0.0
        elif slope < 0.0:
            q = 1.0
        else:
            q = 0.5  # deterministic tie-break for the underdetermined subgame
        return self._certificate("plain_isolated_subgame", q)

    def safe_resolve(self) -> ResolveCertificate:
        """Enforce per-type alternative-payoff bounds and return a certificate."""

        heads_alt, tails_alt = self.alternative_payoffs
        lower = max(0.0, (1.0 - heads_alt) / 2.0)
        upper = min(1.0, (tails_alt + 1.0) / 2.0)
        if lower > upper + 1e-12:
            raise ValueError(
                "alternative payoffs make the toy safety constraints infeasible"
            )

        heads_prior, tails_prior = self.type_prior
        slope = 2.0 * (tails_prior - heads_prior)
        if slope > 0.0:
            q = lower
        elif slope < 0.0:
            q = upper
        else:
            q = (lower + upper) / 2.0
        return self._certificate("safe_alternative_payoff_constraints", q)
