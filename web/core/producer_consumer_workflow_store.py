"""Inert durable adapter for producer/consumer ``JobEnvelope`` values.

This module is intentionally not imported by the production orchestrator,
HTTP server, rating daemon, certification path, or runtime launcher.  It owns
no SQLite schema or scheduler state: every durable transition delegates to the
existing :class:`workflow_kernel.WorkflowStore` event/effect/outbox/inbox
transactions.

The adapter is the Slice-2 crash/recovery foundation, not an activation switch.
It proves that one immutable envelope can be submitted idempotently, leased,
heartbeated, cancelled, recovered after restart, and completed with a fenced
receipt without establishing a second state machine.
"""

from __future__ import annotations

from copy import deepcopy
import math
import time
from typing import Any, Callable, Mapping

from bot_artifact import canonical_digest
from pipeline_job_contract import (
    JobContractError,
    assert_idempotent_job_replay,
    job_envelope_issues,
    job_receipt_issues,
)
from workflow_kernel import (
    EffectLease,
    InvalidCompletion,
    WorkflowConflict,
    WorkflowStore,
)


ADAPTER_DEFINITION_VERSION = 1
EFFECT_INPUT_SCHEMA = "producer-consumer-job-effect-input-v1"
EFFECT_KIND_PREFIX = "producer-consumer-job:"
_EFFECT_INPUT_FIELDS = frozenset({"schema", "envelope_digest", "envelope"})


class ProducerConsumerStoreError(RuntimeError):
    """The durable job boundary could not be proven without guessing."""

    def __init__(self, issues: list[str] | tuple[str, ...] | str):
        values = [issues] if isinstance(issues, str) else list(issues)
        self.issues = tuple(str(value) for value in values)
        super().__init__(";".join(self.issues))


def effect_id_for_envelope(envelope: Mapping[str, Any]) -> str:
    """Return the durable identity for one run-scoped logical job id."""

    issues = job_envelope_issues(envelope)
    if issues:
        raise ProducerConsumerStoreError(issues)
    return "producer-consumer-job:" + canonical_digest({
        "run_id": envelope["run_id"],
        "job_id": envelope["job_id"],
    })


def _effect_input(envelope: Mapping[str, Any]) -> dict[str, Any]:
    frozen = deepcopy(dict(envelope))
    return {
        "schema": EFFECT_INPUT_SCHEMA,
        "envelope_digest": frozen["envelope_digest"],
        "envelope": frozen,
    }


def _lease_projection(lease: EffectLease, envelope: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "effect_id": lease.effect_id,
        "run_id": lease.run_id,
        "job_id": envelope["job_id"],
        "draft_id": envelope["draft_id"],
        "candidate_id": envelope["candidate_id"],
        "envelope_digest": envelope["envelope_digest"],
        "attempt": lease.attempt,
        "max_attempts": lease.max_attempts,
        "lease_epoch": lease.lease_epoch,
        "lease_until": lease.lease_until,
        "status": lease.status,
    }


