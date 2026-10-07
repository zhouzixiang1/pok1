"""Upstream-adaptive LLM load: AIMD dynamic concurrency (2026-10-07).

Operator direction after the static ceiling raise (commit 3e6068f7,
``POK_GLOBAL_LLM_CONCURRENCY`` 9->12 / saturator inflight 6->8): the static
caps stay and the runtime must now self-tune the *actual* load against
upstream GLM feedback —

* keep probing additively PAST the static value while no 1302 arrives (the
  real wall floats across the day);
* multiplicative downshift only when frequency-class failures cluster
  (>= 3 inside the 300s sliding window; isolated 1302s are tolerated);
* GLM 1308 quota exhaustion (5h full stop, concurrency-unrelated) is never
  counted as a frequency failure;
* the learned level persists (``llm_aimd_state.json``) so a restart resumes
  nearby, and every launch guard re-reads LIVE machine state only.

Three layers, mirroring the implementation:
1. resizable ``CrossLoopSemaphore`` (capacity/holders accounting);
2. the AIMD controller in ``llm_concurrency`` (note_* + persistence);
3. the provider-result hooks in ``llm_query_retry`` and the dynamic-cap +
   cgroup-memory launch gates in ``llm_saturator``.
"""

import asyncio
import json

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKError

import llm_concurrency
import llm_saturator


@pytest.fixture(autouse=True)
def _fresh_aimd_state(monkeypatch, tmp_path):
    """Isolate every test from module singletons and from the real state file."""
    monkeypatch.setattr(llm_concurrency, "_SHARED_LLM_SEMAPHORE", None)
    monkeypatch.setattr(llm_concurrency, "_GLOBAL_LLM_SEMAPHORE", None)
    # Production static ceiling (deploy/tencent-cloud/env.runtime sets 12).
    monkeypatch.setattr(llm_concurrency, "GLOBAL_LLM_CONCURRENCY", 12)
    monkeypatch.setattr(
        llm_concurrency, "_AIMD_STATE_FILE", tmp_path / "llm_aimd_state.json"
    )
    monkeypatch.setattr(llm_concurrency, "_AIMD_LIMIT", None)
    monkeypatch.setattr(llm_concurrency, "_AIMD_FAILURE_TS", [])
    monkeypatch.setattr(llm_concurrency, "_AIMD_SUCCESSES", 0)
    monkeypatch.setattr(llm_concurrency, "_AIMD_LAST_LIMIT_CHANGE_TS", None)
    # The wrapped-dispatch tests exercise the real api_concurrency backoff
    # level; reset it so the process-level singleton never leaks across files.
    import api_concurrency

    api_concurrency.reset()
    yield


# ---------------------------------------------------------------------------
# 1. Resizable CrossLoopSemaphore
# ---------------------------------------------------------------------------

def test_shrink_blocks_new_acquires_and_drains_excess_holders():
    """set_capacity() must take effect immediately even with resident waiters.

    The pre-change release() handed a freed permit straight to the oldest
    waiter with no capacity check, so a saturated pool could never shrink:
    permit arithmetic alone cannot drain an over-held pool. With explicit
    _capacity/_holders accounting, a handoff is granted only when
    holders < capacity.
    """
    sem = llm_concurrency.CrossLoopSemaphore(3)

    async def scenario():
        for _ in range(3):
            await sem.acquire()
        resident = asyncio.create_task(sem.acquire())  # parked waiter
        await asyncio.sleep(0.05)
        assert sem._value == 0

        sem.set_capacity(1)
        # Over-held (3 holders vs capacity 1): derived permits read 0 and a
        # brand-new acquire must queue behind the drain, not steal a slot.
        fresh = asyncio.create_task(sem.acquire())
        await asyncio.sleep(0.05)
        assert not resident.done()
        assert not fresh.done()
        assert sem._value == 0
        assert sem.locked() is True

        # Two releases bring holders 3 -> 2 -> 1: still >= capacity, so NO
        # waiter may be granted yet.
        sem.release()
        sem.release()
        await asyncio.sleep(0.05)
        assert not resident.done()
        assert not fresh.done()

        # The third release drops holders below capacity: exactly one waiter
        # (FIFO — the resident one) is granted.
        sem.release()
        await asyncio.sleep(0.05)
        assert resident.done() and resident.result() is True
        assert not fresh.done()

        fresh.cancel()
        sem.release()  # the resident waiter's permit
        await asyncio.sleep(0.05)
        assert sem._value == 1

    asyncio.run(scenario())


