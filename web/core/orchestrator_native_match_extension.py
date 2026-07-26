"""Bounded native-match stream-extension/handoff authority.

Extracted from orchestrator.py as a single business responsibility: privileged
liveness extension for one immutable engine match, with proof/reproof of
liveness and terminal-handoff checkpoint validation/consumption.

Members moved here:

* ``ORCH_NATIVE_MATCH_MAX_EXTENSION_HARD_CAP_SEC`` -- absolute ceiling.
* ``ORCH_NATIVE_MATCH_MAX_EXTENSION_SEC`` -- effective cap (env-tunable).
* ``ORCH_NATIVE_MATCH_PROGRESS_MAX_AGE_SEC`` -- freshness bound on progress.
* ``ORCH_NATIVE_MATCH_REPROOF_INTERVAL_SEC`` -- reproof cadence.
* ``_bounded_native_match_extension`` -- grant one eligible extension.
* ``_native_match_extension_reproof`` -- prove a granted extension still
  belongs to one immutable match.
* ``_native_match_terminal_handoff_checkpoint_valid`` -- require the current
  checkpoint to be the same owner flow or its result.
* ``_consume_native_match_terminal_handoff`` -- consume a runner-return
  receipt for one immutable granted extension.
* ``_native_match_terminal_handoff_reproof`` -- validate a consumed handoff
  without extending its fixed expiry.

IMPORTANT -- shared-symbol access model
---------------------------------------
Symbols referenced by these bodies that live in ``orchestrator``
(``_read_active_pipeline_checkpoint``, ``_checkpoint_actionable_identity``)
are written as ``_o.<name>`` so they resolve against the live
``orchestrator`` module attribute, matching the pattern proven by
``orchestrator_branch_guard`` / ``orchestrator_post_generation``.  This lets
the test suite's
``monkeypatch.setattr(orchestrator, "_bounded_native_match_extension", ...)``
and the constant monkeypatches
(``monkeypatch.setattr(orchestrator, "ORCH_NATIVE_MATCH_REPROOF_INTERVAL_SEC",
...)`` etc.) continue to drive the bare-global call sites in
``orchestrator`` while the authoritative definitions live here.

All public symbols are re-exported by orchestrator.py for backward
compatibility (thin delegate shells for the functions, plain assignment for
the constants).
"""

from __future__ import annotations

import os
import time

# NOTE: do NOT import orchestrator at top level — orchestrator.py reads this
# module's constants during its own module init (line ~513), so a top-level
# ``import orchestrator as _o`` here creates a circular import. Each function
# below that needs orchestrator-internal helpers does a function-local
# ``import orchestrator as _o`` instead.


# Single, absolute extension ceiling for a checkpoint-bound match, never a
# rolling heartbeat lease or a blanket increase to CYCLE_TIMEOUT.  The frozen
# sidecar phase deadline remains the authoritative lower cap.
ORCH_NATIVE_MATCH_MAX_EXTENSION_HARD_CAP_SEC = 5_960.0
ORCH_NATIVE_MATCH_MAX_EXTENSION_SEC = max(
    0.0,
    min(
        ORCH_NATIVE_MATCH_MAX_EXTENSION_HARD_CAP_SEC,
        float(os.environ.get("POK_ORCH_NATIVE_MATCH_MAX_EXTENSION_SEC", "5960")),
    ),
)
ORCH_NATIVE_MATCH_PROGRESS_MAX_AGE_SEC = 90.0
ORCH_NATIVE_MATCH_REPROOF_INTERVAL_SEC = 5.0


