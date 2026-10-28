"""T7 (2026-10-15): orphan never-published ``vN`` trees + cost-ledger compaction.

(a) ``_reap_generation_trees`` used to clear only versions present in the
    abandon ledger, so a generation that crashed without an abandon receipt
    left its ``results/vN`` tree forever. The orphan branch additionally
    reaps trees that were prepared but never published, carry no abandon
    ledger row, have no live reference (primary/draft/consumer checkpoints,
    non-terminal Slice-2b lifecycle rows), contain no ``strict_invocations``
    subpath, and are older than the age window (default 14 days) — with a
    receipt-first append to ``reaped_generation_trees.jsonl``.

(b) ``generation_cost_ledger.jsonl`` grew to 28M because nothing ever
    removed rows. ``compact_generation_cost_ledger`` keeps the recent
    retention window (default 30 days) of full rows byte-verbatim and
    compacts older rows into per-month summary rows plus a per-row summary
    receipt, all under the ``_locked_state_sidecar`` lock every ledger
    reader/writer already takes. No row may silently disappear.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import disk_hygiene

DAY = 24.0 * 3600.0
ORPHAN_WINDOW = 14.0 * DAY


def _touch_tree(path: Path, name: str = "blob.bin", size: int = 64) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_bytes(b"x" * size)


def _age(path: Path, days: float) -> None:
    stamp = time.time() - days * DAY
    os.utime(path, (stamp, stamp))


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _make_results(tmp_path: Path) -> Path:
    results = tmp_path / "results"
    results.mkdir(parents=True)
    return results


def _make_published_bots(tmp_path: Path, versions: list[int]) -> Path:
    bots = tmp_path / "bots"
    for version in versions:
        published = bots / f"national_cloud_v{version}"
        published.mkdir(parents=True, exist_ok=True)
        (published / ".completed").write_text("ok", encoding="utf-8")
    return bots


def _reaped_tree_receipts(results: Path) -> list[dict]:
    path = results / "reaped_generation_trees.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# (a) orphan never-published vN trees
# ---------------------------------------------------------------------------


def test_orphan_never_published_tree_reaped_with_receipt(tmp_path: Path):
    results = _make_results(tmp_path)
    bots = _make_published_bots(tmp_path, [188])
    orphan = results / "v400"
    _touch_tree(orphan, size=256)
    _age(orphan, 15)

    report = disk_hygiene.run_disk_hygiene(
        results,
        bots_dir=bots,
        min_free_bytes=1,
        orphan_result_tree_max_age_sec=ORPHAN_WINDOW,
    )

    assert not orphan.exists()
    assert report["removed"] >= 1
    assert report["bytes_freed"] >= 256
    receipts = _reaped_tree_receipts(results)
    assert [row["version"] for row in receipts] == [400]
    row = receipts[0]
    assert row["kind"] == "orphan_result_tree_reaped"
    assert row["reason"] == "prepared_never_published_no_live_reference_aged_out"
    assert row["tree"] == "v400"
    assert row["bytes_freed"] == 256
    assert row["orphan_max_age_sec"] == ORPHAN_WINDOW


def test_orphan_tree_younger_than_window_is_kept(tmp_path: Path):
    results = _make_results(tmp_path)
    bots = _make_published_bots(tmp_path, [188])
    young = results / "v400"
    _touch_tree(young)
    _age(young, 13)

    disk_hygiene.run_disk_hygiene(
        results,
        bots_dir=bots,
        min_free_bytes=1,
        orphan_result_tree_max_age_sec=ORPHAN_WINDOW,
    )

    assert young.exists()
    assert _reaped_tree_receipts(results) == []


def test_published_tree_is_never_reaped(tmp_path: Path):
    results = _make_results(tmp_path)
    bots = _make_published_bots(tmp_path, [400])
    tree = results / "v400"
    _touch_tree(tree)
    _age(tree, 30)

    disk_hygiene.run_disk_hygiene(
        results,
        bots_dir=bots,
        min_free_bytes=1,
        orphan_result_tree_max_age_sec=ORPHAN_WINDOW,
    )

    assert tree.exists()
    assert _reaped_tree_receipts(results) == []


def test_live_checkpoint_reference_excludes_orphan_reap(tmp_path: Path):
    results = _make_results(tmp_path)
    bots = _make_published_bots(tmp_path, [188])
    _write_json(
        results / "pipeline_state.json",
        {"next_v": 400, "source_v": 1, "stage": "prepared"},
    )
    live_tree = results / "v400"
    _touch_tree(live_tree)
    _age(live_tree, 20)
    source_tree = results / "v1"
    _touch_tree(source_tree)
    _age(source_tree, 20)

    disk_hygiene.run_disk_hygiene(
        results,
        bots_dir=bots,
        min_free_bytes=1,
        orphan_result_tree_max_age_sec=ORPHAN_WINDOW,
    )

    assert live_tree.exists()
    assert source_tree.exists()
    assert _reaped_tree_receipts(results) == []


def test_tree_with_strict_invocations_is_never_reaped(tmp_path: Path):
    results = _make_results(tmp_path)
    bots = _make_published_bots(tmp_path, [188])
    tree = results / "v401"
    invocation = tree / "logs" / "strict_invocations" / "abc123"
    invocation.mkdir(parents=True)
    (invocation / "master_io.txt").write_text("evidence", encoding="utf-8")
    _age(tree, 20)

    disk_hygiene.run_disk_hygiene(
        results,
        bots_dir=bots,
        min_free_bytes=1,
        orphan_result_tree_max_age_sec=ORPHAN_WINDOW,
    )

    assert tree.exists()
    assert (invocation / "master_io.txt").read_text(encoding="utf-8") == "evidence"
    assert _reaped_tree_receipts(results) == []


def test_abandoned_ledger_versions_do_not_use_orphan_receipts(tmp_path: Path):
    results = _make_results(tmp_path)
    bots = _make_published_bots(tmp_path, [188])
    ledger = results / "abandoned_versions.jsonl"
    rows = [
        {"version": 500, "reason": "x"},
        {"version": 501, "reason": "x"},
    ]
    ledger.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )
    for name in ("v500", "v501", "v600"):
        tree = results / name
        _touch_tree(tree)
        _age(tree, 20)

    disk_hygiene.run_disk_hygiene(
        results,
        bots_dir=bots,
        min_free_bytes=1,
        keep_abandoned_result_dirs=1,
        orphan_result_tree_max_age_sec=ORPHAN_WINDOW,
    )

    # v500: abandoned outside the keep window — deleted on ledger authority;
    # since 2026-10-28 it ALSO leaves a reap receipt (the evidence audit
    # found 24 abandoned trees vanished with zero receipts, silently
    # blanking every log-derived consumer window).
    assert not (results / "v500").exists()
    # v501: abandoned inside the keep window — kept.
    assert (results / "v501").exists()
    # v600: no ledger row at all — orphan branch, receipt required.
    assert not (results / "v600").exists()
    receipts = _reaped_tree_receipts(results)
    assert {row["version"] for row in receipts} == {500, 600}
    assert {row["kind"] for row in receipts} == {
        "abandoned_generation_tree_reaped",
        "orphan_result_tree_reaped",
    }


def test_consumer_checkpoint_reference_excludes_orphan_reap(tmp_path: Path):
    results = _make_results(tmp_path)
    bots = _make_published_bots(tmp_path, [188])
    tree = results / "v200"
    _touch_tree(tree)
    _age(tree, 20)
    _write_json(
        results / "pipeline_state_consumer-candidate-v200.json",
        {"next_v": 200, "stage": "review_running"},
    )

    disk_hygiene.run_disk_hygiene(
        results,
        bots_dir=bots,
        min_free_bytes=1,
        orphan_result_tree_max_age_sec=ORPHAN_WINDOW,
    )

    assert tree.exists()
    assert _reaped_tree_receipts(results) == []


def _make_lifecycle_db(path: Path, state: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE slice2b_candidate_lifecycle ("
            "candidate_id TEXT PRIMARY KEY, state TEXT)"
        )
        connection.execute(
            "INSERT INTO slice2b_candidate_lifecycle VALUES ('candidate-v300', ?)",
            (state,),
        )
        connection.commit()
    finally:
        connection.close()


def test_slice2b_nonterminal_lifecycle_reference_excludes_orphan_reap(
    tmp_path: Path,
):
    results = _make_results(tmp_path)
    _make_lifecycle_db(results / "workflow" / "slice2b_lifecycle.sqlite3", "sealed")
    tree = results / "v300"
    _touch_tree(tree)
    _age(tree, 20)

    disk_hygiene.run_disk_hygiene(
        results,
        min_free_bytes=1,
        orphan_result_tree_max_age_sec=ORPHAN_WINDOW,
    )

    assert tree.exists()
    assert _reaped_tree_receipts(results) == []

    # A terminal lifecycle row no longer pins the tree.
    db_path = results / "workflow" / "slice2b_lifecycle.sqlite3"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "UPDATE slice2b_candidate_lifecycle SET state = 'promoted'"
        )
        connection.commit()
    finally:
        connection.close()

    disk_hygiene.run_disk_hygiene(
        results,
        min_free_bytes=1,
        orphan_result_tree_max_age_sec=ORPHAN_WINDOW,
    )

    assert not tree.exists()
    assert [row["version"] for row in _reaped_tree_receipts(results)] == [300]


def test_unreadable_lifecycle_database_blocks_orphan_reap(tmp_path: Path):
    results = _make_results(tmp_path)
    db_path = results / "workflow" / "slice2b_lifecycle.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(b"this is not a sqlite database")
    tree = results / "v310"
    _touch_tree(tree)
    _age(tree, 20)

    disk_hygiene.run_disk_hygiene(
        results,
        min_free_bytes=1,
        orphan_result_tree_max_age_sec=ORPHAN_WINDOW,
    )

    assert tree.exists()
    assert _reaped_tree_receipts(results) == []


# ---------------------------------------------------------------------------
# (b) generation cost ledger compaction
# ---------------------------------------------------------------------------


def _usage_row(
    generation_id: str,
    event_id: str,
    ts: float,
    *,
    cost_usd: float = 0.5,
    input_tokens: int = 1000,
    output_tokens: int = 200,
) -> dict:
    return {
        "schema_version": 1,
        "kind": "usage",
        "entry_id": hashlib.sha256(
            f"{generation_id}\0{event_id}".encode("utf-8")
        ).hexdigest(),
        "event_id": event_id,
        "generation_id": generation_id,
        "role": "MASTER PROPOSAL mechanism",
        "cost_usd": cost_usd,
        "cost_known": cost_usd is not None,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "ts": ts,
    }


def _write_ledger(path: Path, chunks: list[bytes]) -> bytes:
    raw = b"".join(chunks)
    path.write_bytes(raw)
    return raw


def _row_line(row: dict) -> bytes:
    return json.dumps(row, sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\n"


def test_cost_compaction_loses_no_rows(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("POK_COST_LEDGER_COMPACTION_ENABLED", "1")
    results = _make_results(tmp_path)
    ledger = results / "generation_cost_ledger.jsonl"
    # Fixed UTC anchor so month bucketing never depends on the wall clock.
    now = calendar.timegm((2026, 10, 15, 12, 0, 0))
    ts_aug_a = calendar.timegm((2026, 8, 10, 0, 0, 0))
    ts_aug_b = calendar.timegm((2026, 8, 20, 0, 0, 0))
    ts_sep = calendar.timegm((2026, 9, 5, 0, 0, 0))
    recent_generation = "generation:10:workflow-v1"
    old_generation = "generation:9:workflow-v1"
    chunks = [
        _row_line(_usage_row(old_generation, "e-old-1", ts_aug_a, cost_usd=0.5)),
        _row_line(_usage_row(old_generation, "e-old-2", ts_aug_b, cost_usd=1.25)),
        _row_line(_usage_row(old_generation, "e-old-3", ts_sep, cost_usd=0.25)),
        _row_line(
            _usage_row(recent_generation, "e-new-1", now - 5 * DAY, cost_usd=2.0)
        ),
        _row_line(
            _usage_row(recent_generation, "e-new-2", now - 1 * DAY, cost_usd=3.0)
        ),
        # A row without ts is never compacted.
        _row_line({"schema_version": 1, "kind": "notice", "generation_id": recent_generation}),
        # Unparsable bytes are kept verbatim, never dropped.
        b"{not json at all\n",
        # A pre-existing monthly summary row must merge, not vanish.
        _row_line(
            {
                "schema_version": 1,
                "kind": "cost_ledger_monthly_summary",
                "month": "2026-08",
                "row_count": 7,
                "cost_usd_sum": 3.0,
                "cost_known_rows": 7,
                "cost_unknown_rows": 0,
                "input_tokens_sum": 700,
                "output_tokens_sum": 70,
                "generation_ids": ["generation:8:workflow-v1"],
                "first_ts": calendar.timegm((2026, 8, 1, 0, 0, 0)),
                "last_ts": calendar.timegm((2026, 8, 5, 0, 0, 0)),
                "kind_counts": {"usage": 7},
            }
        ),
    ]
    original_lines = [
        line for line in _write_ledger(ledger, chunks).splitlines(keepends=True)
    ]
    assert len(original_lines) == 8

    report = disk_hygiene.compact_generation_cost_ledger(
        results, now=now, retention_sec=30 * DAY, min_interval_sec=0.0
    )

    assert report["ok"] is True
    assert report["skipped"] is None
    assert report["compacted_rows"] == 3
    assert report["kept_recent_lines"] == 4  # 2 recent + no-ts row + unparsable
    assert report["bytes_freed"] > 0
    assert report["bytes_after"] < report["bytes_before"]

    new_lines = ledger.read_bytes().splitlines(keepends=True)
    receipt = json.loads(Path(report["receipt_path"]).read_text(encoding="utf-8"))
    assert receipt["kind"] == "cost_ledger_compaction"
    assert receipt["compacted_rows"] == 3
    assert receipt["kept_recent_lines"] == 4
    assert receipt["ledger_sha256_before"] == hashlib.sha256(
        b"".join(original_lines)
    ).hexdigest()
    assert receipt["merged_into_existing_summary_months"] == ["2026-08"]
    assert receipt["new_summary_months"] == ["2026-09"]

    # No-drop accounting: every original line is either still verbatim in
    # the ledger, has its line digest in the per-row receipt list, or is a
    # replaced pre-existing summary line whose digest is receipted.
    kept_hashes = {hashlib.sha256(line).hexdigest() for line in new_lines}
    receipt_hashes = {row["line_sha256"] for row in receipt["rows"]}
    replaced_summary_hashes = set(receipt["replaced_summary_line_hashes"])
    assert len(replaced_summary_hashes) == 1  # the seeded 2026-08 summary line
    for line in original_lines:
        line_hash = hashlib.sha256(line).hexdigest()
        assert (
            line_hash in kept_hashes
            or line_hash in receipt_hashes
            or line_hash in replaced_summary_hashes
        )

    # Summary rows first, then the retained full lines in original order.
    assert len(new_lines) == 6
    assert new_lines[2:] == original_lines[3:7]

    def _parse(line: bytes):
        try:
            return json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None

    parsed = [item for item in map(_parse, new_lines) if isinstance(item, dict)]
    summaries = [row for row in parsed if row.get("kind") == "cost_ledger_monthly_summary"]
    assert [row["month"] for row in summaries] == ["2026-08", "2026-09"]
    merged = summaries[0]
    assert merged["row_count"] == 7 + 2  # seeded summary + both August rows
    assert merged["cost_usd_sum"] == 3.0 + 0.5 + 1.25
    assert merged["generation_ids"] == sorted(
        {"generation:8:workflow-v1", old_generation}
    )
    assert merged["kind_counts"] == {"usage": 9}
    september = summaries[1]
    assert september["row_count"] == 1
    assert september["cost_usd_sum"] == 0.25

    # The unparsable line survives verbatim.
    assert b"{not json at all\n" in new_lines
    # Receipt per-row summaries identify every compacted row.
    assert {row["entry_id"] for row in receipt["rows"]} == {
        _usage_row(old_generation, "e-old-1", ts_aug_a)["entry_id"],
        _usage_row(old_generation, "e-old-2", ts_aug_b)["entry_id"],
        _usage_row(old_generation, "e-old-3", ts_sep)["entry_id"],
    }
    # Self-digesting receipt.
    unsigned = {
        key: value for key, value in receipt.items() if key != "receipt_digest"
    }
    expected = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert receipt["receipt_digest"] == expected


def test_cost_compaction_preserves_malformed_rows_verbatim(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("POK_COST_LEDGER_COMPACTION_ENABLED", "1")
    results = _make_results(tmp_path)
    ledger = results / "generation_cost_ledger.jsonl"
    now = calendar.timegm((2026, 10, 15, 12, 0, 0))
    malformed_summary = (
        b'{"kind": "cost_ledger_monthly_summary", "month": "not-a-month", '
        b'"row_count": 5}\n'
    )
    blank_line = b"\n"
    original_lines = _write_ledger(
        ledger,
        [
            _row_line(_usage_row("generation:9:workflow-v1", "e-old-1", now - 60 * DAY)),
            malformed_summary,
            blank_line,
            _row_line(_usage_row("generation:10:workflow-v1", "e-new-1", now - DAY)),
        ],
    ).splitlines(keepends=True)

    report = disk_hygiene.compact_generation_cost_ledger(
        results, now=now, retention_sec=30 * DAY, min_interval_sec=0.0
    )

    assert report["ok"] is True
    assert report["compacted_rows"] == 1
    assert report["kept_recent_lines"] == 3
    new_lines = ledger.read_bytes().splitlines(keepends=True)
    # The malformed-month summary line and the blank line survive verbatim.
    assert malformed_summary in new_lines
    assert blank_line in new_lines
    receipt = json.loads(Path(report["receipt_path"]).read_text(encoding="utf-8"))
    kept_hashes = {hashlib.sha256(line).hexdigest() for line in new_lines}
    receipt_hashes = {row["line_sha256"] for row in receipt["rows"]}
    for line in original_lines:
        line_hash = hashlib.sha256(line).hexdigest()
        assert (
            line_hash in kept_hashes
            or line_hash in receipt_hashes
            or line_hash in set(receipt["replaced_summary_line_hashes"])
        )


def test_cost_compaction_second_pass_is_noop(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("POK_COST_LEDGER_COMPACTION_ENABLED", "1")
    results = _make_results(tmp_path)
    ledger = results / "generation_cost_ledger.jsonl"
    now = time.time()
    _write_ledger(
        ledger,
        [
            _row_line(_usage_row("generation:9:workflow-v1", "e-old-1", now - 60 * DAY)),
            _row_line(
                _usage_row("generation:10:workflow-v1", "e-new-1", now - 2 * DAY)
            ),
        ],
    )

    first = disk_hygiene.compact_generation_cost_ledger(
        results, now=now, retention_sec=30 * DAY, min_interval_sec=0.0
    )
    assert first["ok"] is True and first["compacted_rows"] == 1
    snapshot = ledger.read_bytes()
    receipts_dir = results / "cost_ledger_compaction_receipts"
    first_receipts = sorted(path.name for path in receipts_dir.iterdir())

    second = disk_hygiene.compact_generation_cost_ledger(
        results, now=now + 60, retention_sec=30 * DAY, min_interval_sec=0.0
    )

    assert second["ok"] is True
    assert second["skipped"] == "nothing_to_compact"
    assert ledger.read_bytes() == snapshot
    assert sorted(path.name for path in receipts_dir.iterdir()) == first_receipts


def test_cost_compaction_interval_gate(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("POK_COST_LEDGER_COMPACTION_ENABLED", "1")
    results = _make_results(tmp_path)
    ledger = results / "generation_cost_ledger.jsonl"
    now = time.time()
    _write_ledger(
        ledger,
        [_row_line(_usage_row("generation:9:workflow-v1", "e-old-1", now - 60 * DAY))],
    )
    disk_hygiene.compact_generation_cost_ledger(
        results, now=now, retention_sec=30 * DAY, min_interval_sec=0.0
    )

    gated = disk_hygiene.compact_generation_cost_ledger(
        results, now=now, retention_sec=30 * DAY, min_interval_sec=3600.0
    )

    assert gated["ok"] is True
    assert gated["skipped"] == "interval"
    assert gated["bytes_freed"] == 0


def test_cost_reader_sees_recent_rows_after_compaction(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("POK_COST_LEDGER_COMPACTION_ENABLED", "1")
    results = _make_results(tmp_path)
    ledger = results / "generation_cost_ledger.jsonl"
    now = time.time()
    recent_generation = "generation:10:workflow-v1"
    _write_ledger(
        ledger,
        [
            _row_line(
                _usage_row("generation:9:workflow-v1", "e-old-1", now - 60 * DAY)
            ),
            _row_line(_usage_row(recent_generation, "e-new-1", now - 2 * DAY)),
            _row_line(_usage_row(recent_generation, "e-new-2", now - 1 * DAY)),
        ],
    )
    report = disk_hygiene.compact_generation_cost_ledger(
        results, now=now, retention_sec=30 * DAY, min_interval_sec=0.0
    )
    assert report["compacted_rows"] == 1

    import orchestrator_cost_policy as cost_policy

    monkeypatch.setattr(cost_policy, "COST_LEDGER_FILE", ledger)
    entries, errors, historical = cost_policy._read_generation_entries(
        recent_generation
    )
    assert errors == ()
    assert historical == 0
    assert [row["event_id"] for row in entries] == ["e-new-1", "e-new-2"]
    old_entries, old_errors, old_historical = (
        cost_policy._read_generation_entries("generation:9:workflow-v1")
    )
    assert old_errors == ()
    assert old_entries == []


def test_run_disk_hygiene_wires_cost_compaction(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("POK_COST_LEDGER_COMPACTION_ENABLED", "1")
    results = _make_results(tmp_path)
    ledger = results / "generation_cost_ledger.jsonl"
    now = time.time()
    raw = _write_ledger(
        ledger,
        [
            _row_line(
                _usage_row("generation:9:workflow-v1", f"e-old-{i}", now - 40 * DAY)
            )
            for i in range(20)
        ]
        + [
            _row_line(
                _usage_row("generation:10:workflow-v1", "e-new-1", now - 1 * DAY)
            )
        ],
    )

    report = disk_hygiene.run_disk_hygiene(results, min_free_bytes=1)

    cost = report["cost_ledger_compaction"]
    assert cost["ok"] is True
    assert cost["compacted_rows"] == 20
    assert cost["bytes_freed"] > 0
    assert ledger.stat().st_size < len(raw)
    receipts_dir = results / "cost_ledger_compaction_receipts"
    assert list(receipts_dir.glob("*.json"))
    # The recent row is untouched.
    assert (
        _row_line(_usage_row("generation:10:workflow-v1", "e-new-1", now - 1 * DAY))
        in ledger.read_bytes().splitlines(keepends=True)
    )


def test_unreadable_primary_checkpoint_blocks_orphan_reap(tmp_path: Path):
    # Red-team probe 2026-10-15: a present-but-unparsable pipeline_state.json
    # used to silently empty the live set, making the live generation's own
    # tree orphan-reapable. The orphan branch must now fail closed exactly
    # like the unreadable lifecycle-database case.
    results = _make_results(tmp_path)
    bots = _make_published_bots(tmp_path, [188])
    orphan = results / "v400"
    _touch_tree(orphan, size=256)
    _age(orphan, 15)
    (results / "pipeline_state.json").write_text(
        "{corrupt not json", encoding="utf-8"
    )

    disk_hygiene.run_disk_hygiene(
        results,
        bots_dir=bots,
        min_free_bytes=1,
        orphan_result_tree_max_age_sec=ORPHAN_WINDOW,
    )

    assert orphan.exists()
    assert _reaped_tree_receipts(results) == []


def test_run_disk_hygiene_wires_candidate_ledger_compaction(
    tmp_path: Path, monkeypatch
):
    # Red-team finding 2026-10-15: compact_candidate_ledger shipped with zero
    # production callers. The janitor now owns its cadence.
    results = _make_results(tmp_path)
    (results / "candidates.jsonl").write_text("{}", encoding="utf-8")
    calls: list[tuple] = []
    import candidate_store

    def _fake_compact(*args, **kwargs):
        calls.append((args, kwargs))
        return {"receipt_persisted": False}

    monkeypatch.setattr(
        candidate_store, "compact_candidate_ledger", _fake_compact
    )

    disk_hygiene.run_disk_hygiene(results, min_free_bytes=1)

    assert len(calls) == 1
    assert calls[0][0][0].name == "candidates.jsonl"


# ---------------------------------------------------------------------------
# (c) candidate-ledger retention window env knob (G1, 2026-10-20)
# ---------------------------------------------------------------------------

WINDOW_ENV = "POK_CANDIDATE_LEDGER_KEEP_VERSIONS"
SCRIPT_PATH = (
    Path(disk_hygiene.__file__).resolve().parents[2]
    / "scripts"
    / "compact_candidate_ledger.py"
)


def _fake_compaction_collector(calls):
    import candidate_store

    def _fake_compact(*args, **kwargs):
        calls.append((args, kwargs))
        return {"receipt_persisted": False}

    return _fake_compact, candidate_store


def _seed_window_ledger(ledger: Path, versions) -> None:
    from candidate_store import append_candidate_event

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
            scorecard={
                "name": "quality",
                "passed": version % 2 == 0,
                "filler": "q" * 4096,
            },
            metrics={
                "all_passed": version % 2 == 0,
                "decision_pass_rate": round(0.5 + version / 1000, 4),
            },
            path=ledger,
        )


def test_candidate_ledger_step_env_window_reaches_compaction(
    tmp_path: Path, monkeypatch
):
    # G1: the janitor's production call used the hard-coded library default
    # (50). POK_CANDIDATE_LEDGER_KEEP_VERSIONS now tightens the window at the
    # janitor call site — identical production behavior to changing the
    # candidate_store signature default, without touching that module.
    results = _make_results(tmp_path)
    (results / "candidates.jsonl").write_text("{}", encoding="utf-8")
    calls: list[tuple] = []
    fake, candidate_store = _fake_compaction_collector(calls)
    monkeypatch.setattr(candidate_store, "compact_candidate_ledger", fake)
    monkeypatch.setenv(WINDOW_ENV, "7")

    disk_hygiene.run_disk_hygiene(results, min_free_bytes=1)

    assert len(calls) == 1
    assert calls[0][1]["keep_versions"] == 7


def test_candidate_ledger_step_env_window_clamps_and_falls_back(
    tmp_path: Path, monkeypatch
):
    results = _make_results(tmp_path)
    (results / "candidates.jsonl").write_text("{}", encoding="utf-8")
    seen: list[int | None] = []
    fake, candidate_store = _fake_compaction_collector(seen)
    monkeypatch.setattr(candidate_store, "compact_candidate_ledger", fake)
    from candidate_store import DEFAULT_KEEP_VERSIONS

    for raw, expected in (
        ("0", 1),
        ("-5", 1),
        (" 12 ", 12),
        ("garbage", DEFAULT_KEEP_VERSIONS),
    ):
        seen.clear()
        monkeypatch.setenv(WINDOW_ENV, raw)
        disk_hygiene.run_disk_hygiene(results, min_free_bytes=1)
        windows = [kwargs.get("keep_versions") for _args, kwargs in seen]
        assert windows == [expected], raw

    # Unset: the library default is still forwarded explicitly so the
    # compaction receipt always records the effective window.
    seen.clear()
    monkeypatch.delenv(WINDOW_ENV, raising=False)
    disk_hygiene.run_disk_hygiene(results, min_free_bytes=1)
    windows = [kwargs.get("keep_versions") for _args, kwargs in seen]
    assert windows == [DEFAULT_KEEP_VERSIONS]


def test_run_disk_hygiene_applies_env_candidate_window_to_real_ledger(
    tmp_path: Path, monkeypatch
):
    # End to end through the canonical compact_candidate_ledger transaction:
    # the env window must reach the receipt and actually compact old
    # quality_finished rows.
    results = _make_results(tmp_path)
    ledger = results / "candidates.jsonl"
    _seed_window_ledger(ledger, range(1, 16))
    before = ledger.read_bytes()
    monkeypatch.setenv(WINDOW_ENV, "5")

    report = disk_hygiene.run_disk_hygiene(results, min_free_bytes=1)

    compaction = report["candidate_ledger_compaction"]
    assert compaction["keep_versions"] == 5
    assert compaction["version_floor"] == 11
    assert compaction["rows_compacted"] == 10  # quality_finished v1..v10
    assert compaction["receipt_persisted"] is True
    assert len(ledger.read_bytes()) < len(before)
    receipts = (
        results / "candidates_compaction_receipt.jsonl"
    ).read_text(encoding="utf-8")
    assert json.loads(receipts.strip().splitlines()[-1])["keep_versions"] == 5


def test_compact_candidate_ledger_script_dry_run_reads_only(tmp_path: Path):
    # Operator one-shot entry (review follow-up: no bare `python -c`): dry run
    # is lock-free and byte-preserving; it writes no receipt and compacts
    # nothing.
    results = _make_results(tmp_path)
    ledger = results / "candidates.jsonl"
    _seed_window_ledger(ledger, range(1, 31))
    before = ledger.read_bytes()

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--ledger",
            str(ledger),
            "--keep-versions",
            "10",
        ],
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    plan_lines = [
        line for line in proc.stdout.splitlines() if line.startswith("PLAN ")
    ]
    assert len(plan_lines) == 1
    plan = json.loads(plan_lines[0][len("PLAN ") :])
    assert plan["keep_versions"] == 10
    assert plan["version_floor"] == 21  # versions 30..21 stay full
    assert plan["rows_to_compact"] == 20
    assert ledger.read_bytes() == before
    assert not (results / "candidates_compaction_receipt.jsonl").exists()


def test_compact_candidate_ledger_script_execute_compacts(tmp_path: Path):
    results = _make_results(tmp_path)
    ledger = results / "candidates.jsonl"
    _seed_window_ledger(ledger, range(1, 31))

    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--ledger",
            str(ledger),
            "--keep-versions",
            "10",
            "--execute",
        ],
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    receipts_path = results / "candidates_compaction_receipt.jsonl"
    assert receipts_path.exists()
    last = json.loads(
        receipts_path.read_text(encoding="utf-8").strip().splitlines()[-1]
    )
    assert last["keep_versions"] == 10
    assert last["rows_compacted"] == 20  # quality_finished v1..v20
    assert last["receipt_persisted"] is True
    # Old rows became summary rows; the ledger shrank.
    rows = [
        json.loads(line)
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    summarized = [
        row for row in rows if row.get("ledger_compaction")
    ]
    assert len(summarized) == 20
    assert all(row["version"] < 21 for row in summarized)
