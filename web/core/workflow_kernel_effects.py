"""Companion mixin for the fenced effect-lease cluster.

Extracted from workflow_kernel.py.  This module holds the eight
fenced-effect lifecycle methods that consume and renew/cancel/reclaim/fence
effect leases.  They are grouped because they share a cohesive responsibility
(atomic lease state-machine transitions) and together account for the bulk of
the kernel's line count.  All other kernel surface -- the event stream,
schema, command locks, event append/projection helpers, request_effect,
effect/effects_for_run/pending_outbox, complete_effect, and
set_instance_status -- remains in workflow_kernel.py.

Architecture
------------
_WorkflowEffectsMixin is mixed into WorkflowStore::

    class WorkflowStore(_WorkflowEffectsMixin):
        ...

so every extracted method is still reached as store.claim_effect(...) and
WorkflowStore.claim_effect.  The mixin references only:

* self helpers defined on WorkflowStore itself (_connect,
  _append_event_locked, _effect_from_row, effect) -- resolved via
  normal MRO at call time; and
* the kernel's module-level utility symbols (EffectLease,
  WorkflowConflict, WorkflowBusy, InvalidCompletion,
  canonical_json, content_digest, KERNEL_SCHEMA_VERSION) -- imported
  verbatim from workflow_kernel at the top of this file.

No method in this cluster is monkeypatched on the class by the test suite (the
only monkeypatch.setattr touching these names targets an *instance*
attribute, which shadows class and mixin attributes alike), and none of the
imported utility symbols is monkeypatched either.  Behaviour and signatures
are byte-for-byte identical to the pre-extraction implementation.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any

from workflow_kernel import (  # noqa: F401  (re-exported for type checks)
    KERNEL_SCHEMA_VERSION,
    canonical_json,
    content_digest,
)
from workflow_kernel import EffectLease
from workflow_kernel import InvalidCompletion
from workflow_kernel import WorkflowBusy
from workflow_kernel import WorkflowConflict


class _WorkflowEffectsMixin:
    """Fenced effect-lease state-machine transitions for WorkflowStore.

    Mixed into WorkflowStore; not instantiated standalone.
    """

    def claim_effect(
        self,
        effect_id: str,
        *,
        owner: str,
        lease_seconds: float,
        now: float | None = None,
    ) -> EffectLease:
        current_time = float(now if now is not None else time.time())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM effects WHERE effect_id = ?",
                (effect_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise WorkflowConflict(f"unknown effect: {effect_id}")
            if row["status"] in {"completed", "exhausted", "abandoned"}:
                connection.rollback()
                raise WorkflowConflict(
                    f"effect {effect_id} is terminal: {row['status']}"
                )
            if row["status"] == "deferred":
                connection.rollback()
                raise WorkflowConflict(
                    f"effect {effect_id} is deferred pending explicit resume"
                )
            lease_until = row["lease_until"]
            if (
                row["status"] == "running"
                and lease_until is not None
                and float(lease_until) > current_time
            ):
                connection.rollback()
                raise WorkflowBusy(f"effect lease is active: {effect_id}")
            attempt = int(row["attempt"]) + 1
            max_attempts = int(row["max_attempts"])
            if attempt > max_attempts:
                payload = {
                    "effect_id": effect_id,
                    "attempt": int(row["attempt"]),
                    "lease_epoch": int(row["lease_epoch"]),
                    "retryable": False,
                    "error": "effect lease attempt budget exhausted",
                }
                self._append_event_locked(
                    connection,
                    run_id=str(row["run_id"]),
                    event_type="EffectFailed",
                    payload=payload,
                    causation_id=(
                        f"effect-lease-exhausted:{effect_id}:"
                        f"{int(row['lease_epoch'])}"
                    ),
                    expected_version=None,
                    schema_version=KERNEL_SCHEMA_VERSION,
                )
                connection.execute(
                    "UPDATE effects SET status = 'exhausted', updated_at = ? WHERE effect_id = ?",
                    (current_time, effect_id),
                )
                connection.execute(
                    """
                    UPDATE outbox
                    SET dispatched_at = COALESCE(dispatched_at, ?)
                    WHERE effect_id = ?
                    """,
                    (current_time, effect_id),
                )
                connection.commit()
                raise WorkflowConflict(f"effect attempt budget exhausted: {effect_id}")
            epoch = int(row["lease_epoch"]) + 1
            expires = current_time + max(0.001, float(lease_seconds))
            connection.execute(
                """
                UPDATE effects
                SET status = 'running', attempt = ?, lease_epoch = ?,
                    lease_owner = ?, lease_until = ?, updated_at = ?
                WHERE effect_id = ?
                """,
                (attempt, epoch, owner, expires, current_time, effect_id),
            )
            connection.execute(
                "UPDATE outbox SET dispatched_at = COALESCE(dispatched_at, ?) WHERE effect_id = ?",
                (current_time, effect_id),
            )
            connection.commit()
        return EffectLease(
            effect_id=effect_id,
            run_id=str(row["run_id"]),
            kind=str(row["kind"]),
            input_digest=str(row["input_digest"]),
            attempt=attempt,
            max_attempts=max_attempts,
            lease_epoch=epoch,
            lease_until=expires,
            status="running",
        )

    def renew_effect_lease(
        self,
        effect_id: str,
        *,
        owner: str,
        lease_epoch: int,
        lease_seconds: float,
        causation_id: str,
        now: float | None = None,
    ) -> EffectLease:
        """Heartbeat one exact live lease without consuming an attempt.

        Renewal is an owner/epoch/status compare-and-swap.  It never revives an
        expired or terminal lease, never changes the attempt or lease epoch,
        and records the heartbeat and new boundary in the same transaction as
        the effect row update.  The caller freezes ``now`` for idempotent
        command retries; reusing a causation id with different timing fails.
        """

        if (
            not isinstance(owner, str)
            or not owner
            or not isinstance(causation_id, str)
            or not causation_id
            or isinstance(lease_epoch, bool)
            or not isinstance(lease_epoch, int)
            or lease_epoch < 1
        ):
            raise ValueError("effect lease heartbeat identity is invalid")
        current_time = float(now if now is not None else time.time())
        lease_duration = float(lease_seconds)
        if (
            not math.isfinite(current_time)
            or not math.isfinite(lease_duration)
            or lease_duration <= 0
        ):
            raise ValueError("effect lease heartbeat timing is invalid")

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM effects WHERE effect_id = ?",
                (effect_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise WorkflowConflict(f"unknown effect: {effect_id}")

            duplicate = connection.execute(
                "SELECT * FROM workflow_events WHERE run_id = ? AND causation_id = ?",
                (str(row["run_id"]), causation_id),
            ).fetchone()
            if duplicate is not None:
                payload = json.loads(str(duplicate["payload"]))
                exact_retry = bool(
                    str(duplicate["event_type"]) == "EffectLeaseHeartbeat"
                    and payload.get("effect_id") == effect_id
                    and payload.get("lease_owner") == owner
                    and payload.get("lease_epoch") == lease_epoch
                    and payload.get("heartbeat_at") == current_time
                    and payload.get("lease_seconds") == lease_duration
                )
                if not exact_retry:
                    connection.rollback()
                    raise WorkflowConflict(
                        f"causation id reused with different event: {causation_id}"
                    )
                if (
                    row["status"] != "running"
                    or str(row["lease_owner"] or "") != owner
                    or int(row["lease_epoch"] or 0) != lease_epoch
                    or row["lease_until"] is None
                    or float(row["lease_until"]) < float(payload["lease_until"])
                ):
                    connection.rollback()
                    raise InvalidCompletion(
                        f"stale effect lease heartbeat for {effect_id} "
                        f"epoch={lease_epoch}"
                    )
                connection.commit()
                return EffectLease(
                    effect_id=effect_id,
                    run_id=str(row["run_id"]),
                    kind=str(row["kind"]),
                    input_digest=str(row["input_digest"]),
                    attempt=int(row["attempt"]),
                    max_attempts=int(row["max_attempts"]),
                    lease_epoch=int(row["lease_epoch"]),
                    lease_until=float(row["lease_until"]),
                    status="running",
                )

            previous_until = row["lease_until"]
            if (
                row["status"] != "running"
                or str(row["lease_owner"] or "") != owner
                or int(row["lease_epoch"] or 0) != lease_epoch
                or previous_until is None
                or float(previous_until) <= current_time
            ):
                connection.rollback()
                raise InvalidCompletion(
                    f"stale effect lease heartbeat for {effect_id} "
                    f"epoch={lease_epoch}"
                )
            expires = max(float(previous_until), current_time + lease_duration)
            payload = {
                "effect_id": effect_id,
                "attempt": int(row["attempt"]),
                "lease_epoch": lease_epoch,
                "lease_owner": owner,
                "heartbeat_at": current_time,
                "lease_seconds": lease_duration,
                "previous_lease_until": float(previous_until),
                "lease_until": expires,
            }
            try:
                self._append_event_locked(
                    connection,
                    run_id=str(row["run_id"]),
                    event_type="EffectLeaseHeartbeat",
                    payload=payload,
                    causation_id=causation_id,
                    expected_version=None,
                    schema_version=KERNEL_SCHEMA_VERSION,
                )
                connection.execute(
                    """
                    UPDATE effects
                    SET lease_until = ?, updated_at = ?
                    WHERE effect_id = ? AND status = 'running'
                      AND lease_owner = ? AND lease_epoch = ?
                      AND lease_until = ?
                    """,
                    (
                        expires,
                        current_time,
                        effect_id,
                        owner,
                        lease_epoch,
                        float(previous_until),
                    ),
                )
                changed = connection.execute("SELECT changes()").fetchone()[0]
                if int(changed or 0) != 1:
                    raise WorkflowConflict(
                        f"effect lease heartbeat CAS failed: {effect_id}"
                    )
            except Exception:
                connection.rollback()
                raise
            connection.commit()
        return EffectLease(
            effect_id=effect_id,
            run_id=str(row["run_id"]),
            kind=str(row["kind"]),
            input_digest=str(row["input_digest"]),
            attempt=int(row["attempt"]),
            max_attempts=int(row["max_attempts"]),
            lease_epoch=lease_epoch,
            lease_until=expires,
            status="running",
        )

    def cancel_effect(
        self,
        effect_id: str,
        *,
        expected_status: str,
        expected_attempt: int,
        expected_lease_epoch: int,
        expected_owner: str | None,
        reason: str,
        causation_id: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Cancel one exact non-terminal effect and fence late completions.

        The caller must bind the status, lease epoch and owner it observed.
        Running effects require their exact owner; unowned queue/deferred
        states require ``expected_owner=None``.  This is deliberately narrower
        than the run-wide :meth:`terminal_transition`.
        """

        if (
            expected_status not in {"requested", "retry", "deferred", "running"}
            or isinstance(expected_attempt, bool)
            or not isinstance(expected_attempt, int)
            or expected_attempt < 0
            or isinstance(expected_lease_epoch, bool)
            or not isinstance(expected_lease_epoch, int)
            or expected_lease_epoch < 0
            or (expected_owner is not None and (
                not isinstance(expected_owner, str) or not expected_owner
            ))
            or not isinstance(reason, str)
            or not reason.strip()
            or not isinstance(causation_id, str)
            or not causation_id
        ):
            raise ValueError("effect cancellation identity is invalid")
        if (expected_status == "running") != (expected_owner is not None):
            raise ValueError("running effect cancellation owner is invalid")
        current_time = float(now if now is not None else time.time())
        if not math.isfinite(current_time):
            raise ValueError("effect cancellation timing is invalid")
        normalized_reason = reason.strip()[:2000]

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM effects WHERE effect_id = ?",
                (effect_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise WorkflowConflict(f"unknown effect: {effect_id}")

            duplicate = connection.execute(
                "SELECT * FROM workflow_events WHERE run_id = ? AND causation_id = ?",
                (str(row["run_id"]), causation_id),
            ).fetchone()
            if duplicate is not None:
                payload = json.loads(str(duplicate["payload"]))
                if not (
                    str(duplicate["event_type"]) == "EffectCancelled"
                    and payload.get("effect_id") == effect_id
                    and payload.get("previous_status") == expected_status
                    and payload.get("attempt") == expected_attempt
                    and payload.get("lease_epoch") == expected_lease_epoch
                    and payload.get("lease_owner") == expected_owner
                    and payload.get("reason") == normalized_reason
                    and payload.get("cancelled_at") == current_time
                ):
                    connection.rollback()
                    raise WorkflowConflict(
                        f"causation id reused with different event: {causation_id}"
                    )
                if row["status"] != "abandoned":
                    connection.rollback()
                    raise WorkflowConflict(
                        f"cancelled effect changed state: {effect_id}"
                    )
                connection.commit()
                return self._effect_from_row(row)

            observed_owner = (
                str(row["lease_owner"]) if row["lease_owner"] is not None else None
            )
            if (
                str(row["status"]) != expected_status
                or int(row["attempt"] or 0) != expected_attempt
                or int(row["lease_epoch"] or 0) != expected_lease_epoch
                or observed_owner != expected_owner
                or (
                    expected_status == "running"
                    and (
                        row["lease_until"] is None
                        or float(row["lease_until"]) <= current_time
                    )
                )
            ):
                connection.rollback()
                raise WorkflowConflict(
                    f"stale effect cancellation for {effect_id}"
                )
            payload = {
                "effect_id": effect_id,
                "previous_status": expected_status,
                "attempt": expected_attempt,
                "lease_epoch": expected_lease_epoch,
                "lease_owner": expected_owner,
                "reason": normalized_reason,
                "cancelled_at": current_time,
            }
            try:
                self._append_event_locked(
                    connection,
                    run_id=str(row["run_id"]),
                    event_type="EffectCancelled",
                    payload=payload,
                    causation_id=causation_id,
                    expected_version=None,
                    schema_version=KERNEL_SCHEMA_VERSION,
                )
                connection.execute(
                    """
                    UPDATE effects
                    SET status = 'abandoned', lease_owner = NULL,
                        lease_until = NULL, last_error = ?, updated_at = ?
                    WHERE effect_id = ? AND status = ? AND attempt = ?
                      AND lease_epoch = ?
                      AND (
                          (lease_owner IS NULL AND ? IS NULL)
                          OR lease_owner = ?
                      )
                    """,
                    (
                        normalized_reason,
                        current_time,
                        effect_id,
                        expected_status,
                        expected_attempt,
                        expected_lease_epoch,
                        expected_owner,
                        expected_owner,
                    ),
                )
                changed = connection.execute("SELECT changes()").fetchone()[0]
                if int(changed or 0) != 1:
                    raise WorkflowConflict(
                        f"effect cancellation CAS failed: {effect_id}"
                    )
                connection.execute(
                    """
                    UPDATE outbox
                    SET dispatched_at = COALESCE(dispatched_at, ?)
                    WHERE effect_id = ?
                    """,
                    (current_time, effect_id),
                )
            except Exception:
                connection.rollback()
                raise
            connection.commit()
        return self.effect(effect_id)

    def reclaim_effect_lease(
        self,
        effect_id: str,
        *,
        expected_owner: str,
        expected_lease_epoch: int,
        owner: str,
        lease_seconds: float,
        causation_id: str,
        proof: dict[str, Any],
        now: float | None = None,
    ) -> EffectLease:
        """Atomically reclaim one exact lease after domain-owned death proof.

        The kernel deliberately does not decide whether a process is dead; the
        domain owns that policy.  The proof event, attempt increment, lease
        epoch fence, and replacement owner are one SQLite transaction.  There
        is therefore no crash/concurrency window in which the old epoch can
        complete after a nominal fence but before a second claim transaction.
        """

        if (
            not isinstance(effect_id, str)
            or not effect_id
            or not isinstance(expected_owner, str)
            or not expected_owner
            or not isinstance(owner, str)
            or not owner
            or not isinstance(causation_id, str)
            or not causation_id
            or not isinstance(proof, dict)
            or not proof
            or isinstance(expected_lease_epoch, bool)
            or not isinstance(expected_lease_epoch, int)
            or expected_lease_epoch < 1
        ):
            raise ValueError("effect lease reclaim identity is invalid")
        normalized_proof = json.loads(canonical_json(proof))
        proof_digest = normalized_proof.get("proof_digest")
        unsigned_proof = {
            key: value
            for key, value in normalized_proof.items()
            if key != "proof_digest"
        }
        if (
            not isinstance(proof_digest, str)
            or len(proof_digest) != 64
            or proof_digest != content_digest(unsigned_proof)
            or normalized_proof.get("owner") != expected_owner
        ):
            raise ValueError("effect lease reclaim proof digest is invalid")
        current_time = float(now if now is not None else time.time())
        lease_duration = float(lease_seconds)
        if (
            not math.isfinite(current_time)
            or not math.isfinite(lease_duration)
            or lease_duration <= 0
        ):
            raise ValueError("effect lease reclaim timing is invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM effects WHERE effect_id = ?",
                (effect_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise WorkflowConflict(f"unknown effect: {effect_id}")
            if (
                row["status"] != "running"
                or str(row["lease_owner"] or "") != expected_owner
                or int(row["lease_epoch"] or 0) != expected_lease_epoch
            ):
                connection.rollback()
                raise WorkflowConflict(
                    f"stale effect lease reclaim for {effect_id} "
                    f"epoch={expected_lease_epoch}"
                )
            attempt = int(row["attempt"]) + 1
            max_attempts = int(row["max_attempts"])
            if attempt > max_attempts:
                payload = {
                    "effect_id": effect_id,
                    "attempt": int(row["attempt"]),
                    "lease_epoch": int(row["lease_epoch"]),
                    "retryable": False,
                    "error": "effect dead-owner reclaim budget exhausted",
                    "proof_digest": proof_digest,
                    "proof": normalized_proof,
                }
                self._append_event_locked(
                    connection,
                    run_id=str(row["run_id"]),
                    event_type="EffectFailed",
                    payload=payload,
                    causation_id=f"{causation_id}:exhausted",
                    expected_version=None,
                    schema_version=KERNEL_SCHEMA_VERSION,
                )
                connection.execute(
                    """
                    UPDATE effects
                    SET status = 'exhausted', updated_at = ?
                    WHERE effect_id = ? AND status = 'running'
                      AND lease_owner = ? AND lease_epoch = ?
                    """,
                    (
                        current_time,
                        effect_id,
                        expected_owner,
                        expected_lease_epoch,
                    ),
                )
                changed = connection.execute("SELECT changes()").fetchone()[0]
                if int(changed or 0) != 1:
                    connection.rollback()
                    raise WorkflowConflict(
                        f"effect lease reclaim exhaustion CAS failed: {effect_id}"
                    )
                connection.commit()
                raise WorkflowConflict(
                    f"effect attempt budget exhausted: {effect_id}"
                )
            epoch = int(row["lease_epoch"]) + 1
            expires = current_time + max(0.001, lease_duration)
            payload = {
                "effect_id": effect_id,
                "previous_attempt": int(row["attempt"]),
                "attempt": attempt,
                "previous_lease_epoch": expected_lease_epoch,
                "lease_epoch": epoch,
                "previous_lease_owner": expected_owner,
                "lease_owner": owner,
                "previous_lease_until": float(row["lease_until"] or 0.0),
                "lease_until": expires,
                "proof": normalized_proof,
            }
            try:
                event = self._append_event_locked(
                    connection,
                    run_id=str(row["run_id"]),
                    event_type="EffectLeaseReclaimed",
                    payload=payload,
                    causation_id=causation_id,
                    expected_version=None,
                    schema_version=KERNEL_SCHEMA_VERSION,
                )
                connection.execute(
                    """
                    UPDATE effects
                    SET status = 'running', attempt = ?, lease_epoch = ?,
                        lease_owner = ?, lease_until = ?, updated_at = ?
                    WHERE effect_id = ? AND status = 'running'
                      AND lease_owner = ? AND lease_epoch = ?
                    """,
                    (
                        attempt,
                        epoch,
                        owner,
                        expires,
                        current_time,
                        effect_id,
                        expected_owner,
                        expected_lease_epoch,
                    ),
                )
                changed = connection.execute("SELECT changes()").fetchone()[0]
                if int(changed or 0) != 1:
                    raise WorkflowConflict(
                        f"effect lease reclaim CAS failed: {effect_id}"
                    )
                connection.execute(
                    """
                    UPDATE outbox
                    SET dispatched_at = COALESCE(dispatched_at, ?)
                    WHERE effect_id = ?
                    """,
                    (current_time, effect_id),
                )
            except Exception:
                connection.rollback()
                raise
            connection.commit()
        return EffectLease(
            effect_id=effect_id,
            run_id=str(row["run_id"]),
            kind=str(row["kind"]),
            input_digest=str(row["input_digest"]),
            attempt=attempt,
            max_attempts=max_attempts,
            lease_epoch=epoch,
            lease_until=expires,
            status="running",
        )

    def fail_effect(
        self,
        effect_id: str,
        *,
        lease_epoch: int,
        error: str,
        retryable: bool,
        causation_id: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        current_time = float(now if now is not None else time.time())
        if not math.isfinite(current_time):
            raise ValueError("effect failure timing is invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM effects WHERE effect_id = ?",
                (effect_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise WorkflowConflict(f"unknown effect: {effect_id}")
            if row["status"] != "running" or int(row["lease_epoch"]) != int(lease_epoch):
                connection.rollback()
                raise InvalidCompletion(
                    f"stale effect failure for {effect_id} epoch={lease_epoch}"
                )
            exhausted = int(row["attempt"]) >= int(row["max_attempts"])
            status = "exhausted" if exhausted or not retryable else "retry"
            payload = {
                "effect_id": effect_id,
                "attempt": int(row["attempt"]),
                "lease_epoch": int(lease_epoch),
                "retryable": bool(retryable and not exhausted),
                "error": str(error)[:2000],
            }
            try:
                self._append_event_locked(
                    connection,
                    run_id=str(row["run_id"]),
                    event_type="EffectFailed",
                    payload=payload,
                    causation_id=causation_id,
                    expected_version=None,
                    schema_version=KERNEL_SCHEMA_VERSION,
                )
                connection.execute(
                    """
                    UPDATE effects
                    SET status = ?, lease_owner = NULL, lease_until = NULL,
                        last_error = ?, updated_at = ?
                    WHERE effect_id = ?
                    """,
                    (status, str(error)[:4000], current_time, effect_id),
                )
                if status == "retry":
                    connection.execute(
                        """
                        UPDATE outbox
                        SET dispatched_at = NULL, available_at = ?
                        WHERE effect_id = ?
                        """,
                        (current_time, effect_id),
                    )
            except Exception:
                connection.rollback()
                raise
            connection.commit()
        return self.effect(effect_id)

    def defer_effect(
        self,
        effect_id: str,
        *,
        lease_epoch: int,
        reason: str,
        metadata: dict[str, Any] | None = None,
        causation_id: str,
    ) -> dict[str, Any]:
        """Release a fenced lease without consuming its attempt budget.

        A provider-wide availability pause is not an execution attempt by the
        Worker.  Recording it as ``EffectFailed`` would both misclassify the
        outcome and eventually exhaust a generation while no model could run.
        Deferral therefore rolls back the claim's attempt increment, retains
        the monotonically increasing lease epoch (so late completions remain
        fenced), and removes the effect from the dispatchable outbox until an
        explicit ``resume_effect`` transition occurs.
        """
        if metadata is not None and not isinstance(metadata, dict):
            raise TypeError("effect deferral metadata must be an object")
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM effects WHERE effect_id = ?",
                (effect_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise WorkflowConflict(f"unknown effect: {effect_id}")
            if (
                row["status"] != "running"
                or int(row["lease_epoch"]) != int(lease_epoch)
            ):
                connection.rollback()
                raise InvalidCompletion(
                    f"stale effect deferral for {effect_id} epoch={lease_epoch}"
                )
            claimed_attempt = int(row["attempt"])
            restored_attempt = max(0, claimed_attempt - 1)
            payload = {
                "effect_id": effect_id,
                "claimed_attempt": claimed_attempt,
                "restored_attempt": restored_attempt,
                "lease_epoch": int(lease_epoch),
                "reason": str(reason)[:2000],
                "metadata": json.loads(canonical_json(metadata or {})),
            }
            try:
                self._append_event_locked(
                    connection,
                    run_id=str(row["run_id"]),
                    event_type="EffectDeferred",
                    payload=payload,
                    causation_id=causation_id,
                    expected_version=None,
                    schema_version=KERNEL_SCHEMA_VERSION,
                )
                connection.execute(
                    """
                    UPDATE effects
                    SET status = 'deferred', attempt = ?, lease_owner = NULL,
                        lease_until = NULL, last_error = ?, updated_at = ?
                    WHERE effect_id = ?
                    """,
                    (restored_attempt, str(reason)[:4000], now, effect_id),
                )
                connection.execute(
                    """
                    UPDATE outbox
                    SET dispatched_at = COALESCE(dispatched_at, ?)
                    WHERE effect_id = ?
                    """,
                    (now, effect_id),
                )
            except Exception:
                connection.rollback()
                raise
            connection.commit()
        return self.effect(effect_id)

    def interrupt_effect(
        self,
        effect_id: str,
        *,
        expected_owner: str,
        lease_epoch: int,
        claimed_attempt: int,
        interruption_kind: str,
        reason: str,
        metadata: dict[str, Any] | None = None,
        causation_id: str,
        now: float | None = None,
        deadline_monotonic: float | None = None,
    ) -> dict[str, Any]:
        """Fence an interrupted lease and make the same attempt reclaimable.

        Interruption is different from an execution failure and from a
        provider-availability deferral.  The former consumes an attempt; the
        latter requires a separate domain resume receipt.  A process-wide,
        operator-requested shutdown has already stopped dispatch and should be
        resumed automatically by the next process.  This transition therefore
        rolls back only the exact claim increment, retains the monotonically
        increasing lease epoch, and atomically returns the effect to ``retry``.

        Both owner and epoch are required.  A late task from the interrupted
        process cannot release a replacement owner's lease, and its eventual
        completion remains rejected by the normal epoch fence.
        """

        if (
            not isinstance(expected_owner, str)
            or not expected_owner
            or isinstance(lease_epoch, bool)
            or not isinstance(lease_epoch, int)
            or lease_epoch < 1
            or isinstance(claimed_attempt, bool)
            or not isinstance(claimed_attempt, int)
            or claimed_attempt < 1
            or not isinstance(interruption_kind, str)
            or not interruption_kind.strip()
            or not isinstance(reason, str)
            or not reason.strip()
            or not isinstance(causation_id, str)
            or not causation_id
        ):
            raise ValueError("effect interruption identity is invalid")
        if metadata is not None and not isinstance(metadata, dict):
            raise TypeError("effect interruption metadata must be an object")
        normalized_metadata = json.loads(canonical_json(metadata or {}))
        restored_attempt = claimed_attempt - 1
        payload = {
            "effect_id": effect_id,
            "claimed_attempt": claimed_attempt,
            "restored_attempt": restored_attempt,
            "lease_epoch": lease_epoch,
            "lease_owner": expected_owner,
            "interruption_kind": interruption_kind.strip(),
            "reason": reason[:2000],
            "metadata": normalized_metadata,
        }
        current_time = float(now if now is not None else time.time())
        if not math.isfinite(current_time):
            raise ValueError("effect interruption timing is invalid")

        with self._connect(deadline_monotonic=deadline_monotonic) as connection:
            self._begin_immediate(
                connection,
                deadline_monotonic=deadline_monotonic,
                operation="effect_interrupt_begin",
            )
            row = connection.execute(
                "SELECT * FROM effects WHERE effect_id = ?",
                (effect_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise WorkflowConflict(f"unknown effect: {effect_id}")

            # A retry after an ambiguous COMMIT is idempotent only for the
            # exact same causal event.  It must never mutate a later lease.
            duplicate = connection.execute(
                """
                SELECT * FROM workflow_events
                WHERE run_id = ? AND causation_id = ?
                """,
                (str(row["run_id"]), causation_id),
            ).fetchone()
            if duplicate is not None:
                if (
                    str(duplicate["event_type"]) != "EffectInterrupted"
                    or str(duplicate["payload_digest"]) != content_digest(payload)
                ):
                    connection.rollback()
                    raise WorkflowConflict(
                        f"causation id reused with different event: {causation_id}"
                    )
                current = connection.execute(
                    "SELECT * FROM effects WHERE effect_id = ?",
                    (effect_id,),
                ).fetchone()
                if (
                    current is None
                    or current["status"] != "retry"
                    or int(current["attempt"] or 0) != restored_attempt
                    or int(current["lease_epoch"] or 0) != lease_epoch
                    or current["lease_owner"] is not None
                    or current["lease_until"] is not None
                ):
                    connection.rollback()
                    raise InvalidCompletion(
                        f"stale effect interruption replay for {effect_id} "
                        f"owner={expected_owner} epoch={lease_epoch}"
                    )
                outbox = connection.execute(
                    "SELECT * FROM outbox WHERE effect_id = ?",
                    (effect_id,),
                ).fetchone()
                if (
                    outbox is None
                    or outbox["dispatched_at"] is not None
                    or float(outbox["available_at"]) > current_time
                ):
                    connection.rollback()
                    raise WorkflowConflict(
                        f"effect interruption replay outbox unavailable: {effect_id}"
                    )
                self._commit(
                    connection,
                    deadline_monotonic=deadline_monotonic,
                    operation="effect_interrupt_replay_commit",
                )
                return self._effect_from_row(current)

            if (
                row["status"] != "running"
                or str(row["lease_owner"] or "") != expected_owner
                or int(row["lease_epoch"] or 0) != lease_epoch
                or int(row["attempt"] or 0) != claimed_attempt
            ):
                connection.rollback()
                raise InvalidCompletion(
                    f"stale effect interruption for {effect_id} "
                    f"owner={expected_owner} epoch={lease_epoch}"
                )
            try:
                self._append_event_locked(
                    connection,
                    run_id=str(row["run_id"]),
                    event_type="EffectInterrupted",
                    payload=payload,
                    causation_id=causation_id,
                    expected_version=None,
                    schema_version=KERNEL_SCHEMA_VERSION,
                )
                connection.execute(
                    """
                    UPDATE effects
                    SET status = 'retry', attempt = ?, lease_owner = NULL,
                        lease_until = NULL, last_error = ?, updated_at = ?
                    WHERE effect_id = ? AND status = 'running'
                      AND lease_owner = ? AND lease_epoch = ? AND attempt = ?
                    """,
                    (
                        restored_attempt,
                        reason[:4000],
                        current_time,
                        effect_id,
                        expected_owner,
                        lease_epoch,
                        claimed_attempt,
                    ),
                )
                changed = connection.execute("SELECT changes()").fetchone()[0]
                if int(changed or 0) != 1:
                    raise InvalidCompletion(
                        f"effect interruption CAS failed: {effect_id}"
                    )
                connection.execute(
                    """
                    UPDATE outbox
                    SET dispatched_at = NULL, available_at = ?
                    WHERE effect_id = ?
                    """,
                    (current_time, effect_id),
                )
                outbox_changed = connection.execute(
                    "SELECT changes()"
                ).fetchone()[0]
                if int(outbox_changed or 0) != 1:
                    raise WorkflowConflict(
                        f"effect interruption outbox missing: {effect_id}"
                    )
                updated = connection.execute(
                    "SELECT * FROM effects WHERE effect_id = ?",
                    (effect_id,),
                ).fetchone()
            except Exception:
                connection.rollback()
                raise
            self._commit(
                connection,
                deadline_monotonic=deadline_monotonic,
                operation="effect_interrupt_commit",
            )
        return self._effect_from_row(updated)

    def resume_effect(
        self,
        effect_id: str,
        *,
        causation_id: str,
    ) -> dict[str, Any]:
        """Make an explicitly deferred effect dispatchable again."""
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM effects WHERE effect_id = ?",
                (effect_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise WorkflowConflict(f"unknown effect: {effect_id}")
            if row["status"] != "deferred":
                connection.rollback()
                raise WorkflowConflict(
                    f"effect {effect_id} cannot resume from {row['status']}"
                )
            payload = {
                "effect_id": effect_id,
                "attempt": int(row["attempt"]),
                "lease_epoch": int(row["lease_epoch"]),
            }
            try:
                self._append_event_locked(
                    connection,
                    run_id=str(row["run_id"]),
                    event_type="EffectResumed",
                    payload=payload,
                    causation_id=causation_id,
                    expected_version=None,
                    schema_version=KERNEL_SCHEMA_VERSION,
                )
                connection.execute(
                    """
                    UPDATE effects
                    SET status = 'retry', lease_owner = NULL,
                        lease_until = NULL, last_error = NULL, updated_at = ?
                    WHERE effect_id = ?
                    """,
                    (now, effect_id),
                )
                connection.execute(
                    """
                    UPDATE outbox
                    SET dispatched_at = NULL, available_at = ?
                    WHERE effect_id = ?
                    """,
                    (now, effect_id),
                )
            except Exception:
                connection.rollback()
                raise
            connection.commit()
        return self.effect(effect_id)