def _bounded_lease_seconds(
    envelope: Mapping[str, Any],
    *,
    now: float,
    requested: float,
) -> float:
    try:
        current = float(now)
        duration = float(requested)
        not_before = float(envelope["deadline"]["not_before_epoch"])
        expires = float(envelope["deadline"]["expires_at_epoch"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ProducerConsumerStoreError(
            "producer_consumer_lease_timing_invalid"
        ) from exc
    if (
        not all(math.isfinite(value) for value in (
            current, duration, not_before, expires
        ))
        or duration <= 0
        or current < not_before
        or current >= expires
    ):
        raise ProducerConsumerStoreError(
            "producer_consumer_lease_outside_envelope_deadline"
        )
    return min(duration, expires - current)


class ProducerConsumerWorkflowAdapter:
    """Typed facade over one existing ``WorkflowStore`` instance."""

    def __init__(self, store: WorkflowStore):
        if not isinstance(store, WorkflowStore):
            raise TypeError("producer/consumer adapter requires WorkflowStore")
        self.store = store

    def _validated_effect(self, effect_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        effect = self.store.effect(effect_id)
        if not effect:
            raise ProducerConsumerStoreError("producer_consumer_effect_missing")
        payload = effect.get("input_payload")
        if not isinstance(payload, dict) or set(payload) != set(_EFFECT_INPUT_FIELDS):
            raise ProducerConsumerStoreError("producer_consumer_effect_input_fields_mismatch")
        envelope = payload.get("envelope")
        issues = job_envelope_issues(envelope)
        if issues:
            raise ProducerConsumerStoreError([
                *(f"envelope:{issue}" for issue in issues),
            ])
        assert isinstance(envelope, dict)
        if (
            payload.get("schema") != EFFECT_INPUT_SCHEMA
            or payload.get("envelope_digest") != envelope["envelope_digest"]
            or effect.get("run_id") != envelope["run_id"]
            or effect.get("kind") != EFFECT_KIND_PREFIX + envelope["job_kind"]
            or effect_id != effect_id_for_envelope(envelope)
        ):
            raise ProducerConsumerStoreError(
                "producer_consumer_effect_envelope_binding_mismatch"
            )
        return effect, deepcopy(envelope)

    def submit(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        """Durably request one exact envelope through the kernel outbox."""

        issues = job_envelope_issues(envelope)
        if issues:
            raise ProducerConsumerStoreError(issues)
        frozen = deepcopy(dict(envelope))
        effect_id = effect_id_for_envelope(frozen)
        self.store.ensure_instance(
            frozen["run_id"],
            definition_version=ADAPTER_DEFINITION_VERSION,
        )

        # The kernel's run-scoped command lock makes the two uniqueness axes
        # atomic: neither a logical job id nor an idempotency key may name two
        # envelopes, including under concurrent submitters.  The effect id is
        # derived from job_id, while this scan fences key reuse.
        with self.store.command_lock(frozen["run_id"], blocking=True):
            for observed in self.store.effects_for_run(frozen["run_id"]):
                if not str(observed.get("kind") or "").startswith(
                    EFFECT_KIND_PREFIX
                ):
                    continue
                payload = observed.get("input_payload")
                prior = (
                    payload.get("envelope") if isinstance(payload, dict) else None
                )
                if not isinstance(prior, dict):
                    raise ProducerConsumerStoreError(
                        "producer_consumer_existing_effect_unverifiable"
                    )
                same_job = prior.get("job_id") == frozen["job_id"]
                same_key = (
                    prior.get("idempotency_key") == frozen["idempotency_key"]
                )
                if not same_job and not same_key:
                    continue
                try:
                    exact = assert_idempotent_job_replay(prior, frozen)
                except (JobContractError, TypeError) as exc:
                    raise ProducerConsumerStoreError(
                        "producer_consumer_idempotency_conflict"
                    ) from exc
                if not exact or not same_job or not same_key:
                    raise ProducerConsumerStoreError(
                        "producer_consumer_idempotency_conflict"
                    )
            try:
                effect = self.store.request_effect(
                    run_id=frozen["run_id"],
                    effect_id=effect_id,
                    kind=EFFECT_KIND_PREFIX + frozen["job_kind"],
                    input_payload=_effect_input(frozen),
                    causation_id=(
                        "producer-consumer-submit:" + frozen["idempotency_key"]
                    ),
                    max_attempts=frozen["retry_policy"]["max_attempts"],
                    available_at=frozen["deadline"]["not_before_epoch"],
                )
            except WorkflowConflict as exc:
                raise ProducerConsumerStoreError(
                    "producer_consumer_submit_conflict"
                ) from exc
        _, validated = self._validated_effect(effect_id)
        return {
            "effect_id": effect_id,
            "status": effect["status"],
            "envelope": validated,
        }

    def load(self, effect_id: str) -> dict[str, Any]:
        effect, envelope = self._validated_effect(effect_id)
        return {"effect": deepcopy(effect), "envelope": envelope}

    def claim(
        self,
        effect_id: str,
        *,
        owner: str,
        lease_seconds: float,
        now: float,
    ) -> dict[str, Any]:
        _, envelope = self._validated_effect(effect_id)
        bounded_seconds = _bounded_lease_seconds(
            envelope,
            now=now,
            requested=lease_seconds,
        )
        lease = self.store.claim_effect(
            effect_id,
            owner=owner,
            lease_seconds=bounded_seconds,
            now=now,
        )
        return _lease_projection(lease, envelope)

    def heartbeat(
        self,
        effect_id: str,
        *,
        owner: str,
        lease_epoch: int,
        lease_seconds: float,
        heartbeat_id: str,
        now: float,
    ) -> dict[str, Any]:
        _, envelope = self._validated_effect(effect_id)
        bounded_seconds = _bounded_lease_seconds(
            envelope,
            now=now,
            requested=lease_seconds,
        )
        lease = self.store.renew_effect_lease(
            effect_id,
            owner=owner,
            lease_epoch=lease_epoch,
            lease_seconds=bounded_seconds,
            causation_id="producer-consumer-heartbeat:" + heartbeat_id,
            now=now,
        )
        return _lease_projection(lease, envelope)

    def cancel(
        self,
        effect_id: str,
        *,
        expected_status: str,
        expected_attempt: int,
        expected_lease_epoch: int,
        expected_owner: str | None,
        reason: str,
        cancel_id: str,
        now: float,
    ) -> dict[str, Any]:
        self._validated_effect(effect_id)
        return self.store.cancel_effect(
            effect_id,
            expected_status=expected_status,
            expected_attempt=expected_attempt,
            expected_lease_epoch=expected_lease_epoch,
            expected_owner=expected_owner,
            reason=reason,
            causation_id="producer-consumer-cancel:" + cancel_id,
            now=now,
        )

    def record_infrastructure_failure(
        self,
        effect_id: str,
        *,
        lease_owner: str,
        lease_epoch: int,
        error: str,
        failure_id: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        effect, _ = self._validated_effect(effect_id)
        current_time = float(now if now is not None else time.time())
        if (
            effect.get("status") != "running"
            or effect.get("lease_owner") != lease_owner
            or effect.get("lease_epoch") != lease_epoch
            or not math.isfinite(current_time)
            or not isinstance(effect.get("lease_until"), (int, float))
            or isinstance(effect.get("lease_until"), bool)
            or current_time >= effect["lease_until"]
        ):
            raise InvalidCompletion(
                f"stale producer/consumer infrastructure failure: {effect_id}"
            )
        return self.store.fail_effect(
            effect_id,
            lease_epoch=lease_epoch,
            error=error,
            retryable=True,
            causation_id="producer-consumer-failure:" + failure_id,
            now=current_time,
        )

    def complete(
        self,
        effect_id: str,
        *,
        receipt: Mapping[str, Any],
        completion_id: str,
        now: float,
    ) -> dict[str, Any]:
        """Atomically accept one exact live-lease receipt and domain event."""

        effect, envelope = self._validated_effect(effect_id)
        issues = job_receipt_issues(receipt, envelope=envelope)
        if receipt.get("attempt") != effect.get("attempt"):
            issues.append("producer_consumer_receipt_attempt_stale")
        if receipt.get("lease_epoch") != effect.get("lease_epoch"):
            issues.append("producer_consumer_receipt_lease_epoch_stale")
        if receipt.get("lease_owner") != effect.get("lease_owner"):
            issues.append("producer_consumer_receipt_lease_owner_stale")
        lease_until = effect.get("lease_until")
        finished_at = receipt.get("finished_at_epoch")
        if (
            effect.get("status") != "running"
            or not isinstance(lease_until, (int, float))
            or isinstance(lease_until, bool)
            or not isinstance(finished_at, (int, float))
            or isinstance(finished_at, bool)
            or finished_at >= lease_until
            or now >= lease_until
        ):
            issues.append("producer_consumer_receipt_live_lease_missing")
        if receipt.get("outcome") == "infrastructure_failure":
            issues.append("producer_consumer_infrastructure_receipt_must_retry")
        if issues:
            raise ProducerConsumerStoreError(list(dict.fromkeys(issues)))

        frozen_receipt = deepcopy(dict(receipt))
        accepted = self.store.complete_effect(
            effect_id,
            lease_epoch=int(effect["lease_epoch"]),
            completion_id="producer-consumer-completion:" + completion_id,
            result_payload={
                "schema": "producer-consumer-job-result-v1",
                "envelope_digest": envelope["envelope_digest"],
                "receipt_digest": frozen_receipt["receipt_digest"],
                "receipt": frozen_receipt,
            },
            causation_id="producer-consumer-effect-completed:" + completion_id,
            followup_events=[{
                "event_type": "ProducerConsumerJobReceiptAccepted",
                "payload": {
                    "job_id": envelope["job_id"],
                    "draft_id": envelope["draft_id"],
                    "candidate_id": envelope["candidate_id"],
                    "envelope_digest": envelope["envelope_digest"],
                    "receipt_digest": frozen_receipt["receipt_digest"],
                    "outcome": frozen_receipt["outcome"],
                },
                "causation_id": "producer-consumer-receipt:" + completion_id,
            }],
            require_live_lease=True,
            now=now,
        )
        if not accepted.get("accepted"):
            raise ProducerConsumerStoreError(
                "producer_consumer_receipt_rejected_by_kernel_lease"
            )
        return accepted

    def recover(
        self,
        *,
        owner: str,
        lease_seconds: float,
        now: float,
        recovery_id: str,
        death_proof_resolver: (
            Callable[[Mapping[str, Any]], Mapping[str, Any]] | None
        ) = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Claim queued work and reclaim expired work after proven owner death."""

        pending = self.store.pending_outbox(now=now)
        prepared: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
        for row in pending:
            effect, _ = self._validated_effect(str(row["effect_id"]))
            proof = None
            if effect.get("status") == "running":
                if (
                    not isinstance(effect.get("lease_until"), (int, float))
                    or effect["lease_until"] > now
                ):
                    raise ProducerConsumerStoreError(
                        "producer_consumer_recovery_nonexpired_running_effect"
                    )
                if death_proof_resolver is None:
                    raise ProducerConsumerStoreError(
                        "producer_consumer_recovery_death_proof_required"
                    )
                try:
                    resolved = death_proof_resolver(deepcopy(effect))
                except Exception as exc:
                    raise ProducerConsumerStoreError(
                        "producer_consumer_recovery_death_proof_failed"
                    ) from exc
                if not isinstance(resolved, Mapping):
                    raise ProducerConsumerStoreError(
                        "producer_consumer_recovery_death_proof_invalid"
                    )
                proof = deepcopy(dict(resolved))
            prepared.append((effect, proof))

        leases: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        for index, (effect, proof) in enumerate(prepared):
            effect_id = str(effect["effect_id"])
            _, envelope = self._validated_effect(effect_id)
            bounded_seconds = _bounded_lease_seconds(
                envelope,
                now=now,
                requested=lease_seconds,
            )
            try:
                if effect["status"] == "running":
                    assert proof is not None
                    lease = self.store.reclaim_effect_lease(
                        effect_id,
                        expected_owner=str(effect["lease_owner"]),
                        expected_lease_epoch=int(effect["lease_epoch"]),
                        owner=owner,
                        lease_seconds=bounded_seconds,
                        causation_id=(
                            f"producer-consumer-recovery:{recovery_id}:{index}"
                        ),
                        proof=proof,
                        now=now,
                    )
                else:
                    lease = self.store.claim_effect(
                        effect_id,
                        owner=owner,
                        lease_seconds=bounded_seconds,
                        now=now,
                    )
            except (WorkflowConflict, InvalidCompletion):
                conflicts.append({
                    "effect_id": effect_id,
                    "issue": "producer_consumer_recovery_concurrent_conflict",
                })
                continue
            leases.append(_lease_projection(lease, envelope))
        return {"leases": leases, "conflicts": conflicts}
