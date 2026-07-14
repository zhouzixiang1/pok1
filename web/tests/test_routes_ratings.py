"""Tests for ratings, history, daemon, H2H, and bot-stat routes."""

import pytest
from bot_namespace import bot_name


class TestGetRatings:
    def test_returns_list(self, client):
        resp = client.get("/api/ratings")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        if data:
            row = data[0]
            assert "name" in row
            assert "rating" in row
            assert "rd" in row
            assert "rank" in row
            assert "h2h_avg_wr" in row

    @pytest.mark.requires_active_bot
    def test_detail_found(self, client, active_bot_version):
        name = bot_name(active_bot_version)
        resp = client.get(f"/api/ratings/{name}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == name
        assert "rating" in data

    def test_detail_404(self, client):
        resp = client.get("/api/ratings/nonexistent_bot")
        assert resp.status_code == 404


class TestHistory:
    def test_default(self, client):
        resp = client.get("/api/history")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        if data:
            assert "period" in data[0]
            assert "ratings" in data[0]
            assert "win_rates" in data[0]

    @pytest.mark.requires_active_bot
    def test_filtered(self, client, active_bot_version):
        resp = client.get(f"/api/history?bots={bot_name(active_bot_version)}")
        assert resp.status_code == 200

    def test_summary(self, client):
        resp = client.get("/api/history/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)


class TestRetiredExperienceSurface:
    @pytest.mark.parametrize("method", ["get", "put", "post"])
    def test_route_is_absent(self, client, method):
        response = getattr(client, method)("/api/experience")
        assert response.status_code == 404

    def test_append_route_is_absent(self, client):
        assert client.post("/api/experience/append").status_code == 404


class TestDaemonStatus:
    def test_returns_status(self, client):
        resp = client.get("/api/daemon/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data
        assert "last_update_age_seconds" in data
        assert "daemon_enabled" in data

    def test_pre_reset_config_is_not_reported_as_effective_daemon(self, client, monkeypatch):
        import epoch_authority
        from server.state import app_state

        app_state.override_runtime_config(daemon_enabled=True)
        monkeypatch.setattr(
            epoch_authority,
            "strict_epoch_projection",
            lambda **_kwargs: {
                "evaluation_epoch": "national_tcp_policy_v1",
                "state": "reset_required",
                "initialized": False,
            },
        )

        data = client.get("/api/daemon/status").json()

        assert data["status"] == "blocked"
        assert data["reason"] == "policy_epoch_not_initialized"
        assert data["epoch_state"] == "reset_required"
        assert data["daemon_enabled"] is False
        assert data["daemon_configured"] is True

    def test_initialized_status_uses_process_liveness_not_cycle_age(self, client, monkeypatch):
        import epoch_authority
        import server.routes.control as control
        import server.routes.ratings as ratings
        from server.state import app_state

        monkeypatch.setattr(app_state, "daemon_enabled", True)
        monkeypatch.setattr(
            epoch_authority,
            "strict_epoch_projection",
            lambda **_kwargs: {
                "evaluation_epoch": "national_tcp_policy_v1",
                "state": "fresh_bootstrap_ready",
                "initialized": True,
            },
        )
        monkeypatch.setattr(ratings, "_snapshot", lambda: {})
        monkeypatch.setattr(
            control,
            "_daemon_health_snapshot",
            lambda: {
                "alive": False,
                "heartbeat_stale": False,
            },
        )

        data = client.get("/api/daemon/status").json()

        assert data["status"] == "stopped"
        assert data["reason"] == "daemon_process_not_alive"
        assert data["daemon_enabled"] is False
        assert data["daemon_configured"] is True
        assert data["process_alive"] is False
        assert data["strength_evidence_available"] is False
        assert data["strength_evidence_status"] == "awaiting_first_rating_cycle"

    def test_fresh_live_daemon_is_active_before_first_rating_cycle(self, client, monkeypatch):
        import epoch_authority
        import server.routes.control as control
        import server.routes.ratings as ratings
        from server.state import app_state

        monkeypatch.setattr(app_state, "daemon_enabled", True)
        monkeypatch.setattr(
            epoch_authority,
            "strict_epoch_projection",
            lambda **_kwargs: {
                "evaluation_epoch": "national_tcp_policy_v1",
                "state": "strict_published",
                "initialized": True,
            },
        )
        monkeypatch.setattr(ratings, "_snapshot", lambda: {})
        monkeypatch.setattr(
            control,
            "_daemon_health_snapshot",
            lambda: {
                "alive": True,
                "heartbeat_stale": False,
                "heartbeat_age_sec": 2.0,
            },
        )

        data = client.get("/api/daemon/status").json()

        assert data["status"] == "active"
        assert data["daemon_enabled"] is True
        assert data["process_alive"] is True
        assert data["heartbeat_age_seconds"] == 2.0
        assert data["strength_evidence_status"] == "awaiting_first_rating_cycle"


class TestH2H:
    def test_all(self, client):
        resp = client.get("/api/h2h")
        assert resp.status_code == 200
        assert isinstance(resp.json(), dict)

    @pytest.mark.requires_active_bot
    def test_filtered(self, client, active_bot_version):
        name = bot_name(active_bot_version)
        resp = client.get(f"/api/h2h?bot_name={name}")
        assert resp.status_code == 200
        data = resp.json()
        for key in data:
            assert name in key


class TestBotStats:
    def test_returns_dict(self, client):
        resp = client.get("/api/bot-stats")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)
