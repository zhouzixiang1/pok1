"""Identity-bound memory derived from strict native TCP rating replays.

Every persisted row carries the active epoch, execution mode, and evaluation
identity digest.  Retrieval requires an exact expected digest; rows from an
older epoch or an unverifiable replay are silently excluded from prompt text.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import evolution_infra
from bot_namespace import EVALUATION_EPOCH, FIRST_STRICT_POLICY_VERSION, parse_bot_version
from evolution_infra import append_locked_jsonl, locked_file


SCHEMA_VERSION = 2
EXECUTION_MODE = "native_tcp"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


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


def stable_id(prefix: str, payload: Any, length: int = 20) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:length]}"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists() or path.is_symlink():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with locked_file(path, "r", encoding="utf-8") as handle:
            for raw in handle:
                try:
                    row = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except (OSError, UnicodeDecodeError):
        return []
    return rows


def _strict_label(value: Any) -> bool:
    version = parse_bot_version(value if isinstance(value, str) else None)
    return version is not None and version >= FIRST_STRICT_POLICY_VERSION


def _identity_fields_valid(row: dict[str, Any], expected_digest: str | None = None) -> bool:
    digest = row.get("evaluation_identity_digest")
    return bool(
        row.get("schema_version") == SCHEMA_VERSION
        and row.get("epoch") == EVALUATION_EPOCH
        and row.get("execution_mode") == EXECUTION_MODE
        and isinstance(digest, str)
        and _HEX64.fullmatch(digest)
        and (expected_digest is None or digest == expected_digest)
    )


def _evidence_valid(row: dict[str, Any], expected_digest: str | None = None) -> bool:
    if not _identity_fields_valid(row, expected_digest):
        return False
    if not _strict_label(row.get("bot")) or not _strict_label(row.get("opponent")):
        return False
    if row.get("bot") == row.get("opponent"):
        return False
    if not isinstance(row.get("match_id"), str) or not row["match_id"].endswith(".json"):
        return False
    for key in ("artifact_identity_digest", "opponent_artifact_identity_digest"):
        if not isinstance(row.get(key), str) or not _HEX64.fullmatch(row[key]):
            return False
    sample_n = row.get("sample_n")
    return isinstance(sample_n, int) and not isinstance(sample_n, bool) and sample_n > 0


def _append_jsonl_dedup(
    path: Path,
    rows: Iterable[dict[str, Any]],
    key: str,
) -> list[dict[str, Any]]:
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


def append_evidence(
    records: Iterable[dict[str, Any]],
    paths: BattleMemoryPaths | None = None,
) -> list[dict[str, Any]]:
    """Append only schema-valid, strict native replay evidence."""

    paths = paths or default_paths()
    accepted: list[dict[str, Any]] = []
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    for record in records:
        if not isinstance(record, dict):
            continue
        row = dict(record)
        row.setdefault("schema_version", SCHEMA_VERSION)
        row.setdefault("created_at", now)
        if not _evidence_valid(row):
            continue
        if not row.get("evidence_id"):
            row["evidence_id"] = stable_id("native_ev", row)
        accepted.append(row)
    return _append_jsonl_dedup(paths.evidence_file, accepted, "evidence_id")


def append_pending_summary(
    *,
    match_entry: dict[str, Any],
    summary: str,
    evidence_ids: list[str],
    paths: BattleMemoryPaths | None = None,
    status: str = "summary_ready",
) -> dict[str, Any] | None:
    """Persist a bounded summary only when its strict evidence IDs resolve."""

    paths = paths or default_paths()
    digest = match_entry.get("evaluation_identity_digest")
    if not isinstance(summary, str) or not summary.strip():
        return None
    identity_stub = {
        "schema_version": SCHEMA_VERSION,
        "epoch": match_entry.get("evaluation_epoch"),
        "execution_mode": match_entry.get("execution_mode"),
        "evaluation_identity_digest": digest,
    }
    if not _identity_fields_valid(identity_stub):
        return None
    if not _strict_label(match_entry.get("bot0")) or not _strict_label(match_entry.get("bot1")):
        return None
    match_id = match_entry.get("id")
    if not isinstance(match_id, str) or not match_id.endswith(".json"):
        return None
    valid_evidence = {
        str(row.get("evidence_id")): row
        for row in _read_jsonl(paths.evidence_file)
        if _evidence_valid(row, str(digest)) and row.get("match_id") == match_id
    }
    ids = list(dict.fromkeys(str(value) for value in evidence_ids))
    if not ids or any(value not in valid_evidence for value in ids):
        return None
    row = {
        **identity_stub,
        "pending_id": stable_id("native_pending", {"match_id": match_id, "identity": digest}),
        "match_id": match_id,
        "bot0": match_entry["bot0"],
        "bot1": match_entry["bot1"],
        "timestamp": str(match_entry.get("timestamp") or ""),
        "status": status,
        "evidence_ids": ids,
        "summary": summary[:6000],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    appended = _append_jsonl_dedup(paths.pending_file, [row], "pending_id")
    return appended[0] if appended else row


def markdown_lessons_to_records(
    markdown: str,
    *,
    evidence_ids: list[str],
    evaluation_identity_digest: str,
    source: str = "native_replay_analysis",
) -> list[dict[str, Any]]:
    """Turn bounded advisory bullets into identity-bound lesson rows."""

    if not isinstance(markdown, str) or not markdown.strip():
        return []
    if not _HEX64.fullmatch(str(evaluation_identity_digest)) or not evidence_ids:
        return []
    records: list[dict[str, Any]] = []
    section = "Native replay observations"
    for raw in markdown.splitlines():
        line = raw.strip()
        if line.startswith("##"):
            section = line.lstrip("# ")[:120] or section
            continue
        if not line.startswith(("- ", "* ")):
            continue
        text = line[2:].strip()
        if len(text) < 12:
            continue
        payload = {
            "section": section,
            "text": text[:1200],
            "evidence_ids": list(dict.fromkeys(evidence_ids)),
            "evaluation_identity_digest": evaluation_identity_digest,
        }
        records.append({
            "schema_version": SCHEMA_VERSION,
            "epoch": EVALUATION_EPOCH,
            "execution_mode": EXECUTION_MODE,
            "evaluation_identity_digest": evaluation_identity_digest,
            "lesson_id": stable_id("native_lesson", payload),
            "status": "active",
            "source": source,
            "section": section,
            "text": text[:1200],
            "scope": _infer_scope(text),
            "confidence": "advisory",
            "evidence_ids": list(dict.fromkeys(evidence_ids)),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
    return records


def append_lessons(
    records: Iterable[dict[str, Any]],
    paths: BattleMemoryPaths | None = None,
) -> list[dict[str, Any]]:
    """Append lessons only if all cited evidence exists under the same identity."""

    paths = paths or default_paths()
    evidence = {
        str(row.get("evidence_id")): row
        for row in _read_jsonl(paths.evidence_file)
        if _evidence_valid(row)
    }
    accepted: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict) or not _identity_fields_valid(record):
            continue
        row = dict(record)
        ids = row.get("evidence_ids")
        digest = row.get("evaluation_identity_digest")
        if (
            not isinstance(ids, list)
            or not ids
            or any(str(value) not in evidence for value in ids)
            or any(evidence[str(value)].get("evaluation_identity_digest") != digest for value in ids)
        ):
            continue
        if not row.get("lesson_id"):
            row["lesson_id"] = stable_id("native_lesson", row)
        accepted.append(row)
    return _append_jsonl_dedup(paths.lessons_file, accepted, "lesson_id")


def read_identity_bound_lessons(
    *,
    expected_evaluation_identity_digest: str,
    paths: BattleMemoryPaths | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return current-identity lessons whose cited native evidence resolves.

    This is the read-only dashboard boundary.  It intentionally has no
    Markdown fallback and never exposes unverified rows from an older epoch.
    """

    if (
        not isinstance(expected_evaluation_identity_digest, str)
        or not _HEX64.fullmatch(expected_evaluation_identity_digest)
        or not isinstance(limit, int)
        or isinstance(limit, bool)
        or limit <= 0
    ):
        return []
    paths = paths or default_paths()
    evidence_ids = {
        str(row["evidence_id"])
        for row in _read_jsonl(paths.evidence_file)
        if _evidence_valid(row, expected_evaluation_identity_digest)
        and row.get("evidence_id")
    }
    lessons = [
        row for row in _read_jsonl(paths.lessons_file)
        if _identity_fields_valid(row, expected_evaluation_identity_digest)
        and row.get("status") == "active"
        and row.get("confidence") == "advisory"
        and isinstance(row.get("lesson_id"), str)
        and isinstance(row.get("text"), str)
        and row["text"].strip()
        and isinstance(row.get("evidence_ids"), list)
        and row["evidence_ids"]
        and all(str(value) in evidence_ids for value in row["evidence_ids"])
    ]
    return [
        {
            "schema_version": SCHEMA_VERSION,
            "epoch": EVALUATION_EPOCH,
            "execution_mode": EXECUTION_MODE,
            "evaluation_identity_digest": expected_evaluation_identity_digest,
            "lesson_id": row["lesson_id"],
            "status": "active",
            "source": str(row.get("source") or "native_replay_analysis")[:120],
            "section": str(row.get("section") or "Native replay observations")[:120],
            "text": str(row["text"])[:1200],
            "scope": str(row.get("scope") or "general")[:80],
            "confidence": "advisory",
            "evidence_ids": [str(value) for value in row["evidence_ids"]],
            "created_at": str(row.get("created_at") or "")[:40],
        }
        for row in lessons[-limit:]
    ]


