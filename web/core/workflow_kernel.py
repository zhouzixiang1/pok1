"""Small durable-execution kernel for local evolution workflows.

The kernel deliberately owns no poker or LLM semantics.  It provides an
append-only event stream, an atomic event/outbox effect request, fenced effect
leases, and one-writer command locks.  Domain reducers live beside their
workflow and must remain pure.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterable


KERNEL_SCHEMA_VERSION = 1


class WorkflowError(RuntimeError):
    """Base class for durable workflow failures."""


class WorkflowConflict(WorkflowError):
    """The caller used a stale stream version or conflicting idempotency key."""


class WorkflowBusy(WorkflowError):
    """Another process owns the generation command lock."""


class InvalidCompletion(WorkflowError):
    """An effect completion did not match the current fenced lease."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def content_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class WorkflowEvent:
    run_id: str
    seq: int
    event_type: str
    schema_version: int
    payload: dict[str, Any]
    payload_digest: str
    causation_id: str


@dataclass(frozen=True)
class EffectLease:
    effect_id: str
    run_id: str
    kind: str
    input_digest: str
    attempt: int
    max_attempts: int
    lease_epoch: int
    lease_until: float
    status: str


def reduce_events(
    initial_state: Any,
    events: Iterable[WorkflowEvent],
    reducer,
) -> Any:
    """Replay a history through a domain-owned pure reducer."""
    state = initial_state
    for event in events:
        state = reducer(state, event)
    return state


