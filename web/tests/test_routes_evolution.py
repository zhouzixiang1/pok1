"""Tests for /api/evolution/* endpoints."""

import asyncio
import threading
import time

from bot_namespace import bot_name
from conftest import STRICT_SOURCE_V, STRICT_TARGET_V


def _active_epoch(*, revision=7, stage="master_planning"):
    return {
        "evaluation_epoch": "national_tcp_policy_v1",
        "state": "fresh_bootstrap_ready",
        "initialized": True,
        "reset_receipt_valid": True,
        "reset_receipt_digest": "a" * 64,
        "version_authority_high_water": STRICT_SOURCE_V,
        "current_v": STRICT_SOURCE_V,
        "next_v": STRICT_TARGET_V,
        "active_bots": [],
        "active_generation": {
            "next_v": STRICT_TARGET_V,
            "source_v": STRICT_SOURCE_V,
            "stage": stage,
            "run_id": f"{STRICT_TARGET_V}#1",
            "workflow_run_id": f"generation:{STRICT_TARGET_V}:workflow-v1",
            "checkpoint_revision": revision,
        },
    }


def _active_task():
    return {
        "present": True,
        "done": False,
        "cancelled": False,
        "shutdown_requested": False,
        "status_eligible": True,
        "owner_id": "f" * 32,
        "lifecycle_revision": 7,
    }


