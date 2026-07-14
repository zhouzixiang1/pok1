"""Durable heartbeat and append-only event chain for long Route-B runs."""

from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .identity import canonical_json_bytes, payload_sha256, require_sha256
from .strict_io import (
    atomic_json_create,
    atomic_json_write,
    atomic_write_bytes,
    load_hashed_json,
    read_regular_bytes,
    stable_tree_manifest,
    strict_json_loads,
    validate_real_directory,
)


EVENT_SCHEMA = "route-b-durable-run-event-v1"
HEARTBEAT_SCHEMA = "route-b-durable-run-heartbeat-v1"
EVENT_LOG_NAME = "events"
EVENT_EXPORT_NAME = "events.jsonl"
HEARTBEAT_NAME = "heartbeat.json"
HEARTBEAT_STATUSES = frozenset(
    {"started", "running", "cancelled", "completed", "failed"}
)
RUN_IDENTITY_KEYS = {
    "run_contract_sha256",
    "source_snapshot_sha256",
    "config_payload_sha256",
    "config_file_sha256",
}


def utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _validate_utc(value: Any, context: str) -> str:
    if type(value) is not str or not value.endswith("Z"):
        raise ValueError(f"{context} must be an exact UTC timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{context} is not ISO-8601") from exc
    if parsed.tzinfo != dt.timezone.utc:
        raise ValueError(f"{context} is not UTC")
    return value


def process_identity() -> dict[str, Any]:
    """Bind PID to Linux start ticks and its UTC process start instant."""

    pid = os.getpid()
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    closing = raw.rfind(")")
    if closing < 0:
        raise RuntimeError("cannot parse /proc process identity")
    fields_after_comm = raw[closing + 2 :].split()
    # fields_after_comm[0] is proc field 3; starttime is proc field 22.
    start_ticks = int(fields_after_comm[19])
    boot_seconds: int | None = None
    for line in Path("/proc/stat").read_text(encoding="ascii").splitlines():
        if line.startswith("btime "):
            boot_seconds = int(line.split()[1])
            break
    if boot_seconds is None:
        raise RuntimeError("/proc/stat has no boot time")
    clock_ticks = os.sysconf("SC_CLK_TCK")
    started = dt.datetime.fromtimestamp(
        boot_seconds + start_ticks / clock_ticks,
        tz=dt.timezone.utc,
    )
    return {
        "pid": pid,
        "linux_start_ticks": start_ticks,
        "started_at_utc": started.isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        ),
    }


def _validate_process(value: Any) -> dict[str, Any]:
    if type(value) is not dict or set(value) != {
        "pid",
        "linux_start_ticks",
        "started_at_utc",
    }:
        raise ValueError("journal process identity differs from strict schema")
    if type(value["pid"]) is not int or value["pid"] <= 0:
        raise ValueError("journal PID must be a positive exact integer")
    if type(value["linux_start_ticks"]) is not int or value["linux_start_ticks"] < 0:
        raise ValueError("journal process start ticks must be nonnegative exact integer")
    _validate_utc(value["started_at_utc"], "journal process start")
    return value


def _validate_run_identity(value: Any) -> dict[str, str]:
    if type(value) is not dict or set(value) != RUN_IDENTITY_KEYS:
        raise ValueError("journal run identity differs from strict schema")
    result: dict[str, str] = {}
    for name in sorted(RUN_IDENTITY_KEYS):
        result[name] = require_sha256(value[name], f"journal {name}")
    return result


def _checkpoint_digest(value: Any) -> str | None:
    if value is None:
        return None
    return require_sha256(value, "journal checkpoint digest")