def _bounded_native_match_extension(
    *,
    stream_started_epoch: float,
    original_deadline_epoch: float,
    provider_dispatch_nonce: str | None,
) -> dict | None:
    """Return one eligible engine-match extension, or ``None`` fail-closed."""

    import orchestrator as _o
    try:
        from pipeline_state import (
            native_match_dispatch_nonce_is_active,
            read_pipeline_native_match_progress,
            validate_native_match_progress,
        )

        if not native_match_dispatch_nonce_is_active(provider_dispatch_nonce):
            return None
        checkpoint = _o._read_active_pipeline_checkpoint()
        if not isinstance(checkpoint, dict):
            return None
        progress = read_pipeline_native_match_progress(
            checkpoint,
            now=time.time(),
            max_age=ORCH_NATIVE_MATCH_PROGRESS_MAX_AGE_SEC,
            provider_dispatch_nonce=provider_dispatch_nonce,
        )
        # Revalidate here as well as at sidecar read time.  The bounded
        # extension is a privileged liveness exception and must stay closed
        # even if a caller/test substitutes the read helper's output.
        progress = validate_native_match_progress(
            checkpoint,
            progress,
            now=time.time(),
            provider_dispatch_nonce=provider_dispatch_nonce,
        )
    except Exception:
        return None
    if not isinstance(progress, dict):
        return None
    # The sidecar validator already binds owner->stage, PID/revision, frozen
    # phase budget and this exact provider dispatch nonce.  Timestamp proximity
    # is intentionally not an identity fence: an old tool can begin within a
    # few seconds of a new stream, while an exact owned SDK nonce cannot cross
    # that boundary.
    phase_deadline = float(progress.get("phase_deadline_epoch") or 0.0)
    operation_deadline = float(progress.get("operation_deadline_epoch") or 0.0)
    now = time.time()
    if (
        phase_deadline <= now
        or operation_deadline <= now
        or progress.get("terminal") is not False
        or progress.get("provider_dispatch_nonce") != provider_dispatch_nonce
    ):
        return None
    # Read ORCH_NATIVE_MATCH_MAX_EXTENSION_SEC from the live orchestrator module
    # (not the companion-local copy) so test monkeypatches on
    # orchestrator.ORCH_NATIVE_MATCH_MAX_EXTENSION_SEC take effect.
    absolute_cap = float(original_deadline_epoch) + _o.ORCH_NATIVE_MATCH_MAX_EXTENSION_SEC
    deadline = min(
        phase_deadline,
        operation_deadline,
        absolute_cap,
    )
    if deadline <= now:
        return None
    return {
        "deadline_epoch": deadline,
        "cap_epoch": absolute_cap,
        "checkpoint": checkpoint,
        "checkpoint_identity": _o._checkpoint_actionable_identity(checkpoint),
        "progress": progress,
    }


def _native_match_extension_reproof(previous: dict, fresh: dict | None) -> bool:
    """Prove that a granted extension still belongs to one immutable match."""

    if not isinstance(previous, dict) or not isinstance(fresh, dict):
        return False
    if previous.get("checkpoint_identity") != fresh.get("checkpoint_identity"):
        return False
    if previous.get("cap_epoch") != fresh.get("cap_epoch"):
        return False
    old = previous.get("progress") or {}
    new = fresh.get("progress") or {}
    immutable_fields = (
        "owner_tool",
        "provider_dispatch_nonce",
        "match_identity_digest",
        "timing_plan_digest",
        "hands",
        "effective_timeout_us",
        "operation_started_at_epoch",
        "operation_deadline_epoch",
        "operation_budget_us",
    )
    if any(old.get(field) != new.get(field) for field in immutable_fields):
        return False
    try:
        if int(new.get("event_seq")) < int(old.get("event_seq")):
            return False
    except (TypeError, ValueError):
        return False
    phase_order = {"launching": 0, "engine_running": 1, "finalizing": 2}
    old_phase = str(old.get("liveness_phase") or "")
    new_phase = str(new.get("liveness_phase") or "")
    if old_phase not in phase_order or new_phase not in phase_order:
        return False
    if phase_order[new_phase] < phase_order[old_phase]:
        return False
    old_hand = old.get("hand")
    new_hand = new.get("hand")
    old_hand_order = 0 if old_hand is None else int(old_hand)
    new_hand_order = 0 if new_hand is None else int(new_hand)
    if new_hand_order < old_hand_order:
        return False
    if new_phase == old_phase:
        for field in (
            "phase_started_at_epoch",
            "phase_deadline_epoch",
            "phase_budget_us",
        ):
            if old.get(field) != new.get(field):
                return False
    return True


