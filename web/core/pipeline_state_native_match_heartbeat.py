"""Native-match heartbeat and dispatch-nonce lifecycle for pipeline_state.

Extracted as a cohesive business cluster; pipeline_state.py retains thin
delegate shells so external ``from pipeline_state import <name>`` and
``monkeypatch.setattr(pipeline_state, "<name>", ...)`` keep resolving.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
import uuid

import pipeline_state as _ps  # for cross-refs


def activate_native_match_dispatch_nonce(nonce):
    """Activate one exact provider dispatch for native heartbeat reporters."""

    if not _ps._valid_dispatch_nonce(nonce):
        raise ValueError("native match dispatch nonce is invalid")
    with _ps._NATIVE_MATCH_DISPATCH_LOCK:
        # UUID reuse is already forbidden by the owned-attempt contract.  Drop
        # any process-local debris defensively so a newly activated dispatch
        # can never consume a prior terminal handoff.
        _ps._NATIVE_MATCH_TERMINAL_HANDOFFS.pop(nonce, None)
        _ps._NATIVE_MATCH_DISPATCH_NONCES.add(nonce)
    return _ps._NATIVE_MATCH_DISPATCH_NONCE.set(nonce)


def reset_native_match_dispatch_nonce(token):
    """Reset the current-task binding without revoking the shared dispatch."""

    _ps._NATIVE_MATCH_DISPATCH_NONCE.reset(token)


def native_match_dispatch_nonce_is_active(nonce):
    """Whether a provider dispatch can still authorize native liveness."""

    if not _ps._valid_dispatch_nonce(nonce):
        return False
    with _ps._NATIVE_MATCH_DISPATCH_LOCK:
        return nonce in _ps._NATIVE_MATCH_DISPATCH_NONCES


def current_native_match_dispatch_nonce():
    """Return this task's active provider-dispatch fence, else ``None``."""

    nonce = _ps._NATIVE_MATCH_DISPATCH_NONCE.get()
    return nonce if _ps.native_match_dispatch_nonce_is_active(nonce) else None


def _expected_native_match_timing_plan(checkpoint, owner):
    """Resolve the checkpoint-owned plan allowed to keep a cycle alive.

    The runtime heartbeat is deliberately non-semantic, but it cannot choose
    its own timeout identity.  Quality records its immutable plan in the
    active checkpoint before launching the engine; precommit already records
    it inside its immutable evaluation plan.  If either binding is absent or
    has drifted, the sidecar is ineligible for an orchestrator extension.
    """

    try:
        from national_native import (
            LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
            require_native_match_timing_plan,
        )

        audit_context = (checkpoint or {}).get("audit_context") or {}
        if owner == "run_quality_gates":
            raw_plan = audit_context.get("quality_native_match_timing_plan")
            recorded_digest = audit_context.get(
                "quality_native_match_timing_plan_digest"
            )
            # The active quality contract has one system-owned strict profile;
            # environment variables cannot alter the liveness identity.
            from workflow_profiles import get_workflow_profile

            profile = get_workflow_profile("national_native")
            plan = require_native_match_timing_plan(
                raw_plan,
                hands=int(profile.national_acceptance_hands),
                requested_timeout_sec=float(profile.national_acceptance_timeout_sec),
            )
        elif owner == "run_precommit_eval":
            precommit_plan = audit_context.get("precommit_eval_plan") or {}
            settings = precommit_plan.get("settings") or {}
            raw_plan = settings.get("native_match_timing_plan")
            recorded_digest = settings.get("native_match_timing_plan_digest")
            plan = require_native_match_timing_plan(
                raw_plan,
                hands=70,
                requested_timeout_sec=LOCAL_PRECOMMIT_MATCH_TIMEOUT_SEC,
            )
        else:
            return None
        if recorded_digest != plan.digest():
            return None
        return plan
    except Exception:
        return None


