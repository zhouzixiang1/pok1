"""Tests for /api/pipeline/* endpoints."""

import json


class TestPipelineCheckpoint:
    def test_returns_data(self, client):
        resp = client.get("/api/pipeline/checkpoint")
        assert resp.status_code == 200
        # No pipeline_state.json in isolated tmp, so should be None
        data = resp.json()
        assert data is None, f"Expected no pipeline checkpoint in test, got: {data}"

    def test_legacy_checkpoint_is_not_presented_as_current(self, client):
        # A pre-policy shape is operator archive/reset evidence, not a current
        # national_tcp_policy_v1 checkpoint.
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
        assert resp.json() is None


class TestPipelineFailures:
    def test_returns_list(self, client):
        resp = client.get("/api/pipeline/failures")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_with_limit(self, client):
        resp = client.get("/api/pipeline/failures?limit=5")
        assert resp.status_code == 200
        assert len(resp.json()) <= 5

    def test_unbound_failure_row_is_hidden(self, client):
        # Generation numbers are not an epoch/workflow identity and must never
        # be upgraded by the reader.
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
        assert resp.json() == []
