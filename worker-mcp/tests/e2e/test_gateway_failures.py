from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest

from conftest import run_git
from worker_mcp.agent_executor import AgentExecutor
from worker_mcp.compatibility import GatewayContractError, GatewayUnavailable, check_gateway
from worker_mcp.schemas import ExecutionProfile, TaskEnvelope, TaskStatus
from worker_mcp.task_service import TaskService


class Handler(BaseHTTPRequestHandler):
    status = 200
    body = b'{"status":"healthy"}'

    def do_GET(self):
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(type(self).body)

    def log_message(self, format, *args):
        pass


class LocalServer:
    def __init__(self, status, body):
        subclass = type("ScenarioHandler", (Handler,), {"status": status, "body": body})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), subclass)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self.server.server_address[1]

    def __exit__(self, *args):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()


@pytest.mark.asyncio
async def test_gateway_503_and_bad_schema_are_explicit(worker_config):
    with LocalServer(503, b'{"status":"down"}') as port:
        config = worker_config.model_copy(
            update={
                "gateway": worker_config.gateway.model_copy(
                    update={"endpoint": f"http://127.0.0.1:{port}"}
                )
            }
        )
        with pytest.raises(GatewayUnavailable):
            await check_gateway(config)
    with LocalServer(200, b'not-json') as port:
        config = worker_config.model_copy(
            update={
                "gateway": worker_config.gateway.model_copy(
                    update={"endpoint": f"http://127.0.0.1:{port}"}
                )
            }
        )
        with pytest.raises(GatewayContractError):
            await check_gateway(config)


@pytest.mark.asyncio
async def test_gateway_failure_retries_read_once_without_duplicate_worktree(worker_config, git_repo):
    with LocalServer(503, b'{"status":"down"}') as port:
        config = worker_config.model_copy(
            update={
                "gateway": worker_config.gateway.model_copy(
                    update={"endpoint": f"http://127.0.0.1:{port}"}
                ),
                "runtime": worker_config.runtime.model_copy(update={"backend": "claude_sdk"}),
            }
        )
        service = TaskService(config, executor=AgentExecutor(config))
        await service.start()
        try:
            request = TaskEnvelope(
                goal="read source",
                context="gateway failure test",
                repo=str(git_repo),
                base_commit=run_git(git_repo, "rev-parse", "HEAD"),
                allowed_paths=["src"],
                forbidden_paths=["archive"],
                constraints=[],
                acceptance_criteria=[],
                execution=ExecutionProfile(read_only=True, max_turns=4, timeout_sec=30),
                idempotency_key="gateway-failure-0001",
            )
            submitted = await service.submit(request)
            async with asyncio.timeout(10):
                while service.status(submitted.task_id).status not in {
                    TaskStatus.FAILED,
                    TaskStatus.SUCCEEDED,
                }:
                    await asyncio.sleep(0.05)
            status = service.status(submitted.task_id)
            assert status.status is TaskStatus.FAILED and status.attempt == 2
            task_directories = [
                path
                for path in config.worktree_root.rglob(submitted.task_id)
                if path.is_dir()
            ]
            assert len(task_directories) == 1
            assert service.result(submitted.task_id).status.value == "failed"
        finally:
            await service.stop()
