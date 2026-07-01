"""Append-only candidate ledger for the evolution pipeline."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from evolution_infra import RESULTS_DIR, append_locked_jsonl, locked_file
from pipeline_schema import CandidateRecord, ScoreCard


CANDIDATE_EVENTS_FILE = RESULTS_DIR / "candidates.jsonl"


def make_candidate_id(version: int | None, source_v: int | None = None) -> str:
    if version is None:
        return f"candidate_unknown_{int(time.time())}"
    if source_v is None:
        return f"claude_v{version}"
    return f"claude_v{version}_from_v{source_v}"


def append_candidate_event(
    event_type: str,
    *,
    version: int | None = None,
    source_v: int | None = None,
    candidate_id: str | None = None,
    profile_id: str = "default",
    stage: str = "",
    parent_ids: list[str] | None = None,
    changed_files: list[str] | None = None,
    gate: str = "",
    scorecard: ScoreCard | dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    failures: list[str] | None = None,
    failure_class: str = "",
    artifacts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one ledger event and return the serialized entry.

    The function is best-effort for callers: exceptions are allowed to bubble in
    unit tests, but production call sites should wrap it if the ledger must not
    block a pipeline stage.
    """
    cid = candidate_id or make_candidate_id(version, source_v)
    if isinstance(scorecard, ScoreCard):
        scorecard_payload = scorecard.model_dump()
    elif isinstance(scorecard, dict):
        scorecard_payload = scorecard
    else:
        scorecard_payload = {}

    record = CandidateRecord(
        candidate_id=cid,
        event_type=event_type,
        version=version,
        source_v=source_v,
        profile_id=profile_id,
        stage=stage,
        parent_ids=parent_ids or [],
        changed_files=changed_files or [],
        gate=gate,
        scorecard=scorecard_payload,
        metrics=metrics or {},
        failures=failures or [],
        failure_class=failure_class,
        artifacts=artifacts or {},
    )
    CANDIDATE_EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    entry = record.model_dump()
    append_locked_jsonl(CANDIDATE_EVENTS_FILE, entry)
    return entry


def read_candidate_events(
    *,
    version: int | None = None,
    candidate_id: str | None = None,
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
            rows.append(item)
    if limit is not None and limit >= 0:
        return rows[-limit:]
    return rows
