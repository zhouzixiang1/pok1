"""Fail-closed launch boundaries for the strict national-policy epoch."""

from __future__ import annotations

import argparse
import asyncio
import sys

import pytest

from epoch_authority import (
    require_policy_epoch_initialized as _real_require_policy_epoch_initialized,
)


def _state(name: str, *, initialized: bool) -> dict:
    return {
        "evaluation_epoch": "national_tcp_policy_v1",
        "state": name,
        "initialized": initialized,
        "strict_published": name == "strict_published",
        "reset_receipt_valid": name == "fresh_bootstrap_ready",
        "reset_receipt_digest": "a" * 64 if initialized else None,
        "reset_receipt_issues": [] if initialized else ["reset_missing"],
        "version_authority_high_water": 143 if name == "strict_published" else 142,
        "first_strict_version": 143,
        "operator_action": None if initialized else "execute_policy_epoch_reset",
        "operator_command": None if initialized else "reset-command",
    }


def _deny(operation: str):
    from epoch_authority import PolicyEpochInitializationRequired

    raise PolicyEpochInitializationRequired(
        operation,
        _state("reset_required", initialized=False),
    )


def test_control_start_returns_409_before_marking_running(client, monkeypatch):
    import epoch_authority
    from server.state import app_state

    app_state.stop_running()
    monkeypatch.setattr(epoch_authority, "require_policy_epoch_initialized", _deny)

    response = client.post("/api/control/start")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "policy_epoch_not_initialized"
    assert detail["operation"] == "control_start_evolution"
    assert detail["epoch"]["state"] == "reset_required"
    assert app_state.to_dict()["running"] is False
    assert app_state.task_snapshot()["present"] is False


def test_control_mutations_cannot_start_daemon_before_reset(client, monkeypatch):
    import epoch_authority
    import daemon_management

    monkeypatch.setattr(epoch_authority, "require_policy_epoch_initialized", _deny)
    monkeypatch.setattr(
        daemon_management.subprocess,
        "Popen",
        lambda *_a, **_k: pytest.fail("Popen reached before policy reset"),
    )

    config = client.put("/api/control/config", json={"daemon_enabled": True})
    tool = client.post(
        "/api/control/tool/start_daemon",
        json={"args": {"workers": 1, "pairs": 1}},
    )

    assert config.status_code == 409
    assert tool.status_code == 410


def test_control_hides_retired_session_and_exposes_tool_blocking(
    client, monkeypatch
):
    import epoch_authority
    import server.routes.control as control

    control.ORCHESTRATOR_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    control.ORCHESTRATOR_SESSION_FILE.write_text(
        '{"session_id": "retired-session"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(epoch_authority, "require_policy_epoch_initialized", _deny)

    session = client.get("/api/control/orchestrator/session")
    clear = client.delete("/api/control/orchestrator/session")
    tools = client.get("/api/control/tools")

    assert session.status_code == 200
    assert session.json()["session_id"] is None
    assert session.json()["active"] is False
    assert session.json()["blocked"] is True
    assert session.json()["epoch_state"] == "reset_required"
    assert clear.status_code == 409
    assert control.ORCHESTRATOR_SESSION_FILE.exists()
    catalog = tools.json()
    assert catalog["epoch_initialized"] is False
    assert "read_status" in catalog["enabled_tools"]
    assert "stop_evolution" in catalog["enabled_tools"]
    assert "start_evolution" in catalog["blocked_tools"]
    assert "update_config" in catalog["blocked_tools"]
    assert "prepare_next_gen" not in catalog["tools"]


def test_pre_reset_read_only_control_calls_do_not_persist_events(
    client, monkeypatch
):
    import epoch_authority
    import server.routes.control as control
    import system_log

    projection = {
        **_state("reset_required", initialized=False),
        "current_v": 142,
        "next_v": 143,
        "max_committed_v": 142,
        "abandoned_floor": 0,
        "active_bots": [],
        "active_bots_count": 0,
        "strict_published_versions": [],
        "strict_generation_count": 0,
        "active_generation": None,
        "ignored_checkpoint": None,
    }
    monkeypatch.setattr(epoch_authority, "require_policy_epoch_initialized", _deny)
    monkeypatch.setattr(epoch_authority, "strict_epoch_projection", lambda: projection)
    monkeypatch.setattr(epoch_authority, "unpublished_candidate_versions", lambda: [])
    monkeypatch.setattr(
        system_log,
        "log_system_event",
        lambda *_a, **_k: pytest.fail("pre-reset status persisted an event"),
    )

    assert client.get("/api/control/status").status_code == 200
    assert client.get("/api/control/health").status_code == 200
    assert client.get("/api/control/config").status_code == 200
    assert client.get("/api/control/decisions").status_code == 200
    assert client.get("/api/control/tools").status_code == 200
    assert client.post(
        "/api/control/tool/get_status", json={"args": {}}
    ).status_code == 410
    assert client.post(
        "/api/control/tool/not-registered", json={"args": {}}
    ).status_code == 410


def test_daemon_management_denies_before_pid_cleanup_or_popen(
    tmp_path, monkeypatch
):
    import epoch_authority
    import evolution_infra
    import daemon_management

    results = tmp_path / "results"
    results.mkdir()
    pid_file = results / ".daemon_pid"
    original = '{"pid": 999999, "ppid": 1}\n'
    pid_file.write_text(original, encoding="utf-8")

    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results)
    monkeypatch.setattr(epoch_authority, "require_policy_epoch_initialized", _deny)
    monkeypatch.setattr(daemon_management, "daemon_proc", None)
    monkeypatch.setattr(
        daemon_management.subprocess,
        "Popen",
        lambda *_a, **_k: pytest.fail("Popen reached before policy reset"),
    )

    with pytest.raises(Exception, match="requires initialized"):
        daemon_management.start_daemon(workers=1, pairs=1)

    assert pid_file.read_text(encoding="utf-8") == original


