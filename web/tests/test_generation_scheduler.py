"""Tests for generation_scheduler — strategy decision and branch parsing logic."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from bot_namespace import bot_name


def test_re_selected_abandoned_version_gets_fresh_workflow_attempt(monkeypatch):
    """Fix A: every re-selected version advances its workflow-v{K} attempt.

    The scheduler allocates ``generation_workflow_id(next_v, attempt=
    abandoned_version_attempt_count(next_v) + 1)`` for ALL versions.  A version
    abandoned once is re-prepared under ``workflow-v2`` with a fresh
    Worker/strict journal, instead of reusing the dead ``workflow-v1`` instance
    (which surfaced as ``WorkflowConflict: workflow instance is not running``
    on crossover, and as the infinite ``frozen_rework_*`` state-guard loop when
    a terminal journal was replayed while the outer checkpoint was re-created at
    ``master_planned``).  This is the durable per-version retry that the
    ledger's ``workflow-vK`` naming already encodes (see
    ``test_failed_reserved_v143_attempt_is_audited_but_does_not_burn_label`` in
    test_epoch_authority.py).  A future regression that re-gates this bump on
    ``FIRST_STRICT_POLICY_VERSION`` must update this test with an explicit
    fail-closed reason.
    """
    import abandoned_version_ledger as ledger
    import evolution_infra
    from orchestrator_cost_policy import generation_workflow_id

    re_selected_v = 17
    receipts = [
        {
            "version": re_selected_v,
            "receipt_digest": "a" * 64,
            "workflow_run_id": f"generation:{re_selected_v}:workflow-v1",
        }
    ]
    monkeypatch.setattr(
        evolution_infra,
        "load_abandoned_version_receipts",
        lambda **_kwargs: list(receipts),
    )
    # This is the exact formula the scheduler now uses for every version.
    prior_attempt = ledger.abandoned_version_attempt_count(re_selected_v)
    fresh_id = generation_workflow_id(re_selected_v, attempt=prior_attempt + 1)

    assert prior_attempt == 1
    assert fresh_id == f"generation:{re_selected_v}:workflow-v2"

    # A never-abandoned version keeps workflow-v1 (history / first attempts
    # unchanged).
    fresh_v = 99
    assert ledger.abandoned_version_attempt_count(fresh_v) == 0
    assert (
        generation_workflow_id(fresh_v, attempt=ledger.abandoned_version_attempt_count(fresh_v) + 1)
        == f"generation:{fresh_v}:workflow-v1"
    )


def test_prepare_generation_blocks_legacy_adapter_workflow(monkeypatch):
    import epoch_authority
    import generation_scheduler
    import post_publication_handoff
    import workflow_profiles

    events = []
    monkeypatch.setattr(
        post_publication_handoff,
        "pending_handoff_route",
        lambda: {"status": "none"},
    )
    monkeypatch.setattr(
        epoch_authority,
        "strict_epoch_projection",
        lambda **_kwargs: {
            "initialized": True,
            "ignored_checkpoint": None,
            "active_generation": None,
        },
    )
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
        assert _parse_branch_from(bot_name(15)) == 15

    def test_retired_namespace_is_rejected(self):
        from generation_scheduler import _parse_branch_from
        assert _parse_branch_from("claude_v10") is None

    def test_invalid_returns_none(self):
        from generation_scheduler import _parse_branch_from
        assert _parse_branch_from("not_a_number") is None

    def test_empty_string(self):
        from generation_scheduler import _parse_branch_from
        assert _parse_branch_from("") is None

    def test_negative_number(self):
        from generation_scheduler import _parse_branch_from
        assert _parse_branch_from("-5") is None


def _frozen_evidence(gs):
    from glicko2 import Glicko2Player

    weak_v = 143
    strong_v = 146
    mid_v = 144
    weak_name = bot_name(weak_v)
    strong_name = bot_name(strong_v)
    mid_name = bot_name(mid_v)
    # Three active bots. Crossover is disabled in production until the pool
    # reaches _MIN_CROSSOVER_POOL_SIZE (currently 12); the selection tests
    # below monkeypatch the threshold down to 3 so the parent-picking logic
    # itself stays exercised (the fixtures assert frozen-view-driven
    # selection, not the pool-size policy — that has its own tests).
    active = (weak_name, mid_name, strong_name)
    ratings = {
        weak_name: Glicko2Player(r=1510, rd=80, sigma=0.06),
        mid_name: Glicko2Player(r=1525, rd=75, sigma=0.06),
        strong_name: Glicko2Player(r=1550, rd=70, sigma=0.06),
    }
    rows = (
        {
            "name": weak_name,
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
            "name": mid_name,
            "selection_score": 0.60,
            "leaderboard_score": 0.61,
            "secondary_net_chips_mean": 150.0,
            "h2h_avg_wr": 0.50,
            "h2h_games": 20,
            "h2h_opponents": 1,
            "h2h_opponents_total": 1,
            "h2h_coverage": 1.0,
            "strength_confidence": "medium",
        },
        {
            "name": strong_name,
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
            f"{weak_name} vs {strong_name}": {
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
    import generation_scheduler as gs
    import generation_scheduler_source_selection as gss
    import tool_helpers

    monkeypatch.setattr(gs, "_read_source_v_history", lambda: [])
    # The production pool-size gate (12 bots) is a separate policy; these
    # fixtures exercise the frozen-view parent-picking logic with the
    # threshold lowered to match the 3-bot fixture pool.
    monkeypatch.setattr(gss, "_MIN_CROSSOVER_POOL_SIZE", 3)
    view = gs._build_selection_view(_frozen_evidence(gs))

    def no_live(*_args, **_kwargs):
        raise AssertionError("formal frozen selection path must not read live strength data")

    monkeypatch.setattr(tool_helpers, "load_selection_scores", no_live)
    monkeypatch.setattr(tool_helpers, "load_selection_order_keys", no_live)
    monkeypatch.setattr(tool_helpers, "load_h2h_avg_winrates_with_coverage", no_live)
    monkeypatch.setattr(tool_helpers, "_load_h2h_data", no_live)

    assert gs._get_unified_leader_v({}, view) == 146
    assert gs._strength_payload(146, selection_view=view)["selection_score"] == 0.65
    assert gs._pick_crossover_parents({}, 146, selection_view=view) == (146, 143)
    result = gs._decide_strategy(
        {"is_stagnant": True, "confidence": "high"},
        current_v=146,
        ratings={},
        selection_view=view,
    )
    assert result == ("crossover", 146, (146, 143))
    with pytest.raises(TypeError):
        view.metrics[bot_name(146)]["selection_score"] = 0.0


def test_selection_never_reads_mutable_candidate_child_counts(monkeypatch):
    import candidate_store
    import generation_scheduler as gs
    import generation_scheduler_source_selection as gss

    monkeypatch.setattr(gs, "_read_source_v_history", lambda: [])
    monkeypatch.setattr(gss, "_MIN_CROSSOVER_POOL_SIZE", 3)

    def no_candidate_ledger_reads(*_args, **_kwargs):
        raise AssertionError("mutable candidate ledger must not enter selection")

    monkeypatch.setattr(
        candidate_store,
        "count_candidate_children",
        no_candidate_ledger_reads,
    )
    evidence = _frozen_evidence(gs)
    weak_rows = []
    for row in evidence.selection_rows:
        row = dict(row)
        if row["name"] == bot_name(143):
            row["selection_score"] = 0.40
        weak_rows.append(row)
    evidence = gs.EvaluationEvidence(
        **{**evidence.__dict__, "selection_rows": tuple(weak_rows)}
    )
    view = gs._build_selection_view(evidence)

    assert not hasattr(view, "child_counts")
    assert gs._pick_crossover_parents({}, 146, selection_view=view)[0] == 146


def test_frozen_selection_view_never_selects_inactive_rating(monkeypatch):
    import generation_scheduler as gs

    monkeypatch.setattr(gs, "_read_source_v_history", lambda: [143, 143, 143])
    evidence = _frozen_evidence(gs)
    view = gs._build_selection_view(evidence)
    inactive = SimpleNamespace(
        conservative_rating=lambda: 9999.0,
        r=9999.0,
        rd=1.0,
    )

    result = gs._decide_strategy(
        {"is_stagnant": False, "confidence": "low"},
        current_v=146,
        ratings={**evidence.ratings, bot_name(199): inactive},
        selection_view=view,
    )

    assert result[1] in {143, 146}
    assert result[1] != 199


def test_prepare_generation_offloads_blocking_pool_resolution_off_event_loop(monkeypatch):
    """Regression guard for the ASGI event-loop starvation defect.

    ``prepare_generation`` is async and runs on the orchestrator's ASGI event
    loop. The pool-resolution calls it makes (``get_active_bots``,
    ``find_latest_rating_eligible_active_v``, ``resolve_national_bot_spec``)
    are SYNCHRONOUS and each drives git/ssh-keygen subprocesses with 30s
    timeouts. Running them inline blocks the loop for minutes and starves every
    HTTP request (8+ minute hang observed in production). They MUST be offloaded
    to a worker thread via ``run_blocking_isolated`` so the loop stays
    responsive. This test proves the blocking resolvers execute in a worker
    thread, never on the event-loop thread.
    """
    import epoch_authority
    import evolution_infra
    import generation_scheduler
    import post_publication_handoff
    import tool_runtime_guard
    import workflow_profiles

    loop_thread = threading.current_thread()
    # name -> list of threads that invoked the resolver (a resolver may be
    # called more than once, e.g. strict_epoch_projection; EVERY invocation
    # must run off the event-loop thread).
    resolver_threads = {}

    def _record_thread(name, default=...):
        def _impl(*args, **kwargs):
            resolver_threads.setdefault(name, []).append(threading.current_thread())
            if default is not ...:
                return default
            if name == "get_active_bots":
                # Two bots avoids the single-bot protocol-bootstrap branch so
                # prepare reaches the canonical pool-resolution / reap block.
                return ["national_cloud_v1", "national_cloud_v2"]
            if name == "find_latest_rating_eligible_active_v":
                return 2
            # resolve_national_bot_spec: return an eligible spec so prepare
            # proceeds past the rating-pool precheck.
            return SimpleNamespace(eligible=True, issues=())

        return _impl

    monkeypatch.setattr(evolution_infra, "get_active_bots", _record_thread("get_active_bots"))
    monkeypatch.setattr(
        evolution_infra,
        "find_latest_rating_eligible_active_v",
        _record_thread("find_latest_rating_eligible_active_v"),
    )
    # The resolver is imported lazily from bot_namespace inside prepare_generation.
    import bot_namespace

    monkeypatch.setattr(
        bot_namespace,
        "resolve_national_bot_spec",
        _record_thread("resolve_national_bot_spec"),
    )

    # Drive prepare_generation past every guard up to the pool-resolution block.
    monkeypatch.setattr(
        post_publication_handoff,
        "pending_handoff_route",
        _record_thread("pending_handoff_route", default={"status": "none"}),
    )

    _epoch_projection_value = {
        "initialized": True,
        "ignored_checkpoint": None,
        "active_generation": None,
        "published_high_water": 2,
        "allocation_floor": 2,
        "next_v": 3,
        "abandoned_receipt_floor": 0,
        "active_bots": ["national_cloud_v1", "national_cloud_v2"],
    }
    monkeypatch.setattr(
        epoch_authority,
        "strict_epoch_projection",
        _record_thread("strict_epoch_projection", default=_epoch_projection_value),
    )
    monkeypatch.setattr(
        workflow_profiles,
        "get_workflow_profile",
        lambda: SimpleNamespace(
            profile_id="national_native",
            national_execution_mode="native_tcp",
            eval_wait_rd_threshold=90.0,
            eval_wait_rd_min_games=30,
            eval_wait_min_games=10,
        ),
    )
    monkeypatch.setattr(
        tool_runtime_guard,
        "ensure_runtime_git_guard",
        _record_thread("ensure_runtime_git_guard", default=(True, {})),
    )
    monkeypatch.setattr(
        evolution_infra,
        "ensure_publish_ready_for_new_generation",
        _record_thread("ensure_publish_ready_for_new_generation", default=(True, {})),
    )
    monkeypatch.setattr(
        evolution_infra,
        "abandoned_version_attempt_count",
        _record_thread("abandoned_version_attempt_count", default=0),
    )
    monkeypatch.setattr(evolution_infra, "MIN_GAMES_FOR_EVAL", 10)
    monkeypatch.setattr(evolution_infra, "MAX_ACTIVE_BOTS", 8)

    # Skip the real blocking daemon-eval wait. Returning False makes prepare
    # return None at the "insufficient games" guard, AFTER all three blocking
    # resolvers (get_active_bots, find_latest_rating_eligible_active_v,
    # resolve_national_bot_spec) have already been exercised through the
    # run_blocking_isolated offload boundary.
    async def _fake_wait_for_daemon_eval(*_a, **_kw):
        return False

    monkeypatch.setattr(evolution_infra, "wait_for_daemon_eval", _fake_wait_for_daemon_eval)
    # Avoid real cost-policy / logging side effects in the prepare prologue.
    monkeypatch.setattr(
        generation_scheduler,
        "_bind_prepare_generation_cost_scope",
        lambda *_a, **_kw: "test-workflow-run-id",
    )
    monkeypatch.setattr(generation_scheduler, "log_system_event", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        generation_scheduler, "_bind_prepare_log_context", lambda *_a, **_kw: None
    )
    # log_git_worktree_snapshot is imported lazily from repo_state inside
    # prepare_generation and runs a git subprocess; record its thread too.
    import repo_state

    monkeypatch.setattr(
        repo_state,
        "log_git_worktree_snapshot",
        _record_thread("log_git_worktree_snapshot", default={}),
    )

    async def scenario():
        # Run on a real event loop so run_blocking_isolated uses a worker thread.
        await generation_scheduler.prepare_generation(None)

    asyncio.run(scenario())

    # Every blocking resolver reached by prepare MUST have executed off the
    # event-loop thread. These cover BOTH the original pool-resolution defect
    # AND the follow-up offloads (epoch projection, handoff discovery, runtime
    # git guard, publish sync, abandon-ledger scan, worktree snapshot).
    _expected_offloaded = (
        "get_active_bots",
        "find_latest_rating_eligible_active_v",
        "pending_handoff_route",
        "strict_epoch_projection",
        "ensure_runtime_git_guard",
        "ensure_publish_ready_for_new_generation",
        "abandoned_version_attempt_count",
        "log_git_worktree_snapshot",
    )
    for name in _expected_offloaded:
        assert name in resolver_threads, f"{name} was not reached"

    # CRITICAL: every blocking resolver ran off the event-loop thread. If any
    # invocation ran on the loop thread the event loop would be blocked for the
    # duration of the git/ssh-keygen subprocesses (the production hang). Check
    # EVERY recorded invocation (some resolvers are called more than once).
    for name, threads in resolver_threads.items():
        for thread in threads:
            assert thread is not loop_thread, (
                f"blocking resolver {name!r} ran on the event-loop thread "
                f"(ident={thread.ident}); it must be offloaded to a worker thread"
            )
