"""Tests for /api/control/* endpoints."""

import asyncio
import json
import sys


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

    def test_runtime_override_does_not_persist_user_config(self, tmp_path):
        from server.state import AppState

        path = tmp_path / "app_config.json"
        state = AppState(config_file=path)
        state.update_config(daemon_enabled=True, daemon_workers=2, daemon_pairs=4)

        runtime = state.override_runtime_config(daemon_enabled=False, daemon_workers=3, daemon_pairs=9)

        assert runtime["daemon_enabled"] is False
        assert runtime["daemon_workers"] == 3
        assert runtime["daemon_pairs"] == 9
        saved = json.loads(path.read_text())
        assert saved["daemon_enabled"] is True
        assert saved["daemon_workers"] == 2
        assert saved["daemon_pairs"] == 4

        reloaded = AppState(config_file=path)
        assert reloaded.get_config()["daemon_enabled"] is True
        assert reloaded.get_config()["daemon_workers"] == 2
        assert reloaded.get_config()["daemon_pairs"] == 4


class TestStatus:
    def test_returns_state(self, client):
        resp = client.get("/api/control/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "running" in data
        assert "mode" in data
        assert "daemon_enabled" in data

    def test_status_uses_active_checkpoint_target(self, client, monkeypatch):
        import epoch_authority
        import server.routes.control as control
        from server.state import app_state

        app_state.bootstrap(142)
        monkeypatch.setattr(
            epoch_authority,
            "strict_epoch_projection",
            lambda: {
                "current_v": 143,
                "next_v": 144,
                "strict_generation_count": 1,
                "active_generation": {
                    "next_v": 144,
                    "source_v": 143,
                    "stage": "prepared",
                    "run_id": "generation:144:strict",
                    "workflow_run_id": "generation:144:strict",
                    "attempt": {"generation": 0, "audit": 0, "precommit": 0},
                },
                "evaluation_epoch": "national_tcp_policy_v1",
                "state": "strict_published",
                "initialized": True,
                "version_authority_high_water": 143,
                "strict_published_versions": [143],
                "active_bots": ["national_v143"],
                "reset_receipt_valid": True,
                "reset_receipt_issues": [],
                "operator_action": None,
                "operator_command": None,
                "ignored_checkpoint": None,
                "max_committed_v": 143,
            },
        )
        monkeypatch.setattr(
            epoch_authority,
            "unpublished_candidate_versions",
            lambda: [],
        )

        resp = client.get("/api/control/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["current_v"] == 143
        assert data["next_v"] == 144
        assert data["generation_count"] == 1
        assert data["active_generation"]["stage"] == "prepared"
        assert data["evaluation_epoch"] == "national_tcp_policy_v1"

    def test_status_never_promotes_legacy_checkpoint(self, client, monkeypatch):
        import epoch_authority
        import server.routes.control as control
        from server.state import app_state

        app_state.bootstrap(155)
        monkeypatch.setattr(
            epoch_authority,
            "strict_epoch_projection",
            lambda: {
                "current_v": 142,
                "next_v": 143,
                "strict_generation_count": 0,
                "active_generation": None,
                "evaluation_epoch": "national_tcp_policy_v1",
                "state": "reset_required",
                "initialized": False,
                "version_authority_high_water": 142,
                "strict_published_versions": [],
                "active_bots": [],
                "reset_receipt_valid": False,
                "reset_receipt_issues": ["policy_epoch_reset_receipt_missing_or_unsafe"],
                "operator_action": "execute_policy_epoch_reset",
                "operator_command": "python scripts/reset_national_tcp_policy_epoch.py --execute --acknowledge-runtime-checkout",
                "ignored_checkpoint": {
                    "next_v": 155,
                    "source_v": 142,
                    "stage": "direction_audited",
                    "reason": "checkpoint_not_bound_to_strict_epoch",
                    "issues": ["checkpoint_schema_version_missing_or_mismatch"],
                },
                "max_committed_v": 142,
            },
        )
        monkeypatch.setattr(
            epoch_authority,
            "unpublished_candidate_versions",
            lambda: [155],
        )

        resp = client.get("/api/control/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["current_v"] == 142
        assert data["next_v"] == 143
        assert data["generation_count"] == 0
        assert data["active_generation"] is None
        assert data["ignored_checkpoint"]["next_v"] == 155
        assert data["unpublished_candidate_versions"] == [155]
        assert data["epoch_initialized"] is False

    def test_status_projection_failure_is_complete_and_never_leaks_app_state(self, client, monkeypatch):
        import epoch_authority
        from server.state import app_state

        app_state.bootstrap(155)

        def unavailable():
            raise RuntimeError("projection evidence unreadable")

        monkeypatch.setattr(epoch_authority, "strict_epoch_projection", unavailable)

        resp = client.get("/api/control/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["epoch_state"] == "epoch_authority_unavailable"
        assert data["epoch_initialized"] is False
        assert data["current_v"] == 0
        assert data["next_v"] == 0
        assert data["version_authority_high_water"] == 0
        assert data["strict_generation_count"] == 0
        assert data["strict_published_versions"] == []
        assert data["active_bots"] == []
        assert data["active_generation"] is None
        assert data["unpublished_candidate_versions"] == []
        assert data["reset_receipt_issues"] == [
            "canonical_epoch_projection_unavailable"
        ]
        assert "projection evidence unreadable" in data["status_sync_error"]

    def test_status_uses_current_epoch_abandoned_floor_without_checkpoint(self, client, monkeypatch):
        import epoch_authority
        import server.routes.control as control
        from server.state import app_state

        app_state.bootstrap(143)
        monkeypatch.setattr(
            epoch_authority,
            "strict_epoch_projection",
            lambda: {
                "current_v": 143,
                "next_v": 145,
                "strict_generation_count": 1,
                "active_generation": None,
                "evaluation_epoch": "national_tcp_policy_v1",
                "state": "strict_published",
                "initialized": True,
                "version_authority_high_water": 143,
                "strict_published_versions": [143],
                "active_bots": ["national_v143"],
                "reset_receipt_valid": True,
                "reset_receipt_issues": [],
                "operator_action": None,
                "operator_command": None,
                "ignored_checkpoint": None,
                "max_committed_v": 143,
            },
        )
        monkeypatch.setattr(
            epoch_authority,
            "unpublished_candidate_versions",
            lambda: [],
        )

        resp = client.get("/api/control/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["current_v"] == 143
        assert data["next_v"] == 145
        assert data["generation_count"] == 1
        assert data["active_generation"] is None

    def test_view_only_lifespan_does_not_start_orchestrator(self, monkeypatch):
        import server.app as app_module
        from server.state import app_state

        events = []

        class TrapOrchestrator:
            def __getattr__(self, name):
                raise AssertionError(f"view-only mode imported orchestrator.{name}")

        app_state.stop_running()
        monkeypatch.setenv("POK_WEB_VIEW_ONLY", "1")
        monkeypatch.setitem(sys.modules, "orchestrator", TrapOrchestrator())
        monkeypatch.setattr(app_module, "configure_logging", lambda **_kwargs: None)
        monkeypatch.setattr(app_module.web_ui, "log_history", lambda *args: events.append(args))

        async def run_lifespan():
            async with app_module.lifespan(app_module.app):
                assert app_state.to_dict()["running"] is False
                assert app_state.task_snapshot()["present"] is False

        asyncio.run(run_lifespan())

        assert any("view-only mode" in event[0] for event in events)

    def test_health_reports_stopped_state(self, client, monkeypatch):
        import server.routes.control as control
        from server.state import app_state

        app_state.set_running(False)
        monkeypatch.setattr(
            control,
            "_daemon_health_snapshot",
            lambda: {"exists": False, "pid": None, "alive": False},
        )
        monkeypatch.setattr(
            control,
            "_read_pipeline_health",
            lambda _status: {"exists": False, "stage": None},
        )

        resp = client.get("/api/control/health")

        assert resp.status_code == 200
        data = resp.json()
        assert data["overall"] == "stopped"
        assert "evolution_not_running" in data["issues"]
        assert data["task"]["present"] in (False, True)

    def test_health_degrades_when_running_task_is_done(self, client, monkeypatch):
        import server.routes.control as control
        from server.state import app_state

        app_state.set_running(True)
        app_state.override_runtime_config(daemon_enabled=True)
        monkeypatch.setattr(
            app_state,
            "task_snapshot",
            lambda: {
                "present": True,
                "done": True,
                "cancelled": False,
                "shutdown_requested": False,
            },
        )
        monkeypatch.setattr(
            control,
            "_daemon_health_snapshot",
            lambda: {
                "exists": True,
                "pid": 123,
                "alive": True,
                "heartbeat_stale": False,
            },
        )
        monkeypatch.setattr(
            control,
            "_read_pipeline_health",
            lambda _status: {"exists": False, "stage": None},
        )

        resp = client.get("/api/control/health")

        assert resp.status_code == 200
        data = resp.json()
        assert data["overall"] == "degraded"
        assert "orchestrator_task_not_active" in data["issues"]
        app_state.set_running(False)

    def test_health_degrades_when_daemon_heartbeat_is_stale(self, client, monkeypatch):
        import server.routes.control as control
        from server.state import app_state

        app_state.set_running(True)
        app_state.override_runtime_config(daemon_enabled=True)
        monkeypatch.setattr(
            app_state,
            "task_snapshot",
            lambda: {
                "present": True,
                "done": False,
                "cancelled": False,
                "shutdown_requested": False,
            },
        )
        monkeypatch.setattr(
            control,
            "_daemon_health_snapshot",
            lambda: {
                "exists": True,
                "pid": 123,
                "alive": True,
                "heartbeat_stale": True,
                "heartbeat_age_sec": 999,
            },
        )
        monkeypatch.setattr(
            control,
            "_read_pipeline_health",
            lambda _status: {"exists": True, "stage": "direction_audited"},
        )

        resp = client.get("/api/control/health")

        assert resp.status_code == 200
        data = resp.json()
        assert data["overall"] == "degraded"
        assert "daemon_heartbeat_stale" in data["issues"]
        app_state.set_running(False)

    def test_health_degrades_when_checkpoint_recovery_is_unrecoverable(self, client, monkeypatch):
        import server.routes.control as control
        from server.state import app_state

        app_state.set_running(True)
        monkeypatch.setattr(
            app_state,
            "task_snapshot",
            lambda: {
                "present": True,
                "done": False,
                "cancelled": False,
                "shutdown_requested": False,
            },
        )
        monkeypatch.setattr(
            control,
            "_daemon_health_snapshot",
            lambda: {
                "exists": True,
                "pid": 123,
                "alive": True,
                "heartbeat_stale": False,
            },
        )
        monkeypatch.setattr(
            control,
            "_read_pipeline_health",
            lambda _status: {
                "exists": True,
                "stage": "workers_done",
                "recovery": {
                    "recoverable": False,
                    "issues": ["repo_baseline_head_mismatch"],
                },
            },
        )

        resp = client.get("/api/control/health")

        assert resp.status_code == 200
        data = resp.json()
        assert data["overall"] == "degraded"
        assert "pipeline_repo_baseline_head_mismatch" in data["issues"]
        app_state.set_running(False)


class TestDecisions:
    def test_returns_list(self, client):
        resp = client.get("/api/control/decisions")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


class TestTools:
    def test_catalog_is_explicit_http_capabilities_only(self, client):
        resp = client.get("/api/control/tools")
        assert resp.status_code == 200
        data = resp.json()
        capabilities = data["capabilities"]
        assert capabilities
        assert {item["id"] for item in capabilities} == set(data["tools"])
        assert all(
            set(item) == {
                "id", "method", "path", "mutation", "enabled", "blocked_reason"
            }
            for item in capabilities
        )
        assert {
            "prepare_next_gen", "execute_workers", "run_quality_gates",
            "run_precommit_eval", "commit_bot", "abandon_generation",
            "cleanup_incomplete", "reap_incomplete", "start_daemon",
            "stop_daemon",
        }.isdisjoint(data["tools"])
        assert data["operator_auth_required"] is True
        assert data["operator_token_header"] == "X-Control-Token"
        assert "operator-secret" not in resp.text

    def test_old_executor_is_gone_for_unknown_name(self, client):
        resp = client.post("/api/control/tool/nonexistent_tool_xyz", json={"args": {}})
        assert resp.status_code == 410
        assert resp.json()["detail"]["code"] == "control_tool_executor_retired"

    def test_old_executor_never_calls_mcp_handler(self, client, monkeypatch):
        import tools

        class TrapTool:
            name = "prepare_next_gen"

            @staticmethod
            async def handler(_args):
                raise AssertionError("retired HTTP executor invoked MCP handler")

        monkeypatch.setattr(tools, "all_tools", [TrapTool()])

        for name in (
            "get_status", "prepare_next_gen", "execute_workers",
            "run_quality_gates", "run_precommit_eval", "commit_bot",
            "abandon_generation", "cleanup_incomplete", "reap_incomplete",
            "start_daemon", "stop_daemon",
        ):
            response = client.post(
                f"/api/control/tool/{name}",
                json={"args": {"dangerous": True}},
            )
            assert response.status_code == 410


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


class TestRetiredEvolutionReset:
    def test_destructive_reset_endpoint_is_not_registered(self):
        from server.app import app

        assert not any(
            route.path == "/api/control/reset"
            for route in app.routes
        )


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
