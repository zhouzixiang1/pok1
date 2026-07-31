"""Slice 2b one-ahead-buffer wiring over the inert producer/consumer shadow.

This module is the *minimum viable* Slice 2b activation layer described in
section 13 step 2 of ``docs/evolution-producer-consumer-pipeline-v1.md``:

* ``seal_candidate`` snapshots the Producer's finished candidate at
  ``workers_done`` into an immutable :class:`~pipeline_job_contract.JobEnvelope`
  and submits it to the Consumer queue through the existing inert
  :class:`~producer_consumer_workflow_store.ProducerConsumerWorkflowAdapter`.
* :class:`ConsumerDispatcher` dequeues one sealed envelope, claims a fenced
  lease, runs the *unchanged* canonical gate chain against the sealed artifact
  and records the outcome to a consumer-owned validation ledger.
* :class:`OneAheadCoordinator` is the Producer/Consumer rendezvous: the Producer
  lane may begin the next ``prepare_generation`` as soon as the current
  candidate is sealed, while the synchronous promotion barrier
  (``wait_for_promotion_readiness``) blocks publication until the Consumer has
  finished certifying the sealed artifact.

Activation contract (mirrors the design doc):

- This module remains **dormant by default**.  Nothing in the production
  ``orchestrator_loop`` imports or invokes it unless the explicit
  ``pipeline_slice2b_enabled`` flag is set truthy on the orchestrator context.
  The canonical runtime stays on the legacy single-slot path until the shadow
  projection and crash tests are green and a separate activation commit +
  migration receipt land.
- The gate chain is NOT reimplemented here.  ``ConsumerDispatcher`` accepts a
  caller-supplied ``gate_runner`` coroutine that invokes the existing
  ``run_quality_gates`` / ``run_review`` / ``run_critic`` / ``run_precommit_eval``
  / ``commit_bot`` callables against the sealed candidate.  The dispatcher only
  owns lease discipline and the validation ledger.
- The Producer's next-version reservation is *not* advanced by sealing.  The
  allocation floor advances only on Consumer promotion (publication), preserving
  the design-doc invariant: "the canonical checkpoint continues to own only the
  one candidate currently inside the version-allocation/publication critical
  section."  The one-ahead draft therefore shares the still-unpromised ``next_v``
  namespace slot and is fenced the moment the in-flight Consumer promotes or
  rejects.
- The checkpoint CAS, publication authority, byte-pinned
  ``national_native_templates_bot.py``, probe identity and all certification
  remain owned by the unchanged canonical gate chain.  This layer never writes
  to ``pipeline_state.json`` or the producer's checkpoint; it only reads the
  fields needed to bind the envelope.

Resource broker, retry-at clock and backpressure are intentionally left as
TODOs (Section 11 of the design doc): they are throughput optimizations, not
required for the first one-ahead win, and landing them needs their own
reviewed production commit.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import time
from collections import deque
from copy import deepcopy
from pathlib import Path
from typing import Any, Awaitable, Callable, Deque, Mapping, MutableMapping

from pipeline_job_contract import build_job_envelope
from producer_consumer_workflow_store import (
    ProducerConsumerStoreError,
    ProducerConsumerWorkflowAdapter,
    effect_id_for_envelope,
)


SLICE2B_LEDGER_SCHEMA_VERSION = 1
SLICE2B_LEDGER_KIND = "producer-consumer-slice2b-validation-ledger-v1"
SLICE2B_SEALED_KIND = "producer-consumer-slice2b-sealed-candidate-v1"

# The Consumer lane runs the canonical LLM gate chain through precommit only.
# ``commit_bot`` publication remains on the primary orchestrator path behind
# the promotion barrier so publication authority is not double-invoked.
CONSUMER_GATE_CHAIN_ORDER = (
    "run_quality_gates",
    "run_review",
    "run_critic",
    "run_precommit_eval",
)
# Full publication chain (consumer precommit + primary commit_bot).
GATE_CHAIN_ORDER = CONSUMER_GATE_CHAIN_ORDER + ("commit_bot",)

_VALID_SUBSTATE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class Slice2bError(RuntimeError):
    """The one-ahead buffer could not be proven without guessing."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _content_digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,191}$", value
    ):
        raise Slice2bError(f"{label} is invalid")
    return value


def _require_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        raise Slice2bError(f"{label} is not a sha256 digest")
    return value


