"""Tests for the single shared-pool LLM concurrency limiter.

The former producer/consumer hard partition left slots idle during temporally-
separated pipeline phases (Master/Workers vs gates).  A single shared FIFO
semaphore lets every permit fill whichever role has work, roughly doubling
real utilization for the same permit count.
"""

import asyncio
import importlib
import json
import os
import threading
import time

import pytest

import llm_concurrency


@pytest.fixture(autouse=True)
def _reset_module(monkeypatch):
    """Reset the lazily-created semaphore between tests."""
    monkeypatch.setattr(llm_concurrency, "_SHARED_LLM_SEMAPHORE", None)
    monkeypatch.setattr(llm_concurrency, "_GLOBAL_LLM_SEMAPHORE", None)
    yield


def test_shared_pool_all_roles_get_same_semaphore():
    """All roles — producer and consumer alike — share one semaphore."""
    sem_global = llm_concurrency.get_global_llm_semaphore()
    sem_review = llm_concurrency.get_llm_semaphore_for_role("review")
    sem_critic = llm_concurrency.get_llm_semaphore_for_role("critic")
    sem_master = llm_concurrency.get_llm_semaphore_for_role("master")
    sem_worker = llm_concurrency.get_llm_semaphore_for_role("worker")
    sem_direction = llm_concurrency.get_llm_semaphore_for_role("direction_audit")
    sem_none = llm_concurrency.get_llm_semaphore_for_role(None)
    # Since 2026-08-16 pipeline roles share the underlying FIFO pool through
    # the _PipelinePrioritySemaphore wrapper (queue-pending visibility for
    # background-fill preemption); only background fill (SATURATOR) gets the
    # raw object. "Same pool" means same underlying semaphore.
    for sem in (sem_review, sem_critic, sem_master, sem_worker, sem_direction, sem_none):
        assert getattr(sem, "_sem", sem) is sem_global


def test_legacy_aliases_return_shared_semaphore():
    """The legacy get_consumer/get_producer aliases return the same shared pool."""
    shared = llm_concurrency.get_global_llm_semaphore()
    consumer = llm_concurrency.get_consumer_llm_semaphore()
    producer = llm_concurrency.get_producer_llm_semaphore()
    assert consumer is shared
    assert producer is shared
    assert consumer is producer


def test_semaphore_capacity_matches_config():
    """The shared semaphore materializes at the dynamic (AIMD) capacity.

    Since 2026-10-07 the capacity accessor is ``get_capacity()`` — the AIMD
    controller's live limit (fail-open to GLOBAL_LLM_CONCURRENCY when no
    ``llm_aimd_state.json`` exists), and the semaphore is created with it.
    """
    sem = llm_concurrency.get_global_llm_semaphore()
    assert sem._value == llm_concurrency.get_capacity()


def test_master_proposal_critic_uses_shared_pool():
    """All Master proposal roles (Scouts, Critics, final) use the shared pool."""
    shared = llm_concurrency.get_global_llm_semaphore()

    def _pool(role):
        sem = llm_concurrency.get_llm_semaphore_for_role(role)
        return getattr(sem, "_sem", sem)

    assert _pool("MASTER PROPOSAL CRITIC falsification") is shared
    assert _pool("MASTER PROPOSAL mechanism") is shared
    assert _pool("STRATEGY CRITIC") is shared


