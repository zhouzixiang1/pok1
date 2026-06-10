"""Tests for /api/scheduler/status and /api/scheduler/results endpoints."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest


def _append_jsonl(path: Path, records: list[dict]) -> None:
    """Helper to append JSONL records to a file (creates if missing)."""
    with open(path, "a") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


class TestSchedulerStatus:
    def test_returns_valid_structure(self, client):
        resp = client.get("/api/scheduler/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "pending_jobs" in data
        assert "claimed_jobs" in data
        assert "recent_results" in data
        assert "pending_details" in data
        assert isinstance(data["pending_jobs"], int)
        assert isinstance(data["claimed_jobs"], int)
        assert isinstance(data["recent_results"], int)
        assert isinstance(data["pending_details"], list)

    def test_empty_when_no_files(self, client):
        resp = client.get("/api/scheduler/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["pending_jobs"] == 0
        assert data["claimed_jobs"] == 0
        assert data["recent_results"] == 0

    def test_counts_match_file_contents(self, client, tmp_path):
        import battle_scheduler
        jobs_file = tmp_path / "battle_jobs.jsonl"
        claimed_file = tmp_path / "battle_jobs.claimed"
        results_file = tmp_path / "battle_results.jsonl"

        _append_jsonl(jobs_file, [{"job_id": f"j{i}"} for i in range(3)])
        _append_jsonl(claimed_file, [{"job_id": "c0"}])
        _append_jsonl(results_file, [{"job_id": f"r{i}"} for i in range(2)])

        with patch.object(battle_scheduler, "BATTLE_JOBS_FILE", jobs_file), \
             patch.object(battle_scheduler, "BATTLE_CLAIMED_FILE", claimed_file), \
             patch.object(battle_scheduler, "BATTLE_RESULTS_FILE", results_file):
            resp = client.get("/api/scheduler/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["pending_jobs"] == 3
        assert data["claimed_jobs"] == 1
        assert data["recent_results"] == 2
        assert len(data["pending_details"]) == 3


class TestSchedulerResults:
    def test_returns_valid_structure(self, client):
        resp = client.get("/api/scheduler/results")
        assert resp.status_code == 200
        data = resp.json()
        assert "results" in data
        assert isinstance(data["results"], list)

    def test_empty_when_no_file(self, client):
        resp = client.get("/api/scheduler/results")
        assert resp.status_code == 200
        data = resp.json()
        assert data["results"] == []

    def test_limit_parameter(self, client, tmp_path):
        import battle_scheduler
        results_file = tmp_path / "battle_results.jsonl"
        _append_jsonl(results_file, [{"job_id": f"r{i}"} for i in range(10)])

        with patch.object(battle_scheduler, "BATTLE_RESULTS_FILE", results_file):
            resp = client.get("/api/scheduler/results?limit=3")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 3
        # Should be the last 3: r7, r8, r9
        assert data["results"][0]["job_id"] == "r7"
        assert data["results"][2]["job_id"] == "r9"

    def test_default_limit_is_20(self, client, tmp_path):
        import battle_scheduler
        results_file = tmp_path / "battle_results.jsonl"
        _append_jsonl(results_file, [{"job_id": f"r{i}"} for i in range(25)])

        with patch.object(battle_scheduler, "BATTLE_RESULTS_FILE", results_file):
            resp = client.get("/api/scheduler/results")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 20
        # Last 20 of 25: r5 through r24
        assert data["results"][0]["job_id"] == "r5"
        assert data["results"][-1]["job_id"] == "r24"