def _validate_event(
    event: Any,
    *,
    expected_sequence: int,
    previous_sha256: str | None,
    run_identity: Mapping[str, str],
) -> dict[str, Any]:
    expected_keys = {
        "schema",
        "sequence",
        "timestamp_utc",
        "event",
        "process",
        "completed_batches",
        "checkpoint_sha256",
        "run_identity",
        "details",
        "previous_event_sha256",
        "event_sha256",
    }
    if type(event) is not dict or set(event) != expected_keys:
        raise ValueError("journal event differs from strict schema")
    if event["schema"] != EVENT_SCHEMA:
        raise ValueError("unsupported journal event schema")
    if type(event["sequence"]) is not int or event["sequence"] != expected_sequence:
        raise ValueError("journal event sequence is not exact and contiguous")
    _validate_utc(event["timestamp_utc"], "journal event timestamp")
    if type(event["event"]) is not str or not event["event"]:
        raise ValueError("journal event name must be a nonempty exact string")
    _validate_process(event["process"])
    if type(event["completed_batches"]) is not int or event["completed_batches"] < 0:
        raise ValueError("journal completed_batches must be nonnegative exact integer")
    _checkpoint_digest(event["checkpoint_sha256"])
    if _validate_run_identity(event["run_identity"]) != dict(run_identity):
        raise ValueError("journal event belongs to a different run identity")
    if type(event["details"]) is not dict:
        raise TypeError("journal event details must be an exact object")
    if event["previous_event_sha256"] != previous_sha256:
        raise ValueError("journal previous-event chain digest mismatch")
    unsigned = dict(event)
    stored_sha256 = unsigned.pop("event_sha256")
    require_sha256(stored_sha256, "journal event digest")
    if payload_sha256(unsigned) != stored_sha256:
        raise ValueError("journal event self digest mismatch")
    return event


_EVENT_FILE = re.compile(r"^[0-9]{12}\.json$")
_EVENT_TEMP = re.compile(r"^\.[0-9]{12}\.json\.[0-9a-f]{32}\.tmp$")