def test_cross_loop_acquire_does_not_bind_to_one_event_loop():
    """v298: saturator on ASGI + crossover on a private loop must share the cap.

    asyncio.Semaphore only binds its loop on the first *contended* wait, so the
    pipeline waiter permanently owns the object and ASGI acquire then raises
    ``bound to a different event loop``. CrossLoopSemaphore must not.
    """
    from concurrent.futures import ThreadPoolExecutor, wait

    sem = llm_concurrency.CrossLoopSemaphore(1)
    started = threading.Event()
    released = threading.Event()

    def holder():
        async def run():
            await sem.acquire()
            started.set()
            assert released.wait(timeout=5)
            sem.release()
        asyncio.run(run())

    def waiter():
        async def run():
            assert started.wait(timeout=5)
            await sem.acquire()
            sem.release()
            return "ok"
        return asyncio.run(run())

    with ThreadPoolExecutor(max_workers=2) as pool:
        h = pool.submit(holder)
        w = pool.submit(waiter)
        assert started.wait(timeout=5)
        # Contended wait is in flight on a different loop than the holder.
        time.sleep(0.05)
        released.set()
        done, _ = wait([h, w], timeout=5)
        assert h in done and w in done
        assert w.result() == "ok"
        assert h.exception() is None
    assert sem._value == 1


def test_cancelled_waiter_does_not_leak_permit():
    sem = llm_concurrency.CrossLoopSemaphore(1)

    async def scenario():
        await sem.acquire()

        async def blocked():
            await sem.acquire()

        task = asyncio.create_task(blocked())
        await asyncio.sleep(0.05)
        assert sem._value == 0
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        sem.release()
        assert sem._value == 1
        await sem.acquire()
        sem.release()
        assert sem._value == 1

    asyncio.run(scenario())


# --- 2026-10-21 解限流: aggressive AIMD defaults + env knobs -------------------
#
# Every AIMD pacing constant became env-tunable with the AGGRESSIVE posture
# as the CODE default (no env required): floor 8 (was 2), fast recovery 30s
# (was 90s), cautious probe 60s (was 300s). The discipline constants are
# unchanged: ceiling 32, window 300s, failure threshold 3, min successes 4.
# The 1308 quota chain, the memory-gradient gates, and the pipeline durable
# pause curve are locked unchanged in
# test_protected_throttle_semantics_unchanged below.

_AIMD_ENV_KEYS = (
    "POK_GLOBAL_LLM_CONCURRENCY",
    "POK_LLM_AIMD_MIN_LIMIT",
    "POK_LLM_AIMD_MAX_LIMIT",
    "POK_LLM_AIMD_WINDOW_SEC",
    "POK_LLM_AIMD_FAILURE_THRESHOLD",
    "POK_LLM_AIMD_RAISE_INTERVAL_SEC",
    "POK_LLM_AIMD_FAST_RAISE_INTERVAL_SEC",
    "POK_LLM_AIMD_RAISE_MIN_SUCCESSES",
)


@pytest.fixture
def aimd_reload(monkeypatch):
    """Reload llm_concurrency with a clean/overridden AIMD env.

    The knobs are import-time env reads, so overrides are only observable
    through a reload. Teardown pops every knob explicitly (independent of
    monkeypatch undo ordering) and reloads once more so later tests see the
    canonical defaults.
    """

    def _load(**env):
        for key in _AIMD_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, str(value))
        return importlib.reload(llm_concurrency)

    yield _load
    for key in _AIMD_ENV_KEYS:
        os.environ.pop(key, None)
    importlib.reload(llm_concurrency)


def test_aimd_aggressive_defaults_without_env(aimd_reload):
    """No env -> the new aggressive defaults ARE the code defaults."""
    mod = aimd_reload()
    # Discipline constants unchanged (供应商纪律本体, review-confirmed keep).
    assert mod.AIMD_MAX_LIMIT == 32
    assert mod.AIMD_WINDOW_SEC == 300.0
    assert mod.AIMD_FAILURE_THRESHOLD == 3
    assert mod.AIMD_RAISE_MIN_SUCCESSES == 4
    # Aggressive pacing — effective with NO env at all.
    assert mod.AIMD_RAISE_INTERVAL_SEC == 60.0
    assert mod.AIMD_FAST_RAISE_INTERVAL_SEC == 30.0
    # Floor default 8, clamped by the static ceiling (review 必改2): a bare
    # process (GLOBAL code default 4) cannot floor above its own fail-open
    # recovery limit; production (GLOBAL 16) gets the full 8.
    assert mod.AIMD_MIN_LIMIT == min(8, mod.GLOBAL_LLM_CONCURRENCY)


