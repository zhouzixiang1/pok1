"""Tests for the POK_LLM_TRANSPORT=zcode adapter (web/core/llm_query_zcode.py).

Block-48, 2026-11-03: the operator asked for the production LLM base to run
through the local ZCode client's account (bigmodel-individual-coding-plan /
GLM-5.3-Flash) instead of the codex CLI.  These tests pin the contracts that
make the third transport safe to select, without needing a provider call:

1. dispatch — ``POK_LLM_TRANSPORT`` remains a single selector; unset/``claude``
   and ``codex`` keep their exact meaning, ``zcode`` is the new third value,
   and the retry-loop chokepoint picks the matching transport/query pair;
2. event translation — the ZCode session event stream (``part.delta`` fields
   text/reasoning, ``part.upserted``, ``tool.updated``, ``turn.completed``,
   ``turn.failed``) becomes the exact SDK message objects, with coalesced
   parts never double-emitted (a resumed/upserted part after deltas must not
   duplicate the answer text — role-IO evidence and TTFT would both distort);
3. usage mapping — ``inputTokens``/``outputTokens``/``cacheReadTokens``/
   ``cacheWriteTokens`` map onto the SDK usage keys AND keep the vendor keys,
   so the cache-hit KPI (CR / full-prompt input) means exactly what it means on
   the claude and codex paths; missing fields are 0, never absent;
4. error text propagation — a provider failure raises ClaudeSDKError carrying
   the raw GLM 1308 body, so classify_llm_availability and the quota pause
   chain parse it unchanged;
5. binary resolution — the Electron desktop launcher is not a valid target
   (it exits without an X display); resolution failure is
   ZCodeBinaryNotFound (deliberately NOT a ClaudeSDKError) so it can never be
   classified as a provider quota/frequency event;
6. reverse RPC — the app-server asks the client for
   ``session/requestRuntimePreferences`` and
   ``interaction/requestOfficialMcpAuthHeaders`` during session
   materialization; unanswered requests make ``session/create`` fail with
   ``Client request timed out`` (measured 15 s), so the adapter must answer
   both, and its default preferences must be least-capability.

Run in the operator checkout only:
    /home/ubuntu/pok1/.venv/bin/python -m pytest web/tests/test_llm_query_zcode_transport.py
"""

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))

import llm_query_zcode as zc  # noqa: E402

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ClaudeSDKError,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    UserMessage,
)


# ---------------------------------------------------------------------------
# 1. dispatch
# ---------------------------------------------------------------------------

def test_transport_selector_is_single_valued(monkeypatch):
    import llm_query_codex as cx

    for value, want_zcode, want_codex in (
        (None, False, False),
        ("claude", False, False),
        ("codex", False, True),
        ("zcode", True, False),
        (" ZCode ", True, False),
    ):
        if value is None:
            monkeypatch.delenv(zc.TRANSPORT_ENV, raising=False)
        else:
            monkeypatch.setenv(zc.TRANSPORT_ENV, value)
        assert zc.zcode_transport_enabled() is want_zcode, value
        assert cx.codex_transport_enabled() is want_codex, value


def test_retry_dispatch_routes_to_matching_adapter():
    """The retry loop must select transport and query generator as a pair."""

    import inspect

    import llm_query_retry as retry

    source = inspect.getsource(retry._run_stream_with_signature_retry_attempts)
    assert "_zc.zcode_transport_enabled()" in source
    assert "_zc.new_zcode_transport(" in source
    assert "_zc.zcode_query(" in source
    # the codex branch must remain reachable (instant rollback)
    assert "_cx.codex_transport_enabled()" in source
    assert "_cx.new_codex_exec_transport(" in source


# ---------------------------------------------------------------------------
# 2. event translation
# ---------------------------------------------------------------------------

def _translate(events, model="glm-5.3-flash"):
    translator = zc.ZCodeEventTranslator(model)
    messages = []
    for event in events:
        messages.extend(translator.handle(event))
    return translator, messages


def test_text_deltas_stream_and_join():
    translator, messages = _translate(
        [
            {"type": "turn.started"},
            {"type": "part.delta", "field": "text", "delta": "Hel", "partId": "p1"},
            {"type": "part.delta", "field": "text", "delta": "lo", "partId": "p1"},
        ]
    )
    assert translator.text_so_far() == "Hello"
    texts = [b.text for m in messages for b in m.content if isinstance(b, TextBlock)]
    assert texts == ["Hel", "lo"], "deltas must stream, not be buffered to one block"
    assert all(isinstance(m, AssistantMessage) for m in messages[:2])
    assert messages[0].model == "glm-5.3-flash"


def test_reasoning_deltas_become_thinking_blocks():
    _, messages = _translate(
        [{"type": "part.delta", "field": "reasoning", "delta": "hmm", "partId": "r1"}]
    )
    blocks = [b for m in messages for b in m.content if isinstance(b, ThinkingBlock)]
    assert len(blocks) == 1 and blocks[0].thinking == "hmm"
    # ThinkingBlock requires a signature field on this SDK version
    assert isinstance(blocks[0].signature, str)


