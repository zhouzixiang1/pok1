import asyncio
from pathlib import Path

import pytest
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

import llm_query
import evolution_infra


def _rendered(role, text):
    if role.startswith("WORKER_COT_CHECK_"):
        import audit_agents

        task = {"target_files": ["policy.py"]}
        evidence = audit_agents.bind_fenced_worker_output(
            task=task,
            worker_id=role.removeprefix("WORKER_COT_CHECK_"),
            next_v=282,
            source_v=143,
            worker_effect_identity={
                "workflow_run_id": "generation:282:workflow-v1",
                "envelope_digest": "3" * 64,
                "effect_id": "effect-worker-cot-observability",
                "lease_epoch": 1,
            },
            attempt=1,
            dispatch_receipt_digest="4" * 64,
            output=text,
        )

        return llm_query.render_llm_prompt(
            role,
            producer=audit_agents._render_worker_cot_provider_prompt,
            renderer_inputs={
                "task": task,
                "worker_role": "logic",
                "worker_task": "edit policy",
                "worker_output_evidence": evidence.payload,
                "code_diff": "+change",
                "diff_metadata": "policy.py changed",
            },
        )
    if role.startswith("WORKER ") or role == "worker":
        import agent_workers

        workspace = (
            evolution_infra.PROJECT_ROOT
            / "web/core/results/workflow/artifacts/workspaces"
            / ("a" * 64)
        )
        task = {"target_files": ["policy.py"]}
        return llm_query.render_llm_prompt(
            role,
            producer=agent_workers._render_worker_provider_prompt,
            renderer_inputs={
                "task": task,
                "next_v": 282,
                "source_v": 143,
                "candidate_path": str(workspace),
                "allowed_files": ["policy.py"],
                "reviewer_feedback": text,
                "attempt_note": "",
                "retry_guidance": "",
                "role": "logic",
            },
        )
    if role.startswith("MASTER PROPOSAL "):
        import agent_master

        return llm_query.render_llm_prompt(
            role,
            producer=agent_master._render_master_proposal_provider_prompt,
            renderer_inputs={
                "planning_context": text,
                "direction": role.split()[2],
                "directive": "structural mechanism",
                "source_v": 143,
                "next_v": 149,
                "protocol_bootstrap_prepared_only": False,
                "singleton_no_strength": False,
                "source_symbol_index": "policy.py:decide",
                "repair_kind": "",
                "projection_hints": [],
                "invocation_id": "1" * 32,
            },
        )
    if role == "CYCLE ARCHIVIST":
        import cycle_archivist

        snapshot = cycle_archivist._cycle_archivist_prompt_projection(
            {
                "evaluation_epoch": "national_tcp_policy_v1",
                "bot_name": "national_v149",
                "git_tag": "national-bot-v149",
                "publication_identity": {
                    "publication_id": "1" * 64,
                    "commit_oid": "2" * 40,
                    "candidate_artifact_hash": "3" * 64,
                },
                "strength_evidence_identity": {"marker": text},
                "review_score": 9,
                "critic_score": 8,
                "precommit_passed": True,
                "post_publication_handoff": {
                    "identity_digest": "4" * 64,
                    "publication_id": "1" * 64,
                },
            },
            version=149,
            source_v=143,
        )
        return llm_query.render_llm_prompt(
            role,
            producer=cycle_archivist._render_cycle_archivist_provider_prompt,
            renderer_inputs={
                "snapshot": snapshot,
                "version": 149,
                "source_v": 143,
            },
        )
    if role == "COMBINED ANALYST":
        import combined_analyst

        return llm_query.render_llm_prompt(
            role,
            producer=combined_analyst._render_combined_provider_prompt,
            renderer_inputs={
                "source_v": 149,
                "frozen_bundle": {
                    "marker": text,
                    "rendered_view": {
                        "bot_name": text,
                        "opp_eval": "1",
                        "opp_total": "1",
                        "opp_coverage": "100%",
                        "rd_warning": "",
                        "top_bots": "none",
                        "generation_trend": "none",
                        "lineage": "none",
                        "daemon_history": "none",
                        "bot_stats": "none",
                        "h2h_results": "none",
                    },
                },
            },
        )
    raise AssertionError(role)


class _DummyUI:
    def __init__(self):
        self.costs = []

    def log_history(self, *_args, **_kwargs):
        pass

    def log_io(self, *_args, **_kwargs):
        pass

    def emit_tool_call(self, *_args, **_kwargs):
        pass

    def update_cost(self, role_name, cost_usd, usage):
        self.costs.append((role_name, cost_usd, usage))


class _DummyShutdown:
    is_shutting_down = True


class _UnknownSDKMessage:
    pass


async def _no_wait(*_args, **_kwargs):
    return None


def test_run_claude_query_emits_role_start_and_done(monkeypatch, tmp_path):
    events = []

    async def fake_stream(full_prompt, options, log_file_path, ui, role_name):
        assert "base prompt" in full_prompt
        assert "context body" in full_prompt
        return ["hello"], 0.125, {"input_tokens": 10, "output_tokens": 3}

    monkeypatch.setattr(llm_query, "_run_stream_with_signature_retry", fake_stream)
    monkeypatch.setattr(
        llm_query,
        "_emit_llm_event",
        lambda category, severity, message, **fields: events.append(
            (category, severity, message, fields)
        ),
    )

    log_file = tmp_path / "v243" / "logs" / "master_io.txt"
    log_file.parent.mkdir(parents=True)
    worker_root = (
        evolution_infra.PROJECT_ROOT
        / "web/core/results/workflow/artifacts/workspaces"
        / ("a" * 64)
    )
    worker_target = worker_root / "policy.py"

    output, cost, usage = asyncio.run(
        llm_query.run_claude_query(
            _rendered("WORKER 1 (observability)", "base prompt\ncontext body"),
            [],
            _DummyUI(),
            "WORKER 1 (observability)",
            str(log_file),
            tools=["Bash", "Read", "Edit"],
            allowed_write_dir={"files": [worker_target]},
            allowed_read_dirs=[worker_root],
        )
    )

    assert output == "hello"
    assert cost == 0.125
    assert usage["input_tokens"] == 10

    categories = [event[0] for event in events]
    assert "pipeline.llm_role_start" in categories
    assert "pipeline.llm_role_done" in categories

    start = next(fields for category, _sev, _msg, fields in events
                 if category == "pipeline.llm_role_start")
    done = next(fields for category, _sev, _msg, fields in events
                if category == "pipeline.llm_role_done")

    assert start["role"] == "WORKER 1 (observability)"
    assert start["context_file_count"] == 0
    assert start["tools"] == ["Bash", "Read", "Edit"]
    assert start["log_file"] == str(log_file)
    assert done["cost_usd"] == 0.125
    assert done["output_chars"] == len("hello")
    assert done["input_tokens"] == 10
    assert done["output_tokens"] == 3