def test_aimd_min_limit_default_eight_under_production_ceiling(aimd_reload):
    """Under the production static cap (env.runtime GLOBAL=16) the unclamped
    floor default 8 materializes — the deployed learned level of 5-6 is
    re-floored to 8 at the next process start."""
    mod = aimd_reload(POK_GLOBAL_LLM_CONCURRENCY=16)
    assert mod.GLOBAL_LLM_CONCURRENCY == 16
    assert mod.AIMD_MIN_LIMIT == 8


def test_aimd_min_limit_clamped_to_global_concurrency(aimd_reload):
    """Review 必改2 guard: exporting only POK_LLM_AIMD_MIN_LIMIT=8 while
    GLOBAL stays lower must not split the controller's floor from
    _aimd_load's fail-open recovery limit (4-vs-8 divergence pre-clamp)."""
    mod = aimd_reload(POK_GLOBAL_LLM_CONCURRENCY=4, POK_LLM_AIMD_MIN_LIMIT=8)
    assert mod.AIMD_MIN_LIMIT == 4
    assert mod.AIMD_MIN_LIMIT <= mod.GLOBAL_LLM_CONCURRENCY


def test_aimd_env_overrides(aimd_reload):
    mod = aimd_reload(
        POK_GLOBAL_LLM_CONCURRENCY=16,
        POK_LLM_AIMD_MIN_LIMIT=5,
        POK_LLM_AIMD_MAX_LIMIT=24,
        POK_LLM_AIMD_WINDOW_SEC=600,
        POK_LLM_AIMD_FAILURE_THRESHOLD=5,
        POK_LLM_AIMD_RAISE_INTERVAL_SEC=120,
        POK_LLM_AIMD_FAST_RAISE_INTERVAL_SEC=45,
        POK_LLM_AIMD_RAISE_MIN_SUCCESSES=7,
    )
    assert mod.AIMD_MIN_LIMIT == 5
    assert mod.AIMD_MAX_LIMIT == 24
    assert mod.AIMD_WINDOW_SEC == 600.0
    assert mod.AIMD_FAILURE_THRESHOLD == 5
    assert mod.AIMD_RAISE_INTERVAL_SEC == 120.0
    assert mod.AIMD_FAST_RAISE_INTERVAL_SEC == 45.0
    assert mod.AIMD_RAISE_MIN_SUCCESSES == 7


def test_aimd_floor_raises_persisted_limit_on_recovery(
    aimd_reload, monkeypatch, tmp_path
):
    """The deployed AIMD state sat at 5-6 while permits idled: the new floor 8
    lifts the recovered limit at load time (no storm required), and a storm
    at the floor stays floored — the downshift step never digs below 8."""
    mod = aimd_reload(POK_GLOBAL_LLM_CONCURRENCY=16)
    state = tmp_path / "llm_aimd_state.json"
    state.write_text(json.dumps({"limit": 6}), encoding="utf-8")
    monkeypatch.setattr(mod, "_AIMD_STATE_FILE", state)
    monkeypatch.setattr(mod, "_AIMD_LIMIT", None)
    monkeypatch.setattr(mod, "_AIMD_LAST_LIMIT_CHANGE_TS", None)
    assert mod.get_aimd_limit() == 8  # raised at load, was 6 pre-change
    for ts in (100.0, 105.0, 110.0):
        mod.note_llm_rate_limit_failure(now=ts)  # clustered storm
    assert mod.get_aimd_limit() == 8  # 8 - max(2, 8 // 4) = 6 -> floored at 8


