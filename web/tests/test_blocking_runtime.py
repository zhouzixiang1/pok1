import asyncio
import contextvars
import threading

import pytest

from blocking_runtime import run_blocking_isolated


def test_isolated_blocking_call_propagates_context_and_reclaims_worker():
    marker = contextvars.ContextVar("marker", default="missing")
    marker.set("official-job")

    async def scenario():
        return await run_blocking_isolated(
            lambda: (marker.get(), threading.current_thread().name),
            thread_name_prefix="official-test",
        )

    value, thread_name = asyncio.run(scenario())

    assert value == "official-job"
    assert thread_name.startswith("official-test")
    assert not any(
        thread.name.startswith("official-test") and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_isolated_blocking_call_propagates_failure_without_default_executor():
    def fail():
        raise RuntimeError("official control failure")

    async def scenario():
        with pytest.raises(RuntimeError, match="official control failure"):
            await run_blocking_isolated(fail)
        assert asyncio.get_running_loop()._default_executor is None

    asyncio.run(scenario())


def test_back_to_back_isolated_calls_do_not_depend_on_default_executor_wakeup():
    async def scenario():
        first = await run_blocking_isolated(lambda: "first")
        second = await run_blocking_isolated(lambda: "second")
        return first, second, asyncio.get_running_loop()._default_executor

    first, second, default_executor = asyncio.run(scenario())

    assert (first, second) == ("first", "second")
    assert default_executor is None


def test_slow_blocking_call_does_not_starve_the_event_loop():
    """Regression guard for the /start hang.

    The boundary must not busy-poll the worker future while a slow blocking
    call (e.g. ``_runtime_launch_barrier_snapshot`` taking ~44s) runs: a tight
    ``while not done: await asyncio.sleep(0.001)`` loop wakes the event loop
    1000x/sec and starves every concurrent request (health, SSE, the HTTP
    response itself), which is exactly why ``/api/control/start`` hung for
    120s+ with HTTP 000. The fix uses a single ``add_done_callback`` wakeup, so
    a concurrent "health-check" coroutine must keep making progress while the
    blocking worker runs.
    """

    import time

    def slow_worker():
        time.sleep(1.0)
        return "worker-done"

    async def health_like():
        ticks = 0
        for _ in range(10):
            await asyncio.sleep(0.1)
            ticks += 1
        return ticks

    async def scenario():
        loop = asyncio.get_running_loop()
        start = loop.time()
        worker_task = asyncio.create_task(run_blocking_isolated(slow_worker))
        health_task = asyncio.create_task(health_like())
        ticks = await health_task
        worker_result = await worker_task
        elapsed = loop.time() - start
        return ticks, worker_result, elapsed

    ticks, worker_result, elapsed = asyncio.run(scenario())

    # The worker ran to completion.
    assert worker_result == "worker-done"
    # The concurrent coroutine made full progress (not starved by busy-poll).
    assert ticks == 10
    # Both finished in roughly the slow-worker duration, not 2x it (which would
    # indicate they ran serially) nor far beyond it (which would indicate
    # starvation). Allow generous slack for CI scheduling jitter.
    assert elapsed < 2.5