def build_sealed_candidate_snapshot(
    *,
    candidate_id: str,
    draft_id: str,
    artifact_hash: str,
    manifest_digest: str,
    charter_digest: str,
    epoch_binding: Mapping[str, Any],
    next_v: int,
    source_v: int,
    workflow_run_id: str,
    quality_native_match_timing_plan: Mapping[str, Any] | None,
    producer_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the immutable per-candidate snapshot bound into the seal envelope.

    ``epoch_binding`` is the opaque checkpoint sub-state needed for validation
    (target identity, evaluation epoch, lease digest, generation ordinal).  The
    reducer-side :mod:`producer_consumer_pipeline` already validates the full
    target identity; this snapshot only freezes the Producer-observable fields
    so the Consumer can re-derive the same :class:`JobEnvelope` on retry.
    """

    snapshot = {
        "schema": SLICE2B_SEALED_KIND,
        "schema_version": SLICE2B_LEDGER_SCHEMA_VERSION,
        "candidate_id": _require_safe_id(candidate_id, "candidate_id"),
        "draft_id": _require_safe_id(draft_id, "draft_id"),
        "artifact_hash": _require_digest(artifact_hash, "artifact_hash"),
        "manifest_digest": _require_digest(manifest_digest, "manifest_digest"),
        "charter_digest": _require_digest(charter_digest, "charter_digest"),
        "epoch_binding": deepcopy(dict(epoch_binding)),
        "next_v": int(next_v),
        "source_v": int(source_v),
        "workflow_run_id": _require_safe_id(workflow_run_id, "workflow_run_id"),
        "quality_native_match_timing_plan": (
            deepcopy(dict(quality_native_match_timing_plan))
            if quality_native_match_timing_plan is not None
            else None
        ),
        "producer_receipt": (
            deepcopy(dict(producer_receipt))
            if producer_receipt is not None
            else None
        ),
    }
    if snapshot["next_v"] < 1 or snapshot["source_v"] < 0:
        raise Slice2bError("version allocation is invalid")
    snapshot["snapshot_digest"] = _content_digest(snapshot)
    return snapshot


def build_slice2b_quality_envelope(
    *,
    snapshot: Mapping[str, Any],
    run_id: str,
    job_id: str,
    idempotency_key: str,
    resource_claim: Mapping[str, Any],
    retry_policy: Mapping[str, Any],
    deadline: Mapping[str, Any],
    artifact_digest: str,
    evaluation_contract_digest: str,
    executor_digest: str,
    repository_digest: str,
    runtime_digest: str,
) -> dict[str, Any]:
    """Build the ``quality-static`` envelope that opens the Consumer lane.

    The envelope is the scheduling metadata only; the unchanged canonical gate
    chain remains the authority for every byte/identity/CAS check.  We bind the
    minimum closed set of input refs the ``quality-static`` policy requires.
    """

    candidate_id = _require_safe_id(snapshot["candidate_id"], "snapshot candidate_id")
    input_refs = [
        {"kind": "candidate", "subject": candidate_id, "digest": artifact_digest},
        {"kind": "charter", "subject": "charter", "digest": snapshot["charter_digest"]},
        {
            "kind": "contract",
            "subject": "national-evaluation-contract",
            "digest": evaluation_contract_digest,
        },
        {"kind": "executor", "subject": "quality-consumer", "digest": executor_digest},
        {"kind": "repository", "subject": "origin-main", "digest": repository_digest},
        {"kind": "runtime", "subject": "national-runtime", "digest": runtime_digest},
    ]
    return build_job_envelope(
        job_id=job_id,
        run_id=run_id,
        draft_id=snapshot["draft_id"],
        candidate_id=candidate_id,
        job_kind="quality-static",
        charter_digest=snapshot["charter_digest"],
        artifact_digest=artifact_digest,
        input_refs=input_refs,
        dependency_receipt_digests=[],
        idempotency_key=idempotency_key,
        resource_claim=dict(resource_claim),
        priority_class="compliance",
        retry_policy=dict(retry_policy),
        deadline=dict(deadline),
    )


# ---------------------------------------------------------------------------
# Candidate lifecycle (Consumer-owned, PERSISTED; survives process restart)
# ---------------------------------------------------------------------------
#
# Replaces the original in-memory ``ValidationLedger``.  The per-candidate
# lifecycle is a small explicit state machine persisted to a dedicated sqlite
# table so one-ahead survives an orchestrator/service restart:
#
#   [none] --start()--> [SEALED] --record_gate()*--> [SEALED]
#                                   |--promote()--> [PROMOTED] (terminal)
#                                   `--reject()---> [REJECTED] (terminal)
#
# The ``SEALED`` state spans both "envelope submitted, consumer not yet leased"
# and "consumer is running the gate chain" -- the durable envelope outbox +
# fenced lease already own lease discipline, so the lifecycle only needs to
# distinguish sealed-and-unresolved from terminal.  ``non_terminal_candidates``
# is the boot-recovery entry point: after a crash it lists every candidate
# whose seal is durable but whose consumer never reached a terminal outcome,
# so the activation can relaunch the consumer task for them.


# Lifecycle states.  ``None``/missing row == not yet sealed.
_CANDIDATE_SEALED = "sealed"  # sealed, validation in progress (or not yet leased)
_CANDIDATE_PROMOTED = "promoted"  # consumer gates passed; commit_bot may publish
_CANDIDATE_REJECTED = "rejected"  # a gate failed; generation must abandon

# Public mirror of the in-memory ``validation_outcome`` values, for callers
# (coordinator / activation) that read ``snapshot()["validation_outcome"]``.
_VALIDATION_OUTCOME_BY_STATE = {
    _CANDIDATE_SEALED: "running",
    _CANDIDATE_PROMOTED: "promoted",
    _CANDIDATE_REJECTED: "rejected",
}

# Allowed transitions into a new state from the current persisted state.
# ``None`` current = row does not exist yet (only ``start`` is legal).
_ALLOWED_STATE_TRANSITIONS = {
    (None, _CANDIDATE_SEALED),
    (_CANDIDATE_SEALED, _CANDIDATE_PROMOTED),
    (_CANDIDATE_SEALED, _CANDIDATE_REJECTED),
}

_LIFECYCLE_SCHEMA_VERSION = 1


def _empty_lifecycle_row(candidate_id: str) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "sealed_artifact_hash": None,
        "envelope_effect_id": None,
        "envelope_digest": None,
        "validation_outcome": None,  # None | "running" | "promoted" | "rejected"
        "terminal_reason": None,
        "gate_results": {},  # gate_name -> {outcome, digest, finished_at}
        "promotion_receipt": None,
        "completed_at": None,
        "sealed_snapshot": None,  # the immutable snapshot the consumer needs
    }


class CandidateLifecycle:
    """Per-candidate validation lifecycle, persisted to sqlite.

    Drop-in replacement for the former in-memory :class:`ValidationLedger`:
    the public surface (``start``, ``record_gate``, ``promote``, ``reject``,
    ``snapshot``, ``is_terminal``, ``is_promoted``) is preserved so the
    :class:`ConsumerDispatcher` and :class:`OneAheadCoordinator` are unchanged.
    Persistence is added so a process restart does not lose in-flight
    candidates: ``non_terminal_candidates`` lets the activation relaunch the
    consumer task for every sealed-but-unresolved candidate.

    The lifecycle owns its own sqlite database (``slice2b_lifecycle.sqlite3``)
    keyed by ``candidate_id``; it deliberately does NOT piggyback on the
    worker-journal event stream (different key, simpler invariants).  All
    mutations go through a single atomic ``_transition`` that enforces the
    transition whitelist under ``BEGIN IMMEDIATE``.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        # Default to a process-private temp db so the legacy no-arg call
        # ``ValidationLedger()`` (used widely in unit tests) keeps working.
        # Production (the activation layer) passes the real lifecycle path.
        if db_path is None:
            import tempfile
            db_path = Path(tempfile.gettempdir()) / (
                f"slice2b_lifecycle_{os.getpid()}_{id(self)}.sqlite3"
            )
        self._db_path = str(db_path)
        self._ensure_schema()

    # -- schema -------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS slice2b_candidate_lifecycle(
                    candidate_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    sealed_artifact_hash TEXT,
                    envelope_effect_id TEXT,
                    envelope_digest TEXT,
                    gate_results_json TEXT NOT NULL DEFAULT '{}',
                    promotion_receipt_json TEXT,
                    terminal_reason TEXT,
                    completed_at REAL,
                    sealed_snapshot_json TEXT,
                    updated_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS slice2b_lifecycle_meta(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            row = connection.execute(
                "SELECT value FROM slice2b_lifecycle_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO slice2b_lifecycle_meta(key, value) VALUES (?, ?)",
                    ("schema_version", str(_LIFECYCLE_SCHEMA_VERSION)),
                )
            connection.commit()

    # -- single atomic transition ------------------------------------------

    def _transition(
        self,
        candidate_id: str,
        *,
        to_state: str,
        mutator: Callable[[dict[str, Any]], None] | None = None,
        require_snapshot: bool = False,
    ) -> dict[str, Any]:
        """Atomically move ``candidate_id`` to ``to_state`` if allowed.

        Reads the current persisted row, enforces
        ``(current_state, to_state) in _ALLOWED_STATE_TRANSITIONS``, applies
        ``mutator`` to the in-memory row copy, and writes it back under
        ``BEGIN IMMEDIATE``.  Returns the post-transition snapshot.  Raises
        :class:`Slice2bError` on an illegal transition.
        """
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM slice2b_candidate_lifecycle WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            current_state = str(row["state"]) if row is not None else None
            if (current_state, to_state) not in _ALLOWED_STATE_TRANSITIONS:
                connection.rollback()
                raise Slice2bError(
                    f"candidate_lifecycle_illegal_transition:"
                    f"{current_state}->{to_state}"
                )
            entry = self._row_to_entry(row) if row is not None else _empty_lifecycle_row(candidate_id)
            if require_snapshot and not entry.get("sealed_snapshot"):
                connection.rollback()
                raise Slice2bError(
                    "candidate_lifecycle_snapshot_missing:" + candidate_id
                )
            if mutator is not None:
                mutator(entry)
            # Persist.  UPSERT so the SEALED insert and the terminal UPDATE
            # share one code path.
            connection.execute(
                """
                INSERT INTO slice2b_candidate_lifecycle(
                    candidate_id, state, sealed_artifact_hash,
                    envelope_effect_id, envelope_digest, gate_results_json,
                    promotion_receipt_json, terminal_reason, completed_at,
                    sealed_snapshot_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                    state=excluded.state,
                    sealed_artifact_hash=excluded.sealed_artifact_hash,
                    envelope_effect_id=excluded.envelope_effect_id,
                    envelope_digest=excluded.envelope_digest,
                    gate_results_json=excluded.gate_results_json,
                    promotion_receipt_json=excluded.promotion_receipt_json,
                    terminal_reason=excluded.terminal_reason,
                    completed_at=excluded.completed_at,
                    sealed_snapshot_json=excluded.sealed_snapshot_json,
                    updated_at=excluded.updated_at
                """,
                (
                    candidate_id,
                    to_state,
                    entry.get("sealed_artifact_hash"),
                    entry.get("envelope_effect_id"),
                    entry.get("envelope_digest"),
                    _canonical_json(entry.get("gate_results") or {}),
                    (
                        _canonical_json(entry["promotion_receipt"])
                        if entry.get("promotion_receipt") is not None
                        else None
                    ),
                    entry.get("terminal_reason"),
                    entry.get("completed_at"),
                    (
                        _canonical_json(entry["sealed_snapshot"])
                        if entry.get("sealed_snapshot") is not None
                        else None
                    ),
                    now,
                ),
            )
            connection.commit()
            entry["validation_outcome"] = _VALIDATION_OUTCOME_BY_STATE.get(to_state)
            return deepcopy(entry)

    @staticmethod
    def _row_to_entry(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {}  # type: ignore[return-value]
        entry = {
            "candidate_id": str(row["candidate_id"]),
            "sealed_artifact_hash": row["sealed_artifact_hash"],
            "envelope_effect_id": row["envelope_effect_id"],
            "envelope_digest": row["envelope_digest"],
            "validation_outcome": _VALIDATION_OUTCOME_BY_STATE.get(
                str(row["state"])
            ),
            "terminal_reason": row["terminal_reason"],
            "gate_results": json.loads(row["gate_results_json"] or "{}"),
            "promotion_receipt": (
                json.loads(row["promotion_receipt_json"])
                if row["promotion_receipt_json"]
                else None
            ),
            "completed_at": row["completed_at"],
            "sealed_snapshot": (
                json.loads(row["sealed_snapshot_json"])
                if row["sealed_snapshot_json"]
                else None
            ),
        }
        return entry

    # -- public API (mirrors the former ValidationLedger) -------------------

    def start(
        self,
        *,
        candidate_id: str,
        sealed_artifact_hash: str,
        envelope_effect_id: str,
        envelope_digest: str,
        sealed_snapshot: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record that the Producer sealed ``candidate_id``.

        Idempotent: replaying the same seal (same artifact hash) returns the
        existing row; a different artifact hash for the same candidate_id is
        an unrecoverable drift.  ``sealed_snapshot`` is persisted once (at the
        first start) so the consumer can recover it after a crash without the
        Producer still being around.
        """
        _require_safe_id(candidate_id, "candidate_id")
        artifact = _require_digest(sealed_artifact_hash, "sealed_artifact_hash")

        existing = self.snapshot(candidate_id)
        if existing is not None:
            if existing["sealed_artifact_hash"] != artifact:
                raise Slice2bError("validation_ledger_candidate_artifact_drift")
            return deepcopy(existing)

        def _seed(entry: dict[str, Any]) -> None:
            entry["sealed_artifact_hash"] = artifact
            entry["envelope_effect_id"] = envelope_effect_id
            entry["envelope_digest"] = envelope_digest
            if sealed_snapshot is not None and not entry.get("sealed_snapshot"):
                entry["sealed_snapshot"] = deepcopy(dict(sealed_snapshot))

        return self._transition(
            candidate_id, to_state=_CANDIDATE_SEALED, mutator=_seed
        )

    def record_gate(
        self,
        *,
        candidate_id: str,
        gate_name: str,
        outcome: str,
        result_digest: str,
        finished_at: float,
        detail: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if gate_name not in GATE_CHAIN_ORDER:
            raise Slice2bError(f"validation_ledger_unknown_gate:{gate_name}")
        if outcome not in {"success", "candidate_failure", "infrastructure_failure"}:
            raise Slice2bError(f"validation_ledger_unknown_outcome:{outcome}")

        def _record(entry: dict[str, Any]) -> None:
            entry["gate_results"][gate_name] = {
                "outcome": outcome,
                "digest": _require_digest(result_digest, "gate result digest"),
                "finished_at": float(finished_at),
                "detail": deepcopy(dict(detail or {})),
            }

        # Self-transition SEALED -> SEALED to persist the gate result.
        return self._transition_with_self_state(
            candidate_id, mutator=_record
        )

    def promote(
        self,
        *,
        candidate_id: str,
        promotion_receipt: Mapping[str, Any],
        completed_at: float,
    ) -> dict[str, Any]:
        receipt = deepcopy(dict(promotion_receipt))
        if not receipt:
            raise Slice2bError("validation_ledger_promotion_receipt_missing")

        def _promote(entry: dict[str, Any]) -> None:
            entry["promotion_receipt"] = receipt
            entry["completed_at"] = float(completed_at)
            entry["terminal_reason"] = "promoted_by_consumer"

        return self._transition(
            candidate_id, to_state=_CANDIDATE_PROMOTED, mutator=_promote
        )

    def reject(
        self,
        *,
        candidate_id: str,
        reason: str,
        completed_at: float,
    ) -> dict[str, Any]:
        if not isinstance(reason, str) or not reason.strip():
            raise Slice2bError("validation_ledger_reject_reason_missing")
        clean_reason = reason.strip()

        def _reject(entry: dict[str, Any]) -> None:
            entry["terminal_reason"] = clean_reason
            entry["completed_at"] = float(completed_at)

        return self._transition(
            candidate_id, to_state=_CANDIDATE_REJECTED, mutator=_reject
        )

    def snapshot(self, candidate_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM slice2b_candidate_lifecycle WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        return deepcopy(self._row_to_entry(row)) if row is not None else None

    def is_terminal(self, candidate_id: str) -> bool:
        entry = self.snapshot(candidate_id)
        return entry is not None and entry["validation_outcome"] in {
            "promoted",
            "rejected",
        }

    def is_promoted(self, candidate_id: str) -> bool:
        entry = self.snapshot(candidate_id)
        return entry is not None and entry["validation_outcome"] == "promoted"

    # -- boot recovery (new) ------------------------------------------------

    def non_terminal_candidates(self) -> dict[str, str]:
        """Map candidate_id -> sealed_artifact_hash for every sealed-but-
        unresolved candidate.  Used by ``recover_at_boot`` to relaunch the
        consumer task after a crash/restart so one-ahead stays parallel.
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT candidate_id, sealed_artifact_hash FROM slice2b_candidate_lifecycle "
                "WHERE state = ?",
                (_CANDIDATE_SEALED,),
            ).fetchall()
        return {
            str(row["candidate_id"]): str(row["sealed_artifact_hash"])
            for row in rows
        }

    def recover_snapshot(self, candidate_id: str) -> dict[str, Any] | None:
        """Return the persisted sealed snapshot for boot-recovery, or None."""
        entry = self.snapshot(candidate_id)
        if entry is None:
            return None
        snap = entry.get("sealed_snapshot")
        return deepcopy(snap) if snap is not None else None

    # -- internal: self-transition that keeps the same state ----------------

    def _transition_with_self_state(
        self,
        candidate_id: str,
        *,
        mutator: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        """Persist a mutation while staying in the SEALED state (gate records).

        Enforces the candidate exists and is still SEALED (not terminal).
        """
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM slice2b_candidate_lifecycle WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise Slice2bError("validation_ledger_candidate_not_started")
            if str(row["state"]) != _CANDIDATE_SEALED:
                connection.rollback()
                raise Slice2bError("validation_ledger_candidate_already_terminal")
            entry = self._row_to_entry(row)
            mutator(entry)
            connection.execute(
                """
                UPDATE slice2b_candidate_lifecycle
                SET gate_results_json = ?, updated_at = ?
                WHERE candidate_id = ?
                """,
                (_canonical_json(entry.get("gate_results") or {}), now, candidate_id),
            )
            connection.commit()
            return deepcopy(entry)


# Backwards-compatible alias: existing imports of ``ValidationLedger`` keep
# resolving.  Constructed with a db_path (the activation passes the lifecycle
# sqlite path; tests pass a tmp_path).
ValidationLedger = CandidateLifecycle


# ---------------------------------------------------------------------------
# Seal operation (Producer side, invoked at workers_done)
# ---------------------------------------------------------------------------


class SealResult(dict):
    """Snapshot of the seal transaction returned to the orchestrator."""


def seal_candidate(
    adapter: ProducerConsumerWorkflowAdapter,
    *,
    snapshot: Mapping[str, Any],
    run_id: str,
    job_id: str,
    idempotency_key: str,
    artifact_digest: str,
    resource_claim: Mapping[str, Any],
    retry_policy: Mapping[str, Any],
    deadline: Mapping[str, Any],
    evaluation_contract_digest: str,
    executor_digest: str,
    repository_digest: str,
    runtime_digest: str,
) -> SealResult:
    """Submit the sealed candidate to the Consumer queue.

    Returns the durable effect id plus the canonical envelope.  The submit is
    idempotent: replaying the same envelope returns the same effect id and
    never creates a second queue entry (guaranteed by the adapter's
    command-lock-protected idempotency CAS).
    """

    envelope = build_slice2b_quality_envelope(
        snapshot=snapshot,
        run_id=run_id,
        job_id=job_id,
        idempotency_key=idempotency_key,
        resource_claim=resource_claim,
        retry_policy=retry_policy,
        deadline=deadline,
        artifact_digest=artifact_digest,
        evaluation_contract_digest=evaluation_contract_digest,
        executor_digest=executor_digest,
        repository_digest=repository_digest,
        runtime_digest=runtime_digest,
    )
    submitted = adapter.submit(envelope)
    return SealResult(
        effect_id=submitted["effect_id"],
        envelope_digest=envelope["envelope_digest"],
        candidate_id=envelope["candidate_id"],
        artifact_digest=artifact_digest,
        job_id=job_id,
        status=submitted["status"],
    )


# ---------------------------------------------------------------------------
# Consumer dispatcher
# ---------------------------------------------------------------------------


GateRunner = Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]]
"""Coroutine contract: given the sealed snapshot, run one gate and return a dict
with at least ``{"outcome": ..., "result_digest": ..., "detail": ...}``.

``outcome`` is one of ``success`` / ``candidate_failure`` /
``infrastructure_failure``.  The dispatcher converts a ``candidate_failure`` to
a terminal ledger rejection and stops the chain; ``infrastructure_failure``
aborts the current dispatch round but leaves the ledger running so the next
``recover``/``run_once`` retry resumes on the same envelope."""


class ConsumerDispatcher:
    """Dequeue one sealed envelope and run the canonical gate chain against it.

    The dispatcher is deliberately synchronous-in-one-coroutine: it leases the
    envelope, runs the caller-supplied gates in :data:`GATE_CHAIN_ORDER`, and
    records each outcome to the :class:`ValidationLedger`.  It does NOT advance
    the producer's version allocation, write to ``pipeline_state.json``, or
    perform any Git operation -- those remain owned by the unchanged canonical
    gate chain the caller injects via ``gates``.
    """

    def __init__(
        self,
        adapter: ProducerConsumerWorkflowAdapter,
        ledger: ValidationLedger,
        *,
        owner: str,
        death_proof_resolver: (
            Callable[[Mapping[str, Any]], Mapping[str, Any]] | None
        ) = None,
    ) -> None:
        self._adapter = adapter
        self._ledger = ledger
        self._owner = _require_safe_id(owner, "consumer_dispatcher_owner")
        self._death_proof_resolver = death_proof_resolver

    async def run_once(
        self,
        *,
        sealed_snapshots: Mapping[str, Mapping[str, Any]],
        gates: Mapping[str, GateRunner],
        now: float,
        lease_seconds: float,
    ) -> dict[str, Any]:
        """Claim at most one sealed envelope and run its gate chain.

        ``sealed_snapshots`` maps candidate_id -> the sealed snapshot dict (the
        same value passed to :func:`seal_candidate`).  ``gates`` maps each gate
        name in :data:`GATE_CHAIN_ORDER` to a runner.  Returns a dict describing
        what the dispatcher did this round (``dispatched``/``idle``/``failed``).
        """

        for gate_name in CONSUMER_GATE_CHAIN_ORDER:
            if gate_name not in gates:
                raise Slice2bError(f"consumer_dispatcher_missing_gate:{gate_name}")

        pending = self._adapter.recover(
            owner=self._owner,
            lease_seconds=lease_seconds,
            now=now,
            recovery_id=f"slice2b-dispatch-{int(now)}",
            death_proof_resolver=self._death_proof_resolver,
        )
        leases = pending.get("leases", [])
        if not leases:
            return {"dispatched": False, "reason": "no_leasable_envelope", "now": now}
        if len(leases) > 1:
            # Process exactly one per round to keep lease renewal bounded; the
            # next ``run_once`` picks up the rest.  This is the minimum viable
            # dispatcher; a fork/join executor is Slice 3.
            leases = leases[:1]

        lease = leases[0]
        candidate_id = lease["candidate_id"]
        snapshot = sealed_snapshots.get(candidate_id)
        if snapshot is None:
            raise Slice2bError(
                "consumer_dispatcher_sealed_snapshot_missing:" + candidate_id
            )
        self._ledger.start(
            candidate_id=candidate_id,
            sealed_artifact_hash=snapshot["artifact_hash"],
            envelope_effect_id=lease["effect_id"],
            envelope_digest=lease["envelope_digest"],
        )

        for gate_name in CONSUMER_GATE_CHAIN_ORDER:
            runner = gates[gate_name]
            try:
                gate_result = await runner(snapshot)
            except Exception as exc:  # gate-runner contract violation
                self._ledger.record_gate(
                    candidate_id=candidate_id,
                    gate_name=gate_name,
                    outcome="infrastructure_failure",
                    result_digest="0" * 64,
                    finished_at=now,
                    detail={"error": f"{type(exc).__name__}:{exc}"[:300]},
                )
                return {
                    "dispatched": True,
                    "candidate_id": candidate_id,
                    "failed_at_gate": gate_name,
                    "reason": "gate_runner_raised",
                    "now": now,
                }
            outcome = gate_result.get("outcome")
            result_digest = gate_result.get("result_digest")
            if outcome not in {"success", "candidate_failure", "infrastructure_failure"}:
                raise Slice2bError(
                    f"consumer_dispatcher_gate_bad_outcome:{gate_name}:{outcome}"
                )
            self._ledger.record_gate(
                candidate_id=candidate_id,
                gate_name=gate_name,
                outcome=outcome,
                result_digest=result_digest,
                finished_at=now,
                detail=gate_result.get("detail") or {},
            )
            if outcome == "candidate_failure":
                self._ledger.reject(
                    candidate_id=candidate_id,
                    reason=f"gate_failed:{gate_name}",
                    completed_at=now,
                )
                return {
                    "dispatched": True,
                    "candidate_id": candidate_id,
                    "failed_at_gate": gate_name,
                    "reason": "candidate_failure",
                    "now": now,
                }
            if outcome == "infrastructure_failure":
                return {
                    "dispatched": True,
                    "candidate_id": candidate_id,
                    "paused_at_gate": gate_name,
                    "reason": "infrastructure_failure",
                    "now": now,
                }

        # Consumer validation completes at precommit; ``commit_bot`` is owned by
        # the primary orchestrator behind the promotion barrier.
        entry = self._ledger.snapshot(candidate_id)
        if entry is None:
            raise Slice2bError("consumer_dispatcher_ledger_missing")
        precommit_gate = entry["gate_results"].get("run_precommit_eval")
        if precommit_gate is None:
            raise Slice2bError("consumer_dispatcher_precommit_missing")
        receipt_digest = precommit_gate.get("digest")
        if not receipt_digest:
            raise Slice2bError("consumer_dispatcher_precommit_missing_digest")
        promotion_receipt = {
            "receipt_digest": receipt_digest,
            "consumer_precommit_complete": True,
            "detail": precommit_gate.get("detail") or {},
        }
        self._ledger.promote(
            candidate_id=candidate_id,
            promotion_receipt=promotion_receipt,
            completed_at=now,
        )
        return {
            "dispatched": True,
            "candidate_id": candidate_id,
            "promoted": True,
            "promotion_receipt_digest": receipt_digest,
            "now": now,
        }


# ---------------------------------------------------------------------------
# One-ahead coordinator
# ---------------------------------------------------------------------------


class OneAheadCoordinator:
    """Rendezvous between the Producer lane and the in-flight Consumer.

    Minimal-slice semantics:

    * :meth:`note_sealed` records that the Producer has handed candidate N to
      the Consumer and is therefore cleared to begin preparing draft N+1.
    * :meth:`producer_may_prepare_next` returns True iff exactly one sealed
      candidate is in flight (the one-ahead buffer slot is occupied and the
      producer may begin the next ``prepare_generation`` draft).
    * :meth:`producer_may_advance` returns True iff the high-water seal slot is
      free (``len(_in_flight) < MAX_SEALED_AWAITING_VALIDATION``).
    * :meth:`wait_for_promotion_readiness` is the synchronous fail-closed
      promotion barrier: the Producer's publication path must await Consumer
      completion for the candidate it is publishing.  This method does NOT
      publish; it only proves the Consumer has promoted (or surfaces the
      rejection).  Publication itself remains in the unchanged ``commit_bot``.
    """

    MAX_SEALED_AWAITING_VALIDATION = 1

    def __init__(self, ledger: ValidationLedger) -> None:
        self._ledger = ledger
        # candidate_id -> artifact_hash for every sealed-but-unresolved candidate.
        self._in_flight: dict[str, str] = {}
        # candidate_id -> asyncio.Event fired when the Consumer reaches a terminal state.
        self._events: dict[str, asyncio.Event] = {}

    def note_sealed(self, *, candidate_id: str, artifact_hash: str) -> None:
        _require_safe_id(candidate_id, "candidate_id")
        _require_digest(artifact_hash, "artifact_hash")
        if candidate_id in self._in_flight:
            if self._in_flight[candidate_id] != artifact_hash:
                raise Slice2bError("one_ahead_sealed_artifact_drift")
            return
        if len(self._in_flight) >= self.MAX_SEALED_AWAITING_VALIDATION:
            raise Slice2bError("one_ahead_high_water_exceeded")
        self._in_flight[candidate_id] = artifact_hash
        self._events.setdefault(candidate_id, asyncio.Event())

    def producer_may_prepare_next(self) -> bool:
        """Producer may begin the next ``prepare_generation`` draft.

        With ``MAX_SEALED_AWAITING_VALIDATION == 1``, this is True exactly when
        one sealed candidate is in flight and the consumer owns the gate chain.
        """

        return len(self._in_flight) == self.MAX_SEALED_AWAITING_VALIDATION

    def producer_may_advance(self) -> bool:
        """Producer may seal another candidate (high-water capacity check)."""

        return len(self._in_flight) < self.MAX_SEALED_AWAITING_VALIDATION

    def in_flight(self) -> dict[str, str]:
        return dict(self._in_flight)

    def note_terminal(self, *, candidate_id: str) -> None:
        event = self._events.get(candidate_id)
        self._in_flight.pop(candidate_id, None)
        if event is not None:
            event.set()

    async def wait_for_promotion_readiness(
        self,
        *,
        candidate_id: str,
        poll_interval: float = 0.05,
        timeout: float | None = None,
        poll_callback: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Block publication until the Consumer has finished with ``candidate_id``.

        Returns the terminal ledger entry.  Raises :class:`Slice2bError` if the
        Consumer rejected (publication must NOT proceed) or if the candidate is
        unknown to the coordinator.

        ``poll_callback`` is invoked once per poll tick so a host loop can drive
        the Consumer dispatcher while waiting (the dispatcher is itself just an
        async coroutine, so a single event loop can run both lanes without
        threads).  This is the synchronous fail-closed barrier.
        """

        if candidate_id not in self._events and not self._ledger.is_terminal(candidate_id):
            raise Slice2bError("one_ahead_barrier_unknown_candidate")
        start = time.monotonic()
        while not self._ledger.is_terminal(candidate_id):
            if poll_callback is not None:
                poll_callback()
            event = self._events.get(candidate_id)
            if event is not None:
                try:
                    await asyncio.wait_for(
                        event.wait(), timeout=poll_interval
                    )
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(poll_interval)
            if timeout is not None and (time.monotonic() - start) > timeout:
                raise Slice2bError("one_ahead_barrier_timeout")
        entry = self._ledger.snapshot(candidate_id)
        if entry is None or entry["validation_outcome"] not in {"promoted", "rejected"}:
            raise Slice2bError("one_ahead_barrier_no_terminal_entry")
        self.note_terminal(candidate_id=candidate_id)
        if entry["validation_outcome"] != "promoted":
            raise Slice2bError(
                f"one_ahead_barrier_rejected:{entry['terminal_reason']}"
            )
        return entry


# ---------------------------------------------------------------------------
# Opt-in orchestrator seam (default-off; honors the design-doc inertness fence)
# ---------------------------------------------------------------------------


def slice2b_enabled(context: Mapping[str, Any] | None) -> bool:
    """Read the explicit opt-in flag from the orchestrator context.

    The canonical runtime stays on the legacy single-slot path until this flag
    is truthy.  No production call site sets it truthy yet.
    """

    if context is None:
        return False
    return bool(context.get("pipeline_slice2b_enabled"))


__all__ = [
    "CONSUMER_GATE_CHAIN_ORDER",
    "GATE_CHAIN_ORDER",
    "SLICE2B_LEDGER_KIND",
    "SLICE2B_SEALED_KIND",
    "ConsumerDispatcher",
    "GateRunner",
    "OneAheadCoordinator",
    "SealResult",
    "Slice2bError",
    "ValidationLedger",
    "build_sealed_candidate_snapshot",
    "build_slice2b_quality_envelope",
    "seal_candidate",
    "slice2b_enabled",
]
