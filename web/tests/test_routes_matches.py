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
        assert data["total_strength_samples"] == data["total_games"]
        assert data["strength_sample_unit"] == "70_hand_match"
        assert data["hands_per_strength_sample"] == 70
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

    def test_filters_retired_epoch_before_limit(self, client, tmp_path, monkeypatch):
        from server.routes import matches
        from web.tests.test_logic_replay_analysis import make_strict_replay

        current = make_strict_replay("current.json")
        summary = {
            key: value
            for key, value in current.items()
            if key not in {"games", "replay_schema_version"}
        }
        monkeypatch.setattr(
            matches,
            "_snapshot",
            lambda: {"match_history": [summary]},
        )

        resp = client.get("/api/matches/recent?limit=1")
        assert resp.status_code == 200
        assert [row["id"] for row in resp.json()] == ["current.json"]


class TestMatchReplay:
    def test_404(self, client):
        resp = client.get("/api/matches/replay/nonexistent_replay_id")
        assert resp.status_code == 404

    def test_existing(self, client, tmp_path, monkeypatch):
        from server.routes import matches
        from web.tests.test_logic_replay_analysis import (
            BOT_A,
            BOT_B,
            IDENTITY,
            make_strict_replay,
        )

        replay_dir = tmp_path / "match_replay"
        replay_dir.mkdir()
        match_id = "strict.json"
        replay = make_strict_replay(match_id)
        (replay_dir / match_id).write_text(json.dumps(replay))
        monkeypatch.setattr(matches, "REPLAY_DIR", replay_dir)
        monkeypatch.setattr(
            matches,
            "_snapshot",
            lambda: {
                "evaluation_identity_digest": IDENTITY,
                "active_bots": [BOT_A, BOT_B],
                "match_history": [{
                    "id": match_id,
                    "bot0": BOT_A,
                    "bot1": BOT_B,
                }],
            },
        )

        resp = client.get(f"/api/matches/replay/{match_id}")
        assert resp.status_code == 200
        assert resp.json()["execution_mode"] == "native_tcp"
        assert len(resp.json()["games"][0]["hand_records"]) == 70

    def test_existing_old_shape_is_rejected(self, client, tmp_path, monkeypatch):
        from server.routes import matches
        from web.tests.test_logic_replay_analysis import IDENTITY

        replay_dir = tmp_path / "match_replay"
        replay_dir.mkdir()
        match_id = "retired.json"
        (replay_dir / match_id).write_text(json.dumps({
            "bot0": "national_v143",
            "bot1": "national_v144",
            "games": [{"logs": [], "bot0_chips": 10}],
        }))
        monkeypatch.setattr(matches, "REPLAY_DIR", replay_dir)
        monkeypatch.setattr(
            matches,
            "_snapshot",
            lambda: {
                "evaluation_identity_digest": IDENTITY,
                "active_bots": ["national_v143", "national_v144"],
                "match_history": [{
                    "id": match_id,
                    "bot0": "national_v143",
                    "bot1": "national_v144",
                }],
            },
        )

        resp = client.get(f"/api/matches/replay/{match_id}")
        assert resp.status_code == 409
        assert "national_tcp_policy_v1" in resp.json()["detail"]

    def test_unpublished_replay_player_is_rejected(self, client, tmp_path, monkeypatch):
        from server.routes import matches
        from types import SimpleNamespace
        import replay_analysis
        from web.tests.test_logic_replay_analysis import IDENTITY

        replay_dir = tmp_path / "match_replay"
        replay_dir.mkdir()
        match_id = "unpublished.json"
        replay = {
            "id": match_id,
            "bot0": "national_v143",
            "bot1": "national_v155",
        }
        (replay_dir / match_id).write_text(json.dumps(replay))
        monkeypatch.setattr(matches, "REPLAY_DIR", replay_dir)
        monkeypatch.setattr(
            replay_analysis,
            "validate_native_replay",
            lambda *_args, **_kwargs: SimpleNamespace(accepted=True, reason=""),
        )
        monkeypatch.setattr(
            matches,
            "_snapshot",
            lambda: {
                "evaluation_identity_digest": IDENTITY,
                "active_bots": ["national_v143", "national_v144"],
                "match_history": [{
                    "id": match_id,
                    "bot0": "national_v143",
                    "bot1": "national_v144",
                }],
            },
        )

        resp = client.get(f"/api/matches/replay/{match_id}")
        assert resp.status_code == 409