def _load_event_log_with_orphans(
    path: str | Path,
    *,
    root: str | Path,
    run_identity: Mapping[str, str],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    event_root = validate_real_directory(path)
    workspace = validate_real_directory(root)
    try:
        event_root.relative_to(workspace)
    except ValueError as exc:
        raise ValueError("event directory escapes its trusted workspace") from exc
    manifest = stable_tree_manifest(event_root)
    authoritative = sorted(name for name in manifest if _EVENT_FILE.fullmatch(name))
    orphans = {
        name: digest
        for name, digest in sorted(manifest.items())
        if _EVENT_TEMP.fullmatch(name)
    }
    unknown = set(manifest) - set(authoritative) - set(orphans)
    if unknown:
        raise ValueError(f"event directory contains unknown entries: {sorted(unknown)!r}")
    expected_names = [f"{sequence:012d}.json" for sequence in range(len(authoritative))]
    if authoritative != expected_names:
        raise ValueError("authoritative event files are not one contiguous prefix")
    events: list[dict[str, Any]] = []
    previous: str | None = None
    previous_batches = -1
    for sequence, name in enumerate(authoritative):
        event = load_hashed_json(event_root / name, root=workspace)
        validated = _validate_event(
            event,
            expected_sequence=sequence,
            previous_sha256=previous,
            run_identity=run_identity,
        )
        if validated["completed_batches"] < previous_batches:
            raise ValueError("journal durable batch index moved backwards")
        events.append(validated)
        previous = validated["event_sha256"]
        previous_batches = validated["completed_batches"]
    return events, orphans


def load_event_log(
    path: str | Path,
    *,
    root: str | Path,
    run_identity: Mapping[str, str],
) -> list[dict[str, Any]]:
    events, _orphans = _load_event_log_with_orphans(
        path,
        root=root,
        run_identity=run_identity,
    )
    return events


def load_event_export(
    path: str | Path,
    *,
    root: str | Path,
    run_identity: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Validate the atomically materialized JSONL view of authoritative events."""

    raw = read_regular_bytes(path, root=root)
    if not raw or not raw.endswith(b"\n"):
        raise ValueError("event JSONL export is empty or has a partial final record")
    events: list[dict[str, Any]] = []
    previous: str | None = None
    previous_batches = -1
    for sequence, line in enumerate(raw.splitlines()):
        event = strict_json_loads(line, context="event JSONL export line")
        validated = _validate_event(
            event,
            expected_sequence=sequence,
            previous_sha256=previous,
            run_identity=run_identity,
        )
        if validated["completed_batches"] < previous_batches:
            raise ValueError("event export durable batch index moved backwards")
        events.append(validated)
        previous = validated["event_sha256"]
        previous_batches = validated["completed_batches"]
    return events


@dataclass(slots=True)
class DurableRunJournal:
    workspace: Path
    run_identity: dict[str, str]
    process: dict[str, Any]
    events: list[dict[str, Any]]
    previous_heartbeat: Mapping[str, Any] | None
    orphaned_event_temps: dict[str, str]

    @classmethod
    def open(
        cls,
        workspace: str | Path,
        run_identity: Mapping[str, str],
        *,
        resume: bool,
    ) -> "DurableRunJournal":
        if type(resume) is not bool:
            raise TypeError("journal resume must be an exact boolean")
        root = validate_real_directory(workspace)
        identity = _validate_run_identity(dict(run_identity))
        event_path = root / EVENT_LOG_NAME
        export_path = root / EVENT_EXPORT_NAME
        heartbeat_path = root / HEARTBEAT_NAME
        if resume:
            events, orphans = _load_event_log_with_orphans(
                event_path,
                root=root,
                run_identity=identity,
            )
            if heartbeat_path.is_file() and not heartbeat_path.is_symlink():
                heartbeat = load_hashed_json(heartbeat_path, root=root)
                cls._validate_heartbeat_payload(heartbeat, identity, events)
            elif heartbeat_path.exists() or heartbeat_path.is_symlink():
                raise ValueError("resume heartbeat is not a real regular file")
            else:
                heartbeat = None
        else:
            if event_path.exists() or export_path.exists() or heartbeat_path.exists():
                raise ValueError("new journal requires absent heartbeat/event outputs")
            events = []
            heartbeat = None
            orphans = {}
        return cls(root, identity, process_identity(), events, heartbeat, orphans)

    @staticmethod
    def _validate_heartbeat_payload(
        payload: Mapping[str, Any],
        run_identity: Mapping[str, str],
        events: list[dict[str, Any]],
    ) -> None:
        if type(payload) is not dict or set(payload) != {
            "schema",
            "status",
            "detail",
            "updated_at_utc",
            "process",
            "completed_batches",
            "checkpoint_sha256",
            "run_identity",
            "last_event_sequence",
            "last_event_sha256",
        }:
            raise ValueError("heartbeat differs from strict schema")
        if payload["schema"] != HEARTBEAT_SCHEMA or payload["status"] not in HEARTBEAT_STATUSES:
            raise ValueError("heartbeat schema/status is invalid")
        if type(payload["detail"]) is not str or not payload["detail"]:
            raise ValueError("heartbeat detail must be a nonempty exact string")
        _validate_utc(payload["updated_at_utc"], "heartbeat timestamp")
        _validate_process(payload["process"])
        if type(payload["completed_batches"]) is not int or payload["completed_batches"] < 0:
            raise ValueError("heartbeat completed_batches must be nonnegative exact integer")
        _checkpoint_digest(payload["checkpoint_sha256"])
        if _validate_run_identity(payload["run_identity"]) != dict(run_identity):
            raise ValueError("heartbeat belongs to a different run identity")
        sequence = payload["last_event_sequence"]
        digest = payload["last_event_sha256"]
        if type(sequence) is not int or not 0 <= sequence < len(events):
            raise ValueError("heartbeat event sequence is absent from durable log")
        if digest != events[sequence]["event_sha256"]:
            raise ValueError("heartbeat event digest is absent from durable log")
        referenced = events[sequence]
        if (
            payload["completed_batches"] != referenced["completed_batches"]
            or payload["checkpoint_sha256"] != referenced["checkpoint_sha256"]
        ):
            raise ValueError("heartbeat durable pair differs from its authoritative event")

    @property
    def event_path(self) -> Path:
        return self.workspace / EVENT_LOG_NAME

    @property
    def event_export_path(self) -> Path:
        return self.workspace / EVENT_EXPORT_NAME

    @property
    def heartbeat_path(self) -> Path:
        return self.workspace / HEARTBEAT_NAME

    @property
    def last_event(self) -> dict[str, Any] | None:
        return self.events[-1] if self.events else None

    def append(
        self,
        event: str,
        *,
        completed_batches: int,
        checkpoint_sha256: str | None,
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if type(event) is not str or not event:
            raise ValueError("journal event name must be nonempty exact string")
        if type(completed_batches) is not int or completed_batches < 0:
            raise ValueError("journal completed_batches must be nonnegative exact integer")
        _checkpoint_digest(checkpoint_sha256)
        detail_payload = {} if details is None else dict(details)
        if details is not None and type(details) is not dict:
            raise TypeError("journal event details must be an exact object")
        unsigned = {
            "schema": EVENT_SCHEMA,
            "sequence": len(self.events),
            "timestamp_utc": utc_now(),
            "event": event,
            "process": dict(self.process),
            "completed_batches": completed_batches,
            "checkpoint_sha256": checkpoint_sha256,
            "run_identity": dict(self.run_identity),
            "details": detail_payload,
            "previous_event_sha256": (
                None if not self.events else self.events[-1]["event_sha256"]
            ),
        }
        payload = {**unsigned, "event_sha256": payload_sha256(unsigned)}
        event_file = self.event_path / f"{len(self.events):012d}.json"
        atomic_json_create(
            event_file,
            payload,
            root=self.workspace,
        )
        if load_hashed_json(event_file, root=self.workspace) != payload:
            raise RuntimeError("authoritative event failed atomic readback")
        _validate_event(
            payload,
            expected_sequence=len(self.events),
            previous_sha256=unsigned["previous_event_sha256"],
            run_identity=self.run_identity,
        )
        self.events.append(payload)
        atomic_write_bytes(
            self.event_export_path,
            b"".join(canonical_json_bytes(item) + b"\n" for item in self.events),
            root=self.workspace,
        )
        return payload

    def heartbeat(
        self,
        status: str,
        *,
        detail: str,
        completed_batches: int,
        checkpoint_sha256: str | None,
    ) -> Mapping[str, Any]:
        if status not in HEARTBEAT_STATUSES:
            raise ValueError("unknown heartbeat status")
        if type(detail) is not str or not detail:
            raise ValueError("heartbeat detail must be a nonempty exact string")
        if not self.events:
            raise ValueError("heartbeat requires at least one durable event")
        payload = {
            "schema": HEARTBEAT_SCHEMA,
            "status": status,
            "detail": detail,
            "updated_at_utc": utc_now(),
            "process": dict(self.process),
            "completed_batches": completed_batches,
            "checkpoint_sha256": checkpoint_sha256,
            "run_identity": dict(self.run_identity),
            "last_event_sequence": self.events[-1]["sequence"],
            "last_event_sha256": self.events[-1]["event_sha256"],
        }
        atomic_json_write(self.heartbeat_path, payload, root=self.workspace)
        loaded = load_hashed_json(self.heartbeat_path, root=self.workspace)
        self._validate_heartbeat_payload(loaded, self.run_identity, self.events)
        self.previous_heartbeat = loaded
        return loaded


def validate_heartbeat_payload(
    payload: Mapping[str, Any],
    run_identity: Mapping[str, str],
    events: list[dict[str, Any]],
) -> None:
    """Public strict validator for published heartbeat/event evidence."""

    DurableRunJournal._validate_heartbeat_payload(
        payload,
        _validate_run_identity(dict(run_identity)),
        events,
    )
