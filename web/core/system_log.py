"""Structured-event compatibility shim and dashboard broadcaster.

``events.jsonl`` owned by :mod:`event_bus` is the sole runtime event ledger.
This module keeps the long-standing ``log_system_event`` call signature and
the in-process SSE bridge; it no longer creates a second legacy event file.
"""

from __future__ import annotations

import logging


_ui = None


def set_ui(ui):
    global _ui
    _ui = ui


def broadcast_system_event(entry: dict) -> None:
    """Broadcast an already-persisted canonical event to dashboard listeners."""

    if _ui is not None:
        try:
            _ui._emit("system_event", entry)
        except Exception as exc:
            logging.getLogger(__name__).debug(
                "SSE emit failed for structured event: %s", exc
            )


def log_system_event(
    event_type: str,
    severity: str,
    message: str,
    data: dict | None = None,
) -> None:
    """Emit one canonical event while preserving the historical call API."""

    try:
        from event_bus import emit

        fields = dict(data or {})
        fields.pop("category", None)
        emit(event_type, severity, message, **fields)
    except Exception as exc:
        # Logging must never mutate a second fallback ledger or crash business
        # logic.  The failure remains visible in the process log.
        logging.getLogger(__name__).debug(
            "Structured event emission failed for %s: %s", event_type, exc
        )


__all__ = ["broadcast_system_event", "log_system_event", "set_ui"]
