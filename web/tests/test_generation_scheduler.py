"""Tests for generation_scheduler — strategy decision and branch parsing logic."""

import pytest


class TestDecideStrategy:
    def test_default_master_no_stagnation(self):
        from generation_scheduler import _decide_strategy
        strategy, source_v, parents = _decide_strategy(None, None, 30, {})
        assert strategy == "master"
        assert source_v == 30
        assert parents == ()

    def test_default_master_low_confidence(self):
        from generation_scheduler import _decide_strategy
        stag = {"is_stagnant": True, "confidence": "low"}
        strategy, source_v, parents = _decide_strategy(stag, None, 30, {})
        assert strategy == "master"
        assert source_v == 30

    def test_stagnant_high_confidence_triggers_crossover(self, monkeypatch):
        from generation_scheduler import _decide_strategy
        stag = {"is_stagnant": True, "confidence": "high"}
        monkeypatch.setattr(
            "generation_scheduler._pick_crossover_parents",
            lambda ratings, cv: (30, 20),
        )
        strategy, source_v, parents = _decide_strategy(stag, None, 40, {})
        assert strategy == "crossover"
        assert parents == (30, 20)

    def test_stagnant_high_confidence_no_parents_falls_back(self, monkeypatch):
        from generation_scheduler import _decide_strategy
        stag = {"is_stagnant": True, "confidence": "high"}
        monkeypatch.setattr(
            "generation_scheduler._pick_crossover_parents",
            lambda ratings, cv: None,
        )
        strategy, source_v, parents = _decide_strategy(stag, None, 40, {})
        assert strategy == "master"
        assert source_v == 40

    def test_branch_recommendation(self):
        from generation_scheduler import _decide_strategy
        stag = {"recommendation": "branch", "branch_from": "20"}
        strategy, source_v, parents = _decide_strategy(stag, None, 30, {})
        assert strategy == "master"
        assert source_v == 20

    def test_crossover_takes_priority_over_branch(self, monkeypatch):
        from generation_scheduler import _decide_strategy
        stag = {"is_stagnant": True, "confidence": "high", "recommendation": "branch", "branch_from": "15"}
        monkeypatch.setattr(
            "generation_scheduler._pick_crossover_parents",
            lambda ratings, cv: (30, 20),
        )
        strategy, source_v, parents = _decide_strategy(stag, None, 40, {})
        assert strategy == "crossover"


class TestParseBranchFrom:
    def test_integer_string(self):
        from generation_scheduler import _parse_branch_from
        assert _parse_branch_from("25") == 25

    def test_v_prefix(self):
        from generation_scheduler import _parse_branch_from
        assert _parse_branch_from("v15") == 15

    def test_claude_v_prefix(self):
        from generation_scheduler import _parse_branch_from
        assert _parse_branch_from("claude_v10") == 10

    def test_invalid_returns_none(self):
        from generation_scheduler import _parse_branch_from
        assert _parse_branch_from("not_a_number") is None

    def test_empty_string(self):
        from generation_scheduler import _parse_branch_from
        assert _parse_branch_from("") is None

    def test_negative_number(self):
        from generation_scheduler import _parse_branch_from
        assert _parse_branch_from("-5") == -5