def _native_match_terminal_handoff_checkpoint_valid(
    extension: dict,
    receipt: dict,
) -> bool:
    """Require the current checkpoint to be the same owner flow or its result."""

    import orchestrator as _o
    old_checkpoint = extension.get("checkpoint") or {}
    current = _o._read_active_pipeline_checkpoint()
    if not isinstance(old_checkpoint, dict) or not isinstance(current, dict):
        return False
    old_workflow = str(
        old_checkpoint.get("workflow_run_id")
        or old_checkpoint.get("run_id")
        or ""
    )
    current_workflow = str(
        current.get("workflow_run_id") or current.get("run_id") or ""
    )
    owner = str(receipt.get("owner_tool") or "")
    allowed_stages = {
        "run_quality_gates": {
            "workers_done",
            "quality_failed",
            "quality_passed",
        },
        "run_precommit_eval": {
            "critic_checked",
            "precommit_failed",
            "verified",
            "infra_timed_out",
        },
    }.get(owner, set())
    try:
        old_revision = int(old_checkpoint.get("checkpoint_revision") or 0)
        current_revision = int(current.get("checkpoint_revision") or 0)
    except (TypeError, ValueError):
        return False
    return bool(
        old_workflow
        and current_workflow == old_workflow
        and current.get("next_v") == old_checkpoint.get("next_v")
        and current.get("source_v") == old_checkpoint.get("source_v")
        and current_revision >= old_revision
        and str(current.get("stage") or "") in allowed_stages
    )


def _consume_native_match_terminal_handoff(
    extension: dict,
    *,
    observed_at_epoch: float,
) -> dict | None:
    """Consume a runner-return receipt for one immutable granted extension."""

    if not isinstance(extension, dict):
        return None
    checkpoint = extension.get("checkpoint")
    progress = extension.get("progress")
    if not isinstance(checkpoint, dict) or not isinstance(progress, dict):
        return None
    try:
        from pipeline_state import consume_native_match_terminal_handoff

        receipt = consume_native_match_terminal_handoff(
            checkpoint,
            progress,
            now=observed_at_epoch,
        )
    except Exception:
        return None
    if not isinstance(receipt, dict):
        return None
    try:
        created_at = float(receipt.get("created_at_epoch"))
        receipt_expiry = float(receipt.get("expires_at_epoch"))
        cap_epoch = float(extension.get("cap_epoch"))
        extension_deadline = float(extension.get("deadline_epoch"))
        operation_deadline = float(progress.get("operation_deadline_epoch"))
        handoff_deadline = min(
            receipt_expiry,
            cap_epoch,
            extension_deadline,
            operation_deadline,
        )
        previous_seq = int(progress.get("event_seq"))
        last_live_seq = int(receipt.get("last_live_event_seq"))
        terminal_seq = int(receipt.get("terminal_event_seq"))
    except (TypeError, ValueError):
        return None
    if (
        receipt.get("terminal_outcome") != "runner_returned"
        or last_live_seq < previous_seq
        or terminal_seq != last_live_seq + 1
        or created_at > observed_at_epoch + 1.0
        or observed_at_epoch > handoff_deadline
        or not _native_match_terminal_handoff_checkpoint_valid(
            extension,
            receipt,
        )
    ):
        return None
    return {
        "receipt": receipt,
        "deadline_epoch": handoff_deadline,
        "checkpoint_identity": extension.get("checkpoint_identity"),
    }


def _native_match_terminal_handoff_reproof(
    state: dict | None,
    *,
    observed_at_epoch: float,
) -> bool:
    """Validate a consumed handoff without extending its fixed expiry."""

    if not isinstance(state, dict):
        return False
    receipt = state.get("receipt") or {}
    try:
        deadline = float(state.get("deadline_epoch"))
    except (TypeError, ValueError):
        return False
    extension = {
        "checkpoint": {
            "workflow_run_id": receipt.get("workflow_run_id"),
            "checkpoint_revision": receipt.get("checkpoint_revision"),
            "stage": receipt.get("stage"),
            "next_v": receipt.get("next_v"),
            "source_v": receipt.get("source_v"),
        },
    }
    return bool(
        receipt.get("terminal_outcome") == "runner_returned"
        and observed_at_epoch <= deadline
        and _native_match_terminal_handoff_checkpoint_valid(extension, receipt)
    )