def test_growth_wakes_exactly_delta_waiters_without_permit_leak():
    sem = llm_concurrency.CrossLoopSemaphore(1)

    async def scenario():
        await sem.acquire()  # the single holder
        waiters = [asyncio.create_task(sem.acquire()) for _ in range(3)]
        await asyncio.sleep(0.05)
        assert sem._value == 0

        sem.set_capacity(3)  # +2 -> exactly 2 waiters wake, each consuming 1
        await asyncio.sleep(0.05)
        assert [w.done() for w in waiters] == [True, True, False]
        assert sem._value == 0  # 3 holders fill capacity 3

        # Drain everything (1 initial holder + 3 granted waiters = 4 permits).
        for _ in range(4):
            sem.release()
        assert sem._value == 3
        assert sem.locked() is False
        await asyncio.sleep(0.05)  # let the scheduled handoff callback land
        assert all(w.done() for w in waiters)

    asyncio.run(scenario())


def test_adjust_capacity_composes():
    sem = llm_concurrency.CrossLoopSemaphore(4)
    sem.adjust_capacity(-3)
    assert sem._value == 1
    sem.adjust_capacity(2)
    assert sem._value == 3


# ---------------------------------------------------------------------------
# 2. AIMD controller
# ---------------------------------------------------------------------------

def test_isolated_failures_do_not_downshift():
    for ts in (100.0, 110.0):
        llm_concurrency.note_llm_rate_limit_failure(now=ts)
    assert llm_concurrency.get_capacity() == 12


def test_three_failures_in_window_halve_limit_and_clear_window():
    for ts in (100.0, 105.0, 110.0):
        llm_concurrency.note_llm_rate_limit_failure(now=ts)
    assert llm_concurrency.get_capacity() == 6
    # The burst cleared the window: two more isolated failures do nothing...
    llm_concurrency.note_llm_rate_limit_failure(now=130.0)
    llm_concurrency.note_llm_rate_limit_failure(now=135.0)
    assert llm_concurrency.get_capacity() == 6
    # ...a THIRD clustered failure halves again.
    llm_concurrency.note_llm_rate_limit_failure(now=140.0)
    assert llm_concurrency.get_capacity() == 3


def test_failures_expire_from_the_sliding_window():
    llm_concurrency.note_llm_rate_limit_failure(now=0.0)
    llm_concurrency.note_llm_rate_limit_failure(now=200.0)
    # At t=410 the t=0.0 entry is older than the 300s window: only 2 count.
    llm_concurrency.note_llm_rate_limit_failure(now=410.0)
    assert llm_concurrency.get_capacity() == 12


def test_downshift_floors_at_two():
    for round_start in (100.0, 150.0, 200.0, 250.0):
        for ts in (round_start, round_start + 5, round_start + 10):
            llm_concurrency.note_llm_rate_limit_failure(now=ts)
    assert llm_concurrency.get_capacity() == 2


def test_success_raises_additively_after_clean_window():
    for ts in (100.0, 105.0, 110.0):
        llm_concurrency.note_llm_rate_limit_failure(now=ts)  # -> 6
    # 4 successes only 10s after the downshift: not yet — give the provider
    # breathing room for a full raise interval.
    for _ in range(4):
        llm_concurrency.note_llm_stream_success(now=120.0)
    assert llm_concurrency.get_capacity() == 6
    # Past the interval with a clean window: the next success probes +1.
    llm_concurrency.note_llm_stream_success(now=500.0)
    assert llm_concurrency.get_capacity() == 7
    # A further raise needs fresh successes AND another full interval.
    llm_concurrency.note_llm_stream_success(now=501.0)
    assert llm_concurrency.get_capacity() == 7