class TestEvolutionState:
    def test_returns_state(self, client):
        resp = client.get("/api/evolution/state")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)

    def test_state_and_stream_authority_reads_do_not_block_event_loop(
        self, monkeypatch
    ):
        from server.routes import evolution

        def exercise(target_name, async_call, expected):
            entered = threading.Event()
            release = threading.Event()

            def slow_authority(*_args):
                entered.set()
                assert release.wait(timeout=2)
                return expected

            monkeypatch.setattr(evolution, target_name, slow_authority)

            async def run():
                task = asyncio.create_task(async_call())
                deadline = time.monotonic() + 1
                while not entered.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(0.001)
                assert entered.is_set()
                started = time.monotonic()
                await asyncio.sleep(0.02)
                assert time.monotonic() - started < 0.1
                release.set()
                return await task

            return asyncio.run(run())

        assert exercise(
            "_evolution_state_snapshot",
            evolution.evolution_state,
            {"status": "ok"},
        ) == {"status": "ok"}
        expected_stream = ({"initialized": False}, {"status": "none"}, None)
        assert exercise(
            "_stable_stream_projection",
            evolution._stable_stream_projection_async,
            expected_stream,
        ) == expected_stream

    def test_uninitialized_epoch_never_exposes_stale_webui_state(self, client, monkeypatch):
        import epoch_authority
        from server.app import web_ui

        monkeypatch.setattr(
            epoch_authority,
            "strict_epoch_projection",
            lambda **_kwargs: {
                "evaluation_epoch": "national_tcp_policy_v1",
                "state": "reset_required",
                "initialized": False,
                "reset_receipt_valid": False,
                "reset_receipt_digest": None,
                "current_v": STRICT_SOURCE_V,
                "next_v": STRICT_TARGET_V,
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
        assert data["current_v"] == STRICT_SOURCE_V
        assert data["next_v"] == STRICT_TARGET_V
        assert data["status"] == "Stopped: reset_required"
        assert data["is_working"] is False
        assert data["metrics"] == {}
        assert data["ratings"] == []
        assert data["active_bots"] == []
        assert data["pipeline_stage"] is None
        assert data["pipeline_checkpoint_revision"] is None
        assert data["grand_cost_total"] == 0.0
        assert data["gen_cost_total"] == 0.0

    def test_initialized_state_carries_reset_identity(self, client, monkeypatch):
        import epoch_authority

        receipt_digest = "a" * 64
        monkeypatch.setattr(
            epoch_authority,
            "strict_epoch_projection",
            lambda **_kwargs: {
                "evaluation_epoch": "national_tcp_policy_v1",
                "state": "fresh_bootstrap_ready",
                "initialized": True,
                "reset_receipt_valid": True,
                "reset_receipt_digest": receipt_digest,
                "version_authority_high_water": STRICT_SOURCE_V,
                "current_v": STRICT_SOURCE_V,
                "next_v": STRICT_TARGET_V,
                "active_bots": [],
                "active_generation": None,
            },
        )

        data = client.get("/api/evolution/state").json()

        assert data["evaluation_epoch"] == "national_tcp_policy_v1"
        assert data["epoch_state"] == "fresh_bootstrap_ready"
        assert data["epoch_initialized"] is True
        assert data["epoch_reset_receipt_digest"] == receipt_digest
        assert len(data["stream_authority_digest"]) == 64
        assert data["current_v"] == STRICT_SOURCE_V
        assert data["next_v"] == STRICT_TARGET_V
        assert data["pipeline_checkpoint_revision"] is None

    def test_initialized_state_carries_validated_checkpoint_revision(
        self,
        client,
        monkeypatch,
    ):
        import epoch_authority
        from server.routes import _helpers

        monkeypatch.setattr(
            epoch_authority,
            "strict_epoch_projection",
            lambda **_kwargs: {
                "evaluation_epoch": "national_tcp_policy_v1",
                "state": "fresh_bootstrap_ready",
                "initialized": True,
                "reset_receipt_valid": True,
                "reset_receipt_digest": "a" * 64,
                "version_authority_high_water": STRICT_SOURCE_V,
                "current_v": STRICT_SOURCE_V,
                "next_v": STRICT_TARGET_V,
                "active_bots": [],
                "active_generation": {
                    "next_v": STRICT_TARGET_V,
                    "source_v": STRICT_SOURCE_V,
                    "stage": "reviewed",
                    "run_id": f"{STRICT_TARGET_V}#1",
                    "workflow_run_id": "workflow-v1",
                    "checkpoint_revision": 7,
                },
            },
        )
        monkeypatch.setattr(
            _helpers,
            "load_strict_pipeline_checkpoint",
            lambda *_args, **_kwargs: {
                "stage": "reviewed",
                "checkpoint_revision": 7,
            },
        )

        data = client.get("/api/evolution/state").json()

        assert data["pipeline_stage"] == "reviewed"
        assert data["pipeline_checkpoint_revision"] == 7

    def test_state_exposes_transient_status_only_for_current_active_task(
        self,
        client,
        monkeypatch,
    ):
        import server.routes.evolution as route
        from server.app import web_ui

        epoch = _active_epoch()
        handoff = {"status": "none"}
        monkeypatch.setattr(
            route,
            "_stable_stream_projection",
            lambda: (dict(epoch), dict(handoff), "b" * 64),
        )
        monkeypatch.setattr(route, "_live_task_snapshot", _active_task)
        monkeypatch.setitem(web_ui._state, "status", f"Master planning for v{STRICT_TARGET_V}")
        monkeypatch.setitem(web_ui._state, "is_working", True)
        monkeypatch.setitem(web_ui._state, "status_identity", {
            "run_id": f"{STRICT_TARGET_V}#1",
            "workflow_run_id": f"generation:{STRICT_TARGET_V}:workflow-v1",
            "checkpoint_revision": 7,
            "stage": "master_planning",
            "task_owner_id": "f" * 32,
            "task_lifecycle_revision": 7,
            "emitted_at": 1_000.0,
        })
        monkeypatch.setattr(route.time, "time", lambda: 1_001.0)

        data = client.get("/api/evolution/state").json()

        assert data["status"] == f"Master planning for v{STRICT_TARGET_V}"
        assert data["is_working"] is True
        assert data["transient_status"] == {
            "msg": f"Master planning for v{STRICT_TARGET_V}",
            "is_working": True,
            "run_id": f"{STRICT_TARGET_V}#1",
            "workflow_run_id": f"generation:{STRICT_TARGET_V}:workflow-v1",
            "checkpoint_revision": 7,
            "stage": "master_planning",
            "task_owner_id": "f" * 32,
            "task_lifecycle_revision": 7,
            "emitted_at": 1_000.0,
        }
        assert data["transient_status_task"] == {
            "present": True,
            "done": False,
            "shutdown_requested": False,
            "status_eligible": True,
            "owner_id": "f" * 32,
            "lifecycle_revision": 7,
        }

    def test_state_drops_stale_or_inactive_transient_master_status(
        self,
        client,
        monkeypatch,
    ):
        import server.routes.evolution as route
        from server.app import web_ui

        epoch = _active_epoch(revision=8, stage="workers_running")
        monkeypatch.setattr(
            route,
            "_stable_stream_projection",
            lambda: (dict(epoch), {"status": "none"}, "b" * 64),
        )
        monkeypatch.setattr(route, "_live_task_snapshot", _active_task)
        monkeypatch.setitem(web_ui._state, "status", f"Master planning for v{STRICT_TARGET_V}")
        monkeypatch.setitem(web_ui._state, "is_working", True)
        monkeypatch.setitem(web_ui._state, "status_identity", {
            "run_id": f"{STRICT_TARGET_V}#1",
            "workflow_run_id": f"generation:{STRICT_TARGET_V}:workflow-v1",
            "checkpoint_revision": 7,
            "stage": "master_planning",
            "task_lifecycle_revision": 7,
            "emitted_at": 1_000.0,
        })
        monkeypatch.setattr(route.time, "time", lambda: 1_001.0)

        data = client.get("/api/evolution/state").json()

        assert data["transient_status"] is None
        assert data["is_working"] is False
        assert "Master planning" not in data["status"]
        assert data["status"] == "等待当前活动任务状态"

        monkeypatch.setattr(
            route,
            "_live_task_snapshot",
            lambda: {**_active_task(), "done": True},
        )
        data = client.get("/api/evolution/state").json()
        assert data["transient_status"] is None
        assert data["status"] == "无可验证的当前活动任务状态"

    def test_state_rejects_status_from_replaced_task_owner(
        self,
        client,
        monkeypatch,
    ):
        """A retry may retain the same checkpoint identity but not its owner."""

        import server.routes.evolution as route
        from server.app import web_ui

        epoch = _active_epoch()
        old_status = {
            "run_id": f"{STRICT_TARGET_V}#1",
            "workflow_run_id": f"generation:{STRICT_TARGET_V}:workflow-v1",
            "checkpoint_revision": 7,
            "stage": "master_planning",
            "task_owner_id": "e" * 32,
            "task_lifecycle_revision": 7,
            "emitted_at": 1_000.0,
        }
        monkeypatch.setattr(
            route,
            "_stable_stream_projection",
            lambda: (dict(epoch), {"status": "none"}, "b" * 64),
        )
        monkeypatch.setattr(route, "_live_task_snapshot", _active_task)
        monkeypatch.setitem(web_ui._state, "status", f"Master planning for v{STRICT_TARGET_V}")
        monkeypatch.setitem(web_ui._state, "is_working", True)
        monkeypatch.setitem(web_ui._state, "status_identity", old_status)
        monkeypatch.setattr(route.time, "time", lambda: 1_001.0)

        data = client.get("/api/evolution/state").json()

        assert data["transient_status"] is None
        assert data["is_working"] is False
        assert data["status"] == "等待当前活动任务状态"

    def test_state_rejects_same_owner_status_while_shutdown_is_requested(
        self,
        client,
        monkeypatch,
    ):
        """A stop edge cannot leave its old Master status eligible."""

        import server.routes.evolution as route
        from server.app import web_ui

        epoch = _active_epoch()
        stopping = {
            **_active_task(),
            "shutdown_requested": True,
            "status_eligible": False,
        }
        monkeypatch.setattr(
            route,
            "_stable_stream_projection",
            lambda: (dict(epoch), {"status": "none"}, "b" * 64),
        )
        monkeypatch.setattr(route, "_live_task_snapshot", lambda: stopping)
        monkeypatch.setitem(web_ui._state, "status", f"Master planning for v{STRICT_TARGET_V}")
        monkeypatch.setitem(web_ui._state, "is_working", True)
        monkeypatch.setitem(web_ui._state, "status_identity", {
            "run_id": f"{STRICT_TARGET_V}#1",
            "workflow_run_id": f"generation:{STRICT_TARGET_V}:workflow-v1",
            "checkpoint_revision": 7,
            "stage": "master_planning",
            "task_owner_id": "f" * 32,
            "task_lifecycle_revision": 7,
            "emitted_at": 1_000.0,
        })
        monkeypatch.setattr(route.time, "time", lambda: 1_001.0)

        data = client.get("/api/evolution/state").json()

        assert data["transient_status"] is None
        assert data["is_working"] is False
        assert data["status"] == "无可验证的当前活动任务状态"
        assert data["transient_status_task"] == {
            "present": True,
            "done": False,
            "shutdown_requested": True,
            "status_eligible": False,
            "owner_id": "f" * 32,
            "lifecycle_revision": 7,
        }


class TestEvolutionStream:
    def test_route_registered(self, client):
        """SSE endpoint is registered at /api/evolution/stream."""
        # Newer FastAPI/Starlette wraps included routers in _IncludedRouter
        # objects that do not expose ``.path`` at the app route list level, so
        # verify registration by reaching the endpoint rather than enumerating
        # routes.  A registered streaming endpoint returns 200 (or a fenced
        # non-200 body) rather than Starlette's 404 for unknown routes.
        response = client.get("/api/evolution/stream", headers={"Accept": "text/event-stream"})
        assert response.status_code != 404

    def test_status_event_is_bound_to_current_checkpoint_at_emission(
        self,
        monkeypatch,
    ):
        import json
        import epoch_authority
        import server.state
        from web_ui import EventBroadcaster, WebUI

        monkeypatch.setattr(
            epoch_authority,
            "strict_epoch_projection",
            lambda **_kwargs: _active_epoch(),
        )
        monkeypatch.setattr(server.state.app_state, "task_snapshot", _active_task)
        broadcaster = EventBroadcaster()
        broadcaster.bind_authority("a" * 64)
        _, queue = broadcaster.add_client("a" * 64)
        ui = WebUI(broadcaster)

        ui.set_status(f"Master planning for v{STRICT_TARGET_V}", is_working=True)

        event = queue.get_nowait()
        payload = json.loads(event["data"])
        assert event["event"] == "status"
        assert payload["run_id"] == f"{STRICT_TARGET_V}#1"
        assert payload["workflow_run_id"] == f"generation:{STRICT_TARGET_V}:workflow-v1"
        assert payload["checkpoint_revision"] == 7
        assert payload["stage"] == "master_planning"
        assert payload["task_owner_id"] == "f" * 32
        assert payload["task_lifecycle_revision"] == 7
        assert isinstance(payload["emitted_at"], float)

    def test_task_owner_broadcast_is_minimal_typed_invalidation(
        self,
        monkeypatch,
    ):
        """The lifecycle producer never leaks unrelated task internals to SSE."""

        import json
        import server.app as app
        from web_ui import EventBroadcaster

        broadcaster = EventBroadcaster()
        broadcaster.bind_authority("a" * 64)
        _, queue = broadcaster.add_client("a" * 64)
        monkeypatch.setattr(app, "broadcaster", broadcaster)

        app._publish_task_owner(_active_task())

        event = queue.get_nowait()
        payload = json.loads(event["data"])
        assert event["event"] == "task_owner"
        assert payload == {
            "present": True,
            "done": False,
            "shutdown_requested": False,
            "status_eligible": True,
            "owner_id": "f" * 32,
            "lifecycle_revision": 7,
            "ts": payload["ts"],
        }
        assert isinstance(payload["ts"], float)

    def test_task_owner_broadcast_preserves_terminal_lifecycle_revision(
        self,
        monkeypatch,
    ):
        """A done task is an ordered invalidation, never a revision-zero reset."""

        import json
        import server.app as app
        import server.routes.evolution as route
        from web_ui import EventBroadcaster

        terminal = {
            **_active_task(),
            "done": True,
            "status_eligible": False,
            "lifecycle_revision": 8,
        }
        broadcaster = EventBroadcaster()
        broadcaster.bind_authority("a" * 64)
        _, queue = broadcaster.add_client("a" * 64)
        monkeypatch.setattr(app, "broadcaster", broadcaster)
        monkeypatch.setattr(route, "_live_task_snapshot", lambda: terminal)

        app._publish_task_owner(terminal)

        event = queue.get_nowait()
        payload = json.loads(event["data"])
        assert payload == {
            "present": True,
            "done": True,
            "shutdown_requested": False,
            "status_eligible": False,
            "owner_id": "f" * 32,
            "lifecycle_revision": 8,
            "ts": payload["ts"],
        }
        assert route._task_owner_event_is_current(event) is True

    def test_task_owner_broadcast_emits_authority_loss_not_revision_zero(
        self,
        monkeypatch,
    ):
        """Malformed state clears clients without inventing a lifecycle epoch."""

        import json
        import server.app as app
        from web_ui import EventBroadcaster

        broadcaster = EventBroadcaster()
        broadcaster.bind_authority("a" * 64)
        _, queue = broadcaster.add_client("a" * 64)
        monkeypatch.setattr(app, "broadcaster", broadcaster)

        app._publish_task_owner({"present": "corrupt"})

        event = queue.get_nowait()
        payload = json.loads(event["data"])
        assert event["event"] == "task_authority_lost"
        assert payload == {
            "reason": "task_snapshot_projection_invalid",
            "ts": payload["ts"],
        }
        assert isinstance(payload["ts"], float)

    def test_webui_stamps_no_active_identity_after_shutdown(self, monkeypatch):
        """A shutdown-requested owner may publish local text but not SSE status."""

        import json
        import epoch_authority
        import server.state
        from web_ui import EventBroadcaster, WebUI

        monkeypatch.setattr(
            epoch_authority,
            "strict_epoch_projection",
            lambda **_kwargs: _active_epoch(),
        )
        monkeypatch.setattr(
            server.state.app_state,
            "task_snapshot",
            lambda: {
                **_active_task(),
                "shutdown_requested": True,
                "status_eligible": False,
            },
        )
        broadcaster = EventBroadcaster()
        broadcaster.bind_authority("a" * 64)
        _, queue = broadcaster.add_client("a" * 64)

        WebUI(broadcaster).set_status(f"Master planning for v{STRICT_TARGET_V}", is_working=True)

        payload = json.loads(queue.get_nowait()["data"])
        assert payload["run_id"] is None
        assert payload["task_owner_id"] is None
        assert payload["task_lifecycle_revision"] is None

    def test_status_replay_filter_rejects_stale_inactive_or_wrong_revision(
        self,
        monkeypatch,
    ):
        import json
        import server.routes.evolution as route

        epoch = _active_epoch()
        current = {
            "msg": f"Master planning for v{STRICT_TARGET_V}",
            "is_working": True,
            "run_id": f"{STRICT_TARGET_V}#1",
            "workflow_run_id": f"generation:{STRICT_TARGET_V}:workflow-v1",
            "checkpoint_revision": 7,
            "stage": "master_planning",
            "task_owner_id": "f" * 32,
            "task_lifecycle_revision": 7,
            "emitted_at": 1_000.0,
        }
        event = {"event": "status", "data": json.dumps(current)}
        monkeypatch.setattr(route, "_live_task_snapshot", _active_task)
        monkeypatch.setattr(route.time, "time", lambda: 1_001.0)

        assert route._status_event_is_current(event, epoch) is True
        assert route._current_transient_status(
            current,
            epoch,
            task=_active_task(),
            now=1_001.0,
        ) == current
        assert route._current_transient_status(
            {**current, "checkpoint_revision": 6},
            epoch,
            task=_active_task(),
            now=1_001.0,
        ) is None
        assert route._current_transient_status(
            current,
            epoch,
            task={**_active_task(), "done": True},
            now=1_001.0,
        ) is None
        assert route._current_transient_status(
            current,
            epoch,
            task=_active_task(),
            now=1_031.0,
        ) is None
        assert route._current_transient_status(
            current,
            epoch,
            task={**_active_task(), "owner_id": "e" * 32},
            now=1_001.0,
        ) is None
        monkeypatch.setattr(
            route,
            "_live_task_snapshot",
            lambda: {**_active_task(), "owner_id": "e" * 32},
        )
        assert route._status_event_is_current(event, epoch) is False

    def test_task_owner_event_replay_rejects_replaced_owner(self, monkeypatch):
        """Owner lifecycle replay is filtered before it can clear/revive UI state."""

        import json
        import server.routes.evolution as route

        old = {
            "present": True,
            "done": False,
            "shutdown_requested": False,
            "status_eligible": True,
            "owner_id": "e" * 32,
            "lifecycle_revision": 6,
        }
        event = {"event": "task_owner", "data": json.dumps(old)}
        monkeypatch.setattr(route, "_live_task_snapshot", _active_task)

        assert route._task_owner_event_is_current(event) is False
        assert route._task_owner_event_is_current({
            "event": "task_owner",
            "data": json.dumps({
                "present": True,
                "done": False,
                "shutdown_requested": False,
                "status_eligible": True,
                "owner_id": "f" * 32,
                "lifecycle_revision": 7,
            }),
        }) is True
        assert route._task_owner_event_is_current({
            "event": "task_owner",
            "data": json.dumps({
                "present": True,
                "done": None,
                "shutdown_requested": False,
                "status_eligible": True,
                "owner_id": "f" * 32,
                "lifecycle_revision": 7,
            }),
        }) is False

    def test_stream_projection_rejects_torn_epoch_handoff_sample(
        self,
        monkeypatch,
    ):
        import server.routes.evolution as route

        epochs = iter((
            {
                "evaluation_epoch": "national_tcp_policy_v1",
                "state": "fresh_bootstrap_ready",
                "initialized": True,
                "reset_receipt_valid": True,
                "reset_receipt_digest": "a" * 64,
                "version_authority_high_water": STRICT_SOURCE_V,
                "active_bots": [],
            },
            {
                "evaluation_epoch": "national_tcp_policy_v1",
                "state": "strict_published",
                "initialized": True,
                "reset_receipt_valid": True,
                "reset_receipt_digest": "a" * 64,
                "version_authority_high_water": STRICT_TARGET_V,
                "active_bots": [bot_name(STRICT_TARGET_V)],
            },
        ))
        monkeypatch.setattr(route, "_epoch_projection", lambda: next(epochs))
        monkeypatch.setattr(
            route,
            "_handoff_projection",
            lambda epoch: {
                "projection_digest": (
                    "1" * 64
                    if epoch["version_authority_high_water"] == STRICT_SOURCE_V
                    else "2" * 64
                )
            },
        )

        epoch, handoff, digest = route._stable_stream_projection(max_attempts=1)

        assert epoch["version_authority_high_water"] == STRICT_TARGET_V
        assert handoff["projection_digest"] == "1" * 64
        assert digest is None

    def test_stream_projection_accepts_latest_handoff_revision_in_same_epoch(
        self,
        monkeypatch,
    ):
        import epoch_authority
        import server.routes.evolution as route

        projection = {
            "evaluation_epoch": "national_tcp_policy_v1",
            "state": "strict_published",
            "initialized": True,
            "reset_receipt_valid": True,
            "reset_receipt_digest": "a" * 64,
            "version_authority_high_water": STRICT_TARGET_V,
            "active_bots": [bot_name(STRICT_TARGET_V)],
        }
        revision = {"value": 2}
        epoch_reads = {"count": 0}

        def load_epoch():
            epoch_reads["count"] += 1
            if epoch_reads["count"] == 2:
                # The journal may advance after its complete projection was
                # read.  That does not invalidate the observation bracketed by
                # an unchanged epoch.
                revision["value"] = 3
            return dict(projection)

        monkeypatch.setattr(route, "_epoch_projection", load_epoch)
        monkeypatch.setattr(
            route,
            "_handoff_projection",
            lambda _epoch: {
                "projection_digest": "2" * 64,
                "record_revision": revision["value"],
            },
        )

        epoch, handoff, digest = route._stable_stream_projection(max_attempts=1)

        assert epoch == projection
        assert handoff == {
            "projection_digest": "2" * 64,
            "record_revision": 2,
        }
        assert digest == epoch_authority.epoch_stream_authority_digest(
            projection
        )
        assert revision["value"] == 3

    def test_state_fails_closed_instead_of_returning_torn_epoch_handoff(
        self,
        client,
        monkeypatch,
    ):
        import server.routes.evolution as route
        from server.routes._helpers import post_publication_handoff_projection

        monkeypatch.setattr(
            route,
            "_stable_stream_projection",
            lambda: (
                {
                    "evaluation_epoch": "national_tcp_policy_v1",
                    "state": "strict_published",
                    "initialized": True,
                    "reset_receipt_valid": True,
                    "reset_receipt_digest": "a" * 64,
                    "version_authority_high_water": STRICT_TARGET_V,
                    "current_v": STRICT_TARGET_V,
                    "next_v": STRICT_TARGET_V + 1,
                    "active_bots": [bot_name(STRICT_TARGET_V)],
                    "active_generation": None,
                },
                {"status": "pending", "projection_digest": "b" * 64},
                None,
            ),
        )
        monkeypatch.setattr(
            route,
            "_handoff_projection",
            lambda _epoch: post_publication_handoff_projection(enabled=False),
        )

        data = client.get("/api/evolution/state").json()

        assert data["epoch_state"] == "epoch_authority_unavailable"
        assert data["epoch_initialized"] is False
        assert data["stream_authority_digest"] is None
        assert data["post_publication_handoff"]["status"] == "none"

    def test_uninitialized_stream_does_not_subscribe_or_replay_ring(self, client, monkeypatch):
        import epoch_authority
        from server.app import broadcaster

        monkeypatch.setattr(
            epoch_authority,
            "strict_epoch_projection",
            lambda **_kwargs: {
                "evaluation_epoch": "national_tcp_policy_v1",
                "state": "reset_required",
                "initialized": False,
                "reset_receipt_digest": None,
                "current_v": STRICT_SOURCE_V,
                "next_v": STRICT_TARGET_V,
            },
        )

        def forbidden_subscription(*_args, **_kwargs):
            raise AssertionError("pre-reset stream subscribed to broadcaster")

        monkeypatch.setattr(broadcaster, "add_client", forbidden_subscription)

        with client.stream("GET", "/api/evolution/stream") as response:
            body = "".join(response.iter_text())

        assert response.status_code == 200
        assert "event: epoch_blocked" in body
        assert "national_tcp_policy_v1" in body
        assert "event: stream_closed" in body

    def test_stale_expected_authority_is_fenced_before_subscription_or_replay(
        self,
        client,
        monkeypatch,
    ):
        import epoch_authority
        from server.app import broadcaster

        target_v = STRICT_TARGET_V
        projection = {
            "evaluation_epoch": "national_tcp_policy_v1",
            "state": "strict_published",
            "initialized": True,
            "reset_receipt_valid": True,
            "reset_receipt_digest": "a" * 64,
            "version_authority_high_water": target_v,
            "active_bots": [bot_name(target_v)],
            "current_v": target_v,
            "next_v": target_v + 1,
        }
        current = epoch_authority.epoch_stream_authority_digest(projection)
        assert current is not None and current != "f" * 64
        monkeypatch.setattr(
            epoch_authority,
            "strict_epoch_projection",
            lambda **_kwargs: dict(projection),
        )

        def forbidden_subscription(*_args, **_kwargs):
            raise AssertionError("stale expected identity subscribed to replay ring")

        monkeypatch.setattr(broadcaster, "add_client", forbidden_subscription)
        broadcaster.bind_authority("f" * 64)
        broadcaster.broadcast("history", {"msg": "stale-ring-marker"})

        with client.stream(
            "GET",
            f"/api/evolution/stream?authority={'f' * 64}",
        ) as response:
            body = "".join(response.iter_text())

        assert response.status_code == 200
        assert "event: epoch_blocked" in body
        assert "stream_authority_mismatch" in body
        assert "stale-ring-marker" not in body
        assert broadcaster.get_stats()["authority_identity"] == current
