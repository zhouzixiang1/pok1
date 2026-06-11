"""Tests for /api/pipeline/* endpoints."""

import json


class TestPipelineCheckpoint:
    def test_returns_data(self, client):
        resp = client.get("/api/pipeline/checkpoint")
        assert resp.status_code == 200
        # No pipeline_state.json in isolated tmp, so should be None
        data = resp.json()
        assert data is None, f"Expected no pipeline checkpoint in test, got: {data}"

    def test_has_expected_fields(self, client, monkeypatch):
        # Write a sample pipeline_state.json so the endpoint has data to return
        from server.routes import pipeline

        sample = {
            "next_v": 11,
            "source_v": 10,
            "stage": "master_planned",
            "gate_results": {},
        }
        pipeline.PIPELINE_STATE_FILE.write_text(json.dumps(sample))

        resp = client.get("/api/pipeline/checkpoint")
        assert resp.status_code == 200
        data = resp.json()
        assert data is not None
        assert "stage" in data
        assert "next_v" in data
        assert "source_v" in data


class TestPipelineFailures:
    def test_returns_list(self, client):
        resp = client.get("/api/pipeline/failures")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_with_limit(self, client):
        resp = client.get("/api/pipeline/failures?limit=5")
        assert resp.status_code == 200
        assert len(resp.json()) <= 5

    def test_failure_entry_fields(self, client):
        # Write sample worker_failures.jsonl so the endpoint has data to return
        from server.routes import pipeline

        sample_entry = json.dumps({
            "gen": 10,
            "worker_id": 1,
            "error": "test error",
            "failure_type": "unknown",
        })
        pipeline.WORKER_FAILURES_FILE.write_text(sample_entry + "\n")

        resp = client.get("/api/pipeline/failures?limit=1")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        entry = data[0]
        assert "gen" in entry
        assert "worker_id" in entry
        assert "error" in entry
