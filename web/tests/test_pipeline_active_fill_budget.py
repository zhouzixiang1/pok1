"""Pipeline-active fill budget (2026-10-23, second layer of producer priority).

The rate-yield (70e14eff) covers a durable-pause window plus a fixed grace,
but a master ensemble / workers phase streams for 5-10+ minutes; the moment
the yield expired the fill lane refilled to the soft cap and its request
wave re-tripped GLM 1302 onto the still-running pipeline streams (observed
19:29 / 19:44 / 20:00). This layer counts HELD pipeline permits
(``pipeline_active_count`` — the ``_PipelinePrioritySemaphore`` active
counter) and keeps in-flight fill at or below
``POK_LLM_SATURATOR_PIPELINE_ACTIVE_INFLIGHT`` (default 2) while the
producer streams.

Contract:

* The active counter pairs exactly under async-with semantics: acquire
  success takes it, every exit path (body return, body raise, manual
  ``release()``) returns it; a failed acquire never takes it.
* ``pipeline_active_count`` reconciles leaks: clamp to the semaphore's real
  holders, hard reset when zero permits are held, hard reset (with a log)
  after ``POK_LLM_PIPELINE_ACTIVE_STALE_SEC`` stuck positive.
* ``saturator_may_launch(pipeline_active=...)`` refuses with
  ``pipeline_rate_budget`` once in-flight fill is at the cap while pipeline
  streams run, without touching any other gate's semantics.
* ``saturator_pipeline_yield_preempt_n`` cuts fill DOWN TO the cap (not to
  zero — the account keeps some fill throughput), once per pipeline-active
  episode, after the shared persistence threshold.
"""

from __future__ import annotations

import asyncio

import pytest

import llm_concurrency
import llm_saturator
from llm_concurrency import (
    _PipelinePrioritySemaphore,
    _note_pipeline_active,
    pipeline_active_count,
)
from llm_saturator import (
    _pipeline_active_fill_cap,
    saturator_may_launch,
    saturator_pipeline_yield_preempt_n,
)


@pytest.fixture(autouse=True)
def _reset_active_counter(monkeypatch):
    monkeypatch.setattr(llm_concurrency, "_pipeline_active", 0)
    monkeypatch.setattr(llm_concurrency, "_pipeline_active_first_ts", None)
    monkeypatch.setattr(llm_concurrency, "_pipeline_active_last_change_ts", None)
    yield
    # monkeypatch restores the module attributes afterwards.


class _FakeCrossLoopSem:
    """Minimal stand-in with the ``holders`` attribute the reconciliation
    reads (real CrossLoopSemaphore exposes ``holders``)."""

    def __init__(self, value: int = 4) -> None:
        self._value = value

    @property
    def holders(self) -> int:
        return 4 - self._value

    async def acquire(self) -> None:
        # Instant grant; the wrapper's pending counter still exercises.
        self._value -= 1

    def release(self) -> None:
        self._value += 1


class TestActiveCounterPairing:
    def test_async_with_pairs_on_success_and_raise(self):
        async def scenario() -> None:
            sem = _FakeCrossLoopSem()
            w = _PipelinePrioritySemaphore(sem)
            async with w:
                assert llm_concurrency._pipeline_active == 1
            assert llm_concurrency._pipeline_active == 0

            w2 = _PipelinePrioritySemaphore(sem)
            with pytest.raises(RuntimeError):
                async with w2:
                    raise RuntimeError("body boom")
            # The exit path returned the active slot despite the raise.
            assert llm_concurrency._pipeline_active == 0

        asyncio.run(scenario())

    def test_failed_acquire_never_takes_active(self):
        class _FailingSem(_FakeCrossLoopSem):
            async def acquire(self) -> None:
                raise TimeoutError("dispatch window closed")

        async def scenario() -> None:
            w = _PipelinePrioritySemaphore(_FailingSem())
            with pytest.raises(TimeoutError):
                await w.acquire()
            assert llm_concurrency._pipeline_active == 0
            assert llm_concurrency._pipeline_pending == 0

        asyncio.run(scenario())

    def test_manual_acquire_release_pairs(self):
        async def scenario() -> None:
            w = _PipelinePrioritySemaphore(_FakeCrossLoopSem())
            await w.acquire()
            assert llm_concurrency._pipeline_active == 1
            w.release()
            assert llm_concurrency._pipeline_active == 0

        asyncio.run(scenario())