def test_strict_query_parses_and_persists_terminal_result_not_stream_aggregate(
    monkeypatch, tmp_path
):
    import strict_authority_workflow

    captured = {}
    terminal = ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=10,
        is_error=False,
        num_turns=1,
        session_id="strict-terminal-session",
        total_cost_usd=0.01,
        usage={"input_tokens": 2, "output_tokens": 1},
        result='{"terminal":true}',
    )

    async def fake_stream(full_prompt, options, log_file_path, ui, role_name):
        del options, log_file_path, ui, role_name
        assert "SYSTEM-OWNED STRICT SCHEMA REPAIR" in full_prompt
        capture = llm_query._STRICT_PROVIDER_RESULTS.get()
        capture["results"].append(terminal)
        return ["intermediate tool-loop prose", '{"stream":true}'], 0.01, {}

    def fake_dispatch(call, **kwargs):
        captured["dispatch"] = kwargs
        call.update({
            "dispatched": True,
            "effect_id": "effect",
            "invocation_id": "invocation",
        })

    def fake_complete(call, *, raw_output, provider_results):
        captured["raw_output"] = raw_output
        captured["provider_results"] = provider_results
        call["provider_completed"] = True

    monkeypatch.setattr(llm_query, "_run_stream_with_signature_retry", fake_stream)
    monkeypatch.setattr(strict_authority_workflow, "dispatch_call", fake_dispatch)
    monkeypatch.setattr(
        strict_authority_workflow, "complete_provider_call", fake_complete
    )
    monkeypatch.setattr(llm_query, "_emit_llm_event", lambda *_args, **_kwargs: None)
    strict_call = {
        "slot": "proposal:mechanism",
        "schema_retry_required": True,
        "prior_schema_rejection": {
            "projection_errors": ["deterministic_schema_rejected"]
        },
    }

    output, _cost, _usage = asyncio.run(
        llm_query.run_claude_query(
            _rendered("MASTER PROPOSAL mechanism", "base prompt"),
            [],
            _DummyUI(),
            "MASTER PROPOSAL mechanism",
            str(tmp_path / "strict_terminal_io.txt"),
            tools=["Read"],
            allowed_read_dirs=[evolution_infra.PROJECT_ROOT / "bots/national_v143"],
            strict_authority=strict_call,
        )
    )

    assert output == '{"terminal":true}'
    assert captured["raw_output"] == output
    assert captured["provider_results"] == [terminal]
    assert captured["dispatch"]["lease_seconds"] == 960.0


def test_run_claude_query_injects_runtime_path_contract(monkeypatch, tmp_path):
    seen = {}

    async def fake_stream(full_prompt, options, log_file_path, ui, role_name):
        seen["prompt"] = full_prompt
        seen["cwd"] = options.cwd
        return ["ok"], 0.0, {}

    monkeypatch.setattr(llm_query, "_run_stream_with_signature_retry", fake_stream)
    monkeypatch.setattr(llm_query, "_emit_llm_event", lambda *_args, **_kwargs: None)

    log_file = tmp_path / "v282" / "logs" / "worker_io.txt"
    log_file.parent.mkdir(parents=True)
    worker_root = (
        evolution_infra.PROJECT_ROOT
        / "web/core/results/workflow/artifacts/workspaces"
        / ("a" * 64)
    )
    target = worker_root / "policy.py"

    output, _cost, _usage = asyncio.run(
        llm_query.run_claude_query(
            _rendered("worker", "base prompt"),
            [],
            _DummyUI(),
            "worker",
            str(log_file),
            tools=["Bash", "Read", "Edit"],
            allowed_write_dir={"files": [target]},
            allowed_read_dirs=[worker_root],
        )
    )

    assert output == "ok"
    assert seen["cwd"] == str(evolution_infra.PROJECT_ROOT)
    assert "# Runtime Path Contract" in seen["prompt"]
    assert f"`{evolution_infra.PROJECT_ROOT}`" in seen["prompt"]
    assert f"`{target}`" in seen["prompt"]
    assert "python -c" not in seen["prompt"]
    assert "python -B -c" not in seen["prompt"]
    assert "bash`/`sh -c` wrappers" in seen["prompt"]
    assert "base prompt" in seen["prompt"]


def test_run_claude_query_emits_role_failed(monkeypatch, tmp_path):
    events = []

    async def fake_stream(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(llm_query, "_run_stream_with_signature_retry", fake_stream)
    monkeypatch.setattr(
        llm_query,
        "_emit_llm_event",
        lambda category, severity, message, **fields: events.append(
            (category, severity, message, fields)
        ),
    )
    monkeypatch.setattr(llm_query.asyncio, "sleep", _no_wait)

    log_file = tmp_path / "reviewer_io.txt"

    with pytest.raises(RuntimeError):
        asyncio.run(
            llm_query.run_claude_query(
                _rendered("CYCLE ARCHIVIST", "prompt"),
                [],
                _DummyUI(),
                "CYCLE ARCHIVIST",
                str(log_file),
            )
        )

    failed = [event for event in events if event[0] == "pipeline.llm_role_failed"]
    assert len(failed) == 1
    _category, severity, message, fields = failed[0]
    assert severity == "error"
    assert "CYCLE ARCHIVIST" in message
    assert fields["role"] == "CYCLE ARCHIVIST"
    assert fields["exception_type"] == "RuntimeError"
    assert "boom" in fields["error"]


def test_run_claude_query_exit143_during_shutdown_is_cancelled(monkeypatch, tmp_path):
    events = []

    async def fake_stream(*_args, **_kwargs):
        raise Exception("Command failed with exit code 143 (exit code: 143)")

    monkeypatch.setattr(llm_query, "_run_stream_with_signature_retry", fake_stream)
    monkeypatch.setattr(
        llm_query,
        "_emit_llm_event",
        lambda category, severity, message, **fields: events.append(
            (category, severity, message, fields)
        ),
    )
    llm_query.set_shutdown_manager(_DummyShutdown())

    try:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(
                llm_query.run_claude_query(
                    _rendered("COMBINED ANALYST", "prompt"),
                    [],
                    _DummyUI(),
                    "COMBINED ANALYST",
                    str(tmp_path / "match_analyst_io.txt"),
                )
            )
    finally:
        llm_query.set_shutdown_manager(None)

    failed = [event for event in events if event[0] == "pipeline.llm_role_failed"]
    cancelled = [
        event for event in events
        if event[0] == "pipeline.llm_role_shutdown_cancelled"
    ]
    terminated = [
        event for event in events
        if event[0] == "pipeline.llm_role_process_terminated"
    ]
    assert failed == []
    assert len(cancelled) == 1
    assert terminated == []
    _category, severity, message, fields = cancelled[0]
    assert severity == "info"
    assert "stopped during shutdown" in message
    assert fields["role"] == "COMBINED ANALYST"
    assert "exit code 143" in fields["error"]
    assert fields["shutdown_requested"] is True


def test_run_claude_query_exit143_without_shutdown_is_process_terminated(monkeypatch, tmp_path):
    events = []

    async def fake_stream(*_args, **_kwargs):
        raise Exception("Command failed with exit code 143 (exit code: 143)")

    monkeypatch.setattr(llm_query, "_run_stream_with_signature_retry", fake_stream)
    monkeypatch.setattr(
        llm_query,
        "_emit_llm_event",
        lambda category, severity, message, **fields: events.append(
            (category, severity, message, fields)
        ),
    )
    llm_query.set_shutdown_manager(None)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            llm_query.run_claude_query(
                _rendered("COMBINED ANALYST", "prompt"),
                [],
                _DummyUI(),
                "COMBINED ANALYST",
                str(tmp_path / "match_analyst_io.txt"),
            )
        )

    shutdown_cancelled = [
        event for event in events
        if event[0] == "pipeline.llm_role_shutdown_cancelled"
    ]
    terminated = [
        event for event in events
        if event[0] == "pipeline.llm_role_process_terminated"
    ]
    assert shutdown_cancelled == []
    assert len(terminated) == 1
    _category, severity, message, fields = terminated[0]
    assert severity == "warn"
    assert "received SIGTERM" in message
    assert fields["role"] == "COMBINED ANALYST"
    assert fields["shutdown_requested"] is False


