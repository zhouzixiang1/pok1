"""Tests for generation_scheduler — strategy decision and branch parsing logic."""

import asyncio
from types import SimpleNamespace

import pytest


def test_prepare_generation_blocks_legacy_adapter_workflow(monkeypatch):
    import generation_scheduler
    import workflow_profiles

    events = []
    monkeypatch.setattr(
        workflow_profiles,
        "get_workflow_profile",
        lambda: SimpleNamespace(
            profile_id="national_primary",
            national_execution_mode="adapter",
        ),
    )
    monkeypatch.setattr(
        generation_scheduler,
        "log_system_event",
        lambda *args: events.append(args),
    )

    result = asyncio.run(generation_scheduler.prepare_generation(None))

    assert result is None
    assert events[0][0] == "pipeline.prepare_blocked_workflow_contract"
    assert events[0][3]["required_profile_id"] == "national_native"


class TestDecideStrategy:
    def test_default_master_no_stagnation(self):
        from generation_scheduler import _decide_strategy
        strategy, source_v, parents = _decide_strategy(None, 30, {})
        assert strategy == "master"
        assert source_v == 30
        assert parents == ()

    def test_default_master_low_confidence(self):
        from generation_scheduler import _decide_strategy
        combined = {"is_stagnant": True, "confidence": "low"}
        strategy, source_v, parents = _decide_strategy(combined, 30, {})
        assert strategy == "master"
        assert source_v == 30

    def test_stagnant_high_confidence_triggers_crossover(self, monkeypatch):
        from generation_scheduler import _decide_strategy
        combined = {"is_stagnant": True, "confidence": "high"}
        monkeypatch.setattr(
            "generation_scheduler._pick_crossover_parents",
            lambda ratings, cv, **kw: (30, 20),
        )
        strategy, source_v, parents = _decide_strategy(combined, 40, {})
        assert strategy == "crossover"
        assert parents == (30, 20)

    def test_stagnant_high_confidence_no_parents_falls_back(self, monkeypatch):
        from generation_scheduler import _decide_strategy
        combined = {"is_stagnant": True, "confidence": "high"}
        monkeypatch.setattr(
            "generation_scheduler._pick_crossover_parents",
            lambda ratings, cv, **kw: None,
        )
        strategy, source_v, parents = _decide_strategy(combined, 40, {})
        assert strategy == "master"
        assert source_v == 40

    def test_branch_recommendation_without_frozen_view_fails_closed(self, monkeypatch):
        from generation_scheduler import _decide_strategy
        monkeypatch.setattr("generation_scheduler._active_source_versions", lambda *_args: {20})
        monkeypatch.setattr("generation_scheduler._detect_source_loop", lambda n=3: None)
        monkeypatch.setattr("generation_scheduler._detect_source_oscillation", lambda *a, **kw: None)
        combined = {"recommendation": "branch", "branch_from": "20"}
        strategy, source_v, parents = _decide_strategy(combined, 30, {})
        assert strategy == "master"
        assert source_v == 30

    def test_branch_recommendation_rejects_inactive_source(self, monkeypatch):
        import generation_scheduler

        events = []
        monkeypatch.setattr(generation_scheduler, "_active_source_versions", lambda *_args: {30})
        monkeypatch.setattr(generation_scheduler, "_detect_source_loop", lambda n=3: None)
        monkeypatch.setattr(generation_scheduler, "_detect_source_oscillation", lambda *a, **kw: None)
        monkeypatch.setattr(generation_scheduler, "log_system_event", lambda *args: events.append(args))

        combined = {"recommendation": "branch", "branch_from": "20"}
        strategy, source_v, parents = generation_scheduler._decide_strategy(combined, 30, {})

        assert strategy == "master"
        assert source_v == 30
        assert parents == ()
        assert events
        assert events[0][0] == "pipeline.source_selection_rejected"
        assert events[0][3]["trigger"] == "branch_recommendation"
        assert events[0][3]["requested_source_v"] == 20

    def test_crossover_takes_priority_over_branch(self, monkeypatch):
        from generation_scheduler import _decide_strategy
        combined = {"is_stagnant": True, "confidence": "high", "recommendation": "branch", "branch_from": "15"}
        monkeypatch.setattr(
            "generation_scheduler._pick_crossover_parents",
            lambda ratings, cv, **kw: (30, 20),
        )
        strategy, source_v, parents = _decide_strategy(combined, 40, {})
        assert strategy == "crossover"

    def test_unqualified_diversity_hint_does_not_trigger_crossover(self, monkeypatch):
        from generation_scheduler import _decide_strategy
        combined = {"diversity_needed": True}
        monkeypatch.setattr(
            "generation_scheduler._pick_crossover_parents",
            lambda ratings, cv, **kw: (30, 20),
        )
        strategy, source_v, parents = _decide_strategy(combined, 40, {})
        assert strategy == "master"

    def test_diversity_needed_no_parents_falls_back(self, monkeypatch):
        from generation_scheduler import _decide_strategy
        combined = {"diversity_needed": True}
        monkeypatch.setattr(
            "generation_scheduler._pick_crossover_parents",
            lambda ratings, cv, **kw: None,
        )
        strategy, source_v, parents = _decide_strategy(combined, 40, {})
        assert strategy == "master"
        assert source_v == 40


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