class TestLeakReconciliation:
    def test_no_semaphore_hard_resets_counter(self, monkeypatch):
        monkeypatch.setattr(llm_concurrency, "_pipeline_active", 3)
        monkeypatch.setattr(llm_concurrency, "_SHARED_LLM_SEMAPHORE", None)
        assert pipeline_active_count() == 0
        assert llm_concurrency._pipeline_active == 0

    def test_zero_holders_hard_resets_counter(self, monkeypatch):
        monkeypatch.setattr(
            llm_concurrency, "_SHARED_LLM_SEMAPHORE", _FakeCrossLoopSem(4)
        )
        monkeypatch.setattr(llm_concurrency, "_pipeline_active", 2)
        assert pipeline_active_count() == 0
        assert llm_concurrency._pipeline_active == 0

    def test_counter_clamped_to_holders(self, monkeypatch):
        monkeypatch.setattr(
            llm_concurrency, "_SHARED_LLM_SEMAPHORE", _FakeCrossLoopSem(1)
        )  # 3 held
        monkeypatch.setattr(llm_concurrency, "_pipeline_active", 5)
        assert pipeline_active_count() == 3

    def test_stale_counter_resets_with_warning(self, monkeypatch, caplog):
        monkeypatch.setattr(
            llm_concurrency, "_SHARED_LLM_SEMAPHORE", _FakeCrossLoopSem(2)
        )
        monkeypatch.setattr(llm_concurrency, "_pipeline_active", 1)
        import time as _time

        # Stale = NO acquire/release events for the window (audit counter-1),
        # not "continuously active since the epoch".
        monkeypatch.setattr(
            llm_concurrency,
            "_pipeline_active_last_change_ts",
            _time.time() - 7200,
        )
        with caplog.at_level("WARNING", logger="llm_concurrency"):
            assert pipeline_active_count() == 0
        assert any("stale" in rec.message for rec in caplog.records)

    def test_continuous_relay_activity_never_resets(self, monkeypatch):
        # A pipeline relaying streams without ever going idle keeps the
        # counter legitimately positive — the stale reset must not clear it
        # (audit counter-1: clearing it unleashed a refill wave onto live
        # pipeline streams).
        monkeypatch.setattr(
            llm_concurrency, "_SHARED_LLM_SEMAPHORE", _FakeCrossLoopSem(2)
        )
        monkeypatch.setattr(llm_concurrency, "_pipeline_active", 2)
        import time as _time

        monkeypatch.setattr(
            llm_concurrency,
            "_pipeline_active_first_ts",
            _time.time() - 7200,
        )
        monkeypatch.setattr(
            llm_concurrency,
            "_pipeline_active_last_change_ts",
            _time.time() - 5,
        )
        assert pipeline_active_count() == 2


