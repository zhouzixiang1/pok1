"""Tests for /api/logs/* endpoints."""

import json

import pytest


class TestGenerationLogs:
    def test_list_generations(self, client, tmp_path, monkeypatch):
        from server.routes import logs
        v_dir = tmp_path / "v30" / "logs"
        v_dir.mkdir(parents=True)
        (v_dir / "master_io.txt").write_text("log line\n")
        monkeypatch.setattr(logs, "RESULTS_DIR", tmp_path)
        resp = client.get("/api/logs/generations")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        assert "version" in data[0]
        assert "files" in data[0]

    def test_get_log_content(self, client, tmp_path, monkeypatch):
        from server.routes import logs
        v_dir = tmp_path / "v30" / "logs"
        v_dir.mkdir(parents=True)
        (v_dir / "master_io.txt").write_text("line1\nline2\nline3\n")
        monkeypatch.setattr(logs, "RESULTS_DIR", tmp_path)
        resp = client.get("/api/logs/generations/v30/master_io.txt")
        assert resp.status_code == 200
        data = resp.json()
        assert "content" in data
        assert data["version"] == "v30"
        assert data["filename"] == "master_io.txt"

    def test_get_log_tail(self, client, tmp_path, monkeypatch):
        from server.routes import logs
        v_dir = tmp_path / "v30" / "logs"
        v_dir.mkdir(parents=True)
        (v_dir / "worker_io.txt").write_text("line1\nline2\nline3\nline4\nline5\n")
        monkeypatch.setattr(logs, "RESULTS_DIR", tmp_path)
        resp = client.get("/api/logs/generations/v30/worker_io.txt?tail=2")
        assert resp.status_code == 200
        data = resp.json()
        content = data["content"]
        lines = content.strip().split("\n")
        assert len(lines) <= 2

    def test_get_log_missing(self, client):
        resp = client.get("/api/logs/generations/v99999/nonexistent.txt")
        assert resp.status_code == 200
        assert resp.json()["content"] == ""

    def test_path_traversal_blocked(self, client):
        resp = client.get("/api/logs/generations/../../etc/passwd")
        assert resp.status_code in (400, 404, 422)


class TestOrchestratorLogs:
    def test_list(self, client, tmp_path, monkeypatch):
        from server.routes import logs
        (tmp_path / "orchestrator_20260601_120000.txt").write_text("log\n")
        monkeypatch.setattr(logs, "ORCHESTRATOR_LOGS_DIR", tmp_path)
        resp = client.get("/api/logs/orchestrator")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        assert "filename" in data[0]
        assert "size_bytes" in data[0]

    def test_get_log(self, client, tmp_path, monkeypatch):
        from server.routes import logs
        fname = "orchestrator_20260601_120000.txt"
        (tmp_path / fname).write_text("orchestrator log content here\n")
        monkeypatch.setattr(logs, "ORCHESTRATOR_LOGS_DIR", tmp_path)
        resp = client.get(f"/api/logs/orchestrator/{fname}")
        assert resp.status_code == 200
        assert len(resp.text) > 0

    def test_invalid_filename(self, client):
        resp = client.get("/api/logs/orchestrator/../../etc/passwd")
        assert resp.status_code in (400, 404)

    def test_non_matching_filename(self, client):
        resp = client.get("/api/logs/orchestrator/random.txt")
        assert resp.status_code == 400

    def test_not_found(self, client):
        resp = client.get("/api/logs/orchestrator/orchestrator_29990101_000000.txt")
        assert resp.status_code == 404


class TestSystemEvents:
    def test_structured_source_filters_run_id_and_stage(self, client, tmp_path, monkeypatch):
        from server.routes import logs

        events_file = tmp_path / "events.jsonl"
        events_file.write_text(
            json.dumps({
                "ts": 1.0,
                "type": "pipeline.master_done",
                "severity": "info",
                "message": "done",
                "data": {
                    "category": "pipeline.master_done",
                    "run_id": "231#0",
                    "stage": "master_planned",
                },
            }) + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(logs, "RESULTS_DIR", tmp_path)

        resp = client.get(
            "/api/logs/system-events?source=structured&run_id=231%230&stage=master_planned"
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["events"][0]["type"] == "pipeline.master_done"

    def test_structured_source_normalizes_legacy_sigterm_misclassification(self, client, tmp_path, monkeypatch):
        from server.routes import logs

        events_file = tmp_path / "events.jsonl"
        events_file.write_text(
            json.dumps({
                "ts": 1.0,
                "type": "pipeline.llm_role_shutdown_cancelled",
                "severity": "info",
                "message": "LLM process received SIGTERM",
                "data": {
                    "category": "pipeline.llm_role_shutdown_cancelled",
                    "shutdown_requested": False,
                    "run_id": "257#0",
                    "stage": "reviewed",
                },
            }) + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(logs, "RESULTS_DIR", tmp_path)

        resp = client.get(
            "/api/logs/system-events?source=structured&type=pipeline.llm_role_process_terminated"
        )

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        event = data["events"][0]
        assert event["type"] == "pipeline.llm_role_process_terminated"
        assert event["severity"] == "error"
        assert event["data"]["original_type"] == "pipeline.llm_role_shutdown_cancelled"
        assert event["data"]["legacy_misclassified"] is True

    def test_system_events_rejects_unknown_source(self, client):
        resp = client.get("/api/logs/system-events?source=unknown")
        assert resp.status_code == 400
