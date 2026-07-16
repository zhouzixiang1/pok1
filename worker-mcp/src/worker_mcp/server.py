"""Codex MCP server exposing exactly six control-plane tools."""

from __future__ import annotations

import argparse
import anyio
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime
import os
from pathlib import Path
import secrets
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from .config import WorkerConfig, load_config
from .agent_executor import BaseAgentExecutor
from .healthcheck import HealthChecker
from .schemas import (
    CancelResponse,
    ExecutionProfile,
    HealthResponse,
    ListTasksRequest,
    ListTasksResponse,
    StatusResponse,
    SubmitResponse,
    TaskEnvelope,
    TaskResult,
    TaskStatus,
    TaskType,
)
from .task_service import TaskService


SERVER_INSTRUCTIONS = (
    "Submit bounded asynchronous tasks, then poll get_status and fetch get_result. "
    "Codex remains the planner and final reviewer. Every task requires an exact base "
    "commit and explicit allowed paths. Workers never commit, push, deploy, access the "
    "web, or modify the primary checkout. Inspect returned diffs and rerun final tests."
)
HTTP_SCOPE = "worker-mcp"


class StaticTokenVerifier(TokenVerifier):
    """Constant-time verifier for one operator-generated local access token."""

    def __init__(self, expected_token: str, resource: str) -> None:
        self._expected_token = expected_token
        self._resource = resource

    async def verify_token(self, token: str) -> AccessToken | None:
        if not secrets.compare_digest(token, self._expected_token):
            return None
        return AccessToken(
            token=token,
            client_id="codex-local",
            scopes=[HTTP_SCOPE],
            resource=self._resource,
        )


def _http_access_token(config: WorkerConfig) -> str:
    token = os.environ.get(config.server.access_token_env, "")
    if len(token) < 32 or len(token) > 4096 or token.strip() != token:
        raise RuntimeError(
            f"{config.server.access_token_env} must contain a 32-4096 character "
            "local MCP access token without surrounding whitespace"
        )
    return token


