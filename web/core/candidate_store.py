"""Candidate ledger and query store for the evolution pipeline.

JSONL remains the append-only audit log. SQLite is the queryable entity layer
used by selection, UI, and postmortem analysis. The public append/read helpers
stay backward compatible with the original JSONL-only implementation.

Bounded retention (quality window): ``compact_candidate_ledger`` keeps full
rows for the most recent ``keep_versions`` (default 50) distinct versions and
compacts older ``quality_finished`` rows — the ~1.8 MB-per-row byte mass —
into sha256-referenced summary rows. Every compaction step (JSONL rewrite,
SQLite event DELETE + VACUUM, receipt append) runs while holding the exact
``_locked_state_sidecar`` exclusive lock ``append_locked_jsonl`` uses, so a
concurrent appender can never interleave with the read-rewrite window (the
lockless read→tmp→os.replace trim in ``disk_hygiene._trim_jsonl`` is
forbidden here precisely because it can drop lines raced by an append).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import time
import uuid
from pathlib import Path
from typing import Any

import evolution_infra
from bot_namespace import (
    ARCHIVED_VERSION_HIGH_WATER,
    FIRST_STRICT_POLICY_VERSION,
    bot_name,
)
from evolution_infra import append_locked_jsonl, locked_file
from pipeline_schema import ArtifactRef, CandidateRecord, GateResult, ScoreCard


_IMPORT_RESULTS_DIR = evolution_infra.RESULTS_DIR
_IMPORT_CANDIDATE_EVENTS_FILE = _IMPORT_RESULTS_DIR / "candidates.jsonl"

CANDIDATE_EVENTS_FILE = _IMPORT_CANDIDATE_EVENTS_FILE
CANDIDATE_DB_FILE = _IMPORT_RESULTS_DIR / "candidates.sqlite3"


SCHEMA_VERSION = 2


# --- Bounded retention window (quality rows) -------------------------------
#
# ``candidates.jsonl`` grew to ~490 MB with ``candidates.sqlite3`` at ~567 MB
# because every ``quality_finished`` event embeds the full gate/scorecard
# payload (~1.8 MB per row). The retention window keeps the most recent
# ``DEFAULT_KEEP_VERSIONS`` distinct versions byte-exact and compacts older
# ``quality_finished`` rows into summary rows that reference the original row
# by sha256. Compaction shares the append sidecar lock and writes a receipt.

DEFAULT_KEEP_VERSIONS = 50
QUALITY_FINISHED_EVENT = "quality_finished"
QUALITY_SUMMARY_RETENTION = "quality_finished_summary"
COMPACTION_RECEIPTS_FILE = "candidates_compaction_receipt.jsonl"


_CANDIDATE_COLUMNS: tuple[str, ...] = (
    "candidate_id",
    "version",
    "source_v",
    "event_source",
    "profile_id",
    "workflow_profile_id",
    "prompt_profile_id",
    "model_id",
    "run_id",
    "parent_ids_json",
    "skill_layers_json",
    "changed_files_json",
    "diff_hash",
    "latest_stage",
    "latest_event_type",
    "latest_gate",
    "latest_status",
    "latest_scorecard_json",
    "latest_metrics_json",
    "latest_failures_json",
    "failure_class",
    "artifacts_json",
    "artifact_refs_json",
    "created_at",
    "updated_at",
)


_CANDIDATE_DEFAULTS: dict[str, str] = {
    "event_source": "'runtime'",
    "profile_id": "'default'",
    "workflow_profile_id": "''",
    "prompt_profile_id": "''",
    "model_id": "''",
    "run_id": "''",
    "parent_ids_json": "'[]'",
    "skill_layers_json": "'[]'",
    "changed_files_json": "'[]'",
    "diff_hash": "''",
    "latest_stage": "''",
    "latest_event_type": "''",
    "latest_gate": "''",
    "latest_status": "''",
    "latest_scorecard_json": "'{}'",
    "latest_metrics_json": "'{}'",
    "latest_failures_json": "'[]'",
    "failure_class": "''",
    "artifacts_json": "'{}'",
    "artifact_refs_json": "'[]'",
    "created_at": "0",
    "updated_at": "0",
}


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return str(obj)


def _json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, default=_json_default)


def _json_loads(data: str | None, default: Any) -> Any:
    if not data:
        return default
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return default


def _db_path_for_events(path: Path | None = None) -> Path:
    """Return the SQLite path paired with a JSONL event path.

    Tests often monkeypatch only CANDIDATE_EVENTS_FILE. Deriving the DB path
    from that event path keeps writes isolated without every test needing to
    patch another module global.
    """
    event_path = _events_path(path)
    return event_path.with_suffix(".sqlite3")


def _events_path(path: Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    configured = Path(CANDIDATE_EVENTS_FILE)
    dynamic_default = evolution_infra.RESULTS_DIR / "candidates.jsonl"
    if configured == _IMPORT_CANDIDATE_EVENTS_FILE and dynamic_default != _IMPORT_CANDIDATE_EVENTS_FILE:
        return dynamic_default
    return configured


def _connect(path: Path | None = None) -> sqlite3.Connection:
    db_path = _db_path_for_events(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _init_db(conn)
    return conn


def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    _ensure_candidates_table(conn)
    _ensure_event_tables(conn)
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _candidate_pk_columns(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("PRAGMA table_info(candidates)").fetchall()
    return [
        row["name"]
        for row in sorted(rows, key=lambda r: r["pk"])
        if int(row["pk"] or 0) > 0
    ]


def _create_candidates_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS candidates (
            candidate_id TEXT NOT NULL,
            version INTEGER,
            source_v INTEGER,
            event_source TEXT NOT NULL DEFAULT 'runtime',
            profile_id TEXT NOT NULL DEFAULT 'default',
            workflow_profile_id TEXT NOT NULL DEFAULT '',
            prompt_profile_id TEXT NOT NULL DEFAULT '',
            model_id TEXT NOT NULL DEFAULT '',
            run_id TEXT NOT NULL DEFAULT '',
            parent_ids_json TEXT NOT NULL DEFAULT '[]',
            skill_layers_json TEXT NOT NULL DEFAULT '[]',
            changed_files_json TEXT NOT NULL DEFAULT '[]',
            diff_hash TEXT NOT NULL DEFAULT '',
            latest_stage TEXT NOT NULL DEFAULT '',
            latest_event_type TEXT NOT NULL DEFAULT '',
            latest_gate TEXT NOT NULL DEFAULT '',
            latest_status TEXT NOT NULL DEFAULT '',
            latest_scorecard_json TEXT NOT NULL DEFAULT '{}',
            latest_metrics_json TEXT NOT NULL DEFAULT '{}',
            latest_failures_json TEXT NOT NULL DEFAULT '[]',
            failure_class TEXT NOT NULL DEFAULT '',
            artifacts_json TEXT NOT NULL DEFAULT '{}',
            artifact_refs_json TEXT NOT NULL DEFAULT '[]',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (candidate_id, event_source)
        )
        """
    )


