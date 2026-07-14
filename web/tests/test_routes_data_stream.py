"""Tests for /api/data/stream SSE endpoint."""

from fastapi.testclient import TestClient

from server.routes.data_stream import router


class TestDataStream:
    def test_route_registered(self, client):
        """SSE endpoint is registered at /api/data/stream."""
        routes = {r.path for r in client.app.routes}
        assert "/api/data/stream" in routes

    def test_epoch_loss_emits_fence_and_closes_stream(self, monkeypatch):
        from fastapi import FastAPI
        from server.routes import data_stream

        monkeypatch.setattr(data_stream, "_epoch_projection", lambda: {
            "evaluation_epoch": "national_tcp_policy_v1",
            "epoch_state": "reset_required",
            "epoch_initialized": False,
            "epoch_reset_receipt_digest": None,
        })
        app = FastAPI()
        app.include_router(router)

        response = TestClient(app).get("/api/data/stream")

        assert response.status_code == 200
        assert "event: epoch_blocked" in response.text
        assert '"epoch_initialized": false' in response.text
