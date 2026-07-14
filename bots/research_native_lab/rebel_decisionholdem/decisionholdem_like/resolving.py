"""Source-shaped Coin Toss safety falsifier, not a resolver reproduction.

DecisionHoldem's paper states that its diverse-opponent real-time resolver is
safe but defers the algorithmic details, and the public repository ships the
real-time engine only as ``AlascasiaHoldem.so``.  This module therefore does
*not* claim to reproduce that resolver.  It combines the paper's published
blueprint reach distribution (which makes isolated solving always guess Heads)
with the Section-2 per-type Sell payoffs.  This compact functional constraint
game can falsify unsafe replacement, but it is not the paper's full augmented
Resolve game and not DecisionHoldem's unpublished method.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isclose, isfinite


@dataclass(frozen=True, slots=True)
class ResolveCertificate:
    method: str
    guess_heads_probability: float
    solve_prior: tuple[float, float]
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
    """A source-shaped functional constraint game over Coin Toss.

    ``unsafe_subgame_prior=(3/5, 2/5)`` is the conditional H/T reach after the
    paper's blueprint plays Play with probabilities 3/4 and 1/2.  ``type_prior``
    remains the original fair coin used for full-game safety accounting.
    """

    alternative_payoffs: tuple[float, float] = (0.5, -0.5)
    type_prior: tuple[float, float] = (0.5, 0.5)
    unsafe_subgame_prior: tuple[float, float] = (3.0 / 5.0, 2.0 / 5.0)

    def __post_init__(self) -> None:
        if (
            len(self.alternative_payoffs) != 2
            or any(
                type(payoff) not in (int, float) or not isfinite(payoff)
                for payoff in self.alternative_payoffs
            )
        ):
            raise ValueError("alternative payoffs must be two finite numbers")
        for name, prior in (
            ("type prior", self.type_prior),
            ("unsafe subgame prior", self.unsafe_subgame_prior),
        ):
            if len(prior) != 2 or any(
                type(probability) not in (int, float)
                or not isfinite(probability)
                or probability < 0.0
                for probability in prior
            ):
                raise ValueError(f"{name} must contain two finite non-negative numbers")
            if abs(sum(prior) - 1.0) > 1e-12:
                raise ValueError(f"{name} must sum to one")

    @staticmethod
    def play_values(guess_heads_probability: float) -> tuple[float, float]:
        q = guess_heads_probability
        if type(q) not in (int, float) or not isfinite(q) or q < 0.0 or q > 1.0:
            raise ValueError("guess probability must be in [0, 1]")
        # P1 payoff: +1 when P2 guesses incorrectly and -1 when correct.
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

    def _certificate(
        self,
        method: str,
        q: float,
        solve_prior: tuple[float, float],
    ) -> ResolveCertificate:
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
            solve_prior=solve_prior,
            play_values=play,
            alternative_payoffs=self.alternative_payoffs,
            safety_margins=(margins[0], margins[1]),
            opponent_best_response_value=best_response,
            blueprint_floor_value=self.blueprint_floor_value,
            exploitability_delta=delta,
            safe=all(margin >= -1e-12 for margin in margins),
        )

    def safety_interval(self) -> tuple[float, float]:
        """Return the complete feasible interval for the per-type bounds."""

        heads_alt, tails_alt = self.alternative_payoffs
        lower = max(0.0, (1.0 - heads_alt) / 2.0)
        upper = min(1.0, (tails_alt + 1.0) / 2.0)
        if lower > upper + 1e-12:
            raise ValueError(
                "alternative payoffs make the toy safety constraints infeasible"
            )
        return lower, upper

    def verify_certificate(self, certificate: ResolveCertificate) -> None:
        """Recompute every numeric claim and reject a forged safety result."""

        expected = self._certificate(
            certificate.method,
            certificate.guess_heads_probability,
            certificate.solve_prior,
        )
        scalar_fields = (
            "opponent_best_response_value",
            "blueprint_floor_value",
            "exploitability_delta",
        )
        tuple_fields = (
            "play_values",
            "alternative_payoffs",
            "safety_margins",
        )
        if any(
            not isclose(
                getattr(certificate, field),
                getattr(expected, field),
                rel_tol=0.0,
                abs_tol=1e-12,
            )
            for field in scalar_fields
        ) or any(
            any(
                not isclose(actual, truth, rel_tol=0.0, abs_tol=1e-12)
                for actual, truth in zip(
                    getattr(certificate, field),
                    getattr(expected, field),
                    strict=True,
                )
            )
            for field in tuple_fields
        ) or certificate.solve_prior != expected.solve_prior or certificate.safe is not expected.safe:
            raise ValueError("resolve certificate does not match the functional game")
        if certificate.method == "safe_alternative_payoff_constraints":
            if certificate.solve_prior != self.type_prior:
                raise ValueError("safe resolve certificate uses the wrong solve prior")
            lower, upper = self.safety_interval()
            if not lower - 1e-12 <= certificate.guess_heads_probability <= upper + 1e-12:
                raise ValueError("safe resolve certificate lies outside safety interval")
        elif certificate.method == "plain_blueprint_reach_subgame":
            if certificate.solve_prior != self.unsafe_subgame_prior:
                raise ValueError("plain resolve certificate uses the wrong blueprint reach")
        else:
            raise ValueError("unknown functional resolve certificate method")

    def plain_resolve(self) -> ResolveCertificate:
        """Solve Play under blueprint reach, ignoring outside alternatives."""

        heads_prior, tails_prior = self.unsafe_subgame_prior
        slope = 2.0 * (tails_prior - heads_prior)
        if slope > 0.0:
            q = 0.0
        elif slope < 0.0:
            q = 1.0
        else:
            q = 0.5  # deterministic tie-break for the underdetermined subgame
        certificate = self._certificate(
            "plain_blueprint_reach_subgame",
            q,
            self.unsafe_subgame_prior,
        )
        self.verify_certificate(certificate)
        return certificate

    def safe_resolve(self) -> ResolveCertificate:
        """Enforce per-type alternative-payoff bounds and return a certificate."""

        lower, upper = self.safety_interval()

        heads_prior, tails_prior = self.type_prior
        slope = 2.0 * (tails_prior - heads_prior)
        if slope > 0.0:
            q = lower
        elif slope < 0.0:
            q = upper
        else:
            q = (lower + upper) / 2.0
        certificate = self._certificate(
            "safe_alternative_payoff_constraints",
            q,
            self.type_prior,
        )
        self.verify_certificate(certificate)
        return certificate