def _migrate_candidates_v1(conn: sqlite3.Connection) -> None:
    """Split the old candidate_id entity into per-event-source entities.

    The JSONL ledger has always stored event_source per event, but the first
    SQLite entity table used candidate_id as the sole primary key. A test or
    backfill event with the same candidate_id could therefore overwrite the
    runtime entity. The v2 schema promotes event_source into the entity key.
    """
    legacy_name = "candidates_v1_migration"
    conn.execute(f"DROP TABLE IF EXISTS {legacy_name}")
    conn.execute(f"ALTER TABLE candidates RENAME TO {legacy_name}")
    _create_candidates_table(conn)

    old_cols = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({legacy_name})").fetchall()
    }
    select_exprs = []
    for col in _CANDIDATE_COLUMNS:
        if col in old_cols:
            select_exprs.append(col)
        else:
            select_exprs.append(f"{_CANDIDATE_DEFAULTS.get(col, 'NULL')} AS {col}")
    order_expr = "updated_at" if "updated_at" in old_cols else "rowid"
    conn.execute(
        f"""
        INSERT OR REPLACE INTO candidates ({", ".join(_CANDIDATE_COLUMNS)})
        SELECT {", ".join(select_exprs)}
        FROM {legacy_name}
        ORDER BY {order_expr} ASC
        """
    )
    conn.execute(f"DROP TABLE {legacy_name}")


def _ensure_candidates_table(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "candidates"):
        _create_candidates_table(conn)
        return
    if _candidate_pk_columns(conn) != ["candidate_id", "event_source"]:
        _migrate_candidates_v1(conn)


