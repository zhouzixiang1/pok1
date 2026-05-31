"""Tests for /api/evolution/* endpoints."""


class TestEvolutionState:
    def test_returns_state(self, client):
        resp = client.get("/api/evolution/state")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)


class TestEvolutionStream:
    def test_route_registered(self, client):
        """SSE endpoint is registered at /api/evolution/stream."""
        routes = {r.path for r in client.app.routes}
        assert "/api/evolution/stream" in routes
