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
    try:
        from system_log import log_system_event
        log_system_event(
            "orchestrator.session_saved", "info",
            f"Saved orchestrator session {session_id[:8]}",
            {"session_id_prefix": session_id[:8]},
        )
    except Exception:
        pass


def _load_orchestrator_session() -> "str | None":
    """Return saved session_id, or None."""
    if not ORCHESTRATOR_SESSION_FILE.exists():
        return None
    try:
        return json.loads(ORCHESTRATOR_SESSION_FILE.read_text())["session_id"]
    except Exception:
        return None


def _clear_orchestrator_session(reason="completed_or_reset"):
    """Delete session file after a naturally completed cycle."""
    existed = ORCHESTRATOR_SESSION_FILE.exists()
    ORCHESTRATOR_SESSION_FILE.unlink(missing_ok=True)
    try:
        from system_log import log_system_event
        log_system_event(
            "orchestrator.session_cleared", "info",
            f"Cleared orchestrator session ({reason})",
            {"reason": reason, "existed": existed},
        )
    except Exception:
        pass


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
            _clear_orchestrator_session(reason="stale_session_no_checkpoint")
            try:
                from system_log import log_system_event
                log_system_event(
                    "orchestrator.recovery_decision", "warn",
                    "Startup recovery: stale session cleared; fresh start",
                    {"case": "stale_session_clear", "session_present": True},
                )
            except Exception:
                pass
        else:
            try:
                from system_log import log_system_event
                log_system_event(
                    "orchestrator.recovery_decision", "info",
                    "Startup recovery: fresh start",
                    {"case": "fresh", "session_present": False},
                )
            except Exception:
                pass
        return {"action": "fresh_start"}

    stage = checkpoint.get("stage", "unknown")
    next_v = checkpoint.get("next_v")

    try:
        from pipeline_recovery import checkpoint_recovery_diagnostics
        recovery_diag = checkpoint_recovery_diagnostics(checkpoint)
    except Exception as exc:
        recovery_diag = {
            "active": True,
            "recoverable": False,
            "issues": ["checkpoint_recovery_diagnostic_failed"],
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
        }
    if recovery_diag.get("active") and not recovery_diag.get("recoverable"):
        issues = list(recovery_diag.get("issues") or [])
        msg = (
            f"[Recovery] Refusing to resume v{next_v} at '{stage}' because the "
            f"checkpoint is not recoverable on this worktree: {', '.join(issues)}."
        )
        if ui:
            ui.log_history(msg, "error")
        else:
            log.error(msg)
        _clear_orchestrator_session(reason="unrecoverable_checkpoint")
        try:
            from system_log import log_system_event
            log_system_event(
                "orchestrator.recovery_blocked",
                "error",
                msg,
                {
                    "case": "unrecoverable_checkpoint",
                    "next_v": next_v,
                    "stage": stage,
                    "issues": issues,
                    "diagnostics": recovery_diag,
                    "session_present": bool(session_id),
                },
            )
        except Exception:
            pass
        return {
            "action": "blocked",
            "reason": "unrecoverable_checkpoint",
            "checkpoint": checkpoint,
            "diagnostics": recovery_diag,
        }

    # Watchdog recovery: if checkpoint is stale (no stage change for > WATCHDOG_TIMEOUT)
    # and we're at a recoverable stage, treat as stale session and force new LLM session.
    # The classification is owned by pipeline_state so official-certification and
    # future stages cannot drift from this recovery path.
    from pipeline_state import (
        pipeline_runtime_activity_ts,
        session_recoverable_stages,
    )
    recoverable_stages = session_recoverable_stages()
    last_stage_ts = max(
        float(checkpoint.get("last_stage_change_ts") or 0.0),
        pipeline_runtime_activity_ts(checkpoint),
    )
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
            _clear_orchestrator_session(reason="watchdog_recovery")
            session_id = None  # file is gone — force fresh LLM session (Case B below)
            # Fall through to recovery below — session_id is None → Case B

    # timed_out and archived are terminal. Early stages such as selected,
    # preparing, prepared, and crossover_running are recoverable generation
    # leases and must not be cleared here.
    if stage == "timed_out":
        if ui:
            ui.log_history(f"[Recovery] v{next_v} timed out — clearing stale checkpoint.", "warn")
        else:
            log.warning("v%s timed out — clearing stale checkpoint.", next_v)
        clear_pipeline_checkpoint()
        _clear_orchestrator_session(reason="timed_out_checkpoint")
        try:
            from system_log import log_system_event
            log_system_event(
                "orchestrator.recovery_decision", "warn",
                f"Startup recovery: timed_out v{next_v} cleared",
                {"case": "timed_out_clear", "next_v": next_v, "stage": stage,
                 "session_present": bool(session_id)},
            )
        except Exception:
            pass
        return {"action": "fresh_start"}

    # v193 root-cause-audit (2026-06-26): infra-only timeout during precommit.
    # The cycle timed out NOT because the bot regressed (quality/review/critic all
    # passed, no regression blocker) but because the daemon/scheduler failed to
    # deliver battle results. Discarding the generation (like plain timed_out does)
    # wastes already-passed gates and forces a costly master+workers re-run that may
    # hit the SAME infra stall. Instead: roll the stage back to critic_checked
    # (a recoverable stage) WITHOUT clearing the checkpoint, so the next cycle
    # resumes with a fresh LLM session and re-runs run_precommit_eval on the SAME
    # code. precommit_attempt is already incremented (tool_eval.py:384-397), so the
    # existing infra-retry logic + MAX_PRECOMMIT_RETRIES cap still applies — if it
    # genuinely keeps timing out, it degrades to abandon as before.
    if stage == "infra_timed_out":
        from evolution_core import write_pipeline_checkpoint
        msg = (f"[Recovery] v{next_v} infra-only timeout during precommit (gates passed, "
               f"no regression). Preserving code/gate_results — will retry precommit "
               f"(attempt {checkpoint.get('precommit_attempt', 0)}).")
        if ui:
            ui.log_history(msg, "warn")
        else:
            log.warning(msg)
        from system_log import log_system_event
        log_system_event("pipeline.infra_timed_out_recovery", "warn", msg,
                         {"next_v": next_v, "precommit_attempt": checkpoint.get("precommit_attempt", 0)})
        # Roll back to critic_checked so the watchdog/recovery treats it as a
        # recoverable stage and resumes precommit (Case B: new LLM session).
        write_pipeline_checkpoint(
            next_v, checkpoint.get("source_v"), "critic_checked",
            master_plan=checkpoint.get("master_plan"),
        )
        _clear_orchestrator_session(reason="infra_timeout_retry")
        # Fall through to the resume path below (stage is now critic_checked).
        stage = "critic_checked"

    if stage == "archived":
        if ui:
            ui.log_history(f"[Recovery] Pipeline at '{stage}' for v{next_v}. Clearing stale checkpoint.", "warn")
        else:
            log.warning("Pipeline at '%s' for v%s. Clearing stale checkpoint.", stage, next_v)
        clear_pipeline_checkpoint()
        _clear_orchestrator_session(reason=f"stale_stage_{stage}")
        try:
            from system_log import log_system_event
            log_system_event(
                "orchestrator.recovery_decision", "warn",
                f"Startup recovery: stale stage {stage} cleared",
                {"case": "stale_checkpoint_clear", "next_v": next_v,
                 "stage": stage, "session_present": bool(session_id)},
            )
        except Exception:
            pass
        return {"action": "fresh_start"}

    # Aborted pipeline: no git tag for next_v + checkpoint is stale (>=30 min old)
    # This catches cases where a generation was aborted (e.g. via manual git commit)
    # but pipeline_state.json was never cleaned up.
    # EXCEPT: stages beyond "prepared" represent real work (direction audit, master plan, etc.)
    # that should be recovered rather than discarded.
    from evolution_infra import git_has_tag
    if next_v is not None and not git_has_tag(next_v):
        ckpt_ts = checkpoint.get("timestamp")
        # Stages with durable work are classified by the state machine; don't
        # abort them merely because their tag has not been created yet.
        recoverable_stages = session_recoverable_stages()
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
                    _clear_orchestrator_session(reason="untagged_old_checkpoint")
                    try:
                        from system_log import log_system_event
                        log_system_event(
                            "orchestrator.recovery_decision", "warn",
                            f"Startup recovery: old untagged v{next_v} checkpoint cleared",
                            {"case": "untagged_old_clear", "next_v": next_v,
                             "stage": stage, "age_minutes": round(age_minutes, 1),
                             "session_present": bool(session_id)},
                        )
                    except Exception:
                        pass
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
    try:
        from system_log import log_system_event
        log_system_event(
            "orchestrator.recovery_decision", "warn",
            msg,
            {"case": "resume_same_session" if session_id else "resume_new_session",
             "next_v": next_v, "source_v": checkpoint.get("source_v"),
             "stage": stage, "session_present": bool(session_id)},
        )
    except Exception:
        pass
    return recovery
