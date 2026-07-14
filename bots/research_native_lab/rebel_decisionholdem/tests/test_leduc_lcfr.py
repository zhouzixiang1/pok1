from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

import pytest

from ..common_runtime.leduc import (
    actions_by_infoset,
    LeducState,
    all_infosets,
    apply_action,
    card_rank,
    initial_state,
    ordered_deals,
    terminal_utility,
    uniform_strategy,
    validate_strategy,
)
from ..common_runtime.leduc_evaluation import (
    best_response_policy,
    best_response_value,
    expected_utility,
    exploitability,
)
from ..decisionholdem_like.leduc_linear_cfr import LeducLinearCFR


CONFIG = json.loads(
    (Path(__file__).parents[1] / "configs" / "leduc_gate.json").read_text(
        encoding="utf-8"
    )
)


@lru_cache(maxsize=1)
def _trained_gate() -> tuple[LeducLinearCFR, float, float]:
    uniform = exploitability(uniform_strategy())
    solver = LeducLinearCFR()
    solver.train(int(CONFIG["iterations"]))
    trained = exploitability(solver.average_strategy())
    return solver, uniform, trained


def test_exact_leduc_tree_has_120_physical_deals_and_consistent_infosets() -> None:
    deals = ordered_deals()
    assert len(deals) == CONFIG["expected"]["physical_deals"]
    assert len(set(deals)) == CONFIG["expected"]["physical_deals"]
    assert all(len(set(deal)) == 3 for deal in deals)
    assert len(all_infosets()) == CONFIG["expected"]["infosets"]
    assert card_rank(0) == card_rank(1) == 0
    assert card_rank(4) == card_rank(5) == 2


def test_terminal_values_are_exact_and_zero_sum() -> None:
    # Player 0 raises, player 1 folds: contributions are 3 and 1, pot is 4.
    folded = apply_action(apply_action(initial_state(), "raise"), "fold")
    assert terminal_utility(folded, (4, 2, 0), 0) == 1.0
    assert terminal_utility(folded, (4, 2, 0), 1) == -1.0

    # Two checks on each street. P0 pairs the public J and beats P1's K high.
    state = initial_state()
    for action in ("check", "check", "check", "check"):
        state = apply_action(state, action)
    assert state.terminal
    assert terminal_utility(state, (0, 4, 1), 0) == 1.0
    assert terminal_utility(state, (0, 4, 1), 1) == -1.0


def test_exact_value_and_best_response_are_deterministic() -> None:
    profile = uniform_strategy()
    value0 = expected_utility(profile, 0)
    value1 = expected_utility(profile, 1)
    assert value0 == pytest.approx(
        CONFIG["expected"]["uniform_value_player0"], abs=1e-12
    )
    assert value0 == pytest.approx(-value1, abs=1e-12)
    assert best_response_value(profile, 0) == pytest.approx(
        CONFIG["expected"]["uniform_best_response_player0"], abs=1e-12
    )
    assert best_response_value(profile, 1) == pytest.approx(
        CONFIG["expected"]["uniform_best_response_player1"],
        abs=1e-12,
    )
    assert best_response_policy(profile, 0) == best_response_policy(profile, 0)


def test_best_response_handles_exact_zero_reach_opponent_branches() -> None:
    pure = {
        key: {
            action: 1.0 if index == 0 else 0.0
            for index, action in enumerate(actions)
        }
        for key, actions in actions_by_infoset().items()
    }
    value0 = expected_utility(pure, 0)
    value1 = expected_utility(pure, 1)
    response0 = best_response_value(pure, 0)
    response1 = best_response_value(pure, 1)
    assert all(math.isfinite(value) for value in (response0, response1))
    assert response0 >= value0 - 1e-12
    assert response1 >= value1 - 1e-12


def test_leduc_lcfr_passes_the_frozen_exploitability_drop_gate() -> None:
    solver, uniform, trained = _trained_gate()
    assert solver.iterations_completed == CONFIG["iterations"]
    assert trained < CONFIG["maximum_exploitability"]
    assert trained < uniform * CONFIG["maximum_fraction_of_uniform_exploitability"]
    assert uniform == pytest.approx(
        CONFIG["expected"]["uniform_exploitability"], abs=1e-12
    )
    assert trained == pytest.approx(
        CONFIG["expected"]["trained_exploitability"], abs=1e-12
    )
    assert solver.checkpoint_digest() == CONFIG["expected"]["checkpoint_sha256"]


def test_leduc_checkpoint_is_atomic_and_resume_is_bit_exact(tmp_path) -> None:
    uninterrupted = LeducLinearCFR()
    uninterrupted.train(20)

    split = LeducLinearCFR()
    split.train(7)
    checkpoint = tmp_path / "leduc.json"
    split.save_checkpoint(checkpoint)
    assert checkpoint.is_file()
    assert not tuple(tmp_path.glob("*.tmp"))
    resumed = LeducLinearCFR.load_checkpoint(checkpoint)
    resumed.train(13)

    assert resumed.checkpoint_payload() == uninterrupted.checkpoint_payload()
    assert resumed.checkpoint_digest() == uninterrupted.checkpoint_digest()
    assert resumed.average_strategy() == uninterrupted.average_strategy()


def test_leduc_checkpoint_rejects_missing_infosets(tmp_path) -> None:
    solver = LeducLinearCFR()
    payload = solver.checkpoint_payload()
    payload["regrets"].pop(next(iter(payload["regrets"])))
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="infosets do not match Leduc"):
        LeducLinearCFR.load_checkpoint(path)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (lambda payload: payload["fidelity"].update({"lcfr": "source-faithful"}), "fidelity"),
        (
            lambda payload: payload["strategy_sums"].update(
                {next(iter(payload["strategy_sums"])): [True, 0.0]}
            ),
            "not numeric",
        ),
    ),
)
def test_leduc_checkpoint_rejects_fidelity_or_typed_state_tampering(
    tmp_path,
    mutation,
    message: str,
) -> None:
    payload = LeducLinearCFR().checkpoint_payload()
    mutation(payload)
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        LeducLinearCFR.load_checkpoint(path)


def test_leduc_checkpoint_rejects_noncanonical_duplicate_infoset_key(tmp_path) -> None:
    payload = LeducLinearCFR().checkpoint_payload()
    canonical = next(iter(payload["regrets"]))
    player, private_rank, public_rank, history = canonical.split("|", 3)
    duplicate = f"{player}|0{private_rank}|{public_rank}|{history}"
    payload["regrets"][duplicate] = list(payload["regrets"][canonical])
    path = tmp_path / "duplicate.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="not canonical"):
        LeducLinearCFR.load_checkpoint(path)


@pytest.mark.parametrize("bad_value", (float("nan"), float("inf"), -1e-15, True))
def test_exact_leduc_strategy_validation_rejects_nonprobabilities(bad_value) -> None:
    profile = uniform_strategy()
    key = next(iter(profile))
    action = next(iter(profile[key]))
    profile[key][action] = bad_value
    with pytest.raises(ValueError, match="invalid probability"):
        validate_strategy(profile)


def test_exact_leduc_strategy_validation_rejects_extra_action() -> None:
    profile = uniform_strategy()
    next(iter(profile.values()))["unknown"] = 0.0
    with pytest.raises(ValueError, match="actions differ"):
        validate_strategy(profile)