def test_raise_blocked_by_failure_still_inside_window():
    for ts in (100.0, 105.0, 110.0):
        llm_concurrency.note_llm_rate_limit_failure(now=ts)  # -> 6
    llm_concurrency.note_llm_rate_limit_failure(now=480.0)  # isolated
    for _ in range(6):
        llm_concurrency.note_llm_stream_success(now=501.0)
    assert llm_concurrency.get_capacity() == 6  # window not clean
    # The failure ages out of the window; traffic then raises again.
    llm_concurrency.note_llm_stream_success(now=900.0)
    assert llm_concurrency.get_capacity() == 7


def test_upshift_probes_past_static_cap_and_clamps_at_32():
    llm_concurrency._AIMD_STATE_FILE.write_text(json.dumps({"limit": 31}))
    llm_concurrency._AIMD_LIMIT = None  # simulate a restart
    assert llm_concurrency.get_capacity() == 31  # recovered, past static 12
    # A clean window + traffic keeps probing upward, but never past 32.
    for _ in range(4):
        llm_concurrency.note_llm_stream_success(now=1000.0)
    assert llm_concurrency.get_capacity() == 32
    for _ in range(4):
        llm_concurrency.note_llm_stream_success(now=2000.0)
    assert llm_concurrency.get_capacity() == 32


def test_restart_recovers_persisted_level():
    for ts in (100.0, 105.0, 110.0):
        llm_concurrency.note_llm_rate_limit_failure(now=ts)  # -> 6
    persisted = json.loads(llm_concurrency._aimd_state_path().read_text())
    assert persisted["limit"] == 6
    # Simulate a process restart: all in-memory state gone, file intact.
    llm_concurrency._AIMD_LIMIT = None
    llm_concurrency._AIMD_FAILURE_TS = []
    llm_concurrency._AIMD_SUCCESSES = 0
    llm_concurrency._AIMD_LAST_LIMIT_CHANGE_TS = None
    llm_concurrency._SHARED_LLM_SEMAPHORE = None
    llm_concurrency._GLOBAL_LLM_SEMAPHORE = None
    assert llm_concurrency.get_capacity() == 6
    # The lazily-created semaphore materializes at the recovered level.
    sem = llm_concurrency.get_global_llm_semaphore()
    assert sem._value == 6
    assert llm_concurrency.get_active_stream_count() == 0


def test_recovery_clamps_out_of_range_levels(tmp_path):
    for limit in (1, 99):
        llm_concurrency._AIMD_STATE_FILE.write_text(json.dumps({"limit": limit}))
        llm_concurrency._AIMD_LIMIT = None
        assert llm_concurrency.get_capacity() == (2 if limit == 1 else 32)


def test_corrupt_state_file_fails_open_to_static():
    llm_concurrency._AIMD_STATE_FILE.write_text("{not json")
    llm_concurrency._AIMD_LIMIT = None
    assert llm_concurrency.get_capacity() == 12


def test_capacity_change_propagates_to_live_semaphore():
    sem = llm_concurrency.get_global_llm_semaphore()  # materialized at 12
    assert sem._value == 12
    for ts in (100.0, 105.0, 110.0):
        llm_concurrency.note_llm_rate_limit_failure(now=ts)  # -> 6
    assert sem._capacity == 6
    assert sem._value == 6
    assert llm_concurrency.get_capacity() == 6
    assert llm_concurrency.get_active_stream_count() == 0


# ---------------------------------------------------------------------------
# 3. Provider-result hooks in llm_query_retry
# ---------------------------------------------------------------------------

async def _noop_sleep(*_args, **_kwargs):
    return None


def _fake_gen():
    async def _gen():
        if False:  # never yields; _process_stream is monkeypatched anyway
            yield

    return _gen()


def _drive_stream(monkeypatch, process_stream):
    """Drive the real retry helper with a mocked provider stream."""
    import llm_query
    import llm_call_metrics

    monkeypatch.setattr(llm_query, "claude_query", lambda *a, **k: _fake_gen())
    monkeypatch.setattr(llm_query, "_process_stream", process_stream)
    monkeypatch.setattr(llm_query, "_emit_llm_event", lambda *a, **k: None)
    monkeypatch.setattr(
        llm_call_metrics, "record_llm_call_metrics", lambda **_kw: None
    )

    async def run():
        return await llm_query._run_stream_with_signature_retry(
            "prompt", ClaudeAgentOptions(), "/tmp/none.log", None, "role"
        )

    return asyncio.new_event_loop().run_until_complete(run())