def test_aimd_fast_recovery_pace_by_default(aimd_reload, monkeypatch, tmp_path):
    """30s fast tier (was 90s): below the static baseline, four clean
    successes 31s after the last change already probe +1; 29s does not."""
    mod = aimd_reload(POK_GLOBAL_LLM_CONCURRENCY=16)
    state = tmp_path / "llm_aimd_state.json"
    state.write_text(
        json.dumps({"limit": 10, "last_change_ts": 1000.0}), encoding="utf-8"
    )
    monkeypatch.setattr(mod, "_AIMD_STATE_FILE", state)
    monkeypatch.setattr(mod, "_AIMD_LIMIT", None)
    monkeypatch.setattr(mod, "_AIMD_LAST_LIMIT_CHANGE_TS", None)
    monkeypatch.setattr(mod, "_AIMD_SUCCESSES", 0)
    assert mod.get_aimd_limit() == 10
    for _ in range(4):
        mod.note_llm_stream_success(now=1029.0)  # 29s < 30s: still gated
    assert mod.get_aimd_limit() == 10
    mod.note_llm_stream_success(now=1031.0)  # 31s >= 30s: probe fires
    assert mod.get_aimd_limit() == 11


def test_aimd_cautious_probe_pace_by_default(aimd_reload, monkeypatch, tmp_path):
    """60s cautious tier (was 300s) at/above the static baseline."""
    mod = aimd_reload(POK_GLOBAL_LLM_CONCURRENCY=16)
    state = tmp_path / "llm_aimd_state.json"
    state.write_text(
        json.dumps({"limit": 16, "last_change_ts": 1000.0}), encoding="utf-8"
    )
    monkeypatch.setattr(mod, "_AIMD_STATE_FILE", state)
    monkeypatch.setattr(mod, "_AIMD_LIMIT", None)
    monkeypatch.setattr(mod, "_AIMD_LAST_LIMIT_CHANGE_TS", None)
    monkeypatch.setattr(mod, "_AIMD_SUCCESSES", 0)
    assert mod.get_aimd_limit() == 16
    for _ in range(4):
        mod.note_llm_stream_success(now=1059.0)  # 59s < 60s: still gated
    assert mod.get_aimd_limit() == 16
    mod.note_llm_stream_success(now=1061.0)  # 61s >= 60s: probe fires
    assert mod.get_aimd_limit() == 17


def test_protected_throttle_semantics_unchanged(monkeypatch):
    """1308 / 内存 / 管线可用性语义零变化 locks (ask requirement).

    The 解限流 change must not touch: the shared frequency cooldown curve
    (pipeline durable-pause authority, cap 120 in llm_availability), the
    saturator's 1308 quota pause, the memory-gradient gate code defaults, or
    the quota pacer's disabled default ('宁要高峰', env.runtime budget 0).
    """
    from llm_availability import service_unavailable_cooldown_seconds as curve

    assert [curve(n) for n in range(1, 7)] == [8, 16, 32, 64, 120, 120]

    import llm_saturator

    assert llm_saturator._QUOTA_PAUSE_SECONDS == 600.0
    for key in (
        "POK_LLM_AIMD_CGROUP_HEADROOM_MB",
        "POK_LLM_SATURATOR_MEMORY_SOFT_FLOOR_MB",
        "POK_LLM_SATURATOR_LOW_MEMORY_INFLIGHT",
        "POK_LLM_SATURATOR_STREAM_RSS_MB",
        "POK_LLM_SATURATOR_BAND_OVERSHOOT_MB",
        "POK_LLM_SATURATOR_MIN_FREE_MB",
        "POK_LLM_QUOTA_WINDOW_BUDGET_TOKENS",
    ):
        monkeypatch.delenv(key, raising=False)
    assert llm_saturator._cgroup_headroom_mb() == 300
    assert llm_saturator._cgroup_memory_soft_floor_mb() == 150
    assert llm_saturator._low_memory_inflight_cap() == 1
    assert llm_saturator._saturator_stream_rss_mb() == 250
    assert llm_saturator._saturator_band_overshoot_mb() == 0
    assert llm_saturator._min_free_mb() == 512
    assert llm_concurrency._quota_window_budget_tokens() == 0
