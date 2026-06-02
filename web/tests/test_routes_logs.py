"""Tests for /api/logs/* endpoints."""

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
