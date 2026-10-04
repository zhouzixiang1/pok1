"""Tests for the POK_LLM_TRANSPORT codex adapter (web/core/llm_query_codex.py).

Covers the hard contracts of the switchable transport layer:

1. dispatch — default is the unchanged claude path; ``POK_LLM_TRANSPORT=codex``
   routes through the codex adapter at the same retry-loop chokepoint, with
   the global-semaphore acquisition point untouched;
2. JSONL event parsing — codex ``--json`` events translate into the exact SDK
   message objects and the usage mapping feeds llm_call_metrics with its
   schema unchanged (missing fields 0/null, keys present);
3. error text propagation — codex provider failures raise ClaudeSDKError whose
   body carries the raw GLM 1302/1308 envelope so classify_llm_availability
   (and the quota pause chain behind it) keeps parsing them;
4. timeout/cancel — the one-shot process tree is killed and exit confirmed via
   returncode on the same provider-attempt cleanup predicates;
5. binary resolution — the configured binary is pre-resolved via shutil.which
   before every spawn and the web lifespan fails fast when the codex transport
   is selected but the binary is unresolvable in the service PATH (2026-10-03:
   every dispatch died as a bare FileNotFoundError 'codex' because the service
   PATH lacks ~/.local/bin, burning v490/v491/v492); the actionable failure is
   a service-configuration error (CodexBinaryNotFound on the preflight surface,
   ClaudeSDKError with identical text on the dispatch path) and must NEVER be
   classified as an LLM availability issue (no 1302/1308/quota/cooldown pause
   can be armed from it);
6. SDK-free preflight — the transport-selection + binary-resolution surface is
   stdlib-only and importable by ANY python3: a service-environment smoke (or
   systemd ExecStartPre) whose ``python3`` resolves from the service PATH
   without the venv still gets the binary diagnosis instead of
   ``ModuleNotFoundError: No module named 'claude_agent_sdk'`` (2026-10-04);
   SDK-typed entries (translator/codex_query) fail closed with the POK_PYTHON
   guidance under such an interpreter.
"""

import asyncio
import json

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    ResultMessage,
    SystemMessage,
    TextBlock,
)

import llm_query
import llm_query_codex as lcx
from llm_availability import (
    classify_llm_availability,
    QUOTA_429,
    SERVICE_UNAVAILABLE,
)


def asyncio_run(coro):
    return asyncio.run(coro)


def llm_query_codex_module():
    # The retry loop resolves ``codex_query`` / ``new_codex_exec_transport``
    # through this companion module object, so patching the module attribute
    # is the live seam (mirrors the llm_query monkeypatch contract).
    import llm_query_retry

    return llm_query_retry._cx


class _NullUI:
    def log_io(self, *args, **kwargs):
        pass

    def log_history(self, *args, **kwargs):
        pass

    def update_cost(self, *args, **kwargs):
        pass

    def emit_tool_call(self, *args, **kwargs):
        pass


class _FakeGenerator:
    """Minimal async generator stand-in (never iterated; _process_stream is
    monkeypatched in the dispatch tests)."""

    def __init__(self):
        async def _gen():
            if False:  # pragma: no cover - body never runs
                yield

        self._gen = _gen()

    def __aiter__(self):
        return self._gen

    async def aclose(self):
        await self._gen.aclose()


async def _noop_sleep(*_args, **_kwargs):
    return None


def _smoke_events():
    """The exact event stream shape observed in the real codex smoke run."""

    return [
        {"type": "thread.started", "thread_id": "01a101c5-0000"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"id": "item_0", "type": "reasoning", "text": "thinking…"},
        },
        {
            "type": "item.completed",
            "item": {"id": "item_1", "type": "agent_message", "text": "CODEX_OK"},
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 4775,
                "cached_input_tokens": 64,
                "cache_write_input_tokens": 0,
                "output_tokens": 24,
                "reasoning_output_tokens": 19,
            },
        },
    ]


# ---------------------------------------------------------------------------
# Contract 1: dispatch (default claude; codex routing; semaphore point)
# ---------------------------------------------------------------------------


def test_default_transport_is_claude(monkeypatch):
    monkeypatch.delenv("POK_LLM_TRANSPORT", raising=False)
    assert lcx.codex_transport_enabled() is False

    calls = {"claude": 0}

    def fake_claude_query(*_args, **_kwargs):
        calls["claude"] += 1
        return _FakeGenerator()

    async def fake_process_stream(query_gen, log_file_path, ui, role_name):
        return (["ok"], 0.0, {}, {})

    monkeypatch.setattr(llm_query, "claude_query", fake_claude_query)
    monkeypatch.setattr(llm_query, "_process_stream", fake_process_stream)
    monkeypatch.setattr(llm_query.asyncio, "sleep", _noop_sleep)

    result = asyncio_run(
        llm_query._run_stream_with_signature_retry(
            "prompt", ClaudeAgentOptions(), "/tmp/none.log", None, "role"
        )
    )
    assert result[0] == ["ok"]
    assert calls["claude"] == 1


def test_codex_transport_dispatches_through_adapter(monkeypatch):
    monkeypatch.setenv("POK_LLM_TRANSPORT", "codex")
    assert lcx.codex_transport_enabled() is True

    calls = {"claude": 0, "codex": 0}

    def fake_claude_query(*_args, **_kwargs):
        calls["claude"] += 1
        return _FakeGenerator()

    def fake_codex_query(prompt=None, *, options=None, transport=None):
        calls["codex"] += 1
        assert prompt == "prompt"
        assert isinstance(transport, lcx.CodexExecTransport)

        async def _gen():
            yield 1

        return _gen()

    async def fake_process_stream(query_gen, log_file_path, ui, role_name):
        # Prove the codex generator (not the claude _FakeGenerator) reached
        # the stream processor.
        assert type(query_gen).__name__ == "async_generator"
        return (["ok"], 0.0, {}, {})

    monkeypatch.setattr(llm_query, "claude_query", fake_claude_query)
    monkeypatch.setattr(llm_query_codex_module(), "codex_query", fake_codex_query)
    monkeypatch.setattr(llm_query, "_process_stream", fake_process_stream)
    monkeypatch.setattr(llm_query.asyncio, "sleep", _noop_sleep)

    result = asyncio_run(
        llm_query._run_stream_with_signature_retry(
            "prompt", ClaudeAgentOptions(), "/tmp/none.log", None, "role"
        )
    )
    assert result[0] == ["ok"]
    assert calls == {"claude": 0, "codex": 1}


def test_codex_transport_acquires_global_semaphore_around_stream(monkeypatch):
    """The permit acquisition point is unchanged: acquired around the stream
    processing call for both transports (contract 1)."""

    monkeypatch.setenv("POK_LLM_TRANSPORT", "codex")
    order = []

    class _RecordingSemaphore:
        async def __aenter__(self):
            order.append("acquire")
            return self

        async def __aexit__(self, *exc):
            order.append("release")
            return False

    def fake_codex_query(prompt=None, *, options=None, transport=None):
        async def _gen():
            yield 1

        return _gen()

    async def fake_process_stream(query_gen, log_file_path, ui, role_name):
        order.append("stream")
        return (["ok"], 0.0, {}, {})

    monkeypatch.setattr(llm_query_codex_module(), "codex_query", fake_codex_query)
    monkeypatch.setattr(llm_query, "_process_stream", fake_process_stream)
    monkeypatch.setattr(llm_query.asyncio, "sleep", _noop_sleep)

    asyncio_run(
        llm_query._run_stream_with_signature_retry(
            "prompt",
            ClaudeAgentOptions(),
            "/tmp/none.log",
            None,
            "role",
            semaphore=_RecordingSemaphore(),
        )
    )
    assert order == ["acquire", "stream", "release"]


