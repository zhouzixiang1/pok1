import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from claude_agent_sdk import ClaudeAgentOptions

pytestmark = pytest.mark.usefixtures("synthetic_checkpoint_authority")


@pytest.fixture()
def isolated_cost_policy(tmp_path, monkeypatch):
    import orchestrator_cost_policy as policy

    ledger = tmp_path / "generation_cost_ledger.jsonl"
    pending = tmp_path / "generation_cost_pending.json"
    monkeypatch.setattr(policy, "COST_LEDGER_FILE", ledger)
    monkeypatch.setattr(policy, "COST_PENDING_FILE", pending)
    policy._accounting_errors.clear()
    policy.deactivate_generation_cost_scope()
    policy.configure_runtime_cost_policy(policy.GenerationCostPolicy())
    yield policy, ledger
    policy.deactivate_generation_cost_scope()
    policy._accounting_errors.clear()
    policy.configure_runtime_cost_policy(policy.GenerationCostPolicy())


def test_default_policy_is_monitor_only_and_receipted():
    import orchestrator_cost_policy as policy

    selected = policy.load_operator_generation_cost_policy({})
    receipt = selected.receipt()

    assert selected.enforcement_mode == "monitor_only"
    assert selected.hard_limit_usd is None
    assert selected.warning_usd == 7.0
    assert receipt["configuration_from_llm_input"] is False
    assert receipt["same_uid_llm_resistance"] is False
    assert receipt["candidate_sandbox_mutable"] is False
    assert receipt["workflow_guarded_paths"] is True
    assert receipt["configuration_source"] == "operator_process_environment"
    assert len(receipt["receipt_sha256"]) == 64
    assert len(receipt["implementation_sha256"]) == 64


@pytest.mark.parametrize("raw", ["", "0", "off", "none", "unlimited", "disabled"])
def test_explicit_disable_spellings_remain_monitor_only(raw):
    import orchestrator_cost_policy as policy

    selected = policy.load_operator_generation_cost_policy({policy.HARD_LIMIT_ENV: raw})
    assert selected.hard_limit_usd is None
    assert selected.enforcement_mode == "monitor_only"


def test_only_finite_positive_operator_value_enables_hard_limit():
    import orchestrator_cost_policy as policy

    selected = policy.load_operator_generation_cost_policy(
        {policy.HARD_LIMIT_ENV: "12.75"}
    )
    assert selected.hard_limit_usd == 12.75
    assert selected.enforcement_mode == "operator_hard_limit"

    for invalid in ("bad", "-1", "nan", "inf", "-inf"):
        with pytest.raises(policy.CostPolicyConfigurationError):
            policy.load_operator_generation_cost_policy(
                {policy.HARD_LIMIT_ENV: invalid}
            )


def test_ledger_survives_session_rebind_and_isolates_generations(isolated_cost_policy):
    policy, _ledger = isolated_cost_policy
    selected = policy.GenerationCostPolicy()

    first = policy.activate_generation_cost_scope("generation:151:stable", selected)
    policy.record_generation_cost(
        "Master",
        2.25,
        {"input_tokens": 100, "output_tokens": 20},
        source="test",
    )
    policy.deactivate_generation_cost_scope(first.generation_id)

    resumed = policy.activate_generation_cost_scope("generation:151:stable", selected)
    policy.record_generation_cost("Worker", 1.75, None, source="test")
    assert policy.generation_cost_status(resumed)["spent_usd"] == 4.0

    other = policy.activate_generation_cost_scope("generation:152:new", selected)
    assert policy.generation_cost_status(other)["spent_usd"] == 0.0


def test_default_warning_never_stops_generation(isolated_cost_policy, monkeypatch):
    policy, _ledger = isolated_cost_policy
    import orchestrator

    events = []
    monkeypatch.setattr(
        orchestrator,
        "log_system_event",
        lambda kind, severity, message, data=None: events.append(
            (kind, severity, message, data or {})
        ),
    )
    scope = policy.activate_generation_cost_scope(
        "generation:151:monitor",
        policy.GenerationCostPolicy(),
    )
    policy.record_generation_cost("Workers", 8.5, None, source="test")

    status = orchestrator._check_generation_cost_policy()

    assert status["spent_usd"] == 8.5
    assert status["enforcement_mode"] == "monitor_only"
    assert any(event[0] == "pipeline.generation_cost_warning" for event in events)
    assert not any("limit_tripped" in event[0] for event in events)
    # Persistent notice means a checkpoint/session hand-off does not spam it.
    orchestrator._check_generation_cost_policy()
    assert sum(event[0] == "pipeline.generation_cost_warning" for event in events) == 1
    assert scope.policy.hard_limit_usd is None


