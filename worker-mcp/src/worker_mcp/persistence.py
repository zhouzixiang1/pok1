"""SQLite-WAL task state, history, leases, idempotency, and results."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator
import uuid

from .schemas import TaskEnvelope, TaskStatus, TaskType
from .state_machine import is_terminal, validate_transition


SCHEMA_VERSION = 1


class PersistenceError(RuntimeError):
    pass


class TaskNotFound(PersistenceError):
    pass


class IdempotencyConflict(PersistenceError):
    pass


class ClaimConflict(PersistenceError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Persistence:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._initialize()
        self.path.chmod(0o600)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=30000")
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in {0, SCHEMA_VERSION}:
                raise PersistenceError(
                    f"unsupported database schema {version}; expected {SCHEMA_VERSION}"
                )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_fingerprint TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    base_commit TEXT NOT NULL,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    progress_summary TEXT NOT NULL,
                    worktree_path TEXT,
                    result_json TEXT,
                    error_class TEXT,
                    error_message TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    lease_owner TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_transitions (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_status_updated
                    ON tasks(status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_repo_updated
                    ON tasks(repository, updated_at);
                CREATE INDEX IF NOT EXISTS idx_transitions_task_seq
                    ON task_transitions(task_id, seq);
                """
            )
            if version == 0:
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            raise TaskNotFound("task not found")
        return dict(row)

    def ping(self) -> None:
        with self._connect() as connection:
            if connection.execute("SELECT 1").fetchone()[0] != 1:
                raise PersistenceError("database ping failed")

    def create_or_get(
        self,
        request: TaskEnvelope,
        fingerprint: str,
    ) -> tuple[dict[str, Any], bool]:
        request_json = request.model_dump_json()
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM tasks WHERE idempotency_key = ?",
                (request.idempotency_key,),
            ).fetchone()
            if existing is not None:
                row = dict(existing)
                if row["request_fingerprint"] != fingerprint:
                    connection.rollback()
                    raise IdempotencyConflict(
                        "idempotency_key was already used with a different request"
                    )
                connection.commit()
                return row, True

            task_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO tasks(
                    task_id, idempotency_key, request_fingerprint, request_json,
                    task_type, repository, base_commit, status, phase, attempt,
                    progress_summary, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    task_id,
                    request.idempotency_key,
                    fingerprint,
                    request_json,
                    request.task_type.value,
                    request.repo,
                    request.base_commit,
                    TaskStatus.ACCEPTED.value,
                    "accepted",
                    "Task accepted and durably recorded",
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO task_transitions(
                    task_id, from_status, to_status, phase, reason, created_at
                ) VALUES (?, NULL, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    TaskStatus.ACCEPTED.value,
                    "accepted",
                    "submit persisted",
                    now,
                ),
            )
            connection.commit()
        return self.get_task(task_id), False

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            return self._row(
                connection.execute(
                    "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
            )

    def request_for(self, task_id: str) -> TaskEnvelope:
        return TaskEnvelope.model_validate_json(self.get_task(task_id)["request_json"])

    def transition(
        self,
        task_id: str,
        target: TaskStatus,
        *,
        phase: str,
        reason: str,
        progress_summary: str,
        recovery: bool = False,
        expected: set[TaskStatus] | None = None,
        clear_lease: bool = False,
        error_class: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            current_row = self._row(row)
            current = TaskStatus(current_row["status"])
            if current is target:
                connection.commit()
                return current_row
            if expected is not None and current not in expected:
                connection.rollback()
                raise ClaimConflict(
                    f"task {task_id} is {current.value}, expected one of "
                    f"{sorted(item.value for item in expected)}"
                )
            validate_transition(current, target, recovery=recovery)
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, phase = ?, progress_summary = ?, updated_at = ?,
                    lease_owner = CASE WHEN ? THEN NULL ELSE lease_owner END,
                    error_class = ?, error_message = ?
                WHERE task_id = ?
                """,
                (
                    target.value,
                    phase,
                    progress_summary,
                    now,
                    int(clear_lease),
                    error_class,
                    (error_message or "")[:4000] or None,
                    task_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO task_transitions(
                    task_id, from_status, to_status, phase, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (task_id, current.value, target.value, phase, reason[:4000], now),
            )
            connection.commit()
        return self.get_task(task_id)

    def claim_task(self, task_id: str, owner: str) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            current_row = self._row(row)
            if current_row["status"] != TaskStatus.QUEUED.value:
                connection.rollback()
                raise ClaimConflict(
                    f"task {task_id} cannot be claimed from {current_row['status']}"
                )
            if current_row["lease_owner"]:
                connection.rollback()
                raise ClaimConflict(f"task {task_id} already has an active executor")
            connection.execute(
                """
                UPDATE tasks
                SET status = ?, phase = ?, progress_summary = ?,
                    attempt = attempt + 1, lease_owner = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (
                    TaskStatus.PREPARING.value,
                    "preparing",
                    "Executor claimed task and is preparing isolation",
                    owner,
                    now,
                    task_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO task_transitions(
                    task_id, from_status, to_status, phase, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    TaskStatus.QUEUED.value,
                    TaskStatus.PREPARING.value,
                    "preparing",
                    f"claimed by executor {owner}",
                    now,
                ),
            )
            connection.commit()
        return self.get_task(task_id)

    def update_worktree(self, task_id: str, path: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE tasks SET worktree_path = ?, updated_at = ? WHERE task_id = ?",
                (path, utc_now(), task_id),
            )

    def save_result(self, task_id: str, result_json: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE tasks SET result_json = ?, updated_at = ? WHERE task_id = ?",
                (result_json, utc_now(), task_id),
            )
            if cursor.rowcount != 1:
                raise TaskNotFound(task_id)

    def cancel_or_request(
        self,
        task_id: str,
        *,
        pre_execution_result_json: str,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically cancel an unclaimed task or flag an active executor.

        The returned boolean is true only when this transaction performed the
        terminal transition.  Keeping the status read, cancellation request,
        and queued-task transition under one ``BEGIN IMMEDIATE`` prevents an
        executor claim from racing a stale pre-cancel status read.
        """

        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current_row = self._row(
                connection.execute(
                    "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
            )
            current = TaskStatus(current_row["status"])
            if is_terminal(current):
                connection.commit()
                return current_row, False
            if current in {TaskStatus.ACCEPTED, TaskStatus.QUEUED}:
                validate_transition(current, TaskStatus.CANCELLED)
                summary = "Task was cancelled before execution"
                connection.execute(
                    """
                    UPDATE tasks
                    SET status = ?, phase = ?, progress_summary = ?,
                        result_json = ?, cancel_requested = 1,
                        lease_owner = NULL, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (
                        TaskStatus.CANCELLED.value,
                        "cancelled",
                        summary,
                        pre_execution_result_json,
                        now,
                        task_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO task_transitions(
                        task_id, from_status, to_status, phase, reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        current.value,
                        TaskStatus.CANCELLED.value,
                        "cancelled",
                        "cancel requested before executor claim",
                        now,
                    ),
                )
                terminal = True
            else:
                connection.execute(
                    """
                    UPDATE tasks
                    SET cancel_requested = 1, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (now, task_id),
                )
                terminal = False
            updated = self._row(
                connection.execute(
                    "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
                ).fetchone()
            )
            connection.commit()
        return updated, terminal

    def incomplete_tasks(self) -> list[dict[str, Any]]:
        terminal = tuple(status.value for status in TaskStatus if is_terminal(status))
        placeholders = ",".join("?" for _ in terminal)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM tasks WHERE status NOT IN ({placeholders}) ORDER BY created_at",
                terminal,
            ).fetchall()
        return [dict(row) for row in rows]

    def list_tasks(
        self,
        *,
        status: TaskStatus | None = None,
        repo: str | None = None,
        task_type: TaskType | None = None,
        since: datetime | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if repo is not None:
            clauses.append("repository = ?")
            params.append(repo)
        if task_type is not None:
            clauses.append("task_type = ?")
            params.append(task_type.value)
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since.astimezone(UTC).isoformat())
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM tasks{where} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def transitions(self, task_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM task_transitions WHERE task_id = ? ORDER BY seq",
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]
