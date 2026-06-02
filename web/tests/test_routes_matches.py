"""Tests for /api/matches/* endpoints."""

import json


class TestMatchMatrix:
    def test_returns_data(self, client):
        resp = client.get("/api/matches/matrix")
        assert resp.status_code == 200
        data = resp.json()
        assert "bots" in data
        assert "matrix" in data
        assert isinstance(data["bots"], list)
        assert isinstance(data["matrix"], list)


class TestMatchStats:
    def test_returns_data(self, client):
        resp = client.get("/api/matches/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_games" in data
        assert "total_pairs" in data
        assert "total_periods" in data


class TestRecentMatches:
    def test_default(self, client):
        resp = client.get("/api/matches/recent")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

    def test_limit(self, client):
        resp = client.get("/api/matches/recent?limit=5")
        assert resp.status_code == 200
        assert len(resp.json()) <= 5


class TestMatchReplay:
    def test_404(self, client):
        resp = client.get("/api/matches/replay/nonexistent_replay_id")
        assert resp.status_code == 404

    def test_existing(self, client, tmp_path, monkeypatch):
        from server.routes import matches
        import json

        replay_dir = tmp_path / "match_replay"
        replay_dir.mkdir()
        match_id = "test_match_001"
        (replay_dir / match_id).write_text(json.dumps({"hands": [], "winner": "a"}))
        monkeypatch.setattr(matches, "REPLAY_DIR", replay_dir)

        resp = client.get(f"/api/matches/replay/{match_id}")
        assert resp.status_code == 200
        assert isinstance(resp.json(), dict)


class TestMatchCommentary:
    def test_404(self, client):
        resp = client.get("/api/matches/commentary/nonexistent_id")
        assert resp.status_code == 404
