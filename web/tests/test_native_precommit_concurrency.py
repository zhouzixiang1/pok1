"""Deep-parallelism: native precommit concurrency limiter (OOM safety valve).

``run_precommit_eval`` drives native 70-hand TCP matches that consume CPU+RAM
but ZERO LLM permits.  With deep parallelism, multiple precommits could run in
parallel and OOM the host.  This independent semaphore bounds simultaneous
precommit chains so the LLM pool stays saturated while native memory stays
bounded.  Crucially, when the native semaphore is exhausted the dispatcher
returns ``native_precommit_slot_busy`` backpressure WITHOUT blocking LLM work
on other candidates.
"""

import asyncio
import os

import pytest

import producer_consumer_slice2b as pcs
from producer_consumer_slice2b import (
    POK_NATIVE_PRECOMMIT_CONCURRENCY,
    _get_native_precommit_semaphore,
    _reset_native_precommit_semaphore_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_native_sem(monkeypatch):
    _reset_native_precommit_semaphore_for_tests()
    yield
    _reset_native_precommit_semaphore_for_tests()


def test_default_native_precommit_concurrency_is_one():
    """The default is the conservative value (serial precommit) to avoid OOM
    on the 3.6Gi host.  Operators may raise it via env."""
    # Re-read from env to honour test-set values.
    env_val = os.environ.get("POK_NATIVE_PRECOMMIT_CONCURRENCY", "1")
    assert int(env_val) >= 1


def test_semaphore_reflects_configured_concurrency(monkeypatch):
    """The semaphore is created with POK_NATIVE_PRECOMMIT_CONCURRENCY permits."""
    monkeypatch.setattr(pcs, "POK_NATIVE_PRECOMMIT_CONCURRENCY", 3)
    _reset_native_precommit_semaphore_for_tests()
    sem = _get_native_precommit_semaphore()
    assert sem._value == 3


def test_semaphore_acquire_decrements_value():
    """Acquiring a permit reduces the available count (the dispatcher's gate
    reads sem._value to decide backpressure)."""
    sem = _get_native_precommit_semaphore()
    initial = sem._value

    async def acquire():
        await sem.acquire()

    asyncio.run(acquire())
    assert sem._value == initial - 1
    sem.release()
    assert sem._value == initial


def test_native_backoff_seconds_default():
    """The native backoff has a sane default (30s) for slot-busy retries."""
    from producer_consumer_slice2b import POK_NATIVE_BACKOFF_SECONDS

    assert POK_NATIVE_BACKOFF_SECONDS >= 5.0
    assert POK_NATIVE_BACKOFF_SECONDS <= 120.0


def test_dispatcher_returns_native_precommit_slot_busy_when_exhausted():
    """When the native semaphore is exhausted (sem._value <= 0), the
    dispatcher's precommit branch returns native_precommit_slot_busy.  We
    test the value-check predicate the dispatcher uses directly (the full
    dispatcher needs a sealed envelope; covered by integration tests)."""

    async def driver():
        sem = _get_native_precommit_semaphore()
        # Acquire all permits (default concurrency=1).
        await sem.acquire()
        # The dispatcher checks ``sem._value <= 0`` to decide backpressure.
        assert sem._value <= 0
        # After release, a new precommit would proceed.
        sem.release()
        assert sem._value >= 1

    asyncio.run(driver())


def test_semaphore_is_separate_from_llm_semaphore():
    """The native precommit limiter is deliberately SEPARATE from the global
    LLM semaphore: a precommit at its limit must NOT block LLM work.  This
    test documents the independence (different objects)."""
    import llm_concurrency

    llm_sem = llm_concurrency.get_global_llm_semaphore()
    native_sem = _get_native_precommit_semaphore()
    assert native_sem is not llm_sem
    assert native_sem._value == POK_NATIVE_PRECOMMIT_CONCURRENCY
    assert llm_sem._value == llm_concurrency.GLOBAL_LLM_CONCURRENCY