class WorkflowStore:
    """SQLite-WAL event/effect store for one local checkout."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_dir = self.path.parent / "workflow_locks"
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            existing_user_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            if existing_user_version not in {0, KERNEL_SCHEMA_VERSION}:
                raise WorkflowConflict(
                    "unsupported workflow database schema: "
                    f"{existing_user_version} != {KERNEL_SCHEMA_VERSION}"
                )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS workflow_instances (
                    run_id TEXT PRIMARY KEY,
                    definition_version INTEGER NOT NULL,
                    stream_version INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    fence_epoch INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workflow_events (
                    run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    causation_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (run_id, seq),
                    UNIQUE (run_id, causation_id),
                    FOREIGN KEY (run_id) REFERENCES workflow_instances(run_id)
                );

                CREATE TABLE IF NOT EXISTS effects (
                    effect_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    input_payload TEXT NOT NULL,
                    input_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL,
                    lease_epoch INTEGER NOT NULL DEFAULT 0,
                    lease_owner TEXT,
                    lease_until REAL,
                    result_payload TEXT,
                    result_digest TEXT,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES workflow_instances(run_id)
                );

                CREATE TABLE IF NOT EXISTS outbox (
                    effect_id TEXT PRIMARY KEY,
                    available_at REAL NOT NULL,
                    dispatched_at REAL,
                    FOREIGN KEY (effect_id) REFERENCES effects(effect_id)
                );

                CREATE TABLE IF NOT EXISTS inbox (
                    completion_id TEXT PRIMARY KEY,
                    effect_id TEXT NOT NULL,
                    lease_epoch INTEGER NOT NULL,
                    payload_digest TEXT NOT NULL,
                    accepted INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY (effect_id) REFERENCES effects(effect_id)
                );

                CREATE INDEX IF NOT EXISTS idx_workflow_events_run
                    ON workflow_events(run_id, seq);
                CREATE INDEX IF NOT EXISTS idx_effects_run_status
                    ON effects(run_id, status);
                CREATE INDEX IF NOT EXISTS idx_outbox_available
                    ON outbox(dispatched_at, available_at);
                """
            )
            user_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            if user_version == 0:
                connection.execute(
                    f"PRAGMA user_version = {KERNEL_SCHEMA_VERSION}"
                )
            elif user_version != KERNEL_SCHEMA_VERSION:
                raise WorkflowConflict(
                    "unsupported workflow database schema: "
                    f"{user_version} != {KERNEL_SCHEMA_VERSION}"
                )

    @contextmanager
    def command_lock(self, run_id: str, *, blocking: bool = False):
        """Serialize commands for a generation across local processes."""
        safe = hashlib.sha256(str(run_id).encode("utf-8")).hexdigest()
        lock_path = self.lock_dir / f"{safe}.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                flags = fcntl.LOCK_EX
                if not blocking:
                    flags |= fcntl.LOCK_NB
                fcntl.flock(descriptor, flags)
            except BlockingIOError as exc:
                raise WorkflowBusy(f"workflow command already active: {run_id}") from exc
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def ensure_instance(
        self,
        run_id: str,
        *,
        definition_version: int,
        status: str = "running",
    ) -> dict[str, Any]:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM workflow_instances WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO workflow_instances(
                        run_id, definition_version, stream_version, status,
                        fence_epoch, created_at, updated_at
                    ) VALUES (?, ?, 0, ?, 0, ?, ?)
                    """,
                    (run_id, int(definition_version), status, now, now),
                )
            elif int(row["definition_version"]) != int(definition_version):
                connection.rollback()
                raise WorkflowConflict(
                    f"definition version mismatch for {run_id}: "
                    f"stored={row['definition_version']} requested={definition_version}"
                )
            connection.commit()
        return self.instance(run_id)

    def instance(self, run_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_instances WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return dict(row) if row is not None else {}

    def bump_fence(self, run_id: str, *, expected_epoch: int | None = None) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT fence_epoch FROM workflow_instances WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise WorkflowConflict(f"unknown workflow instance: {run_id}")
            current = int(row["fence_epoch"])
            if expected_epoch is not None and current != int(expected_epoch):
                connection.rollback()
                raise WorkflowConflict(
                    f"stale workflow fence for {run_id}: {expected_epoch} != {current}"
                )
            next_epoch = current + 1
            connection.execute(
                "UPDATE workflow_instances SET fence_epoch = ?, updated_at = ? WHERE run_id = ?",
                (next_epoch, time.time(), run_id),
            )
            connection.commit()
        return next_epoch

    def _append_event_locked(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        causation_id: str,
        expected_version: int | None,
        schema_version: int,
    ) -> WorkflowEvent:
        encoded = canonical_json(payload)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        duplicate = connection.execute(
            "SELECT * FROM workflow_events WHERE run_id = ? AND causation_id = ?",
            (run_id, causation_id),
        ).fetchone()
        if duplicate is not None:
            if (
                duplicate["run_id"] != run_id
                or duplicate["event_type"] != event_type
                or duplicate["payload_digest"] != digest
            ):
                raise WorkflowConflict(
                    f"causation id reused with different event: {causation_id}"
                )
            return self._event_from_row(duplicate)

        instance = connection.execute(
            "SELECT * FROM workflow_instances WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if instance is None:
            raise WorkflowConflict(f"unknown workflow instance: {run_id}")
        current = int(instance["stream_version"])
        if expected_version is not None and current != int(expected_version):
            raise WorkflowConflict(
                f"stale stream version for {run_id}: {expected_version} != {current}"
            )
        seq = current + 1
        now = time.time()
        connection.execute(
            """
            INSERT INTO workflow_events(
                run_id, seq, event_type, schema_version, payload,
                payload_digest, causation_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                seq,
                event_type,
                int(schema_version),
                encoded,
                digest,
                causation_id,
                now,
            ),
        )
        connection.execute(
            """
            UPDATE workflow_instances
            SET stream_version = ?, updated_at = ?
            WHERE run_id = ?
            """,
            (seq, now, run_id),
        )
        return WorkflowEvent(
            run_id=run_id,
            seq=seq,
            event_type=event_type,
            schema_version=int(schema_version),
            payload=json.loads(encoded),
            payload_digest=digest,
            causation_id=causation_id,
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> WorkflowEvent:
        return WorkflowEvent(
            run_id=str(row["run_id"]),
            seq=int(row["seq"]),
            event_type=str(row["event_type"]),
            schema_version=int(row["schema_version"]),
            payload=json.loads(row["payload"]),
            payload_digest=str(row["payload_digest"]),
            causation_id=str(row["causation_id"]),
        )

    def append_event(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        causation_id: str,
        expected_version: int | None = None,
        schema_version: int = KERNEL_SCHEMA_VERSION,
    ) -> WorkflowEvent:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                event = self._append_event_locked(
                    connection,
                    run_id=run_id,
                    event_type=event_type,
                    payload=payload,
                    causation_id=causation_id,
                    expected_version=expected_version,
                    schema_version=schema_version,
                )
            except Exception:
                connection.rollback()
                raise
            connection.commit()
        return event

    def terminal_transition(
        self,
        run_id: str,
        *,
        event_type: str,
        payload: dict[str, Any],
        causation_id: str,
        expected_version: int,
        status: str,
    ) -> WorkflowEvent:
        """Atomically append a terminal event, fence leases, and cancel effects."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                event = self._append_event_locked(
                    connection,
                    run_id=run_id,
                    event_type=event_type,
                    payload=payload,
                    causation_id=causation_id,
                    expected_version=expected_version,
                    schema_version=KERNEL_SCHEMA_VERSION,
                )
                now = time.time()
                connection.execute(
                    """
                    UPDATE effects
                    SET status = 'abandoned', lease_owner = NULL,
                        lease_until = NULL, updated_at = ?
                    WHERE run_id = ?
                      AND status NOT IN ('completed', 'exhausted', 'abandoned')
                    """,
                    (now, run_id),
                )
                connection.execute(
                    """
                    UPDATE outbox
                    SET dispatched_at = COALESCE(dispatched_at, ?)
                    WHERE effect_id IN (
                        SELECT effect_id FROM effects WHERE run_id = ?
                    )
                    """,
                    (now, run_id),
                )
                connection.execute(
                    """
                    UPDATE workflow_instances
                    SET status = ?, fence_epoch = fence_epoch + 1, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (str(status), now, run_id),
                )
            except Exception:
                connection.rollback()
                raise
            connection.commit()
        return event

    def append_event_and_set_status(
        self,
        run_id: str,
        *,
        event_type: str,
        payload: dict[str, Any],
        causation_id: str,
        expected_version: int,
        status: str,
    ) -> WorkflowEvent:
        """Append a domain event and update its instance projection atomically."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                event = self._append_event_locked(
                    connection,
                    run_id=run_id,
                    event_type=event_type,
                    payload=payload,
                    causation_id=causation_id,
                    expected_version=expected_version,
                    schema_version=KERNEL_SCHEMA_VERSION,
                )
                connection.execute(
                    """
                    UPDATE workflow_instances
                    SET status = ?, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (str(status), time.time(), run_id),
                )
            except Exception:
                connection.rollback()
                raise
            connection.commit()
        return event

    def events(self, run_id: str) -> list[WorkflowEvent]:
        with self._connect() as connection:
            connection.execute("BEGIN")
            try:
                rows = connection.execute(
                    "SELECT * FROM workflow_events WHERE run_id = ? ORDER BY seq",
                    (run_id,),
                ).fetchall()
                instance = connection.execute(
                    "SELECT stream_version FROM workflow_instances WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
            finally:
                connection.commit()
        events = []
        for expected_seq, row in enumerate(rows, start=1):
            event = self._event_from_row(row)
            if event.seq != expected_seq:
                raise WorkflowConflict(
                    f"non-contiguous workflow history for {run_id}: "
                    f"expected {expected_seq}, got {event.seq}"
                )
            if content_digest(event.payload) != event.payload_digest:
                raise WorkflowConflict(
                    f"workflow payload digest mismatch: {run_id}#{event.seq}"
                )
            events.append(event)
        stored_version = int(instance["stream_version"]) if instance else 0
        if stored_version != len(events):
            raise WorkflowConflict(
                f"workflow stream version mismatch for {run_id}: "
                f"{stored_version} != {len(events)}"
            )
        return events

    def request_effect(
        self,
        *,
        run_id: str,
        effect_id: str,
        kind: str,
        input_payload: dict[str, Any],
        causation_id: str,
        max_attempts: int = 3,
        available_at: float | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """Atomically append EffectRequested and enqueue its outbox row."""
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        encoded_input = canonical_json(input_payload)
        input_digest = hashlib.sha256(encoded_input.encode("utf-8")).hexdigest()
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            instance = connection.execute(
                "SELECT status FROM workflow_instances WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if instance is None or str(instance["status"]) != "running":
                connection.rollback()
                raise WorkflowConflict(
                    f"workflow instance is not running: {run_id}"
                )
            existing = connection.execute(
                "SELECT * FROM effects WHERE effect_id = ?",
                (effect_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["run_id"] != run_id
                    or existing["kind"] != kind
                    or existing["input_digest"] != input_digest
                ):
                    connection.rollback()
                    raise WorkflowConflict(
                        f"effect id reused with different input: {effect_id}"
                    )
                connection.commit()
                return dict(existing)

            try:
                self._append_event_locked(
                    connection,
                    run_id=run_id,
                    event_type="EffectRequested",
                    payload={
                        "effect_id": effect_id,
                        "kind": kind,
                        "input_digest": input_digest,
                        "max_attempts": int(max_attempts),
                    },
                    causation_id=causation_id,
                    expected_version=expected_version,
                    schema_version=KERNEL_SCHEMA_VERSION,
                )
                connection.execute(
                    """
                    INSERT INTO effects(
                        effect_id, run_id, kind, input_payload, input_digest,
                        status, attempt, max_attempts, lease_epoch,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'requested', 0, ?, 0, ?, ?)
                    """,
                    (
                        effect_id,
                        run_id,
                        kind,
                        encoded_input,
                        input_digest,
                        int(max_attempts),
                        now,
                        now,
                    ),
                )
                connection.execute(
                    "INSERT INTO outbox(effect_id, available_at) VALUES (?, ?)",
                    (effect_id, float(available_at or now)),
                )
            except Exception:
                connection.rollback()
                raise
            connection.commit()
        return self.effect(effect_id)

    def effect(self, effect_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM effects WHERE effect_id = ?",
                (effect_id,),
            ).fetchone()
        if row is None:
            return {}
        result = dict(row)
        result["input_payload"] = json.loads(result["input_payload"])
        if result.get("result_payload"):
            result["result_payload"] = json.loads(result["result_payload"])
        return result

    def pending_outbox(self, *, now: float | None = None) -> list[dict[str, Any]]:
        cutoff = float(now if now is not None else time.time())
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT o.*, e.run_id, e.kind, e.input_digest, e.status
                FROM outbox o JOIN effects e USING(effect_id)
                WHERE (
                    o.dispatched_at IS NULL AND o.available_at <= ?
                    AND e.status IN ('requested', 'retry')
                ) OR (
                    e.status = 'running' AND e.lease_until IS NOT NULL
                    AND e.lease_until <= ?
                )
                ORDER BY o.available_at, o.effect_id
                """,
                (cutoff, cutoff),
            ).fetchall()
        return [dict(row) for row in rows]

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

    def fail_effect(
        self,
        effect_id: str,
        *,
        lease_epoch: int,
        error: str,
        retryable: bool,
        causation_id: str,
    ) -> dict[str, Any]:
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
                    (status, str(error)[:4000], now, effect_id),
                )
                if status == "retry":
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

    def complete_effect(
        self,
        effect_id: str,
        *,
        lease_epoch: int,
        completion_id: str,
        result_payload: dict[str, Any],
        causation_id: str,
        followup_events: Iterable[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Accept a fenced completion and its domain events atomically.

        A durable effect receipt without the domain transition that consumes it
        is an unrecoverable split-brain window: replay sees a terminal effect but
        cannot derive the next command.  Callers therefore supply any consuming
        domain events here so the receipt, effect row, and workflow history share
        one SQLite transaction.
        """
        encoded = canonical_json(result_payload)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = connection.execute(
                "SELECT * FROM inbox WHERE completion_id = ?",
                (completion_id,),
            ).fetchone()
            if duplicate is not None:
                if duplicate["effect_id"] != effect_id or duplicate["payload_digest"] != digest:
                    connection.rollback()
                    raise WorkflowConflict(
                        f"completion id reused with different result: {completion_id}"
                    )
                connection.commit()
                return {
                    "accepted": bool(duplicate["accepted"]),
                    "duplicate": True,
                    "effect": self.effect(effect_id),
                }
            row = connection.execute(
                "SELECT * FROM effects WHERE effect_id = ?",
                (effect_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise WorkflowConflict(f"unknown effect: {effect_id}")
            accepted = bool(
                row["status"] == "running"
                and int(row["lease_epoch"]) == int(lease_epoch)
            )
            connection.execute(
                """
                INSERT INTO inbox(
                    completion_id, effect_id, lease_epoch, payload_digest,
                    accepted, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    completion_id,
                    effect_id,
                    int(lease_epoch),
                    digest,
                    1 if accepted else 0,
                    now,
                ),
            )
            if not accepted:
                connection.commit()
                return {
                    "accepted": False,
                    "duplicate": False,
                    "reason": "stale_lease_epoch",
                    "effect": self.effect(effect_id),
                }
            try:
                self._append_event_locked(
                    connection,
                    run_id=str(row["run_id"]),
                    event_type="EffectCompleted",
                    payload={
                        "effect_id": effect_id,
                        "attempt": int(row["attempt"]),
                        "lease_epoch": int(lease_epoch),
                        "result_digest": digest,
                    },
                    causation_id=causation_id,
                    expected_version=None,
                    schema_version=KERNEL_SCHEMA_VERSION,
                )
                for followup in followup_events or ():
                    event_type = str(followup.get("event_type") or "").strip()
                    event_causation_id = str(
                        followup.get("causation_id") or ""
                    ).strip()
                    event_payload = followup.get("payload")
                    if (
                        not event_type
                        or not event_causation_id
                        or not isinstance(event_payload, dict)
                    ):
                        raise WorkflowConflict(
                            "effect follow-up event requires event_type, "
                            "causation_id, and object payload"
                        )
                    self._append_event_locked(
                        connection,
                        run_id=str(row["run_id"]),
                        event_type=event_type,
                        payload=event_payload,
                        causation_id=event_causation_id,
                        expected_version=None,
                        schema_version=int(
                            followup.get("schema_version")
                            or KERNEL_SCHEMA_VERSION
                        ),
                    )
                connection.execute(
                    """
                    UPDATE effects
                    SET status = 'completed', result_payload = ?,
                        result_digest = ?, lease_owner = NULL,
                        lease_until = NULL, updated_at = ?
                    WHERE effect_id = ?
                    """,
                    (encoded, digest, now, effect_id),
                )
            except Exception:
                connection.rollback()
                raise
            connection.commit()
        return {
            "accepted": True,
            "duplicate": False,
            "effect": self.effect(effect_id),
        }

    def set_instance_status(self, run_id: str, status: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                "UPDATE workflow_instances SET status = ?, updated_at = ? WHERE run_id = ?",
                (str(status), time.time(), run_id),
            ).rowcount
            if not updated:
                connection.rollback()
                raise WorkflowConflict(f"unknown workflow instance: {run_id}")
            connection.commit()