def test_explicit_operator_limit_fails_closed_with_bound_receipt(
    isolated_cost_policy,
    monkeypatch,
):
    policy, _ledger = isolated_cost_policy
    import orchestrator

    events = []
    monkeypatch.setattr(
        orchestrator,
        "log_system_event",
        lambda kind, severity, message, data=None: events.append(
            (kind, severity, message, data or {})
        ),
    )
    selected = policy.GenerationCostPolicy(hard_limit_usd=5.0)
    scope = policy.activate_generation_cost_scope("generation:151:capped", selected)
    policy.record_generation_cost("Workers", 5.01, None, source="test")

    with pytest.raises(policy.OperatorGenerationCostLimitExceeded):
        orchestrator._check_generation_cost_policy()

    trip = next(
        event for event in events
        if event[0] == "pipeline.operator_generation_cost_limit_tripped"
    )
    assert trip[3]["hard_limit_usd"] == 5.0
    assert trip[3]["generation_id"] == scope.generation_id
    assert trip[3]["operator_action_required"] is True
    assert len(trip[3]["policy_binding"]["binding_sha256"]) == 64


def test_enforced_policy_stops_when_durable_accounting_is_corrupt(
    isolated_cost_policy,
):
    policy, ledger = isolated_cost_policy
    ledger.write_text("not-json\n", encoding="utf-8")
    scope = policy.activate_generation_cost_scope(
        "generation:151:capped",
        policy.GenerationCostPolicy(hard_limit_usd=5.0),
    )

    status = policy.generation_cost_status(scope)
    assert status["accounting_ok"] is False
    with pytest.raises(policy.OperatorGenerationCostLimitExceeded):
        policy.assert_operator_cost_limit_available(scope)


