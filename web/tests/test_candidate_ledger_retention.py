"""Bounded retention window for the candidates ledger (T3).

``compact_candidate_ledger`` keeps the most recent ``keep_versions`` distinct
versions byte-exact and compacts older ``quality_finished`` rows into
sha256-referenced summary rows under the append sidecar lock. These tests pin:

- the window boundary (version == floor stays full, version < floor is
  summarized, non-quality rows and sparse version gaps behave);
- that concurrent appends — same-process threads and a real second process —
  never lose a line while compaction rewrites the file;
- the compaction receipt's shape and its on-disk persistence.
"""

import hashlib
import json
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

from candidate_store import (
    COMPACTION_RECEIPTS_FILE,
    DEFAULT_KEEP_VERSIONS,
    append_candidate_event,
    compact_candidate_ledger,
    read_candidate_entities,
    read_candidate_events,
)

FILLER = "q" * 8192


def _scorecard_for(version: int) -> dict:
    return {
        "name": "quality",
        "passed": version % 2 == 0,
        "gates": [{"name": "compile", "passed": True}],
        "filler": FILLER,
    }


def _seed_ledger(ledger: Path, versions) -> None:
    for version in versions:
        source_v = version - 1 if version > 1 else None
        append_candidate_event(
            "quality_started",
            version=version,
            source_v=source_v,
            stage="quality",
            path=ledger,
        )
        append_candidate_event(
            "quality_finished",
            version=version,
            source_v=source_v,
            gate="quality",
            scorecard=_scorecard_for(version),
            metrics={
                "all_passed": version % 2 == 0,
                "decision_pass_rate": round(0.5 + version / 1000, 4),
            },
            path=ledger,
        )


def _ledger_lines(ledger: Path) -> list[bytes]:
    return [line for line in ledger.read_bytes().splitlines(keepends=True) if line.strip()]


def _is_old_quality_row(row, floor: int) -> bool:
    version = row.get("version")
    return (
        row.get("event_type") == "quality_finished"
        and isinstance(version, int)
        and version < floor
    )


def test_retention_window_boundary_and_summary_rows(tmp_path):
    ledger = tmp_path / "candidates.jsonl"
    _seed_ledger(ledger, range(1, 61))

    original_lines = _ledger_lines(ledger)
    original_rows = [json.loads(line) for line in original_lines]
    expected_sha = {
        row["version"]: hashlib.sha256(line.rstrip(b"\r\n")).hexdigest()
        for row, line in zip(original_rows, original_lines)
        if _is_old_quality_row(row, floor=11)
    }
    assert len(expected_sha) == 10  # versions 1..10 fall below the floor

    receipt = compact_candidate_ledger(ledger, keep_versions=50)

    assert receipt["version_floor"] == 11
    assert receipt["rows_total"] == 120
    assert receipt["rows_compacted"] == 10
    assert receipt["bytes_reclaimed"] > 0
    assert receipt["sqlite_events_deleted"] == 10
    assert receipt["sqlite_vacuumed"] is True
    assert receipt["receipt_persisted"] is True

    rows = read_candidate_events(path=ledger)
    assert len(rows) == 120  # 1:1 replacement, no rows lost or duplicated

    by_key = {(row["event_type"], row["version"]): row for row in rows}

    # Window boundary: version == floor keeps the full row.
    full = by_key[("quality_finished", 11)]
    assert full["scorecard"]["filler"] == FILLER
    assert "ledger_compaction" not in full
    # Versions inside the window are untouched too.
    assert by_key[("quality_finished", 60)]["scorecard"]["filler"] == FILLER

    # Older quality_finished rows become sha256-referenced summaries that
    # keep identity + conclusion scalars.
    for version in range(1, 11):
        summary = by_key[("quality_finished", version)]
        assert "ledger_compaction" in summary
        assert "scorecard" not in summary
        compaction = summary["ledger_compaction"]
        assert compaction["retention"] == "quality_finished_summary"
        assert compaction["original_sha256"] == expected_sha[version]
        assert compaction["original_bytes"] > len(FILLER)
        assert summary["gate"] == "quality"
        assert summary["failure_class"] == ""
        assert summary["quality_summary"]["all_passed"] is (version % 2 == 0)
        assert summary["quality_summary"]["passed"] is (version % 2 == 0)
        assert summary["quality_summary"]["decision_pass_rate"] == round(
            0.5 + version / 1000, 4
        )

    # Non-quality rows of old versions stay full.
    for version in range(1, 11):
        started = by_key[("quality_started", version)]
        assert "ledger_compaction" not in started
        assert started["stage"] == "quality"

    # Untouched rows keep their exact original bytes.
    new_lines = _ledger_lines(ledger)
    kept_original = {
        line
        for row, line in zip(original_rows, original_lines)
        if not _is_old_quality_row(row, floor=11)
    }
    assert kept_original.issubset(set(new_lines))
    assert len(new_lines) == len(original_lines)

    # SQLite: old quality_finished events deleted, everything else kept and
    # the entity projections stay queryable and full.
    db = ledger.with_suffix(".sqlite3")
    with sqlite3.connect(db) as conn:
        old_qf = conn.execute(
            "SELECT COUNT(*) FROM candidate_events "
            "WHERE event_type='quality_finished' AND version < 11"
        ).fetchone()[0]
        kept_qf = conn.execute(
            "SELECT COUNT(*) FROM candidate_events "
            "WHERE event_type='quality_finished' AND version >= 11"
        ).fetchone()[0]
        started = conn.execute(
            "SELECT COUNT(*) FROM candidate_events WHERE event_type='quality_started'"
        ).fetchone()[0]
    assert old_qf == 0
    assert kept_qf == 50
    assert started == 60

    entities = read_candidate_entities(path=ledger, event_source=None)
    assert len(entities) == 60
    for entity in entities:
        assert entity["latest_event_type"] == "quality_finished"
        assert entity["latest_scorecard"]["filler"] == FILLER

    # Idempotent second run: already-summarized rows are skipped, nothing is
    # double-hashed, and no second receipt is appended.
    second = compact_candidate_ledger(ledger, keep_versions=50)
    assert second["rows_compacted"] == 0
    assert second["sqlite_events_deleted"] == 0
    assert second["receipt_persisted"] is False
    assert _ledger_lines(ledger) == new_lines


