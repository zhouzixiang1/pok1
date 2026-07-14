"""Tests for the unified LLM availability boundary."""

import asyncio
import json

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from core import llm_query
from llm_availability import (
    BILLING_CYCLE_LIMIT,
    INVALID_AUTH,
    QUOTA_429,
    SERVICE_UNAVAILABLE,
    TRANSPORT_UNAVAILABLE,
    LLMAvailabilityBlocked,
    LLMAvailabilityTrace,
    build_llm_pause_state,
    classify_llm_availability,
    gather_llm_fail_fast,
    looks_like_provider_error_envelope,
)


class _UI:
    def log_io(self, *_args, **_kwargs):
        pass

    def log_history(self, *_args, **_kwargs):
        pass

    def emit_tool_call(self, *_args, **_kwargs):
        pass


def test_availability_gather_cancels_and_awaits_sibling_cleanup():
    started = asyncio.Event()
    closed = asyncio.Event()
    never = asyncio.Event()
    issue = classify_llm_availability(
        ["API Error: 403 usage limit for this billing cycle"],
        statuses=[403],
    )
    assert issue is not None

    async def sibling_stream():
        started.set()
        try:
            await never.wait()
        finally:
            closed.set()

    async def blocked_role():
        await started.wait()
        raise LLMAvailabilityBlocked(issue, role="blocked")

    async def run():
        with pytest.raises(LLMAvailabilityBlocked):
            await gather_llm_fail_fast(blocked_role(), sibling_stream())
        assert closed.is_set()

    asyncio.run(run())


def test_availability_gather_preserves_order_and_ordinary_exceptions():
    failure = RuntimeError("schema failure")

    async def value(item):
        return item

    async def ordinary_failure():
        raise failure

    results = asyncio.run(
        gather_llm_fail_fast(value("first"), ordinary_failure(), value("last"))
    )
    assert results == ["first", failure, "last"]


def _error_result(*, status, errors=None):
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=True,
        num_turns=0,
        session_id="availability-test",
        total_cost_usd=0.0,
        usage={},
        errors=errors,
        api_error_status=status,
    )


def test_billing_cycle_evidence_outranks_trailing_generic_sdk_error():
    trace = LLMAvailabilityTrace()
    trace.observe_text(
        "API Error: 403 You've reached your usage limit for this billing cycle"
    )
    trace.observe_result(_error_result(status=403))

    blocked = trace.blocked(
        role="worker",
        exception=Exception("Claude Code returned an error result: success"),
    )

    assert isinstance(blocked, LLMAvailabilityBlocked)
    assert blocked.issue.category == BILLING_CYCLE_LIMIT
    assert blocked.issue.http_status == 403
    assert blocked.issue.requires_manual_resume is True
    assert "error result: success" not in blocked.issue.summary


def test_actual_stream_sequence_raises_typed_billing_block_before_generic_tail(
    monkeypatch,
    tmp_path,
):
    events = []
    tail_reached = False

    async def stream():
        nonlocal tail_reached
        yield AssistantMessage(
            content=[
                TextBlock(
                    text=(
                        "API Error: 403 You've reached your usage limit for "
                        "this billing cycle"
                    )
                )
            ],
            model="sonnet",
        )
        yield _error_result(status=403)
        tail_reached = True
        raise Exception("Claude Code returned an error result: success")

    monkeypatch.setattr(
        llm_query,
        "_emit_llm_event",
        lambda category, severity, message, **fields: events.append(
            (category, severity, message, fields)
        ),
    )

    with pytest.raises(LLMAvailabilityBlocked) as caught:
        asyncio.run(
            llm_query._process_stream(
                stream(),
                str(tmp_path / "availability.log"),
                _UI(),
                "worker",
            )
        )

    assert caught.value.issue.category == BILLING_CYCLE_LIMIT
    assert tail_reached is False
    blocked_events = [
        event
        for event in events
        if event[0] == "pipeline.llm_role_availability_blocked"
    ]
    assert len(blocked_events) == 1
    assert blocked_events[0][3]["availability_category"] == BILLING_CYCLE_LIMIT


@pytest.mark.parametrize(
    ("evidence", "statuses", "exception", "expected"),
    [
        ("authentication_error: invalid API key", [401], None, INVALID_AUTH),
        ("Request rejected (429): quota exceeded", [429], None, QUOTA_429),
        ("API error 529: overloaded", [529], None, SERVICE_UNAVAILABLE),
        ("HTTP/1.1 503 Service Unavailable", [503], None, SERVICE_UNAVAILABLE),
        ("", [], ConnectionError("connection reset by peer"), TRANSPORT_UNAVAILABLE),
    ],
)
def test_classifier_covers_availability_classes(
    evidence,
    statuses,
    exception,
    expected,
):
    issue = classify_llm_availability(
        [evidence],
        statuses=statuses,
        exception=exception,
    )
    assert issue is not None
    assert issue.category == expected


def test_real_api_error_403_invalid_token_wording_is_invalid_auth():
    issue = classify_llm_availability(
        ["Failed to authenticate. API Error: 403 Invalid token"]
    )

    assert issue is not None
    assert issue.category == INVALID_AUTH
    assert issue.http_status == 403
    assert issue.requires_manual_resume is True


def test_generic_success_error_without_prior_evidence_is_not_classified():
    issue = classify_llm_availability(
        [],
        exception=Exception("Claude Code returned an error result: success"),
    )
    assert issue is None


@pytest.mark.parametrize("error_number", [28, 13])
def test_local_filesystem_oserror_is_not_provider_transport(error_number):
    issue = classify_llm_availability(
        [],
        exception=OSError(error_number, "local filesystem failure"),
    )
    assert issue is None


