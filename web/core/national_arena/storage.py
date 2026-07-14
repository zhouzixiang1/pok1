"""Crash-safe file storage for Arena metadata, events, logs, and THP files."""

from __future__ import annotations

import json
import os
import fcntl
from contextlib import contextmanager
from pathlib import Path
import re
from typing import Any
import uuid

from national_arena.models import ArenaSession


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ARENA_ROOT = ROOT / "web" / "core" / "results" / "national_arena"
SESSION_ID_RE = re.compile(r"^arena_[0-9A-Za-z_-]{8,80}$")
ARTIFACT_NAME_RE = re.compile(r"^[0-9A-Za-z_.-]{1,120}$")


class ArenaStore:
    def __init__(self, root: str | Path = DEFAULT_ARENA_ROOT) -> None:
        self.root = Path(root)
        self._owner_handle = None

    def ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def acquire_owner(self) -> None:
        self.ensure_root()
        if self._owner_handle is not None:
            return
        handle = (self.root / ".owner.lock").open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise RuntimeError("national Arena already has a process owner")
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        self._owner_handle = handle

    def release_owner(self) -> None:
        handle, self._owner_handle = self._owner_handle, None
        if handle is None:
            return
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()

    @contextmanager
    def _session_lock(self, session_id: str, *, shared: bool = False):
        directory = self.session_dir(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / ".lock").open("a+b") as handle:
            fcntl.flock(
                handle.fileno(),
                fcntl.LOCK_SH if shared else fcntl.LOCK_EX,
            )
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def session_dir(self, session_id: str) -> Path:
        if not SESSION_ID_RE.fullmatch(session_id):
            raise ValueError("invalid arena session id")
        return self.root / session_id

    def create_session(self, session: ArenaSession) -> None:
        directory = self.session_dir(session.session_id)
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "artifacts").mkdir()
        self.write_session(session)

    def write_session(self, session: ArenaSession) -> None:
        directory = self.session_dir(session.session_id)
        directory.mkdir(parents=True, exist_ok=True)
        with self._session_lock(session.session_id):
            target = directory / "session.json"
            temporary = target.with_name(
                f"session.json.tmp.{os.getpid()}.{uuid.uuid4().hex}"
            )
            payload = json.dumps(
                session.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ) + "\n"
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)

    def load_session(self, session_id: str) -> ArenaSession:
        path = self.session_dir(session_id) / "session.json"
        with self._session_lock(session_id, shared=True):
            payload = json.loads(path.read_text(encoding="utf-8"))
        return ArenaSession.from_dict(payload)

    def _load_session_for_discovery(self, session_id: str) -> ArenaSession:
        """Read metadata without creating a directory or per-session lock.

        ``list_sessions`` runs only while the process owns the exclusive Arena
        root lease.  It must inspect an old session's epoch binding before any
        per-session mutation (including creation of ``.lock``) occurs.
        """

        path = self.session_dir(session_id) / "session.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        return ArenaSession.from_dict(payload)

    def list_sessions(self) -> list[ArenaSession]:
        if not self.root.is_dir():
            return []
        rows: list[ArenaSession] = []
        for path in self.root.iterdir():
            if not path.is_dir() or not SESSION_ID_RE.fullmatch(path.name):
                continue
            try:
                rows.append(self._load_session_for_discovery(path.name))
            except (OSError, ValueError, json.JSONDecodeError, TypeError):
                continue
        return sorted(rows, key=lambda item: item.created_at, reverse=True)

    def append_event(self, session_id: str, event: dict[str, Any]) -> None:
        path = self.session_dir(session_id) / "events.jsonl"
        line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
        with self._session_lock(session_id):
            descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                self._write_all(descriptor, line.encode("utf-8"))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def append_event_and_session(
        self,
        session: ArenaSession,
        event: dict[str, Any],
    ) -> None:
        """Persist the event first, then its materialized session snapshot.

        Recovery uses the event high-water mark if the process exits between
        these writes, so the journal remains the ordering authority.
        """
        self.append_event(session.session_id, event)
        self.write_session(session)

    def append_wire_batch(self, session_id: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        path = self.session_dir(session_id) / "artifacts" / "wire.jsonl"
        payload = "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            for row in rows
        ).encode("utf-8")
        with self._session_lock(session_id):
            descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                self._write_all(descriptor, payload)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    @staticmethod
    def _write_all(descriptor: int, payload: bytes) -> None:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while appending Arena journal")
            view = view[written:]

    def event_high_watermark(self, session_id: str) -> int:
        path = self.session_dir(session_id) / "events.jsonl"
        if not path.is_file():
            return 0
        high = 0
        with self._session_lock(session_id, shared=True):
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines:
            try:
                row = json.loads(line)
                high = max(high, int(row.get("event_id", 0) or 0))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        return high

    def read_events(
        self,
        session_id: str,
        *,
        after_event_id: int = 0,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        path = self.session_dir(session_id) / "events.jsonl"
        if not path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        with self._session_lock(session_id, shared=True):
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if int(row.get("event_id", 0) or 0) <= int(after_event_id):
                continue
            rows.append(row)
            if len(rows) >= max(1, min(int(limit), 5000)):
                break
        return rows

    def read_wire(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        path = self.session_dir(session_id) / "artifacts" / "wire.jsonl"
        if not path.is_file():
            return []
        with self._session_lock(session_id, shared=True):
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        rows: list[dict[str, Any]] = []
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if int(row.get("sequence", 0) or 0) <= int(after_sequence):
                continue
            rows.append(row)
            if len(rows) >= max(1, min(int(limit), 5000)):
                break
        return rows

    def artifact_path(
        self,
        session_id: str,
        name: str,
        *,
        create_parent: bool = False,
    ) -> Path:
        if not ARTIFACT_NAME_RE.fullmatch(name):
            raise ValueError("invalid arena artifact name")
        directory = self.session_dir(session_id) / "artifacts"
        if create_parent:
            directory.mkdir(parents=True, exist_ok=True)
        return directory / name