def test_run_claude_query_exit143_without_shutdown_manager_is_process_cancelled(monkeypatch, tmp_path):
    events = []

    async def fake_stream(*_args, **_kwargs):
        raise Exception("Command failed with exit code 143 (exit code: 143)")

    monkeypatch.setattr(llm_query, "_run_stream_with_signature_retry", fake_stream)
    monkeypatch.setattr(
        llm_query,
        "_emit_llm_event",
        lambda category, severity, message, **fields: events.append(
            (category, severity, message, fields)
        ),
    )
    llm_query.set_shutdown_manager(None)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            llm_query.run_claude_query(
                _rendered("COMBINED ANALYST", "prompt"),
                [],
                _DummyUI(),
                "COMBINED ANALYST",
                str(tmp_path / "combined_analysis.txt"),
            )
        )

    failed = [event for event in events if event[0] == "pipeline.llm_role_failed"]
    cancelled = [
        event for event in events
        if event[0] == "pipeline.llm_role_shutdown_cancelled"
    ]
    terminated = [
        event for event in events
        if event[0] == "pipeline.llm_role_process_terminated"
    ]
    assert failed == []
    assert cancelled == []
    assert len(terminated) == 1
    _category, severity, message, fields = terminated[0]
    assert severity == "warn"
    assert "received SIGTERM" in message
    assert fields["role"] == "COMBINED ANALYST"
    assert fields["shutdown_requested"] is False


def test_process_stream_emits_periodic_progress(monkeypatch, tmp_path):
    events = []

    async def fake_stream():
        yield AssistantMessage(content=[TextBlock(text="alpha")], model="sonnet")
        await asyncio.sleep(0.02)
        yield AssistantMessage(content=[TextBlock(text="beta")], model="sonnet")
        yield ResultMessage(
            subtype="success",
            duration_ms=10,
            duration_api_ms=10,
            is_error=False,
            num_turns=1,
            session_id="session",
            total_cost_usd=0.1,
            usage={"input_tokens": 4, "output_tokens": 2},
        )

    monkeypatch.setattr(llm_query, "_LLM_PROGRESS_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(
        llm_query,
        "_emit_llm_event",
        lambda category, severity, message, **fields: events.append(
            (category, severity, message, fields)
        ),
    )

    log_file = tmp_path / "v243" / "logs" / "master_io.txt"
    log_file.parent.mkdir(parents=True)

    texts, cost, usage = asyncio.run(
        llm_query._process_stream(fake_stream(), str(log_file), _DummyUI(), "master")
    )

    assert texts == ["alpha", "beta"]
    assert cost == 0.1
    assert usage["input_tokens"] == 4

    progress = [
        event for event in events if event[0] == "pipeline.llm_role_progress"
    ]
    assert progress
    _category, severity, _message, fields = progress[0]
    assert severity == "info"
    assert fields["role"] == "master"
    assert fields["messages_seen"] >= 2
    assert fields["text_chars"] == len("alphabeta")
    assert fields["progress_interval_sec"] == 0.01


def test_process_stream_logs_user_message_tool_results(monkeypatch, tmp_path):
    events = []

    async def fake_stream():
        yield AssistantMessage(
            content=[
                ToolUseBlock(
                    id="toolu_1",
                    name="Bash",
                    input={"command": "printf ok"},
                )
            ],
            model="sonnet",
        )
        await asyncio.sleep(0.02)
        yield UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="toolu_1",
                    content="ok\n",
                    is_error=False,
                )
            ]
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=10,
            duration_api_ms=10,
            is_error=False,
            num_turns=1,
            session_id="session",
            total_cost_usd=0.1,
            usage={"input_tokens": 4, "output_tokens": 2},
        )

    monkeypatch.setattr(llm_query, "_LLM_PROGRESS_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(
        llm_query,
        "_emit_llm_event",
        lambda category, severity, message, **fields: events.append(
            (category, severity, message, fields)
        ),
    )

    log_file = tmp_path / "v243" / "logs" / "worker_1_io.txt"
    log_file.parent.mkdir(parents=True)

    texts, cost, usage = asyncio.run(
        llm_query._process_stream(fake_stream(), str(log_file), _DummyUI(), "worker")
    )

    assert texts == []
    assert cost == 0.1
    assert usage["output_tokens"] == 2

    role_log = log_file.read_text(encoding="utf-8")
    assert "[TOOL_CALL] Bash" in role_log
    assert "[TOOL_RESULT source=ToolResultBlock is_error=False] ok" in role_log

    progress = [
        event for event in events if event[0] == "pipeline.llm_role_progress"
    ]
    assert progress
    _category, severity, _message, fields = progress[-1]
    assert severity == "info"
    assert fields["role"] == "worker"
    assert fields["tool_use_count"] == 1
    assert fields["tool_result_count"] == 1


def test_process_stream_emits_silence_watchdog(monkeypatch, tmp_path):
    events = []

    async def fake_stream():
        yield AssistantMessage(content=[TextBlock(text="alpha")], model="sonnet")
        await asyncio.sleep(0.06)
        yield ResultMessage(
            subtype="success",
            duration_ms=10,
            duration_api_ms=10,
            is_error=False,
            num_turns=1,
            session_id="session",
            total_cost_usd=0.1,
            usage={"input_tokens": 4, "output_tokens": 2},
        )

    monkeypatch.setattr(llm_query, "_LLM_PROGRESS_INTERVAL_SEC", 999)
    monkeypatch.setattr(llm_query, "_LLM_SILENCE_WARN_SEC", 0.02)
    monkeypatch.setattr(
        llm_query,
        "_emit_llm_event",
        lambda category, severity, message, **fields: events.append(
            (category, severity, message, fields)
        ),
    )

    log_file = tmp_path / "v243" / "logs" / "master_io.txt"
    log_file.parent.mkdir(parents=True)

    texts, cost, usage = asyncio.run(
        llm_query._process_stream(fake_stream(), str(log_file), _DummyUI(), "master")
    )

    assert texts == ["alpha"]
    assert cost == 0.1
    assert usage["output_tokens"] == 2

    silent = [
        event for event in events if event[0] == "pipeline.llm_role_stream_silent"
    ]
    assert silent
    _category, severity, _message, fields = silent[0]
    assert severity == "warn"
    assert fields["role"] == "master"
    assert fields["messages_seen"] == 1
    assert fields["text_chars"] == len("alpha")
    assert fields["silent_for_sec"] >= 0.02
    assert fields["silence_warn_sec"] == 0.02


