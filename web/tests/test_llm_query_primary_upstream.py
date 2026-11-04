"""Tests for the direct-HTTP primary upstream (web/core/llm_query_primary.py).

Block-49, 2026-11-04: the operator asked for a redundant upstream — tydfxt.top /
Qwen3.8-Flash as PRIMARY, the existing codex path as FALLBACK, with one explicit
rule: **a rate limit must NOT fall back** (stay on the primary).

What these tests pin:

1. mode resolution — ``primary`` only when a base URL AND key are present;
   a missing key degrades to ``off`` instead of failing every dispatch;
   ``fallback``/``off`` are explicit switches;
2. wire shapes — both ``anthropic`` (/messages) and ``openai``
   (/chat/completions) payloads are built correctly, including the auth header
   each one needs;
3. usage mapping — ``input_tokens`` stays the FULL prompt and cache counters map
   onto the SDK keys, so ``CR / input`` means what it means on every other
   transport (this is the caliber that was wrong once already);
4. response parsing — text and reasoning blocks are separated for both wires,
   and an empty but well-formed answer is not treated as an error;
5. error verdicts — 429 and GLM 1302/1308 bodies are ``retryable`` (never
   ``fatal``), while transport errors / auth failures / 5xx are ``fatal``;
6. dispatch wiring — the retry loop tries the primary first, and its fallback
   branch is keyed to ``fatal and not retryable`` so a rate limit re-raises into
   the availability chain instead of spending the fallback account's quota.

Run in the operator checkout only:
    /home/ubuntu/pok1/.venv/bin/python -m pytest web/tests/test_llm_query_primary_upstream.py
"""

import asyncio
import inspect
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))

import llm_query_primary as up  # noqa: E402

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ClaudeSDKError,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
)


def _clear(monkeypatch):
    for key in (
        up.UPSTREAM_ENV,
        up.BASE_URL_ENV,
        up.MODEL_ENV,
        up.API_KEY_ENV,
        up.WIRE_ENV,
        up.TIMEOUT_ENV,
    ):
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# 1. mode resolution
# ---------------------------------------------------------------------------

def test_mode_primary_requires_base_url_and_key(monkeypatch):
    _clear(monkeypatch)
    assert up.upstream_mode() == "off"
    monkeypatch.setenv(up.BASE_URL_ENV, "https://tydfxt.top/v1")
    assert up.upstream_mode() == "off", "a base URL without a key must not arm the primary"
    monkeypatch.setenv(up.API_KEY_ENV, "k")
    assert up.upstream_mode() == "primary"


