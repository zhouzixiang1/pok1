"""Adversarial authorization tests for the operator control HTTP boundary."""

from __future__ import annotations

from starlette.testclient import TestClient

from conftest import STRICT_SOURCE_V, STRICT_TARGET_V
from server.state import app_state
from testclient_compat import backend_options_for_testclient


def _client(
    app,
    *,
    address: str,
    origin: str | None,
    token: str | None = None,
) -> TestClient:
    headers = {}
    if origin is not None:
        headers["Origin"] = origin
    if token is not None:
        headers["X-Control-Token"] = token
    return TestClient(
        app,
        base_url="http://127.0.0.1",
        headers=headers,
        client=(address, 42_424),
        backend_options=backend_options_for_testclient(),
    )


def _forbidden(response, operation: str) -> None:
    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail["code"] == "operator_control_forbidden"
    assert detail["operation"] == operation


def test_remote_same_origin_forgery_cannot_reach_any_control_mutation(
    app, monkeypatch
):
    import server.routes.control as control

    monkeypatch.delenv("POK_CONTROL_TOKEN", raising=False)
    control.ORCHESTRATOR_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    control.ORCHESTRATOR_SESSION_FILE.write_text(
        '{"session_id":"must-survive"}\n', encoding="utf-8"
    )

    def trap(*_args, **_kwargs):
        raise AssertionError("mutation handler reached after failed authorization")

    monkeypatch.setattr(app_state, "try_set_running", trap)
    monkeypatch.setattr(app_state, "request_shutdown", trap)
    monkeypatch.setattr(app_state, "update_config", trap)

    with _client(
        app,
        address="203.0.113.10",
        origin="http://127.0.0.1",
    ) as remote:
        _forbidden(
            remote.post("/api/control/start"),
            "control_start_evolution",
        )
        _forbidden(
            remote.post("/api/control/stop"),
            "control_stop_evolution",
        )
        _forbidden(
            remote.put("/api/control/config", json={"daemon_pairs": 8}),
            "control_config_update",
        )
        _forbidden(
            remote.delete("/api/control/orchestrator/session"),
            "control_orchestrator_session_clear",
        )

    assert control.ORCHESTRATOR_SESSION_FILE.exists()


def test_loopback_cross_origin_and_missing_origin_are_rejected(app, monkeypatch):
    monkeypatch.delenv("POK_CONTROL_TOKEN", raising=False)

    with _client(
        app,
        address="127.0.0.1",
        origin="https://attacker.example",
    ) as cross_origin:
        _forbidden(
            cross_origin.post("/api/control/stop"),
            "control_stop_evolution",
        )

    with _client(app, address="127.0.0.1", origin=None) as no_origin:
        _forbidden(
            no_origin.post("/api/control/stop"),
            "control_stop_evolution",
        )

    # Matching attacker-controlled Origin and Host must not turn a DNS-rebound
    # loopback connection into operator authority.
    with _client(
        app,
        address="127.0.0.1",
        origin="http://rebind.attacker.example",
    ) as rebound:
        _forbidden(
            rebound.post(
                "/api/control/stop",
                headers={"Host": "rebind.attacker.example"},
            ),
            "control_stop_evolution",
        )


def test_remote_automation_requires_exact_configured_token(app, monkeypatch):
    import evolution_core

    monkeypatch.setenv("POK_CONTROL_TOKEN", "operator-secret")
    monkeypatch.setattr(evolution_core, "stop_daemon", lambda: None)

    with _client(
        app,
        address="203.0.113.10",
        origin="https://attacker.example",
        token="wrong-secret",
    ) as invalid:
        _forbidden(
            invalid.post("/api/control/stop"),
            "control_stop_evolution",
        )

    with _client(
        app,
        address="203.0.113.10",
        origin="https://attacker.example",
        token="operator-secret",
    ) as authorized:
        response = authorized.post("/api/control/stop")

    assert response.status_code == 200
    assert response.json() == {"status": "stopped"}


def test_standard_loopback_same_origin_test_client_still_operates(
    client, monkeypatch
):
    import evolution_core

    monkeypatch.delenv("POK_CONTROL_TOKEN", raising=False)
    monkeypatch.setattr(evolution_core, "stop_daemon", lambda: None)

    response = client.post("/api/control/stop")

    assert response.status_code == 200


def test_capability_catalog_describes_token_header_without_leaking_secret(
    client, monkeypatch
):
    monkeypatch.setenv("POK_CONTROL_TOKEN", "catalog-must-not-leak-this")

    response = client.get("/api/control/tools")

    assert response.status_code == 200
    payload = response.json()
    assert payload["operator_auth_required"] is True
    assert payload["operator_token_configured"] is True
    assert payload["operator_token_header"] == "X-Control-Token"
    assert "catalog-must-not-leak-this" not in response.text


def test_control_gets_never_persist_events_or_files(client, monkeypatch):
    import server.routes.control as control
    import system_log

    monkeypatch.setattr(
        system_log,
        "log_system_event",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("read-only control endpoint wrote an event")
        ),
    )
    before = sorted(
        str(path.relative_to(control.RESULTS_DIR))
        for path in control.RESULTS_DIR.rglob("*")
    )

    for endpoint in (
        "/api/control/status",
        "/api/control/health",
        "/api/control/config",
        "/api/control/decisions",
        "/api/control/orchestrator/session",
        "/api/control/tools",
    ):
        assert client.get(endpoint).status_code == 200

    after = sorted(
        str(path.relative_to(control.RESULTS_DIR))
        for path in control.RESULTS_DIR.rglob("*")
    )
    assert after == before


def test_health_uses_only_canonical_ignored_checkpoint_summary(
    client, monkeypatch
):
    import epoch_authority
    import server.routes.control as control

    ignored = {
        "next_v": 155,
        "source_v": STRICT_SOURCE_V,
        "stage": "workers_done",
        "reason": "checkpoint_not_bound_to_strict_epoch",
        "issues": ["checkpoint_schema_version_missing_or_mismatch"],
    }
    projection = {
        "current_v": STRICT_SOURCE_V,
        "next_v": STRICT_TARGET_V,
        "strict_generation_count": 0,
        "active_generation": None,
        "evaluation_epoch": "national_tcp_policy_v1",
        "state": "reset_required",
        "initialized": False,
        "version_authority_high_water": STRICT_SOURCE_V,
        "strict_published_versions": [],
        "strict_published_bot_identities": [],
        "active_bots": [],
        "reset_receipt_valid": False,
        "reset_receipt_issues": ["reset_missing"],
        "operator_action": "execute_policy_epoch_reset",
        "operator_command": "reset-command",
        "ignored_checkpoint": ignored,
        "max_committed_v": STRICT_SOURCE_V,
    }
    monkeypatch.setattr(
        epoch_authority,
        "strict_epoch_projection",
        lambda **_kwargs: projection,
    )
    monkeypatch.setattr(
        epoch_authority,
        "unpublished_candidate_versions",
        lambda **_kwargs: [155],
    )
    raw_path = control.RESULTS_DIR / "pipeline_state.json"
    raw_path.write_text(
        '{"next_v":155,"run_id":"retired-secret-run",'
        '"prompt":"retired-secret-payload"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        control.json,
        "load",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("health reopened raw retired checkpoint")
        ),
    )

    response = client.get("/api/control/health")

    assert response.status_code == 200
    pipeline = response.json()["pipeline"]
    assert pipeline["authority"] == "strict_epoch_projection"
    assert pipeline["exists"] is False
    assert pipeline["blocked"] is True
    assert pipeline["ignored_checkpoint"] == ignored
    assert "retired-secret" not in response.text