# ---------------------------------------------------------------------------
# Contract 2: JSONL event translation -> SDK messages + metrics usage
# ---------------------------------------------------------------------------


def test_translator_maps_events_to_sdk_messages():
    translator = lcx.CodexEventTranslator(model_label="glm-5.3")
    messages = [m for event in _smoke_events() for m in translator.translate(event)]

    kinds = [type(m).__name__ for m in messages]
    # thread.started/turn.started -> SystemMessage bookkeeping; reasoning ->
    # AssistantMessage[ThinkingBlock]; agent_message -> AssistantMessage
    # [TextBlock]; turn.completed -> thinking_tokens SystemMessage +
    # ResultMessage.
    assert kinds == [
        "SystemMessage",
        "SystemMessage",
        "AssistantMessage",
        "AssistantMessage",
        "SystemMessage",
        "ResultMessage",
    ]
    text_msg = messages[3]
    assert isinstance(text_msg.content[0], TextBlock)
    assert text_msg.content[0].text == "CODEX_OK"

    result = messages[-1]
    assert isinstance(result, ResultMessage)
    assert result.subtype == "success"
    assert result.is_error is False
    assert result.num_turns == 1
    assert result.session_id == "01a101c5-0000"
    assert result.result == "CODEX_OK"
    assert result.total_cost_usd is None  # codex reports no cost
    assert result.usage == {
        "input_tokens": 4775,
        "output_tokens": 24,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 64,
        "cached_input_tokens": 64,
        "cache_write_input_tokens": 0,
        "reasoning_output_tokens": 19,
    }
    assert messages[4].subtype == "thinking_tokens"
    assert messages[4].data["estimated_tokens"] == 19


def test_translator_missing_usage_fields_are_zero():
    translator = lcx.CodexEventTranslator(model_label="glm-5.3")
    messages = translator.translate({"type": "turn.completed", "usage": {}})
    result = messages[-1]
    assert result.usage["input_tokens"] == 0
    assert result.usage["output_tokens"] == 0
    assert result.usage["cache_creation_input_tokens"] == 0
    assert result.usage["cache_read_input_tokens"] == 0
    assert result.usage["reasoning_output_tokens"] == 0


def test_translator_unknown_events_are_ignored_not_fatal():
    translator = lcx.CodexEventTranslator(model_label="glm-5.3")
    assert translator.translate({"type": "future.event"}) == []
    assert translator.translate({"type": "item.started", "item": {}}) == []
    assert translator.translate({"type": "item.updated", "item": {}}) == []
    assert translator.fatal_error is None
    assert translator.unknown_event_types == {"future.event": 1}


def test_translator_command_execution_maps_to_tool_result():
    translator = lcx.CodexEventTranslator(model_label="glm-5.3")
    messages = translator.translate(
        {
            "type": "item.completed",
            "item": {
                "id": "cmd_0",
                "type": "command_execution",
                "command": "rg -n foo sever/",
                "exit_code": 0,
            },
        }
    )
    assert len(messages) == 1
    block = messages[0].content[0]
    payload = json.loads(block.content)
    assert payload["type"] == "command_execution"
    assert payload["command"] == "rg -n foo sever/"
    assert block.is_error is False


# --- benign item error (model-metadata fallback) vs real error items --------


BENIGN_METADATA_ITEM = {
    "type": "item.completed",
    "item": {
        "id": "item_0",
        "type": "error",
        "message": (
            "Model metadata for glm-5.2 not found. Defaulting to fallback "
            "metadata; this can degrade performance and cause issues."
        ),
    },
}


# --- non-fatal "Reconnecting..." stream errors (codex retries on its own) ----


RECONNECT_1302 = (
    "Reconnecting... 1/5 (stream disconnected before completion: "
    "您的账户已达到速率限制，请您控制请求频率)"
)


def test_translator_reconnecting_error_is_nonfatal():
    """codex retries on its own after a Reconnecting stream error; the adapter
    must not rob it of those retries by marking the stream fatal."""

    translator = lcx.CodexEventTranslator(model_label="glm-5.3")
    messages = translator.translate({"type": "error", "message": RECONNECT_1302})
    assert translator.fatal_error is None
    assert translator.warnings == [RECONNECT_1302]
    assert translator.last_stream_error == RECONNECT_1302
    assert [type(m).__name__ for m in messages] == ["SystemMessage"]
    assert messages[0].subtype == "codex_reconnecting"
    # A duplicate is recorded/surfaced only once.
    assert translator.translate({"type": "error", "message": RECONNECT_1302}) == []
    assert len(translator.warnings) == 1
    # A later attempt number is a distinct warning and updates the last error.
    second = RECONNECT_1302.replace("1/5", "2/5")
    translator.translate({"type": "error", "message": second})
    assert len(translator.warnings) == 2
    assert translator.last_stream_error == second
    # A NON-reconnecting stream error is still fatal.
    assert (
        translator.translate({"type": "error", "message": GLM_1302_BODY}) == []
    )
    assert translator.fatal_error is not None


def test_codex_query_reconnecting_then_turn_completed_succeeds():
    """Real-smoke regression: a Reconnecting error followed by codex's own
    recovery must complete normally with the agent text."""

    events = [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "turn.started"},
        {"type": "error", "message": RECONNECT_1302},
        {
            "type": "item.completed",
            "item": {"id": "item_1", "type": "agent_message", "text": "CODEX_OK"},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 10, "output_tokens": 2},
        },
    ]
    transport = _FakeTransport([json.dumps(e) for e in events])
    messages = _run_query(transport)  # must NOT raise
    texts = [
        block.text
        for m in messages
        if isinstance(m, AssistantMessage)
        for block in m.content
        if isinstance(block, TextBlock)
    ]
    assert texts == ["CODEX_OK"]
    subtypes = [m.subtype for m in messages if isinstance(m, SystemMessage)]
    assert "codex_reconnecting" in subtypes
    results = [m for m in messages if isinstance(m, ResultMessage)]
    assert len(results) == 1
    assert results[0].subtype == "success"


def test_codex_query_reconnecting_then_dead_stream_keeps_rate_limit_semantics():
    """Real-smoke regression: when the reconnects never recover, the terminal
    error must still carry the original 1302 body so classify_llm_availability
    keeps rating it as a short service_unavailable cooldown (not 5h quota)."""

    dead = RECONNECT_1302.replace("1/5", "5/5")
    events = [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "turn.started"},
        {"type": "error", "message": RECONNECT_1302},
        {"type": "error", "message": dead},
    ]
    transport = _FakeTransport([json.dumps(e) for e in events], exit_code=0)
    with pytest.raises(ClaudeSDKError) as excinfo:
        _run_query(transport)
    text = str(excinfo.value)
    assert "stream ended without a completed turn" in text
    assert dead in text
    # The existing 1302 classification chain still reads the semantics.
    assert llm_query._is_quota_exceeded(text) is False
    assert llm_query._is_rate_limited(text) is True
    issue = classify_llm_availability(exception=excinfo.value)
    assert issue is not None and issue.category == SERVICE_UNAVAILABLE