def test_retention_window_handles_sparse_version_gaps(tmp_path):
    ledger = tmp_path / "candidates.jsonl"
    _seed_ledger(ledger, [1, 5, 9, *range(20, 70)])

    receipt = compact_candidate_ledger(ledger, keep_versions=50)

    # The 50 newest distinct versions are 20..69, so the floor is 20 and only
    # the sparse old versions 1/5/9 compact.
    assert receipt["version_floor"] == 20
    assert receipt["rows_compacted"] == 3
    assert {entry["version"] for entry in receipt["summaries"]} == {1, 5, 9}

    rows = read_candidate_events(path=ledger)
    by_key = {(row["event_type"], row["version"]): row for row in rows}
    for version in (1, 5, 9):
        assert "ledger_compaction" in by_key[("quality_finished", version)]
    assert by_key[("quality_finished", 20)]["scorecard"]["filler"] == FILLER
    assert "ledger_compaction" not in by_key[("quality_finished", 69)]


def test_retention_is_noop_when_window_covers_everything(tmp_path):
    ledger = tmp_path / "candidates.jsonl"
    _seed_ledger(ledger, range(1, 11))

    before = ledger.read_bytes()
    receipt = compact_candidate_ledger(ledger, keep_versions=50)

    assert DEFAULT_KEEP_VERSIONS == 50
    assert receipt["version_floor"] is None
    assert receipt["rows_compacted"] == 0
    assert receipt["bytes_reclaimed"] == 0
    assert receipt["sqlite_events_deleted"] == 0
    assert receipt["receipt_persisted"] is False
    assert ledger.read_bytes() == before
    assert not (tmp_path / COMPACTION_RECEIPTS_FILE).exists()


