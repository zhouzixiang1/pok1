"""Workflow journal retention janitor.

Covers the three contract legs of the retention window:

- a terminal, retention-aged, unreferenced run pair is deleted whole (rows,
  effects/outbox/inbox, exclusive content-addressed artifacts) with a
  self-digesting receipt;
- a run referenced by any live surface (checkpoint, Slice-2b lifecycle) is
  never touched, and an artifact digest shared with a survivor is kept;
- the surviving dual journals still satisfy the replay contract (contiguous
  seq, payload digests, stream_version, sole-last ``abandoned`` event).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

import disk_hygiene
import workflow_journal_hygiene as wjh
from workflow_kernel import WorkflowStore, content_digest

RETENTION_SEC = 30.0 * 24.0 * 3600.0
AGE_SEC = 40.0 * 24.0 * 3600.0
SUFFIX = ":strict-authority-v3"


def _age_runs(workflow_dir: Path, run_ids, age_sec: float = AGE_SEC) -> None:
    """Backdate a run's journal timestamps to simulate retention age."""
    stamp = time.time() - age_sec
    connection = sqlite3.connect(workflow_dir / "events.sqlite3")
    try:
        for run_id in run_ids:
            connection.execute(
                "UPDATE workflow_instances SET updated_at = ? WHERE run_id = ?",
                (stamp, run_id),
            )
            connection.execute(
                "UPDATE workflow_events SET created_at = ? WHERE run_id = ?",
                (stamp, run_id),
            )
            connection.execute(
                "UPDATE effects SET updated_at = ? WHERE run_id = ?",
                (stamp, run_id),
            )
        connection.commit()
    finally:
        connection.close()


