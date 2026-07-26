"""Legacy provider-session sidecar abandonment for the Orchestrator.

The orchestrator never persists opaque provider-session identity across
cycles.  These helpers enforce that policy by deleting any leftover
``orchestrator_session.json`` sidecar that older code might have written
and emitting a system-log event for observability.

This module is NOT a session manager:

* Pipeline checkpoint/handoff recovery lives in :mod:`orchestrator`,
  where it can distinguish an absent checkpoint from invalid on-disk
  bytes and route every stage through the active state machine.
* Orchestrator log rotation lives next to ``LOGS_DIR`` in
  :mod:`orchestrator` (``_rotate_orchestrator_logs``).
* Rate-limit detection for stream output lives in :mod:`llm_query`
  (``_is_rate_limited``).

Keeping these abandonment helpers separate prevents the old
provider-history sidecar from mutating pipeline authority.
"""

import logging
from pathlib import Path

log = logging.getLogger("pok.orchestrator")

RESULTS_DIR = Path(__file__).resolve().parent / "results"
ORCHESTRATOR_SESSION_FILE = RESULTS_DIR / "orchestrator_session.json"


def _save_orchestrator_session(session_id: str):
    """Discard provider session identity; recovery always uses a fresh stream."""

    had_legacy_sidecar = ORCHESTRATOR_SESSION_FILE.exists()
    ORCHESTRATOR_SESSION_FILE.unlink(missing_ok=True)
    try:
        from system_log import log_system_event

        log_system_event(
            "orchestrator.session_resume_forbidden",
            "info",
            "Provider session identity was not persisted; checkpoint recovery uses a fresh stream",
            {
                "provider_session_observed": bool(session_id),
                "legacy_sidecar_removed": had_legacy_sidecar,
                "history_policy": (
                    "fresh_provider_session_from_checkpoint_projection_only"
                ),
            },
        )
    except Exception:
        pass


def _load_orchestrator_session() -> "str | None":
    """Delete any legacy opaque-session sidecar and always return ``None``."""

    ORCHESTRATOR_SESSION_FILE.unlink(missing_ok=True)
    return None


def _clear_orchestrator_session(reason="completed_or_reset"):
    """Delete any legacy provider-session sidecar."""

    existed = ORCHESTRATOR_SESSION_FILE.exists()
    ORCHESTRATOR_SESSION_FILE.unlink(missing_ok=True)
    try:
        from system_log import log_system_event

        log_system_event(
            "orchestrator.session_cleared",
            "info",
            f"Cleared orchestrator session ({reason})",
            {"reason": reason, "existed": existed},
        )
    except Exception:
        pass
