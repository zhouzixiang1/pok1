import asyncio

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

from core import llm_query
from core import evolution_infra


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

    context_file = tmp_path / "ctx.txt"
    context_file.write_text("context body", encoding="utf-8")
    log_file = tmp_path / "v243" / "logs" / "master_io.txt"
    log_file.parent.mkdir(parents=True)

    output, cost, usage = asyncio.run(
        llm_query.run_claude_query(
            "base prompt",
            [str(context_file)],
            _DummyUI(),
            "master",
            str(log_file),
            tools=["Read"],
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

    assert start["role"] == "master"
    assert start["context_file_count"] == 1
    assert start["tools"] == ["Read"]
    assert start["log_file"] == str(log_file)
    assert done["cost_usd"] == 0.125
    assert done["output_chars"] == len("hello")
    assert done["input_tokens"] == 10
    assert done["output_tokens"] == 3


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
    target = evolution_infra.PROJECT_ROOT / "bots" / "claude_v282" / "opponent.py"

    output, _cost, _usage = asyncio.run(
        llm_query.run_claude_query(
            "base prompt",
            [],
            _DummyUI(),
            "worker",
            str(log_file),
            tools=["Read", "Edit"],
            allowed_write_dir={"files": [target]},
        )
    )

    assert output == "ok"
    assert seen["cwd"] == str(evolution_infra.PROJECT_ROOT)
    assert "# Runtime Path Contract" in seen["prompt"]
    assert f"`{evolution_infra.PROJECT_ROOT}`" in seen["prompt"]
    assert f"`{target}`" in seen["prompt"]
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
                "prompt",
                [],
                _DummyUI(),
                "reviewer",
                str(log_file),
            )
        )

    failed = [event for event in events if event[0] == "pipeline.llm_role_failed"]
    assert len(failed) == 1
    _category, severity, message, fields = failed[0]
    assert severity == "error"
    assert "reviewer" in message
    assert fields["role"] == "reviewer"
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
                    "prompt",
                    [],
                    _DummyUI(),
                    "MATCH ANALYST",
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
    assert fields["role"] == "MATCH ANALYST"
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
                "prompt",
                [],
                _DummyUI(),
                "MATCH ANALYST",
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
    assert fields["role"] == "MATCH ANALYST"
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
                "prompt",
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


def test_process_stream_system_thinking_messages_are_productive_activity(
    monkeypatch, tmp_path
):
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

    monkeypatch.setenv("POK_LLM_MASTER_FIRST_ACTIVITY_TIMEOUT", "0.02")
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

    first_activity = next(
        event for event in events
        if event[0] == "pipeline.llm_role_first_activity"
    )
    _category, severity, _message, fields = first_activity
    assert severity == "info"
    assert fields["activity_kind"] == "system:thinking_tokens"

    role_log = log_file.read_text(encoding="utf-8")
    assert "[SYSTEM_MESSAGE subtype=thinking_tokens" in role_log


def test_default_role_timeout_policy_is_bounded(monkeypatch):
    monkeypatch.delenv("POK_LLM_DEFAULT_FIRST_ACTIVITY_TIMEOUT", raising=False)
    monkeypatch.delenv("POK_LLM_DEFAULT_IDLE_TIMEOUT", raising=False)
    monkeypatch.delenv("POK_LLM_DEFAULT_TOTAL_TIMEOUT", raising=False)

    policy = llm_query._role_timeout_policy("COMBINED ANALYST")

    assert policy["policy_key"] == "DEFAULT"
    assert policy["first_activity_timeout"] > 0
    assert policy["idle_timeout"] > 0
    assert policy["total_timeout"] > 0


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
        llm_query._subagent_bash_cost_detector("git log -Sfoo -- strategy.py")
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
        "git log --oneline bot-v250..HEAD"
    ) is None


def test_subagent_mutation_guard_allows_dev_null_in_command_substitution():
    command = """cd bots && for f in main.py strategy.py; do
  diff_lines=$(diff claude_v239/$f claude_v248/$f 2>/dev/null | wc -l)
  v239_lines=$(wc -l < claude_v239/$f 2>/dev/null)
  echo "$f: v239=${v239_lines}L diff=${diff_lines}"
done"""

    assert llm_query._subagent_bash_mutation_detector(command) is None
    assert list(llm_query._iter_shell_write_redirect_targets(command)) == [
        "/dev/null",
        "/dev/null",
    ]


def test_subagent_mutation_guard_ignores_redirect_operators_inside_comments():
    command = """# Get the full strategy.py diff for the to_call>0 block
diff bots/claude_v200/strategy.py bots/claude_v249/strategy.py | wc -l
echo "---"
# Check the choose_raise changes more carefully
diff bots/claude_v200/strategy.py bots/claude_v249/strategy.py | tail -200
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

    log_file = tmp_path / "battle_exp_llm.txt"

    with pytest.raises(Exception):
        asyncio.run(
            llm_query.run_claude_query(
                "prompt",
                [],
                _DummyUI(),
                "battle_experience",
                str(log_file),
            )
        )

    failed = [event for event in events if event[0] == "pipeline.llm_role_failed"]
    assert len(failed) == 1
    _category, severity, _message, fields = failed[0]
    assert severity == "info"
    assert fields["role"] == "battle_experience"
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
                    "prompt",
                    [],
                    _DummyUI(),
                    "DYNAMIC_TEST_GEN",
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