def test_process_stream_hard_times_out_idle_role(monkeypatch, tmp_path):
    events = []

    async def fake_stream():
        yield AssistantMessage(content=[TextBlock(text="alpha")], model="sonnet")
        await asyncio.sleep(0.08)
        yield ResultMessage(
            subtype="success",
            duration_ms=10,
            duration_api_ms=10,
            is_error=False,
            num_turns=1,
            session_id="session",
            total_cost_usd=0.1,
            usage={"input_tokens": 4, "output_tokens": 2},
        )

    monkeypatch.setenv("POK_LLM_MASTER_IDLE_TIMEOUT", "0.02")
    monkeypatch.setenv("POK_LLM_MASTER_TOTAL_TIMEOUT", "1")
    monkeypatch.setattr(llm_query, "_LLM_SILENCE_WARN_SEC", 999)
    monkeypatch.setattr(
        llm_query,
        "_emit_llm_event",
        lambda category, severity, message, **fields: events.append(
            (category, severity, message, fields)
        ),
    )

    log_file = tmp_path / "v243" / "logs" / "master_io.txt"
    log_file.parent.mkdir(parents=True)

    with pytest.raises(llm_query.LLMRoleTimeout) as exc:
        asyncio.run(
            llm_query._process_stream(
                fake_stream(), str(log_file), _DummyUI(), "MASTER (Try 1)"
            )
        )

    assert exc.value.timeout_kind == "idle"
    timeout_events = [
        event for event in events if event[0] == "pipeline.llm_role_idle_timeout"
    ]
    assert timeout_events
    _category, severity, _message, fields = timeout_events[0]
    assert severity == "error"
    assert fields["role"] == "MASTER (Try 1)"
    assert fields["messages_seen"] == 1
    assert fields["idle_timeout"] == 0.02


def test_process_stream_unknown_messages_do_not_refresh_idle_timeout(
    monkeypatch, tmp_path
):
    events = []

    async def fake_stream():
        yield AssistantMessage(content=[TextBlock(text="alpha")], model="sonnet")
        for _ in range(20):
            await asyncio.sleep(0.005)
            yield _UnknownSDKMessage()
        yield ResultMessage(
            subtype="success",
            duration_ms=10,
            duration_api_ms=10,
            is_error=False,
            num_turns=1,
            session_id="session",
            total_cost_usd=0.1,
            usage={"input_tokens": 4, "output_tokens": 2},
        )

    monkeypatch.setenv("POK_LLM_MASTER_IDLE_TIMEOUT", "0.025")
    monkeypatch.setenv("POK_LLM_MASTER_TOTAL_TIMEOUT", "1")
    monkeypatch.setattr(llm_query, "_LLM_SILENCE_WARN_SEC", 999)
    monkeypatch.setattr(
        llm_query,
        "_emit_llm_event",
        lambda category, severity, message, **fields: events.append(
            (category, severity, message, fields)
        ),
    )

    log_file = tmp_path / "v269" / "logs" / "master_io.txt"
    log_file.parent.mkdir(parents=True)

    with pytest.raises(llm_query.LLMRoleTimeout) as exc:
        asyncio.run(
            llm_query._process_stream(
                fake_stream(), str(log_file), _DummyUI(), "MASTER (Try 1)"
            )
        )

    assert exc.value.timeout_kind == "idle"
    unknown_events = [
        event for event in events if event[0] == "pipeline.llm_role_unknown_message"
    ]
    assert unknown_events
    timeout_events = [
        event for event in events if event[0] == "pipeline.llm_role_idle_timeout"
    ]
    assert timeout_events
    _category, severity, _message, fields = timeout_events[0]
    assert severity == "error"
    assert fields["role"] == "MASTER (Try 1)"
    assert fields["text_chars"] == len("alpha")
    assert fields["unknown_messages_seen"] > 0
    assert fields["idle_timeout"] == 0.025


def test_process_stream_system_thinking_messages_are_observed_but_not_substantive(
    monkeypatch, tmp_path
):
    # B2 (2026-07-09): SystemMessages (init / thinking_tokens) are SDK/proxy
    # bookkeeping, not model output. They must still be observed (emit the
    # first-activity milestone and refresh last_message_at for the silence
    # watchdog), but they must NOT satisfy the substantive first-activity gate
    # — otherwise a backend that stalls right after init slips into the longer
    # idle_timeout dead-wait instead of being cut at first_activity_timeout.
    events = []

    async def fake_stream():
        for index in range(8):
            await asyncio.sleep(0.005)
            yield SystemMessage(
                subtype="thinking_tokens",
                data={
                    "estimated_tokens": index + 1,
                    "estimated_tokens_delta": 1,
                },
            )
        yield ResultMessage(
            subtype="success",
            duration_ms=10,
            duration_api_ms=10,
            is_error=False,
            num_turns=1,
            session_id="session",
            total_cost_usd=0.1,
            usage={"input_tokens": 4, "output_tokens": 2},
        )

    # Generous first-activity budget so the bookkeeping stream completes; this
    # verifies SystemMessages are observed and the ResultMessage terminates.
    monkeypatch.setenv("POK_LLM_MASTER_FIRST_ACTIVITY_TIMEOUT", "5")
    monkeypatch.setenv("POK_LLM_MASTER_IDLE_TIMEOUT", "5")
    monkeypatch.setenv("POK_LLM_MASTER_TOTAL_TIMEOUT", "10")
    monkeypatch.setattr(llm_query, "_LLM_SILENCE_WARN_SEC", 999)
    monkeypatch.setattr(
        llm_query,
        "_emit_llm_event",
        lambda category, severity, message, **fields: events.append(
            (category, severity, message, fields)
        ),
    )

    log_file = tmp_path / "v269" / "logs" / "master_io.txt"
    log_file.parent.mkdir(parents=True)

    texts, cost, usage = asyncio.run(
        llm_query._process_stream(
            fake_stream(), str(log_file), _DummyUI(), "MASTER (Try 1)"
        )
    )

    assert texts == []
    assert cost == 0.1
    assert usage["output_tokens"] == 2
    assert [
        event for event in events
        if event[0] == "pipeline.llm_role_unknown_message"
    ] == []

    # The first-activity milestone IS emitted for the SystemMessage (observability),
    # but it is recorded as non-substantive.
    first_activity = next(
        event for event in events
        if event[0] == "pipeline.llm_role_first_activity"
    )
    _category, severity, _message, fields = first_activity
    assert severity == "info"
    assert fields["activity_kind"] == "system:thinking_tokens"
    assert fields["substantive"] is False

    role_log = log_file.read_text(encoding="utf-8")
    assert "[SYSTEM_MESSAGE subtype=thinking_tokens" in role_log


