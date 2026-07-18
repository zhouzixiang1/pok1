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
import math
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


class WorkflowDeadlineExceeded(WorkflowBusy):
    """A bounded workflow operation could not finish before its deadline."""


class InvalidCompletion(WorkflowError):
    """An effect completion did not match the current fenced lease."""


class _ClosingConnection(sqlite3.Connection):
    """Commit/rollback like sqlite's context manager, then close the FD.

    ``sqlite3.Connection.__exit__`` does not close the connection.  The
    workflow store opens a fresh connection per command, so relying on that
    default leaked one descriptor per read/write and eventually made recovery
    fail with ``EMFILE`` during repeated Master retries.
    """

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


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

    def __init__(
        self,
        path: str | Path,
        *,
        deadline_monotonic: float | None = None,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_dir = self.path.parent / "workflow_locks"
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        # SQLite can reject concurrent first-open ``PRAGMA journal_mode=WAL``
        # calls with ``database is locked`` before its busy handler has a
        # chance to serialize them.  Every command lock is acquired after a
        # store exists, so a first-use begin/abandon race otherwise fails
        # outside the workflow fence.  Serialize schema/WAL initialization
        # across processes with a database-scoped file lock; normal commands
        # retain their narrower run-scoped locks below.
        with self._initialization_lock(deadline_monotonic=deadline_monotonic):
            self._initialize(deadline_monotonic=deadline_monotonic)

    @contextmanager
    def _initialization_lock(self, *, deadline_monotonic: float | None = None):
        safe = hashlib.sha256(
            str(self.path.resolve()).encode("utf-8")
        ).hexdigest()
        lock_path = self.lock_dir / f"{safe}.initialize.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        acquired = False
        try:
            if deadline_monotonic is None:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                acquired = True
            else:
                while True:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                        break
                    except BlockingIOError:
                        remaining = self._remaining_deadline_seconds(
                            deadline_monotonic,
                            operation="store_initialization_lock",
                        )
                        assert remaining is not None
                        time.sleep(min(0.01, remaining))
            self._remaining_deadline_seconds(
                deadline_monotonic,
                operation="store_initialization_lock",
            )
            yield
        finally:
            try:
                if acquired:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    @staticmethod
    def _remaining_deadline_seconds(
        deadline_monotonic: float | None,
        *,
        operation: str,
    ) -> float | None:
        if deadline_monotonic is None:
            return None
        if isinstance(deadline_monotonic, bool):
            raise WorkflowDeadlineExceeded(
                f"workflow deadline invalid: {operation}"
            )
        try:
            deadline = float(deadline_monotonic)
        except (TypeError, ValueError) as exc:
            raise WorkflowDeadlineExceeded(
                f"workflow deadline invalid: {operation}"
            ) from exc
        if not math.isfinite(deadline):
            raise WorkflowDeadlineExceeded(
                f"workflow deadline invalid: {operation}"
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WorkflowDeadlineExceeded(
                f"workflow deadline exceeded: {operation}"
            )
        return remaining

    @staticmethod
    def _sqlite_busy_error(exc: BaseException) -> bool:
        message = str(exc).lower()
        return "locked" in message or "busy" in message

    def _set_connection_busy_timeout(
        self,
        connection: sqlite3.Connection,
        deadline_monotonic: float | None,
        *,
        operation: str,
    ) -> None:
        remaining = self._remaining_deadline_seconds(
            deadline_monotonic,
            operation=operation,
        )
        timeout_ms = (
            30_000
            if remaining is None
            else max(1, min(30_000, int(math.ceil(remaining * 1000.0))))
        )
        connection.execute(f"PRAGMA busy_timeout={timeout_ms}")

    def _raise_deadline_sqlite_busy(
        self,
        exc: sqlite3.OperationalError,
        deadline_monotonic: float | None,
        *,
        operation: str,
    ) -> None:
        if deadline_monotonic is not None and self._sqlite_busy_error(exc):
            raise WorkflowDeadlineExceeded(
                f"workflow deadline exceeded waiting for SQLite: {operation}"
            ) from exc
        raise exc

    def _begin_immediate(
        self,
        connection: sqlite3.Connection,
        *,
        deadline_monotonic: float | None,
        operation: str,
    ) -> None:
        self._set_connection_busy_timeout(
            connection,
            deadline_monotonic,
            operation=operation,
        )
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            self._raise_deadline_sqlite_busy(
                exc,
                deadline_monotonic,
                operation=operation,
            )
        self._remaining_deadline_seconds(
            deadline_monotonic,
            operation=operation,
        )

    def _commit(
        self,
        connection: sqlite3.Connection,
        *,
        deadline_monotonic: float | None,
        operation: str,
    ) -> None:
        self._remaining_deadline_seconds(
            deadline_monotonic,
            operation=operation,
        )
        self._set_connection_busy_timeout(
            connection,
            deadline_monotonic,
            operation=operation,
        )
        try:
            connection.commit()
        except sqlite3.OperationalError as exc:
            self._raise_deadline_sqlite_busy(
                exc,
                deadline_monotonic,
                operation=operation,
            )
        # Once COMMIT returns successfully, those bytes are the durable
        # authority.  Re-checking the clock here could report a timeout after
        # the transition was already visible, creating an avoidable ambiguous
        # outcome.  The deadline is checked immediately before COMMIT and all
        # lock/busy waits inside it use the remaining SQLite busy timeout.

    def _connect(
        self,
        *,
        deadline_monotonic: float | None = None,
    ) -> sqlite3.Connection:
        remaining = self._remaining_deadline_seconds(
            deadline_monotonic,
            operation="sqlite_connect",
        )
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=30.0 if remaining is None else max(0.001, remaining),
                isolation_level=None,
                factory=_ClosingConnection,
            )
            connection.row_factory = sqlite3.Row
            self._set_connection_busy_timeout(
                connection,
                deadline_monotonic,
                operation="sqlite_configure",
            )
            connection.execute("PRAGMA journal_mode=WAL")
            self._remaining_deadline_seconds(
                deadline_monotonic,
                operation="sqlite_journal_mode",
            )
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            self._set_connection_busy_timeout(
                connection,
                deadline_monotonic,
                operation="sqlite_ready",
            )
            return connection
        except sqlite3.OperationalError as exc:
            if connection is not None:
                connection.close()
            self._raise_deadline_sqlite_busy(
                exc,
                deadline_monotonic,
                operation="sqlite_connect",
            )
            raise AssertionError("unreachable")
        except Exception:
            if connection is not None:
                connection.close()
            raise

    def _initialize(self, *, deadline_monotonic: float | None = None) -> None:
        with self._connect(deadline_monotonic=deadline_monotonic) as connection:
            self._remaining_deadline_seconds(
                deadline_monotonic,
                operation="sqlite_initialize",
            )
            existing_user_version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            if existing_user_version not in {0, KERNEL_SCHEMA_VERSION}:
                raise WorkflowConflict(
                    "unsupported workflow database schema: "
                    f"{existing_user_version} != {KERNEL_SCHEMA_VERSION}"
                )
            try:
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
            except sqlite3.OperationalError as exc:
                self._raise_deadline_sqlite_busy(
                    exc,
                    deadline_monotonic,
                    operation="sqlite_initialize_schema",
                )
            self._remaining_deadline_seconds(
                deadline_monotonic,
                operation="sqlite_initialize_schema",
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
    def command_lock(
        self,
        run_id: str,
        *,
        blocking: bool = False,
        deadline_monotonic: float | None = None,
    ):
        """Serialize commands for a generation across local processes."""
        safe = hashlib.sha256(str(run_id).encode("utf-8")).hexdigest()
        lock_path = self.lock_dir / f"{safe}.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        acquired = False
        try:
            if blocking and deadline_monotonic is None:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                acquired = True
            else:
                while True:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                        break
                    except BlockingIOError as exc:
                        if not blocking:
                            raise WorkflowBusy(
                                f"workflow command already active: {run_id}"
                            ) from exc
                        remaining = self._remaining_deadline_seconds(
                            deadline_monotonic,
                            operation=f"command_lock:{run_id}",
                        )
                        assert remaining is not None
                        time.sleep(min(0.01, remaining))
            self._remaining_deadline_seconds(
                deadline_monotonic,
                operation=f"command_lock:{run_id}",
            )
            yield
        finally:
            try:
                if acquired:
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

    def create_terminal_transition(
        self,
        run_id: str,
        *,
        definition_version: int,
        event_type: str,
        payload: dict[str, Any],
        causation_id: str,
        status: str,
    ) -> WorkflowEvent:
        """Atomically create a new instance with its first terminal event."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if connection.execute(
                    "SELECT 1 FROM workflow_instances WHERE run_id = ?",
                    (run_id,),
                ).fetchone() is not None:
                    raise WorkflowConflict(
                        f"workflow instance already exists: {run_id}"
                    )
                now = time.time()
                connection.execute(
                    """
                    INSERT INTO workflow_instances(
                        run_id, definition_version, stream_version, status,
                        fence_epoch, created_at, updated_at
                    ) VALUES (?, ?, 0, ?, 0, ?, ?)
                    """,
                    (
                        run_id,
                        int(definition_version),
                        str(status),
                        now,
                        now,
                    ),
                )
                event = self._append_event_locked(
                    connection,
                    run_id=run_id,
                    event_type=event_type,
                    payload=payload,
                    causation_id=causation_id,
                    expected_version=0,
                    schema_version=KERNEL_SCHEMA_VERSION,
                )
                connection.execute(
                    """
                    UPDATE workflow_instances
                    SET status = ?, fence_epoch = 1, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (str(status), time.time(), run_id),
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

    def events(
        self,
        run_id: str,
        *,
        deadline_monotonic: float | None = None,
    ) -> list[WorkflowEvent]:
        with self._connect(deadline_monotonic=deadline_monotonic) as connection:
            self._set_connection_busy_timeout(
                connection,
                deadline_monotonic,
                operation="events_begin",
            )
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
                self._commit(
                    connection,
                    deadline_monotonic=deadline_monotonic,
                    operation="events_commit",
                )
        self._remaining_deadline_seconds(
            deadline_monotonic,
            operation="events_read",
        )
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

    @staticmethod
    def _effect_from_row(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {}
        result = dict(row)
        result["input_payload"] = json.loads(result["input_payload"])
        if result.get("result_payload"):
            result["result_payload"] = json.loads(result["result_payload"])
        return result

    def effect(
        self,
        effect_id: str,
        *,
        deadline_monotonic: float | None = None,
    ) -> dict[str, Any]:
        with self._connect(deadline_monotonic=deadline_monotonic) as connection:
            row = connection.execute(
                "SELECT * FROM effects WHERE effect_id = ?",
                (effect_id,),
            ).fetchone()
        self._remaining_deadline_seconds(
            deadline_monotonic,
            operation="effect_read",
        )
        return self._effect_from_row(row)

    def effects_for_run(self, run_id: str) -> list[dict[str, Any]]:
        """Read all effects owned by one workflow in stable identity order."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM effects WHERE run_id = ? ORDER BY effect_id",
                (str(run_id),),
            ).fetchall()
        return [self._effect_from_row(row) for row in rows]

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
            not isinstance(expected_owner, str)
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

    def complete_effect(
        self,
        effect_id: str,
        *,
        lease_epoch: int,
        completion_id: str,
        result_payload: dict[str, Any],
        causation_id: str,
        followup_events: Iterable[dict[str, Any]] | None = None,
        require_live_lease: bool = False,
        now: float | None = None,
        deadline_monotonic: float | None = None,
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
        self._remaining_deadline_seconds(
            deadline_monotonic,
            operation="complete_effect_encode",
        )
        with self._connect(deadline_monotonic=deadline_monotonic) as connection:
            self._begin_immediate(
                connection,
                deadline_monotonic=deadline_monotonic,
                operation="complete_effect_begin",
            )
            duplicate = connection.execute(
                "SELECT * FROM inbox WHERE completion_id = ?",
                (completion_id,),
            ).fetchone()
            self._remaining_deadline_seconds(
                deadline_monotonic,
                operation="complete_effect_duplicate_check",
            )
            if duplicate is not None:
                if duplicate["effect_id"] != effect_id or duplicate["payload_digest"] != digest:
                    connection.rollback()
                    raise WorkflowConflict(
                        f"completion id reused with different result: {completion_id}"
                    )
                duplicate_effect_row = connection.execute(
                    "SELECT * FROM effects WHERE effect_id = ?",
                    (effect_id,),
                ).fetchone()
                self._remaining_deadline_seconds(
                    deadline_monotonic,
                    operation="complete_effect_duplicate_read",
                )
                duplicate_effect = self._effect_from_row(duplicate_effect_row)
                self._commit(
                    connection,
                    deadline_monotonic=deadline_monotonic,
                    operation="complete_effect_duplicate_commit",
                )
                return {
                    "accepted": bool(duplicate["accepted"]),
                    "duplicate": True,
                    "effect": duplicate_effect,
                }
            row = connection.execute(
                "SELECT * FROM effects WHERE effect_id = ?",
                (effect_id,),
            ).fetchone()
            # Lease liveness is evaluated only after this transaction owns the
            # SQLite writer lock.  A bounded busy wait must never admit a lease
            # that expired while BEGIN IMMEDIATE was waiting.
            completion_time = float(now if now is not None else time.time())
            self._remaining_deadline_seconds(
                deadline_monotonic,
                operation="complete_effect_lease_read",
            )
            if row is None:
                connection.rollback()
                raise WorkflowConflict(f"unknown effect: {effect_id}")
            current_lease = bool(
                row["status"] == "running"
                and int(row["lease_epoch"]) == int(lease_epoch)
            )
            live_lease = bool(
                row["lease_until"] is not None
                and float(row["lease_until"]) > completion_time
            )
            accepted = bool(
                current_lease
                and (not require_live_lease or live_lease)
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
                    completion_time,
                ),
            )
            self._remaining_deadline_seconds(
                deadline_monotonic,
                operation="complete_effect_inbox_insert",
            )
            if not accepted:
                rejected_effect = self._effect_from_row(row)
                self._commit(
                    connection,
                    deadline_monotonic=deadline_monotonic,
                    operation="complete_effect_rejection_commit",
                )
                return {
                    "accepted": False,
                    "duplicate": False,
                    "reason": (
                        "expired_lease"
                        if current_lease and require_live_lease and not live_lease
                        else "stale_lease_epoch"
                    ),
                    "effect": rejected_effect,
                }
            recorded_followups: list[WorkflowEvent] = []
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
                self._remaining_deadline_seconds(
                    deadline_monotonic,
                    operation="complete_effect_event_append",
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
                    recorded_followups.append(self._append_event_locked(
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
                    ))
                    self._remaining_deadline_seconds(
                        deadline_monotonic,
                        operation="complete_effect_followup_append",
                    )
                connection.execute(
                    """
                    UPDATE effects
                    SET status = 'completed', result_payload = ?,
                        result_digest = ?, lease_owner = NULL,
                        lease_until = NULL, updated_at = ?
                    WHERE effect_id = ?
                    """,
                    (encoded, digest, completion_time, effect_id),
                )
                self._remaining_deadline_seconds(
                    deadline_monotonic,
                    operation="complete_effect_update",
                )
                completed_effect_row = connection.execute(
                    "SELECT * FROM effects WHERE effect_id = ?",
                    (effect_id,),
                ).fetchone()
                completed_effect = self._effect_from_row(completed_effect_row)
                self._remaining_deadline_seconds(
                    deadline_monotonic,
                    operation="complete_effect_snapshot",
                )
            except Exception:
                connection.rollback()
                raise
            self._commit(
                connection,
                deadline_monotonic=deadline_monotonic,
                operation="complete_effect_commit",
            )
            return {
                "accepted": True,
                "duplicate": False,
                "effect": completed_effect,
                "followup_events": tuple(recorded_followups),
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