@pytest.fixture
def aimd_reported(monkeypatch):
    calls = {"failure": 0, "success": 0}
    monkeypatch.setattr(
        llm_concurrency,
        "note_llm_rate_limit_failure",
        lambda now=None: calls.__setitem__("failure", calls["failure"] + 1),
    )
    monkeypatch.setattr(
        llm_concurrency,
        "note_llm_stream_success",
        lambda now=None: calls.__setitem__("success", calls["success"] + 1),
    )
    return calls


def test_hook_reports_1302_frequency_failure(monkeypatch, aimd_reported):
    async def fail_1302(query_gen, log_file_path, ui, role_name):
        raise ClaudeSDKError(
            "Request rejected (429) · [1302][您的账户已达到速率限制，请您控制请求频率]"
        )

    with pytest.raises(ClaudeSDKError):
        _drive_stream(monkeypatch, fail_1302)
    assert aimd_reported == {"failure": 1, "success": 0}


def test_hook_ignores_1308_quota_exhaustion(monkeypatch, aimd_reported):
    async def fail_1308(query_gen, log_file_path, ui, role_name):
        raise ClaudeSDKError(
            "Request rejected (429) · [1308][已达到 5 小时的使用上限。]"
        )

    with pytest.raises(ClaudeSDKError):
        _drive_stream(monkeypatch, fail_1308)
    # 1308 is a 5h full stop unrelated to concurrency — must NOT count.
    assert aimd_reported == {"failure": 0, "success": 0}


def test_hook_reports_bare_429(monkeypatch, aimd_reported):
    async def fail_bare(query_gen, log_file_path, ui, role_name):
        raise ClaudeSDKError("Request rejected (429)")

    with pytest.raises(ClaudeSDKError):
        _drive_stream(monkeypatch, fail_bare)
    assert aimd_reported == {"failure": 1, "success": 0}


def test_hook_reports_stream_success(monkeypatch, aimd_reported):
    async def ok_stream(query_gen, log_file_path, ui, role_name):
        return (["ok"], 0.0, {}, {})

    texts, _cost, _usage = _drive_stream(monkeypatch, ok_stream)
    assert texts == ["ok"]
    assert aimd_reported == {"failure": 0, "success": 1}


# ---------------------------------------------------------------------------
# 3b. Production error path (2026-10-07 downshift defect)
#
# In production a GLM 1302 arrives as a ClaudeSDKError, but _process_stream's
# OWN except-ClaudeSDKError handler classifies it via classify_llm_availability
# and re-raises LLMAvailabilityBlocked — a RuntimeError, NOT a ClaudeSDKError
# subclass (llm_query_retry.py:682-700). The retry loop's except-ClaudeSDKError
# AIMD hook (llm_query_retry.py:1208-1222) therefore never sees it: production
# events.jsonl logged 2293 (service_unavailable, ClaudeSDKError) availability
# blocks and ZERO keyword-matched raw SDK errors, so the multiplicative
# downshift never fired while storms hit 13-18 blocks/minute. The
# frequency-failure report must live where the wrapped error actually flows:
# run_claude_query's except-LLMAvailabilityBlocked handler (llm_query.py).
# ---------------------------------------------------------------------------

_GLM_1302_BODY = (
    "Request rejected (429) · [1302][您的账户已达到速率限制，请您控制请求频率]"
)
_GLM_1308_BODY = (
    "Request rejected (429) · [1308][已达到 5 小时的使用上限。"
    "您的限额将在 2026-10-07 23:59:59 重置。]"
)


class _UI:
    def log_history(self, *_args, **_kwargs):
        return None

    def log_io(self, *_args, **_kwargs):
        return None

    def emit_tool_call(self, *_args, **_kwargs):
        return None

    def update_cost(self, *_args, **_kwargs):
        return None


