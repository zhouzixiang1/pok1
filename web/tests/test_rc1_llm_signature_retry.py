"""Test _run_stream_with_signature_retry helper in llm_query.

Verifies that the extracted helper retries on transient SDK signature errors
and re-raises immediately on non-signature SDK errors, so the 529/429 retry
paths inherit the same handling as the initial query.
"""

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKError

import llm_query


async def _noop_sleep(*_args, **_kwargs):
    return None


def _make_fake_generator():
    """A minimal fake async generator with a no-op aclose()."""
    async def _gen():
        if False:  # never yields; _process_stream is monkeypatched anyway
            yield

    gen = _gen()
    return gen


def test_retries_on_signature_error(monkeypatch):
    """Attempt 1 raises a signature ClaudeSDKError; attempt 2 returns normally.

    Asserts the helper returns the success result and that _process_stream was
    called exactly twice (one retry happened).
    """

    call_count = {"n": 0}

    async def fake_process_stream(query_gen, log_file_path, ui, role_name):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ClaudeSDKError("Missing required field in assistant message: signature")
        return (["ok"], 0.01, {}, {})

    def fake_claude_query(*_args, **_kwargs):
        return _make_fake_generator()

    monkeypatch.setattr(llm_query, "claude_query", fake_claude_query)
    monkeypatch.setattr(llm_query, "_process_stream", fake_process_stream)
    monkeypatch.setattr(llm_query.asyncio, "sleep", _noop_sleep)

    async def run():
        return await llm_query._run_stream_with_signature_retry(
            "prompt", ClaudeAgentOptions(), "/tmp/none.log", None, "role")

    texts, cost_usd, usage = asyncio_run(run())

    assert texts == ["ok"]
    assert cost_usd == 0.01
    assert usage == {}
    assert call_count["n"] == 2  # one initial failure + one success = 2 calls


def test_reraises_on_non_signature_error(monkeypatch):
    """A NON-signature ClaudeSDKError must propagate immediately without retry.

    Asserts the helper re-raises the original error and _process_stream was
    called only once.
    """

    call_count = {"n": 0}

    async def fake_process_stream(query_gen, log_file_path, ui, role_name):
        call_count["n"] += 1
        raise ClaudeSDKError("some unrelated hard SDK failure")

    def fake_claude_query(*_args, **_kwargs):
        return _make_fake_generator()

    monkeypatch.setattr(llm_query, "claude_query", fake_claude_query)
    monkeypatch.setattr(llm_query, "_process_stream", fake_process_stream)
    monkeypatch.setattr(llm_query.asyncio, "sleep", _noop_sleep)

    async def run():
        return await llm_query._run_stream_with_signature_retry(
            "prompt", ClaudeAgentOptions(), "/tmp/none.log", None, "role")

    with pytest.raises(ClaudeSDKError):
        asyncio_run(run())

    assert call_count["n"] == 1  # no retry on non-signature error


def test_signature_retries_share_one_total_wall_clock_deadline(monkeypatch):
    """Backoff and fresh SDK attempts cannot each reset total_timeout."""

    call_count = {"n": 0}

    async def fake_process_stream(query_gen, log_file_path, ui, role_name):
        del query_gen, log_file_path, ui, role_name
        call_count["n"] += 1
        await llm_query.asyncio.sleep(0.02)
        raise ClaudeSDKError(
            "Missing required field in assistant message: signature"
        )

    def fake_claude_query(*_args, **_kwargs):
        return _make_fake_generator()

    events = []
    monkeypatch.setattr(llm_query, "claude_query", fake_claude_query)
    monkeypatch.setattr(llm_query, "_process_stream", fake_process_stream)
    monkeypatch.setattr(
        llm_query,
        "_emit_llm_event",
        lambda category, severity, message, **fields: events.append(
            (category, severity, message, fields)
        ),
    )
    monkeypatch.setenv("POK_LLM_MASTER_TOTAL_TIMEOUT", "0.05")

    async def run():
        return await llm_query._run_stream_with_signature_retry(
            "prompt", ClaudeAgentOptions(), "/tmp/none.log", None, "MASTER"
        )

    with pytest.raises(llm_query.LLMRoleTimeout) as exc:
        asyncio_run(run())

    assert exc.value.timeout_kind == "total"
    assert call_count["n"] == 1
    total_event = next(
        event for event in events
        if event[0] == "pipeline.llm_role_total_timeout"
    )
    assert total_event[3]["retry_phase"] == "sdk_signature_backoff"


def asyncio_run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)
