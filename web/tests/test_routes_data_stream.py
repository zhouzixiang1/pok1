"""Tests for /api/data/stream SSE endpoint."""


class TestDataStream:
    def test_route_registered(self, client):
        """SSE endpoint is registered at /api/data/stream."""
        routes = {r.path for r in client.app.routes}
        assert "/api/data/stream" in routes