def build_server(
    config: WorkerConfig,
    *,
    executor_factory: Callable[[], BaseAgentExecutor] | None = None,
    transport: str | None = None,
    shared_service: TaskService | None = None,
) -> FastMCP:
    active_transport = transport or config.server.transport
    if active_transport not in {"stdio", "streamable-http"}:
        raise ValueError(f"unsupported MCP transport: {active_transport}")
    container: dict[str, TaskService] = {}

    @asynccontextmanager
    async def lifespan(_server: FastMCP):
        if shared_service is not None:
            yield {"service": shared_service}
            return
        service = TaskService(
            config, executor=executor_factory() if executor_factory else None
        )
        container["service"] = service
        await service.start()
        try:
            yield {"service": service}
        finally:
            await service.stop()
            container.pop("service", None)

    http_options: dict[str, Any] = {}
    if active_transport == "streamable-http":
        host = config.server.host
        port = config.server.port
        resource = f"http://{host}:{port}{config.server.path}"
        http_options = {
            "host": host,
            "port": port,
            "streamable_http_path": config.server.path,
            "json_response": True,
            "stateless_http": True,
            "token_verifier": StaticTokenVerifier(
                _http_access_token(config), resource
            ),
            "auth": AuthSettings(
                issuer_url=f"http://{host}:{port}/",
                resource_server_url=resource,
                required_scopes=[HTTP_SCOPE],
            ),
            "transport_security": TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=[f"{host}:{port}"],
                allowed_origins=[f"http://{host}:{port}"],
            ),
        }

    mcp = FastMCP(
        "pok-worker",
        instructions=SERVER_INSTRUCTIONS,
        lifespan=lifespan,
        log_level="WARNING",
        **http_options,
    )

    read_annotations = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    control_annotations = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )

    def service() -> TaskService:
        if shared_service is not None:
            return shared_service
        try:
            return container["service"]
        except KeyError as exc:
            raise RuntimeError("Worker MCP service lifecycle is not active") from exc

    @mcp.tool(
        name="submit",
        description="Durably submit one bounded asynchronous Worker task and return its task_id immediately.",
        annotations=control_annotations,
        structured_output=True,
    )
    async def submit(
        goal: str,
        context: str,
        repo: str,
        base_commit: str,
        allowed_paths: list[str],
        idempotency_key: str,
        execution: ExecutionProfile,
        forbidden_paths: list[str] | None = None,
        constraints: list[str] | None = None,
        acceptance_criteria: list[str] | None = None,
        trace_id: str | None = None,
        task_type: TaskType = TaskType.ANALYZE,
    ) -> SubmitResponse:
        request = TaskEnvelope(
            goal=goal,
            context=context,
            repo=repo,
            base_commit=base_commit,
            allowed_paths=allowed_paths,
            forbidden_paths=forbidden_paths or [],
            constraints=constraints or [],
            acceptance_criteria=acceptance_criteria or [],
            execution=execution,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            task_type=task_type,
        )
        return await service().submit(request)

    @mcp.tool(
        name="get_status",
        description="Return the durable state and coarse phase for one asynchronous task.",
        annotations=read_annotations,
        structured_output=True,
    )
    async def get_status(task_id: str) -> StatusResponse:
        return service().status(task_id)

    @mcp.tool(
        name="get_result",
        description="Return the schema-validated result and independently measured Git diff for a completed task.",
        annotations=read_annotations,
        structured_output=True,
    )
    async def get_result(task_id: str) -> TaskResult:
        return service().result(task_id)

    @mcp.tool(
        name="cancel",
        description="Idempotently cancel a queued or running task; dirty write worktrees become needs_review.",
        annotations=control_annotations,
        structured_output=True,
    )
    async def cancel(task_id: str) -> CancelResponse:
        return await service().cancel(task_id)

    @mcp.tool(
        name="list",
        description="List durable tasks by state, repository, task type, and creation time.",
        annotations=read_annotations,
        structured_output=True,
    )
    async def list_tasks(
        status: TaskStatus | None = None,
        repo: str | None = None,
        task_type: TaskType | None = None,
        since: datetime | None = None,
        limit: int = 50,
    ) -> ListTasksResponse:
        return service().list(
            ListTasksRequest(
                status=status,
                repo=repo,
                task_type=task_type,
                since=since,
                limit=limit,
            )
        )

    @mcp.tool(
        name="healthcheck",
        description="Run a shallow check of server, storage, queue, Git, pinned SDK/CLI, and the local gateway.",
        annotations=read_annotations,
        structured_output=True,
    )
    async def healthcheck() -> HealthResponse:
        return await HealthChecker(config, service()).check(deep=False)

    return mcp


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Codex Worker MCP server")
    parser.add_argument(
        "--config",
        default=os.environ.get("WORKER_MCP_CONFIG"),
        help="Path to the Worker MCP YAML configuration",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        help="Override the configured MCP transport",
    )
    args = parser.parse_args(argv)
    if not args.config:
        parser.error("--config or WORKER_MCP_CONFIG is required")
    return args


async def _run_streamable_http(config: WorkerConfig) -> None:
    """Own TaskService once per daemon, independently of HTTP client sessions."""

    service = TaskService(config)
    await service.start()
    try:
        server = build_server(
            config,
            transport="streamable-http",
            shared_service=service,
        )
        await server.run_streamable_http_async()
    finally:
        await service.stop()


def main(argv: list[str] | None = None) -> None:
    args = _arguments(argv)
    config = load_config(Path(args.config))
    transport = args.transport or config.server.transport
    if transport == "stdio":
        build_server(config, transport="stdio").run(transport="stdio")
        return
    anyio.run(_run_streamable_http, config)


if __name__ == "__main__":
    main()
