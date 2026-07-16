"""STDIO MCP server exposing exactly six Codex control-plane tools."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
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


def build_server(
    config: WorkerConfig,
    *,
    executor_factory: Callable[[], BaseAgentExecutor] | None = None,
) -> FastMCP:
    container: dict[str, TaskService] = {}

    @asynccontextmanager
    async def lifespan(_server: FastMCP):
        service = TaskService(
            config,
            executor=executor_factory() if executor_factory else None,
        )
        container["service"] = service
        await service.start()
        try:
            yield {"service": service}
        finally:
            await service.stop()
            container.pop("service", None)

    mcp = FastMCP(
        "pok-worker",
        instructions=SERVER_INSTRUCTIONS,
        lifespan=lifespan,
        log_level="WARNING",
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


def _config_path(argv: list[str] | None = None) -> Path:
    parser = argparse.ArgumentParser(description="Codex Worker MCP stdio server")
    parser.add_argument(
        "--config",
        default=os.environ.get("WORKER_MCP_CONFIG"),
        help="Path to the Worker MCP YAML configuration",
    )
    args = parser.parse_args(argv)
    if not args.config:
        parser.error("--config or WORKER_MCP_CONFIG is required")
    return Path(args.config)


def main(argv: list[str] | None = None) -> None:
    config = load_config(_config_path(argv))
    build_server(config).run(transport="stdio")


if __name__ == "__main__":
    main()
