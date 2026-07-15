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

    def test_current_checkpoint_projection_carries_revision(self, client, monkeypatch):
        from server.routes import pipeline

        checkpoint = {
            "checkpoint_schema_version": 2,
            "evaluation_epoch": "national_tcp_policy_v1",
            "checkpoint_revision": 7,
            "next_v": 143,
            "source_v": 142,
            "stage": "reviewed",
            "workflow_run_id": "workflow-v1",
            "run_id": "143#1",
        }
        monkeypatch.setattr(
            pipeline,
            "load_strict_pipeline_checkpoint",
            lambda *_args, **_kwargs: checkpoint,
        )

        resp = client.get("/api/pipeline/checkpoint")

        assert resp.status_code == 200
        assert resp.json()["checkpoint_revision"] == 7


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
