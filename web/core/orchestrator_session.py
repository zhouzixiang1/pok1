"""Orchestrator session persistence and startup recovery.

Handles saving/loading/clearing the orchestrator session ID for crash recovery,
log rotation, and rate-limit detection.
"""

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("pok.orchestrator")

RESULTS_DIR = Path(__file__).resolve().parent / "results"
ORCHESTRATOR_SESSION_FILE = RESULTS_DIR / "orchestrator_session.json"


def _rotate_orchestrator_logs(logs_dir, keep=20):
    """Keep only the most recent N orchestrator log files."""
    if not logs_dir.exists():
        return
    files = sorted(
        (f for f in logs_dir.iterdir()
         if f.name.startswith("orchestrator_") and f.name.endswith(".txt")),
        key=lambda f: f.stat().st_mtime,
    )
    for old_file in files[:-keep]:
        try:
            old_file.unlink()
        except OSError:
            pass


from llm_query import _is_rate_limited  # noqa: E402


def _save_orchestrator_session(session_id: str):
    """Persist session_id so a killed process can resume the exact conversation."""
    tmp = ORCHESTRATOR_SESSION_FILE.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, json.dumps({"session_id": session_id}).encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(ORCHESTRATOR_SESSION_FILE))


def _load_orchestrator_session() -> "str | None":
    """Return saved session_id, or None."""
    if not ORCHESTRATOR_SESSION_FILE.exists():
        return None
    try:
        return json.loads(ORCHESTRATOR_SESSION_FILE.read_text())["session_id"]
    except Exception:
        return None


def _clear_orchestrator_session():
    """Delete session file after a naturally completed cycle."""
    ORCHESTRATOR_SESSION_FILE.unlink(missing_ok=True)


def _startup_recovery(ui=None) -> dict:
    """Assess interrupted state on startup. Returns recovery action dict.

    Decision matrix:
        checkpoint + session → Case C: resume LLM conversation + pipeline
        checkpoint + no session → Case B: new LLM session, resume from checkpoint stage
        no checkpoint + session → Case D: stale session, clear and start fresh
        no checkpoint + no session → Case A: fresh start
    """
    from evolution_core import read_pipeline_checkpoint, clear_pipeline_checkpoint
    checkpoint = read_pipeline_checkpoint()
    session_id = _load_orchestrator_session()

    if not checkpoint:
        if session_id:
            if ui:
                ui.log_history("[Recovery] Stale session file (no pipeline checkpoint). Clearing.", "warn")
            else:
                log.warning("Stale session file (no pipeline checkpoint). Clearing.")
            _clear_orchestrator_session()
        return {"action": "fresh_start"}

    stage = checkpoint.get("stage", "unknown")
    next_v = checkpoint.get("next_v")

    # Watchdog recovery: if checkpoint is stale (no stage change for > WATCHDOG_TIMEOUT)
    # and we're at a recoverable stage, treat as stale session and force new LLM session.
    recoverable_stages = {"direction_audited", "master_planned", "workers_done",
                          "quality_passed", "reviewed", "critic_checked", "precommit_failed", "verified"}
    last_stage_ts = checkpoint.get("last_stage_change_ts", 0.0)
    if stage in recoverable_stages and last_stage_ts > 0:
        from evolution_infra import WATCHDOG_TIMEOUT
        elapsed = time.time() - last_stage_ts
        if elapsed > WATCHDOG_TIMEOUT:
            msg = (f"[Watchdog Recovery] v{next_v} at stage '{stage}' with no progress "
                   f"for {elapsed:.0f}s (>{WATCHDOG_TIMEOUT}s). Clearing stale session, "
                   f"will resume from checkpoint with new LLM session.")
            if ui:
                ui.log_history(msg, "warn")
            else:
                log.warning(msg)
            from system_log import log_system_event
            log_system_event("pipeline.watchdog_recovery", "warn", msg,
                             {"next_v": next_v, "stage": stage, "elapsed_s": round(elapsed, 1),
                              "watchdog_timeout": WATCHDOG_TIMEOUT})
            # Clear session to force new LLM conversation, but keep checkpoint for stage resume
            _clear_orchestrator_session()
            session_id = None  # file is gone — force fresh LLM session (Case B below)
            # Fall through to recovery below — session_id is None → Case B

    # archived, prepared with no master_plan, or timed_out = no real work to recover
    if stage == "timed_out":
        if ui:
            ui.log_history(f"[Recovery] v{next_v} timed out — clearing stale checkpoint.", "warn")
        else:
            log.warning("v%s timed out — clearing stale checkpoint.", next_v)
        clear_pipeline_checkpoint()
        _clear_orchestrator_session()
        return {"action": "fresh_start"}

    if stage == "archived" or (stage == "prepared" and not checkpoint.get("master_plan")):
        if ui:
            ui.log_history(f"[Recovery] Pipeline at '{stage}' for v{next_v}. Clearing stale checkpoint.", "warn")
        else:
            log.warning("Pipeline at '%s' for v%s. Clearing stale checkpoint.", stage, next_v)
        clear_pipeline_checkpoint()
        _clear_orchestrator_session()
        return {"action": "fresh_start"}

    # Aborted pipeline: no git tag for next_v + checkpoint is stale (>=30 min old)
    # This catches cases where a generation was aborted (e.g. via manual git commit)
    # but pipeline_state.json was never cleaned up.
    # EXCEPT: stages beyond "prepared" represent real work (direction audit, master plan, etc.)
    # that should be recovered rather than discarded.
    from evolution_infra import git_has_tag
    if next_v is not None and not git_has_tag(next_v):
        ckpt_ts = checkpoint.get("timestamp")
        # Stages with real work — don't abort even if old
        recoverable_stages = {"direction_audited", "master_planned", "workers_done",
                              "quality_passed", "reviewed", "critic_checked", "precommit_failed", "verified"}
        if stage in recoverable_stages:
            if ui:
                ui.log_history(f"[Recovery] v{next_v} at stage '{stage}' — preserving for resume (no 30-min abort).", "warn")
            # Fall through to recovery below
        elif ckpt_ts:
            try:
                from datetime import datetime, timezone
                ckpt_time = datetime.fromisoformat(ckpt_ts).replace(tzinfo=None)
                age_minutes = (datetime.now() - ckpt_time).total_seconds() / 60
                if age_minutes >= 30:
                    msg = (f"[Recovery] v{next_v} has no git tag and checkpoint is "
                           f"{age_minutes:.0f} min old — treating as aborted. Clearing.")
                    if ui:
                        ui.log_history(msg, "warn")
                    else:
                        log.warning(msg)
                    clear_pipeline_checkpoint()
                    _clear_orchestrator_session()
                    return {"action": "fresh_start"}
            except (ValueError, TypeError):
                pass

    # Significant work was done — attempt recovery
    recovery = {
        "action": "resume",
        "checkpoint": checkpoint,
        "session_id": session_id,
        "stage": stage,
        "next_v": next_v,
        "source_v": checkpoint.get("source_v"),
    }
    if session_id:
        msg = f"[Recovery] Resuming v{next_v} at '{stage}' with session {session_id[:8]}..."
    else:
        msg = f"[Recovery] Resuming v{next_v} at '{stage}' (new LLM session)."
    if ui:
        ui.log_history(msg, "warn")
        log.warning(msg)
    return recovery
