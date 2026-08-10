"""Master-abandon signal: let the tool layer request a generation abandon that the
orchestrator loop finalizes against a quiescent checkpoint.

Background
----------
``_abandon_master_generation`` (tool_planning.py) used to call
``_do_abandon_generation`` *inline* from inside a tool dispatch — i.e. while the
orchestrator loop was still running and concurrently bumping the checkpoint
``checkpoint_revision`` (heartbeats, deterministic routes).  The canonical
abandon transaction revalidates the checkpoint CAS under the publication lock
and refuses with ``expected_checkpoint_identity_mismatch`` when the identity has
drifted.  The inline abandon therefore lost every race, the tool layer ignored
the ``abandoned: False`` result, and the orchestrator re-entered ``run_master``
every ~30 s — burning LLM budget forever (the v161 / v106 livelock class).

The HTTP ``POST /api/control/abandon`` path already solves the *same* race by
stopping the orchestrator task first (inside ``_RUNTIME_LIFECYCLE_LOCK``) and
only then running the abandon against a static checkpoint
(``control.py::_abandon_generation_transaction``).  This module provides the
in-process equivalent of that "stop-then-abandon" pattern: instead of running
the publication-authority transaction from the tool layer, the tool layer just
*signals* the request, and the orchestrator loop — which is already the sole
owner of the publication lifecycle between cycles — finalizes it when the
checkpoint is guaranteed quiescent (right after ``_run_one_cycle`` returns,
before any re-entry).

Design
------
The signal is a process-wide singleton carrying the abandon reason.  It does
**not** carry an ``expected_abandon_identity`` snapshot: by the time the loop
consumes the signal, the orchestrator's own cycle is over, the checkpoint is
not being mutated, and the loop re-reads the live checkpoint to build a fresh
``expected_abandon_identity``.  Carrying a stale identity snapshot would just
re-introduce the exact CAS race this module exists to close.

The signal is intentionally one-shot: a single pending request at a time (a
generation has exactly one Master, so at most one terminal Master abandon can
be pending).  ``request_abandon`` overwrites a pending request because the
latest reason is always the authoritative one, and a retry that set a second
request must not clobber the first's intent — but since both target the same
generation and the loop drains before re-entry, there is only ever one consumer.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Optional

# ``asyncio.Event`` / ``get_event_loop`` are loop-bound, so the signal holds the
# *data* (lock-protected, loop-agnostic) separately from the *wakeup* (an
# asyncio.Event lazily bound to the running loop on first consume).  This keeps
# ``request_abandon`` callable from the tool layer (which runs inside the same
# loop via ``run_async_off_event_loop``, but may also be called from a worker
# thread for native gates) without needing a loop reference at request time.

_lock = threading.Lock()
_pending_reason: Optional[str] = None
_pending_at: Optional[float] = None
# Lazily-created asyncio.Event bound to the consuming loop.  Created on first
# ``consume_pending`` / ``wait`` call so it always belongs to the orchestrator
# loop, never a transient worker-thread loop.
_event: Optional[asyncio.Event] = None
_event_loop: Optional[asyncio.AbstractEventLoop] = None


def _get_event() -> asyncio.Event:
    """Return the singleton asyncio.Event bound to the current running loop.

    The orchestrator loop is the only legitimate consumer, so this is normally
    called once from the loop thread.  If the loop changes (service restart
    reuses the module), ``clear`` resets the binding so the next call rebinds.
    """
    global _event, _event_loop
    loop = asyncio.get_running_loop()
    if _event is None or _event_loop is not loop:
        _event = asyncio.Event()
        _event_loop = loop
    return _event


def request_abandon(reason: str) -> None:
    """Record a Master-abandon request from the tool layer.

    Safe to call from any thread / any loop (including worker threads running
    native gates via ``run_async_off_event_loop``).  Overwrites any prior
    pending request — only the latest reason is authoritative for the current
    generation.
    """
    global _pending_reason, _pending_at
    with _lock:
        _pending_reason = str(reason) if reason is not None else "master_abandon"
        _pending_at = time.time()
    # Best-effort wakeup: if the event exists and is bound to a loop, set it
    # from the loop's thread so a ``wait_for`` consumer wakes immediately.  If
    # the event is not yet bound (loop never consumed), the request is picked up
    # on the next ``consume_pending`` poll — correctness does not depend on the
    # wakeup, only latency does.
    try:
        ev = _event
        if ev is not None and _event_loop is not None and _event_loop.is_running():
            _event_loop.call_soon_threadsafe(ev.set)
    except Exception:
        pass


def consume_pending() -> Optional[str]:
    """Pop and return the pending abandon reason, or ``None`` if none pending.

    Must be called from the orchestrator loop thread.  Clears the signal and
    resets the wakeup event so a subsequent ``request_abandon`` can fire again.
    """
    global _pending_reason, _pending_at
    with _lock:
        reason = _pending_reason
        _pending_reason = None
        _pending_at = None
    # Clear the wakeup event from the consuming loop.
    try:
        _get_event().clear()
    except RuntimeError:
        # No running loop — called from a non-loop context; the data was still
        # cleared above, which is the correctness-critical part.
        pass
    return reason


def pending_reason() -> Optional[str]:
    """Peek the pending reason without consuming it (diagnostic only)."""
    with _lock:
        return _pending_reason


def pending_age_seconds() -> Optional[float]:
    """Return how long the pending request has been waiting, or ``None``."""
    with _lock:
        if _pending_at is None:
            return None
        return max(0.0, time.time() - _pending_at)


def clear() -> None:
    """Reset the signal entirely (loop startup / generation boundary)."""
    global _pending_reason, _pending_at, _event, _event_loop
    with _lock:
        _pending_reason = None
        _pending_at = None
    # Reset the loop binding so a restarted loop rebinds cleanly.
    _event = None
    _event_loop = None
