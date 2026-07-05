"""Structured battle memory for replay-derived evidence and lessons.

The battle-experience thread is intentionally best-effort: LLM synthesis may be
disabled or unavailable while the daemon is under load. This module keeps the
deterministic replay evidence and the LLM-authored lessons separate so match
data is never lost just because the advisory LLM path is off.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import evolution_infra
from evolution_infra import append_locked_jsonl, locked_file


SCHEMA_VERSION = 1


@dataclass(frozen=True)
class BattleMemoryPaths:
    evidence_file: Path
    pending_file: Path
    lessons_file: Path


def default_paths() -> BattleMemoryPaths:
    results = evolution_infra.RESULTS_DIR
    return BattleMemoryPaths(
        evidence_file=results / "battle_evidence.jsonl",
        pending_file=results / "battle_pending_summaries.jsonl",
        lessons_file=results / "battle_lessons.jsonl",
    )


def stable_id(prefix: str, payload: Any, length: int = 16) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]
    return f"{prefix}_{digest}"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with locked_file(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except (OSError, UnicodeDecodeError):
        return []
    return rows


def _append_jsonl_dedup(path: Path, rows: Iterable[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = {str(row.get(key)) for row in _read_jsonl(path) if row.get(key)}
    appended: list[dict[str, Any]] = []
    for row in rows:
        row_id = row.get(key)
        if not row_id or str(row_id) in existing:
            continue
        append_locked_jsonl(path, row)
        existing.add(str(row_id))
        appended.append(row)
    return appended


def append_evidence(records: Iterable[dict[str, Any]], paths: BattleMemoryPaths | None = None) -> list[dict[str, Any]]:
    """Append deterministic replay evidence, deduplicated by evidence_id."""
    paths = paths or default_paths()
    normalized = []
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    for record in records:
        if not isinstance(record, dict):
            continue
        row = dict(record)
        row.setdefault("schema_version", SCHEMA_VERSION)
        row.setdefault("created_at", now)
        if not row.get("evidence_id"):
            row["evidence_id"] = stable_id("ev", row)
        normalized.append(row)
    return _append_jsonl_dedup(paths.evidence_file, normalized, "evidence_id")


def append_pending_summary(
    *,
    match_entry: dict[str, Any],
    summary: str,
    evidence_ids: list[str],
    paths: BattleMemoryPaths | None = None,
    status: str = "llm_pending",
) -> dict[str, Any] | None:
    """Record a compact pending summary for later lesson extraction.

    The pending id is match-scoped so the daemon can safely retry the same replay
    without creating unbounded duplicate pending rows.
    """
    paths = paths or default_paths()
    match_id = str(match_entry.get("id") or "")
    if not match_id or not summary:
        return None
    row = {
        "schema_version": SCHEMA_VERSION,
        "pending_id": stable_id("pending", {"match_id": match_id}),
        "match_id": match_id,
        "bot0": match_entry.get("bot0", ""),
        "bot1": match_entry.get("bot1", ""),
        "timestamp": match_entry.get("timestamp", ""),
        "status": status,
        "evidence_ids": list(dict.fromkeys(evidence_ids)),
        "summary": summary[:6000],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    appended = _append_jsonl_dedup(paths.pending_file, [row], "pending_id")
    return appended[0] if appended else row


def markdown_lessons_to_records(
    markdown: str,
    *,
    evidence_ids: list[str],
    source: str = "battle_experience_llm",
) -> list[dict[str, Any]]:
    """Convert concise LLM markdown bullets into structured lesson rows.

    This is intentionally conservative. It does not try to infer poker truth
    from prose; it only gives the existing LLM output stable IDs and evidence
    references so later prompts and attribution code can cite it precisely.
    """
    if not markdown or markdown.strip() == "No new observations.":
        return []
    records: list[dict[str, Any]] = []
    section = ""
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("##"):
            section = line.lstrip("#").strip()
            continue
        if not line.startswith(("-", "*")):
            continue
        text = line.lstrip("-* ").strip()
        if len(text) < 12:
            continue
        payload = {"section": section, "text": text, "evidence_ids": evidence_ids}
        records.append({
            "schema_version": SCHEMA_VERSION,
            "lesson_id": stable_id("battle_lesson", payload),
            "status": "active",
            "source": source,
            "section": section or "Battle observations",
            "text": text[:1200],
            "scope": _infer_scope(text),
            "confidence": "medium",
            "sample_n": None,
            "evidence_ids": list(dict.fromkeys(evidence_ids)),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
    return records


def append_lessons(records: Iterable[dict[str, Any]], paths: BattleMemoryPaths | None = None) -> list[dict[str, Any]]:
    paths = paths or default_paths()
    normalized = []
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    for record in records:
        if not isinstance(record, dict):
            continue
        row = dict(record)
        row.setdefault("schema_version", SCHEMA_VERSION)
        row.setdefault("status", "active")
        row.setdefault("created_at", now)
        if not row.get("lesson_id"):
            row["lesson_id"] = stable_id("battle_lesson", row)
        normalized.append(row)
    return _append_jsonl_dedup(paths.lessons_file, normalized, "lesson_id")


def format_battle_memory_for_master(
    *,
    paths: BattleMemoryPaths | None = None,
    source_bot: str = "",
    max_lessons: int = 8,
    max_pending: int = 6,
    max_evidence: int = 8,
) -> str:
    """Return a bounded prompt section from structured battle memory."""
    paths = paths or default_paths()
    lessons = [
        row for row in _read_jsonl(paths.lessons_file)
        if row.get("status", "active") == "active"
    ]
    pending = [
        row for row in _read_jsonl(paths.pending_file)
        if row.get("status", "llm_pending") in {"llm_pending", "summary_ready"}
    ]
    evidence = _read_jsonl(paths.evidence_file)
    if source_bot:
        source_lessons = [
            row for row in lessons
            if source_bot in json.dumps(row, ensure_ascii=False)
        ]
        if source_lessons:
            lessons = [row for row in lessons if row not in source_lessons] + source_lessons
        source_evidence = [
            row for row in evidence
            if row.get("bot") == source_bot or row.get("opponent") == source_bot
        ]
        if source_evidence:
            evidence = [row for row in evidence if row not in source_evidence] + source_evidence

    lines: list[str] = []
    if lessons:
        lines.append("## Structured Battle Lessons")
        for row in lessons[-max_lessons:]:
            eid = ",".join(row.get("evidence_ids", [])[:3]) or "no-evidence-id"
            lines.append(
                f"- [{row.get('lesson_id')}] scope={row.get('scope','general')} "
                f"confidence={row.get('confidence','unknown')} evidence={eid}: "
                f"{str(row.get('text','')).strip()[:500]}"
            )

    if pending:
        lines.append("## Pending Battle Summaries (deterministic evidence captured; LLM lesson extraction pending)")
        for row in pending[-max_pending:]:
            eid = ",".join(row.get("evidence_ids", [])[:3]) or "no-evidence-id"
            summary = " ".join(str(row.get("summary", "")).split())[:500]
            lines.append(f"- match={row.get('match_id')} evidence={eid}: {summary}")

    if evidence:
        lines.append("## Replay Evidence Snapshot")
        for row in evidence[-max_evidence:]:
            wr = row.get("win_rate")
            wr_txt = f"{float(wr) * 100:.1f}%" if isinstance(wr, (int, float)) else "n/a"
            avg_delta = row.get("avg_delta")
            avg_txt = f"{float(avg_delta):.0f}" if isinstance(avg_delta, (int, float)) else "n/a"
            lines.append(
                f"- [{row.get('evidence_id')}] {row.get('bot')} vs {row.get('opponent')}: "
                f"n={row.get('sample_n')} wr={wr_txt} avg_delta={avg_txt} "
                f"tags={','.join(row.get('spot_tags', [])[:4]) or 'none'}"
            )

    return "\n".join(lines).strip()


def _infer_scope(text: str) -> str:
    lower = text.lower()
    for street in ("preflop", "flop", "turn", "river"):
        if street in lower:
            return street
    if "opponent" in lower or "vs " in lower:
        return "opponent_model"
    if "allin" in lower or "raise" in lower:
        return "bet_sizing"
    return "general"
