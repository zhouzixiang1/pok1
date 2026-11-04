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

# ---------------------------------------------------------------------------
# 7. scoped Read tool loop
# ---------------------------------------------------------------------------

class _ScopeOptions:
    def __init__(self, tools=("Read",), dirs=("/tmp",)):
        self.model = "q"
        self.tools = list(tools)
        self.read_scope = {"dirs": list(dirs), "files": []}
        self.write_scope = {"dirs": [], "files": []}
        self.exact_bash_commands = ()
        self.role_name = "TEST"


def test_declared_tools_normalizes_strings_and_dicts():
    opts = _ScopeOptions(tools=["Read", {"name": "Bash"}])
    assert up.declared_tools(opts) == ["Read", "Bash"]
    assert up.declared_tools(None) == []
    assert up.declared_tools(_ScopeOptions(tools=[])) == []


def test_read_edit_bash_are_exposed_by_declaration():
    assert [s["name"] for s in up.tool_schemas_for(_ScopeOptions(tools=["Read"]))] == ["Read"]
    got = [s["name"] for s in up.tool_schemas_for(_ScopeOptions(tools=["Read", "Edit", "Bash"]))]
    assert got == ["Read", "Edit", "Bash"]
    # a role that declares nothing gets no tools
    assert up.tool_schemas_for(_ScopeOptions(tools=[])) == []


def test_read_inside_scope_succeeds(tmp_path):
    target = tmp_path / "sample.py"
    target.write_text("line one\nline two\n", encoding="utf-8")
    out = up.execute_read({"file_path": str(target)}, _ScopeOptions(dirs=(str(tmp_path),)))
    assert "line one" in out and "line two" in out
    # numbered for the model
    assert out.lstrip().startswith("1")


def test_read_outside_scope_is_denied(tmp_path):
    other = tmp_path / "secret.txt"
    other.write_text("nope", encoding="utf-8")
    scoped = _ScopeOptions(dirs=(str(tmp_path / "allowed"),))
    with pytest.raises(up.ToolScopeError):
        up.execute_read({"file_path": str(other)}, scoped)


def test_read_with_no_scope_is_denied(tmp_path):
    target = tmp_path / "x.txt"
    target.write_text("data", encoding="utf-8")
    with pytest.raises(up.ToolScopeError):
        up.execute_read({"file_path": str(target)}, _ScopeOptions(dirs=()))


def test_relative_path_and_missing_file_path_are_rejected():
    with pytest.raises(up.ToolScopeError):
        up.execute_read({"file_path": "web/core/x.py"}, _ScopeOptions(dirs=("/home",)))
    with pytest.raises(up.ToolScopeError):
        up.execute_read({}, _ScopeOptions(dirs=("/home",)))


def test_missing_file_returns_an_error_string_not_a_raise(tmp_path):
    """A missing file is model-recoverable: return text so it can adapt."""

    missing = tmp_path / "absent.py"
    out = up.execute_read({"file_path": str(missing)}, _ScopeOptions(dirs=(str(tmp_path),)))
    assert "<error>" in out and "not found" in out