def format_battle_memory_for_master(
    *,
    expected_evaluation_identity_digest: str,
    paths: BattleMemoryPaths | None = None,
    source_bot: str = "",
    max_lessons: int = 8,
    max_pending: int = 6,
    max_evidence: int = 8,
) -> str:
    """Render only current-identity native evidence for prompt injection."""

    if not isinstance(expected_evaluation_identity_digest, str) or not _HEX64.fullmatch(
        expected_evaluation_identity_digest
    ):
        return ""
    paths = paths or default_paths()
    evidence = [
        row for row in _read_jsonl(paths.evidence_file)
        if _evidence_valid(row, expected_evaluation_identity_digest)
    ]
    evidence_by_id = {str(row["evidence_id"]): row for row in evidence if row.get("evidence_id")}
    pending = [
        row for row in _read_jsonl(paths.pending_file)
        if _identity_fields_valid(row, expected_evaluation_identity_digest)
        and row.get("status") in {"summary_ready", "llm_pending"}
        and all(str(value) in evidence_by_id for value in (row.get("evidence_ids") or []))
    ]
    lessons = [
        row for row in _read_jsonl(paths.lessons_file)
        if _identity_fields_valid(row, expected_evaluation_identity_digest)
        and row.get("status") == "active"
        and row.get("evidence_ids")
        and all(str(value) in evidence_by_id for value in row["evidence_ids"])
    ]
    if source_bot:
        if not _strict_label(source_bot):
            return ""
        evidence = [
            row for row in evidence
            if row.get("bot") == source_bot or row.get("opponent") == source_bot
        ]
        allowed_ids = {str(row["evidence_id"]) for row in evidence}
        pending = [row for row in pending if any(str(value) in allowed_ids for value in row["evidence_ids"])]
        lessons = [row for row in lessons if any(str(value) in allowed_ids for value in row["evidence_ids"])]

    lines: list[str] = []
    if lessons:
        lines.append("## Identity-bound native replay lessons (advisory)")
        for row in lessons[-max_lessons:]:
            ids = ",".join(str(value) for value in row["evidence_ids"][:3])
            lines.append(
                f"- [{row['lesson_id']}] scope={row.get('scope','general')} evidence={ids}: "
                f"{str(row.get('text') or '')[:500]}"
            )
    if pending:
        lines.append("## Deterministic native replay summaries")
        for row in pending[-max_pending:]:
            ids = ",".join(str(value) for value in row["evidence_ids"][:3])
            summary = " ".join(str(row.get("summary") or "").split())[:500]
            lines.append(f"- match={row['match_id']} evidence={ids}: {summary}")
    if evidence:
        lines.append("## Native replay evidence")
        for row in evidence[-max_evidence:]:
            terminal = row.get("opponent_terminal") or {}
            showdown = row.get("showdown_range") or {}
            lines.append(
                f"- [{row['evidence_id']}] {row['bot']} vs {row['opponent']}: "
                f"70-hand samples={row['sample_n']} score={float(row.get('win_rate',0.0)):.3f} "
                f"avg_net={float(row.get('avg_delta',0.0)):+.1f}; "
                f"fold_to_raise={terminal.get('fold_to_raise')} n={terminal.get('fold_to_raise_samples',0)}; "
                f"fold_to_jam={terminal.get('fold_to_jam')} n={terminal.get('fold_to_jam_samples',0)}; "
                f"river_overcall={terminal.get('river_overcall')} n={terminal.get('river_overcall_samples',0)}; "
                f"showdowns={showdown.get('samples',0)} buckets="
                f"{json.dumps(showdown.get('bucket_counts') or {}, sort_keys=True, separators=(',', ':'))}"
            )
    if not lines:
        return ""
    header = (
        f"Native replay contract: epoch={EVALUATION_EPOCH} mode={EXECUTION_MODE} "
        f"evaluation_identity={expected_evaluation_identity_digest}"
    )
    return (header + "\n" + "\n".join(lines))[:16000]


def _infer_scope(text: str) -> str:
    lower = text.lower()
    for street in ("preflop", "flop", "turn", "river"):
        if street in lower:
            return street
    if "showdown" in lower or "range" in lower or "opponent" in lower:
        return "opponent_model"
    if "allin" in lower or "raise" in lower or "sizing" in lower:
        return "bet_sizing"
    return "general"


__all__ = [
    "BattleMemoryPaths",
    "SCHEMA_VERSION",
    "append_evidence",
    "append_lessons",
    "append_pending_summary",
    "default_paths",
    "format_battle_memory_for_master",
    "markdown_lessons_to_records",
    "read_identity_bound_lessons",
    "stable_id",
]
