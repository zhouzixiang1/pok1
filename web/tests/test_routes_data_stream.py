"""Tests for /api/data/stream SSE endpoint."""

import asyncio
import threading
import time

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

    def test_slow_complete_tick_does_not_block_event_loop(self, monkeypatch):
        from server.routes import data_stream

        entered = threading.Event()
        release = threading.Event()
        expected = ({"epoch_initialized": False}, [])

        def slow_tick(*_args):
            entered.set()
            assert release.wait(timeout=2)
            return expected

        monkeypatch.setattr(data_stream, "_build_data_tick", slow_tick)

        async def exercise():
            task = asyncio.create_task(
                data_stream._build_data_tick_async(0, "a" * 64, True)
            )
            deadline = time.monotonic() + 1
            while not entered.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.001)
            assert entered.is_set()
            started = time.monotonic()
            await asyncio.sleep(0.02)
            assert time.monotonic() - started < 0.1
            release.set()
            return await task

        assert asyncio.run(exercise()) == expected

    def test_one_tick_reuses_one_complete_epoch_and_strength_observation(
        self,
        monkeypatch,
    ):
        from server.routes import data_stream

        counts = {"epoch": 0, "strength": 0, "bots": 0, "daemon": 0}
        authority = "b" * 64

        def epoch():
            counts["epoch"] += 1
            return {
                "evaluation_epoch": "national_tcp_policy_v1",
                "state": "strict_published",
                "initialized": True,
                "epoch_state": "strict_published",
                "epoch_initialized": True,
                "stream_authority_digest": authority,
                "active_bots": [],
                "strict_published_bot_identities": [],
            }

        def strength():
            counts["strength"] += 1
            return {}

        monkeypatch.setattr(data_stream, "_data_tick_content_key", lambda: ("same",))
        monkeypatch.setattr(data_stream, "_epoch_projection", epoch)
        monkeypatch.setattr(data_stream, "_strict_snapshot", strength)
        monkeypatch.setattr(data_stream, "_get_bots", lambda *_args: counts.__setitem__("bots", counts["bots"] + 1) or {"active": []})
        monkeypatch.setattr(data_stream, "_get_daemon_status", lambda *_args: counts.__setitem__("daemon", counts["daemon"] + 1) or {})
        monkeypatch.setattr(data_stream, "_get_generations", lambda: [])

        epoch_value, events = data_stream._build_data_tick(0, authority, True)

        assert epoch_value["stream_authority_digest"] == authority
        assert counts == {"epoch": 1, "strength": 1, "bots": 1, "daemon": 1}
        assert {event["event"] for event in events} >= {
            "ratings", "daemon", "bots", "stats", "matches",
            "generations", "matrix", "h2h", "bot_stats", "history", "ping",
        }

    def test_tick_authority_drift_withholds_every_event(self, monkeypatch):
        from server.routes import data_stream

        keys = iter((("before",), ("after",)))
        authority = "b" * 64
        monkeypatch.setattr(data_stream, "_data_tick_content_key", lambda: next(keys))
        monkeypatch.setattr(data_stream, "_epoch_projection", lambda: {
            "evaluation_epoch": "national_tcp_policy_v1",
            "state": "strict_published",
            "initialized": True,
            "epoch_state": "strict_published",
            "epoch_initialized": True,
            "stream_authority_digest": authority,
            "active_bots": [],
            "strict_published_bot_identities": [],
        })
        monkeypatch.setattr(data_stream, "_strict_snapshot", lambda: {})
        monkeypatch.setattr(data_stream, "_get_generations", lambda: [])

        epoch, events = data_stream._build_data_tick(0, authority, True)

        assert events == []
        assert epoch["epoch_initialized"] is False
        assert epoch["stream_authority_digest"] is None
        assert epoch["authority_issue"] == "data_tick_authority_changed_during_build"
