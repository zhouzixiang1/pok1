"""Asyncio-native shutdown coordinator for the evolution system.

Uses loop.add_signal_handler() (not signal.signal()) to correctly handle
SIGINT/SIGTERM within the asyncio event loop. Provides a clean shutdown_event
that all phases check between operations.

Usage:
    mgr = ShutdownManager()
    loop = asyncio.get_running_loop()
    mgr.install_signal_handlers(loop)

    while not mgr.is_shutting_down:
        await do_work()
"""

import asyncio
import logging
import signal

log = logging.getLogger("pok.shutdown")


class ShutdownManager:
    def __init__(self, grace_period: float = 15.0):
        self._event = asyncio.Event()
        self._grace_period = grace_period

    @property
    def is_shutting_down(self) -> bool:
        return self._event.is_set()

    def request_shutdown(self):
        """Programmatically trigger shutdown (e.g. from web UI stop button)."""
        if not self._event.is_set():
            self._event.set()

    def install_signal_handlers(self, loop: asyncio.AbstractEventLoop):
        """Install SIGINT/SIGTERM handlers on the event loop.

        Must be called from within a running event loop.
        NOTE: Do NOT call this inside a uvicorn lifespan — it overwrites
        uvicorn's signal handlers and prevents graceful shutdown.
        """
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._on_signal, sig)

    def _on_signal(self, sig):
        if self._event.is_set():
            # Second signal — restore default so the next one terminates the process
            signal.signal(sig, signal.SIG_DFL)
            return
        log.warning("Received %s, initiating graceful shutdown...", sig.name)
        self._event.set()

    async def wait_for_shutdown(self):
        await self._event.wait()
