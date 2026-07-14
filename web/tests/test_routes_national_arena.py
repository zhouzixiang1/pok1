import asyncio

from fastapi import FastAPI
import httpx

from national_arena import manager as arena_manager
from national_arena.manager import NationalArenaManager
from national_arena.sandbox import ArenaSandboxUnavailable
from national_arena.storage import ArenaStore
from server.routes.national_arena import router


CONTROL_HEADERS = {"Origin": "http://127.0.0.1"}


async def _app_client(tmp_path, *, client_address=("127.0.0.1", 12345)):
    app = FastAPI()
    app.include_router(router)
    manager = NationalArenaManager(ArenaStore(tmp_path / "arena"))
    await manager.startup()
    app.state.national_arena_manager = manager
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=client_address),
        base_url="http://127.0.0.1",
    )
    return manager, client


def test_arena_crud_is_local_diagnostic_and_never_official(tmp_path):
    async def scenario():
        manager, client = await _app_client(tmp_path)
        try:
            response = await client.post(
                "/api/national-arena/sessions",
                headers=CONTROL_HEADERS,
                json={
                    "mode": "external_tcp",
                    "host": "127.0.0.1",
                    "port": 0,
                    "hands": 70,
                },
            )
            assert response.status_code == 201
            session = response.json()
            assert session["result_authority"] == "diagnostic_only"
            assert session["official_exe_certification"] is False
            assert session["affects_glicko"] is False
            assert session["compliance_oracle"] == "official_windows_exe"
            assert session["evaluation_epoch"] == "national_tcp_policy_v1"
            assert session["epoch_reset_receipt_digest"] == "a" * 64
            assert len(session["epoch_authority_identity"]) == 64

            health = (await client.get("/api/national-arena/health")).json()
            assert health["can_certify"] is False
            assert health["compliance_oracle"] == "official_windows_exe"

            listing = (await client.get(
                "/api/national-arena/sessions"
            )).json()["sessions"]
            assert [row["session_id"] for row in listing] == [session["session_id"]]

            history_payload = (await client.get(
                f"/api/national-arena/sessions/{session['session_id']}/events/history"
            )).json()
            assert history_payload["high_watermark"] == 1
            history = history_payload["events"]
            assert history[0]["type"] == "session_created"

            stopped = await client.post(
                f"/api/national-arena/sessions/{session['session_id']}/stop",
                headers=CONTROL_HEADERS,
            )
            assert stopped.status_code == 200
            assert stopped.json()["status"] == "stopped"
            thp = await client.get(
                f"/api/national-arena/sessions/{session['session_id']}/thp"
            )
            assert thp.status_code == 404
        finally:
            await client.aclose()
            await manager.shutdown()

    asyncio.run(scenario())