def test_codex_query_agent_text_without_turn_completed_synthesizes_result():
    """agent_message arrived but turn.completed never did: not fatal — the
    terminal ResultMessage is synthesized with unknown usage (zeros) so the
    one-ResultMessage stream contract still holds."""

    events = [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "turn.started"},
        {"type": "error", "message": RECONNECT_1302},
        {
            "type": "item.completed",
            "item": {"id": "item_1", "type": "agent_message", "text": "CODEX_OK"},
        },
    ]
    transport = _FakeTransport([json.dumps(e) for e in events], exit_code=0)
    messages = _run_query(transport)  # must NOT raise
    texts = [
        block.text
        for m in messages
        if isinstance(m, AssistantMessage)
        for block in m.content
        if isinstance(block, TextBlock)
    ]
    assert texts == ["CODEX_OK"]
    results = [m for m in messages if isinstance(m, ResultMessage)]
    assert len(results) == 1
    assert results[0].subtype == "success"
    assert results[0].usage["input_tokens"] == 0
    assert results[0].usage["output_tokens"] == 0
    assert results[0].result == "CODEX_OK"


def test_translator_benign_model_metadata_item_is_warning_not_fatal():
    """Real-smoke regression: codex emits this benign item error for any
    non-built-in model name and keeps executing; it must not set fatal_error."""

    translator = lcx.CodexEventTranslator(model_label="glm-5.2")
    messages = translator.translate(BENIGN_METADATA_ITEM)
    assert translator.fatal_error is None
    assert len(translator.warnings) == 1
    assert "Model metadata" in translator.warnings[0]
    # Surfaced once as non-substantive bookkeeping, not as a fatal error.
    assert [type(m).__name__ for m in messages] == ["SystemMessage"]
    assert messages[0].subtype == "codex_item_warning"
    assert messages[0].data["message"] == translator.warnings[0]
    # A duplicate benign warning is recorded/surfaced only once.
    again = translator.translate(BENIGN_METADATA_ITEM)
    assert len(translator.warnings) == 1
    assert again == []


def test_translator_real_error_item_is_still_fatal():
    translator = lcx.CodexEventTranslator(model_label="glm-5.3")
    messages = translator.translate(
        {
            "type": "item.completed",
            "item": {"id": "item_0", "type": "error", "message": GLM_1308_BODY},
        }
    )
    assert messages == []
    assert translator.fatal_error is not None
    assert GLM_1308_BODY in translator.fatal_error
    assert translator.warnings == []


def test_codex_query_stream_survives_benign_metadata_item():
    """End-to-end regression: the benign metadata item arrives BEFORE the real
    answer (the exact real-smoke ordering); the stream must continue, deliver
    the agent text, and still terminate with a success ResultMessage."""

    events = [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "turn.started"},
        BENIGN_METADATA_ITEM,
        {
            "type": "item.completed",
            "item": {"id": "item_1", "type": "agent_message", "text": "CODEX_OK"},
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 10, "output_tokens": 2},
        },
    ]
    transport = _FakeTransport([json.dumps(e) for e in events])
    messages = _run_query(transport)  # must NOT raise
    texts = [
        block.text
        for m in messages
        if isinstance(m, AssistantMessage)
        for block in m.content
        if isinstance(block, TextBlock)
    ]
    assert texts == ["CODEX_OK"]
    subtypes = [m.subtype for m in messages if isinstance(m, SystemMessage)]
    assert "codex_item_warning" in subtypes
    results = [m for m in messages if isinstance(m, ResultMessage)]
    assert len(results) == 1
    assert results[0].subtype == "success"
    assert results[0].usage["input_tokens"] == 10


class _FakeStdout:
    def __init__(self, lines):
        self._lines = [line.encode("utf-8") + b"\n" for line in lines]

    async def readline(self):
        if not self._lines:
            return b""
        return self._lines.pop(0)

    async def readuntil(self, separator):
        if not self._lines:
            raise asyncio.IncompleteReadError(b"", None)
        return self._lines.pop(0)


class _FakeStdin:
    def write(self, data):
        pass

    async def drain(self):
        return None

    def close(self):
        pass

    async def wait_closed(self):
        return None


class _FakeStderr:
    async def readline(self):
        return b""

    async def readuntil(self, separator):
        raise asyncio.IncompleteReadError(b"", None)


class _FakeProcess:
    def __init__(self, stdout_lines, *, exit_code=0):
        self.stdout = _FakeStdout(stdout_lines)
        self.stderr = _FakeStderr()
        self.stdin = _FakeStdin()
        self.pid = 424242
        self.returncode = None
        self._exit_code = exit_code

    async def wait(self):
        if self.returncode is None:
            self.returncode = self._exit_code
        return self.returncode


class _FakeTransport(lcx.CodexExecTransport):
    """Scriptable transport: spawn returns a fake process; group signals are
    recorded instead of touching the real process table."""

    def __init__(self, stdout_lines, *, exit_code=0):
        super().__init__("", None)
        self._stdout_lines = stdout_lines
        self._exit_code = exit_code
        self.signals = []

    async def spawn(self):
        self._process = _FakeProcess(self._stdout_lines, exit_code=self._exit_code)
        return self._process

    async def close(self):
        process = self._process
        if process is None or process.returncode is not None:
            return True
        self.signals.append("SIGTERM")
        process.returncode = self._exit_code
        return True


def _run_query(transport):
    async def _consume():
        messages = []
        async for message in lcx.codex_query(
            prompt="p", options=None, transport=transport
        ):
            messages.append(message)
        return messages

    return asyncio_run(_consume())


def test_codex_query_end_to_end_messages():
    transport = _FakeTransport([json.dumps(e) for e in _smoke_events()])
    messages = _run_query(transport)
    kinds = [type(m).__name__ for m in messages]
    assert kinds == [
        "SystemMessage",
        "SystemMessage",
        "AssistantMessage",
        "AssistantMessage",
        "SystemMessage",
        "ResultMessage",
    ]
    # Clean one-shot exit already confirmed.
    assert transport._process.returncode == 0