def test_process_stream_binds_real_result_to_strict_call_context(tmp_path):
    import strict_authority_workflow

    result = ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=10,
        is_error=False,
        num_turns=1,
        session_id="strict-observed-session",
        total_cost_usd=0.0,
        usage={},
    )

    async def stream():
        yield result

    capture = {
        "invocation_id": "strict-invocation",
        "effect_id": "strict-effect",
        "results": [],
    }
    token = llm_query._STRICT_PROVIDER_RESULTS.set(capture)
    try:
        asyncio.run(llm_query._process_stream(
            stream(),
            str(tmp_path / "strict-role-io.txt"),
            _DummyUI(),
            "LEAD CODE REVIEWER",
        ))
    finally:
        llm_query._STRICT_PROVIDER_RESULTS.reset(token)

    assert capture["results"] == [result]
    assert strict_authority_workflow._provider_results_were_observed(
        capture["results"],
        invocation_id="strict-invocation",
        effect_id="strict-effect",
    )
    assert not strict_authority_workflow._provider_results_were_observed(
        capture["results"],
        invocation_id="different-invocation",
        effect_id="strict-effect",
    )
    strict_authority_workflow._consume_observed_provider_results(
        capture["results"]
    )


def test_process_stream_system_only_stall_times_out_at_first_activity(
    monkeypatch, tmp_path
):
    # B2: when a stream only produces SystemMessages and then goes silent (the
    # GLM-behind-cc-switch stall signature), the shorter first_activity_timeout
    # must fire — NOT the longer idle_timeout. Previously the init message
    # flipped the budget to idle_timeout, so each stalled attempt cost the full
    # idle budget before any recovery.
    events = []

    async def fake_stream():
        # init then silence forever (no AssistantMessage / ResultMessage)
        yield SystemMessage(subtype="init", data={})
        await asyncio.sleep(10.0)

    # first_activity is short, idle is long: only correct if SystemMessage does
    # NOT satisfy the substantive first-activity gate.
    monkeypatch.setenv("POK_LLM_MASTER_FIRST_ACTIVITY_TIMEOUT", "0.05")
    monkeypatch.setenv("POK_LLM_MASTER_IDLE_TIMEOUT", "5")
    monkeypatch.setenv("POK_LLM_MASTER_TOTAL_TIMEOUT", "10")
    monkeypatch.setattr(llm_query, "_LLM_SILENCE_WARN_SEC", 999)
    monkeypatch.setattr(
        llm_query,
        "_emit_llm_event",
        lambda category, severity, message, **fields: events.append(
            (category, severity, message, fields)
        ),
    )

    log_file = tmp_path / "v269" / "logs" / "crossover_io.txt"
    log_file.parent.mkdir(parents=True)

    with pytest.raises(llm_query.LLMRoleTimeout) as exc:
        asyncio.run(
            llm_query._process_stream(
                fake_stream(), str(log_file), _DummyUI(), "MASTER (Try 1)"
            )
        )

    # The critical assertion: it timed out at first_activity, not idle.
    assert exc.value.timeout_kind == "first_activity"
    timeout_events = [
        event for event in events
        if event[0] == "pipeline.llm_role_first_activity_timeout"
    ]
    assert timeout_events
    _category, severity, _message, fields = timeout_events[0]
    assert severity == "error"
    assert fields["first_activity_timeout"] == 0.05
    # The init message was observed but did not lift the gate.
    assert fields["system_messages_seen"] >= 1


def test_system_bookkeeping_cannot_refresh_mid_loop_stall_or_parent_progress(
    monkeypatch, tmp_path
):
    events = []

    async def fake_stream():
        yield AssistantMessage(
            content=[TextBlock(text="real model activity")], model="sonnet"
        )
        for index in range(100):
            await asyncio.sleep(0.005)
            yield SystemMessage(
                subtype="thinking_tokens",
                data={
                    "estimated_tokens": index + 1,
                    "estimated_tokens_delta": 1,
                },
            )

    monkeypatch.setenv("POK_LLM_MASTER_FIRST_ACTIVITY_TIMEOUT", "1")
    monkeypatch.setenv("POK_LLM_MASTER_IDLE_TIMEOUT", "1")
    monkeypatch.setenv("POK_LLM_MASTER_STALL_TIMEOUT", "0.04")
    monkeypatch.setenv("POK_LLM_MASTER_TOTAL_TIMEOUT", "2")
    monkeypatch.setattr(llm_query, "_LLM_PROGRESS_INTERVAL_SEC", 0.001)
    monkeypatch.setattr(llm_query, "_LLM_SILENCE_WARN_SEC", 999)
    monkeypatch.setattr(
        llm_query,
        "_emit_llm_event",
        lambda category, severity, message, **fields: events.append(
            (category, severity, message, fields)
        ),
    )

    with pytest.raises(llm_query.LLMRoleTimeout) as exc:
        asyncio.run(
            llm_query._process_stream(
                fake_stream(),
                str(tmp_path / "master_system_flood_io.txt"),
                _DummyUI(),
                "MASTER PROPOSAL counterfactual",
            )
        )

    assert exc.value.timeout_kind == "stall"
    assert not [
        event
        for event in events
        if event[0] == "pipeline.llm_role_progress"
    ]
    timeout = next(
        event for event in events
        if event[0] == "pipeline.llm_role_stall_timeout"
    )
    assert timeout[3]["system_messages_seen"] > 0


def test_process_stream_mid_loop_stall_cuts_at_stall_timeout(monkeypatch, tmp_path):
    # B3 (2026-07-09): once a stream has produced substantive output (entered
    # the tool/think loop), a mid-loop stall — a tool_use is emitted but its
    # tool_result never returns, or the model stops streaming mid-think — must
    # be caught at the shorter stall_timeout, NOT the longer idle_timeout.
    # Otherwise every mid-loop stall costs the full idle budget (240-420s)
    # before the role retry can restart. This is the recurring tool-exec stall
    # observed against the deepseek-v4-pro endpoint behind cc-switch.
    events = []

    async def fake_stream():
        # substantive output (AssistantMessage with text) then silence forever
        yield AssistantMessage(content=[TextBlock(text="partial plan")], model="sonnet")
        await asyncio.sleep(10.0)

    # stall_timeout is SHORT; idle is LONG: only correct if the stall ceiling
    # is enforced inside the tool/think loop.
    monkeypatch.setenv("POK_LLM_MASTER_FIRST_ACTIVITY_TIMEOUT", "5")
    monkeypatch.setenv("POK_LLM_MASTER_IDLE_TIMEOUT", "300")
    monkeypatch.setenv("POK_LLM_MASTER_STALL_TIMEOUT", "0.5")
    monkeypatch.setenv("POK_LLM_MASTER_TOTAL_TIMEOUT", "600")
    monkeypatch.setattr(llm_query, "_LLM_SILENCE_WARN_SEC", 999)
    monkeypatch.setattr(
        llm_query,
        "_emit_llm_event",
        lambda category, severity, message, **fields: events.append(
            (category, severity, message, fields)
        ),
    )

    log_file = tmp_path / "v269" / "logs" / "master_io.txt"
    log_file.parent.mkdir(parents=True)

    with pytest.raises(llm_query.LLMRoleTimeout) as exc:
        asyncio.run(
            llm_query._process_stream(
                fake_stream(), str(log_file), _DummyUI(), "MASTER (Try 1)"
            )
        )

    # The critical assertion: it timed out at stall (~0.5s), not idle (300s).
    assert exc.value.timeout_kind == "stall"
    timeout_events = [
        event for event in events
        if event[0] == "pipeline.llm_role_stall_timeout"
    ]
    assert timeout_events
    _category, severity, _message, fields = timeout_events[0]
    assert severity == "error"
    assert fields["role"] == "MASTER (Try 1)"
    assert fields["stall_timeout"] == 0.5


