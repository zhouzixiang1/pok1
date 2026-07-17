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
from collections.abc import Callable

log = logging.getLogger("pok.shutdown")


class ShutdownManager:
    def __init__(self, grace_period: float = 15.0):
        self._event = asyncio.Event()
        self._grace_period = grace_period
        # These callbacks are deliberately edge-triggered and advisory.  The
        # shutdown event is set before they run, so UI/control-plane observers
        # can never veto or delay a real shutdown.
        self._shutdown_listeners: list[Callable[[], None]] = []

    @property
    def is_shutting_down(self) -> bool:
        return self._event.is_set()

    def add_shutdown_listener(self, listener: Callable[[], None]) -> None:
        """Observe the first shutdown edge without owning shutdown itself."""

        if not callable(listener):
            raise TypeError("shutdown listener must be callable")
        self._shutdown_listeners.append(listener)

    def request_shutdown(self):
        """Programmatically trigger shutdown (e.g. from web UI stop button)."""
        if self._event.is_set():
            return False
        self._event.set()
        for listener in tuple(self._shutdown_listeners):
            try:
                listener()
            except Exception:
                # The process is already stopping.  An observer failure must
                # not hide that fact from the actual shutdown owner or stop
                # other observers from receiving the edge.
                log.exception("Shutdown lifecycle listener failed")
        return True

    def install_signal_handlers(self, loop: asyncio.AbstractEventLoop):
        """Install SIGINT/SIGTERM/SIGHUP handlers on the event loop.

        Must be called from within a running event loop.
        NOTE: Do NOT call this inside a uvicorn lifespan — it overwrites
        uvicorn's signal handlers and prevents graceful shutdown.
        """
        # C6: include SIGHUP so a terminal disconnect (SSH hangup / closed
        # window) triggers graceful shutdown + flush instead of the default
        # instant-kill that orphans the daemon and corrupts checkpoints.
        sigs = [signal.SIGINT, signal.SIGTERM]
        hup = getattr(signal, "SIGHUP", None)
        if hup is not None:  # POSIX only; absent on Windows
            sigs.append(hup)
        for sig in sigs:
            loop.add_signal_handler(sig, self._on_signal, sig)

    def _on_signal(self, sig):
        if self._event.is_set():
            # Second signal — restore default so the next one terminates the process
            signal.signal(sig, signal.SIG_DFL)
            return
        log.warning("Received %s, initiating graceful shutdown...", sig.name)
        self.request_shutdown()

    async def wait_for_shutdown(self):
        await self._event.wait()