def _native_match_phase_budget_us(plan, event_type, liveness_phase):
    """Return the exact plan-owned budget for one liveness phase."""

    if event_type == "launching" and liveness_phase == "launching":
        # Launch begins before capacity, two read-only artifact preparations,
        # and the aggregate client handshake.  It may refresh freshness but
        # can never move this fixed pre-engine deadline.
        return int(plan.launch_timeout_us)
    if (
        event_type in _ps._NATIVE_MATCH_PROGRESS_EVENTS - {"launching", "finalizing"}
        and liveness_phase == "engine_running"
    ):
        return int(plan.effective_timeout_us)
    if event_type == "finalizing" and liveness_phase == "finalizing":
        # The trusted hand-70 boundary transfers liveness to a fixed phase that
        # covers process drain plus result normalization, spec re-hash, sealed
        # SQLite completion, and replay projection.  It is not a rolling grace.
        return int(plan.finalization_timeout_us)
    return None


def _normalize_native_match_progress(
    checkpoint,
    value,
    *,
    now,
    provider_dispatch_nonce=None,
):
    """Validate the non-semantic, engine-originated native-match projection."""

    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "owner_tool",
        "provider_dispatch_nonce",
        "match_identity_digest",
        "timing_plan_digest",
        "hands",
        "event_seq",
        "event_type",
        "hand",
        "liveness_phase",
        "operation_started_at_epoch",
        "operation_deadline_epoch",
        "operation_budget_us",
        "phase_started_at_epoch",
        "phase_deadline_epoch",
        "phase_budget_us",
        "effective_timeout_us",
        "terminal",
    }:
        return None
    owner = str(value.get("owner_tool") or "")
    expected_plan = _ps._expected_native_match_timing_plan(checkpoint, owner)
    event_type = str(value.get("event_type") or "")
    liveness_phase = str(value.get("liveness_phase") or "")
    hand = value.get("hand")
    if event_type == "launching" and liveness_phase == "launching":
        hand_valid = hand is None
    elif event_type == "finalizing" and liveness_phase == "finalizing":
        hand_valid = _ps._plain_int(hand) and int(hand) == 70
    else:
        hand_valid = _ps._plain_int(hand) and 1 <= int(hand) <= 70
    if (
        value.get("schema_version") != _ps._NATIVE_MATCH_PROGRESS_SCHEMA
        or _ps._NATIVE_MATCH_PROGRESS_OWNERS.get(owner)
        != str(checkpoint.get("stage") or "")
        or expected_plan is None
        or not _ps._valid_dispatch_nonce(value.get("provider_dispatch_nonce"))
        or not _ps._valid_digest(value.get("match_identity_digest"))
        or value.get("timing_plan_digest") != expected_plan.digest()
        or value.get("hands") != 70
        or not _ps._plain_int(value.get("event_seq"))
        or int(value.get("event_seq") or 0) < 1
        or event_type not in _ps._NATIVE_MATCH_PROGRESS_EVENTS
        or not hand_valid
        or value.get("terminal") is not False
        or not _ps._plain_int(value.get("operation_budget_us"))
        or not _ps._plain_int(value.get("phase_budget_us"))
        or not _ps._plain_int(value.get("effective_timeout_us"))
    ):
        return None
    if provider_dispatch_nonce is not None and (
        value.get("provider_dispatch_nonce") != provider_dispatch_nonce
        or not _ps.native_match_dispatch_nonce_is_active(provider_dispatch_nonce)
    ):
        return None
    timeout_us = int(value["effective_timeout_us"])
    # The current fixed profile's full 70-hand envelope is a little over 90
    # minutes.  Keep a deliberately narrow hard ceiling here so a forged
    # runtime sidecar can never authorize an unbounded provider extension.
    if timeout_us != expected_plan.effective_timeout_us:
        return None
    phase_budget_us = _ps._native_match_phase_budget_us(
        expected_plan,
        event_type,
        liveness_phase,
    )
    if phase_budget_us is None or int(value["phase_budget_us"]) != phase_budget_us:
        return None
    try:
        operation_started = float(value.get("operation_started_at_epoch"))
        operation_deadline = float(value.get("operation_deadline_epoch"))
        phase_started = float(value.get("phase_started_at_epoch"))
        phase_deadline = float(value.get("phase_deadline_epoch"))
    except (TypeError, ValueError):
        return None
    operation_budget_us = int(value["operation_budget_us"])
    if (
        operation_budget_us != int(expected_plan.first_strict_lease_timeout_us)
        or not math.isfinite(operation_started)
        or not math.isfinite(operation_deadline)
        or not math.isfinite(phase_started)
        or not math.isfinite(phase_deadline)
        or operation_started <= 0
        or operation_deadline <= operation_started
        or phase_started < operation_started - 2.0
        or phase_deadline <= phase_started
        or operation_started > now + 5.0
        or abs(
            (operation_deadline - operation_started)
            - operation_budget_us / 1_000_000.0
        ) > 2.0
        or abs(
            (phase_deadline - phase_started)
            - phase_budget_us / 1_000_000.0
        ) > 2.0
        or phase_deadline > operation_deadline + 2.0
    ):
        return None
    return {
        "schema_version": _ps._NATIVE_MATCH_PROGRESS_SCHEMA,
        "owner_tool": owner,
        "provider_dispatch_nonce": str(value["provider_dispatch_nonce"]),
        "match_identity_digest": str(value["match_identity_digest"]),
        "timing_plan_digest": str(value["timing_plan_digest"]),
        "hands": 70,
        "event_seq": int(value["event_seq"]),
        "event_type": str(value["event_type"]),
        "hand": None if hand is None else int(hand),
        "liveness_phase": liveness_phase,
        "operation_started_at_epoch": operation_started,
        "operation_deadline_epoch": operation_deadline,
        "operation_budget_us": operation_budget_us,
        "phase_started_at_epoch": phase_started,
        "phase_deadline_epoch": phase_deadline,
        "phase_budget_us": phase_budget_us,
        "effective_timeout_us": timeout_us,
        "terminal": False,
    }