def test_default_role_timeout_policy_is_bounded(monkeypatch):
    monkeypatch.delenv("POK_LLM_DEFAULT_FIRST_ACTIVITY_TIMEOUT", raising=False)
    monkeypatch.delenv("POK_LLM_DEFAULT_IDLE_TIMEOUT", raising=False)
    monkeypatch.delenv("POK_LLM_DEFAULT_TOTAL_TIMEOUT", raising=False)
    monkeypatch.delenv("POK_LLM_DEFAULT_STALL_TIMEOUT", raising=False)

    policy = llm_query._role_timeout_policy("COMBINED ANALYST")

    assert policy["policy_key"] == "DEFAULT"
    assert policy["first_activity_timeout"] > 0
    assert policy["idle_timeout"] > 0
    assert policy["total_timeout"] > 0
    # B3: stall_timeout is derived from idle (~55%, clamped) and exposed.
    assert policy["stall_timeout"] > 0
    assert policy["stall_timeout"] < policy["idle_timeout"]


def test_crossover_role_timeout_policy_has_extended_total(monkeypatch):
    monkeypatch.delenv("POK_LLM_CROSSOVER_FIRST_ACTIVITY_TIMEOUT", raising=False)
    monkeypatch.delenv("POK_LLM_CROSSOVER_IDLE_TIMEOUT", raising=False)
    monkeypatch.delenv("POK_LLM_CROSSOVER_TOTAL_TIMEOUT", raising=False)

    default_policy = llm_query._role_timeout_policy("COMBINED ANALYST")
    crossover_policy = llm_query._role_timeout_policy("CROSSOVER v200x254")

    assert crossover_policy["policy_key"] == "CROSSOVER"
    assert crossover_policy["total_timeout"] > default_policy["total_timeout"]
    assert crossover_policy["idle_timeout"] > 0


def test_process_stream_hard_times_out_default_role_first_activity(monkeypatch, tmp_path):
    events = []

    async def fake_stream():
        await asyncio.sleep(0.08)
        yield AssistantMessage(content=[TextBlock(text="late")], model="sonnet")

    monkeypatch.setenv("POK_LLM_DEFAULT_FIRST_ACTIVITY_TIMEOUT", "0.02")
    monkeypatch.setenv("POK_LLM_DEFAULT_IDLE_TIMEOUT", "1")
    monkeypatch.setenv("POK_LLM_DEFAULT_TOTAL_TIMEOUT", "1")
    monkeypatch.setattr(
        llm_query,
        "_emit_llm_event",
        lambda category, severity, message, **fields: events.append(
            (category, severity, message, fields)
        ),
    )

    log_file = tmp_path / "v254" / "logs" / "combined_analysis.txt"
    log_file.parent.mkdir(parents=True)

    with pytest.raises(llm_query.LLMRoleTimeout) as exc:
        asyncio.run(
            llm_query._process_stream(
                fake_stream(), str(log_file), _DummyUI(), "COMBINED ANALYST"
            )
        )

    assert exc.value.timeout_kind == "first_activity"
    timeout_events = [
        event for event in events if event[0] == "pipeline.llm_role_first_activity_timeout"
    ]
    assert timeout_events
    _category, severity, _message, fields = timeout_events[0]
    assert severity == "error"
    assert fields["role"] == "COMBINED ANALYST"
    assert fields["first_activity_timeout"] == 0.02


def test_process_stream_unknown_messages_do_not_satisfy_first_activity(
    monkeypatch, tmp_path
):
    events = []

    async def fake_stream():
        for _ in range(20):
            await asyncio.sleep(0.005)
            yield _UnknownSDKMessage()
        yield AssistantMessage(content=[TextBlock(text="late")], model="sonnet")

    monkeypatch.setenv("POK_LLM_DEFAULT_FIRST_ACTIVITY_TIMEOUT", "0.02")
    monkeypatch.setenv("POK_LLM_DEFAULT_IDLE_TIMEOUT", "1")
    monkeypatch.setenv("POK_LLM_DEFAULT_TOTAL_TIMEOUT", "1")
    monkeypatch.setattr(llm_query, "_LLM_SILENCE_WARN_SEC", 999)
    monkeypatch.setattr(
        llm_query,
        "_emit_llm_event",
        lambda category, severity, message, **fields: events.append(
            (category, severity, message, fields)
        ),
    )

    log_file = tmp_path / "v269" / "logs" / "combined_analysis.txt"
    log_file.parent.mkdir(parents=True)

    with pytest.raises(llm_query.LLMRoleTimeout) as exc:
        asyncio.run(
            llm_query._process_stream(
                fake_stream(), str(log_file), _DummyUI(), "COMBINED ANALYST"
            )
        )

    assert exc.value.timeout_kind == "first_activity"
    unknown_events = [
        event for event in events if event[0] == "pipeline.llm_role_unknown_message"
    ]
    assert unknown_events
    timeout_events = [
        event for event in events if event[0] == "pipeline.llm_role_first_activity_timeout"
    ]
    assert timeout_events
    _category, severity, _message, fields = timeout_events[0]
    assert severity == "error"
    assert fields["role"] == "COMBINED ANALYST"
    assert fields["unknown_messages_seen"] > 0
    assert fields["first_activity_timeout"] == 0.02


def test_subagent_cost_guard_blocks_unbounded_git_history():
    assert (
        llm_query._subagent_bash_cost_detector("git log --all -S foo")
        == "git_log_all_history"
    )
    assert (
        llm_query._subagent_bash_cost_detector("git log -Sfoo -- policy.py")
        == "git_log_pickaxe_full_history"
    )
    assert (
        llm_query._subagent_bash_cost_detector("git log --oneline")
        == "git_log_unbounded_history"
    )
    assert llm_query._subagent_bash_cost_detector(
        "git log --oneline --max-count 20"
    ) is None
    assert llm_query._subagent_bash_cost_detector(
        "git log --oneline national-bot-v250..HEAD"
    ) is None