def test_network_oserror_is_provider_transport():
    issue = classify_llm_availability(
        [],
        exception=OSError(111, "connection refused"),
    )
    assert issue is not None
    assert issue.category == TRANSPORT_UNAVAILABLE


def test_classifier_priority_is_independent_of_evidence_order():
    evidence = [
        "API error 503 overloaded",
        "Request rejected (429): quota exceeded",
        "authentication_error: invalid API key",
        "403 usage limit for this billing cycle",
    ]
    issue = classify_llm_availability(
        reversed(evidence),
        statuses=[503, 429, 401, 403],
    )
    assert issue is not None
    assert issue.category == BILLING_CYCLE_LIMIT


def test_success_result_plus_generic_tail_does_not_promote_discussion_to_outage():
    trace = LLMAvailabilityTrace()
    trace.observe_text("Explain the usage limit for this billing cycle")
    trace.observe_result(
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="normal-answer",
        )
    )
    assert trace.blocked(
        exception=Exception("Claude Code returned an error result: success")
    ) is None


def test_credible_success_can_quote_exact_provider_error_without_pausing():
    trace = LLMAvailabilityTrace()
    trace.observe_text(
        "We observed API Error: 403 usage limit for this billing cycle in a log."
    )
    trace.observe_result(
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="quoted-error-answer",
            total_cost_usd=0.01,
            usage={"output_tokens": 12},
        )
    )
    assert trace.blocked(
        exception=Exception("Claude Code returned an error result: success")
    ) is None


def test_unrelated_error_result_does_not_promote_normal_assistant_discussion():
    trace = LLMAvailabilityTrace()
    trace.observe_text(
        "We should document the billing cycle usage limit and overloaded fallback."
    )
    trace.observe_result(
        _error_result(status=None, errors=["maximum turns exceeded"])
    )

    assert trace.blocked(role="reviewer") is None


@pytest.mark.parametrize(
    "text",
    [
        "We should document the overloaded fallback.",
        "The model discusses rate limit handling here.",
        "Overloaded services need a bounded recovery policy.",
    ],
)
def test_normal_discussion_is_not_a_provider_error_envelope(text):
    assert looks_like_provider_error_envelope(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "HTTP 529 service unavailable",
        "Error: model overloaded, please retry",
        "rate limit reached",
        "该模型当前访问量过大，请稍后重试",
    ],
)
def test_terse_provider_errors_are_provider_error_envelopes(text):
    assert looks_like_provider_error_envelope(text) is True


def test_429_requires_explicit_provider_reset_for_automatic_resume():
    bare = LLMAvailabilityTrace()
    bare.observe_result(_error_result(status=429, errors=["quota exceeded"]))
    bare_block = bare.blocked(role="worker")
    assert bare_block is not None
    assert bare_block.issue.category == QUOTA_429
    assert bare_block.issue.requires_manual_resume is True
    assert bare_block.issue.provider_reset_at is None

    reset = LLMAvailabilityTrace()
    reset.observe_text(
        "API Error: 429 quota reset at 2026-07-13T18:30:00+08:00"
    )
    reset.observe_result(_error_result(status=429))
    reset_block = reset.blocked(role="worker")
    assert reset_block is not None
    assert reset_block.issue.category == QUOTA_429
    assert reset_block.issue.requires_manual_resume is False
    assert reset_block.issue.provider_reset_at == "2026-07-13T18:30:00+08:00"


def test_429_accepts_common_provider_resets_at_wording():
    trace = LLMAvailabilityTrace()
    trace.observe_text(
        "API Error: 429 quota resets at 2026-07-13T18:30:00.500+08:00"
    )
    trace.observe_result(_error_result(status=429))

    blocked = trace.blocked(role="worker")
    assert blocked is not None
    assert blocked.issue.provider_reset_at == "2026-07-13T18:30:00.500+08:00"
    assert blocked.issue.requires_manual_resume is False


def test_successful_model_text_discussing_quota_is_not_a_stream_failure(
    monkeypatch,
    tmp_path,
):
    async def stream():
        yield AssistantMessage(
            content=[TextBlock(text="Explain the usage limit for this billing cycle")],
            model="sonnet",
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="normal-answer",
            total_cost_usd=0.01,
            usage={"output_tokens": 8},
        )

    monkeypatch.setattr(llm_query, "_emit_llm_event", lambda *_args, **_kwargs: None)
    texts, cost, usage = asyncio.run(
        llm_query._process_stream(
            stream(),
            str(tmp_path / "normal.log"),
            _UI(),
            "master",
        )
    )
    assert texts == ["Explain the usage limit for this billing cycle"]
    assert cost == 0.01
    assert usage == {"output_tokens": 8}


def test_pause_projection_is_pure_stable_json_contract():
    issue = classify_llm_availability(
        ["API Error: 403 usage limit for this billing cycle"],
        statuses=[403],
    )
    assert issue is not None

    state = build_llm_pause_state(
        issue,
        role="orchestrator",
        observed_at="2026-07-13T08:00:00+00:00",
    )

    assert state == {
        "schema_version": 1,
        "active": True,
        "source": "llm_availability",
        "observed_at": "2026-07-13T08:00:00+00:00",
        "role": "orchestrator",
        "category": BILLING_CYCLE_LIMIT,
        "summary": "provider billing-cycle usage limit reached",
        "http_status": 403,
        "retry_policy": "manual_resume",
        "requires_manual_resume": True,
        "persistent_pause": True,
        "evidence_digest": issue.evidence_digest,
        "provider_reset_at": None,
    }
    json.dumps(state, sort_keys=True)