def _ensure_event_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS candidate_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_source TEXT NOT NULL DEFAULT 'runtime',
            stage TEXT NOT NULL DEFAULT '',
            gate TEXT NOT NULL DEFAULT '',
            run_id TEXT NOT NULL DEFAULT '',
            stage_attempt INTEGER NOT NULL DEFAULT 0,
            version INTEGER,
            source_v INTEGER,
            payload_json TEXT NOT NULL,
            timestamp REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'other',
            path TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            sha256 TEXT NOT NULL DEFAULT '',
            size_bytes INTEGER,
            hidden INTEGER NOT NULL DEFAULT 0,
            timestamp REAL NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_candidate_events_candidate ON candidate_events(candidate_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_candidate_events_version ON candidate_events(version)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_candidate_events_source ON candidate_events(event_source)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_candidates_version ON candidates(version)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_candidates_source ON candidates(event_source)")


def candidate_observability_identity(
    version: int | None,
    source_v: int | None = None,
) -> dict[str, Any]:
    """Project candidate lineage without inventing a first-strict parent.

    v142 is only the numeric completion-tag high-water for the first strict
    label.  It is not an artifact parent of v143 and must not appear in the
    mutable observability ledger's ``parent_ids``.
    """

    if version is None:
        return {
            "candidate_id": f"candidate_unknown_{int(time.time())}",
            "parent_ids": [],
            "lineage_kind": "unknown",
            "source_artifact_inherited": False,
        }
    if (
        int(version) == int(FIRST_STRICT_POLICY_VERSION)
        and source_v is not None
        and int(source_v) == int(ARCHIVED_VERSION_HIGH_WATER)
    ):
        return {
            "candidate_id": (
                f"{bot_name(version)}_numeric_high_water_v{int(source_v)}"
            ),
            "parent_ids": [],
            "lineage_kind": "numeric_high_water_only",
            "numeric_high_water_version": int(source_v),
            "source_artifact_inherited": False,
        }
    if source_v is None:
        return {
            "candidate_id": bot_name(version),
            "parent_ids": [],
            "lineage_kind": "no_parent",
            "source_artifact_inherited": False,
        }
    return {
        "candidate_id": f"{bot_name(version)}_from_{bot_name(source_v)}",
        "parent_ids": [bot_name(source_v)],
        "lineage_kind": "strict_parent_artifact",
        "source_artifact_inherited": True,
    }


def make_candidate_id(version: int | None, source_v: int | None = None) -> str:
    return str(candidate_observability_identity(version, source_v)["candidate_id"])


def _candidate_event_identity(
    version: int | None,
    source_v: int | None,
    candidate_id: str | None,
    parent_ids: list[str] | None,
    metrics: dict[str, Any] | None,
) -> tuple[str, list[str], dict[str, Any]]:
    """Normalize numeric-only lineage even when a stale caller supplies it."""

    identity = candidate_observability_identity(version, source_v)
    numeric_only = identity.get("lineage_kind") == "numeric_high_water_only"
    resolved_candidate_id = (
        str(identity["candidate_id"])
        if numeric_only
        else str(candidate_id or identity["candidate_id"])
    )
    resolved_parent_ids = (
        []
        if numeric_only
        else list(parent_ids if parent_ids is not None else identity["parent_ids"])
    )
    resolved_metrics = dict(metrics or {})
    if numeric_only:
        resolved_metrics = {
            **resolved_metrics,
            "lineage_kind": identity["lineage_kind"],
            "numeric_high_water_version": identity["numeric_high_water_version"],
            "source_artifact_inherited": False,
        }
    return resolved_candidate_id, resolved_parent_ids, resolved_metrics


def _scorecard_payload(scorecard: ScoreCard | dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(scorecard, ScoreCard):
        return scorecard.model_dump()
    if isinstance(scorecard, dict):
        return scorecard
    return {}


def _gate_results_payload(gate_results: list[GateResult | dict[str, Any]] | None) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for gate in gate_results or []:
        if isinstance(gate, GateResult):
            payload.append(gate.model_dump())
        elif isinstance(gate, dict):
            payload.append(gate)
    return payload


def _artifact_refs_payload(artifact_refs: list[ArtifactRef | dict[str, Any]] | None) -> list[ArtifactRef]:
    refs: list[ArtifactRef] = []
    for ref in artifact_refs or []:
        if isinstance(ref, ArtifactRef):
            refs.append(ref)
        elif isinstance(ref, dict):
            refs.append(ArtifactRef(**ref))
    return refs


def _infer_latest_status(record: CandidateRecord) -> str:
    if record.scorecard:
        if record.scorecard.get("passed") is True:
            return "passed"
        if record.scorecard.get("passed") is False:
            return "failed"
    if record.failures:
        return "failed"
    if record.event_type.endswith("_finished") or record.event_type.endswith("_passed"):
        return "passed"
    if record.event_type.endswith("_failed"):
        return "failed"
    return ""


def _upsert_sqlite_record(
    record: CandidateRecord,
    *,
    path: Path | None = None,
    persisted_entry: dict[str, Any] | None = None,
) -> None:
    """Upsert the entity row and append the event row.

    ``persisted_entry`` (optional) is the payload already written to the JSONL
    ledger. When supplied it backs only ``candidate_events.payload_json``; the
    ``candidates`` entity columns (``latest_scorecard_json`` etc.) always
    project from the full ``record``.
    """
    entry = persisted_entry if persisted_entry is not None else record.model_dump()
    with _connect(path) as conn:
        now = float(record.timestamp)
        existing = conn.execute(
            "SELECT created_at FROM candidates WHERE candidate_id = ? AND event_source = ?",
            (record.candidate_id, record.event_source),
        ).fetchone()
        created_at = float(existing["created_at"]) if existing else now
        conn.execute(
            """
            INSERT INTO candidates (
                candidate_id, version, source_v, event_source, profile_id,
                workflow_profile_id, prompt_profile_id, model_id, run_id,
                parent_ids_json, skill_layers_json, changed_files_json, diff_hash,
                latest_stage, latest_event_type, latest_gate, latest_status,
                latest_scorecard_json, latest_metrics_json, latest_failures_json,
                failure_class, artifacts_json, artifact_refs_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(candidate_id, event_source) DO UPDATE SET
                version=excluded.version,
                source_v=excluded.source_v,
                profile_id=excluded.profile_id,
                workflow_profile_id=excluded.workflow_profile_id,
                prompt_profile_id=excluded.prompt_profile_id,
                model_id=excluded.model_id,
                run_id=excluded.run_id,
                parent_ids_json=excluded.parent_ids_json,
                skill_layers_json=excluded.skill_layers_json,
                changed_files_json=excluded.changed_files_json,
                diff_hash=excluded.diff_hash,
                latest_stage=excluded.latest_stage,
                latest_event_type=excluded.latest_event_type,
                latest_gate=excluded.latest_gate,
                latest_status=excluded.latest_status,
                latest_scorecard_json=excluded.latest_scorecard_json,
                latest_metrics_json=excluded.latest_metrics_json,
                latest_failures_json=excluded.latest_failures_json,
                failure_class=excluded.failure_class,
                artifacts_json=excluded.artifacts_json,
                artifact_refs_json=excluded.artifact_refs_json,
                updated_at=excluded.updated_at
            """,
            (
                record.candidate_id,
                record.version,
                record.source_v,
                record.event_source,
                record.profile_id,
                record.workflow_profile_id,
                record.prompt_profile_id,
                record.model_id,
                record.run_id,
                _json_dumps(record.parent_ids),
                _json_dumps(record.skill_layers),
                _json_dumps(record.changed_files),
                record.diff_hash,
                record.stage,
                record.event_type,
                record.gate,
                _infer_latest_status(record),
                _json_dumps(record.scorecard),
                _json_dumps(record.metrics),
                _json_dumps(record.failures),
                record.failure_class,
                _json_dumps(record.artifacts),
                _json_dumps([ref.model_dump() for ref in record.artifact_refs]),
                created_at,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO candidate_events (
                candidate_id, event_type, event_source, stage, gate, run_id,
                stage_attempt, version, source_v, payload_json, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.candidate_id,
                record.event_type,
                record.event_source,
                record.stage,
                record.gate,
                record.run_id,
                record.stage_attempt,
                record.version,
                record.source_v,
                _json_dumps(entry),
                record.timestamp,
            ),
        )
        for ref in record.artifact_refs:
            conn.execute(
                """
                INSERT INTO artifacts (
                    candidate_id, kind, path, label, sha256, size_bytes, hidden, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.candidate_id,
                    ref.kind,
                    ref.path,
                    ref.label,
                    ref.sha256,
                    ref.size_bytes,
                    1 if ref.hidden else 0,
                    record.timestamp,
                ),
            )
        conn.commit()


def append_candidate_event(
    event_type: str,
    *,
    event_source: str = "runtime",
    version: int | None = None,
    source_v: int | None = None,
    candidate_id: str | None = None,
    profile_id: str = "default",
    workflow_profile_id: str = "",
    prompt_profile_id: str = "",
    model_id: str = "",
    run_id: str = "",
    stage_attempt: int = 0,
    stage: str = "",
    parent_ids: list[str] | None = None,
    changed_files: list[str] | None = None,
    skill_layers: list[str] | None = None,
    diff_hash: str = "",
    gate: str = "",
    scorecard: ScoreCard | dict[str, Any] | None = None,
    gate_results: list[GateResult | dict[str, Any]] | None = None,
    metrics: dict[str, Any] | None = None,
    failures: list[str] | None = None,
    failure_class: str = "",
    artifacts: dict[str, Any] | None = None,
    artifact_refs: list[ArtifactRef | dict[str, Any]] | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """Append one ledger event and return the serialized entry.

    The function is best-effort for callers: exceptions are allowed to bubble in
    unit tests, but production call sites should wrap it if the ledger must not
    block a pipeline stage.
    """
    cid, normalized_parent_ids, normalized_metrics = _candidate_event_identity(
        version,
        source_v,
        candidate_id,
        parent_ids,
        metrics,
    )
    scorecard_payload = _scorecard_payload(scorecard)
    refs = _artifact_refs_payload(artifact_refs)

    record = CandidateRecord(
        candidate_id=cid,
        event_type=event_type,
        event_source=event_source,  # type: ignore[arg-type]
        version=version,
        source_v=source_v,
        profile_id=profile_id,
        workflow_profile_id=workflow_profile_id,
        prompt_profile_id=prompt_profile_id,
        model_id=model_id,
        run_id=run_id,
        stage_attempt=stage_attempt,
        stage=stage,
        parent_ids=normalized_parent_ids,
        changed_files=changed_files or [],
        skill_layers=skill_layers or [],
        diff_hash=diff_hash,
        gate=gate,
        scorecard=scorecard_payload,
        gate_results=_gate_results_payload(gate_results),
        metrics=normalized_metrics,
        failures=failures or [],
        failure_class=failure_class,
        artifacts=artifacts or {},
        artifact_refs=refs,
    )
    events_path = _events_path(path)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    entry = record.model_dump()
    if entry["gate_results"] and entry["scorecard"].get("gates") == entry["gate_results"]:
        # Lossless bounded-retention dedupe: callers constructively pass
        # ``gate_results=scorecard.gates`` (tool_gates/tool_eval), storing the
        # same gate list twice per ledger row. When both copies are
        # structurally equal, persist an empty ``gate_results`` and keep the
        # canonical copy inside ``scorecard`` — no bytes are lost. Divergent
        # ``gate_results`` are always persisted verbatim.
        entry["gate_results"] = []
    append_locked_jsonl(events_path, entry)
    _upsert_sqlite_record(record, path=events_path, persisted_entry=entry)
    return entry


def read_candidate_events(
    *,
    version: int | None = None,
    candidate_id: str | None = None,
    event_source: str | None = None,
    limit: int | None = None,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    """Read candidate ledger entries, newest-last."""
    path = _events_path(path)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with locked_file(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if version is not None and item.get("version") != version:
                continue
            if candidate_id is not None and item.get("candidate_id") != candidate_id:
                continue
            if event_source is not None and item.get("event_source", "runtime") != event_source:
                continue
            rows.append(item)
    if limit is not None and limit >= 0:
        return rows[-limit:]
    return rows


def _candidate_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["parent_ids"] = _json_loads(item.pop("parent_ids_json"), [])
    item["skill_layers"] = _json_loads(item.pop("skill_layers_json"), [])
    item["changed_files"] = _json_loads(item.pop("changed_files_json"), [])
    item["latest_scorecard"] = _json_loads(item.pop("latest_scorecard_json"), {})
    item["latest_metrics"] = _json_loads(item.pop("latest_metrics_json"), {})
    item["latest_failures"] = _json_loads(item.pop("latest_failures_json"), [])
    item["artifacts"] = _json_loads(item.pop("artifacts_json"), {})
    item["artifact_refs"] = _json_loads(item.pop("artifact_refs_json"), [])
    return item


def read_candidate_entities(
    *,
    version: int | None = None,
    candidate_id: str | None = None,
    event_source: str | None = "runtime",
    limit: int | None = None,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    """Read queryable candidate entities from SQLite, newest-last."""
    db_path = _db_path_for_events(path)
    if not db_path.exists():
        return []
    clauses = []
    params: list[Any] = []
    if version is not None:
        clauses.append("version = ?")
        params.append(version)
    if candidate_id is not None:
        clauses.append("candidate_id = ?")
        params.append(candidate_id)
    if event_source is not None:
        clauses.append("event_source = ?")
        params.append(event_source)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    sql = "SELECT * FROM candidates" + where + " ORDER BY updated_at ASC"
    if limit is not None and limit >= 0:
        sql = "SELECT * FROM (" + "SELECT * FROM candidates" + where + " ORDER BY updated_at DESC LIMIT ?" + ") ORDER BY updated_at ASC"
        params.append(limit)
    with _connect(path) as conn:
        return [_candidate_row_to_dict(row) for row in conn.execute(sql, params).fetchall()]


def get_candidate_summary(
    candidate_id: str,
    *,
    event_source: str | None = "runtime",
    path: Path | None = None,
) -> dict[str, Any] | None:
    rows = read_candidate_entities(
        candidate_id=candidate_id,
        event_source=event_source,
        limit=1,
        path=path,
    )
    if not rows and event_source == "runtime":
        rows = read_candidate_entities(
            candidate_id=candidate_id,
            event_source=None,
            limit=1,
            path=path,
        )
    return rows[0] if rows else None


def read_candidate_artifacts(candidate_id: str, *, path: Path | None = None) -> list[dict[str, Any]]:
    db_path = _db_path_for_events(path)
    if not db_path.exists():
        return []
    with _connect(path) as conn:
        rows = conn.execute(
            "SELECT kind, path, label, sha256, size_bytes, hidden, timestamp "
            "FROM artifacts WHERE candidate_id = ? ORDER BY timestamp ASC, id ASC",
            (candidate_id,),
        ).fetchall()
    return [
        {
            **dict(row),
            "hidden": bool(row["hidden"]),
        }
        for row in rows
    ]


def count_candidate_children(parent_id: str, *, path: Path | None = None) -> int:
    """Count runtime candidates whose parent_ids include parent_id."""
    count = 0
    for row in read_candidate_entities(event_source="runtime", path=path):
        if parent_id in row.get("parent_ids", []):
            count += 1
    return count


def _retention_version_floor(
    versions: set[int],
    keep_versions: int,
) -> int | None:
    """Return the oldest version that stays inside the full-row window.

    ``None`` means the window covers every version present (fewer distinct
    versions than ``keep_versions``), so nothing is eligible for compaction.
    """

    if keep_versions <= 0:
        return None
    ordered = sorted(versions, reverse=True)
    if len(ordered) < keep_versions:
        return None
    return ordered[keep_versions - 1]


def _quality_finished_summary_row(
    row: dict[str, Any],
    original_line: bytes,
) -> dict[str, Any]:
    """Build the compacted replacement for one old quality_finished row.

    Keeps the identity/conclusion scalars plus a sha256 reference to the exact
    original JSON bytes, so the summary stays auditable against any retained
    copy of the full row (e.g. cycle archives) without carrying its bulk.
    """

    original_json = original_line.rstrip(b"\r\n")
    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    scorecard = row.get("scorecard") if isinstance(row.get("scorecard"), dict) else {}
    quality_summary: dict[str, Any] = {}
    if isinstance(metrics.get("all_passed"), bool):
        quality_summary["all_passed"] = metrics["all_passed"]
    decision_pass_rate = metrics.get("decision_pass_rate")
    if isinstance(decision_pass_rate, (int, float)) and not isinstance(
        decision_pass_rate, bool
    ):
        quality_summary["decision_pass_rate"] = decision_pass_rate
    if isinstance(scorecard.get("passed"), bool):
        quality_summary["passed"] = scorecard["passed"]
    return {
        "candidate_id": row.get("candidate_id"),
        "event_type": QUALITY_FINISHED_EVENT,
        "event_source": row.get("event_source", "runtime"),
        "version": row.get("version"),
        "source_v": row.get("source_v"),
        "timestamp": row.get("timestamp"),
        "gate": row.get("gate", ""),
        "run_id": row.get("run_id", ""),
        "failure_class": row.get("failure_class", ""),
        "quality_summary": quality_summary,
        "ledger_compaction": {
            "retention": QUALITY_SUMMARY_RETENTION,
            "original_sha256": hashlib.sha256(original_json).hexdigest(),
            "original_bytes": len(original_json),
        },
    }


def _parse_ledger_line(line: bytes) -> dict[str, Any] | None:
    """Parse one JSONL row, returning None for anything not a JSON object."""

    try:
        item = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return item if isinstance(item, dict) else None


def _line_version(item: dict[str, Any] | None) -> int | None:
    if item is None:
        return None
    version = item.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        return None
    return version


def _is_compaction_candidate(item: dict[str, Any] | None, floor: int) -> bool:
    return (
        item is not None
        and item.get("event_type") == QUALITY_FINISHED_EVENT
        and "ledger_compaction" not in item
        and _line_version(item) is not None
        and _line_version(item) < floor
    )


def compact_candidate_ledger(
    path: Path | None = None,
    *,
    keep_versions: int = DEFAULT_KEEP_VERSIONS,
) -> dict[str, Any]:
    """Bound the candidates ledger under the append sidecar lock.

    Full rows for the most recent ``keep_versions`` distinct versions are kept
    byte-exact; older ``quality_finished`` rows are rewritten in place as
    sha256-referenced summary rows; the paired SQLite ``candidate_events``
    rows are DELETEd and the database is VACUUMed to reclaim the bytes. The
    whole transaction — JSONL scan, rewrite, SQLite DELETE/VACUUM, receipt
    append — holds the exact ``_locked_state_sidecar`` exclusive lock that
    ``append_locked_jsonl`` acquires (evolution_infra_state_io.py), so a
    concurrent appender (same process via the thread lock, another process
    via flock) serializes fully with the rewrite and no line can be dropped.

    This is deliberately NOT the lockless ``read → tmp → os.replace`` shape of
    ``disk_hygiene._trim_jsonl``: without the sidecar lock an append landing
    between the read and the replace is silently lost. The rewrite itself is
    streamed through a private temp inode (the ``_atomic_publish_state_text``
    protocol: O_EXCL temp, fsync, inode revalidation, ``os.replace``,
    directory fsync) so a ~490 MB ledger never has to be materialized twice
    in memory on this memory-capped VM.

    Returns the receipt dict (also appended to
    ``candidates_compaction_receipt.jsonl`` beside the ledger whenever the
    run actually compacted or deleted something). A no-op run leaves both
    files untouched and returns ``receipt_persisted=False``.
    """

    events_path = _events_path(path)
    receipt: dict[str, Any] = {
        "receipt_type": "candidates_ledger_compaction",
        "events_file": events_path.name,
        "compacted_at": time.time(),
        "keep_versions": keep_versions,
        "version_floor": None,
        "rows_total": 0,
        "rows_compacted": 0,
        "bytes_before": 0,
        "bytes_after": 0,
        "bytes_reclaimed": 0,
        "sqlite_events_deleted": 0,
        "sqlite_vacuumed": False,
        "sqlite_bytes_before": 0,
        "sqlite_bytes_after": 0,
        "sqlite_bytes_reclaimed": 0,
        "summaries": [],
        "receipt_persisted": False,
    }
    events_path.parent.mkdir(parents=True, exist_ok=True)
    # Re-entrant exclusive lease on the append sidecar: the receipt append and
    # every other nested helper below re-enter (or bypass via raw os.open,
    # mirroring append_locked_jsonl's own in-section style) this same lock
    # instead of releasing it between the scan and the rewrite.
    with evolution_infra._locked_state_sidecar(
        events_path, lock_type=fcntl.LOCK_EX
    ):
        if not events_path.exists():
            return receipt
        existing = os.lstat(events_path)
        if (
            not stat.S_ISREG(existing.st_mode)
            or stat.S_ISLNK(existing.st_mode)
            or existing.st_nlink != 1
        ):
            raise OSError("candidates ledger compaction target is unsafe")
        receipt["bytes_before"] = int(existing.st_size)
        read_fd = os.open(
            events_path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            opened = os.fstat(read_fd)
            live = os.lstat(events_path)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or live.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (live.st_dev, live.st_ino)
            ):
                raise OSError("candidates ledger identity changed before scan")
            with os.fdopen(read_fd, "rb") as read_handle:
                read_fd = None
                # Pass 1 (streamed): distinct versions only — the window floor
                # must be known before any row can be classified.
                rows_total = 0
                versions: set[int] = set()
                for line in read_handle:
                    if not line.strip():
                        continue
                    rows_total += 1
                    version = _line_version(_parse_ledger_line(line))
                    if version is not None:
                        versions.add(version)
                floor = _retention_version_floor(versions, keep_versions)
                receipt["rows_total"] = rows_total
                receipt["version_floor"] = floor

                summaries: list[dict[str, Any]] = []
                if floor is not None:
                    # Pass 2 (streamed): rewrite into a private temp inode,
                    # copying kept lines verbatim and substituting summaries.
                    read_handle.seek(0)
                    temp = events_path.with_name(
                        f".{events_path.name}.{uuid.uuid4().hex}.tmp"
                    )
                    temp_flags = (
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0)
                    )
                    temp_fd = os.open(temp, temp_flags, 0o600)
                    bytes_after = 0
                    try:
                        with os.fdopen(temp_fd, "wb") as out:
                            temp_fd = None
                            for line in read_handle:
                                item = _parse_ledger_line(line)
                                if _is_compaction_candidate(item, floor):
                                    summary_row = _quality_finished_summary_row(
                                        item, line
                                    )
                                    chunk = (
                                        _json_dumps(summary_row) + "\n"
                                    ).encode("utf-8")
                                    out.write(chunk)
                                    bytes_after += len(chunk)
                                    compaction = summary_row["ledger_compaction"]
                                    summaries.append(
                                        {
                                            "candidate_id": summary_row[
                                                "candidate_id"
                                            ],
                                            "version": summary_row["version"],
                                            "timestamp": summary_row["timestamp"],
                                            "original_sha256": compaction[
                                                "original_sha256"
                                            ],
                                            "original_bytes": compaction[
                                                "original_bytes"
                                            ],
                                        }
                                    )
                                else:
                                    out.write(line)
                                    bytes_after += len(line)
                            out.flush()
                            os.fsync(out.fileno())
                            temp_identity = os.fstat(out.fileno())
                        # Fail closed if a writer that ignored the sidecar
                        # changed the destination while the temp was written.
                        current = os.lstat(events_path)
                        if (
                            not stat.S_ISREG(current.st_mode)
                            or stat.S_ISLNK(current.st_mode)
                            or current.st_nlink != 1
                            or (current.st_dev, current.st_ino)
                            != (existing.st_dev, existing.st_ino)
                        ):
                            raise OSError(
                                "candidates ledger changed during compaction"
                            )
                        if summaries:
                            os.replace(temp, events_path)
                            published = os.lstat(events_path)
                            if (
                                not stat.S_ISREG(published.st_mode)
                                or stat.S_ISLNK(published.st_mode)
                                or published.st_nlink != 1
                                or (published.st_dev, published.st_ino)
                                != (
                                    temp_identity.st_dev,
                                    temp_identity.st_ino,
                                )
                                or published.st_size != temp_identity.st_size
                            ):
                                raise OSError(
                                    "candidates ledger compaction publication"
                                    " did not retain the temporary inode"
                                )
                            evolution_infra._fsync_directory(events_path.parent)
                    finally:
                        if temp_fd is not None:
                            os.close(temp_fd)
                        try:
                            temp.unlink(missing_ok=True)
                        except OSError:
                            pass
                    receipt["bytes_after"] = bytes_after
                else:
                    receipt["bytes_after"] = receipt["bytes_before"]
                receipt["rows_compacted"] = len(summaries)
                receipt["bytes_reclaimed"] = max(
                    0, receipt["bytes_before"] - receipt["bytes_after"]
                )
                receipt["summaries"] = summaries
        finally:
            if read_fd is not None:
                os.close(read_fd)

        db_path = _db_path_for_events(events_path)
        if floor is not None and db_path.exists():
            try:
                receipt["sqlite_bytes_before"] = int(db_path.stat().st_size)
            except OSError:
                receipt["sqlite_bytes_before"] = 0
            with _connect(events_path) as conn:
                cursor = conn.execute(
                    "DELETE FROM candidate_events "
                    "WHERE event_type = ? AND version IS NOT NULL AND version < ?",
                    (QUALITY_FINISHED_EVENT, floor),
                )
                receipt["sqlite_events_deleted"] = max(0, cursor.rowcount or 0)
                conn.commit()
                try:
                    conn.execute("VACUUM")
                    receipt["sqlite_vacuumed"] = True
                except sqlite3.Error:
                    receipt["sqlite_vacuumed"] = False
                try:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error:
                    pass
            try:
                receipt["sqlite_bytes_after"] = int(db_path.stat().st_size)
            except OSError:
                receipt["sqlite_bytes_after"] = receipt["sqlite_bytes_before"]
            receipt["sqlite_bytes_reclaimed"] = max(
                0, receipt["sqlite_bytes_before"] - receipt["sqlite_bytes_after"]
            )

        if receipt["rows_compacted"] or receipt["sqlite_events_deleted"]:
            persisted = {**receipt, "receipt_persisted": True}
            append_locked_jsonl(
                events_path.parent / COMPACTION_RECEIPTS_FILE, persisted
            )
            receipt["receipt_persisted"] = True
    return receipt
