"""Tests for /api/logs/* endpoints."""

from pathlib import Path


RESULTS_DIR = Path(__file__).resolve().parents[2] / "web" / "core" / "results"
ORCHESTRATOR_LOGS_DIR = Path(__file__).resolve().parents[2] / "web" / "logs"


class TestGenerationLogs:
    def test_list_generations(self, client):
        resp = client.get("/api/logs/generations")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        if data:
            assert "version" in data[0]
            assert "files" in data[0]

    def test_get_log_content(self, client):
        # Find a real log
        if not RESULTS_DIR.exists():
            return
        dirs = [d for d in RESULTS_DIR.iterdir()
                if d.is_dir() and d.name.startswith("v") and (d / "logs").is_dir()]
        if not dirs:
            return
        d = dirs[0]
        version = d.name
        log_files = list((d / "logs").iterdir())
        if not log_files:
            return
        filename = log_files[0].name
        resp = client.get(f"/api/logs/generations/{version}/{filename}")
        assert resp.status_code == 200
        data = resp.json()
        assert "content" in data
        assert data["version"] == version
        assert data["filename"] == filename

    def test_get_log_tail(self, client):
        if not RESULTS_DIR.exists():
            return
        dirs = [d for d in RESULTS_DIR.iterdir()
                if d.is_dir() and d.name.startswith("v") and (d / "logs").is_dir()]
        if not dirs:
            return
        d = dirs[0]
        version = d.name
        log_files = list((d / "logs").iterdir())
        if not log_files:
            return
        filename = log_files[0].name
        resp = client.get(f"/api/logs/generations/{version}/{filename}?tail=5")
        assert resp.status_code == 200

    def test_get_log_missing(self, client):
        resp = client.get("/api/logs/generations/v99999/nonexistent.txt")
        assert resp.status_code == 200
        assert resp.json()["content"] == ""

    def test_path_traversal_blocked(self, client):
        resp = client.get("/api/logs/generations/../../etc/passwd")
        assert resp.status_code in (400, 404, 422)


class TestOrchestratorLogs:
    def test_list(self, client):
        resp = client.get("/api/logs/orchestrator")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        if data:
            assert "filename" in data[0]
            assert "size_bytes" in data[0]

    def test_get_log(self, client):
        if not ORCHESTRATOR_LOGS_DIR.exists():
            return
        files = [f for f in ORCHESTRATOR_LOGS_DIR.iterdir()
                 if f.name.startswith("orchestrator_") and f.name.endswith(".txt")]
        if not files:
            return
        resp = client.get(f"/api/logs/orchestrator/{files[0].name}")
        assert resp.status_code == 200
        assert len(resp.text) > 0

    def test_invalid_filename(self, client):
        # Path traversal: resolved before route matching → 404
        resp = client.get("/api/logs/orchestrator/../../etc/passwd")
        assert resp.status_code in (400, 404)

    def test_non_matching_filename(self, client):
        resp = client.get("/api/logs/orchestrator/random.txt")
        assert resp.status_code == 400

    def test_not_found(self, client):
        resp = client.get("/api/logs/orchestrator/orchestrator_29990101_000000.txt")
        assert resp.status_code == 404