def test_uninitialized_epoch_gets_are_empty_and_mutations_are_409(
    tmp_path, monkeypatch
):
    from epoch_authority import PolicyEpochInitializationRequired
    import epoch_authority

    state = {
        "evaluation_epoch": "national_tcp_policy_v1",
        "state": "reset_required",
        "initialized": False,
        "reset_receipt_valid": False,
        "reset_receipt_digest": None,
        "operator_action": "execute_policy_epoch_reset",
        "operator_command": "reset-command",
    }

    def blocked(operation):
        raise PolicyEpochInitializationRequired(operation, state)

    monkeypatch.setattr(epoch_authority, "require_policy_epoch_initialized", blocked)

    async def scenario():
        root = tmp_path / "arena-never-created"
        app = FastAPI()
        app.include_router(router)
        app.state.national_arena_manager = NationalArenaManager(ArenaStore(root))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=app,
                client=("127.0.0.1", 12345),
            ),
            base_url="http://127.0.0.1",
        ) as client:
            bots = (await client.get("/api/national-arena/bots")).json()
            health = (await client.get("/api/national-arena/health")).json()
            sessions = (await client.get("/api/national-arena/sessions")).json()
            detail = (await client.get(
                "/api/national-arena/sessions/arena_20260711_retired1"
            )).json()
            history = (await client.get(
                "/api/national-arena/sessions/arena_20260711_retired1/events/history"
            )).json()
            wire = (await client.get(
                "/api/national-arena/sessions/arena_20260711_retired1/wire/history"
            )).json()
            artifact = (await client.get(
                "/api/national-arena/sessions/arena_20260711_retired1/thp"
            )).json()
            stream = (await client.get(
                "/api/national-arena/sessions/arena_20260711_retired1/events"
            )).text

            assert bots["bots"] == []
            assert health["active_session"] is None
            assert health["accepting_new_session"] is False
            assert sessions["sessions"] == []
            assert detail["session"] is None
            assert history["events"] == []
            assert wire["records"] == []
            assert artifact["artifact"] is None
            assert "event: epoch_blocked" in stream
            for payload in (bots, health, sessions, detail, history, wire, artifact):
                assert payload["epoch_authority"]["state"] == "reset_required"
                assert payload["epoch_initialized"] is False
                assert payload["result_authority"] == "diagnostic_only"
                assert payload["affects_glicko"] is False
                assert payload["official_exe_certification"] is False

            denied = await client.post(
                "/api/national-arena/sessions",
                headers=CONTROL_HEADERS,
                json={"mode": "external_tcp"},
            )
            assert denied.status_code == 409
            assert denied.json()["detail"]["epoch"]["state"] == "reset_required"
        assert not root.exists()

    asyncio.run(scenario())


def test_web_lifespan_does_not_start_or_write_arena_before_reset(
    tmp_path, monkeypatch
):
    from epoch_authority import PolicyEpochInitializationRequired
    import epoch_authority
    import server.app as app_module

    state = {
        "evaluation_epoch": "national_tcp_policy_v1",
        "state": "reset_required",
        "initialized": False,
        "reset_receipt_valid": False,
        "reset_receipt_digest": None,
        "operator_action": "execute_policy_epoch_reset",
        "operator_command": "reset-command",
    }

    def blocked(operation):
        raise PolicyEpochInitializationRequired(operation, state)

    monkeypatch.setattr(epoch_authority, "require_policy_epoch_initialized", blocked)
    root = tmp_path / "lifespan-arena-must-not-exist"
    manager = NationalArenaManager(ArenaStore(root))
    monkeypatch.setattr(app_module, "arena_manager", manager)

    async def scenario():
        async with app_module.lifespan(app_module.app):
            assert app_module.app.state.national_arena_manager is manager
            assert manager.started is False
            assert not root.exists()
        assert manager.started is False
        assert not root.exists()

    asyncio.run(scenario())


def test_arena_mutation_rejects_cross_origin_without_control_token(
    tmp_path, monkeypatch
):
    async def scenario():
        manager, client = await _app_client(tmp_path)
        try:
            response = await client.post(
                "/api/national-arena/sessions",
                headers={"Origin": "https://attacker.example"},
                json={"mode": "external_tcp"},
            )
            assert response.status_code == 403
        finally:
            await client.aclose()
            await manager.shutdown()

    monkeypatch.delenv("POK_CONTROL_TOKEN", raising=False)
    asyncio.run(scenario())


def test_arena_mutation_rejects_remote_client_with_forged_same_origin(
    tmp_path, monkeypatch
):
    async def scenario():
        manager, client = await _app_client(
            tmp_path,
            client_address=("203.0.113.10", 4242),
        )
        try:
            response = await client.post(
                "/api/national-arena/sessions",
                headers={"Origin": "http://127.0.0.1", "Host": "127.0.0.1"},
                json={"mode": "external_tcp"},
            )
            assert response.status_code == 403
            assert response.json()["detail"]["code"] == "operator_control_forbidden"
        finally:
            await client.aclose()
            await manager.shutdown()

    monkeypatch.delenv("POK_CONTROL_TOKEN", raising=False)
    asyncio.run(scenario())


