import asyncio

import pytest
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from core import llm_query


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
