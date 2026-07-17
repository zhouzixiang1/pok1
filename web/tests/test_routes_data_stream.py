"""Tests for /api/data/stream SSE endpoint."""

from fastapi.testclient import TestClient

from server.routes.data_stream import router
from testclient_compat import backend_options_for_testclient


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

        response = TestClient(
            app,
            backend_options=backend_options_for_testclient(),
        ).get("/api/data/stream")

        assert response.status_code == 200
        assert "event: epoch_blocked" in response.text
        assert '"epoch_initialized": false' in response.text

    def test_stale_expected_authority_is_fenced_before_any_data_fetch(
        self,
        monkeypatch,
    ):
        from fastapi import FastAPI
        from server.routes import data_stream

        monkeypatch.setattr(data_stream, "_epoch_projection", lambda: {
            "evaluation_epoch": "national_tcp_policy_v1",
            "epoch_state": "strict_published",
            "epoch_initialized": True,
            "epoch_reset_receipt_digest": "a" * 64,
            "stream_authority_digest": "b" * 64,
        })

        def forbidden_snapshot():
            raise AssertionError("stale expected identity fetched current data")

        monkeypatch.setattr(data_stream, "_strict_snapshot", forbidden_snapshot)
        app = FastAPI()
        app.include_router(router)

        response = TestClient(
            app,
            backend_options=backend_options_for_testclient(),
        ).get(
            f"/api/data/stream?authority={'a' * 64}"
        )

        assert response.status_code == 200
        assert "event: epoch_blocked" in response.text
        assert '"stream_authority_digest": "' + "b" * 64 + '"' in response.text
        assert "event: ratings" not in response.text