def _frozen_evidence(gs):
    from glicko2 import Glicko2Player

    active = ("national_v1", "national_v4")
    ratings = {
        "national_v1": Glicko2Player(r=1510, rd=80, sigma=0.06),
        "national_v4": Glicko2Player(r=1550, rd=70, sigma=0.06),
    }
    rows = (
        {
            "name": "national_v1",
            "selection_score": 0.55,
            "leaderboard_score": 0.56,
            "secondary_net_chips_mean": 100.0,
            "h2h_avg_wr": 0.45,
            "h2h_games": 20,
            "h2h_opponents": 1,
            "h2h_opponents_total": 1,
            "h2h_coverage": 1.0,
            "strength_confidence": "medium",
        },
        {
            "name": "national_v4",
            "selection_score": 0.65,
            "leaderboard_score": 0.66,
            "secondary_net_chips_mean": 200.0,
            "h2h_avg_wr": 0.55,
            "h2h_games": 20,
            "h2h_opponents": 1,
            "h2h_opponents_total": 1,
            "h2h_coverage": 1.0,
            "strength_confidence": "medium",
        },
    )
    return gs.EvaluationEvidence(
        active_bots=active,
        ratings=ratings,
        bot_stats={name: {"games": 20, "win_rate": 0.5} for name in active},
        h2h={
            "national_v1 vs national_v4": {
                "games": 20,
                "a_wins": 9,
                "b_wins": 11,
                "draws": 0,
            }
        },
        selection_rows=rows,
        rating_history_tail=(),
        games=20,
        rd=70.0,
        readiness_reason="rd_threshold",
        cutoffs={"cycle_manifest_digest": "c" * 64},
    )


def test_frozen_selection_view_drives_leader_and_crossover_without_live_reads(monkeypatch):
    import candidate_store
    import crossover_compat
    import generation_scheduler as gs
    import map_elites
    import tool_helpers

    monkeypatch.setattr(gs, "_read_source_v_history", lambda: [])
    monkeypatch.setattr(candidate_store, "count_candidate_children", lambda _name: 0)
    monkeypatch.setattr(crossover_compat, "is_crossover_pair_blocked", lambda *_args: False)
    monkeypatch.setattr(map_elites, "read_behavior_archive", lambda: {})
    view = gs._build_selection_view(_frozen_evidence(gs))

    def no_live(*_args, **_kwargs):
        raise AssertionError("formal frozen selection path must not read live strength data")

    monkeypatch.setattr(tool_helpers, "load_selection_scores", no_live)
    monkeypatch.setattr(tool_helpers, "load_selection_order_keys", no_live)
    monkeypatch.setattr(tool_helpers, "load_h2h_avg_winrates_with_coverage", no_live)
    monkeypatch.setattr(tool_helpers, "_load_h2h_data", no_live)

    assert gs._get_unified_leader_v({}, view) == 4
    assert gs._strength_payload(4, selection_view=view)["selection_score"] == 0.65
    assert gs._pick_crossover_parents({}, 4, selection_view=view) == (4, 1)
    result = gs._decide_strategy(
        {"is_stagnant": True, "confidence": "high"},
        current_v=4,
        ratings={},
        selection_view=view,
    )
    assert result == ("crossover", 4, (4, 1))
    with pytest.raises(TypeError):
        view.metrics["national_v4"]["selection_score"] = 0.0


def test_successful_child_never_demotes_strong_parent_below_weak_unused_bot(monkeypatch):
    import candidate_store
    import generation_scheduler as gs

    monkeypatch.setattr(gs, "_read_source_v_history", lambda: [])
    monkeypatch.setattr(
        candidate_store,
        "count_candidate_children",
        lambda name: 1 if str(name) in {"national_v4", "v4", "4"} else 0,
    )
    evidence = _frozen_evidence(gs)
    weak_rows = []
    for row in evidence.selection_rows:
        row = dict(row)
        if row["name"] == "national_v1":
            row["selection_score"] = 0.40
        weak_rows.append(row)
    evidence = gs.EvaluationEvidence(
        **{**evidence.__dict__, "selection_rows": tuple(weak_rows)}
    )
    view = gs._build_selection_view(evidence)

    assert view.child_counts["national_v4"] == 1
    assert gs._pick_crossover_parents({}, 4, selection_view=view)[0] == 4


def test_frozen_selection_view_never_selects_inactive_rating(monkeypatch):
    import candidate_store
    import crossover_compat
    import generation_scheduler as gs
    import map_elites

    monkeypatch.setattr(gs, "_read_source_v_history", lambda: [1, 1, 1])
    monkeypatch.setattr(candidate_store, "count_candidate_children", lambda _name: 0)
    monkeypatch.setattr(crossover_compat, "is_crossover_pair_blocked", lambda *_args: False)
    monkeypatch.setattr(map_elites, "read_behavior_archive", lambda: {})
    evidence = _frozen_evidence(gs)
    view = gs._build_selection_view(evidence)
    inactive = SimpleNamespace(
        conservative_rating=lambda: 9999.0,
        r=9999.0,
        rd=1.0,
    )

    result = gs._decide_strategy(
        {"is_stagnant": False, "confidence": "low"},
        current_v=4,
        ratings={**evidence.ratings, "national_v99": inactive},
        selection_view=view,
    )

    assert result[1] in {1, 4}
    assert result[1] != 99
