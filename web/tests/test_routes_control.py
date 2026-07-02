"""Tests for /api/control/* endpoints."""

import json
import sys
from types import SimpleNamespace


class TestConfig:
    def test_get(self, client):
        resp = client.get("/api/control/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "daemon_enabled" in data
        assert "daemon_workers" in data
        assert "daemon_pairs" in data

    def test_set_partial(self, client):
        # Get current config first
        orig = client.get("/api/control/config").json()
        resp = client.put("/api/control/config", json={"daemon_pairs": 3})
        assert resp.status_code == 200
        data = resp.json()
        assert data["daemon_pairs"] == 3
        assert data["daemon_enabled"] == orig["daemon_enabled"]
        # Restore
        client.put("/api/control/config", json={"daemon_pairs": orig["daemon_pairs"]})

    def test_set_invalid_type(self, client):
        resp = client.put("/api/control/config", json={"daemon_workers": "not_a_number"})
        assert resp.status_code == 422


class TestStatus:
    def test_returns_state(self, client):
        resp = client.get("/api/control/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "running" in data
        assert "mode" in data
        assert "daemon_enabled" in data

    def test_status_uses_active_checkpoint_target(self, client, monkeypatch):
        import server.routes.control as control
        from server.state import app_state

        app_state.bootstrap(224)
        control._last_status_sync_correction = None
        fake_evolution_core = SimpleNamespace(
            compute_next_generation_v=lambda current_v, max_committed_v, abandoned_floor: max(
                current_v, max_committed_v, abandoned_floor
            ) + 1,
            find_abandoned_version_floor=lambda: 230,
            find_current_v=lambda: 224,
            find_max_committed_v=lambda: 230,
            read_pipeline_checkpoint=lambda: {
                "next_v": 231,
                "source_v": 224,
                "stage": "prepared",
                "run_id": "231#0",
            },
        )
        monkeypatch.setitem(sys.modules, "evolution_core", fake_evolution_core)

        resp = client.get("/api/control/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["current_v"] == 224
        assert data["next_v"] == 231
        assert data["generation_count"] == 224
        assert data["active_generation"]["stage"] == "prepared"

    def test_status_derives_run_id_and_attempt_when_checkpoint_is_legacy(self, client, monkeypatch):
        import server.routes.control as control
        from server.state import app_state

        app_state.bootstrap(224)
        control._last_status_sync_correction = None
        fake_evolution_core = SimpleNamespace(
            compute_next_generation_v=lambda current_v, max_committed_v, abandoned_floor: max(
                current_v, max_committed_v, abandoned_floor
            ) + 1,
            find_abandoned_version_floor=lambda: 230,
            find_current_v=lambda: 224,
            find_max_committed_v=lambda: 230,
            read_pipeline_checkpoint=lambda: {
                "next_v": 231,
                "source_v": 224,
                "stage": "direction_audited",
                "generation_attempt": 2,
                "audit_attempt": None,
                "precommit_attempt": 1,
            },
        )
        monkeypatch.setitem(sys.modules, "evolution_core", fake_evolution_core)

        resp = client.get("/api/control/status")

        assert resp.status_code == 200
        active = resp.json()["active_generation"]
        assert active["run_id"] == "231#2"
        assert active["attempt"] == {"generation": 2, "audit": 0, "precommit": 1}

    def test_status_uses_abandoned_floor_without_checkpoint(self, client, monkeypatch):
        import server.routes.control as control
        from server.state import app_state

        app_state.bootstrap(254)
        control._last_status_sync_correction = None
        fake_evolution_core = SimpleNamespace(
            compute_next_generation_v=lambda current_v, max_committed_v, abandoned_floor: max(
                current_v, max_committed_v, abandoned_floor
            ) + 1,
            find_abandoned_version_floor=lambda: 255,
            find_current_v=lambda: 254,
            find_max_committed_v=lambda: 254,
            read_pipeline_checkpoint=lambda: {},
        )
        monkeypatch.setitem(sys.modules, "evolution_core", fake_evolution_core)

        resp = client.get("/api/control/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["current_v"] == 254
        assert data["next_v"] == 256
        assert data["generation_count"] == 254
        assert data["active_generation"] is None


class TestDecisions:
    def test_returns_list(self, client):
        resp = client.get("/api/control/decisions")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


class TestTools:
    def test_list(self, client):
        resp = client.get("/api/control/tools")
        assert resp.status_code == 200
        data = resp.json()
        assert "tools" in data
        assert len(data["tools"]) > 0

    def test_call_unknown(self, client):
        resp = client.post("/api/control/tool/nonexistent_tool_xyz", json={"args": {}})
        assert resp.status_code == 404

    def test_call_tool_logs_request_success_and_redacts_args(self, client, monkeypatch):
        import server.routes.control as control

        events = []

        async def fake_tool(args):
            assert args["token"] == "secret-value"
            return {"content": [{"type": "text", "text": "ok"}]}

        monkeypatch.setattr(control, "_tool_map", {"fake_tool": fake_tool})
        monkeypatch.setattr(control, "_control_log", lambda *event: events.append(event))

        resp = client.post(
            "/api/control/tool/fake_tool",
            json={"args": {"token": "secret-value", "limit": 3, "payload": {"a": 1}}},
        )

        assert resp.status_code == 200
        assert [e[0] for e in events] == ["control.tool_requested", "control.tool_succeeded"]
        assert events[0][3]["tool"] == "fake_tool"
        assert events[0][3]["args"]["token"] == "<redacted>"
        assert events[0][3]["args"]["payload"] == {"type": "dict", "keys": ["a"]}


class TestOrchestratorSession:
    def test_get(self, client):
        resp = client.get("/api/control/orchestrator/session")
        assert resp.status_code == 200
        data = resp.json()
        assert "session_id" in data
        assert "active" in data

    def test_clear(self, client):
        resp = client.delete("/api/control/orchestrator/session")
        assert resp.status_code == 200
        data = resp.json()
        assert "cleared" in data


class TestStop:
    def test_stop(self, client):
        resp = client.post("/api/control/stop")
        assert resp.status_code == 200
        assert resp.json()["status"] == "stopped"


class TestReset:
    def test_reset_does_not_auto_stage_or_commit(self, client, monkeypatch):
        import server.routes.control as control
        import orchestrator

        client.post("/api/control/stop")
        calls = []
        events = []

        def fake_run(cmd, **_kwargs):
            calls.append(tuple(cmd))
            if tuple(cmd) == ("git", "status", "--short"):
                return SimpleNamespace(
                    returncode=0,
                    stdout=" M web/core/experience_pool.md\n?? web/core/results/tmp.json\n",
                    stderr="",
                )
            raise AssertionError(f"unexpected subprocess call: {cmd}")

        async def fake_loop(*_args, **_kwargs):
            return None

        fake_reset_module = SimpleNamespace(
            reset_evolution=lambda: {"reset_files": ["experience_pool.md"], "deleted_bot_dirs": []}
        )
        monkeypatch.setitem(sys.modules, "reset", fake_reset_module)
        monkeypatch.setattr(orchestrator, "orchestrator_loop", fake_loop)
        monkeypatch.setattr(control.subprocess, "run", fake_run)
        monkeypatch.setattr(control, "_control_log", lambda *event: events.append(event))

        resp = client.post("/api/control/reset")
        data = resp.json()

        assert resp.status_code == 200
        assert data["status"] == "reset_complete"
        assert data["git_status"]["entry_count"] == 2
        assert ("git", "status", "--short") in calls
        assert not any(call[:2] == ("git", "add") for call in calls)
        assert not any(call[:2] == ("git", "commit") for call in calls)
        assert any(event[0] == "control.reset_git_status" for event in events)
        client.post("/api/control/stop")


class TestStartConflict:
    def test_start_when_not_running(self, client, monkeypatch):
        client.post("/api/control/stop")
        import orchestrator
        async def fake_loop(*a, **kw):
            pass
        monkeypatch.setattr(orchestrator, "orchestrator_loop", fake_loop)
        resp = client.post("/api/control/start")
        assert resp.status_code == 200
        client.post("/api/control/stop")