class TestFillCapGate:
    def test_pipeline_active_refuses_at_cap(self, monkeypatch):
        import llm_concurrency as lc

        monkeypatch.setattr(llm_saturator, "_mem_available_mb", lambda: 2048)
        monkeypatch.setattr(
            llm_saturator, "_saturator_provider_paused", lambda: False
        )
        monkeypatch.setattr(lc, "_SHARED_LLM_SEMAPHORE", None)
        monkeypatch.setattr(lc, "_GLOBAL_LLM_SEMAPHORE", None)

        # Fill below the cap still launches while the pipeline streams.
        ok, reason = saturator_may_launch(
            in_flight=1, soft_cap=4, pipeline_active=2
        )
        assert (ok, reason) == (True, "ok")

        # At the cap the budget gate refuses — BEFORE the soft cap (4).
        ok, reason = saturator_may_launch(
            in_flight=2, soft_cap=4, pipeline_active=2
        )
        assert ok is False
        assert reason == "pipeline_rate_budget"

    def test_cap_zero_parks_fill_entirely(self, monkeypatch):
        monkeypatch.setenv("POK_LLM_SATURATOR_PIPELINE_ACTIVE_INFLIGHT", "0")
        monkeypatch.setattr(llm_saturator, "_mem_available_mb", lambda: 2048)
        monkeypatch.setattr(
            llm_saturator, "_saturator_provider_paused", lambda: False
        )
        ok, reason = saturator_may_launch(
            in_flight=0, soft_cap=4, pipeline_active=1
        )
        assert ok is False
        assert reason == "pipeline_rate_budget"

    def test_no_pipeline_active_keeps_old_semantics(self, monkeypatch):
        monkeypatch.setattr(llm_saturator, "_mem_available_mb", lambda: 2048)
        monkeypatch.setattr(
            llm_saturator, "_saturator_provider_paused", lambda: False
        )
        ok, reason = saturator_may_launch(
            in_flight=4, soft_cap=4, pipeline_active=0
        )
        assert ok is False
        assert reason == "soft_cap"

    def test_env_cap_default_and_override(self, monkeypatch):
        monkeypatch.delenv(
            "POK_LLM_SATURATOR_PIPELINE_ACTIVE_INFLIGHT", raising=False
        )
        assert _pipeline_active_fill_cap() == 2
        monkeypatch.setenv("POK_LLM_SATURATOR_PIPELINE_ACTIVE_INFLIGHT", "1")
        assert _pipeline_active_fill_cap() == 1
        monkeypatch.setenv("POK_LLM_SATURATOR_PIPELINE_ACTIVE_INFLIGHT", "x")
        assert _pipeline_active_fill_cap() == 2


class TestPipelineYieldPreempt:
    def test_cuts_down_to_cap_once_per_episode(self):
        n = saturator_pipeline_yield_preempt_n(
            pipeline_active=2,
            in_flight=5,
            cap=2,
            already_done=False,
            active_age_sec=20.0,
        )
        assert n == 3

    def test_at_or_below_cap_is_zero(self):
        assert (
            saturator_pipeline_yield_preempt_n(
                pipeline_active=2,
                in_flight=2,
                cap=2,
                already_done=False,
                active_age_sec=60.0,
            )
            == 0
        )

    def test_below_persistence_threshold_is_zero(self):
        assert (
            saturator_pipeline_yield_preempt_n(
                pipeline_active=1,
                in_flight=5,
                cap=1,
                already_done=False,
                active_age_sec=5.0,
            )
            == 0
        )

    def test_once_per_episode_and_no_pipeline_zero(self):
        kwargs = dict(
            pipeline_active=1, in_flight=4, cap=1, active_age_sec=60.0
        )
        assert saturator_pipeline_yield_preempt_n(already_done=False, **kwargs) == 3
        assert saturator_pipeline_yield_preempt_n(already_done=True, **kwargs) == 0
        assert (
            saturator_pipeline_yield_preempt_n(
                already_done=False,
                pipeline_active=0,
                in_flight=4,
                cap=1,
                active_age_sec=60.0,
            )
            == 0
        )


def test_rate_budget_gate_is_wired_into_the_loop(monkeypatch):
    """The main loop must both read the counter and pass it to the gate."""
    import inspect

    src = inspect.getsource(llm_saturator.run_llm_saturator)
    assert "pipeline_active_count" in src
    assert "pipeline_active=_pipeline_active_now" in src
    assert "saturator_pipeline_yield_preempt_n" in src