def validate_native_match_progress(
    checkpoint,
    value,
    *,
    now=None,
    provider_dispatch_nonce=None,
):
    """Public fail-closed validator for a checkpoint-bound live match."""

    return _ps._normalize_native_match_progress(
        checkpoint,
        value,
        now=time.time() if now is None else float(now),
        provider_dispatch_nonce=provider_dispatch_nonce,
    )


def write_pipeline_runtime_heartbeat(
    checkpoint,
    *,
    phase,
    audit_attempt=None,
    audit_context=None,
    native_match_progress=None,
):
    """Atomically publish non-semantic in-process liveness for one checkpoint."""
    if not isinstance(checkpoint, dict) or not checkpoint:
        return False
    pid = os.getpid()
    process_start_token = _ps._process_start_token(pid)
    if not process_start_token:
        return False
    now = time.time()
    normalized_native_progress = (
        _ps._normalize_native_match_progress(
            checkpoint,
            native_match_progress,
            now=now,
        )
        if native_match_progress is not None
        else None
    )
    if native_match_progress is not None and normalized_native_progress is None:
        return False
    payload = {
        "schema_version": _ps.PIPELINE_RUNTIME_HEARTBEAT_SCHEMA,
        "checkpoint_identity": _ps._checkpoint_runtime_identity(checkpoint),
        "workflow_run_id": str(
            checkpoint.get("workflow_run_id")
            or checkpoint.get("run_id")
            or ""
        ),
        "checkpoint_revision": int(checkpoint.get("checkpoint_revision") or 0),
        "next_v": checkpoint.get("next_v"),
        "source_v": checkpoint.get("source_v"),
        "stage": str(checkpoint.get("stage") or ""),
        "phase": str(phase),
        "audit_attempt": (
            int(audit_attempt) if audit_attempt is not None else None
        ),
        "audit_context_digest": (
            hashlib.sha256(
                json.dumps(
                    audit_context,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            if audit_context is not None
            else ""
        ),
        "pid": pid,
        "process_start_token": process_start_token,
        "written_at": now,
        "native_match_progress": normalized_native_progress,
    }
    path = _ps.PIPELINE_RUNTIME_HEARTBEAT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{pid}.{uuid.uuid4().hex}.tmp")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    dispatch_nonce = (
        str(normalized_native_progress.get("provider_dispatch_nonce") or "")
        if isinstance(normalized_native_progress, dict)
        else ""
    )

    def write_locked():
        """Commit while the caller owns the heartbeat lock."""

        try:
            with open(temporary, "x", encoding="utf-8") as writer:
                writer.write(encoded)
                writer.flush()
                os.fsync(writer.fileno())
            os.replace(temporary, path)
            if dispatch_nonce:
                # A new live match under the same provider attempt atomically
                # supersedes a prior terminal handoff.  One completed match
                # can therefore never license the next match or a later model
                # stall in the same SDK stream.
                _ps._NATIVE_MATCH_TERMINAL_HANDOFFS.pop(dispatch_nonce, None)
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return True
        finally:
            temporary.unlink(missing_ok=True)

    if dispatch_nonce:
        # Keep the lock order fixed everywhere: dispatch ownership first,
        # heartbeat/receipt state second.  Re-check under that ownership lock
        # so cancellation cannot race a previously normalized write back into
        # existence after revocation.
        with _ps._NATIVE_MATCH_DISPATCH_LOCK:
            if dispatch_nonce not in _ps._NATIVE_MATCH_DISPATCH_NONCES:
                temporary.unlink(missing_ok=True)
                return False
            with _ps._PIPELINE_RUNTIME_HEARTBEAT_LOCK:
                return write_locked()
    with _ps._PIPELINE_RUNTIME_HEARTBEAT_LOCK:
        return write_locked()


def read_pipeline_runtime_heartbeat(checkpoint, *, now=None, max_age=None):
    """Read a live, identity-bound heartbeat; reject restart/PID/stage debris."""
    if not isinstance(checkpoint, dict) or not checkpoint:
        return None
    try:
        with _ps._PIPELINE_RUNTIME_HEARTBEAT_LOCK:
            encoded = _ps.PIPELINE_RUNTIME_HEARTBEAT_FILE.read_text(encoding="utf-8")
        payload = json.loads(encoded)
        if not isinstance(payload, dict):
            return None
        if payload.get("schema_version") != _ps.PIPELINE_RUNTIME_HEARTBEAT_SCHEMA:
            return None
        if payload.get("checkpoint_identity") != _ps._checkpoint_runtime_identity(checkpoint):
            return None
        if int(payload.get("checkpoint_revision") or 0) != int(
            checkpoint.get("checkpoint_revision") or 0
        ):
            return None
        if str(payload.get("stage") or "") != str(checkpoint.get("stage") or ""):
            return None
        pid = int(payload.get("pid") or 0)
        if not pid or payload.get("process_start_token") != _ps._process_start_token(pid):
            return None
        written_at = float(payload.get("written_at") or 0.0)
        current = time.time() if now is None else float(now)
        if (
            not math.isfinite(written_at)
            or written_at <= 0
            or written_at > current + 5.0
        ):
            return None
        if max_age is not None and current - written_at > float(max_age):
            return None
        native_progress = payload.get("native_match_progress")
        if native_progress is not None:
            normalized_native_progress = _ps.validate_native_match_progress(
                checkpoint,
                native_progress,
                now=current,
            )
            if (
                normalized_native_progress is None
                or not _ps.native_match_dispatch_nonce_is_active(
                    normalized_native_progress.get("provider_dispatch_nonce")
                )
            ):
                return None
            payload["native_match_progress"] = normalized_native_progress
        return payload
    except Exception:
        return None


def publish_native_match_terminal_handoff(
    checkpoint,
    *,
    owner_tool,
    provider_dispatch_nonce,
    match_identity_digest,
    timing_plan_digest,
    terminal_outcome,
    now=None,
):
    """Atomically replace one exact live match with a short one-shot receipt.

    The receipt is process-local non-semantic control state.  It exists only
    to bridge the tiny interval between the trusted runner returning and the
    owning provider stream yielding its final response.  A restart loses it
    deliberately; recovery must use the durable checkpoint, never recreate a
    liveness capability from disk.
    """

    owner = str(owner_tool or "")
    nonce = str(provider_dispatch_nonce or "")
    match_digest = str(match_identity_digest or "")
    timing_digest = str(timing_plan_digest or "")
    outcome = str(terminal_outcome or "")
    if (
        not isinstance(checkpoint, dict)
        or not checkpoint
        or _ps._NATIVE_MATCH_PROGRESS_OWNERS.get(owner)
        != str(checkpoint.get("stage") or "")
        or not _ps._valid_dispatch_nonce(nonce)
        or not _ps._valid_digest(match_digest)
        or not _ps._valid_digest(timing_digest)
        or outcome not in {
            "runner_returned",
            "runner_raised",
            "runner_cancelled",
        }
    ):
        return False
    created_at = time.time() if now is None else float(now)
    if not math.isfinite(created_at) or created_at <= 0.0:
        return False
    path = _ps.PIPELINE_RUNTIME_HEARTBEAT_FILE
    with _ps._NATIVE_MATCH_DISPATCH_LOCK:
        if nonce not in _ps._NATIVE_MATCH_DISPATCH_NONCES:
            _ps._NATIVE_MATCH_TERMINAL_HANDOFFS.pop(nonce, None)
            return False
        with _ps._PIPELINE_RUNTIME_HEARTBEAT_LOCK:
            heartbeat = _ps.read_pipeline_runtime_heartbeat(
                checkpoint,
                now=created_at,
                max_age=300.0,
            )
            progress = (heartbeat or {}).get("native_match_progress")
            if (
                not isinstance(progress, dict)
                or progress.get("owner_tool") != owner
                or progress.get("provider_dispatch_nonce") != nonce
                or progress.get("match_identity_digest") != match_digest
                or progress.get("timing_plan_digest") != timing_digest
            ):
                _ps._NATIVE_MATCH_TERMINAL_HANDOFFS.pop(nonce, None)
                return False
            try:
                last_event_seq = int(progress.get("event_seq"))
                operation_deadline = float(
                    progress.get("operation_deadline_epoch")
                )
                phase_deadline = float(progress.get("phase_deadline_epoch"))
            except (TypeError, ValueError):
                _ps._NATIVE_MATCH_TERMINAL_HANDOFFS.pop(nonce, None)
                return False
            expires_at = min(
                created_at + _ps.NATIVE_MATCH_TERMINAL_HANDOFF_MAX_AGE_SEC,
                operation_deadline,
                phase_deadline,
            )
            if last_event_seq < 1 or expires_at <= created_at:
                _ps._NATIVE_MATCH_TERMINAL_HANDOFFS.pop(nonce, None)
                return False
            receipt = {
                "schema_version": _ps._NATIVE_MATCH_TERMINAL_HANDOFF_SCHEMA,
                "owner_tool": owner,
                "provider_dispatch_nonce": nonce,
                "match_identity_digest": match_digest,
                "timing_plan_digest": timing_digest,
                "checkpoint_identity": _ps._checkpoint_runtime_identity(checkpoint),
                "workflow_run_id": str(
                    checkpoint.get("workflow_run_id")
                    or checkpoint.get("run_id")
                    or ""
                ),
                "checkpoint_revision": int(
                    checkpoint.get("checkpoint_revision") or 0
                ),
                "stage": str(checkpoint.get("stage") or ""),
                "next_v": checkpoint.get("next_v"),
                "source_v": checkpoint.get("source_v"),
                "hands": progress.get("hands"),
                "last_live_event_seq": last_event_seq,
                "terminal_event_seq": last_event_seq + 1,
                "last_live_event_type": progress.get("event_type"),
                "last_live_hand": progress.get("hand"),
                "last_live_liveness_phase": progress.get("liveness_phase"),
                "operation_started_at_epoch": progress.get(
                    "operation_started_at_epoch"
                ),
                "operation_deadline_epoch": operation_deadline,
                "operation_budget_us": progress.get("operation_budget_us"),
                "phase_started_at_epoch": progress.get(
                    "phase_started_at_epoch"
                ),
                "phase_deadline_epoch": phase_deadline,
                "phase_budget_us": progress.get("phase_budget_us"),
                "effective_timeout_us": progress.get("effective_timeout_us"),
                "terminal_outcome": outcome,
                "created_at_epoch": created_at,
                "expires_at_epoch": expires_at,
            }
            _ps._NATIVE_MATCH_TERMINAL_HANDOFFS[nonce] = receipt
            try:
                path.unlink()
            except Exception:
                # Live removal and receipt publication are one in-process
                # state transition.  If unlink did not complete, neither side
                # may claim terminal authority.
                _ps._NATIVE_MATCH_TERMINAL_HANDOFFS.pop(nonce, None)
                return False
            return True


def consume_native_match_terminal_handoff(
    checkpoint,
    expected_progress,
    *,
    now=None,
):
    """Consume exactly once a terminal receipt matching one granted extension."""

    if not isinstance(checkpoint, dict) or not isinstance(expected_progress, dict):
        return None
    nonce = str(expected_progress.get("provider_dispatch_nonce") or "")
    current = time.time() if now is None else float(now)
    if not _ps._valid_dispatch_nonce(nonce) or not math.isfinite(current):
        return None
    with _ps._NATIVE_MATCH_DISPATCH_LOCK:
        with _ps._PIPELINE_RUNTIME_HEARTBEAT_LOCK:
            receipt = _ps._NATIVE_MATCH_TERMINAL_HANDOFFS.pop(nonce, None)
        active = nonce in _ps._NATIVE_MATCH_DISPATCH_NONCES
    if not active or not isinstance(receipt, dict):
        return None
    expected_fields = {
        "schema_version",
        "owner_tool",
        "provider_dispatch_nonce",
        "match_identity_digest",
        "timing_plan_digest",
        "checkpoint_identity",
        "workflow_run_id",
        "checkpoint_revision",
        "stage",
        "next_v",
        "source_v",
        "hands",
        "last_live_event_seq",
        "terminal_event_seq",
        "last_live_event_type",
        "last_live_hand",
        "last_live_liveness_phase",
        "operation_started_at_epoch",
        "operation_deadline_epoch",
        "operation_budget_us",
        "phase_started_at_epoch",
        "phase_deadline_epoch",
        "phase_budget_us",
        "effective_timeout_us",
        "terminal_outcome",
        "created_at_epoch",
        "expires_at_epoch",
    }
    if set(receipt) != expected_fields:
        return None
    workflow_run_id = str(
        checkpoint.get("workflow_run_id") or checkpoint.get("run_id") or ""
    )
    immutable_progress_fields = (
        "owner_tool",
        "provider_dispatch_nonce",
        "match_identity_digest",
        "timing_plan_digest",
        "hands",
        "operation_started_at_epoch",
        "operation_deadline_epoch",
        "operation_budget_us",
        "effective_timeout_us",
    )
    if (
        receipt.get("schema_version")
        != _ps._NATIVE_MATCH_TERMINAL_HANDOFF_SCHEMA
        or receipt.get("checkpoint_identity")
        != _ps._checkpoint_runtime_identity(checkpoint)
        or receipt.get("workflow_run_id") != workflow_run_id
        or receipt.get("checkpoint_revision")
        != int(checkpoint.get("checkpoint_revision") or 0)
        or receipt.get("stage") != str(checkpoint.get("stage") or "")
        or receipt.get("next_v") != checkpoint.get("next_v")
        or receipt.get("source_v") != checkpoint.get("source_v")
        or any(
            receipt.get(field) != expected_progress.get(field)
            for field in immutable_progress_fields
        )
    ):
        return None
    try:
        last_live_seq = int(receipt.get("last_live_event_seq"))
        terminal_seq = int(receipt.get("terminal_event_seq"))
        expected_seq = int(expected_progress.get("event_seq"))
        created_at = float(receipt.get("created_at_epoch"))
        expires_at = float(receipt.get("expires_at_epoch"))
        operation_deadline = float(receipt.get("operation_deadline_epoch"))
        phase_deadline = float(receipt.get("phase_deadline_epoch"))
    except (TypeError, ValueError):
        return None
    expected_expiry = min(
        created_at + _ps.NATIVE_MATCH_TERMINAL_HANDOFF_MAX_AGE_SEC,
        operation_deadline,
        phase_deadline,
    )
    if (
        last_live_seq < expected_seq
        or terminal_seq != last_live_seq + 1
        or not math.isfinite(created_at)
        or not math.isfinite(expires_at)
        or expires_at != expected_expiry
        or created_at > current + 1.0
        or current > expires_at
        or receipt.get("terminal_outcome")
        not in {"runner_returned", "runner_raised", "runner_cancelled"}
    ):
        return None
    return dict(receipt)


def make_native_match_heartbeat_reporter(
    checkpoint,
    *,
    owner_tool,
    provider_dispatch_nonce=None,
):
    """Create a throttled reporter for trusted native-engine progress only.

    The returned callback accepts the sanitized match projection built by
    ``national_native``.  It is deliberately runtime-only and does not write
    a checkpoint, evidence snapshot, or rating history.
    """

    owner = str(owner_tool)
    if _ps._NATIVE_MATCH_PROGRESS_OWNERS.get(owner) != str(
        (checkpoint or {}).get("stage") or ""
    ):
        return None
    expected_plan = _ps._expected_native_match_timing_plan(checkpoint, owner)
    if expected_plan is None:
        return None
    dispatch_nonce = (
        provider_dispatch_nonce
        if provider_dispatch_nonce is not None
        else _ps.current_native_match_dispatch_nonce()
    )
    # A manual/test caller may pass the nonce explicitly, but it still has to
    # be an active owned dispatch.  A sidecar created outside an SDK attempt
    # cannot keep any provider stream alive.
    if not _ps.native_match_dispatch_nonce_is_active(dispatch_nonce):
        return None
    event_seq = 0
    last_written = 0.0

    async def report(progress):
        nonlocal event_seq, last_written
        if not isinstance(progress, dict):
            return False
        # The runner sends terminal only at its outer return/raise boundary.
        # Convert the exact live state to a short process-local receipt before
        # removing it; an old completion must never delete or authorize a
        # concurrent newer match sidecar.
        if progress.get("terminal") is True:
            handed_off = _ps.publish_native_match_terminal_handoff(
                checkpoint,
                owner_tool=owner,
                provider_dispatch_nonce=dispatch_nonce,
                match_identity_digest=progress.get("match_identity_digest"),
                timing_plan_digest=progress.get("timing_plan_digest"),
                terminal_outcome=progress.get("terminal_outcome"),
            )
            if handed_off:
                return True
            # A rejected conversion/unlink must not leave this provider dispatch
            # eligible to borrow a stale hand-70 heartbeat.  Registry
            # revocation is immediate and the exact cleanup helper makes one
            # final best-effort unlink without touching another dispatch.
            _ps.revoke_native_match_dispatch_nonce(dispatch_nonce)
            return False
        if not _ps.native_match_dispatch_nonce_is_active(dispatch_nonce):
            return False
        if (
            progress.get("timing_plan_digest") != expected_plan.digest()
            or progress.get("effective_timeout_us")
            != expected_plan.effective_timeout_us
            or progress.get("hands") != expected_plan.hands
        ):
            return False
        event_seq += 1
        now = time.time()
        event_type = str(progress.get("event_type") or "")
        # Every hand boundary is meaningful; actions otherwise coalesce to a
        # modest cadence.  The sequence remains monotonic across coalescing.
        if (
            event_type not in {
                "launching",
                "engine_started",
                "hand_start",
                "settle",
                "finalizing",
            }
            and now - last_written < 15.0
        ):
            return True
        payload = {
            "schema_version": _ps._NATIVE_MATCH_PROGRESS_SCHEMA,
            "owner_tool": owner,
            "provider_dispatch_nonce": dispatch_nonce,
            "match_identity_digest": progress.get("match_identity_digest"),
            "timing_plan_digest": progress.get("timing_plan_digest"),
            "hands": progress.get("hands"),
            "event_seq": event_seq,
            "event_type": event_type,
            "hand": progress.get("hand"),
            "liveness_phase": progress.get("liveness_phase"),
            "operation_started_at_epoch": progress.get(
                "operation_started_at_epoch"
            ),
            "operation_deadline_epoch": progress.get(
                "operation_deadline_epoch"
            ),
            "operation_budget_us": progress.get("operation_budget_us"),
            "phase_started_at_epoch": progress.get("phase_started_at_epoch"),
            "phase_deadline_epoch": progress.get("phase_deadline_epoch"),
            "phase_budget_us": progress.get("phase_budget_us"),
            "effective_timeout_us": progress.get("effective_timeout_us"),
            "terminal": False,
        }
        written = _ps.write_pipeline_runtime_heartbeat(
            checkpoint,
            phase="native_match_progress",
            native_match_progress=payload,
        )
        if written:
            last_written = now
        return written

    return report


def clear_pipeline_native_match_heartbeat(
    checkpoint=None,
    *,
    owner_tool=None,
    provider_dispatch_nonce,
    match_identity_digest=None,
    timing_plan_digest=None,
):
    """Clear one exact native sidecar without racing another live match.

    Runtime sidecars are a single file, so a broad checkpoint-only unlink is
    insufficient when cancellation and a new provider dispatch overlap.  The
    caller has to prove the dispatch nonce and, for terminal cleanup, the
    match/timing identity it intends to remove.
    """

    if not _ps._valid_dispatch_nonce(provider_dispatch_nonce):
        return False
    if match_identity_digest is not None and not _ps._valid_digest(match_identity_digest):
        return False
    if timing_plan_digest is not None and not _ps._valid_digest(timing_plan_digest):
        return False
    path = _ps.PIPELINE_RUNTIME_HEARTBEAT_FILE
    with _ps._PIPELINE_RUNTIME_HEARTBEAT_LOCK:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return True
        except Exception:
            return False
        if not isinstance(payload, dict):
            return False
        if checkpoint is not None and (
            payload.get("checkpoint_identity")
            != _ps._checkpoint_runtime_identity(checkpoint)
        ):
            return False
        progress = payload.get("native_match_progress")
        if not isinstance(progress, dict):
            return False
        if progress.get("provider_dispatch_nonce") != provider_dispatch_nonce:
            return False
        if owner_tool is not None and progress.get("owner_tool") != owner_tool:
            return False
        if (
            match_identity_digest is not None
            and progress.get("match_identity_digest") != match_identity_digest
        ):
            return False
        if (
            timing_plan_digest is not None
            and progress.get("timing_plan_digest") != timing_plan_digest
        ):
            return False
        try:
            path.unlink(missing_ok=True)
            return True
        except Exception:
            return False


def revoke_native_match_dispatch_nonce(nonce):
    """Revoke one provider dispatch and remove its live/terminal control state."""

    if not _ps._valid_dispatch_nonce(nonce):
        return False
    with _ps._NATIVE_MATCH_DISPATCH_LOCK:
        _ps._NATIVE_MATCH_DISPATCH_NONCES.discard(nonce)
        _ps._NATIVE_MATCH_TERMINAL_HANDOFFS.pop(nonce, None)
    # A detached tool callback may retain the ContextVar after its provider
    # task is cancelled.  Registry revocation plus exact sidecar removal keeps
    # it from extending any later stream.
    return _ps.clear_pipeline_native_match_heartbeat(
        provider_dispatch_nonce=nonce,
    )
