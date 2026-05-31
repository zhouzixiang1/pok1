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

    def test_existing(self, client):
        # Find a real replay file
        from pathlib import Path
        replay_dir = Path(__file__).resolve().parents[2] / "web" / "core" / "results" / "match_replay"
        if not replay_dir.exists():
            return
        replays = list(replay_dir.iterdir())
        if not replays:
            return
        match_id = replays[0].name
        resp = client.get(f"/api/matches/replay/{match_id}")
        assert resp.status_code == 200
        assert isinstance(resp.json(), dict)


class TestMatchCommentary:
    def test_404(self, client):
        resp = client.get("/api/matches/commentary/nonexistent_id")
        assert resp.status_code == 404
