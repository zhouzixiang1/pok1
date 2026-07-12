from __future__ import annotations

import json

import pytest

from ..common_runtime.evaluation import exploitability
from ..common_runtime.kuhn import uniform_strategy
from ..decisionholdem_like.linear_cfr import LinearCFR


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
