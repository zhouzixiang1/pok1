"""Tests for /api/evolution/* endpoints."""


class TestEvolutionState:
    def test_returns_state(self, client):
        resp = client.get("/api/evolution/state")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)

    def test_uninitialized_epoch_never_exposes_stale_webui_state(self, client, monkeypatch):
        import epoch_authority
        from server.app import web_ui

        monkeypatch.setattr(
            epoch_authority,
            "strict_epoch_projection",
            lambda: {
                "evaluation_epoch": "national_tcp_policy_v1",
                "state": "reset_required",
                "initialized": False,
                "reset_receipt_valid": False,
                "reset_receipt_digest": None,
                "current_v": 142,
                "next_v": 143,
                "active_bots": [],
                "active_generation": None,
            },
        )
        monkeypatch.setattr(web_ui, "grand_cost_total", 999.0)
        monkeypatch.setattr(web_ui, "gen_cost_total", 88.0)
        monkeypatch.setitem(web_ui._state, "metrics", {"success_rate": 1.0})
        monkeypatch.setitem(web_ui._state, "ratings", [{"name": "national_v155"}])
        monkeypatch.setitem(web_ui._state, "active_bots", ["national_v155"])
        monkeypatch.setitem(web_ui._state, "status", "old generation running")
        monkeypatch.setitem(web_ui._state, "is_working", True)

        data = client.get("/api/evolution/state").json()

        assert data["epoch_state"] == "reset_required"
        assert data["epoch_initialized"] is False
        assert data["current_v"] == 142
        assert data["next_v"] == 143
        assert data["status"] == "Stopped: reset_required"
        assert data["is_working"] is False
        assert data["metrics"] == {}
        assert data["ratings"] == []
        assert data["active_bots"] == []
        assert data["pipeline_stage"] is None
        assert data["grand_cost_total"] == 0.0
        assert data["gen_cost_total"] == 0.0

    def test_initialized_state_carries_reset_identity(self, client, monkeypatch):
        import epoch_authority

        receipt_digest = "a" * 64
        monkeypatch.setattr(
            epoch_authority,
            "strict_epoch_projection",
            lambda: {
                "evaluation_epoch": "national_tcp_policy_v1",
                "state": "fresh_bootstrap_ready",
                "initialized": True,
                "reset_receipt_valid": True,
                "reset_receipt_digest": receipt_digest,
                "current_v": 142,
                "next_v": 143,
                "active_bots": [],
                "active_generation": None,
            },
        )

        data = client.get("/api/evolution/state").json()

        assert data["evaluation_epoch"] == "national_tcp_policy_v1"
        assert data["epoch_state"] == "fresh_bootstrap_ready"
        assert data["epoch_initialized"] is True
        assert data["epoch_reset_receipt_digest"] == receipt_digest
        assert data["current_v"] == 142
        assert data["next_v"] == 143


class TestEvolutionStream:
    def test_route_registered(self, client):
        """SSE endpoint is registered at /api/evolution/stream."""
        routes = {r.path for r in client.app.routes}
        assert "/api/evolution/stream" in routes

    def test_uninitialized_stream_does_not_subscribe_or_replay_ring(self, client, monkeypatch):
        import epoch_authority
        from server.app import broadcaster

        monkeypatch.setattr(
            epoch_authority,
            "strict_epoch_projection",
            lambda: {
                "evaluation_epoch": "national_tcp_policy_v1",
                "state": "reset_required",
                "initialized": False,
                "reset_receipt_digest": None,
                "current_v": 142,
                "next_v": 143,
            },
        )

        def forbidden_subscription():
            raise AssertionError("pre-reset stream subscribed to broadcaster")

        monkeypatch.setattr(broadcaster, "add_client", forbidden_subscription)

        with client.stream("GET", "/api/evolution/stream") as response:
            body = "".join(response.iter_text())

        assert response.status_code == 200
        assert "event: epoch_blocked" in body
        assert "national_tcp_policy_v1" in body
        assert "event: stream_closed" in body