def test_subagent_cost_guard_denial_is_recoverable_warning(monkeypatch):
    import system_log

    events = []
    monkeypatch.setattr(
        system_log,
        "log_system_event",
        lambda category, severity, message, data=None: events.append(
            (category, severity, message, data or {})
        ),
    )

    hooks = llm_query._make_subagent_cost_guard("MASTER")
    handler = hooks["PreToolUse"][0].hooks[0]
    output = asyncio.run(
        handler(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "git log --oneline --all national-bot-v143..HEAD"},
            },
            "tool-use-1",
            {},
        )
    )

    decision = output["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "runtime cost guard" in decision["permissionDecisionReason"]
    assert events
    category, severity, message, data = events[0]
    assert category == "pipeline.subagent_cost_guard_block"
    assert severity == "warn"
    assert "MASTER" in message
    assert data["reason"] == "git_log_all_history"
    assert data["recoverable"] is True
    assert data["next_action"] == "retry_with_bounded_inspection"


@pytest.mark.parametrize(
    "command",
    (
        "git log --max-count=1 national-bot-v143",
        "git --no-pager show national-bot-v143:policy.py",
        "git rev-list --max-count=1 HEAD",
        "bash -lc 'git log -1 -- policy.py'",
    ),
)
def test_strategy_critic_guard_denies_all_git_history_reads(
    monkeypatch, command
):
    import system_log

    events = []
    monkeypatch.setattr(
        system_log,
        "log_system_event",
        lambda category, severity, message, data=None: events.append(
            (category, severity, message, data or {})
        ),
    )
    hooks = llm_query._make_subagent_cost_guard("STRATEGY CRITIC")
    handler = hooks["PreToolUse"][0].hooks[0]

    output = asyncio.run(
        handler(
            {"tool_name": "Bash", "tool_input": {"command": command}},
            "critic-history",
            {},
        )
    )

    decision = output["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "not admissible critic evidence" in decision[
        "permissionDecisionReason"
    ]
    category, severity, _message, data = events[0]
    assert category == "pipeline.subagent_role_evidence_guard_block"
    assert severity == "error"
    assert data["role"] == "STRATEGY CRITIC"
    assert data["next_action"] == "use_frozen_envelope_and_exact_diff"


def test_strategy_critic_guard_allows_exact_diff_but_other_roles_keep_bounded_log():
    critic_hooks = llm_query._make_subagent_cost_guard("STRATEGY CRITIC")
    critic_handler = critic_hooks["PreToolUse"][0].hooks[0]
    allowed_diff = asyncio.run(
        critic_handler(
            {
                "tool_name": "Bash",
                "tool_input": {
                    "command": "git diff --no-index -- parent/policy.py target/policy.py"
                },
            },
            "critic-diff",
            {},
        )
    )
    assert allowed_diff.get("hookSpecificOutput") is None

    master_hooks = llm_query._make_subagent_cost_guard("MASTER")
    master_handler = master_hooks["PreToolUse"][0].hooks[0]
    allowed_log = asyncio.run(
        master_handler(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "git log --max-count=1 HEAD"},
            },
            "master-log",
            {},
        )
    )
    assert allowed_log.get("hookSpecificOutput") is None


def test_readonly_guard_denial_suggests_non_mutating_comparison():
    hint = llm_query._readonly_guard_recovery_hint("write_redirect:/tmp/v73_sfp.txt")

    assert "Do not create temp files" in hint
    assert "diff -u parent_file target_file" in hint
    assert "git diff --no-index -- parent target" in hint
    assert "Redirect only to `/dev/null`" in hint


def test_reviewer_and_critic_forbid_git_history():
    prompts_dir = Path(__file__).resolve().parents[1] / "core" / "prompts"
    reviewer = (prompts_dir / "reviewer_prompt.md").read_text(encoding="utf-8")
    critic = (prompts_dir / "critic_prompt.md").read_text(encoding="utf-8")

    assert "Git history" in reviewer
    assert "git diff --no-index" in reviewer
    assert "--max-count" not in reviewer
    assert "git log" not in reviewer

    assert "only tool is Read" in critic
    assert "Bash, Git, Python subprocesses" in critic
    assert "SYSTEM-SUPPLIED" not in critic  # bytes are injected at runtime
    assert "another lineage comparison" in critic
    assert "git log --oneline" not in critic


def test_subagent_mutation_guard_allows_dev_null_in_command_substitution():
    command = """cd bots && for f in national_bot.py policy.py; do
  diff_lines=$(diff national_v243/$f national_v248/$f 2>/dev/null | wc -l)
  v243_lines=$(wc -l < national_v243/$f 2>/dev/null)
  echo "$f: v243=${v243_lines}L diff=${diff_lines}"
done"""

    assert llm_query._subagent_bash_mutation_detector(command) is None
    assert list(llm_query._iter_shell_write_redirect_targets(command)) == [
        "/dev/null",
        "/dev/null",
    ]


def test_subagent_mutation_guard_ignores_redirect_operators_inside_comments():
    command = """# Get the full policy.py diff for the to_call>0 block
diff bots/national_v243/policy.py bots/national_v249/policy.py | wc -l
echo "---"
# Check the choose_raise changes more carefully
diff bots/national_v243/policy.py bots/national_v249/policy.py | tail -200
"""

    assert llm_query._subagent_bash_mutation_detector(command) is None
    assert list(llm_query._iter_shell_write_redirect_targets(command)) == []
    assert (
        llm_query._subagent_bash_mutation_detector(
            "printf '# not a comment' > web/core/tmp.txt"
        )
        == "write_redirect:web/core/tmp.txt"
    )


def test_subagent_mutation_guard_still_blocks_real_redirect():
    assert (
        llm_query._subagent_bash_mutation_detector("echo x > web/core/tmp.txt")
        == "write_redirect:web/core/tmp.txt"
    )


def test_runtime_path_contract_warns_against_tmp_probe_logs(tmp_path):
    target_dir = tmp_path / "bots" / "national_v243"
    target_dir.mkdir(parents=True)

    contract = llm_query._format_runtime_path_contract(tmp_path, target_dir)

    assert str(target_dir) in contract
    assert "/tmp" in contract
    assert "/var/tmp" in contract
    assert "2>&1 | grep" in contract
    assert "Cleanup is also a write" in contract
    assert "Only mutate files inside the declared write scope" in contract
    assert "`__pycache__`" in contract
    assert "source, parent, opponent, or other bot directories" in contract
    assert "leave them in place" in contract
    assert "diff --exclude=__pycache__" in contract


def test_runtime_path_contract_readonly_roles_ban_temp_redirects(tmp_path):
    contract = llm_query._format_runtime_path_contract(tmp_path, allowed_write_dir=None)

    assert "This LLM role is read-only" in contract
    assert "Do not use output redirection" in contract
    assert "`tee`" in contract
    assert "diff -u EXACT_A EXACT_B" in contract
    assert "Never write comparison snippets to `/tmp`" in contract


def test_worker_and_crossover_prompts_ban_tmp_probe_logs_and_parent_cache_cleanup():
    prompt_dir = Path(__file__).resolve().parents[1] / "core" / "prompts"
    for name in ("worker_prompt.md", "crossover_prompt.md"):
        text = (prompt_dir / name).read_text(encoding="utf-8")
        assert "`/tmp`" in text
        assert "`/var/tmp`" in text
        assert "2>&1 | grep" in text
        assert "Cleanup is also mutation" in text
        assert "Do not perform cache cleanup from Bash" in text
        assert "`__pycache__`" in text
        assert "leave them in place" in text
        assert "the harness ignores those caches" in text
        assert "rm -rf\n__pycache__" not in text
        assert "rm -rf __pycache__" not in text