def test_arena_control_token_allows_explicit_remote_automation(
    tmp_path, monkeypatch
):
    async def scenario():
        manager, client = await _app_client(
            tmp_path,
            client_address=("203.0.113.10", 4242),
        )
        try:
            denied = await client.post(
                "/api/national-arena/sessions",
                json={"mode": "external_tcp"},
            )
            allowed = await client.post(
                "/api/national-arena/sessions",
                headers={"X-Control-Token": "secret-token"},
                json={"mode": "external_tcp"},
            )
            assert denied.status_code == 403
            assert allowed.status_code == 201
        finally:
            await client.aclose()
            await manager.shutdown()

    monkeypatch.setenv("POK_CONTROL_TOKEN", "secret-token")
    asyncio.run(scenario())


def test_arena_sse_first_connection_snapshots_and_reconnect_replays(tmp_path):
    async def scenario():
        manager, client = await _app_client(tmp_path)
        try:
            session = (await client.post(
                "/api/national-arena/sessions",
                headers=CONTROL_HEADERS,
                json={"mode": "external_tcp"},
            )).json()
            await client.post(
                f"/api/national-arena/sessions/{session['session_id']}/stop",
                headers=CONTROL_HEADERS,
            )
            url = f"/api/national-arena/sessions/{session['session_id']}/events"

            first = (await client.get(url)).text
            assert "event: snapshot" in first
            assert "event: stream_closed" in first

            replay = (await client.get(url, headers={"Last-Event-ID": "1"})).text
            assert "event: snapshot" not in replay
            assert "event: session_stopping" in replay
            assert "event: session_stopped" in replay
            assert "event: stream_closed" in replay
        finally:
            await client.aclose()
            await manager.shutdown()

    asyncio.run(scenario())


def test_managed_start_reports_sandbox_infrastructure_failure_as_503(
    tmp_path, monkeypatch
):
    async def scenario():
        manager, client = await _app_client(tmp_path)
        identity = {
            "label": "national_v1",
            "artifact_hash": "a" * 64,
            "tag": "national-bot-v1",
            "tag_object": "tag-a",
            "commit_oid": "commit-a",
            "current_tree_oid": "tree-a",
        }

        def catalog(*, force_refresh=False):
            del force_refresh
            return [{
                "id": "national_v1",
                "artifact_identity": identity,
                "certification": {"arena_launch_eligible": True},
            }]

        def unavailable():
            raise ArenaSandboxUnavailable(
                "arena_sandbox_bwrap_unavailable; managed execution has no fallback"
            )

        monkeypatch.setattr(manager, "list_launchable_bots", catalog)
        monkeypatch.setattr(arena_manager, "require_managed_sandbox", unavailable)
        try:
            created_response = await client.post(
                "/api/national-arena/sessions",
                headers=CONTROL_HEADERS,
                json={
                    "mode": "managed_bots",
                    "port": 10001,
                    "top_bot": "national_v1",
                    "bottom_bot": "national_v1",
                },
            )
            assert created_response.status_code == 201
            created = created_response.json()
            assert created["port"] == 0
            assert created["requested_port"] == 0

            started = await client.post(
                f"/api/national-arena/sessions/{created['session_id']}/start",
                headers=CONTROL_HEADERS,
            )
            assert started.status_code == 503
            assert "no fallback" in started.json()["detail"]
            assert manager.get_session(created["session_id"])["status"] == "created"

            override = await client.post(
                "/api/national-arena/sessions",
                headers=CONTROL_HEADERS,
                json={
                    "mode": "managed_bots",
                    "port": 10001,
                    "managed_port_override": True,
                    "top_bot": "national_v1",
                    "bottom_bot": "national_v1",
                },
            )
            assert override.status_code == 201
            assert override.json()["requested_port"] == 10001
            await manager.stop_session(created["session_id"])
            await manager.stop_session(override.json()["session_id"])
        finally:
            await client.aclose()
            await manager.shutdown()

    asyncio.run(scenario())
