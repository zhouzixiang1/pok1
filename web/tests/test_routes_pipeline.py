"""Tests for /api/pipeline/* endpoints."""


class TestPipelineCheckpoint:
    def test_returns_data(self, client):
        resp = client.get("/api/pipeline/checkpoint")
        assert resp.status_code == 200
        # May be null if no active pipeline
        data = resp.json()
        assert data is None or isinstance(data, dict)

    def test_has_expected_fields(self, client):
        resp = client.get("/api/pipeline/checkpoint")
        data = resp.json()
        if data:
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
        resp = client.get("/api/pipeline/failures")
        data = resp.json()
        if data:
            entry = data[0]
            assert "gen" in entry
            assert "worker_id" in entry
            assert "error" in entry