def _digest(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _store(workflow_dir: Path) -> WorkflowStore:
    return WorkflowStore(workflow_dir / "events.sqlite3")


def _abandon_pair(workflow_dir: Path, base: str, *, digests=()) -> None:
    store = _store(workflow_dir)
    for run_id, event_type in (
        (base, "WorkerAbandoned"),
        (base + SUFFIX, "StrictAuthorityAbandoned"),
    ):
        store.ensure_instance(run_id, definition_version=3)
        for index, digest in enumerate(digests):
            store.append_event(
                run_id,
                "WorkerPrepared",
                {"artifact_hash": digest, "index": index},
                causation_id=f"prepared-{run_id}-{index}",
            )
        version = int(store.instance(run_id)["stream_version"])
        store.terminal_transition(
            run_id,
            event_type=event_type,
            payload={"reason": "test-abandon"},
            causation_id=f"terminal-{run_id}",
            expected_version=version,
            status="abandoned",
        )


def _complete_pair(workflow_dir: Path, base: str, *, digests=()) -> None:
    store = _store(workflow_dir)
    for run_id in (base, base + SUFFIX):
        store.ensure_instance(run_id, definition_version=3)
        for index, digest in enumerate(digests):
            store.append_event(
                run_id,
                "WorkerPrepared",
                {"artifact_hash": digest, "index": index},
                causation_id=f"prepared-{run_id}-{index}",
            )
        version = int(store.instance(run_id)["stream_version"])
        store.append_event_and_set_status(
            run_id,
            event_type="WorkerProjected",
            payload={"stage": "workers_done"},
            causation_id=f"projected-{run_id}",
            expected_version=version,
            status="completed",
        )


def _artifact(workflow_dir: Path, digest: str, size: int = 128) -> Path:
    path = workflow_dir / "artifacts" / digest
    path.mkdir(parents=True, exist_ok=True)
    (path / "blob.bin").write_bytes(b"x" * size)
    return path


def _checkpoint(results_dir: Path, payload: dict) -> Path:
    path = results_dir / "pipeline_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _receipt_bodies(workflow_dir: Path) -> list[dict]:
    receipts_dir = workflow_dir / "journal_retention_receipts"
    bodies = []
    for path in sorted(receipts_dir.glob("*.json")):
        bodies.append(json.loads(path.read_text(encoding="utf-8")))
    return bodies


def _instance_rows(workflow_dir: Path, table: str, column: str = "run_id"):
    connection = sqlite3.connect(workflow_dir / "events.sqlite3")
    try:
        if table in ("outbox", "inbox"):
            return [
                str(row[0])
                for row in connection.execute(
                    f"SELECT e.run_id FROM {table} t "
                    "JOIN effects e ON e.effect_id = t.effect_id"
                )
            ]
        return [
            str(row[0])
            for row in connection.execute(f"SELECT {column} FROM {table}")
        ]
    finally:
        connection.close()


def test_terminal_old_pair_cleaned_with_receipt_and_exclusive_artifacts(
    tmp_path: Path,
):
    results = tmp_path / "results"
    workflow = results / "workflow"
    old_base = "generation:101:workflow-v1"
    young_base = "generation:102:workflow-v1"
    live_base = "generation:103:workflow-v1"
    old_only = _digest("old-only")
    shared = _digest("shared")
    young_only = _digest("young-only")
    _abandon_pair(workflow, old_base, digests=[old_only, shared])
    _age_runs(workflow, [old_base, old_base + SUFFIX])
    _complete_pair(workflow, young_base, digests=[shared, young_only])
    _store(workflow).ensure_instance(live_base, definition_version=3)
    for digest in (old_only, shared, young_only):
        _artifact(workflow, digest)

    plan = wjh.plan_workflow_journal_retention(
        workflow, results_dir=results, retention_sec=RETENTION_SEC,
    )
    assert set(plan["delete_runs"]) == {old_base, old_base + SUFFIX}
    kept_reasons = {
        item["run_id"]: item["reason"] for item in plan["kept_runs"]
    }
    assert kept_reasons[young_base] == "too_young"
    assert kept_reasons[young_base + SUFFIX] == "too_young"
    assert kept_reasons[live_base] == "non_terminal"

    receipt = wjh.apply_workflow_journal_retention(
        workflow, plan, results_dir=results, retention_sec=RETENTION_SEC,
    )

    # Both journals of the old generation are gone from every table.
    for table in (
        "workflow_instances",
        "workflow_events",
        "effects",
        "outbox",
        "inbox",
    ):
        rows = _instance_rows(workflow, table)
        assert old_base not in rows
        assert old_base + SUFFIX not in rows
        if table == "workflow_instances":
            assert set(rows) == {
                young_base,
                young_base + SUFFIX,
                live_base,
            }

    # Exclusive content-addressed artifact removed; shared one kept.
    assert not (workflow / "artifacts" / old_only).exists()
    assert (workflow / "artifacts" / shared).is_dir()
    assert (workflow / "artifacts" / young_only).is_dir()
    assert receipt["deleted_artifact_digests"] == [old_only]
    assert receipt["kept_shared_artifact_digests"] == 1
    assert receipt["post_replay_verified"] is True
    assert receipt["runs_verified"] == 3

    # Receipt is durable and self-digesting.
    bodies = _receipt_bodies(workflow)
    assert len(bodies) == 1
    body = bodies[0]
    unsigned = {
        key: value for key, value in body.items() if key != "receipt_digest"
    }
    assert body["receipt_digest"] == content_digest(unsigned)
    assert body["kind"] == "workflow_journal_retention"
    assert {item["run_id"] for item in body["deleted_runs"]} == {
        old_base,
        old_base + SUFFIX,
    }


def test_live_checkpoint_reference_blocks_cleanup(tmp_path: Path):
    results = tmp_path / "results"
    workflow = results / "workflow"
    base = "generation:201:workflow-v1"
    digest = _digest("pinned")
    _abandon_pair(workflow, base, digests=[digest])
    _age_runs(workflow, [base, base + SUFFIX])
    _artifact(workflow, digest)
    _checkpoint(
        results,
        {
            "next_v": 201,
            "source_v": 200,
            "stage": "workers_running",
            "workflow_run_id": base,
            "candidate_artifact_hash": digest,
        },
    )

    live_runs, live_digests = wjh.live_workflow_run_references(results)
    assert base in live_runs
    assert base + SUFFIX in live_runs
    assert digest in live_digests

    plan = wjh.plan_workflow_journal_retention(
        workflow, results_dir=results, retention_sec=RETENTION_SEC,
    )
    assert plan["delete_runs"] == []
    reasons = {item["run_id"]: item["reason"] for item in plan["kept_runs"]}
    assert reasons[base] == "live_reference"
    assert reasons[base + SUFFIX] == "live_reference"

    receipt = wjh.apply_workflow_journal_retention(
        workflow, plan, results_dir=results, retention_sec=RETENTION_SEC,
    )
    assert receipt["deleted_runs"] == []
    assert receipt["deleted_artifact_digests"] == []
    assert _instance_rows(workflow, "workflow_instances") == [
        base,
        base + SUFFIX,
    ]
    assert (workflow / "artifacts" / digest).is_dir()
    events = _store(workflow).events(base)
    assert events[-1].event_type == "WorkerAbandoned"


def test_slice2b_sealed_candidate_pins_its_workflow_run(tmp_path: Path):
    results = tmp_path / "results"
    workflow = results / "workflow"
    base = "generation:202:workflow-v1"
    _abandon_pair(workflow, base)
    _age_runs(workflow, [base, base + SUFFIX])
    db_path = workflow / "slice2b_lifecycle.sqlite3"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            CREATE TABLE slice2b_candidate_lifecycle (
                candidate_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                envelope_effect_id TEXT,
                sealed_snapshot_json TEXT,
                promotion_receipt_json TEXT,
                gate_results_json TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO slice2b_candidate_lifecycle(
                candidate_id, state, envelope_effect_id, sealed_snapshot_json
            ) VALUES (?, ?, ?, ?)
            """,
            (
                "consumer-candidate-v202",
                "sealed",
                f"worker:{base}:cycle-0:{_digest('effect')[:16]}",
                json.dumps({"workflow_run_id": base}),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    live_runs, _ = wjh.live_workflow_run_references(results)
    assert base in live_runs
    assert base + SUFFIX in live_runs
    plan = wjh.plan_workflow_journal_retention(
        workflow, results_dir=results, retention_sec=RETENTION_SEC,
    )
    assert plan["delete_runs"] == []


def test_dual_journal_consistency_after_cleanup(tmp_path: Path):
    results = tmp_path / "results"
    workflow = results / "workflow"
    gone_base = "generation:301:workflow-v1"
    kept_base = "generation:302:workflow-v1"
    _abandon_pair(workflow, gone_base, digests=[_digest("gone")])
    _age_runs(workflow, [gone_base, gone_base + SUFFIX])
    _artifact(workflow, _digest("gone"))
    # The kept pair's strict child is still running, so the pair rule keeps
    # BOTH journals of generation 302.
    _abandon_pair(workflow, kept_base)
    _age_runs(workflow, [kept_base, kept_base + SUFFIX])
    _store(workflow).set_instance_status(kept_base + SUFFIX, "running")

    plan = wjh.plan_workflow_journal_retention(
        workflow, results_dir=results, retention_sec=RETENTION_SEC,
    )
    assert set(plan["delete_runs"]) == {gone_base, gone_base + SUFFIX}
    reasons = {item["run_id"]: item["reason"] for item in plan["kept_runs"]}
    assert reasons[kept_base] == "pair_not_deletable"
    assert reasons[kept_base + SUFFIX] == "non_terminal"

    receipt = wjh.apply_workflow_journal_retention(
        workflow, plan, results_dir=results, retention_sec=RETENTION_SEC,
    )
    assert receipt["post_replay_verified"] is True

    store = _store(workflow)
    remaining = _instance_rows(workflow, "workflow_instances")
    assert set(remaining) == {kept_base, kept_base + SUFFIX}
    for run_id in remaining:
        events = store.events(run_id)  # raises on any replay violation
        instance = store.instance(run_id)
        assert int(instance["stream_version"]) == len(events)
    kept_events = store.events(kept_base)
    terminal = [
        event
        for event in kept_events
        if event.event_type == "WorkerAbandoned"
    ]
    assert len(terminal) == 1
    assert terminal[0].seq == len(kept_events)


def test_busy_command_lock_keeps_the_pair(tmp_path: Path):
    results = tmp_path / "results"
    workflow = results / "workflow"
    base = "generation:401:workflow-v1"
    digest = _digest("locked")
    _abandon_pair(workflow, base, digests=[digest])
    _age_runs(workflow, [base, base + SUFFIX])
    _artifact(workflow, digest)
    _store(workflow)  # initialize the lock directory tree

    lock_dir = workflow / "workflow_locks"
    child_lock = lock_dir / (
        hashlib.sha256((base + SUFFIX).encode("utf-8")).hexdigest() + ".lock"
    )
    descriptor = os.open(child_lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        plan = wjh.plan_workflow_journal_retention(
            workflow, results_dir=results, retention_sec=RETENTION_SEC,
        )
        receipt = wjh.apply_workflow_journal_retention(
            workflow, plan, results_dir=results, retention_sec=RETENTION_SEC,
        )
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    skipped = {
        item["run_id"]: item["reason"] for item in receipt["skipped_runs"]
    }
    assert skipped[base + SUFFIX] == "command_lock_busy"
    assert skipped[base] == "strict_pair_not_deleted"
    assert receipt["deleted_runs"] == []
    assert receipt["deleted_artifact_digests"] == []
    assert set(_instance_rows(workflow, "workflow_instances")) == {
        base,
        base + SUFFIX,
    }
    assert (workflow / "artifacts" / digest).is_dir()


def test_run_disk_hygiene_wires_the_journal_step(tmp_path: Path, monkeypatch):
    results = tmp_path / "results"
    workflow = results / "workflow"
    base = "generation:501:workflow-v1"
    digest = _digest("wired")
    _abandon_pair(workflow, base, digests=[digest])
    _age_runs(workflow, [base, base + SUFFIX])
    _artifact(workflow, digest)
    monkeypatch.setenv("POK_WORKFLOW_JOURNAL_HYGIENE_MIN_INTERVAL_SEC", "0")

    report = disk_hygiene.run_disk_hygiene(results, min_free_bytes=1)
    journal = report["workflow_journal"]
    assert journal["ok"] is True
    assert journal["deleted_runs"] == 2
    assert not _instance_rows(workflow, "workflow_instances")

    # The interval gate suppresses an immediate second pass.
    monkeypatch.setenv("POK_WORKFLOW_JOURNAL_HYGIENE_MIN_INTERVAL_SEC", "3600")
    second = wjh.run_workflow_journal_hygiene(results)
    assert second["skipped"] == "interval"


def test_disabled_flag_and_missing_db(tmp_path: Path, monkeypatch):
    results = tmp_path / "results"
    monkeypatch.setenv("POK_WORKFLOW_JOURNAL_HYGIENE_ENABLED", "0")
    assert wjh.run_workflow_journal_hygiene(results)["skipped"] == "disabled"
    monkeypatch.setenv("POK_WORKFLOW_JOURNAL_HYGIENE_ENABLED", "1")
    assert wjh.run_workflow_journal_hygiene(results)["skipped"] == (
        "workflow_db_missing"
    )


def test_unreadable_checkpoint_fails_closed(tmp_path: Path):
    results = tmp_path / "results"
    results.mkdir(parents=True)
    (results / "pipeline_state.json").write_text("{not-json", encoding="utf-8")
    with pytest.raises(RuntimeError):
        wjh.live_workflow_run_references(results)