def test_generation_prompts_explain_oversized_parent_line_limit():
    prompt_dir = Path(__file__).resolve().parents[1] / "core" / "prompts"

    worker = (prompt_dir / "worker_prompt.md").read_text(encoding="utf-8")
    assert "LINE-COUNT GATE CONTRACT" in worker
    assert "If the injected contract says the source was\nalready oversized" in worker
    assert "15% growth budget does not apply to an already-oversized source" in worker
    assert "repair-contract limit" in worker
    assert "authoritative" in worker

    crossover = (prompt_dir / "crossover_prompt.md").read_text(encoding="utf-8")
    assert "If Parent A/source is already over the base limit" in crossover
    assert "15% growth budget does\n    not apply to already-oversized parents" in crossover


def test_run_claude_query_downgrades_success_error_result_to_info(monkeypatch, tmp_path):
    events = []

    async def fake_stream(*_args, **_kwargs):
        raise Exception("Claude Code returned an error result: success")

    monkeypatch.setattr(llm_query, "_run_stream_with_signature_retry", fake_stream)
    monkeypatch.setattr(
        llm_query,
        "_emit_llm_event",
        lambda category, severity, message, **fields: events.append(
            (category, severity, message, fields)
        ),
    )
    monkeypatch.setattr(llm_query.asyncio, "sleep", _no_wait)

    log_file = tmp_path / "match_analyst_llm.txt"

    with pytest.raises(Exception):
        asyncio.run(
            llm_query.run_claude_query(
                _rendered("COMBINED ANALYST", "prompt"),
                [],
                _DummyUI(),
                "COMBINED ANALYST",
                str(log_file),
            )
        )

    failed = [event for event in events if event[0] == "pipeline.llm_role_failed"]
    assert len(failed) == 1
    _category, severity, _message, fields = failed[0]
    assert severity == "info"
    assert fields["role"] == "COMBINED ANALYST"
    assert "error result: success" in fields["error"]


def test_run_claude_query_parent_timeout_cancel_is_typed(monkeypatch, tmp_path):
    events = []

    async def fake_stream(*_args, **_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(llm_query, "_run_stream_with_signature_retry", fake_stream)
    monkeypatch.setattr(
        llm_query,
        "_emit_llm_event",
        lambda category, severity, message, **fields: events.append(
            (category, severity, message, fields)
        ),
    )

    with pytest.raises(asyncio.CancelledError):
        with llm_query.llm_cancel_scope(
            "dynamic_test_gen", reason="parent_timeout", timeout_sec=25
        ):
            asyncio.run(
                llm_query.run_claude_query(
                    _rendered("WORKER_COT_CHECK_dynamic_test_gen", "prompt"),
                    [],
                    _DummyUI(),
                    "WORKER_COT_CHECK_dynamic_test_gen",
                    str(tmp_path / "dynamic_test_gen_io.txt"),
                )
            )

    categories = [event[0] for event in events]
    assert "pipeline.llm_role_parent_timeout_cancelled" in categories
    assert "pipeline.llm_role_cancelled" not in categories
    cancelled = next(
        event for event in events
        if event[0] == "pipeline.llm_role_parent_timeout_cancelled"
    )
    _category, severity, message, fields = cancelled
    assert severity == "info"
    assert "parent timeout" in message
    assert fields["cancel_scope"] == "dynamic_test_gen"
    assert fields["cancel_reason"] == "parent_timeout"
    assert fields["timeout_sec"] == 25.0


def test_process_stream_parent_timeout_cancel_is_typed(monkeypatch, tmp_path):
    events = []

    async def fake_stream():
        yield AssistantMessage(content=[TextBlock(text="partial")], model="sonnet")
        raise asyncio.CancelledError()

    monkeypatch.setattr(
        llm_query,
        "_emit_llm_event",
        lambda category, severity, message, **fields: events.append(
            (category, severity, message, fields)
        ),
    )

    with pytest.raises(asyncio.CancelledError):
        with llm_query.llm_cancel_scope(
            "dynamic_test_gen", reason="parent_timeout", timeout_sec=25
        ):
            asyncio.run(
                llm_query._process_stream(
                    fake_stream(),
                    str(tmp_path / "dynamic_test_gen_io.txt"),
                    _DummyUI(),
                    "DYNAMIC_TEST_GEN",
                )
            )

    categories = [event[0] for event in events]
    assert "pipeline.llm_role_stream_parent_timeout_cancelled" in categories
    assert "pipeline.llm_role_stream_cancelled" not in categories
    cancelled = next(
        event for event in events
        if event[0] == "pipeline.llm_role_stream_parent_timeout_cancelled"
    )
    _category, severity, message, fields = cancelled
    assert severity == "info"
    assert "parent timeout" in message
    assert fields["cancel_scope"] == "dynamic_test_gen"
    assert fields["cancel_reason"] == "parent_timeout"


def test_role_log_metadata_preserves_nested_strict_invocation_identity():
    import llm_query

    path = (
        "/tmp/results/v143/logs/strict_invocations/"
        + "1" * 32
        + "/master_proposal_mechanism_io.txt"
    )
    assert llm_query._role_log_metadata(path) == {
        "log_file": path,
        "version": 143,
        "role_log": "master_proposal_mechanism",
    }


@pytest.mark.parametrize("path_kind", ["path", "string"])
def test_role_log_rotation_accepts_path_and_replaces_backup(
    monkeypatch,
    tmp_path,
    path_kind,
):
    monkeypatch.setattr(llm_query, "_ROLE_IO_MAX_BYTES", 1)
    log_file = tmp_path / f"{path_kind}_io.txt"
    log_file.write_text("old live bytes\n", encoding="utf-8")
    rotated = Path(str(log_file) + ".1")
    rotated.write_text("stale backup\n", encoding="utf-8")

    llm_query._append_role_io(
        log_file if path_kind == "path" else str(log_file),
        "new live bytes\n",
    )

    assert rotated.read_text(encoding="utf-8") == "old live bytes\n"
    live = log_file.read_text(encoding="utf-8")
    assert "new live bytes" in live
    assert "old live bytes" not in live


def test_strict_invocation_log_never_rotates_evidence_bytes(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(llm_query, "_ROLE_IO_MAX_BYTES", 1)
    log_file = (
        tmp_path
        / "v143"
        / "logs"
        / "strict_invocations"
        / ("8" * 32)
        / "critic_io.txt"
    )
    log_file.parent.mkdir(parents=True)
    log_file.write_text("sealed prefix\n", encoding="utf-8")

    llm_query._append_role_io(log_file, "continued evidence\n")

    assert not Path(str(log_file) + ".1").exists()
    live = log_file.read_text(encoding="utf-8")
    assert "sealed prefix" in live
    assert "continued evidence" in live
