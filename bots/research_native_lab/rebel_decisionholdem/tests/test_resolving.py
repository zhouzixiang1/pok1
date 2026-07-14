from __future__ import annotations

from dataclasses import replace

import pytest

from ..decisionholdem_like.resolving import CoinTossResolveGame


def test_plain_resolve_ignores_outside_game_and_is_exploitable() -> None:
    game = CoinTossResolveGame()
    certificate = game.plain_resolve()
    assert certificate.guess_heads_probability == pytest.approx(1.0)
    assert certificate.solve_prior == pytest.approx((0.6, 0.4))
    assert not certificate.safe
    assert certificate.exploitability_delta == pytest.approx(0.75)
    lower, upper = game.safety_interval()
    assert not lower <= certificate.guess_heads_probability <= upper


def test_safe_resolve_satisfies_each_type_alternative_payoff() -> None:
    game = CoinTossResolveGame()
    certificate = game.safe_resolve()
    assert certificate.guess_heads_probability == pytest.approx(0.25)
    assert certificate.solve_prior == pytest.approx((0.5, 0.5))
    assert certificate.safe
    assert certificate.safety_margins == pytest.approx((0.0, 0.0))
    assert certificate.exploitability_delta == pytest.approx(0.0)
    assert game.safety_interval() == pytest.approx((0.25, 0.25))
    game.verify_certificate(certificate)


def test_infeasible_alternative_payoffs_fail_closed() -> None:
    game = CoinTossResolveGame(alternative_payoffs=(-1.0, -1.0))
    with pytest.raises(ValueError, match="infeasible"):
        game.safe_resolve()


def test_safe_certificate_is_recomputed_and_cannot_hide_a_violation() -> None:
    game = CoinTossResolveGame()
    valid = game.safe_resolve()
    forged = replace(valid, exploitability_delta=0.0, safe=True, safety_margins=(1.0, 1.0))
    with pytest.raises(ValueError, match="does not match"):
        game.verify_certificate(forged)

    # Relabelling the known plain counterexample as safe must also fail even if
    # its other exact fields are retained.
    plain = game.plain_resolve()
    with pytest.raises(ValueError, match="does not match"):
        game.verify_certificate(
            replace(plain, method="safe_alternative_payoff_constraints", safe=True)
        )


@pytest.mark.parametrize("bad", (float("nan"), float("inf"), True, "0.5"))
def test_functional_resolve_inputs_reject_nonfinite_or_typed_values(bad) -> None:
    with pytest.raises(ValueError, match="finite non-negative"):
        CoinTossResolveGame(type_prior=(bad, 0.5))
    with pytest.raises(ValueError, match="guess probability"):
        CoinTossResolveGame.play_values(bad)


def test_functional_resolve_rejects_nonfinite_payoff() -> None:
    with pytest.raises(ValueError, match="finite numbers"):
        CoinTossResolveGame(alternative_payoffs=(0.5, float("nan")))
