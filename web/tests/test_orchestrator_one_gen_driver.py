from types import SimpleNamespace

import pytest


def _checkpoint(stage, *, workflow="generation:143:workflow-v23", revision=1):
    return {
        "workflow_run_id": workflow,
        "checkpoint_revision": revision,
        "stage": stage,
        "next_v": 143,
        "source_v": 142,
        "parent2_v": None,
    }


def _recovery(stage, **kwargs):
    checkpoint = _checkpoint(stage, **kwargs)
    return {
        "action": "resume",
        "checkpoint": checkpoint,
        "stage": stage,
        "next_v": checkpoint["next_v"],
        "source_v": checkpoint["source_v"],
    }


def _verified_abandon_proof(checkpoint, *, seed="a"):
    """Return the compact proof shape yielded by finalized schema-2 handoff."""

    return {
        "transaction_id": seed * 64,
        "abandon_receipt_digest": "b" * 64,
        "finalize_receipt_digest": "c" * 64,
        "checkpoint_identity": {
            "digest": "d" * 64,
            "workflow_run_id": checkpoint["workflow_run_id"],
            "next_v": checkpoint["next_v"],
            "source_v": checkpoint["source_v"],
            "checkpoint_revision": checkpoint["checkpoint_revision"],
            "stage": checkpoint["stage"],
        },
        "workflow_fences": {"worker": {}, "strict_authority": {}},
    }


