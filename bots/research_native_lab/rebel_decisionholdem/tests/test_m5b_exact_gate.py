from __future__ import annotations

import pytest

from ..rebel_like.m5b_exact_gate import (
    FROZEN_FOUR_ITERATION_DIGESTS,
    ExactLinearCFRGate,
)


@pytest.mark.parametrize(
    ("game", "infosets"), (("kuhn", 12), ("leduc", 288))
)
def test_m5b_owned_exact_lcfr_matches_frozen_independent_oracle(
    game: str, infosets: int
) -> None:
    solver = ExactLinearCFRGate(game)
    keys_before = tuple(solver.game.infosets)
    solver.train(4)
    receipt = solver.assert_frozen_four_iteration_differential()
    assert tuple(solver.game.infosets) == keys_before
    assert len(solver.regrets) == infosets
    assert len(solver.strategy_sums) == infosets
    assert receipt["checkpoint_sha256"] == FROZEN_FOUR_ITERATION_DIGESTS[game]
    assert receipt["stable_infoset_identity"] is True


def test_exact_gate_rejects_namespace_swap_or_wrong_iteration() -> None:
    solver = ExactLinearCFRGate("kuhn")
    solver.train(3)
    with pytest.raises(ValueError, match="four iterations"):
        solver.assert_frozen_four_iteration_differential()
    solver.train(1)
    # Swapping regret and average-policy namespaces must invalidate the exact
    # checkpoint even though shapes happen to agree.
    solver.regrets, solver.strategy_sums = solver.strategy_sums, solver.regrets
    with pytest.raises(ValueError, match="differential failed"):
        solver.assert_frozen_four_iteration_differential()

