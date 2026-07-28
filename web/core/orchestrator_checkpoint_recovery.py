"""Checkpoint recovery-context builder + startup recovery.

Extracted from orchestrator.py as a single business responsibility: build a
recovery context from an active pipeline checkpoint (the bridge between
disposable LLM sessions and durable pipeline checkpoints), expose the
deterministic-route log metadata, and read the one strict
checkpoint/handoff recovery at process startup.

Members moved here (all re-exported by orchestrator.py):

* ``_checkpoint_recovery_context``  -- build a recovery context from an
  active pipeline checkpoint (resume / blocked / operator_action_required).
* ``_recovery_route_log_kwargs``  -- log metadata carried by a recovery ctx.
* ``_startup_recovery``  -- the one strict checkpoint/handoff reader at
  process startup.
* ``_startup_recovery_terminal_cost``  -- map canonical startup stop states
  to typed loop outcomes.

IMPORTANT -- shared-symbol access model
---------------------------------------
Symbols referenced by these bodies that live in ``orchestrator`` are written
as ``_o.<name>`` so they resolve against the live ``orchestrator`` module
attribute, matching the pattern proven by ``orchestrator_branch_guard`` /
``orchestrator_post_generation``.  This covers:

* ``log`` (the module logger), ``log_system_event``.
* helper re-exported from ``orchestrator_stage_routing``:
  ``_pipeline_checkpoint_observation``.
* typed cost constants: ``ORCH_RECOVERY_BLOCKED_COST``,
  ``ORCH_OPERATOR_ACTION_REQUIRED_COST``.
"""

from __future__ import annotations

import orchestrator as _o