def _patch_cli_shell(monkeypatch, tmp_path, orchestrator):
    """Remove unrelated process services around CLI boundary tests."""
    import epoch_authority
    import evolution_infra
    import llm_query
    import logging_config

    monkeypatch.setattr(orchestrator, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(
        epoch_authority,
        "require_policy_epoch_initialized",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(logging_config, "configure_logging", lambda: None)
    monkeypatch.setattr(orchestrator, "inject_ui", lambda _ui: None)
    monkeypatch.setattr(orchestrator, "set_system_log_ui", lambda _ui: None)
    monkeypatch.setattr(llm_query, "set_shutdown_manager", lambda _manager: None)
    monkeypatch.setattr(
        orchestrator,
        "load_operator_generation_cost_policy",
        lambda: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "configure_runtime_cost_policy",
        lambda _policy: SimpleNamespace(receipt=lambda: {}),
    )
    monkeypatch.setattr(orchestrator, "deactivate_generation_cost_scope", lambda: None)
    monkeypatch.setattr(evolution_infra, "stop_daemon", lambda: None)


def test_ambiguous_nested_abandon_results_are_not_authority():
    import orchestrator

    proof = {
        "abandoned": True,
        "cleared_checkpoint": True,
        "workflow_run_id": "generation:143:workflow-v23",
        "abandon_transaction_id": "a" * 64,
        "abandon_receipt_digest": "b" * 64,
        "finalize_receipt_digest": "c" * 64,
        "abandon_checkpoint_identity": {},
    }

    assert orchestrator._completed_abandon_tool_result({
        "content": [proof, {**proof, "abandon_transaction_id": "d" * 64}],
    }) is None


def test_one_gen_exit_codes_distinguish_success_and_terminal_controls():
    import orchestrator

    assert orchestrator._one_generation_exit_code(0.0) == 0
    assert orchestrator._one_generation_exit_code(
        orchestrator.ORCH_GENERATION_ABANDONED_COST
    ) == 2
    assert orchestrator._one_generation_exit_code(
        orchestrator.ORCH_OPERATOR_ACTION_REQUIRED_COST
    ) == 3
    assert orchestrator._one_generation_exit_code(
        orchestrator.ORCH_RECOVERY_BLOCKED_COST
    ) == 4
    assert orchestrator._one_generation_exit_code(
        orchestrator.ORCH_ACCOUNTING_BLOCKED_COST
    ) == 6
    assert orchestrator._one_generation_exit_code(
        orchestrator.ORCH_CONSECUTIVE_ABANDON_LIMIT_COST
    ) == 7
    assert orchestrator._one_generation_exit_code(
        orchestrator.ORCH_LLM_AVAILABILITY_BLOCKED_COST
    ) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("one_gen", "action", "expected_exit"),
    [
        (True, "blocked", 4),
        (True, "operator_action_required", 3),
        (False, "blocked", 4),
        (False, "operator_action_required", 3),
    ],
)
async def test_cli_startup_recovery_preserves_resume_ack_and_pause_on_stop(
    monkeypatch,
    tmp_path,
    one_gen,
    action,
    expected_exit,
):
    import os
    import orchestrator

    _patch_cli_shell(monkeypatch, tmp_path, orchestrator)
    args = SimpleNamespace(
        dry_run=False,
        one_gen=one_gen,
        no_daemon=True,
        max_turns=None,
    )
    pause = {"active": True, "evidence_digest": "a" * 64}
    recovery = {
        "action": action,
        "reason": "test_startup_stop",
        "checkpoint": _checkpoint("official_bootstrap_required"),
        "diagnostics": {"issues": ["test_startup_stop"]},
    }
    events = []
    monkeypatch.setenv("POK_LLM_RESUME_ACK", "operator-one-shot")

    def startup(_ui):
        events.append("recovery")
        return recovery

    def consume():
        events.append("ack")
        pause.clear()
        os.environ.pop("POK_LLM_RESUME_ACK", None)

    monkeypatch.setattr(orchestrator, "_startup_recovery", startup)
    monkeypatch.setattr(orchestrator, "consume_operator_resume_ack_from_env", consume)
    monkeypatch.setattr(orchestrator, "_runtime_branch_guard_enabled", lambda: False)
    monkeypatch.setattr(
        orchestrator,
        "_run_one_generation_cli",
        lambda **_kwargs: pytest.fail("blocked startup reached one-gen driver"),
    )

    exit_code = await orchestrator.run_orchestrator_cli(args)

    assert exit_code == expected_exit
    assert events == ["recovery"]
    assert os.environ["POK_LLM_RESUME_ACK"] == "operator-one-shot"
    assert pause == {"active": True, "evidence_digest": "a" * 64}


@pytest.mark.asyncio
async def test_one_gen_cli_reuses_pre_ack_recovery_without_second_read(
    monkeypatch,
    tmp_path,
):
    import orchestrator

    _patch_cli_shell(monkeypatch, tmp_path, orchestrator)
    args = SimpleNamespace(
        dry_run=False,
        one_gen=True,
        no_daemon=True,
        max_turns=None,
    )
    recovery = _recovery("selected")
    events = []

    def startup(_ui):
        events.append("recovery")
        return recovery

    def consume():
        events.append("ack")

    async def run_one_gen(**kwargs):
        events.append("driver")
        assert kwargs["startup_recovery"] is recovery
        return 0.0

    monkeypatch.setattr(orchestrator, "_startup_recovery", startup)
    monkeypatch.setattr(orchestrator, "consume_operator_resume_ack_from_env", consume)
    monkeypatch.setattr(orchestrator, "_run_one_generation_cli", run_one_gen)
    monkeypatch.setattr(orchestrator, "generation_cost_status", lambda: {"active": False})

    exit_code = await orchestrator.run_orchestrator_cli(args)

    assert exit_code == 0
    assert events == ["recovery", "ack", "driver"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_outcome", "expected_exit"),
    [
        ("ORCH_GENERATION_ABANDONED_COST", 2),
        ("ORCH_OPERATOR_ACTION_REQUIRED_COST", 3),
        ("ORCH_RECOVERY_BLOCKED_COST", 4),
        ("ORCH_ACCOUNTING_BLOCKED_COST", 6),
        ("ORCH_CONSECUTIVE_ABANDON_LIMIT_COST", 7),
    ],
)
async def test_continuous_cli_maps_typed_loop_terminal_outcome(
    monkeypatch,
    tmp_path,
    terminal_outcome,
    expected_exit,
):
    import orchestrator

    _patch_cli_shell(monkeypatch, tmp_path, orchestrator)
    args = SimpleNamespace(
        dry_run=False,
        one_gen=False,
        no_daemon=True,
        max_turns=None,
    )
    recovery = _recovery("selected")
    loop_calls = []

    async def loop(**kwargs):
        loop_calls.append(kwargs)
        return getattr(orchestrator, terminal_outcome)

    monkeypatch.setattr(orchestrator, "_startup_recovery", lambda _ui: recovery)
    monkeypatch.setattr(orchestrator, "orchestrator_loop", loop)

    exit_code = await orchestrator.run_orchestrator_cli(args)

    assert exit_code == expected_exit
    assert len(loop_calls) == 1
    assert "startup_recovery" not in loop_calls[0]


@pytest.mark.asyncio
async def test_one_gen_consumes_stage_handoffs_until_publication_cleanup(
    monkeypatch,
    tmp_path,
):
    import orchestrator

    selected = _recovery("selected", revision=1)
    prepared = _recovery("prepared", revision=2)
    handoff = _recovery("archived", revision=0)
    handoff["post_publication_handoff"] = True
    recoveries = iter((None, selected, prepared, handoff, None))
    prepare_calls = []
    provider_calls = []
    cleanup_calls = []

    async def prepare(_shutdown, _ui):
        prepare_calls.append(True)
        return SimpleNamespace(next_v=143, source_v=142)

    async def route(recovery, _ui=None, *, outcome=None, **_kwargs):
        if recovery.get("post_publication_handoff") is not True:
            return False
        if outcome is not None:
            outcome.update({"result": {"success": True}})
        return True

    async def run_cycle(**kwargs):
        provider_calls.append(kwargs)
        return orchestrator.ORCH_ACTIONABLE_HANDOFF_COST

    async def cleanup(_shutdown, _ui, ctx, gen_count=None):
        cleanup_calls.append((ctx, gen_count))
        return True

    monkeypatch.setattr(
        orchestrator,
        "_checkpoint_recovery_context",
        lambda *_args, **_kwargs: next(recoveries),
    )
    monkeypatch.setattr("generation_scheduler.prepare_generation", prepare)
    monkeypatch.setattr(orchestrator, "_try_deterministic_checkpoint_route", route)
    monkeypatch.setattr(orchestrator, "_run_one_cycle", run_cycle)
    monkeypatch.setattr(
        orchestrator,
        "_run_post_generation_cleanup_with_timeout",
        cleanup,
    )
    monkeypatch.setattr(
        orchestrator,
        "generation_cost_status",
        lambda: {
            "active": True,
            "accounting_ok": True,
            "spent_usd": 7.25,
        },
    )

    cost = await orchestrator._run_one_generation_cli(
        log_file=tmp_path / "one-gen.log",
        max_turns=None,
        shutdown_mgr=None,
        cost_policy=None,
    )

    assert cost == 7.25
    assert prepare_calls == [True]
    assert len(provider_calls) == 2
    assert {
        call["gen_ctx"].next_v for call in provider_calls
    } == {143}
    assert len(cleanup_calls) == 1
    assert cleanup_calls[0][1] == 1


@pytest.mark.asyncio
async def test_one_gen_active_workflow_abandon_never_prepares_successor(
    monkeypatch,
    tmp_path,
):
    import orchestrator

    prepare_calls = []
    cleanup_calls = []
    deactivated = []

    async def prepare(_shutdown, _ui):
        prepare_calls.append(True)
        raise AssertionError("active one-gen workflow must not prepare")

    async def route(*_args, **_kwargs):
        return False

    async def run_cycle(**kwargs):
        context = kwargs["gen_ctx"]
        assert orchestrator._remember_verified_canonical_abandon(
            context,
            _verified_abandon_proof({
                "workflow_run_id": "generation:143:workflow-v23",
                "checkpoint_revision": 1,
                "stage": "direction_audited",
                "next_v": context.next_v,
                "source_v": context.source_v,
            }),
        )
        return orchestrator.ORCH_GENERATION_ABANDONED_COST

    monkeypatch.setattr(
        orchestrator,
        "_checkpoint_recovery_context",
        lambda *_args, **_kwargs: _recovery("direction_audited"),
    )
    monkeypatch.setattr("generation_scheduler.prepare_generation", prepare)
    monkeypatch.setattr(orchestrator, "_try_deterministic_checkpoint_route", route)
    monkeypatch.setattr(orchestrator, "_run_one_cycle", run_cycle)
    monkeypatch.setattr(
        orchestrator,
        "deactivate_generation_cost_scope",
        lambda: deactivated.append(True),
    )
    monkeypatch.setattr(
        orchestrator,
        "_run_post_generation_cleanup_with_timeout",
        lambda *_args, **_kwargs: cleanup_calls.append(True),
    )

    cost = await orchestrator._run_one_generation_cli(
        log_file=tmp_path / "one-gen-abandon.log",
        max_turns=None,
        shutdown_mgr=None,
        cost_policy=None,
    )

    assert cost == orchestrator.ORCH_GENERATION_ABANDONED_COST
    assert prepare_calls == []
    assert cleanup_calls == []
    assert deactivated == [True]


@pytest.mark.asyncio
async def test_one_gen_bare_abandon_sentinel_is_recovery_blocked(
    monkeypatch,
    tmp_path,
):
    """One-gen has no successor, but still must not trust a bare terminal cost."""

    import orchestrator

    async def run_cycle(**_kwargs):
        return orchestrator.ORCH_GENERATION_ABANDONED_COST

    monkeypatch.setattr(
        orchestrator,
        "_checkpoint_recovery_context",
        lambda *_args, **_kwargs: _recovery("direction_audited"),
    )
    monkeypatch.setattr(orchestrator, "_run_one_cycle", run_cycle)
    monkeypatch.setattr(
        "generation_scheduler.prepare_generation",
        lambda *_args, **_kwargs: pytest.fail("must not prepare"),
    )

    cost = await orchestrator._run_one_generation_cli(
        log_file=tmp_path / "one-gen-bare-abandon.log",
        max_turns=None,
        shutdown_mgr=None,
        cost_policy=None,
    )

    assert cost == orchestrator.ORCH_RECOVERY_BLOCKED_COST


@pytest.mark.asyncio
async def test_one_gen_postpublication_cleanup_failure_is_not_success(
    monkeypatch,
    tmp_path,
):
    import orchestrator

    handoff = _recovery("archived", revision=0)
    handoff["post_publication_handoff"] = True
    recoveries = iter((handoff, None))

    async def route(*_args, **_kwargs):
        return True

    async def cleanup(*_args, **_kwargs):
        return False

    monkeypatch.setattr(
        orchestrator,
        "_checkpoint_recovery_context",
        lambda *_args, **_kwargs: next(recoveries),
    )
    monkeypatch.setattr(orchestrator, "_try_deterministic_checkpoint_route", route)
    monkeypatch.setattr(
        orchestrator,
        "_run_post_generation_cleanup_with_timeout",
        cleanup,
    )

    cost = await orchestrator._run_one_generation_cli(
        log_file=tmp_path / "one-gen-cleanup-failed.log",
        max_turns=None,
        shutdown_mgr=None,
        cost_policy=None,
    )

    assert cost == orchestrator.ORCH_RECOVERY_BLOCKED_COST


@pytest.mark.asyncio
async def test_one_gen_workflow_identity_drift_fails_closed(
    monkeypatch,
    tmp_path,
):
    import orchestrator

    recoveries = iter((
        _recovery("direction_audited", workflow="generation:143:workflow-v23"),
        _recovery("master_planned", workflow="generation:143:workflow-v24"),
    ))

    async def route(*_args, **_kwargs):
        return False

    async def run_cycle(**_kwargs):
        return orchestrator.ORCH_ACTIONABLE_HANDOFF_COST

    monkeypatch.setattr(
        orchestrator,
        "_checkpoint_recovery_context",
        lambda *_args, **_kwargs: next(recoveries),
    )
    monkeypatch.setattr(orchestrator, "_try_deterministic_checkpoint_route", route)
    monkeypatch.setattr(orchestrator, "_run_one_cycle", run_cycle)

    cost = await orchestrator._run_one_generation_cli(
        log_file=tmp_path / "one-gen-drift.log",
        max_turns=None,
        shutdown_mgr=None,
        cost_policy=None,
    )

    assert cost == orchestrator.ORCH_RECOVERY_BLOCKED_COST


@pytest.mark.asyncio
async def test_one_gen_prepare_operator_limit_maps_to_typed_exit(
    monkeypatch,
    tmp_path,
):
    import generation_scheduler
    import orchestrator
    import orchestrator_cost_policy

    prepare_calls = []
    provider_calls = []
    cleared = []

    async def available(*_args, **_kwargs):
        return True

    async def prepare(*_args, **_kwargs):
        prepare_calls.append(True)
        raise orchestrator_cost_policy.OperatorGenerationCostLimitExceeded(
            "operator limit"
        )

    monkeypatch.setattr(
        orchestrator,
        "_checkpoint_recovery_context",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(orchestrator, "_honor_active_llm_pause", available)
    monkeypatch.setattr(generation_scheduler, "prepare_generation", prepare)
    monkeypatch.setattr(
        orchestrator,
        "_run_one_cycle",
        lambda **kwargs: provider_calls.append(kwargs),
    )
    monkeypatch.setattr(
        orchestrator,
        "_clear_orchestrator_session",
        lambda reason=None: cleared.append(reason),
    )

    cost = await orchestrator._run_one_generation_cli(
        log_file=tmp_path / "one-gen-operator-limit.log",
        max_turns=None,
        shutdown_mgr=None,
        cost_policy=None,
    )

    assert cost == orchestrator.ORCH_OPERATOR_COST_LIMIT_COST
    assert orchestrator._one_generation_exit_code(cost) == 5
    assert prepare_calls == [True]
    assert provider_calls == []
    assert cleared == ["one_gen_operator_cost_limit"]


@pytest.mark.asyncio
async def test_one_gen_unexpected_prepare_failure_maps_to_failure_exit(
    monkeypatch,
    tmp_path,
):
    import generation_scheduler
    import orchestrator

    provider_calls = []
    events = []

    async def available(*_args, **_kwargs):
        return True

    async def prepare(*_args, **_kwargs):
        raise RuntimeError("unexpected prepare failure")

    monkeypatch.setattr(
        orchestrator,
        "_checkpoint_recovery_context",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(orchestrator, "_honor_active_llm_pause", available)
    monkeypatch.setattr(generation_scheduler, "prepare_generation", prepare)
    monkeypatch.setattr(
        orchestrator,
        "_run_one_cycle",
        lambda **kwargs: provider_calls.append(kwargs),
    )
    monkeypatch.setattr(
        orchestrator,
        "log_system_event",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )

    cost = await orchestrator._run_one_generation_cli(
        log_file=tmp_path / "one-gen-unexpected-failure.log",
        max_turns=None,
        shutdown_mgr=None,
        cost_policy=None,
    )

    assert cost == -1.0
    assert orchestrator._one_generation_exit_code(cost) == 5
    assert provider_calls == []
    assert events[0][0][0] == "orchestrator.one_gen_control_failed"


@pytest.mark.asyncio
async def test_one_gen_malformed_existing_checkpoint_fails_closed_before_provider(
    monkeypatch,
    tmp_path,
):
    import evolution_core
    import evolution_infra
    import generation_scheduler
    import orchestrator

    checkpoint_path = tmp_path / "pipeline_state.json"
    checkpoint_path.write_text("{}", encoding="utf-8")
    prepare_calls = []
    provider_calls = []

    async def prepare(*_args, **_kwargs):
        prepare_calls.append(True)
        return SimpleNamespace(next_v=143, source_v=142)

    monkeypatch.setattr(evolution_core, "PIPELINE_STATE_FILE", checkpoint_path)
    # The slot-aware resolver in evolution_infra.pipeline_state_path() reads
    # evolution_infra.PIPELINE_STATE_FILE (not the evolution_core re-export),
    # so patch both module references to the same tmp file.
    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", checkpoint_path)
    monkeypatch.setattr(evolution_core, "read_pipeline_checkpoint", lambda: {})
    monkeypatch.setattr(generation_scheduler, "prepare_generation", prepare)
    monkeypatch.setattr(
        orchestrator,
        "_run_one_cycle",
        lambda **kwargs: provider_calls.append(kwargs),
    )

    observation = orchestrator._pipeline_checkpoint_observation()
    cost = await orchestrator._run_one_generation_cli(
        log_file=tmp_path / "one-gen-malformed-checkpoint.log",
        max_turns=None,
        shutdown_mgr=None,
        cost_policy=None,
    )

    assert observation["checkpoint"] is None
    assert observation["path_exists"] is True
    assert observation["error"] == (
        "checkpoint_projection_identity_invalid:"
        "next_v,source_v,checkpoint_revision,stage,workflow_run_id"
    )
    assert cost == orchestrator.ORCH_RECOVERY_BLOCKED_COST
    assert orchestrator._one_generation_exit_code(cost) == 4
    assert prepare_calls == []
    assert provider_calls == []


@pytest.mark.asyncio
async def test_one_gen_cli_accounting_status_failure_exits_six(
    monkeypatch,
    tmp_path,
):
    import epoch_authority
    import evolution_infra
    import llm_query
    import logging_config
    import orchestrator

    args = SimpleNamespace(
        dry_run=False,
        one_gen=True,
        no_daemon=True,
        max_turns=None,
    )
    one_gen_calls = []
    deactivated = []
    daemon_stops = []

    async def run_one_gen(**kwargs):
        one_gen_calls.append(kwargs)
        return 0.0

    def unavailable_status():
        raise RuntimeError("accounting status unavailable")

    monkeypatch.setattr(orchestrator, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(
        epoch_authority,
        "require_policy_epoch_initialized",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(logging_config, "configure_logging", lambda: None)
    monkeypatch.setattr(orchestrator, "inject_ui", lambda _ui: None)
    monkeypatch.setattr(orchestrator, "set_system_log_ui", lambda _ui: None)
    monkeypatch.setattr(llm_query, "set_shutdown_manager", lambda _manager: None)
    monkeypatch.setattr(
        orchestrator,
        "load_operator_generation_cost_policy",
        lambda: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "configure_runtime_cost_policy",
        lambda policy: policy,
    )
    monkeypatch.setattr(
        orchestrator,
        "consume_operator_resume_ack_from_env",
        lambda: None,
    )
    monkeypatch.setattr(orchestrator, "_run_one_generation_cli", run_one_gen)
    monkeypatch.setattr(orchestrator, "generation_cost_status", unavailable_status)
    monkeypatch.setattr(
        orchestrator,
        "deactivate_generation_cost_scope",
        lambda: deactivated.append(True),
    )
    monkeypatch.setattr(
        evolution_infra,
        "stop_daemon",
        lambda: daemon_stops.append(True),
    )

    exit_code = await orchestrator.run_orchestrator_cli(args)

    assert exit_code == 6
    assert len(one_gen_calls) == 1
    assert deactivated == [True]
    assert daemon_stops == [True]


@pytest.mark.asyncio
async def test_one_gen_deterministic_abandon_requires_exact_terminal_proof(
    monkeypatch,
    tmp_path,
):
    import orchestrator
    import tool_bot_management

    recovery = _recovery("quality_failed")
    terminal = {
        "abandoned": True,
        "cleared_checkpoint": True,
        "workflow_run_id": "generation:143:workflow-v23",
        "abandon_transaction_id": "a" * 64,
        "abandon_receipt_digest": "b" * 64,
        "finalize_receipt_digest": "c" * 64,
        "abandon_checkpoint_identity": {},
    }
    recoveries = iter((recovery, None))
    validated = []
    deactivated = []

    async def route(current, _ui=None, *, outcome=None, **_kwargs):
        outcome.update({"terminal_abandon_result": terminal})
        return True

    def validate(checkpoint, result):
        validated.append((checkpoint, result))
        return _verified_abandon_proof(checkpoint)

    monkeypatch.setattr(
        orchestrator,
        "_checkpoint_recovery_context",
        lambda *_args, **_kwargs: next(recoveries),
    )
    monkeypatch.setattr(orchestrator, "_try_deterministic_checkpoint_route", route)
    monkeypatch.setattr(
        tool_bot_management,
        "validate_completed_abandon_handoff",
        validate,
    )
    monkeypatch.setattr(
        orchestrator,
        "deactivate_generation_cost_scope",
        lambda: deactivated.append(True),
    )

    cost = await orchestrator._run_one_generation_cli(
        log_file=tmp_path / "one-gen-deterministic-abandon.log",
        max_turns=None,
        shutdown_mgr=None,
        cost_policy=None,
    )

    assert cost == orchestrator.ORCH_GENERATION_ABANDONED_COST
    assert validated == [(recovery["checkpoint"], terminal)]
    assert deactivated == [True]


@pytest.mark.asyncio
async def test_one_gen_operator_boundary_parks_without_provider_or_prepare(
    monkeypatch,
    tmp_path,
):
    import orchestrator

    provider_calls = []

    monkeypatch.setattr(
        orchestrator,
        "_checkpoint_recovery_context",
        lambda *_args, **_kwargs: _recovery("official_bootstrap_required"),
    )
    monkeypatch.setattr(
        orchestrator,
        "_run_one_cycle",
        lambda **kwargs: provider_calls.append(kwargs),
    )

    cost = await orchestrator._run_one_generation_cli(
        log_file=tmp_path / "one-gen-operator.log",
        max_turns=None,
        shutdown_mgr=None,
        cost_policy=None,
    )

    assert cost == orchestrator.ORCH_OPERATOR_ACTION_REQUIRED_COST
    assert provider_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("active_checkpoint", (False, True))
async def test_one_gen_honors_durable_llm_pause_before_prepare_or_provider(
    monkeypatch,
    tmp_path,
    active_checkpoint,
):
    import orchestrator

    prepare_calls = []
    provider_calls = []
    recovery = _recovery("direction_audited") if active_checkpoint else None

    async def prepare(_shutdown, _ui):
        prepare_calls.append(True)
        return SimpleNamespace(next_v=143, source_v=142)

    async def route(*_args, **_kwargs):
        return False

    async def run_cycle(**kwargs):
        provider_calls.append(kwargs)
        return 0.0

    async def paused(*_args, **_kwargs):
        return False

    monkeypatch.setattr(
        orchestrator,
        "_checkpoint_recovery_context",
        lambda *_args, **_kwargs: recovery,
    )
    monkeypatch.setattr("generation_scheduler.prepare_generation", prepare)
    monkeypatch.setattr(orchestrator, "_try_deterministic_checkpoint_route", route)
    monkeypatch.setattr(orchestrator, "_run_one_cycle", run_cycle)
    monkeypatch.setattr(orchestrator, "_honor_active_llm_pause", paused)

    cost = await orchestrator._run_one_generation_cli(
        log_file=tmp_path / "one-gen-paused.log",
        max_turns=None,
        shutdown_mgr=None,
        cost_policy=None,
    )

    assert cost == orchestrator.ORCH_LLM_AVAILABILITY_BLOCKED_COST
    assert prepare_calls == []
    assert provider_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("with_terminal_proof", (False, True))
async def test_shared_deterministic_recovery_requires_terminal_proof(
    monkeypatch,
    with_terminal_proof,
):
    import orchestrator
    import tool_bot_management

    recovery = _recovery("quality_failed")
    terminal = {
        "abandoned": True,
        "cleared_checkpoint": True,
        "workflow_run_id": "generation:143:workflow-v23",
        "abandon_transaction_id": "a" * 64,
        "abandon_receipt_digest": "b" * 64,
        "finalize_receipt_digest": "c" * 64,
        "abandon_checkpoint_identity": {},
    }
    validated = []

    async def route(_recovery, _ui=None, *, outcome=None, **_kwargs):
        if with_terminal_proof:
            outcome["terminal_abandon_result"] = terminal
        return True

    def validate(checkpoint, result):
        validated.append((checkpoint, result))
        return {"transaction_id": "a" * 64}

    monkeypatch.setattr(orchestrator, "_try_deterministic_checkpoint_route", route)
    monkeypatch.setattr(
        orchestrator,
        "_checkpoint_recovery_context",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        tool_bot_management,
        "validate_completed_abandon_handoff",
        validate,
    )

    advanced = await orchestrator._advance_deterministic_recovery(
        recovery,
        None,
        cost_policy=None,
        shutdown_mgr=None,
    )

    assert advanced["routed"] is True
    if with_terminal_proof:
        assert advanced["recovery"] is None
        assert validated == [(recovery["checkpoint"], terminal)]
    else:
        assert advanced["recovery"]["action"] == "blocked"
        assert validated == []


@pytest.mark.asyncio
async def test_executed_false_deterministic_route_still_reproves_terminal_result(
    monkeypatch,
):
    import orchestrator
    import tool_bot_management

    recovery = _recovery("crossover_running")
    terminal = {
        "abandoned": True,
        "cleared_checkpoint": True,
        "workflow_run_id": "generation:143:workflow-v23",
        "abandon_transaction_id": "a" * 64,
        "abandon_receipt_digest": "b" * 64,
        "finalize_receipt_digest": "c" * 64,
        "abandon_checkpoint_identity": {},
    }

    async def route(_recovery, _ui=None, *, outcome=None, **_kwargs):
        outcome.update({
            "result": {"error": "CROSSOVER_ARTIFACT_DRIFT"},
            "terminal_abandon_result": terminal,
        })
        return False

    monkeypatch.setattr(orchestrator, "_try_deterministic_checkpoint_route", route)
    monkeypatch.setattr(
        orchestrator,
        "_checkpoint_recovery_context",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        tool_bot_management,
        "validate_completed_abandon_handoff",
        lambda checkpoint, result: {
            "checkpoint": checkpoint,
            "result": result,
        },
    )

    advanced = await orchestrator._advance_deterministic_recovery(
        recovery,
        None,
        cost_policy=None,
        shutdown_mgr=None,
    )

    assert advanced["routed"] is True
    assert advanced["terminal_action"] == "generation_abandoned"
    assert advanced["recovery"] is None


# ---------------------------------------------------------------------------
# Deterministic checkpoint route offloads the blocking canonical handler off
# the ASGI event loop (ROOT dispatch site; same defect class as
# prepare_generation 86b7aa77 + 30626e87 and the consumer gate chain 72653707).
# ---------------------------------------------------------------------------


def _patch_route_gates(monkeypatch, orchestrator, next_tool):
    """Neutralize the deterministic-route entry gates so a synthetic recovery
    reaches the single ``await handler(args)`` dispatch site inside
    ``_try_deterministic_checkpoint_route`` without touching real checkpoint
    / cost-policy / session machinery."""

    monkeypatch.setattr(
        orchestrator,
        "_bind_generation_cost_runtime",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "_check_generation_cost_policy",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "_resolve_recovery_route",
        lambda checkpoint: {
            "next_tool": next_tool,
            "next_v": checkpoint.get("next_v"),
            "source_v": checkpoint.get("source_v"),
            "parent2_v": checkpoint.get("parent2_v"),
            "stage": checkpoint.get("stage"),
            "route": {},
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "_deterministic_route_requires_llm",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(orchestrator, "_load_orchestrator_session", lambda: None)
    monkeypatch.setattr(
        orchestrator,
        "_clear_orchestrator_session",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        orchestrator,
        "log_system_event",
        lambda *_args, **_kwargs: None,
    )


def test_deterministic_route_offloads_handler_off_event_loop(monkeypatch):
    """The blocking canonical handler dispatched by the deterministic route
    MUST run in a worker thread, NOT on the orchestrator's ASGI event loop.

    Regression for the ROOT event-loop blocker (the prior three fixes
    86b7aa77 + 30626e87 + 72653707 covered prepare/audit/pool/cert and the
    Slice-2b consumer closure, but NOT the deterministic route dispatch
    itself). ``_try_deterministic_checkpoint_route``'s single
    ``await handler(args)`` site dispatches every canonical stage handler
    (prepare_next_gen / run_direction_audit / run_master / run_quality_gates
    / run_review / run_critic / run_precommit_eval / commit_bot / run_crossover
    / run_archivist / abandon_generation) INLINE on the ASGI loop, on BOTH the
    primary lane and the draft lane (via _draft_prepare_task ->
    _advance_deterministic_recovery). py-spy confirmed this blocking HTTP
    (000) while ``prepare_next_gen`` ran ``get_active_bots()`` inline.

    The fix offloads ``handler(args)`` to an owned worker thread via
    ``run_async_off_event_loop``. This test records the executing thread of the
    handler and asserts it is NEVER the event-loop thread. It fails with an
    inline ``await handler(args)``.
    """
    import threading

    import orchestrator

    loop_thread = threading.current_thread()
    handler_threads = []

    async def recording_handler(args):
        # Record the thread the canonical handler actually ran on. The @tool
        # wrapper's git-guard + the handler body both execute on THIS thread.
        handler_threads.append(threading.current_thread())
        import json as _json

        return {
            "content": [
                {"type": "text", "text": _json.dumps({"ok": True})}
            ]
        }

    _patch_route_gates(monkeypatch, orchestrator, next_tool="prepare_next_gen")
    monkeypatch.setattr(
        orchestrator,
        "_deterministic_route_handler_and_args",
        lambda *_args, **_kwargs: (recording_handler, {"source_v": 142, "next_v": 143}),
    )

    recovery = _recovery("selected")

    async def driver():
        return await orchestrator._try_deterministic_checkpoint_route(recovery)

    import asyncio

    asyncio.run(driver())

    assert handler_threads, "canonical handler was never invoked"
    for thread in handler_threads:
        assert thread is not loop_thread, (
            "deterministic-route handler ran on the event-loop thread "
            f"(ident={thread.ident}); it must be offloaded to a worker thread "
            "so the ASGI loop can keep serving HTTP during prepare/match/commit"
        )


def test_deterministic_route_offload_runs_on_a_private_event_loop(monkeypatch):
    """The offloaded deterministic-route handler runs on a PRIVATE event loop
    inside the worker thread (``run_async_off_event_loop``), so genuinely-async
    handlers (e.g. run_precommit_eval's native TCP match engine using
    asyncio.start_server / asyncio.wait / loop.time) still function while the
    orchestrator's ASGI loop stays free.

    This guards the specific offload mechanism: a plain ``run_blocking_isolated``
    cannot be used (no running loop in the worker), so the helper must drive the
    coroutine with ``asyncio.run`` inside the worker. The handler records its
    running loop identity and asserts it differs from the caller's loop.
    """
    import asyncio as _asyncio
    import threading

    import orchestrator

    caller_loop = _asyncio.new_event_loop()
    seen = {}

    async def introspecting_handler(args):
        seen["thread"] = threading.current_thread()
        seen["running_loop"] = _asyncio.get_running_loop()
        import json as _json

        return {
            "content": [
                {"type": "text", "text": _json.dumps({"ok": True})}
            ]
        }

    _patch_route_gates(monkeypatch, orchestrator, next_tool="run_precommit_eval")
    monkeypatch.setattr(
        orchestrator,
        "_deterministic_route_handler_and_args",
        lambda *_args, **_kwargs: (introspecting_handler, {"version": 143, "source_v": 142}),
    )

    recovery = _recovery("critic_checked")

    async def driver():
        seen["caller_loop"] = _asyncio.get_running_loop()
        routed = await orchestrator._try_deterministic_checkpoint_route(recovery)
        return routed

    try:
        result = caller_loop.run_until_complete(driver())
    finally:
        caller_loop.close()

    # The route executed (reached the dispatch site) ...
    assert result is not False
    # ... the handler ran off the caller's (event-loop) thread ...
    assert seen.get("thread") is not threading.current_thread(), (
        "deterministic-route handler ran on the caller (event-loop) thread"
    )
    # ... and on a DISTINCT event loop (the worker's private loop), proving the
    # async native-match engine can still use its asyncio primitives.
    assert seen.get("running_loop") is not None, "handler had no running loop"
    assert seen.get("running_loop") is not seen.get("caller_loop"), (
        "deterministic-route handler ran on the caller's event loop; "
        "run_async_off_event_loop must drive it on a fresh private loop inside "
        "the worker thread"
    )
