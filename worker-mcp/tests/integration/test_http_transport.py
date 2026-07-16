from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import socket

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import pytest
import uvicorn

import worker_mcp.server as server_module
from worker_mcp.agent_executor import MockAgentExecutor
from worker_mcp.config import WorkerConfig
from worker_mcp.server import build_server
from worker_mcp.task_service import TaskService


TOKEN = "local-test-token-" + "x" * 32
TOOLS = {"submit", "get_status", "get_result", "cancel", "list", "healthcheck"}


def _http_config(worker_config: WorkerConfig, port: int) -> WorkerConfig:
    payload = worker_config.model_dump()
    payload["server"] = {
        "transport": "streamable-http",
        "host": "127.0.0.1",
        "port": port,
        "path": "/mcp",
        "access_token_env": "WORKER_MCP_ACCESS_TOKEN",
    }
    return WorkerConfig.model_validate(payload)


@asynccontextmanager
async def _running_server(config: WorkerConfig):
    service = TaskService(
        config,
        executor=MockAgentExecutor(),
        additional_redaction_secrets=(TOKEN,),
    )
    await service.start()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((config.server.host, config.server.port))
    sock.listen(128)
    server = build_server(
        config,
        transport="streamable-http",
        shared_service=service,
        http_access_token=TOKEN,
    )
    uvicorn_server = uvicorn.Server(
        uvicorn.Config(
            server.streamable_http_app(),
            log_level="error",
            lifespan="on",
        )
    )
    task = asyncio.create_task(uvicorn_server.serve(sockets=[sock]))
    try:
        for _ in range(200):
            if uvicorn_server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(0.01)
        else:
            raise RuntimeError("HTTP test server did not start")
        yield service
    finally:
        uvicorn_server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
        sock.close()
        await service.stop()


async def _discover(url: str) -> set[str]:
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {TOKEN}"}, timeout=5
    ) as http_client:
        async with streamable_http_client(url, http_client=http_client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                tools = {tool.name for tool in (await session.list_tools()).tools}
                listed = await session.call_tool("list", {})
                assert not listed.isError
                assert listed.structuredContent == {"tasks": []}
                return tools


@pytest.mark.asyncio
async def test_two_clients_share_one_service_with_auth_and_transport_gates(
    worker_config: WorkerConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    config = _http_config(worker_config, port)
    monkeypatch.setenv("WORKER_MCP_ACCESS_TOKEN", TOKEN)
    url = f"http://127.0.0.1:{port}/mcp"

    async with _running_server(config):
        first, second = await asyncio.gather(_discover(url), _discover(url))
        assert first == second == TOOLS

        contender = TaskService(config, executor=MockAgentExecutor())
        with pytest.raises(RuntimeError, match="already owns this state_dir"):
            await contender.start()

        async with httpx.AsyncClient(timeout=5) as client:
            unauthorized = await client.post(url, json={})
            assert unauthorized.status_code == 401
            bad_host = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {TOKEN}",
                    "Host": "attacker.invalid",
                },
                json={},
            )
            assert bad_host.status_code == 421
            bad_origin = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {TOKEN}",
                    "Origin": "https://attacker.invalid",
                },
                json={},
            )
            assert bad_origin.status_code == 403

    replacement = TaskService(config, executor=MockAgentExecutor())
    await replacement.start()
    await replacement.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "too-short"])
async def test_invalid_http_token_cannot_construct_or_recover_task_service(
    worker_config: WorkerConfig,
    monkeypatch: pytest.MonkeyPatch,
    value: str | None,
) -> None:
    config = _http_config(worker_config, 18765)
    calls: list[str] = []

    class SentinelTaskService:
        def __init__(self, *args, **kwargs):
            calls.append("init")

        async def start(self):
            calls.append("start")
            await self._recover()

        async def _recover(self):
            calls.append("recover")

    monkeypatch.setattr(server_module, "TaskService", SentinelTaskService)
    if value is None:
        monkeypatch.delenv(config.server.access_token_env, raising=False)
    else:
        monkeypatch.setenv(config.server.access_token_env, value)

    with pytest.raises(RuntimeError, match="32-4096 character"):
        await server_module._run_streamable_http(config)

    assert calls == []


@pytest.mark.asyncio
async def test_equal_gateway_and_http_token_values_fail_before_task_service(
    worker_config: WorkerConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _http_config(worker_config, 18766)
    shared = "different-env-names-but-identical-value-" + "x" * 32
    calls: list[str] = []

    class SentinelTaskService:
        def __init__(self, *args, **kwargs):
            calls.append("init")

        async def start(self):
            calls.append("start")

        async def _recover(self):
            calls.append("recover")

    monkeypatch.setattr(server_module, "TaskService", SentinelTaskService)
    monkeypatch.setenv(config.server.access_token_env, shared)
    monkeypatch.setenv(config.gateway.auth_token_env, shared)

    with pytest.raises(RuntimeError, match="must not reuse"):
        await server_module._run_streamable_http(config)

    assert calls == []


@pytest.mark.asyncio
async def test_bind_failure_cannot_construct_or_recover_task_service(
    worker_config: WorkerConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind(("127.0.0.1", 0))
    occupied.listen(1)
    port = occupied.getsockname()[1]
    config = _http_config(worker_config, port)
    calls: list[str] = []

    class SentinelTaskService:
        def __init__(self, *args, **kwargs):
            calls.append("init")

        async def start(self):
            calls.append("start")

        async def _recover(self):
            calls.append("recover")

    monkeypatch.setattr(server_module, "TaskService", SentinelTaskService)
    monkeypatch.setenv(config.server.access_token_env, TOKEN)
    monkeypatch.delenv(config.gateway.auth_token_env, raising=False)
    try:
        with pytest.raises(OSError):
            await server_module._run_streamable_http(config)
    finally:
        occupied.close()

    assert calls == []
