"""Structured system event logger — writes to system_events.jsonl."""

import json
import logging
import os
import time

from evolution_infra import RESULTS_DIR, locked_file

SYSTEM_EVENTS_FILE = RESULTS_DIR / "system_events.jsonl"
MAX_SYSTEM_EVENTS_LINES = 5000

_ui = None

# Write counter for system_events.jsonl rotation (checked every 100 writes).
_write_count = 0


def set_ui(ui):
    global _ui
    _ui = ui


def _rotate_self():
    """Rotate system_events.jsonl — called by the writer itself, so no cross-process race."""
    f = SYSTEM_EVENTS_FILE
    if not f.exists() or f.stat().st_size < 1_000_000:
        return
    import fcntl as _fcntl
    fd = open(f, "r")
    try:
        _fcntl.flock(fd, _fcntl.LOCK_EX)
        content = fd.read()
        lines = content.splitlines() if content else []
        if len(lines) <= MAX_SYSTEM_EVENTS_LINES:
            return
        trimmed = lines[-MAX_SYSTEM_EVENTS_LINES:]
        tmp = f.with_suffix(".tmp")
        tmp.write_text("\n".join(trimmed) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(f))
    except Exception:
        pass
    finally:
        _fcntl.flock(fd, _fcntl.LOCK_UN)
        fd.close()


def _write_system_event_raw(event_type: str, severity: str, message: str, data: dict = None):
    """Low-level write to system_events.jsonl + SSE broadcast + rotation.

    This is the legacy-side writer called by ``event_bus._dispatch`` during
    dual-write. It writes exactly what it is given — no correlation resolution,
    no severity normalisation (the canonical path is event_bus.emit, which does
    both and then calls this for the legacy file + SSE). Business code should
    call ``event_bus.emit`` (or the ``log_system_event`` shim) instead.
    """
    global _write_count
    entry = {
        "ts": time.time(),
        "type": event_type,
        "severity": severity,
        "message": message,
    }
    if data:
        entry["data"] = data
    with locked_file(SYSTEM_EVENTS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    # Rotate every 100 writes (cheap check: only stat + flock when over 1MB)
    _write_count += 1
    if _write_count % 100 == 0:
        try:
            _rotate_self()
        except Exception:
            pass
    if _ui is not None:
        try:
            _ui._emit("system_event", entry)
        except Exception as e:
            logging.getLogger(__name__).debug("SSE emit failed for system_event: %s", e)


def log_system_event(event_type: str, severity: str, message: str, data: dict = None):
    """Emit a system event (legacy entry — now a thin shim over event_bus.emit).

    Signature is unchanged so all existing call sites and test monkeypatches keep
    working. Forwards to ``event_bus.emit()``, which resolves correlation
    (run_id/stage/attempt from pipeline_state.json), normalises severity to the
    frontend's canonical 4 values, and dual-writes events.jsonl (new schema) +
    system_events.jsonl (legacy, via _write_system_event_raw) + SSE.

    New code should call ``event_bus.emit/success/failure/progress/warn`` directly
    so it picks up the failure_mode/category kwargs with a clearer signature.
    """
    try:
        from event_bus import emit
        # Forward business fields as kwargs. ``category`` is emit's positional
        # arg — defensively drop it (legacy data never carries it, but a stray
        # key would otherwise raise TypeError and lose the event).
        fields = dict(data or {})
        fields.pop("category", None)
        emit(event_type, severity, message, **fields)
    except Exception:
        # Fallback: never lose an event. Raw write preserves the exact legacy
        # behaviour if event_bus is unavailable or dispatch raises.
        _write_system_event_raw(event_type, severity, message, data)
