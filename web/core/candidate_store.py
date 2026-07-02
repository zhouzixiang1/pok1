"""Candidate ledger and query store for the evolution pipeline.

JSONL remains the append-only audit log. SQLite is the queryable entity layer
used by selection, UI, and postmortem analysis. The public append/read helpers
stay backward compatible with the original JSONL-only implementation.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from evolution_infra import RESULTS_DIR, append_locked_jsonl, locked_file
from pipeline_schema import ArtifactRef, CandidateRecord, GateResult, ScoreCard


CANDIDATE_EVENTS_FILE = RESULTS_DIR / "candidates.jsonl"
CANDIDATE_DB_FILE = RESULTS_DIR / "candidates.sqlite3"


SCHEMA_VERSION = 1


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
    event_path = Path(path or CANDIDATE_EVENTS_FILE)
    return event_path.with_suffix(".sqlite3")


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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS candidates (
            candidate_id TEXT PRIMARY KEY,
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
            updated_at REAL NOT NULL
        )
        """
    )
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
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def make_candidate_id(version: int | None, source_v: int | None = None) -> str:
    if version is None:
        return f"candidate_unknown_{int(time.time())}"
    if source_v is None:
        return f"claude_v{version}"
    return f"claude_v{version}_from_v{source_v}"


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


def _upsert_sqlite_record(record: CandidateRecord, *, path: Path | None = None) -> None:
    entry = record.model_dump()
    with _connect(path) as conn:
        now = float(record.timestamp)
        existing = conn.execute(
            "SELECT created_at FROM candidates WHERE candidate_id = ?",
            (record.candidate_id,),
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
            ON CONFLICT(candidate_id) DO UPDATE SET
                version=excluded.version,
                source_v=excluded.source_v,
                event_source=excluded.event_source,
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
    cid = candidate_id or make_candidate_id(version, source_v)
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
        parent_ids=parent_ids or [],
        changed_files=changed_files or [],
        skill_layers=skill_layers or [],
        diff_hash=diff_hash,
        gate=gate,
        scorecard=scorecard_payload,
        gate_results=_gate_results_payload(gate_results),
        metrics=metrics or {},
        failures=failures or [],
        failure_class=failure_class,
        artifacts=artifacts or {},
        artifact_refs=refs,
    )
    events_path = Path(path or CANDIDATE_EVENTS_FILE)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    entry = record.model_dump()
    append_locked_jsonl(events_path, entry)
    _upsert_sqlite_record(record, path=events_path)
    return entry


def read_candidate_events(
    *,
    version: int | None = None,
    candidate_id: str | None = None,
    event_source: str | None = None,
    limit: int | None = None,
    path: Path = CANDIDATE_EVENTS_FILE,
) -> list[dict[str, Any]]:
    """Read candidate ledger entries, newest-last."""
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
        sql += " LIMIT ?"
        params.append(limit)
    with _connect(path) as conn:
        return [_candidate_row_to_dict(row) for row in conn.execute(sql, params).fetchall()]


def get_candidate_summary(candidate_id: str, *, path: Path | None = None) -> dict[str, Any] | None:
    rows = read_candidate_entities(candidate_id=candidate_id, event_source=None, limit=1, path=path)
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
