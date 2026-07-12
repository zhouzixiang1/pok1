from __future__ import annotations

import pytest

from ..decisionholdem_like.resolving import CoinTossResolveGame


def test_plain_resolve_ignores_outside_game_and_is_exploitable() -> None:
    certificate = CoinTossResolveGame().plain_resolve()
    assert certificate.guess_heads_probability == pytest.approx(0.5)
    assert not certificate.safe
    assert certificate.exploitability_delta == pytest.approx(0.25)


def test_safe_resolve_satisfies_each_type_alternative_payoff() -> None:
    certificate = CoinTossResolveGame().safe_resolve()
    assert certificate.guess_heads_probability == pytest.approx(0.25)
    assert certificate.safe
    assert certificate.safety_margins == pytest.approx((0.0, 0.0))
    assert certificate.exploitability_delta == pytest.approx(0.0)


def test_infeasible_alternative_payoffs_fail_closed() -> None:
    game = CoinTossResolveGame(alternative_payoffs=(-1.0, -1.0))
    with pytest.raises(ValueError, match="infeasible"):
        game.safe_resolve()
