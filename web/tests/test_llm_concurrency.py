"""Tests for the single shared-pool LLM concurrency limiter.

The former producer/consumer hard partition left slots idle during temporally-
separated pipeline phases (Master/Workers vs gates).  A single shared FIFO
semaphore lets every permit fill whichever role has work, roughly doubling
real utilization for the same permit count.
"""

import asyncio
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
    """The shared semaphore capacity equals GLOBAL_LLM_CONCURRENCY."""
    sem = llm_concurrency.get_global_llm_semaphore()
    assert sem._value == llm_concurrency.GLOBAL_LLM_CONCURRENCY


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
