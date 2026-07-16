"""Component and optional end-to-end canary health checks."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import os
from pathlib import Path
import subprocess

from .agent_executor import BaseAgentExecutor
from .compatibility import check_gateway, require_worker_credential, runtime_inventory
from .config import WorkerConfig
from .schemas import (
    ComponentHealth,
    ExecutionProfile,
    HealthResponse,
    TaskEnvelope,
    TaskType,
)


class HealthChecker:
    def __init__(self, config: WorkerConfig, service: object):
        self.config = config
        self.service = service

    @staticmethod
    def _component(ok: bool, detail: str) -> ComponentHealth:
        return ComponentHealth(status="healthy" if ok else "unhealthy", detail=detail)

    async def check(self, *, deep: bool = False) -> HealthResponse:
        components: dict[str, ComponentHealth] = {
            "mcp_server": ComponentHealth(status="healthy", detail="server lifecycle active")
        }
        try:
            self.service.persistence.ping()
            components["database"] = self._component(True, "SQLite-WAL database reachable")
        except Exception:
            components["database"] = self._component(False, "SQLite database unavailable")

        components["task_executor"] = self._component(
            bool(self.service.queue.running),
            "bounded async queue workers active" if self.service.queue.running else "queue workers inactive",
        )
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["git", "--version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
                env={"PATH": os.environ.get("PATH", "")},
            )
            components["git"] = self._component(result.returncode == 0, "Git executable available")
        except Exception:
            components["git"] = self._component(False, "Git executable unavailable")

        try:
            probe = self.config.state_dir / ".health-write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            components["state_directory"] = self._component(True, "state directory writable")
            components["worktree_root"] = self._component(
                self.config.worktree_root.is_dir(), "configured worktree root available"
            )
        except OSError:
            components["state_directory"] = self._component(False, "state directory not writable")
            components["worktree_root"] = self._component(False, "worktree root unavailable")

        for name, (ok, detail) in runtime_inventory(self.config).items():
            components[name] = self._component(ok, detail)

        try:
            await check_gateway(self.config)
            components["cc_switch_endpoint"] = self._component(
                True, "local logical agent backend health endpoint is healthy"
            )
        except Exception:
            components["cc_switch_endpoint"] = self._component(
                False, "local logical agent backend is unavailable or invalid"
            )

        try:
            credential = require_worker_credential(self.config)
            components["worker_credential"] = self._component(
                True,
                "dedicated Worker credential environment is available"
                if credential
                else "credential is optional in this configuration",
            )
        except Exception:
            components["worker_credential"] = self._component(
                False, "dedicated Worker credential environment is missing"
            )

        if deep:
            await self._deep_canary(components)
        else:
            for name in ("text_canary", "tool_canary", "structured_output_canary"):
                components[name] = ComponentHealth(
                    status="skipped", detail="run healthcheck with deep=true"
                )

        statuses = {component.status for component in components.values()}
        if "unhealthy" in statuses:
            overall = "unhealthy"
        elif "degraded" in statuses:
            overall = "degraded"
        else:
            overall = "healthy"
        return HealthResponse(
            status=overall,
            components=components,
            checked_at=datetime.now(UTC),
        )

    async def _deep_canary(self, components: dict[str, ComponentHealth]) -> None:
        canary = self.config.state_dir / "canary"
        canary.mkdir(parents=True, exist_ok=True, mode=0o700)
        (canary / "README.txt").write_text(
            "Worker MCP canary: read this file and report the exact canary purpose.",
            encoding="utf-8",
        )
        request = TaskEnvelope(
            goal="Read README.txt and return a schema-valid one-sentence summary.",
            context="This is an isolated healthcheck canary; do not modify files.",
            repo=str(self.config.allowed_repositories[0]),
            base_commit="0000000",
            allowed_paths=["README.txt"],
            forbidden_paths=[],
            constraints=["Use the Read tool exactly once"],
            acceptance_criteria=["Return valid structured output"],
            execution=ExecutionProfile(read_only=True, use_worktree=True, max_turns=4, timeout_sec=120),
            idempotency_key="health-canary-0001",
            task_type=TaskType.ANALYZE,
        )
        cancel_event = asyncio.Event()
        try:
            execution = await self.service.executor.run(request, canary, cancel_event)
            components["text_canary"] = self._component(
                bool(execution.reported.summary), "text response received"
            )
            components["tool_canary"] = self._component(
                bool(execution.audit.get("files_read")), "Read tool call observed"
            )
            components["structured_output_canary"] = self._component(
                True, "structured output validated"
            )
        except Exception:
            for name in ("text_canary", "tool_canary", "structured_output_canary"):
                components[name] = self._component(False, "deep Agent SDK canary failed")