def _wrapped_like_production(body, role="SATURATOR STRATEGY RESEARCH"):
    """Reproduce the exact _process_stream conversion (llm_query_retry.py:682):
    a ClaudeSDKError carrying the raw provider body is classified by the real
    LLMAvailabilityTrace and re-raised as LLMAvailabilityBlocked."""
    from llm_availability import LLMAvailabilityTrace

    blocked = LLMAvailabilityTrace().blocked(
        role=role, exception=ClaudeSDKError(body)
    )
    assert blocked is not None, "production body must classify"
    return blocked


def _archivist_prompt():
    """Lightest self-contained rendered role prompt (same route as
    test_llm_zero_tools)."""
    import cycle_archivist
    import llm_query
    from bot_namespace import bot_name, bot_tag

    snapshot = cycle_archivist._cycle_archivist_prompt_projection(
        {
            "evaluation_epoch": "national_tcp_policy_v1",
            "bot_name": bot_name(149),
            "git_tag": bot_tag(149),
            "publication_identity": {
                "publication_id": "1" * 64,
                "commit_oid": "2" * 40,
                "candidate_artifact_hash": "3" * 64,
            },
            "strength_evidence_identity": {"marker": "aimd downshift"},
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
        "CYCLE ARCHIVIST",
        producer=cycle_archivist._render_cycle_archivist_provider_prompt,
        renderer_inputs={"snapshot": snapshot, "version": 149, "source_v": 143},
    )


@pytest.fixture
def run_query_env(monkeypatch):
    """Isolate run_claude_query from cost policy, the pause store, the rate
    limiter, and the event bus so the availability handler can be driven."""
    import llm_availability_store
    import llm_query
    import orchestrator_cost_policy
    import rate_limiter

    monkeypatch.setattr(
        orchestrator_cost_policy,
        "assert_operator_cost_limit_available",
        lambda: None,
    )
    monkeypatch.setattr(
        llm_availability_store, "raise_if_llm_paused", lambda **_kwargs: None
    )
    monkeypatch.setattr(
        llm_availability_store, "persist_llm_pause", lambda _e: {"ok": True}
    )
    monkeypatch.setattr(rate_limiter.rate_limiter, "is_blocked", lambda: False)
    monkeypatch.setattr(llm_query, "_emit_llm_event", lambda *a, **k: None)
    return llm_query


def _run_wrapped_dispatch(llm_query, monkeypatch, body, tmp_path):
    async def blocked_stream(*_args, **_kwargs):
        raise _wrapped_like_production(body)

    monkeypatch.setattr(
        llm_query, "_run_stream_with_signature_retry", blocked_stream
    )
    from llm_availability import LLMAvailabilityBlocked

    with pytest.raises(LLMAvailabilityBlocked):
        asyncio.run(
            llm_query.run_claude_query(
                _archivist_prompt(),
                [],
                _UI(),
                "CYCLE ARCHIVIST",
                str(tmp_path / "aimd_io.txt"),
            )
        )


def test_three_wrapped_1302_dispatches_halve_the_limit(
    monkeypatch, tmp_path, run_query_env
):
    """The production-shaped failure (Chinese 1302 body -> classify ->
    LLMAvailabilityBlocked) must drive the real AIMD window: three clustered
    dispatch failures halve the live limit and persist it."""
    llm_query = run_query_env
    for _ in range(3):
        _run_wrapped_dispatch(llm_query, monkeypatch, _GLM_1302_BODY, tmp_path)
    assert llm_concurrency.get_capacity() == 6  # 12 -> 6, multiplicative
    persisted = json.loads(llm_concurrency._aimd_state_path().read_text())
    assert persisted["limit"] == 6


def test_wrapped_1302_counts_exactly_once_per_dispatch(
    monkeypatch, tmp_path, run_query_env, aimd_reported
):
    """One failed dispatch reports exactly one frequency failure — no double
    counting from the retry loop's raw-SDK hook or anywhere else."""
    llm_query = run_query_env
    _run_wrapped_dispatch(llm_query, monkeypatch, _GLM_1302_BODY, tmp_path)
    assert aimd_reported == {"failure": 1, "success": 0}


def test_wrapped_1302_reports_api_concurrency_rate_limit_once(
    monkeypatch, tmp_path, run_query_env
):
    """The same SERVICE_UNAVAILABLE dispatch must also feed the legacy
    api_concurrency backoff level (agent_workers' get_adaptive_limit) exactly
    once — its documented wiring point (api_concurrency.py docstring) is
    run_claude_query; the retry loop's raw-SDK site never fires for classified
    errors, so this is the only failure feed. The success=True recovery counter
    stays on the success path (llm_query_retry.py) and is not touched here."""
    import api_concurrency

    calls = []
    monkeypatch.setattr(
        api_concurrency,
        "record_llm_outcome",
        lambda success, rate_limited=False: calls.append((success, rate_limited)),
    )
    llm_query = run_query_env
    _run_wrapped_dispatch(llm_query, monkeypatch, _GLM_1302_BODY, tmp_path)
    assert calls == [(False, True)]  # exactly one, frequency-class failure
    # A quota-class dispatch shares the same handler but must NOT feed the
    # backoff level (only SERVICE_UNAVAILABLE counts, same gate as AIMD).
    _run_wrapped_dispatch(llm_query, monkeypatch, _GLM_1308_BODY, tmp_path)
    assert calls == [(False, True)]  # unchanged — 1308 excluded


def test_wrapped_1308_quota_pause_never_downshifts(
    monkeypatch, tmp_path, run_query_env
):
    """1308 (five-hour usage cap with a provider reset timestamp) classifies as
    quota_429 — a full stop unrelated to concurrency — and must not enter the
    frequency window even after many dispatch failures."""
    llm_query = run_query_env
    blocked = _wrapped_like_production(_GLM_1308_BODY)
    assert blocked.issue.category == "quota_429"  # classifier separates 1308
    for _ in range(5):
        _run_wrapped_dispatch(llm_query, monkeypatch, _GLM_1308_BODY, tmp_path)
    assert llm_concurrency.get_capacity() == 12  # untouched


def test_pre_dispatch_pause_replay_does_not_count(
    monkeypatch, tmp_path, run_query_env, aimd_reported
):
    """Idempotency: while a durable pause is active, raise_if_llm_paused
    replays the SAME failure as LLMAvailabilityBlocked before the try block —
    those replays must not re-enter the window (only fresh dispatch failures
    observed inside a stream count)."""
    import llm_availability_store

    llm_query = run_query_env

    def replay_pause(**_kwargs):
        raise _wrapped_like_production(_GLM_1302_BODY)

    monkeypatch.setattr(llm_availability_store, "raise_if_llm_paused", replay_pause)
    from llm_availability import LLMAvailabilityBlocked

    with pytest.raises(LLMAvailabilityBlocked):
        asyncio.run(
            llm_query.run_claude_query(
                _archivist_prompt(),
                [],
                _UI(),
                "CYCLE ARCHIVIST",
                str(tmp_path / "aimd_io.txt"),
            )
        )
    assert aimd_reported == {"failure": 0, "success": 0}


def test_wrapped_1302_through_real_process_stream_bypasses_retry_loop_hook(
    monkeypatch, aimd_reported
):
    """Layer characterization: driving the REAL _process_stream (not mocked),
    the 1302 ClaudeSDKError escapes the retry helper already wrapped as
    LLMAvailabilityBlocked and is NOT counted at the retry loop's raw-SDK hook
    — the count belongs to run_claude_query's availability handler above."""
    import llm_call_metrics
    import llm_query
    from llm_availability import LLMAvailabilityBlocked

    def raising_gen():
        async def _gen():
            raise ClaudeSDKError(_GLM_1302_BODY)
            yield  # pragma: no cover - makes this an async generator

        return _gen()

    monkeypatch.setattr(llm_query, "claude_query", lambda *a, **k: raising_gen())
    monkeypatch.setattr(llm_query, "_emit_llm_event", lambda *a, **k: None)
    monkeypatch.setattr(
        llm_call_metrics, "record_llm_call_metrics", lambda **_kw: None
    )

    async def run():
        return await llm_query._run_stream_with_signature_retry(
            "prompt", ClaudeAgentOptions(), "/tmp/none.log", _UI(), "role"
        )

    with pytest.raises(LLMAvailabilityBlocked) as excinfo:
        asyncio.new_event_loop().run_until_complete(run())
    assert excinfo.value.issue.category == "service_unavailable"
    assert excinfo.value.issue.summary == "provider rate limit; reduce request frequency"
    # The retry loop's own hook must stay silent on the wrapped path.
    assert aimd_reported == {"failure": 0, "success": 0}


# ---------------------------------------------------------------------------
# 4. Saturator launch gates on the dynamic capacity
# ---------------------------------------------------------------------------

def test_saturator_soft_cap_follows_dynamic_capacity(monkeypatch):
    monkeypatch.setenv("POK_LLM_SATURATOR_MAX_INFLIGHT", "8")
    # Global downshift propagates into background fill.
    assert llm_saturator.saturator_soft_cap(6, 12) == 6
    # Static steady state (deploy: eff 12, env inflight 8).
    assert llm_saturator.saturator_soft_cap(12, 12) == 8
    # Probing past the static cap: saturator gets env inflight + the surplus
    # beyond static (the 4-permit pipeline reserve stays reserved).
    assert llm_saturator.saturator_soft_cap(16, 12) == 12
    assert llm_saturator.saturator_soft_cap(2, 12) == 2
    assert llm_saturator.saturator_soft_cap(0, 12) == 1  # never below 1


def test_may_launch_children_gate_tracks_dynamic_capacity(monkeypatch):
    llm_concurrency._AIMD_STATE_FILE.write_text(json.dumps({"limit": 6}))
    assert llm_concurrency.get_capacity() == 6

    monkeypatch.setattr(llm_saturator, "_saturator_provider_paused", lambda: False)
    monkeypatch.setattr(llm_saturator, "_mem_available_mb", lambda: 2048)
    monkeypatch.setattr(llm_saturator, "_min_free_mb", lambda: 512)
    monkeypatch.setattr(
        llm_saturator, "_cgroup_memory_headroom_mb", lambda **_kw: None
    )
    monkeypatch.setattr(llm_saturator, "_claude_child_count", lambda: 7)
    ok, reason = llm_saturator.saturator_may_launch(in_flight=0, soft_cap=6)
    assert (ok, reason) == (False, "claude_children")

    # Below the DYNAMIC cap (7 children >= static 12 would have passed!).
    monkeypatch.setattr(llm_saturator, "_claude_child_count", lambda: 5)
    ok, reason = llm_saturator.saturator_may_launch(in_flight=0, soft_cap=6)
    assert (ok, reason) == (True, "ok")


def test_cgroup_headroom_parses_v2_unified_path(tmp_path):
    root = tmp_path / "cgroup"
    svc = root / "system.slice" / "pok-evolution.service"
    svc.mkdir(parents=True)
    # Deploy fact: memory.high = 2516582400 (2400 MiB) on this service.
    (svc / "memory.high").write_text("2516582400\n")
    (svc / "memory.current").write_text(str(1271 * 1024 * 1024) + "\n")  # 1271 MiB
    cg = tmp_path / "self_cgroup"
    cg.write_text("0::/system.slice/pok-evolution.service\n")
    headroom = llm_saturator._cgroup_memory_headroom_mb(
        cgroup_file=str(cg), cgroup_root=str(root)
    )
    assert headroom == 2400 - 1271


def test_cgroup_headroom_fails_open_without_limit(tmp_path):
    root = tmp_path / "cgroup"
    svc = root / "system.slice" / "pok-evolution.service"
    svc.mkdir(parents=True)
    cg = tmp_path / "self_cgroup"
    cg.write_text("0::/system.slice/pok-evolution.service\n")
    # memory.high = "max" → no cgroup limit → gate inactive.
    (svc / "memory.high").write_text("max\n")
    assert llm_saturator._cgroup_memory_headroom_mb(
        cgroup_file=str(cg), cgroup_root=str(root)
    ) is None
    # cgroup v1 / no unified path → inactive.
    cg.write_text("1:name=systemd:/system.slice/pok-evolution.service\n")
    assert llm_saturator._cgroup_memory_headroom_mb(
        cgroup_file=str(cg), cgroup_root=str(root)
    ) is None
    # Missing memory.high file entirely → inactive.
    cg.write_text("0::/system.slice/nonexistent.service\n")
    assert llm_saturator._cgroup_memory_headroom_mb(
        cgroup_file=str(cg), cgroup_root=str(root)
    ) is None


def test_may_launch_cgroup_memory_gate(monkeypatch):
    monkeypatch.setattr(llm_saturator, "_saturator_provider_paused", lambda: False)
    monkeypatch.setattr(llm_saturator, "_mem_available_mb", lambda: 2048)
    monkeypatch.setattr(llm_saturator, "_min_free_mb", lambda: 512)
    monkeypatch.setattr(llm_saturator, "_claude_child_count", lambda: 0)

    monkeypatch.setattr(
        llm_saturator, "_cgroup_memory_headroom_mb", lambda **_kw: 100
    )
    ok, reason = llm_saturator.saturator_may_launch(in_flight=0, soft_cap=8)
    assert (ok, reason) == (False, "low_memory_cgroup")

    # Enough headroom (default floor is 300 MB) → launches proceed.
    monkeypatch.setattr(
        llm_saturator, "_cgroup_memory_headroom_mb", lambda **_kw: 400
    )
    ok, reason = llm_saturator.saturator_may_launch(in_flight=0, soft_cap=8)
    assert (ok, reason) == (True, "ok")

    # Unresolvable cgroup → fail-open (the MemAvailable gate remains).
    monkeypatch.setattr(
        llm_saturator, "_cgroup_memory_headroom_mb", lambda **_kw: None
    )
    ok, reason = llm_saturator.saturator_may_launch(in_flight=0, soft_cap=8)
    assert (ok, reason) == (True, "ok")


def test_cgroup_headroom_env_override(monkeypatch):
    monkeypatch.setattr(
        llm_saturator, "_cgroup_memory_headroom_mb", lambda **_kw: 400
    )
    monkeypatch.setattr(llm_saturator, "_saturator_provider_paused", lambda: False)
    monkeypatch.setattr(llm_saturator, "_mem_available_mb", lambda: 2048)
    monkeypatch.setattr(llm_saturator, "_min_free_mb", lambda: 512)
    monkeypatch.setattr(llm_saturator, "_claude_child_count", lambda: 0)
    monkeypatch.setenv("POK_LLM_AIMD_CGROUP_HEADROOM_MB", "500")
    ok, reason = llm_saturator.saturator_may_launch(in_flight=0, soft_cap=8)
    assert (ok, reason) == (False, "low_memory_cgroup")


def test_saturator_loop_wires_dynamic_soft_cap():
    import inspect

    source = inspect.getsource(llm_saturator.run_llm_saturator)
    assert "saturator_soft_cap(" in source
    assert "get_capacity" in source
    gate_source = inspect.getsource(llm_saturator.saturator_may_launch)
    assert "get_capacity()" in gate_source


def test_fast_recovery_pace_below_static_baseline():
    """Operator direction 2026-10-07: climb back faster after a storm.

    Below the static baseline the raise probe fires after the fast 90s pace,
    not the cautious 300s exploration pace — a storm's downshift lands far
    under the sustained level and the slow pace made the dip ~50 minutes.
    """
    llm_concurrency._AIMD_STATE_FILE.write_text(json.dumps({"limit": 3}))
    llm_concurrency._AIMD_LIMIT = None
    llm_concurrency._AIMD_LAST_LIMIT_CHANGE_TS = 1000.0
    llm_concurrency._AIMD_SUCCESSES = 0
    for _ in range(4):
        llm_concurrency.note_llm_stream_success(now=1091.0)  # 91s since change
    assert llm_concurrency.get_capacity() == 4


def test_cautious_pace_returns_at_static_baseline():
    """Fast recovery is for getting BACK to the static baseline, not for
    exploring above it: at limit >= static the 300s probe pace applies."""
    llm_concurrency._AIMD_STATE_FILE.write_text(json.dumps({"limit": 12}))
    llm_concurrency._AIMD_LIMIT = None
    llm_concurrency._AIMD_LAST_LIMIT_CHANGE_TS = 1000.0
    llm_concurrency._AIMD_SUCCESSES = 0
    for _ in range(4):
        llm_concurrency.note_llm_stream_success(now=1080.0)  # only 80s later
    assert llm_concurrency.get_capacity() == 12