def test_pre_reset_daemon_stop_is_safe_but_does_not_write_event(
    tmp_path, monkeypatch
):
    import epoch_authority
    import daemon_management

    monkeypatch.setattr(epoch_authority, "require_policy_epoch_initialized", _deny)
    monkeypatch.setattr(daemon_management, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(daemon_management, "daemon_proc", None)
    monkeypatch.setattr(
        daemon_management,
        "_persist_system_event",
        lambda *_a, **_k: pytest.fail("stop contaminated the retired event ledger"),
    )

    daemon_management.stop_daemon()


@pytest.mark.parametrize("epoch_state", ["fresh_bootstrap_ready", "strict_published"])
def test_valid_epoch_initialization_reaches_daemon_popen(
    epoch_state, tmp_path, monkeypatch
):
    import epoch_authority
    import evolution_infra
    import daemon_management

    class PopenReached(RuntimeError):
        pass

    monkeypatch.setattr(
        epoch_authority,
        "require_policy_epoch_initialized",
        lambda _operation: _state(epoch_state, initialized=True),
    )
    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(daemon_management, "daemon_proc", None)
    monkeypatch.setattr(daemon_management, "_daemon_shutting_down", True)
    monkeypatch.setattr(
        daemon_management.subprocess,
        "Popen",
        lambda *_a, **_k: (_ for _ in ()).throw(PopenReached()),
    )

    with pytest.raises(PopenReached):
        daemon_management.start_daemon(workers=1, pairs=1)


def test_daemon_cli_denies_before_creating_results_or_writer_lease(
    tmp_path, monkeypatch
):
    import epoch_authority
    import elo_daemon

    results = tmp_path / "not-created"
    monkeypatch.setattr(elo_daemon, "RESULTS_DIR", results)
    monkeypatch.setattr(epoch_authority, "require_policy_epoch_initialized", _deny)
    monkeypatch.setattr(sys, "argv", ["elo_daemon.py", "--once"])

    with pytest.raises(Exception, match="requires initialized"):
        elo_daemon.main()

    assert not results.exists()


def test_orchestrator_cli_and_direct_loop_fail_closed(monkeypatch):
    import epoch_authority
    import orchestrator

    monkeypatch.setattr(epoch_authority, "require_policy_epoch_initialized", _deny)

    args = argparse.Namespace(
        dry_run=True,
        one_gen=False,
        no_daemon=True,
        max_turns=None,
    )
    with pytest.raises(Exception, match="requires initialized"):
        asyncio.run(orchestrator.run_orchestrator_cli(args))

    class UI:
        def __init__(self):
            self.messages = []
            self.statuses = []

        def log_history(self, message, severity="info"):
            self.messages.append((message, severity))

        def set_status(self, message, is_working=False):
            self.statuses.append((message, is_working))

    ui = UI()
    assert asyncio.run(orchestrator.orchestrator_loop(ui, no_daemon=True)) is None
    assert ui.statuses == [("Stopped: reset_required", False)]
    assert any("not started" in message for message, _ in ui.messages)


def test_non_view_web_lifespan_stays_stopped_when_reset_required(monkeypatch):
    import epoch_authority
    import server.app as app_module
    from server.state import app_state

    async def noop(*_args, **_kwargs):
        return None

    class TrapOrchestrator:
        def __getattr__(self, name):
            raise AssertionError(f"orchestrator imported while reset is required: {name}")

    app_state.stop_running()
    monkeypatch.delenv("POK_WEB_VIEW_ONLY", raising=False)
    monkeypatch.setattr(epoch_authority, "require_policy_epoch_initialized", _deny)
    monkeypatch.setattr(app_module, "configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr(app_module.arena_manager, "startup", noop)
    monkeypatch.setattr(app_module.arena_manager, "shutdown", noop)
    monkeypatch.setitem(sys.modules, "orchestrator", TrapOrchestrator())

    async def exercise():
        async with app_module.lifespan(app_module.app):
            assert app_state.to_dict()["running"] is False
            assert app_state.task_snapshot()["present"] is False

    asyncio.run(exercise())


def test_view_only_lifespan_can_read_reset_required_status(monkeypatch):
    import epoch_authority
    import server.app as app_module
    import server.routes.control as control
    from server.state import app_state

    async def noop(*_args, **_kwargs):
        return None

    projection = {
        **_state("reset_required", initialized=False),
        "current_v": 142,
        "next_v": 143,
        "max_committed_v": 142,
        "abandoned_floor": 0,
        "active_bots": [],
        "active_bots_count": 0,
        "strict_published_versions": [],
        "strict_generation_count": 0,
        "active_generation": None,
        "ignored_checkpoint": None,
    }
    app_state.stop_running()
    monkeypatch.setenv("POK_WEB_VIEW_ONLY", "1")
    monkeypatch.setattr(epoch_authority, "require_policy_epoch_initialized", _deny)
    monkeypatch.setattr(epoch_authority, "strict_epoch_projection", lambda: projection)
    monkeypatch.setattr(epoch_authority, "unpublished_candidate_versions", lambda: [])
    monkeypatch.setattr(app_module, "configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr(app_module.arena_manager, "startup", noop)
    monkeypatch.setattr(app_module.arena_manager, "shutdown", noop)

    async def exercise():
        async with app_module.lifespan(app_module.app):
            status = await control.control_status()
            assert status["running"] is False
            assert status["epoch_state"] == "reset_required"
            assert status["operator_action"] == "execute_policy_epoch_reset"

    asyncio.run(exercise())


@pytest.mark.parametrize("epoch_state", ["fresh_bootstrap_ready", "strict_published"])
def test_canonical_guard_allows_both_valid_initialization_states(
    epoch_state, monkeypatch
):
    import epoch_authority

    state = _state(epoch_state, initialized=True)
    monkeypatch.setattr(epoch_authority, "policy_epoch_initialization", lambda: state)

    assert _real_require_policy_epoch_initialized("test") == state


def test_fresh_bootstrap_receipt_allows_web_orchestrator_launch(monkeypatch):
    import epoch_authority
    import orchestrator
    import server.app as app_module
    from server.state import app_state

    started = asyncio.Event()

    async def fake_loop(*_args, **_kwargs):
        started.set()

    async def noop(*_args, **_kwargs):
        return None

    app_state.stop_running()
    app_state.override_runtime_config(daemon_enabled=False)
    monkeypatch.delenv("POK_WEB_VIEW_ONLY", raising=False)
    monkeypatch.setattr(
        epoch_authority,
        "require_policy_epoch_initialized",
        lambda _operation: _state("fresh_bootstrap_ready", initialized=True),
    )
    monkeypatch.setattr(orchestrator, "orchestrator_loop", fake_loop)
    monkeypatch.setattr(app_module, "configure_logging", lambda **_kwargs: None)
    monkeypatch.setattr(app_module.arena_manager, "startup", noop)
    monkeypatch.setattr(app_module.arena_manager, "shutdown", noop)

    async def exercise():
        async with app_module.lifespan(app_module.app):
            await asyncio.wait_for(started.wait(), timeout=1)
            assert app_state.to_dict()["running"] is True

    asyncio.run(exercise())