def test_coalesced_part_does_not_double_emit():
    """A part already delivered via deltas must not be re-emitted whole."""

    translator, _ = _translate(
        [{"type": "part.delta", "field": "text", "delta": "abc", "partId": "p1"}]
    )
    after = translator.handle(
        {"type": "part.upserted", "part": {"partId": "p1", "text": "abc"}}
    )
    assert after == []
    assert translator.text_so_far() == "abc"


def test_tool_lifecycle_counts_use_and_result():
    translator, messages = _translate(
        [
            {
                "type": "part.upserted",
                "part": {
                    "partId": "p2",
                    "type": "tool",
                    "toolCallId": "c1",
                    "toolName": "Bash",
                    "input": {"cmd": "ls"},
                },
            },
            {"type": "tool.updated", "kind": "completed", "toolCallId": "c1", "output": "ok"},
        ]
    )
    assert translator.tool_use_count == 1
    assert translator.tool_result_count == 1
    uses = [b for m in messages for b in m.content if isinstance(b, ToolUseBlock)]
    results = [
        b for m in messages if isinstance(m, UserMessage) for b in m.content
    ]
    assert uses and uses[0].name == "Bash" and uses[0].input == {"cmd": "ls"}
    assert results and results[0].is_error is False


# ---------------------------------------------------------------------------
# 3. usage mapping
# ---------------------------------------------------------------------------

def test_usage_mapping_preserves_cache_caliber():
    translator, _ = _translate(
        [
            {
                "type": "turn.completed",
                "response": "Hello",
                "duration": 1.5,
                "toolCallCount": 1,
                "usage": {
                    "inputTokens": 1000,
                    "outputTokens": 20,
                    "cacheReadTokens": 800,
                    "cacheWriteTokens": 100,
                },
                "cacheStats": {
                    "totalMessages": 5,
                    "cachedMessages": 4,
                    "lastCacheHit": True,
                    "cacheReadTokens": 800,
                },
                "resultType": "success",
            }
        ]
    )
    result = translator.result_messages()
    assert len(result) == 1 and isinstance(result[0], ResultMessage)
    usage = result[0].usage
    # SDK keys the metrics writer reads
    assert usage["input_tokens"] == 1000
    assert usage["output_tokens"] == 20
    assert usage["cache_read_input_tokens"] == 800
    assert usage["cache_creation_input_tokens"] == 100
    # vendor keys stay present so the record is self-describing
    assert usage["cacheReadTokens"] == 800
    assert usage["cacheWriteTokens"] == 100
    # the KPI definition must be CR / full-prompt input, exactly as on the
    # claude/codex paths (this is the layer that was wrong on 2026-11-02)
    assert usage["cache_read_input_tokens"] / usage["input_tokens"] == pytest.approx(0.8)
    assert result[0].total_cost_usd is None


def test_usage_missing_fields_are_zero_not_absent():
    translator, _ = _translate(
        [{"type": "turn.completed", "response": "x", "duration": 0.1,
          "resultType": "success"}]
    )
    usage = translator.result_messages()[0].usage
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "cacheReadTokens",
        "cacheWriteTokens",
        "reasoningTokens",
    ):
        assert key in usage and usage[key] == 0, key


def test_cache_stats_supplies_cache_read_when_usage_omits_it():
    translator, _ = _translate(
        [
            {
                "type": "turn.completed",
                "response": "x",
                "duration": 0.1,
                "usage": {"inputTokens": 500, "outputTokens": 5},
                "cacheStats": {
                    "totalMessages": 3,
                    "cachedMessages": 2,
                    "lastCacheHit": True,
                    "cacheReadTokens": 400,
                },
                "resultType": "success",
            }
        ]
    )
    usage = translator.result_messages()[0].usage
    assert usage["cache_read_input_tokens"] == 400


# ---------------------------------------------------------------------------
# 4. error propagation
# ---------------------------------------------------------------------------

def test_turn_failure_records_raw_provider_body():
    translator, _ = _translate(
        [
            {
                "type": "turn.failed",
                "error": {
                    "message": "[1308][已达到 5 小时的使用上限。您的限额将在 "
                               "2026-11-03 21:00:33 重置。]",
                    "code": "1308",
                },
                "turnPhase": "streaming",
            }
        ]
    )
    assert "1308" in translator.fatal_error
    assert "21:00:33" in translator.fatal_error
    assert translator.result_subtype == "error_during_execution"


def test_non_success_result_type_is_an_error():
    translator, _ = _translate(
        [{"type": "turn.completed", "response": "", "duration": 0.1,
          "resultType": "error_max_budget"}]
    )
    result = translator.result_messages()[0]
    assert result.is_error is True
    assert result.subtype == "error_max_budget"


def test_zcode_query_raises_claudesdkerror_with_provider_text(monkeypatch):
    """The retry loop must see ClaudeSDKError whose text keeps the 1308 body."""

    class _FakeTransport:
        _stderr_note = staticmethod(lambda: "")
        _session_id = "sess_x"

        async def spawn(self):
            return None

        async def start_session(self):
            return "sess_x"

        async def send_prompt(self):
            return None

        async def next_event(self):
            return (
                "event",
                {
                    "type": "turn.failed",
                    "error": {"message": "[1308][已达到 5 小时的使用上限。]",
                              "code": "1308"},
                },
            )

        async def close(self):
            return True

    async def _run():
        gen = zc.zcode_query(prompt="p", options=None, transport=_FakeTransport())
        with pytest.raises(ClaudeSDKError) as excinfo:
            async for _ in gen:
                pass
        return str(excinfo.value)

    text = asyncio.run(_run())
    assert "1308" in text