def test_full_retry_loop_records_codex_metrics_schema(monkeypatch, tmp_path):
    """Run the REAL _process_stream over a scripted codex transport through
    the retry loop; capture the exact record_llm_call_metrics kwargs and prove
    the persisted JSONL record keeps every schema key."""

    monkeypatch.setenv("POK_LLM_TRANSPORT", "codex")
    captured = {}

    import llm_call_metrics

    real_record = llm_call_metrics.record_llm_call_metrics

    def fake_record(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(llm_call_metrics, "record_llm_call_metrics", fake_record)

    monkeypatch.setattr(llm_query, "_append_role_io", lambda *a, **k: None)
    monkeypatch.setattr(llm_query, "_emit_llm_event", lambda *a, **k: None)
    # NOTE: deliberately NOT monkeypatching asyncio.sleep here — the real
    # _process_stream runs the silence watchdog on it; a noop sleep turns
    # that loop into a busy starve (the rc1 tests patch sleep only because
    # their _process_stream is fake).

    def fake_new_transport(full_prompt, options):
        return _FakeTransport([json.dumps(e) for e in _smoke_events()])

    monkeypatch.setattr(
        llm_query_codex_module(), "new_codex_exec_transport", fake_new_transport
    )

    texts, cost_usd, usage = asyncio_run(
        llm_query._run_stream_with_signature_retry(
            "prompt", ClaudeAgentOptions(), "/tmp/none.log", _NullUI(), "role"
        )
    )
    assert texts == ["CODEX_OK"]
    assert cost_usd == 0.0  # codex reports no cost; billing keeps the schema
    assert usage["input_tokens"] == 4775
    assert usage["output_tokens"] == 24
    assert usage["cache_read_input_tokens"] == 64

    # Metrics contract: the codex JSONL usage reached the exact schema call.
    assert captured["role"] == "role"
    assert captured["input_tokens"] == 4775
    assert captured["output_tokens"] == 24
    assert captured["cache_creation_input_tokens"] == 0
    assert captured["cache_read_input_tokens"] == 64
    assert captured["thinking_tokens_estimated"] == 19
    assert captured["success"] is True
    assert captured["model"] == "codex"
    assert captured["api_error_status"] is None
    assert captured["stop_reason"] is None

    # The persisted record keeps every schema key (null/0 when absent).
    metrics_path = tmp_path / "llm_call_metrics.jsonl"
    monkeypatch.setattr(llm_call_metrics, "_metrics_file", lambda: metrics_path)
    real_record(
        call_id=captured.get("call_id"),
        attempt=captured.get("attempt"),
        max_attempts=captured.get("max_attempts"),
        role="role",
        model="codex",
        total_elapsed_sec=captured.get("total_elapsed_sec") or 1.0,
        first_token_latency_sec=captured.get("first_token_latency_sec"),
        first_text_latency_sec=captured.get("first_text_latency_sec"),
        stream_active_sec=captured.get("stream_active_sec"),
        input_tokens=captured["input_tokens"],
        output_tokens=captured["output_tokens"],
        cache_creation_input_tokens=captured["cache_creation_input_tokens"],
        cache_read_input_tokens=captured["cache_read_input_tokens"],
        thinking_tokens_estimated=captured["thinking_tokens_estimated"],
        thinking_tokens_delta_total=captured.get("thinking_tokens_delta_total"),
        cost_usd=captured.get("cost_usd"),
        success=captured.get("success"),
        api_error_status=captured.get("api_error_status"),
        stop_reason=captured.get("stop_reason"),
        num_turns=captured.get("num_turns"),
        sdk_subtype=captured.get("sdk_subtype"),
        sdk_duration_ms=captured.get("sdk_duration_ms"),
        sdk_duration_api_ms=captured.get("sdk_duration_api_ms"),
        terminal_reason=captured.get("terminal_reason"),
        sdk_session_id=captured.get("sdk_session_id"),
        sdk_uuid=captured.get("sdk_uuid"),
        sdk_result_text=captured.get("sdk_result_text"),
        model_usage=captured.get("model_usage"),
        raw_usage=usage,
        effort=captured.get("effort"),
        thinking_budget=captured.get("thinking_budget"),
        thinking_mode=captured.get("thinking_mode"),
        global_concurrency=captured.get("global_concurrency"),
        prompt_chars=captured.get("prompt_chars"),
        output_chars=captured.get("output_chars"),
        text_block_count=captured.get("text_block_count"),
        thinking_chars=captured.get("thinking_chars"),
        tool_use_count=captured.get("tool_use_count"),
        tool_result_count=captured.get("tool_result_count"),
        message_count=captured.get("message_count"),
        assistant_message_count=captured.get("assistant_message_count"),
        log_file=captured.get("log_file"),
        timeout_kind=captured.get("timeout_kind"),
    )
    record = json.loads(metrics_path.read_text(encoding="utf-8").splitlines()[-1])
    for key in (
        "schema_version",
        "ts",
        "epoch_ts",
        "call_id",
        "attempt",
        "max_attempts",
        "role",
        "model",
        "total_elapsed_sec",
        "first_token_latency_sec",
        "first_text_latency_sec",
        "semaphore_wait_sec",
        "stream_active_sec",
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "cache_hit_rate",
        "thinking_tokens_estimated",
        "thinking_tokens_delta_total",
        "total_tokens",
        "output_tokens_per_sec",
        "total_tokens_per_sec",
        "cost_usd",
        "success",
        "error_type",
        "error_message",
        "api_error_status",
        "stop_reason",
        "num_turns",
        "sdk_subtype",
        "terminal_reason",
        "timeout_kind",
        "sdk_duration_ms",
        "sdk_duration_api_ms",
        "sdk_session_id",
        "sdk_uuid",
        "sdk_result_text",
        "model_usage",
        "raw_usage",
        "effort",
        "thinking_budget",
        "thinking_mode",
        "global_concurrency",
        "prompt_chars",
        "output_chars",
        "text_block_count",
        "thinking_chars",
        "tool_use_count",
        "tool_result_count",
        "message_count",
        "assistant_message_count",
        "invocation_id",
        "generation_id",
        "log_file",
    ):
        assert key in record, f"metrics record missing key {key}"
    assert record["epoch_ts"] is not None
    assert record["input_tokens"] == 4775
    assert record["raw_usage"]["reasoning_output_tokens"] == 19


# ---------------------------------------------------------------------------
# Contract 3: provider error text -> 1302/1308 classification chain
# ---------------------------------------------------------------------------

GLM_1308_BODY = (
    "Request rejected (429) · [1308][已达到 5 小时的使用上限。"
    "您的限额将在 2026-10-03T20:00:00 重置。]"
)
GLM_1302_BODY = (
    "Request rejected (429) · [1302][您的账户已达到速率限制，请您控制请求频率]"
)


def test_codex_turn_failed_1308_raises_classifiable_quota_error():
    transport = _FakeTransport(
        [
            json.dumps({"type": "thread.started", "thread_id": "t"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "turn.failed", "error": {"message": GLM_1308_BODY}}),
        ],
        exit_code=1,
    )
    with pytest.raises(ClaudeSDKError) as excinfo:
        _run_query(transport)
    text = str(excinfo.value)
    assert GLM_1308_BODY in text
    # The existing classifier chain parses it as quota exhaustion…
    assert llm_query._is_quota_exceeded(text) is True
    issue = classify_llm_availability(exception=excinfo.value)
    assert issue is not None and issue.category == QUOTA_429


def test_codex_error_event_1302_raises_classifiable_rate_limit_error():
    transport = _FakeTransport(
        [json.dumps({"type": "error", "message": GLM_1302_BODY})],
        exit_code=1,
    )
    with pytest.raises(ClaudeSDKError) as excinfo:
        _run_query(transport)
    text = str(excinfo.value)
    assert GLM_1302_BODY in text
    # …and 1302/bare-429 must NOT arm the five-hour quota pause.
    assert llm_query._is_quota_exceeded(text) is False
    assert llm_query._is_rate_limited(text) is True
    issue = classify_llm_availability(exception=excinfo.value)
    assert issue is not None and issue.category == SERVICE_UNAVAILABLE


def test_codex_nonzero_exit_folds_stderr_tail_into_error_text():
    transport = _FakeTransport(
        [json.dumps({"type": "thread.started", "thread_id": "t"})],
        exit_code=2,
    )
    pending_stderr = [f"provider: {GLM_1308_BODY}"]

    class _StderrWithBody(_FakeStderr):
        async def readuntil(self, separator):
            if pending_stderr:
                return (pending_stderr.pop(0) + "\n").encode()
            raise asyncio.IncompleteReadError(b"", None)

    real_spawn = transport.spawn

    async def spawn_with_stderr():
        process = await real_spawn()
        process.stderr = _StderrWithBody()
        return process

    transport.spawn = spawn_with_stderr
    with pytest.raises(ClaudeSDKError) as excinfo:
        _run_query(transport)
    assert "exited with code 2" in str(excinfo.value)
    assert GLM_1308_BODY in str(excinfo.value)
    assert llm_query._is_quota_exceeded(str(excinfo.value)) is True


def test_codex_stream_through_process_stream_raises_availability_block():
    """The real _process_stream availability trace converts a codex provider
    failure into LLMAvailabilityBlocked (quota pause chain entry point)."""

    from llm_availability import LLMAvailabilityBlocked

    async def run():
        transport = _FakeTransport(
            [
                json.dumps({"type": "thread.started", "thread_id": "t"}),
                json.dumps({"type": "turn.started"}),
                json.dumps(
                    {"type": "turn.failed", "error": {"message": GLM_1308_BODY}}
                ),
            ],
            exit_code=1,
        )
        gen = lcx.codex_query(prompt="p", options=None, transport=transport)
        return await llm_query._process_stream(
            gen, "/tmp/none.log", _NullUI(), "role"
        )

    with pytest.raises(LLMAvailabilityBlocked) as excinfo:
        asyncio_run(run())
    assert excinfo.value.issue.category == QUOTA_429


# ---------------------------------------------------------------------------
# Contract 4: timeout/cancel kills the process tree and proves exit
# ---------------------------------------------------------------------------


def test_cancel_kills_process_tree_and_confirms_exit():
    """A hung codex stream (no events) cancelled mid-read must terminate the
    process group and leave returncode set — the provider-attempt exit
    confirmation predicate then resolves."""

    class _HangingStdout:
        async def readline(self):
            await asyncio.Event().wait()  # never resolves
            return b""  # pragma: no cover

        async def readuntil(self, separator):
            await asyncio.Event().wait()  # never resolves
            return b""  # pragma: no cover

    transport = _FakeTransport([])

    async def spawn_hanging():
        process = _FakeProcess([])
        process.stdout = _HangingStdout()
        transport._process = process
        return process

    transport.spawn = spawn_hanging

    async def _consume_all(gen):
        async for _message in gen:
            pass

    async def run():
        gen = lcx.codex_query(prompt="p", options=None, transport=transport)
        task = asyncio.create_task(_consume_all(gen))
        await asyncio.sleep(0.05)
        assert not task.done()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio_run(run())
    # The generator finalizer closed the transport: group signalled and the
    # one-shot process exit is proven via returncode.
    assert transport.signals == ["SIGTERM"]
    assert transport._process.returncode is not None

    # The shared provider-attempt exit predicate accepts this transport.
    import llm_provider_attempt as _pa

    attempt = _pa._new_provider_attempt(transport)
    attempt["transport_close_attempted"] = True
    assert _pa._resolve_provider_attempt_if_stopped(attempt) is True


def test_real_transport_close_signals_process_group(monkeypatch):
    """CodexExecTransport.close() itself escalates SIGTERM on the spawned
    process group (start_new_session=True makes pgid == pid)."""

    transport = lcx.CodexExecTransport("p", None)
    signaled = []
    waiting = asyncio.Event()

    class _Proc:
        pid = 999999
        returncode = None

        async def wait(self):
            await waiting.wait()
            self.returncode = -15
            return self.returncode

    proc = _Proc()
    transport._process = proc

    monkeypatch.setattr(
        lcx, "_signal_process_group", lambda process, sig: signaled.append(sig.name)
    )

    async def run():
        task = asyncio.create_task(transport.close())
        await asyncio.sleep(0.05)
        assert signaled == ["SIGTERM"]
        waiting.set()
        return await task

    assert asyncio_run(run()) is True
    assert proc.returncode == -15
    assert signaled == ["SIGTERM"]


# ---------------------------------------------------------------------------
# Effort mapping + argv shape (contract 8 / transport config)
# ---------------------------------------------------------------------------


def test_effort_mapping_official_levels(monkeypatch):
    monkeypatch.setenv("POK_LLM_EFFORT", "max")
    assert lcx.codex_effort_from_env() == "max"
    monkeypatch.setenv("POK_LLM_EFFORT", "high")
    assert lcx.codex_effort_from_env() == "high"
    monkeypatch.setenv("POK_LLM_EFFORT", "low")
    assert lcx.codex_effort_from_env() == "low"
    # Unknown values fail closed to the lightest tier; unset defaults to max.
    monkeypatch.setenv("POK_LLM_EFFORT", "medium")
    assert lcx.codex_effort_from_env() == "low"
    monkeypatch.delenv("POK_LLM_EFFORT", raising=False)
    assert lcx.codex_effort_from_env() == "max"


def test_argv_shape_readonly_json_stdin(monkeypatch):
    monkeypatch.setenv("POK_LLM_EFFORT", "max")
    monkeypatch.delenv("POK_CODEX_MODEL", raising=False)
    argv = lcx.CodexExecTransport("p", None).build_argv()
    assert argv[0] == "codex"
    assert argv[1] == "exec"
    assert "--json" in argv
    assert "--ephemeral" in argv
    assert argv[argv.index("-s") + 1] == "read-only"
    assert argv[argv.index("-c") + 1] == "model_reasoning_effort=max"
    assert argv[-1] == "-"  # prompt via stdin

    monkeypatch.setenv("POK_CODEX_MODEL", "glm-5.3")
    argv = lcx.CodexExecTransport("p", None).build_argv()
    assert "model=glm-5.3" in argv
    assert lcx.codex_metrics_model() == "glm-5.3"


# ---------------------------------------------------------------------------
# Contract 5: binary pre-resolution (fail-fast, distinct from 1302/1308)
# ---------------------------------------------------------------------------


def test_resolve_codex_binary_which_lookup_and_absolute_override(monkeypatch):
    """Unset POK_CODEX_BIN searches PATH for a bare `codex`; an absolute
    override is checked directly (shutil.which honours the directory
    component), so the operator can pin the service install path."""

    monkeypatch.delenv("POK_CODEX_BIN", raising=False)
    seen = {}

    def fake_which(name):
        seen["name"] = name
        return "/opt/codex/bin/codex"

    monkeypatch.setattr(lcx.shutil, "which", fake_which)
    assert lcx.resolve_codex_binary() == "/opt/codex/bin/codex"
    assert seen["name"] == "codex"

    monkeypatch.setenv("POK_CODEX_BIN", "/home/ubuntu/.local/bin/codex")
    assert lcx.resolve_codex_binary() == "/opt/codex/bin/codex"
    assert seen["name"] == "/home/ubuntu/.local/bin/codex"


def _make_user_local_codex(base):
    """Create a fake user-local install under ``base``: .local/bin/codex
    executable.  Returns (home_dir, binary_path)."""

    bindir = base / ".local" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    binfile = bindir / "codex"
    binfile.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binfile.chmod(0o755)
    return base, binfile


def test_resolve_codex_binary_falls_back_to_user_local_bin(monkeypatch, tmp_path):
    """2026-10-04 service-env smoke regression: a BARE name absent from the
    service PATH resolves from the pip --user install base $HOME/.local/bin
    (proven executable via shutil.which) — the transport needs no
    operator-added env key."""

    import os as _os

    home, binfile = _make_user_local_codex(tmp_path / "home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    monkeypatch.delenv("POK_CODEX_BIN", raising=False)
    real_which = lcx.shutil.which

    def fake_which(name, *args, **kwargs):
        if not _os.path.dirname(str(name)):
            return None  # bare name is NOT on the service PATH
        return real_which(name, *args, **kwargs)

    monkeypatch.setattr(lcx.shutil, "which", fake_which)
    assert lcx.resolve_codex_binary() == str(binfile)
    # The startup preflight passes under exactly the smoke's env shape.
    monkeypatch.setenv("POK_LLM_TRANSPORT", "codex")
    assert lcx.assert_codex_transport_ready() == str(binfile)


def test_resolve_codex_binary_explicit_path_gets_no_fallback(monkeypatch, tmp_path):
    """A POK_CODEX_BIN carrying a directory component is authoritative: a
    nonexistent explicit path raises even though a valid user-local install
    exists under $HOME (no silent substitution of the operator's choice)."""

    import os as _os

    home, _binfile = _make_user_local_codex(tmp_path / "home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    monkeypatch.setenv("POK_CODEX_BIN", str(tmp_path / "missing" / "codex"))
    real_which = lcx.shutil.which

    def fake_which(name, *args, **kwargs):
        if not _os.path.dirname(str(name)):
            return None
        return real_which(name, *args, **kwargs)

    monkeypatch.setattr(lcx.shutil, "which", fake_which)
    with pytest.raises(lcx.CodexBinaryNotFound) as excinfo:
        lcx.resolve_codex_binary()
    # The guidance reports exactly the configured path, and NO fallback
    # candidate from the fake $HOME was attempted or echoed (str(home)
    # absent from the message; the only .local/bin mention is the literal
    # documented "$HOME/.local/bin" phrase).
    assert str(tmp_path / "missing" / "codex") in str(excinfo.value)
    assert str(home) not in str(excinfo.value)
    assert "$HOME/.local/bin" in str(excinfo.value)


def test_resolve_codex_binary_without_home_has_no_fallback(monkeypatch, tmp_path):
    """No HOME -> no user-local fallback attempted (nothing to resolve
    from); the actionable guidance still raises."""

    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("POK_CODEX_BIN", raising=False)
    monkeypatch.setenv("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    monkeypatch.setattr(lcx.shutil, "which", lambda _name: None)
    with pytest.raises(lcx.CodexBinaryNotFound) as excinfo:
        lcx.resolve_codex_binary()
    assert "codex binary not found" in str(excinfo.value)


def test_service_env_preflight_resolves_via_user_local_bin_real_which(
    monkeypatch, tmp_path
):
    """Direct replica of the failing 2026-10-04 smoke with the REAL
    shutil.which: env keys exactly HOME + committed service PATH +
    POK_LLM_TRANSPORT=codex (no POK_CODEX_BIN) — the preflight must now
    resolve the $HOME/.local/bin install instead of refusing to start."""

    home, binfile = _make_user_local_codex(tmp_path / "home")
    monkeypatch.setenv("HOME", str(home))
    # The committed service PATH from deploy/tencent-cloud/env.runtime:19.
    monkeypatch.setenv(
        "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    )
    monkeypatch.setenv("POK_LLM_TRANSPORT", "codex")
    monkeypatch.delenv("POK_CODEX_BIN", raising=False)
    # NOTE: no shutil.which monkeypatch — the real resolver runs, exactly as
    # in the service smoke.
    assert lcx.assert_codex_transport_ready() == str(binfile)


def test_spawn_missing_binary_raises_actionable_sdk_error(monkeypatch):
    """The exact 2026-10-03 production shape: service PATH without
    ~/.local/bin, POK_CODEX_BIN unset.  spawn() must fail with an
    actionable ClaudeSDKError (not a bare FileNotFoundError) whose text
    matches NO availability marker, so no cooldown/quota pause is armed."""

    monkeypatch.delenv("POK_CODEX_BIN", raising=False)
    monkeypatch.setenv("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    monkeypatch.setattr(lcx.shutil, "which", lambda _name: None)

    transport = lcx.CodexExecTransport("p", None)
    with pytest.raises(ClaudeSDKError) as excinfo:
        asyncio_run(transport.spawn())
    text = str(excinfo.value)

    # Actionable guidance, echoing the configured name and the searched PATH.
    assert "codex binary not found" in text
    assert "'codex'" in text
    assert "/usr/local/bin" in text
    assert "POK_CODEX_BIN=/home/ubuntu/.local/bin/codex" in text
    assert "deploy/tencent-cloud/env.runtime" in text
    # The guidance documents the bare-name $HOME/.local/bin fallback.
    assert "$HOME/.local/bin" in text

    # Spawn-class failure stays distinct from provider availability: it is
    # never quota (1308), never a rate limit (1302), and never a durable
    # availability pause — classify returns None.
    assert llm_query._is_quota_exceeded(text) is False
    assert llm_query._is_rate_limited(text) is False
    issue = classify_llm_availability(exception=excinfo.value)
    assert issue is None

    # No process was created; the transport stays closeable (exit confirmed).
    assert transport._process is None
    assert asyncio_run(transport.close()) is True


def test_spawn_execs_resolved_absolute_binary(monkeypatch):
    """spawn() execs the RESOLVED absolute path as argv[0] (independent of
    the child PATH), keeping the rest of the argv contract unchanged."""

    monkeypatch.delenv("POK_CODEX_BIN", raising=False)
    monkeypatch.setenv("POK_LLM_EFFORT", "max")
    monkeypatch.delenv("POK_CODEX_MODEL", raising=False)
    monkeypatch.setattr(lcx.shutil, "which", lambda _name: "/opt/codex/bin/codex")

    captured = {}

    class _Proc:
        pid = 424243
        returncode = None

    async def fake_exec(*argv, **kwargs):
        captured["argv"] = list(argv)
        captured["start_new_session"] = kwargs.get("start_new_session")
        return _Proc()

    monkeypatch.setattr(lcx.asyncio, "create_subprocess_exec", fake_exec)

    transport = lcx.CodexExecTransport("p", None)
    proc = asyncio_run(transport.spawn())
    assert captured["argv"][0] == "/opt/codex/bin/codex"
    assert captured["argv"][1] == "exec"
    assert "--json" in captured["argv"]
    assert captured["argv"][-1] == "-"
    assert captured["start_new_session"] is True
    assert transport._argv[0] == "/opt/codex/bin/codex"
    assert proc is transport._process


def test_codex_spawn_failure_through_process_stream_is_not_a_pause(monkeypatch):
    """End-to-end through the REAL _process_stream: the spawn failure
    propagates as ClaudeSDKError (not LLMAvailabilityBlocked), so the
    saturator/orchestrator cooldown and quota-pause chains never engage."""

    from llm_availability import LLMAvailabilityBlocked

    monkeypatch.delenv("POK_CODEX_BIN", raising=False)
    monkeypatch.setattr(lcx.shutil, "which", lambda _name: None)

    async def run():
        gen = lcx.codex_query(prompt="p", options=None)
        return await llm_query._process_stream(
            gen, "/tmp/none.log", _NullUI(), "role"
        )

    with pytest.raises(ClaudeSDKError) as excinfo:
        asyncio_run(run())
    assert "codex binary not found" in str(excinfo.value)
    assert classify_llm_availability(exception=excinfo.value) is None
    # Guard the import-time assumption used above (LLMAvailabilityBlocked is
    # the pause entry point; it must NOT be the raised type here).
    assert not isinstance(excinfo.value, LLMAvailabilityBlocked)


def test_assert_codex_transport_ready_preflight_gates_on_transport(monkeypatch):
    """The lifespan preflight is a no-op on the default claude transport
    (even with a broken PATH) and resolves/raises only under codex."""

    # claude transport: never touches the binary.
    monkeypatch.delenv("POK_LLM_TRANSPORT", raising=False)
    monkeypatch.setattr(lcx.shutil, "which", lambda _name: None)
    assert lcx.assert_codex_transport_ready() is None

    # codex transport + resolvable binary -> resolved absolute path.
    monkeypatch.setenv("POK_LLM_TRANSPORT", "codex")
    monkeypatch.setattr(lcx.shutil, "which", lambda _name: "/opt/codex/bin/codex")
    assert lcx.assert_codex_transport_ready() == "/opt/codex/bin/codex"

    # codex transport + unresolvable binary -> actionable startup failure.
    # The preflight raises the SDK-free CodexBinaryNotFound (deterministic
    # under ANY interpreter); the dispatch path (spawn) is what re-raises
    # the same text as ClaudeSDKError.
    monkeypatch.setattr(lcx.shutil, "which", lambda _name: None)
    with pytest.raises(lcx.CodexBinaryNotFound) as excinfo:
        lcx.assert_codex_transport_ready()
    assert "codex binary not found" in str(excinfo.value)
    assert "POK_CODEX_BIN" in str(excinfo.value)


def test_web_lifespan_fails_fast_when_codex_binary_unresolvable(monkeypatch):
    """The web lifespan itself must refuse to start (fail-fast) when
    POK_LLM_TRANSPORT=codex is configured but the binary cannot be resolved
    — the startup-diagnosable contract asked for after the v490-v492
    incident, instead of burning generations on per-role spawn errors."""

    import server.app as app_module

    async def noop(*_args, **_kwargs):
        return None

    def _deny(_label):
        raise RuntimeError("epoch not initialized")

    import epoch_authority

    monkeypatch.setenv("POK_LLM_TRANSPORT", "codex")
    monkeypatch.delenv("POK_WEB_VIEW_ONLY", raising=False)
    monkeypatch.delenv("POK_CODEX_BIN", raising=False)
    monkeypatch.setattr(lcx.shutil, "which", lambda _name: None)
    monkeypatch.setattr(epoch_authority, "require_policy_epoch_initialized", _deny)
    monkeypatch.setattr(app_module, "configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr(app_module.arena_manager, "startup", noop)
    monkeypatch.setattr(app_module.arena_manager, "shutdown", noop)

    async def exercise():
        async with app_module.lifespan(app_module.app):
            raise AssertionError("lifespan must not start under a broken codex transport")

    with pytest.raises(lcx.CodexBinaryNotFound) as excinfo:
        asyncio_run(exercise())
    assert "codex binary not found" in str(excinfo.value)
    assert "POK_CODEX_BIN" in str(excinfo.value)


def test_web_lifespan_allows_resolvable_codex_binary(monkeypatch):
    """Mirror: with the binary resolvable the preflight passes and the
    lifespan proceeds (reset_required keeps the runtime stopped, exactly
    like the claude path)."""

    import server.app as app_module
    import epoch_authority
    from server.state import app_state

    async def noop(*_args, **_kwargs):
        return None

    def _deny(_label):
        raise RuntimeError("epoch not initialized")

    monkeypatch.setenv("POK_LLM_TRANSPORT", "codex")
    monkeypatch.delenv("POK_WEB_VIEW_ONLY", raising=False)
    monkeypatch.delenv("POK_CODEX_BIN", raising=False)
    monkeypatch.setattr(lcx.shutil, "which", lambda _name: "/opt/codex/bin/codex")
    monkeypatch.setattr(epoch_authority, "require_policy_epoch_initialized", _deny)
    monkeypatch.setattr(app_module, "configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr(app_module.arena_manager, "startup", noop)
    monkeypatch.setattr(app_module.arena_manager, "shutdown", noop)
    app_state.stop_running()

    async def exercise():
        async with app_module.lifespan(app_module.app):
            assert app_state.to_dict()["running"] is False

    asyncio_run(exercise())


# ---------------------------------------------------------------------------
# Contract 6: SDK-free preflight (any python3: smoke / ExecStartPre)
# ---------------------------------------------------------------------------


def _run_sdkfree_python(code: str, tmp_path, *, path, extra_env=None):
    """Run ``code`` under an interpreter whose claude_agent_sdk import FAILS.

    A stub package on PYTHONPATH raises ImportError on import — the exact
    shape of the 2026-10-04 service-environment smoke, whose ``python3``
    resolves from the service PATH and therefore does not see the venv's
    site-packages.  ``path`` becomes the child PATH (hermetic: no host
    codex install can leak into the assertion).
    """
    import subprocess
    import sys

    stub_dir = tmp_path / "sdk_stub"
    stub_dir.mkdir(exist_ok=True)
    (stub_dir / "claude_agent_sdk.py").write_text(
        "raise ImportError('simulated missing claude_agent_sdk (smoke interpreter)')\n",
        encoding="utf-8",
    )
    env = {
        "HOME": str(tmp_path),
        "PATH": str(path),
        "PYTHONPATH": str(stub_dir),
    }
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _core_dir() -> str:
    from pathlib import Path

    return str(Path(__file__).resolve().parents[1] / "core")


def test_preflight_importable_and_actionable_without_sdk(tmp_path):
    """The exact 2026-10-04 smoke regression: SDK-free interpreter, service
    PATH without the install directory, POK_LLM_TRANSPORT=codex.  The module
    must IMPORT (not die on claude_agent_sdk) and the preflight must raise
    the actionable CodexBinaryNotFound guidance."""

    empty_bin = tmp_path / "svcpath"
    empty_bin.mkdir()
    code = f"""
import sys
sys.path.insert(0, {_core_dir()!r})
import llm_query_codex as lcx
assert lcx._SDK_IMPORT_ERROR is not None, "stub must block claude_agent_sdk"
try:
    lcx.assert_codex_transport_ready()
    raise SystemExit("FAIL: no error raised")
except lcx.CodexBinaryNotFound as exc:
    text = str(exc)
    assert "codex binary not found" in text, text
    assert "'codex'" in text, text
    assert "POK_CODEX_BIN=/home/ubuntu/.local/bin/codex" in text, text
    assert "deploy/tencent-cloud/env.runtime" in text, text
print("PREFLIGHT_ACTIONABLE_OK")
"""
    proc = _run_sdkfree_python(
        code, tmp_path, path=empty_bin, extra_env={"POK_LLM_TRANSPORT": "codex"}
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert "PREFLIGHT_ACTIONABLE_OK" in proc.stdout


def test_preflight_resolves_absolute_binary_without_sdk(tmp_path):
    """Mirror: with POK_CODEX_BIN pointing at a real executable the SDK-free
    preflight resolves and returns the absolute path (smoke ok:true)."""

    svc_bin = tmp_path / "svcbin"
    svc_bin.mkdir()
    codex_sh = svc_bin / "codex"
    codex_sh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    codex_sh.chmod(0o755)
    code = f"""
import sys
sys.path.insert(0, {_core_dir()!r})
import llm_query_codex as lcx
assert lcx._SDK_IMPORT_ERROR is not None
resolved = lcx.assert_codex_transport_ready()
assert resolved == {str(codex_sh)!r}, resolved
print("PREFLIGHT_RESOLVED_OK", resolved)
"""
    proc = _run_sdkfree_python(
        code,
        tmp_path,
        path=svc_bin,
        extra_env={
            "POK_LLM_TRANSPORT": "codex",
            "POK_CODEX_BIN": str(codex_sh),
        },
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert "PREFLIGHT_RESOLVED_OK" in proc.stdout


def test_sdkfree_service_env_smoke_resolves_via_user_local_bin(tmp_path):
    """The exact failing 2026-10-04 service-env smoke, SDK-free: env keys
    HOME + service PATH (no ~/.local/bin) + POK_LLM_TRANSPORT=codex, NO
    POK_CODEX_BIN.  The user-local install base under $HOME resolves and the
    preflight returns ok (this is the round that failed PATH-only)."""

    home_codex = tmp_path / ".local" / "bin" / "codex"
    home_codex.parent.mkdir(parents=True, exist_ok=True)
    home_codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    home_codex.chmod(0o755)
    svc_path = tmp_path / "svcpath"
    svc_path.mkdir()
    code = f"""
import sys
sys.path.insert(0, {_core_dir()!r})
import llm_query_codex as lcx
assert lcx._SDK_IMPORT_ERROR is not None
resolved = lcx.assert_codex_transport_ready()
assert resolved == {str(home_codex)!r}, resolved
print("SMOKE_USER_LOCAL_OK", resolved)
"""
    proc = _run_sdkfree_python(
        code, tmp_path, path=svc_path, extra_env={"POK_LLM_TRANSPORT": "codex"}
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert "SMOKE_USER_LOCAL_OK" in proc.stdout


def test_sdk_typed_entries_fail_closed_without_sdk(tmp_path):
    """Under an SDK-free interpreter the SDK-typed entry points fail closed
    with the POK_PYTHON guidance (never NameError from unbound SDK names)."""

    empty_bin = tmp_path / "svcpath"
    empty_bin.mkdir()
    code = f"""
import asyncio
import sys
sys.path.insert(0, {_core_dir()!r})
import llm_query_codex as lcx
assert lcx._SDK_IMPORT_ERROR is not None

try:
    lcx.CodexEventTranslator(model_label="m")
    raise SystemExit("FAIL: translator constructed without SDK")
except RuntimeError as exc:
    assert "claude_agent_sdk" in str(exc), exc
    assert "POK_PYTHON" in str(exc), exc

async def _consume():
    async for _message in lcx.codex_query(prompt="p"):
        pass

try:
    asyncio.run(_consume())
    raise SystemExit("FAIL: codex_query ran without SDK")
except RuntimeError as exc:
    assert "POK_PYTHON" in str(exc), exc
print("SDK_GUARD_OK")
"""
    proc = _run_sdkfree_python(
        code, tmp_path, path=empty_bin, extra_env={"POK_LLM_TRANSPORT": "codex"}
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert "SDK_GUARD_OK" in proc.stdout


def test_sdk_present_import_is_unaffected():
    """Under the real venv (claude_agent_sdk installed) the guarded import
    binds the SDK names exactly as before — no behavior change in-service."""

    assert lcx._SDK_IMPORT_ERROR is None
    assert lcx.ClaudeSDKError is not None


# ---------------------------------------------------------------------------
# Contract 7: over-long JSONL lines must not void the one-shot stream
# (2026-10-04 journal: four streams dead in 20 minutes on
#  "LimitOverrunError: Separator is found, but chunk is longer than limit")
# ---------------------------------------------------------------------------


def test_spawn_passes_stream_reader_limit(monkeypatch):
    """spawn() must raise the pipe StreamReader limit (asyncio default 64 KiB
    caps ONE JSONL line; a codex item.completed embedding a large diff exceeds
    it and kills the whole attempt)."""

    monkeypatch.delenv("POK_CODEX_BIN", raising=False)
    monkeypatch.setenv("POK_LLM_EFFORT", "max")
    monkeypatch.delenv("POK_CODEX_MODEL", raising=False)
    monkeypatch.setattr(lcx.shutil, "which", lambda _name: "/opt/codex/bin/codex")

    captured = {}

    class _Proc:
        pid = 424244
        returncode = None

    async def fake_exec(*argv, **kwargs):
        captured["limit"] = kwargs.get("limit")
        return _Proc()

    monkeypatch.setattr(lcx.asyncio, "create_subprocess_exec", fake_exec)

    asyncio_run(lcx.CodexExecTransport("p", None).spawn())
    assert captured["limit"] == lcx._STREAM_LINE_LIMIT
    assert captured["limit"] >= 8 * 1024 * 1024


def test_unbounded_line_reader_reassembles_lines_beyond_reader_limit():
    """The safe reader reassembles a line longer than the StreamReader limit
    out of bounded read() calls and keeps subsequent lines intact."""

    overlong_middle = b'{"type":"item.completed","text":"' + b"y" * 500 + b'"}\n'

    async def run():
        stream = asyncio.StreamReader(limit=200)
        stream.feed_data(b'{"type":"thread.started","thread_id":"t"}\n')
        stream.feed_data(overlong_middle)
        stream.feed_data(b'{"type":"turn.completed"}\n')
        stream.feed_eof()
        reader = lcx._UnboundedLineReader(stream)
        lines = []
        while True:
            line = await reader.readline()
            if not line:
                break
            lines.append(line)
        return lines

    lines = asyncio_run(run())
    assert lines == [
        b'{"type":"thread.started","thread_id":"t"}\n',
        overlong_middle,
        b'{"type":"turn.completed"}\n',
    ]


def test_unbounded_line_reader_survives_line_beyond_raised_limit():
    """Even a line beyond the raised 8 MiB _STREAM_LINE_LIMIT is reassembled
    instead of raising LimitOverrunError out of the read loop."""

    huge = b'{"type":"item.completed","text":"' + b"z" * (lcx._STREAM_LINE_LIMIT + 4096) + b'"}\n'

    async def run():
        stream = asyncio.StreamReader(limit=lcx._STREAM_LINE_LIMIT)
        stream.feed_data(b'{"type":"turn.started"}\n')
        stream.feed_data(huge)
        stream.feed_data(b'{"type":"turn.completed"}\n')
        stream.feed_eof()
        reader = lcx._UnboundedLineReader(stream)
        lines = []
        while True:
            line = await reader.readline()
            if not line:
                break
            lines.append(line)
        return lines

    lines = asyncio_run(run())
    assert lines == [b'{"type":"turn.started"}\n', huge, b'{"type":"turn.completed"}\n']


def test_codex_query_survives_overlong_jsonl_line(monkeypatch, tmp_path):
    """End-to-end through a REAL subprocess: a >64 KiB codex JSONL event line
    (embedded agent message) must translate normally instead of voiding the
    stream with LimitOverrunError (the exact 2026-10-04 journal failure)."""

    big_text = "x" * 300_000  # > 64 KiB asyncio StreamReader default limit
    events = [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"id": "item_1", "type": "agent_message", "text": big_text},
        },
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}},
    ]
    events_file = tmp_path / "events.jsonl"
    events_file.write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )
    script = tmp_path / "codex"
    script.write_text(
        "#!/bin/sh\ncat '%s'\n" % events_file, encoding="utf-8"
    )
    script.chmod(0o755)
    monkeypatch.setenv("POK_CODEX_BIN", str(script))

    async def _consume():
        messages = []
        async for message in lcx.codex_query(prompt="p", options=None):
            messages.append(message)
        return messages

    messages = asyncio_run(_consume())
    texts = [
        block.text
        for m in messages
        if isinstance(m, AssistantMessage)
        for block in m.content
        if isinstance(block, TextBlock)
    ]
    assert texts == [big_text]
    results = [m for m in messages if isinstance(m, ResultMessage)]
    assert len(results) == 1
    assert results[0].subtype == "success"
    assert results[0].result == big_text