def test_symlink_is_rejected(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("data", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(up.ToolScopeError):
        up.execute_read({"file_path": str(link)}, _ScopeOptions(dirs=(str(tmp_path),)))


def test_unsupported_tools_are_refused(monkeypatch):
    """Read/Edit/Bash are implemented with scope enforcement. Capabilities this
    transport genuinely lacks (Write/WebSearch/Task) must be refused rather than
    silently dropped, or a role would run with less authority than it declared."""

    _clear(monkeypatch)
    monkeypatch.setenv(up.BASE_URL_ENV, "https://x/v1")
    monkeypatch.setenv(up.API_KEY_ENV, "k")
    for tools in (["WebSearch"], ["Write"], ["Read", "Task"]):
        transport = up.new_primary_transport("p", _ScopeOptions(tools=tools))
        with pytest.raises(up.PrimaryUpstreamError) as excinfo:
            asyncio.run(transport.run())
        assert excinfo.value.fatal is True
        assert excinfo.value.upstream_code == "unsupported_tools"
    # and the implemented trio is NOT refused
    for tools in (["Read"], ["Edit"], ["Bash"], ["Read", "Edit", "Bash"]):
        transport = up.new_primary_transport("p", _ScopeOptions(tools=tools))
        assert [s["name"] for s in up.tool_schemas_for(transport._options)] == tools


def test_build_request_carries_tools_on_both_wires(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(up.BASE_URL_ENV, "https://x/v1")
    monkeypatch.setenv(up.API_KEY_ENV, "k")
    cfg = up.primary_config()
    url, payload, _ = up._build_request(cfg, "p", None, [up._READ_TOOL])
    assert payload["messages"][0]["content"] == "p"
    assert payload["tools"][0]["name"] == "Read"
    monkeypatch.setenv(up.WIRE_ENV, "openai")
    cfg2 = up.primary_config()
    _, payload2, _ = up._build_request(cfg2, "p", None, [up._READ_TOOL])
    assert payload2["tools"][0]["type"] == "function"
    assert payload2["tools"][0]["function"]["name"] == "Read"


def test_parse_response_extracts_tool_calls_on_both_wires(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(up.BASE_URL_ENV, "https://x/v1")
    monkeypatch.setenv(up.API_KEY_ENV, "k")
    cfg = up.primary_config()
    anth = up._parse_response(cfg, {
        "content": [{"type": "tool_use", "id": "c1", "name": "Read",
                     "input": {"file_path": "/a"}}],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 0},
    })
    assert anth["tool_calls"] == [{"id": "c1", "name": "Read", "input": {"file_path": "/a"}}]
    monkeypatch.setenv(up.WIRE_ENV, "openai")
    cfg2 = up.primary_config()
    oai = up._parse_response(cfg2, {
        "choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c2", "type": "function",
             "function": {"name": "Read", "arguments": '{"file_path": "/b"}'}}]},
            "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
    })
    assert oai["tool_calls"] == [{"id": "c2", "name": "Read", "input": {"file_path": "/b"}}]


def test_read_budget_is_bounded(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv(up.BASE_URL_ENV, "https://x/v1")
    monkeypatch.setenv(up.API_KEY_ENV, "k")
    monkeypatch.setenv("POK_LLM_PRIMARY_MAX_READ_CALLS", "3")
    assert up._max_read_calls() == 3
    monkeypatch.setenv("POK_LLM_PRIMARY_MAX_READ_CALLS", "0")
    assert up._max_read_calls() == 0
    monkeypatch.delenv("POK_LLM_PRIMARY_MAX_READ_CALLS")
    monkeypatch.setenv("POK_LLM_SATURATOR_MAX_READ_CALLS", "18")
    assert up._max_read_calls() == 18, "must inherit the saturator's own hard stop"


# ---------------------------------------------------------------------------
# 8. Edit / Bash scope enforcement
# ---------------------------------------------------------------------------

class _WriteOptions:
    def __init__(self, dirs, tools=("Read", "Edit", "Bash")):
        self.model = "q"
        self.tools = list(tools)
        self.read_scope = {"dirs": list(dirs), "files": []}
        self.write_scope = {"dirs": list(dirs), "files": []}
        self.exact_bash_commands = ()
        self.role_name = "WORKER"


def test_edit_inside_scope_applies(tmp_path):
    target = tmp_path / "policy.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    out = up.execute_edit(
        {"file_path": str(target), "old_string": "VALUE = 1", "new_string": "VALUE = 2"},
        _WriteOptions((str(tmp_path),)),
    )
    assert "<ok>" in out
    assert target.read_text(encoding="utf-8") == "VALUE = 2\n"


def test_edit_outside_scope_is_denied(tmp_path):
    allowed = tmp_path / "lease"
    allowed.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("X = 1\n", encoding="utf-8")
    with pytest.raises(up.ToolScopeError):
        up.execute_edit(
            {"file_path": str(outside), "old_string": "X = 1", "new_string": "X = 2"},
            _WriteOptions((str(allowed),)),
        )
    assert outside.read_text(encoding="utf-8") == "X = 1\n", "must be untouched"


