"""1302 amplifier fix (2026-10-18): saturator dispatches must not arm the
durable pipeline pause. Under sustained provider frequency pressure every
saturator retry re-extended auto_resume_at along the 8->120s curve, so the
pipeline's Master/Workers starved behind a pause no pipeline role ever
observed a failure for. Pipeline-role failures keep persisting it, and the
AIMD concurrency reduction still sees every frequency-class failure."""

import asyncio
from types import SimpleNamespace

import pytest

import llm_query as _lq
from llm_availability import SERVICE_UNAVAILABLE, LLMAvailabilityBlocked, classify_llm_availability


def _blocked():
    issue = classify_llm_availability(
        ["Request rejected (429) · [1302][您的账户已达到速率限制，请您控制请求频率]"],
        statuses=[429],
    )
    assert issue is not None and issue.category == SERVICE_UNAVAILABLE
    return LLMAvailabilityBlocked(issue, role="harness")


def _capability_stub():
    return SimpleNamespace(
        read_dirs=[],
        read_files=[],
        write_dirs=[],
        write_files=[],
        evidence_dir=None,
        context_files=[],
        exact_bash_commands=(),
        selected_tools=[],
        model="sonnet",
        strict_authority_json=None,
    )


@pytest.fixture
def harness(monkeypatch, tmp_path):
    import llm_availability_store
    import api_concurrency
    import llm_concurrency

    persisted = []
    aimd_failures = []

    monkeypatch.setattr(
        llm_availability_store,
        "persist_llm_pause",
        lambda e: persisted.append(e) or {"active": True},
    )
    monkeypatch.setattr(
        llm_availability_store, "raise_if_llm_paused", lambda **_kw: None
    )
    monkeypatch.setattr(
        api_concurrency,
        "record_llm_outcome",
        lambda **_kw: aimd_failures.append(1),
    )
    monkeypatch.setattr(
        llm_concurrency, "note_llm_rate_limit_failure", lambda now=None: None
    )
    monkeypatch.setattr(
        _lq,
        "validate_llm_role_dispatch",
        lambda role, **k: (
            SimpleNamespace(name=str(role)),
            SimpleNamespace(text=str(k.get("rendered_prompt") or "prompt")),
            _capability_stub(),
        ),
    )
    events = []
    monkeypatch.setattr(_lq, "_emit_llm_event", lambda *a, **k: events.append((a, k)))

    # A permissive registered contract so deep consumers (timeout policy,
    # guards) accept both test role labels.
    contract = _lq._llm_role_contract(
        role_id="pause-amplifier-harness",
        pattern=r".*",
        renderer="harness",
        producer_file="tests",
        producer_name="harness",
        evidence_kind="system_bound_prompt_only",
    )
    monkeypatch.setattr(
        _lq, "ACTIVE_LLM_ROLE_CONTRACTS", _lq.ACTIVE_LLM_ROLE_CONTRACTS + (contract,)
    )

    async def _failing_stream(*_a, **_k):
        raise _blocked()

    monkeypatch.setattr(_lq, "_run_stream_with_signature_retry", _failing_stream)
    # The provider-prompt binder re-validates the frozen capability's
    # authority token; bypass it with a passthrough that keeps the stub
    # receipt (the capability authority chain is not under test here).
    monkeypatch.setattr(
        _lq,
        "bind_llm_role_provider_prompt",
        lambda receipt, role, **_k: (
            str(getattr(receipt, "text", receipt)),
            SimpleNamespace(
                role_id="pause-amplifier-harness",
                renderer="harness",
                evidence_policy="system_bound_prompt_only",
                history_policy="forbidden",
            ),
        ),
    )

    return SimpleNamespace(
        persisted=persisted, aimd_failures=aimd_failures, events=events
    )


def _run(role_name):
    async def _call():
        return await _lq.run_claude_query(
            SimpleNamespace(text="prompt"), [], None, role_name, "/tmp/none.log"
        )

    return asyncio.new_event_loop().run_until_complete(_call())


def test_saturator_role_failure_does_not_persist_pipeline_pause(harness):
    with pytest.raises(LLMAvailabilityBlocked):
        _run("SATURATOR STRATEGY RESEARCH")
    assert harness.persisted == []
    # AIMD still sees the frequency failure (shared pressure signal).
    assert harness.aimd_failures == [1]
    kinds = [event[0][0] for event in harness.events]
    assert "pipeline.llm_role_availability_pause_exempt" in kinds
    assert "pipeline.llm_role_availability_pause_persisted" not in kinds


def test_pipeline_role_failure_still_persists_pipeline_pause(harness):
    with pytest.raises(LLMAvailabilityBlocked):
        _run("Master final analysis")
    assert len(harness.persisted) == 1
    assert harness.aimd_failures == [1]
    kinds = [event[0][0] for event in harness.events]
    assert "pipeline.llm_role_availability_pause_persisted" in kinds


def test_exemption_prefix_matcher():
    assert _lq._role_exempt_from_pause_persistence("SATURATOR STRATEGY RESEARCH")
    assert _lq._role_exempt_from_pause_persistence("saturator lowercase")
    assert not _lq._role_exempt_from_pause_persistence("Master final analysis")
    assert not _lq._role_exempt_from_pause_persistence(None)
    assert not _lq._role_exempt_from_pause_persistence("")
