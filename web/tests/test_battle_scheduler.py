"""Tests for battle_scheduler.py — file-based battle job queue."""

import json
import threading
import time
from pathlib import Path

import pytest

from battle_scheduler import (
    BattleJob,
    BattleResult,
    _append_jsonl,
    _read_jsonl,
    _write_jsonl_atomic,
    ack_claimed,
    cleanup_stale,
    collect_results,
    drain_pending_jobs,
    requeue_unclaimed_on_startup,
    submit_jobs,
    write_result,
)


@pytest.fixture(autouse=True)
def patch_results_dir(monkeypatch, tmp_path):
    """Redirect all scheduler file paths into tmp_path."""
    import battle_scheduler

    monkeypatch.setattr(battle_scheduler, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(battle_scheduler, "BATTLE_JOBS_FILE", tmp_path / "battle_jobs.jsonl")
    monkeypatch.setattr(battle_scheduler, "BATTLE_CLAIMED_FILE", tmp_path / "battle_jobs.claimed")
    monkeypatch.setattr(battle_scheduler, "BATTLE_RESULTS_FILE", tmp_path / "battle_results.jsonl")


# ── Low-level helpers ──

class TestAppendJsonl:
    def test_creates_file(self, tmp_path):
        f = tmp_path / "test.jsonl"
        _append_jsonl(f, [{"a": 1}])
        assert f.exists()
        assert _read_jsonl(f) == [{"a": 1}]

    def test_appends_multiple(self, tmp_path):
        f = tmp_path / "test.jsonl"
        _append_jsonl(f, [{"a": 1}, {"b": 2}])
        assert len(_read_jsonl(f)) == 2


class TestReadJsonl:
    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.jsonl"
        f.write_text("")
        assert _read_jsonl(f) == []

    def test_missing_file(self, tmp_path):
        f = tmp_path / "missing.jsonl"
        assert _read_jsonl(f) == []

    def test_malformed_jsonl_skip(self, tmp_path, caplog):
        import logging
        caplog.set_level(logging.WARNING, logger="pok.scheduler")
        f = tmp_path / "bad.jsonl"
        f.write_text('{"a": 1}\nnot json\n{"b": 2}\n')
        result = _read_jsonl(f)
        assert len(result) == 2
        assert result[0]["a"] == 1
        assert result[1]["b"] == 2
        assert "Malformed JSON line" in caplog.text


class TestWriteJsonlAtomic:
    def test_overwrites(self, tmp_path):
        f = tmp_path / "atomic.jsonl"
        f.write_text('{"old": 1}\n')
        _write_jsonl_atomic(f, [{"new": 2}])
        assert _read_jsonl(f) == [{"new": 2}]


# ── submit_jobs ──

class TestSubmitJobs:
    def test_submit_single_job(self, tmp_path):
        job = BattleJob(
            job_id="j1",
            bot_a_name="bot_a",
            bot_b_name="bot_b",
            bot_a_path=str(tmp_path / "bot_a.py"),
            bot_b_path=str(tmp_path / "bot_b.py"),
            n_pairs=5,
            submitted_at=time.time(),
        )
        ids = submit_jobs([job])
        assert ids == ["j1"]
        pending = _read_jsonl(tmp_path / "battle_jobs.jsonl")
        assert len(pending) == 1
        assert pending[0]["job_id"] == "j1"

    def test_submit_batch_jobs(self, tmp_path):
        jobs = [
            BattleJob(
                job_id=f"j{i}",
                bot_a_name="bot_a",
                bot_b_name="bot_b",
                bot_a_path=str(tmp_path / "bot_a.py"),
                bot_b_path=str(tmp_path / "bot_b.py"),
                n_pairs=3,
                submitted_at=time.time(),
            )
            for i in range(3)
        ]
        ids = submit_jobs(jobs)
        assert len(ids) == 3
        pending = _read_jsonl(tmp_path / "battle_jobs.jsonl")
        assert len(pending) == 3

    def test_max_pending_jobs_rejection(self, tmp_path, monkeypatch):
        monkeypatch.setattr("battle_scheduler.MAX_PENDING_JOBS", 2)
        job = BattleJob(
            job_id="j1",
            bot_a_name="bot_a",
            bot_b_name="bot_b",
            bot_a_path=str(tmp_path / "bot_a.py"),
            bot_b_path=str(tmp_path / "bot_b.py"),
            n_pairs=1,
            submitted_at=time.time(),
        )
        submit_jobs([job, job])
        with pytest.raises(RuntimeError, match="Pending job limit exceeded"):
            submit_jobs([job])


# ── drain_pending_jobs ──

class TestDrainPendingJobs:
    def test_drain_pending_jobs(self, tmp_path):
        # Create bot files so they exist
        (tmp_path / "bot_a.py").write_text("# bot a")
        (tmp_path / "bot_b.py").write_text("# bot b")
        job = BattleJob(
            job_id="j1",
            bot_a_name="bot_a",
            bot_b_name="bot_b",
            bot_a_path=str(tmp_path / "bot_a.py"),
            bot_b_path=str(tmp_path / "bot_b.py"),
            n_pairs=5,
            submitted_at=time.time(),
        )
        submit_jobs([job])
        valid = drain_pending_jobs()
        assert len(valid) == 1
        assert valid[0]["job_id"] == "j1"
        # Pending file should be empty
        assert _read_jsonl(tmp_path / "battle_jobs.jsonl") == []
        # Claimed file should have the job
        claimed = _read_jsonl(tmp_path / "battle_jobs.claimed")
        assert len(claimed) == 1

    def test_drain_empty_file(self, tmp_path):
        valid = drain_pending_jobs()
        assert valid == []

    def test_bot_file_not_found_filtered(self, tmp_path):
        job = BattleJob(
            job_id="j1",
            bot_a_name="bot_a",
            bot_b_name="bot_b",
            bot_a_path=str(tmp_path / "missing_a.py"),
            bot_b_path=str(tmp_path / "missing_b.py"),
            n_pairs=5,
            submitted_at=time.time(),
        )
        submit_jobs([job])
        valid = drain_pending_jobs()
        assert valid == []
        # Result should contain not_found error
        results = _read_jsonl(tmp_path / "battle_results.jsonl")
        assert len(results) == 1
        assert results[0]["error"] == "not_found"

    def test_expired_job_filtered(self, tmp_path):
        (tmp_path / "bot_a.py").write_text("# bot a")
        (tmp_path / "bot_b.py").write_text("# bot b")
        job = BattleJob(
            job_id="j1",
            bot_a_name="bot_a",
            bot_b_name="bot_b",
            bot_a_path=str(tmp_path / "bot_a.py"),
            bot_b_path=str(tmp_path / "bot_b.py"),
            n_pairs=5,
            submitted_at=time.time() - 3600,  # 1 hour ago
        )
        submit_jobs([job])
        valid = drain_pending_jobs()
        assert valid == []
        results = _read_jsonl(tmp_path / "battle_results.jsonl")
        assert len(results) == 1
        assert results[0]["error"] == "expired"


# ── write_result / collect_result ──

class TestWriteAndCollectResult:
    def test_write_and_collect_result(self, tmp_path):
        # Seed a claimed job
        (tmp_path / "bot_a.py").write_text("# bot a")
        (tmp_path / "bot_b.py").write_text("# bot b")
        job = BattleJob(
            job_id="j1",
            bot_a_name="bot_a",
            bot_b_name="bot_b",
            bot_a_path=str(tmp_path / "bot_a.py"),
            bot_b_path=str(tmp_path / "bot_b.py"),
            n_pairs=5,
            submitted_at=time.time(),
        )
        submit_jobs([job])
        drain_pending_jobs()

        result = BattleResult(
            job_id="j1",
            wins_a=3,
            wins_b=1,
            draws=1,
            total=5,
        )
        write_result(result)

        # Claimed should be empty now
        assert _read_jsonl(tmp_path / "battle_jobs.claimed") == []

        collected = collect_results(["j1"])
        assert "j1" in collected
        assert collected["j1"]["wins_a"] == 3

    def test_collect_partial(self, tmp_path):
        # Write two results, collect only one
        _append_jsonl(tmp_path / "battle_results.jsonl", [
            {"job_id": "j1", "wins_a": 1, "wins_b": 0, "draws": 0, "total": 1, "completed_at": time.time()},
            {"job_id": "j2", "wins_a": 0, "wins_b": 1, "draws": 0, "total": 1, "completed_at": time.time()},
        ])
        collected = collect_results(["j1"])
        assert "j1" in collected
        assert "j2" not in collected
        # j2 should remain in results file
        remaining = _read_jsonl(tmp_path / "battle_results.jsonl")
        assert len(remaining) == 1
        assert remaining[0]["job_id"] == "j2"


# ── ack_claimed ──

class TestAckClaimed:
    def test_ack_claimed_removes_record(self, tmp_path):
        _append_jsonl(tmp_path / "battle_jobs.claimed", [
            {"job_id": "j1", "bot_a_name": "a"},
            {"job_id": "j2", "bot_a_name": "b"},
        ])
        ack_claimed("j1")
        claimed = _read_jsonl(tmp_path / "battle_jobs.claimed")
        assert len(claimed) == 1
        assert claimed[0]["job_id"] == "j2"


# ── cleanup_stale ──

class TestCleanupStale:
    def test_cleanup_stale(self, tmp_path):
        now = time.time()
        _append_jsonl(tmp_path / "battle_results.jsonl", [
            {"job_id": "old", "completed_at": now - 7200},
            {"job_id": "fresh", "completed_at": now - 60},
        ])
        removed = cleanup_stale(max_age_sec=3600)
        assert removed == 1
        remaining = _read_jsonl(tmp_path / "battle_results.jsonl")
        assert len(remaining) == 1
        assert remaining[0]["job_id"] == "fresh"


# ── requeue_unclaimed_on_startup ──

class TestRequeueUnclaimed:
    def test_requeue_unclaimed_on_startup(self, tmp_path):
        # j1 claimed but has no result -> orphaned
        # j2 claimed and has result -> not orphaned
        _append_jsonl(tmp_path / "battle_jobs.claimed", [
            {"job_id": "j1", "bot_a_name": "a"},
            {"job_id": "j2", "bot_a_name": "b"},
        ])
        _append_jsonl(tmp_path / "battle_results.jsonl", [
            {"job_id": "j2", "wins_a": 1, "wins_b": 0, "draws": 0, "total": 1, "completed_at": time.time()},
        ])
        orphaned = requeue_unclaimed_on_startup()
        assert len(orphaned) == 1
        assert orphaned[0]["job_id"] == "j1"


# ── concurrency ──

class TestConcurrentSubmit:
    def test_concurrent_submit_threads(self, tmp_path):
        errors = []
        ids_collected = []

        def worker(i):
            try:
                job = BattleJob(
                    job_id=f"t{i}",
                    bot_a_name="bot_a",
                    bot_b_name="bot_b",
                    bot_a_path=str(tmp_path / "bot_a.py"),
                    bot_b_path=str(tmp_path / "bot_b.py"),
                    n_pairs=1,
                    submitted_at=time.time(),
                )
                ids = submit_jobs([job])
                ids_collected.extend(ids)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(ids_collected) == 10
        pending = _read_jsonl(tmp_path / "battle_jobs.jsonl")
        assert len(pending) == 10