def test_edit_requires_a_unique_old_string(tmp_path):
    target = tmp_path / "dup.py"
    target.write_text("A\nA\n", encoding="utf-8")
    out = up.execute_edit(
        {"file_path": str(target), "old_string": "A", "new_string": "B"},
        _WriteOptions((str(tmp_path),)),
    )
    assert "<error>" in out and "appears 2 times" in out
    assert target.read_text(encoding="utf-8") == "A\nA\n"


def test_edit_missing_old_string_is_reported_not_raised(tmp_path):
    target = tmp_path / "m.py"
    target.write_text("hello\n", encoding="utf-8")
    out = up.execute_edit(
        {"file_path": str(target), "old_string": "absent", "new_string": "x"},
        _WriteOptions((str(tmp_path),)),
    )
    assert "<error>" in out and "not found" in out


def test_bash_runs_inside_scope(tmp_path):
    out = up.execute_bash({"command": "echo scoped-ok"}, _WriteOptions((str(tmp_path),)))
    assert "scoped-ok" in out and "<exit_code>0</exit_code>" in out


def test_bash_write_outside_scope_is_denied(tmp_path):
    allowed = tmp_path / "lease"
    allowed.mkdir()
    with pytest.raises(up.ToolScopeError):
        up.execute_bash(
            {"command": "echo pwned > /tmp/outside-the-lease.txt"},
            _WriteOptions((str(allowed),)),
        )


def test_bash_with_no_write_scope_allows_reads_but_denies_writes(tmp_path):
    """A read-only command cannot mutate the tree, so it passes; the same call
    with a redirect is refused. The gate is about writes, not about Bash."""

    assert "hi" in up.execute_bash({"command": "echo hi"}, _WriteOptions(()))
    with pytest.raises(up.ToolScopeError):
        up.execute_bash({"command": "echo hi > /tmp/anywhere.txt"}, _WriteOptions(()))


def test_execute_tool_rejects_unknown_names(tmp_path):
    with pytest.raises(up.ToolScopeError):
        up.execute_tool("Task", {}, _WriteOptions((str(tmp_path),)))


# ---------------------------------------------------------------------------
# 9. non-streaming fold + metrics attribution on the primary path
# ---------------------------------------------------------------------------

def test_primary_transport_folds_first_activity_gate(monkeypatch):
    """Measured failure this prevents (2026-11-04): after the full migration to
    the direct-HTTP primary, 6 saturator packets died at exactly
    'first_activity timeout after 900.0s' while surviving calls legitimately
    ran 347-884s. The transport yields its first substantive message only when
    the whole tool loop completes (num_turns is always 1), so a first-activity
    budget cannot observe progress there either.

    The fold lives in llm_query_retry._process_stream and is keyed to the SAME
    env predicate that selects the transport (primary_enabled), so llm_query.py
    — a CRITICAL_EXACT contract path — needs no signature change.
    """

    import llm_query_retry as retry

    source = inspect.getsource(retry._process_stream)
    assert "primary_enabled()" in source
    assert "first_activity_timeout = 0.0" in source
    # streaming policy must still read the per-role budget (the fold zeroes it
    # only under the non-streaming predicates)
    assert 'timeout_policy.get("first_activity_timeout")' in source


def test_primary_metrics_model_reads_env(monkeypatch):
    """Without this the metrics rows keep the static ANTHROPIC_MODEL label
    (glm-5.3-flash) even though every call went to Qwen — measured: 37 primary
    rows mislabeled, poisoning any per-model cache-hit analysis.
    """

    monkeypatch.setenv(up.MODEL_ENV, "Qwen3.8-Flash")
    assert up.primary_metrics_model() == "Qwen3.8-Flash"
    monkeypatch.delenv(up.MODEL_ENV)
    assert up.primary_metrics_model() == "primary"


def test_retry_records_primary_model_label():
    """The success-path metrics write must branch to primary_metrics_model()
    when the primary transport is in effect (mirror of codex_metrics_model)."""

    import llm_query_retry as retry

    source = inspect.getsource(retry._run_stream_with_signature_retry_attempts)
    assert "primary_metrics_model()" in source
