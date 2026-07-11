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
