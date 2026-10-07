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
