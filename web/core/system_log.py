"""Structured system event logger — writes to system_events.jsonl."""

import json
import logging
import time

from evolution_infra import RESULTS_DIR, locked_file

SYSTEM_EVENTS_FILE = RESULTS_DIR / "system_events.jsonl"

_ui = None


def set_ui(ui):
    global _ui
    _ui = ui


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
    if _ui is not None:
        try:
            _ui._emit("system_event", entry)
        except Exception as e:
            logging.getLogger(__name__).debug("SSE emit failed for system_event: %s", e)