# ---------------------------------------------------------------------------
# 5. binary resolution
# ---------------------------------------------------------------------------

def test_desktop_launcher_rejected_and_error_is_not_a_quota_class(monkeypatch, tmp_path):
    # a directory path is authoritative and must exist as an executable file
    missing = tmp_path / "nope" / "zcode"
    with pytest.raises(zc.ZCodeBinaryNotFound) as excinfo:
        zc.resolve_zcode_binary(str(missing))
    assert "POK_ZCODE_BIN" in str(excinfo.value)
    # deliberately NOT a ClaudeSDKError (classify_llm_availability must never
    # arm a 1302/1308 pause from a spawn-class problem)
    assert not isinstance(excinfo.value, ClaudeSDKError)


def test_bare_name_resolution_fails_actionably(monkeypatch):
    monkeypatch.setenv("PATH", "")
    with pytest.raises(zc.ZCodeBinaryNotFound):
        zc.resolve_zcode_binary("definitely-not-a-real-zcode-binary-xyz")


def test_default_binary_points_at_server_cli_not_desktop():
    assert zc.ZCODE_DEFAULT_BIN.endswith("/server/bin/zcode")
    assert zc.ZCODE_DEFAULT_BIN in zc.zcode_binary_from_env() or os.environ.get(
        zc.ZCODE_BIN_ENV
    )


# ---------------------------------------------------------------------------
# 6. reverse RPC answers and least-capability defaults
# ---------------------------------------------------------------------------

def test_default_preferences_are_least_capability(monkeypatch):
    for key in (
        "POK_ZCODE_NATIVE_SEARCH",
        "POK_ZCODE_TOOLSEARCH_MODE",
        "POK_ZCODE_MEMORY",
    ):
        monkeypatch.delenv(key, raising=False)
    prefs = zc._prefs_from_env()
    assert prefs["nativeSearchEnhancementsEnabled"] is False
    assert prefs["memoryEnabled"] is False
    assert prefs["askUserQuestionAutoResolutionEnabled"] is False
    assert prefs["toolSearchMode"] == "off"
    assert prefs["modelContextBudgetStrategy"] == "preflight-v1"


def test_reverse_rpc_requests_are_answered():
    """session/create hangs (then fails) unless both reverse RPCs are answered."""

    sent = []

    class _Transport(zc.ZCodeTransport):
        def __init__(self):
            pass

        async def _send(self, obj):
            sent.append(obj)

    transport = _Transport()
    asyncio.run(
        transport._answer_reverse_request(
            "srv-1", "session/requestRuntimePreferences", {"sessionId": "s"}
        )
    )
    asyncio.run(
        transport._answer_reverse_request(
            "srv-2", "interaction/requestOfficialMcpAuthHeaders", {}
        )
    )
    assert sent[0]["id"] == "srv-1" and "result" in sent[0]
    assert "nativeSearchEnhancementsEnabled" in sent[0]["result"]
    assert sent[1]["id"] == "srv-2" and "result" in sent[1]
    # unknown reverse requests must be refused, never silently accepted
    asyncio.run(transport._answer_reverse_request("srv-3", "x/unknown", {}))
    assert sent[2]["error"]["code"] == -32601


def test_model_selection_defaults_to_requested_account_and_model(monkeypatch):
    for key in (zc.ZCODE_PROVIDER_ENV, zc.ZCODE_MODEL_ENV, zc.ZCODE_THOUGHT_ENV):
        monkeypatch.delenv(key, raising=False)
    transport = zc.ZCodeTransport.__new__(zc.ZCodeTransport)
    selection = transport.model_selection()
    assert selection == {
        "providerId": "account:bigmodel-individual-coding-plan",
        "modelId": "GLM-5.3-Flash",
    }
    monkeypatch.setenv(zc.ZCODE_THOUGHT_ENV, "max")
    assert transport.model_selection()["options"] == {"reasoningLevel": "max"}


def test_prompt_goes_through_the_rpc_body_not_argv():
    """Regression guard: a production prompt exceeds the 128 KB argv cap.

    ``-p <prompt>`` dies with ``Argument list too long`` and ``-p -`` does not
    read stdin, so the adapter MUST carry the prompt inside the session/send
    JSON body.  This test fails if anyone "simplifies" it back to argv.
    """

    import inspect

    source = inspect.getsource(zc.ZCodeTransport.send_prompt)
    assert '"content": self._prompt' in source
    assert "session/send" in source
    # argv is only ever the launcher + subcommand
    spawn_source = inspect.getsource(zc.ZCodeTransport.spawn)
    assert '"app-server"' in spawn_source and '"--stdio"' in spawn_source
    assert "_prompt" not in spawn_source