def _checkpoint_recovery_context(reason: str, ui=None, *, log_level: str = "warn", label: str = "[Recovery]"):
    """Build a recovery context from an active pipeline checkpoint.

    LLM sessions are disposable after SDK/cost-cap failures; pipeline checkpoints
    are not. This helper keeps those concepts separate so an infra retry resumes
    the same generation instead of falling back to Phase 1 source selection.
    """
    observation = _o._pipeline_checkpoint_observation()
    checkpoint = observation.get("checkpoint")
    checkpoint_error = observation.get("error")
    if checkpoint_error:
        msg = (
            f"{label} Checkpoint authority is unreadable or invalid after "
            f"{reason}: {checkpoint_error}."
        )
        if ui:
            ui.log_history(msg, "error")
        else:
            _o.log.error(msg)
        return {
            "action": "blocked",
            "reason": "checkpoint_unreadable_or_invalid",
            "checkpoint": None,
            "diagnostics": {
                "active": True,
                "recoverable": False,
                "issues": [str(checkpoint_error)],
                "checkpoint_path_exists": observation.get("path_exists"),
            },
        }

    if not checkpoint:
        try:
            from post_publication_handoff import (
                pending_handoff_route,
                pending_handoff_route_checkpoint,
            )

            handoff = pending_handoff_route()
        except Exception as exc:
            handoff = {
                "status": "blocked",
                "issues": [f"handoff_discovery_failed:{type(exc).__name__}"],
            }
        if handoff.get("status") == "blocked":
            return {
                "action": "blocked",
                "reason": "post_publication_handoff_ambiguous_or_invalid",
                "checkpoint": None,
                "diagnostics": {
                    "active": True,
                    "recoverable": False,
                    "issues": list(handoff.get("issues") or []),
                    "post_publication_handoff": True,
                },
            }
        if handoff.get("status") != "pending":
            return None
        route_checkpoint = pending_handoff_route_checkpoint(handoff)
        msg = (
            f"{label} Resuming published v{handoff['version']} at the durable "
            f"post-publication Archivist handoff after {reason}."
        )
        if ui:
            ui.log_history(msg, log_level)
        elif log_level == "info":
            _o.log.info(msg)
        else:
            _o.log.warning(msg)
        return {
            "action": "resume",
            "checkpoint": route_checkpoint,
            "session_id": None,
            "stage": "post_publication_handoff",
            "next_v": handoff["version"],
            "source_v": handoff["source_v"],
            "post_publication_handoff": True,
            "log_level": log_level,
            "label": label,
        }

    stage = checkpoint.get("stage")
    next_v = checkpoint.get("next_v")
    source_v = checkpoint.get("source_v")
    if next_v is None or source_v is None:
        return None
    try:
        from pipeline_recovery import checkpoint_recovery_diagnostics
        recovery_diag = checkpoint_recovery_diagnostics(checkpoint)
    except Exception as e:
        recovery_diag = {
            "active": True,
            "recoverable": False,
            "issues": ["checkpoint_recovery_diagnostic_failed"],
            "error": f"{type(e).__name__}: {str(e)[:200]}",
        }
    if stage == "official_bootstrap_required":
        issues = list(recovery_diag.get("issues") or [])
        expected_issue = "official_bootstrap_requires_operator_action"
        unexpected = [issue for issue in issues if issue != expected_issue]
        if (
            recovery_diag.get("active") is True
            and expected_issue in issues
            and not unexpected
        ):
            return {
                "action": "operator_action_required",
                "reason": expected_issue,
                "checkpoint": checkpoint,
                "stage": stage,
                "next_v": next_v,
                "source_v": source_v,
                "operator_action_required": True,
                "diagnostics": recovery_diag,
            }
    if recovery_diag.get("active") and not recovery_diag.get("recoverable"):
        issues = list(recovery_diag.get("issues") or [])
        msg = (
            f"[Recovery] Refusing checkpoint resume for v{next_v} at '{stage}' "
            f"after {reason}: {', '.join(issues)}."
        )
        if ui:
            ui.log_history(msg, "error")
        else:
            _o.log.error(msg)
        try:
            _o.log_system_event(
                "orchestrator.recovery_blocked",
                "error",
                msg,
                {
                    "case": f"blocked_after_{reason}",
                    "next_v": next_v,
                    "source_v": source_v,
                    "stage": stage,
                    "issues": issues,
                    "diagnostics": recovery_diag,
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

    dead_stages = {None, "archived", "abandoned"}
    if stage in dead_stages:
        return None

    recovery = {
        "action": "resume",
        "checkpoint": checkpoint,
        "session_id": None,  # force a fresh LLM session, but keep pipeline identity
        "stage": stage,
        "next_v": next_v,
        "source_v": source_v,
        "log_level": log_level,
        "label": label,
    }

    msg = f"{label} Resuming v{next_v} at '{stage}' after {reason} (new LLM session)."
    if ui:
        ui.log_history(msg, log_level)
    else:
        if log_level == "info":
            _o.log.info(msg)
        else:
            _o.log.warning(msg)
    try:
        _o.log_system_event(
            "orchestrator.recovery_decision", log_level, msg,
            {"case": f"resume_after_{reason}",
             "next_v": next_v, "source_v": source_v,
             "stage": stage, "session_present": False},
        )
    except Exception:
        pass
    return recovery


def _recovery_route_log_kwargs(recovery):
    """Return deterministic-route log metadata carried by a recovery context."""
    if not isinstance(recovery, dict):
        return {}
    return {
        "log_level": recovery.get("log_level") or "warn",
        "label": recovery.get("label") or "[Recovery]",
    }


def _startup_recovery(ui=None):
    """Use the one strict checkpoint/handoff reader at process startup."""

    return _o._checkpoint_recovery_context(
        "startup",
        ui,
        log_level="warn",
        label="[Recovery]",
    )


def _startup_recovery_terminal_cost(recovery) -> float | None:
    """Map only canonical startup stop states to typed loop outcomes."""

    action = recovery.get("action") if isinstance(recovery, dict) else None
    if action == "blocked":
        return _o.ORCH_RECOVERY_BLOCKED_COST
    if action == "operator_action_required":
        return _o.ORCH_OPERATOR_ACTION_REQUIRED_COST
    return None