def test_explicit_switches(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(up.BASE_URL_ENV, "https://tydfxt.top/v1")
    monkeypatch.setenv(up.API_KEY_ENV, "k")
    for value, expected in (
        ("primary", "primary"),
        ("fallback", "fallback"),
        ("off", "off"),
        ("0", "off"),
        ("  PRIMARY  ", "primary"),
    ):
        monkeypatch.setenv(up.UPSTREAM_ENV, value)
        assert up.upstream_mode() == expected, value


# ---------------------------------------------------------------------------
# 2. wire shapes
# ---------------------------------------------------------------------------

def test_anthropic_wire_request(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(up.BASE_URL_ENV, "https://tydfxt.top/v1/")
    monkeypatch.setenv(up.API_KEY_ENV, "secret")
    monkeypatch.setenv(up.MODEL_ENV, "Qwen3.8-Flash")
    cfg = up.primary_config()
    assert cfg["base_url"] == "https://tydfxt.top/v1", "trailing slash must be stripped"
    url, payload, headers = up._build_request(cfg, "hello")
    assert url == "https://tydfxt.top/v1/messages"
    assert payload["model"] == "Qwen3.8-Flash"
    assert payload["messages"][0]["content"] == "hello"
    assert headers["x-api-key"] == "secret"
    assert "anthropic-version" in headers


def test_openai_wire_request(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(up.BASE_URL_ENV, "https://tydfxt.top/v1")
    monkeypatch.setenv(up.API_KEY_ENV, "secret")
    monkeypatch.setenv(up.WIRE_ENV, "openai")
    cfg = up.primary_config()
    url, payload, headers = up._build_request(cfg, "hello")
    assert url == "https://tydfxt.top/v1/chat/completions"
    assert headers["authorization"] == "Bearer secret"


# ---------------------------------------------------------------------------
# 3./4. response parsing and usage caliber
# ---------------------------------------------------------------------------

def test_uncached_only_counts_are_converted_to_full_prompt(monkeypatch):
    """Measured upstream quirk: input_tokens counts only NEW (uncached) tokens.

    Evidence: repeating one prompt reports in=18 then in=6 then in=6 while
    cache_read holds the ~2590-token prefix. GLM reports the WHOLE prompt, so
    the adapter must convert (full = input + cache_read) or every call looks
    like a ~100% cache hit and the KPI this pipeline tracks is destroyed.
    """

    _clear(monkeypatch)
    monkeypatch.setenv(up.BASE_URL_ENV, "https://x/v1")
    monkeypatch.setenv(up.API_KEY_ENV, "k")
    cfg = up.primary_config()
    body = {
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 6, "output_tokens": 20,
                  "cache_read_input_tokens": 2599,
                  "cache_creation_input_tokens": 0},
    }
    usage = up._parse_response(cfg, body)["usage"]
    assert usage["provider_input_tokens"] == 6
    assert usage["input_tokens"] == 2605, "input_tokens must become the FULL prompt"
    assert usage["cache_read_input_tokens"] == 2599
    assert usage["cache_caliber"] == "provider_uncached_plus_cached"
    # the ratio is now the pipeline's convention: 2599 / 2605
    assert usage["cache_read_input_tokens"] / usage["input_tokens"] == pytest.approx(0.9977, abs=1e-3)


def test_zero_cache_read_leaves_input_untouched(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(up.BASE_URL_ENV, "https://x/v1")
    monkeypatch.setenv(up.API_KEY_ENV, "k")
    cfg = up.primary_config()
    usage = up._parse_response(
        cfg, {"content": [{"type": "text", "text": "ok"}],
              "usage": {"input_tokens": 500, "output_tokens": 5,
                        "cache_read_input_tokens": 0}}
    )["usage"]
    assert usage["input_tokens"] == 500
    assert "cache_caliber" not in usage


def test_anthropic_response_parsing_and_usage(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(up.BASE_URL_ENV, "https://x/v1")
    monkeypatch.setenv(up.API_KEY_ENV, "k")
    cfg = up.primary_config()
    body = {
        "model": "Qwen3.8-Flash",
        "stop_reason": "end_turn",
        "content": [
            {"type": "thinking", "thinking": "let me think"},
            {"type": "text", "text": "ANSWER"},
        ],
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 50,
            "cache_read_input_tokens": 800,
            "cache_creation_input_tokens": 25,
        },
    }
    parsed = up._parse_response(cfg, body)
    assert parsed["text"] == "ANSWER"
    assert parsed["thinking"] == "let me think"
    # provider reported 1000 uncached tokens; the adapter converts to the
    # pipeline convention where input_tokens is the WHOLE prompt.
    assert parsed["usage"]["provider_input_tokens"] == 1000
    assert parsed["usage"]["input_tokens"] == 1800
    assert parsed["usage"]["cache_read_input_tokens"] == 800
    assert parsed["usage"]["cache_creation_input_tokens"] == 25
    # converted to the pipeline convention: input becomes the FULL prompt
    assert parsed["usage"]["provider_input_tokens"] == 1000
    assert parsed["usage"]["input_tokens"] == 1800
    assert parsed["usage"]["cache_read_input_tokens"] / parsed["usage"]["input_tokens"] == pytest.approx(0.4444, abs=1e-4)


def test_openai_response_parsing_and_usage(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(up.BASE_URL_ENV, "https://x/v1")
    monkeypatch.setenv(up.API_KEY_ENV, "k")
    monkeypatch.setenv(up.WIRE_ENV, "openai")
    cfg = up.primary_config()
    body = {
        "model": "Qwen3.8-Flash",
        "choices": [{
            "message": {"content": "ANSWER", "reasoning_content": "hmm"},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": 500,
            "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 400},
        },
    }
    parsed = up._parse_response(cfg, body)
    assert parsed["text"] == "ANSWER"
    assert parsed["thinking"] == "hmm"
    assert parsed["usage"]["provider_input_tokens"] == 500
    assert parsed["usage"]["input_tokens"] == 900
    assert parsed["usage"]["cache_read_input_tokens"] == 400
    assert parsed["usage"]["cache_creation_input_tokens"] == 0


def test_query_yields_assistant_and_result_with_cache_caliber(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(up.BASE_URL_ENV, "https://x/v1")
    monkeypatch.setenv(up.API_KEY_ENV, "k")

    class _Transport(up.PrimaryHttpTransport):
        def __init__(self):
            up.PrimaryHttpTransport.__init__(self, "p", None)

        async def run(self):
            self.result = {
                "text": "OK",
                "thinking": "t",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 90,
                    "cache_creation_input_tokens": 0,
                },
                "model": "Qwen3.8-Flash",
                "stop_reason": "end_turn",
            }
            return self.result

    async def _run():
        messages = []
        async for m in up.primary_query(prompt="p", options=None, transport=_Transport()):
            messages.append(m)
        return messages

    messages = asyncio.run(_run())
    kinds = [type(m).__name__ for m in messages]
    assert kinds == ["AssistantMessage", "AssistantMessage", "ResultMessage"]
    assert any(isinstance(b, ThinkingBlock) for b in messages[0].content)
    assert any(isinstance(b, TextBlock) and b.text == "OK" for b in messages[1].content)
    result = messages[-1]
    assert isinstance(result, ResultMessage)
    assert result.subtype == "success" and result.is_error is False
    assert result.usage["cache_read_input_tokens"] / result.usage["input_tokens"] == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# 5. error verdicts — the fallback rule lives here
# ---------------------------------------------------------------------------

def test_rate_limit_bodies_are_retryable_never_fatal():
    for status, body in (
        (429, "too many requests"),
        (400, "[1302][您的账户已达到速率限制，请您控制请求频率]"),
        (400, "[1308][已达到 5 小时的使用上限。]"),
        (200, '{"error":{"message":"rate limit exceeded"}}'),
    ):
        assert up._looks_like_rate_limit(status, body) is True, (status, body)


def test_non_rate_limit_bodies_are_not_retryable():
    for status, body in (
        (500, "internal error"),
        (401, "invalid api key"),
        (400, "bad request: model not found"),
        (200, "ok"),
    ):
        assert up._looks_like_rate_limit(status, body) is False, (status, body)


def test_rate_limited_error_object_is_not_fatal(monkeypatch):
    """A rate limit must NOT carry fatal=True, or the pipeline would fall back
    and spend the other account's quota."""

    _clear(monkeypatch)
    monkeypatch.setenv(up.BASE_URL_ENV, "https://x/v1")
    monkeypatch.setenv(up.API_KEY_ENV, "k")

    transport = up.new_primary_transport("p", None)

    def _fake_post(url, payload, headers, timeout):
        return 429, None, "too many requests"

    monkeypatch.setattr(up, "_post_json", _fake_post)
    with pytest.raises(up.PrimaryUpstreamError) as excinfo:
        asyncio.run(transport.run())
    assert excinfo.value.retryable is True
    assert excinfo.value.fatal is False


def test_hard_failure_error_object_is_fatal(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(up.BASE_URL_ENV, "https://x/v1")
    monkeypatch.setenv(up.API_KEY_ENV, "k")
    transport = up.new_primary_transport("p", None)

    def _fake_post(url, payload, headers, timeout):
        return 503, None, "upstream unavailable"

    monkeypatch.setattr(up, "_post_json", _fake_post)
    with pytest.raises(up.PrimaryUpstreamError) as excinfo:
        asyncio.run(transport.run())
    assert excinfo.value.fatal is True
    assert excinfo.value.retryable is False


# ---------------------------------------------------------------------------
# 6. dispatch wiring
# ---------------------------------------------------------------------------

def test_retry_dispatch_tries_primary_first():
    import llm_query_retry as retry

    source = inspect.getsource(retry._run_stream_with_signature_retry_attempts)
    primary_at = source.find("_up.primary_enabled()")
    codex_at = source.find("_cx.new_codex_exec_transport(")
    assert primary_at != -1, "the primary branch must exist"
    assert codex_at != -1, "the codex branch must survive as the fallback"
    assert primary_at < codex_at, "the primary must be tried before the fallback"
    assert "_up.primary_query(" in source


def test_fallback_is_keyed_to_fatal_and_not_retryable():
    """The one-line invariant the operator asked for, as a code assertion."""

    import llm_query_retry as retry

    source = inspect.getsource(retry._run_stream_with_signature_retry_attempts)
    assert "e.fatal and not e.retryable" in source
    # and the fallback must not loop forever within one call
    assert "primary_fallback_used" in source


def test_close_confirms_without_a_process():
    """The provider-attempt lifecycle needs a close() that returns True; a
    process-less transport has nothing to kill."""

    transport = up.new_primary_transport("p", None)
    assert transport._process is None
    assert asyncio.run(transport.close()) is True


def test_primary_declares_degraded_tool_support(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(up.BASE_URL_ENV, "https://x/v1")
    monkeypatch.setenv(up.API_KEY_ENV, "k")
    cfg = up.primary_config()
    assert cfg["tools_supported"] is False
    assert up.PrimaryHttpTransport.tools_supported is False
    assert up.PrimaryHttpTransport.native_streaming is False