import asyncio

from fastapi import FastAPI
import httpx

from national_arena import manager as arena_manager
from national_arena.manager import NationalArenaManager
from national_arena.sandbox import ArenaSandboxUnavailable
from national_arena.storage import ArenaStore
from server.routes.national_arena import router


CONTROL_HEADERS = {"Origin": "http://testserver"}


async def _app_client(tmp_path, *, client_address=("127.0.0.1", 12345)):
    app = FastAPI()
    app.include_router(router)
    manager = NationalArenaManager(ArenaStore(tmp_path / "arena"))
    await manager.startup()
    app.state.national_arena_manager = manager
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=client_address),
        base_url="http://testserver",
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

    monkeypatch.delenv("POK_ARENA_CONTROL_TOKEN", raising=False)
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
                headers={"Origin": "http://testserver", "Host": "testserver"},
                json={"mode": "external_tcp"},
            )
            assert response.status_code == 403
            assert "control_token" in response.json()["detail"].lower()
        finally:
            await client.aclose()
            await manager.shutdown()

    monkeypatch.delenv("POK_ARENA_CONTROL_TOKEN", raising=False)
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
                headers={"X-Arena-Token": "secret-token"},
                json={"mode": "external_tcp"},
            )
            assert denied.status_code == 403
            assert allowed.status_code == 201
        finally:
            await client.aclose()
            await manager.shutdown()

    monkeypatch.setenv("POK_ARENA_CONTROL_TOKEN", "secret-token")
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
