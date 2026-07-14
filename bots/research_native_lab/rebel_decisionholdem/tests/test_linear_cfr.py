from __future__ import annotations

import json

import pytest

from ..common_runtime.evaluation import exploitability
from ..common_runtime.kuhn import uniform_strategy
from ..decisionholdem_like.linear_cfr import LinearCFR
from ..decisionholdem_like.linear_cfr_reference import EquationLinearCFRReference
from ..rebel_like.pbs import KuhnPublicBeliefState


def test_linear_cfr_reduces_exact_kuhn_exploitability() -> None:
    initial = exploitability(uniform_strategy())
    solver = LinearCFR()
    solver.train(10_000)
    trained = exploitability(solver.average_strategy())
    assert trained < 0.02
    assert trained < initial * 0.2


def test_checkpoint_resume_matches_uninterrupted_training(tmp_path) -> None:
    uninterrupted = LinearCFR()
    uninterrupted.train(1_200)

    split = LinearCFR()
    split.train(400)
    checkpoint = tmp_path / "kuhn-lcfr.json"
    split.save_checkpoint(checkpoint)
    resumed = LinearCFR.load_checkpoint(checkpoint)
    resumed.train(800)

    assert resumed.checkpoint_payload() == uninterrupted.checkpoint_payload()
    assert resumed.checkpoint_digest() == uninterrupted.checkpoint_digest()
    assert resumed.average_strategy() == uninterrupted.average_strategy()


def test_malformed_checkpoint_fails_closed(tmp_path) -> None:
    solver = LinearCFR()
    payload = solver.checkpoint_payload()
    payload["regrets"].pop(next(iter(payload["regrets"])))
    checkpoint = tmp_path / "malformed.json"
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="infosets do not match"):
        LinearCFR.load_checkpoint(checkpoint)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (lambda payload: payload.update({"iterations_completed": True}), "iteration"),
        (
            lambda payload: payload["strategy_sums"].update(
                {next(iter(payload["strategy_sums"])): [-1.0, 2.0]}
            ),
            "row is invalid",
        ),
        (
            lambda payload: payload["regrets"].update(
                {next(iter(payload["regrets"])): [True, 0.0]}
            ),
            "not numeric",
        ),
    ),
)
def test_kuhn_checkpoint_rejects_typed_or_negative_state(
    tmp_path,
    mutation,
    message: str,
) -> None:
    payload = LinearCFR().checkpoint_payload()
    mutation(payload)
    checkpoint = tmp_path / "tampered.json"
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        LinearCFR.load_checkpoint(checkpoint)


def test_kuhn_checkpoint_rejects_noncanonical_decoded_key(tmp_path) -> None:
    payload = LinearCFR().checkpoint_payload()
    canonical = next(iter(payload["regrets"]))
    player, card, history = canonical.split("|", 2)
    payload["regrets"][f"0{player}|{card}|{history}"] = list(
        payload["regrets"][canonical]
    )
    checkpoint = tmp_path / "duplicate.json"
    checkpoint.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="not canonical"):
        LinearCFR.load_checkpoint(checkpoint)


@pytest.mark.parametrize("iterations", (1, 2, 7))
def test_lcfr_matches_independent_equation_reference(iterations: int) -> None:
    production = LinearCFR()
    reference = EquationLinearCFRReference()
    production.train(iterations)
    reference.train(iterations)

    assert production.iterations_completed == reference.iterations_completed
    for field in ("regrets", "strategy_sums"):
        actual = getattr(production, field)
        expected = getattr(reference, field)
        assert set(actual) == set(expected)
        for key in actual:
            assert actual[key] == pytest.approx(expected[key], abs=1e-14)


def test_reference_formula_uses_absolute_linear_iteration_weight() -> None:
    one = EquationLinearCFRReference()
    one.train(1)
    two = EquationLinearCFRReference()
    two.train(2)
    # A second iteration is not an unweighted duplicate.  This assertion is a
    # direct regression guard for the defining ``t`` multiplier in both state
    # families, independent of the production checkpoint/resume test.
    assert any(
        two.regrets[key] != pytest.approx(
            [2.0 * value for value in one.regrets[key]], abs=1e-14
        )
        for key in two.regrets
    )
    assert any(
        two.strategy_sums[key] != pytest.approx(
            [2.0 * value for value in one.strategy_sums[key]], abs=1e-14
        )
        for key in two.strategy_sums
    )


def test_kuhn_pbs_unnormalized_cfv_matches_lcfr_regret_equation() -> None:
    profile = uniform_strategy()
    pbs = KuhnPublicBeliefState.initial()
    cfv = pbs.cfr_counterfactual_action_values(profile)
    solver = LinearCFR()
    solver.train(1)
    for card in (0, 1, 2):
        key = (0, card, "")
        node = sum(
            profile[key][action] * cfv[card][action]
            for action in ("check", "bet")
        )
        assert solver.regrets[key] == pytest.approx(
            [cfv[card][action] - node for action in ("check", "bet")],
            abs=1e-12,
        )