def test_compaction_receipt_shape_and_persistence(tmp_path):
    ledger = tmp_path / "candidates.jsonl"
    _seed_ledger(ledger, range(1, 61))

    receipt = compact_candidate_ledger(ledger, keep_versions=50)

    expected_keys = {
        "receipt_type",
        "events_file",
        "compacted_at",
        "keep_versions",
        "version_floor",
        "rows_total",
        "rows_compacted",
        "bytes_before",
        "bytes_after",
        "bytes_reclaimed",
        "sqlite_events_deleted",
        "sqlite_vacuumed",
        "sqlite_bytes_before",
        "sqlite_bytes_after",
        "sqlite_bytes_reclaimed",
        "summaries",
        "receipt_persisted",
    }
    assert set(receipt) == expected_keys
    assert receipt["receipt_type"] == "candidates_ledger_compaction"
    assert receipt["events_file"] == "candidates.jsonl"
    assert isinstance(receipt["compacted_at"], float)
    assert receipt["keep_versions"] == 50
    assert isinstance(receipt["version_floor"], int)
    for key in (
        "rows_total",
        "rows_compacted",
        "bytes_before",
        "bytes_after",
        "bytes_reclaimed",
        "sqlite_events_deleted",
        "sqlite_bytes_before",
        "sqlite_bytes_after",
        "sqlite_bytes_reclaimed",
    ):
        assert isinstance(receipt[key], int)
    assert isinstance(receipt["sqlite_vacuumed"], bool)
    assert isinstance(receipt["summaries"], list) and receipt["summaries"]

    for entry in receipt["summaries"]:
        assert set(entry) == {
            "candidate_id",
            "version",
            "timestamp",
            "original_sha256",
            "original_bytes",
        }
        assert len(entry["original_sha256"]) == 64
        int(entry["original_sha256"], 16)
        assert entry["original_bytes"] > 0

    assert receipt["bytes_before"] == ledger.stat().st_size + receipt[
        "bytes_reclaimed"
    ]

    receipt_file = tmp_path / COMPACTION_RECEIPTS_FILE
    persisted = [
        json.loads(line)
        for line in receipt_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(persisted) == 1
    assert persisted[0] == receipt


def test_concurrent_thread_appends_are_never_dropped(tmp_path):
    ledger = tmp_path / "candidates.jsonl"
    _seed_ledger(ledger, range(1, 61))
    rows_before = len(_ledger_lines(ledger))

    errors: list[Exception] = []
    stop = threading.Event()

    def appender(tid: int) -> None:
        for i in range(8):
            try:
                append_candidate_event(
                    "quality_finished",
                    version=60,
                    source_v=59,
                    run_id=f"race-t{tid}-{i}",
                    gate="quality",
                    scorecard=_scorecard_for(60),
                    metrics={"all_passed": True},
                    path=ledger,
                )
            except Exception as exc:  # pragma: no cover - failure evidence
                errors.append(exc)

    def compactor() -> None:
        while not stop.is_set():
            compact_candidate_ledger(ledger, keep_versions=50)

    compactor_thread = threading.Thread(target=compactor)
    compactor_thread.start()
    appenders = [
        threading.Thread(target=appender, args=(tid,)) for tid in range(6)
    ]
    for thread in appenders:
        thread.start()
    for thread in appenders:
        thread.join()
    stop.set()
    compactor_thread.join()

    assert errors == []

    lines = _ledger_lines(ledger)
    assert len(lines) == rows_before + 48
    for line in lines:  # no torn/corrupt line
        json.loads(line)

    rows = read_candidate_events(path=ledger)
    race_rows = [
        row for row in rows if str(row.get("run_id", "")).startswith("race-t")
    ]
    assert len(race_rows) == 48
    assert all(row["scorecard"]["filler"] == FILLER for row in race_rows)

    db = ledger.with_suffix(".sqlite3")
    with sqlite3.connect(db) as conn:
        race_db = conn.execute(
            "SELECT COUNT(*) FROM candidate_events WHERE run_id LIKE 'race-t%'"
        ).fetchone()[0]
    assert race_db == 48


def test_cross_process_appends_are_never_dropped(tmp_path):
    ledger = tmp_path / "candidates.jsonl"
    _seed_ledger(ledger, range(1, 61))
    rows_before = len(_ledger_lines(ledger))

    core_dir = Path(__file__).resolve().parents[1] / "core"
    script = textwrap.dedent(
        """
        import sys, time
        sys.path.insert(0, {core_dir!r})
        from candidate_store import append_candidate_event

        ledger = {ledger!r}
        for i in range(30):
            append_candidate_event(
                "quality_finished",
                version=60,
                source_v=59,
                run_id=f"race-p-{{i}}",
                gate="quality",
                scorecard={{"name": "quality", "passed": True, "filler": "z" * 2048}},
                metrics={{"all_passed": True}},
                path=ledger,
            )
            time.sleep(0.002)
        """
    ).format(core_dir=str(core_dir), ledger=str(ledger))

    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        while process.poll() is None:
            compact_candidate_ledger(ledger, keep_versions=50)
            time.sleep(0.001)
    finally:
        stdout, stderr = process.communicate(timeout=60)
    assert process.returncode == 0, stderr.decode("utf-8", "replace")

    lines = _ledger_lines(ledger)
    assert len(lines) == rows_before + 30
    for line in lines:
        json.loads(line)

    rows = read_candidate_events(path=ledger)
    race_rows = [
        row for row in rows if str(row.get("run_id", "")).startswith("race-p-")
    ]
    assert len(race_rows) == 30
    assert all(row["scorecard"]["filler"] == "z" * 2048 for row in race_rows)
