"""Structured system event logger — writes to system_events.jsonl."""

import json
import logging
import os
import time

from evolution_infra import RESULTS_DIR, locked_file

SYSTEM_EVENTS_FILE = RESULTS_DIR / "system_events.jsonl"
MAX_SYSTEM_EVENTS_LINES = 5000

_ui = None


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


def log_system_event(event_type: str, severity: str, message: str, data: dict = None):
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
    if not hasattr(log_system_event, '_write_count'):
        log_system_event._write_count = 0
    log_system_event._write_count += 1
    if log_system_event._write_count % 100 == 0:
        try:
            _rotate_self()
        except Exception:
            pass
    if _ui is not None:
        try:
            _ui._emit("system_event", entry)
        except Exception as e:
            logging.getLogger(__name__).debug("SSE emit failed for system_event: %s", e)