def test_operator_limit_parks_stream_and_preserves_checkpoint(
    isolated_cost_policy,
    tmp_path,
    monkeypatch,
):
    from claude_agent_sdk import AssistantMessage, TextBlock
    import evolution_core
    import orchestrator

    policy, _ledger = isolated_cost_policy
    checkpoint = {
        "next_v": 151,
        "source_v": 142,
        "stage": "prepared",
        "generation_attempt": 0,
        "run_id": "151#0",
        "workflow_run_id": "generation:151:preserve-me",
    }
    monkeypatch.setattr(evolution_core, "read_pipeline_checkpoint", lambda: dict(checkpoint))
    monkeypatch.setattr(orchestrator, "_build_context", lambda **_kwargs: "")
    monkeypatch.setattr(orchestrator, "_load_orchestrator_session", lambda: None)
    cleared = []
    monkeypatch.setattr(
        orchestrator,
        "_clear_orchestrator_session",
        lambda reason=None: cleared.append(reason),
    )
    monkeypatch.setattr(
        orchestrator,
        "_detect_actionable_stage_handoff",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(orchestrator, "log_system_event", lambda *_args, **_kwargs: None)

    selected = policy.GenerationCostPolicy(hard_limit_usd=5.0)
    policy.activate_generation_cost_scope(checkpoint["workflow_run_id"], selected)
    policy.record_generation_cost("Workers", 6.0, None, source="test")

    async def _one_message():
        yield AssistantMessage(content=[TextBlock(text="continue")], model="sonnet")

    monkeypatch.setattr(orchestrator, "claude_query", lambda **_kwargs: _one_message())

    result = asyncio.run(
        orchestrator._run_one_cycle(
            ui=None,
            log_file=tmp_path / "orchestrator.log",
            gen_ctx=None,
            _cost_policy=selected,
        )
    )

    assert result == orchestrator.ORCH_OPERATOR_COST_LIMIT_COST
    assert "operator_generation_cost_limit" in cleared
    # The cost stop owns no checkpoint mutation API; the generation identity
    # remains available for an explicit operator restart.
    assert evolution_core.read_pipeline_checkpoint()["workflow_run_id"] == checkpoint["workflow_run_id"]


def test_prepare_operator_limit_propagates_to_outer_loop(monkeypatch):
    import generation_scheduler
    import orchestrator
    import orchestrator_cost_policy as policy

    async def stopped_prepare(*_args, **_kwargs):
        raise policy.OperatorGenerationCostLimitExceeded("operator stop")

    monkeypatch.setattr(generation_scheduler, "prepare_generation", stopped_prepare)
    with pytest.raises(policy.OperatorGenerationCostLimitExceeded):
        asyncio.run(orchestrator._prepare_or_fail(None, None))


def test_post_cleanup_operator_limit_propagates_to_outer_loop(monkeypatch):
    import generation_scheduler
    import orchestrator
    import orchestrator_cost_policy as policy

    async def stopped_cleanup(*_args, **_kwargs):
        raise policy.OperatorGenerationCostLimitExceeded("operator stop")

    monkeypatch.setattr(generation_scheduler, "post_generation_cleanup", stopped_cleanup)
    with pytest.raises(policy.OperatorGenerationCostLimitExceeded):
        asyncio.run(
            orchestrator._run_post_generation_cleanup_with_timeout(
                None,
                None,
                type("Ctx", (), {"next_v": 151, "source_v": 142})(),
            )
        )


def test_cost_policy_source_is_head_drift_critical():
    from evaluation_contract import ALWAYS_CRITICAL_EXACT
    from evolution_scope import CRITICAL_GENERATION_EXACT, is_critical_evolution_path

    path = "web/core/orchestrator_cost_policy.py"
    assert path in CRITICAL_GENERATION_EXACT
    assert path in ALWAYS_CRITICAL_EXACT
    assert is_critical_evolution_path(path)


def test_prepare_scope_is_stable_and_checkpoint_adopts_exact_identity(
    isolated_cost_policy,
    tmp_path,
    monkeypatch,
):
    policy, _ledger = isolated_cost_policy
    import checkpoint_schema
    import evolution_infra
    import generation_scheduler

    checkpoint_path = tmp_path / "pipeline_state.json"
    monkeypatch.setattr(evolution_infra, "PIPELINE_STATE_FILE", checkpoint_path)
    monkeypatch.setattr(generation_scheduler, "log_system_event", lambda *_a, **_k: None)
    monkeypatch.setattr(
        checkpoint_schema,
        "resolve_national_bot_spec",
        lambda *_args, **_kwargs: SimpleNamespace(
            eligible=True,
            version=143,
            issues=(),
            runtime_manifest={"epoch": "national_tcp_policy_v1"},
            epoch_receipt={"epoch": "national_tcp_policy_v1", "version": 143},
            publication_identity={"published": True, "version": 143},
            certificate_digest="a" * 64,
        ),
    )

    first = generation_scheduler._bind_prepare_generation_cost_scope(151)
    rebound = generation_scheduler._bind_prepare_generation_cost_scope(151)
    assert first == rebound == policy.generation_workflow_id(151)
    assert policy.current_generation_cost_scope().generation_id == first

    assert evolution_infra.write_pipeline_checkpoint(
        151,
        143,
        "selected",
        workflow_run_id=first,
    )
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["workflow_run_id"] == first
    assert policy.generation_identity(checkpoint) == first

    second = generation_scheduler._bind_prepare_generation_cost_scope(152)
    assert second == policy.generation_workflow_id(152)
    assert policy.current_generation_cost_scope().generation_id == second


def test_empty_output_retry_records_every_billed_attempt_once(
    isolated_cost_policy,
    monkeypatch,
):
    policy, ledger = isolated_cost_policy
    import llm_query

    scope = policy.activate_generation_cost_scope(
        policy.generation_workflow_id(151),
        policy.GenerationCostPolicy(),
    )
    attempts = iter(
        [
            (
                [],
                1.25,
                {
                    "input_tokens": 10,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 3,
                },
            ),
            (
                ["ok"],
                2.0,
                {
                    "input_tokens": 20,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 7,
                    "cache_creation_input_tokens": 4,
                },
            ),
        ]
    )

    async def fake_process_stream(_gen, _path, _ui, _role):
        return next(attempts)

    async def fake_sleep(_seconds):
        return None

    async def fake_generator():
        if False:
            yield None

    monkeypatch.setattr(llm_query, "_process_stream", fake_process_stream)
    monkeypatch.setattr(llm_query, "claude_query", lambda **_kwargs: fake_generator())
    monkeypatch.setattr(llm_query.asyncio, "sleep", fake_sleep)

    class UI:
        def __init__(self):
            self.total = 0.0

        def update_cost(self, _role, cost, _usage):
            self.total += float(cost or 0.0)

        def log_history(self, *_args, **_kwargs):
            pass

    ui = UI()
    texts, cost, usage = asyncio.run(
        llm_query._run_stream_with_signature_retry(
            "prompt",
            ClaudeAgentOptions(),
            str(Path("/tmp/cost-audit.log")),
            ui,
            "Master",
        )
    )

    assert texts == ["ok"]
    assert cost == 3.25
    assert usage == {
        "input_tokens": 30,
        "output_tokens": 5,
        "cache_read_input_tokens": 10,
        "cache_creation_input_tokens": 4,
    }
    assert ui.total == 3.25
    assert policy.generation_cost_status(scope)["spent_usd"] == 3.25
    usage_entries = [
        json.loads(line)
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("kind") == "usage"
    ]
    assert len(usage_entries) == 2
    assert len({entry["event_id"] for entry in usage_entries}) == 2
    assert usage_entries[0]["usage"]["cache_read_input_tokens"] == 3
    assert usage_entries[1]["usage"]["cache_creation_input_tokens"] == 4


def test_sdk_event_id_is_idempotent_across_replay(isolated_cost_policy):
    policy, _ledger = isolated_cost_policy
    scope = policy.activate_generation_cost_scope(
        policy.generation_workflow_id(151),
        policy.GenerationCostPolicy(),
    )

    first = policy.record_generation_cost(
        "Orchestrator", 1.5, None, source="test", event_id="sdk-result:stable"
    )
    replay = policy.record_generation_cost(
        "Orchestrator", 1.5, None, source="test", event_id="sdk-result:stable"
    )

    assert first["recorded"] is True
    assert replay["recorded"] is False
    assert replay["duplicate"] is True
    assert policy.generation_cost_status(scope)["spent_usd"] == 1.5


def test_zero_dollar_result_still_preserves_token_usage(isolated_cost_policy):
    policy, ledger = isolated_cost_policy
    scope = policy.activate_generation_cost_scope(
        policy.generation_workflow_id(151),
        policy.GenerationCostPolicy(),
    )
    status = policy.record_generation_cost(
        "Worker",
        0.0,
        {"input_tokens": 5, "cache_read_input_tokens": 11},
        source="test",
        event_id="sdk-result:zero-dollar",
    )

    assert status["recorded"] is True
    assert policy.generation_cost_status(scope)["spent_usd"] == 0.0
    entry = next(
        json.loads(line)
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("kind") == "usage"
    )
    assert entry["usage"]["input_tokens"] == 5
    assert entry["usage"]["cache_read_input_tokens"] == 11


def test_missing_sdk_cost_is_telemetry_in_monitor_and_fail_closed_in_hard_mode(
    isolated_cost_policy,
    monkeypatch,
):
    policy, _ledger = isolated_cost_policy
    import orchestrator

    events = []
    monkeypatch.setattr(
        orchestrator,
        "log_system_event",
        lambda kind, severity, message, data=None: events.append(
            (kind, severity, message, data or {})
        ),
    )

    class UI:
        def __init__(self):
            self.history = []
            self.bindings = []

        def log_history(self, message, status):
            self.history.append((message, status))

        def begin_generation_cost(self, generation_id, spent_usd, receipt):
            self.bindings.append((generation_id, spent_usd, receipt))

    ui = UI()
    generation_id = policy.generation_workflow_id(151)
    monitor = policy.activate_generation_cost_scope(
        generation_id,
        policy.GenerationCostPolicy(),
    )
    status = policy.record_generation_cost(
        "Reviewer",
        None,
        {"input_tokens": 9},
        source="test",
        event_id="sdk-result:missing-cost",
    )
    assert status["recorded"] is True
    assert status["accounting_ok"] is False
    assert any("unknown_cost" in item for item in status["accounting_errors"])
    checked = orchestrator._check_generation_cost_policy(ui)
    assert checked["accounting_ok"] is False
    warning = next(
        event
        for event in events
        if event[0] == "pipeline.generation_cost_accounting_warning"
    )
    assert warning[1] == "warn"
    assert any("unknown_cost" in item for item in warning[3]["accounting_errors"])
    assert warning[3]["directive"].startswith("Telemetry is incomplete")
    assert ui.history[-1][1] == "warn"
    assert ui.bindings[-1][0] == generation_id
    assert any(
        "unknown_cost" in item
        for item in ui.bindings[-1][2]["ledger_errors"]
    )
    orchestrator._check_generation_cost_policy(ui)
    assert sum(
        event[0] == "pipeline.generation_cost_accounting_warning"
        for event in events
    ) == 1

    hard = policy.activate_generation_cost_scope(
        generation_id,
        policy.GenerationCostPolicy(hard_limit_usd=5.0),
    )
    with pytest.raises(policy.OperatorGenerationCostLimitExceeded):
        orchestrator._check_generation_cost_policy(ui)
    assert sum(
        event[0] == "pipeline.generation_cost_accounting_warning"
        for event in events
    ) == 1


def test_failed_ledger_append_remains_fail_closed_after_restart_rebind(
    isolated_cost_policy,
    monkeypatch,
):
    policy, ledger = isolated_cost_policy
    real_locked_file = policy.locked_file
    scope = policy.activate_generation_cost_scope(
        policy.generation_workflow_id(151),
        policy.GenerationCostPolicy(hard_limit_usd=10.0),
    )

    def fail_ledger_only(path, *args, **kwargs):
        if Path(path) == ledger:
            raise OSError("injected ledger append failure")
        return real_locked_file(path, *args, **kwargs)

    monkeypatch.setattr(policy, "locked_file", fail_ledger_only)
    failed = policy.record_generation_cost(
        "Master", 4.0, None, source="test", event_id="sdk-result:pending"
    )
    assert failed["pending_only"] is True
    assert failed["accounting_ok"] is False
    assert failed["spent_usd"] == 4.0

    # Simulate a new process: volatile error memory is gone, durable pending
    # state and the generation id remain.
    policy.deactivate_generation_cost_scope()
    policy._accounting_errors.clear()
    rebound = policy.activate_generation_cost_scope(
        scope.generation_id,
        policy.GenerationCostPolicy(hard_limit_usd=10.0),
    )
    status = policy.generation_cost_status(rebound)
    assert status["spent_usd"] == 4.0
    assert status["accounting_ok"] is False
    assert status["pending_usage_count"] == 1
    with pytest.raises(policy.OperatorGenerationCostLimitExceeded):
        policy.assert_operator_cost_limit_available(rebound)

    monitor = policy.activate_generation_cost_scope(
        scope.generation_id,
        policy.GenerationCostPolicy(),
    )
    assert policy.assert_operator_cost_limit_available(monitor)["accounting_ok"] is False


def test_hard_limit_trips_when_threshold_is_reached(isolated_cost_policy):
    policy, _ledger = isolated_cost_policy
    scope = policy.activate_generation_cost_scope(
        policy.generation_workflow_id(151),
        policy.GenerationCostPolicy(hard_limit_usd=5.0),
    )
    policy.record_generation_cost(
        "Critic", 5.0, None, source="test", event_id="sdk-result:at-limit"
    )

    assert policy.generation_cost_status(scope)["hard_limit_exceeded"] is True
    with pytest.raises(policy.OperatorGenerationCostLimitExceeded):
        policy.assert_operator_cost_limit_available(scope)


@pytest.mark.parametrize(
    "tool_name,tool_input",
    [
        (
            "Bash",
            {"command": "rm -f web/core/results/generation_cost_ledger.jsonl"},
        ),
        (
            "Edit",
            {"file_path": "web/core/orchestrator_cost_policy.py"},
        ),
        (
            "Write",
            {"file_path": "pokctl.sh"},
        ),
    ],
)
def test_main_llm_guard_denies_cost_authority_mutation(tool_name, tool_input):
    import orchestrator_context

    hook = orchestrator_context._make_bot_dir_guard_hook()["PreToolUse"][0].hooks[0]
    output = asyncio.run(
        hook(
            {"tool_name": tool_name, "tool_input": tool_input},
            "cost-policy-audit",
            None,
        )
    )
    decision = output["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"


def test_web_ui_emits_zero_cost_policy_binding_and_clear(isolated_cost_policy):
    policy, _ledger = isolated_cost_policy
    from web_ui import EventBroadcaster, WebUI

    broadcaster = EventBroadcaster()
    authority = "a" * 64
    broadcaster.bind_authority(authority)
    client_id, queue = broadcaster.add_client(authority)
    try:
        ui = WebUI(broadcaster)
        scope = policy.activate_generation_cost_scope(
            policy.generation_workflow_id(151),
            policy.GenerationCostPolicy(),
        )
        ui.begin_generation_cost(scope.generation_id, 0.0, scope.receipt(spent_before_usd=0.0))
        bound = queue.get_nowait()
        assert bound["event"] == "generation_cost_policy"
        bound_data = json.loads(bound["data"])
        assert bound_data["generation_id"] == scope.generation_id
        assert bound_data["spent_usd"] == 0.0
        assert bound_data["policy"]["enforcement_mode"] == "monitor_only"

        ui.reset_gen_cost()
        cleared = queue.get_nowait()
        assert cleared["event"] == "generation_cost_policy"
        assert json.loads(cleared["data"])["policy"] is None
    finally:
        broadcaster.remove_client(client_id)
def test_generation_workflow_id_fences_reserved_bootstrap_retries():
    import orchestrator_cost_policy as policy

    assert policy.generation_workflow_id(143) == "generation:143:workflow-v1"
    assert policy.generation_workflow_id(143, attempt=2) == (
        "generation:143:workflow-v2"
    )
    with pytest.raises(ValueError, match="attempt must be positive"):
        policy.generation_workflow_id(143, attempt=0)
