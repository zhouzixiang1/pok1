"""Isolated child-process wrapper around Claude Agent SDK."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import sys
from typing import Any, Awaitable, Callable

from .compatibility import check_gateway, require_worker_credential
from .config import WorkerConfig
from .schemas import TaskEnvelope, WorkerReportedResult


class AgentExecutionError(RuntimeError):
    retryable = True


class AgentCancelled(AgentExecutionError):
    retryable = False


class AgentTimedOut(AgentExecutionError):
    retryable = False


class AgentProtocolError(AgentExecutionError):
    pass


@dataclass(frozen=True)
class AgentExecution:
    reported: WorkerReportedResult
    audit: dict[str, Any]
    session_id: str
    turns: int
    duration_ms: int


class BaseAgentExecutor:
    async def run(
        self,
        request: TaskEnvelope,
        worktree: Path,
        cancel_event: asyncio.Event,
    ) -> AgentExecution:
        raise NotImplementedError


class AgentExecutor(BaseAgentExecutor):
    def __init__(self, config: WorkerConfig):
        self.config = config

    def _child_environment(self) -> dict[str, str]:
        token = require_worker_credential(self.config)
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(Path.home()),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": "C.UTF-8",
            "PYTHONNOUSERSITE": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "0",
            "ANTHROPIC_BASE_URL": self.config.gateway.endpoint,
        }
        if token:
            environment["ANTHROPIC_AUTH_TOKEN"] = token
        if os.environ.get("SSL_CERT_FILE"):
            environment["SSL_CERT_FILE"] = os.environ["SSL_CERT_FILE"]
        if os.environ.get("SSL_CERT_DIR"):
            environment["SSL_CERT_DIR"] = os.environ["SSL_CERT_DIR"]
        return environment

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(
                process.wait(), timeout=self.config.runtime.child_shutdown_grace_sec
            )
        except TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()

    async def run(
        self,
        request: TaskEnvelope,
        worktree: Path,
        cancel_event: asyncio.Event,
    ) -> AgentExecution:
        await check_gateway(self.config)
        environment = self._child_environment()
        python = self.config.runtime.python_executable or sys.executable
        child_request = {
            "task": request.model_dump(mode="json"),
            "cwd": str(worktree),
            "gateway_endpoint": self.config.gateway.endpoint,
            "claude_cli_path": self.config.runtime.claude_cli_path,
            "mandatory_forbidden_paths": self.config.mandatory_forbidden_paths,
        }
        process = await asyncio.create_subprocess_exec(
            python,
            "-m",
            "worker_mcp.agent_child",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
            cwd=str(worktree),
            start_new_session=True,
        )
        communicate = asyncio.create_task(
            process.communicate(json.dumps(child_request).encode("utf-8"))
        )
        cancelled = asyncio.create_task(cancel_event.wait())
        try:
            done, _ = await asyncio.wait(
                {communicate, cancelled},
                timeout=request.execution.timeout_sec,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancelled in done and cancel_event.is_set():
                await self._terminate(process)
                communicate.cancel()
                await asyncio.gather(communicate, return_exceptions=True)
                raise AgentCancelled("task cancellation interrupted Agent SDK child")
            if communicate not in done:
                await self._terminate(process)
                communicate.cancel()
                await asyncio.gather(communicate, return_exceptions=True)
                raise AgentTimedOut("Agent SDK child exceeded task timeout")
            stdout, stderr = communicate.result()
        finally:
            cancelled.cancel()
            await asyncio.gather(cancelled, return_exceptions=True)
            if process.returncode is None:
                await self._terminate(process)

        if len(stdout) > self.config.runtime.max_result_bytes:
            raise AgentProtocolError("Agent SDK child result exceeded byte limit")
        if process.returncode != 0:
            # Detailed upstream diagnostics stay in the child stderr and are not
            # promoted into the normal MCP task result.
            detail = stderr.decode("utf-8", "replace")[-1000:]
            raise AgentExecutionError(
                "Agent SDK child failed" + (f" ({detail})" if detail else "")
            )
        lines = [line for line in stdout.decode("utf-8", "replace").splitlines() if line.strip()]
        if len(lines) != 1:
            raise AgentProtocolError("Agent SDK child emitted an invalid IPC frame count")
        try:
            payload = json.loads(lines[0])
        except json.JSONDecodeError as exc:
            raise AgentProtocolError("Agent SDK child emitted invalid JSON") from exc
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise AgentExecutionError("Agent SDK execution failed")
        try:
            reported = WorkerReportedResult.model_validate(payload["structured_output"])
        except (KeyError, ValueError) as exc:
            raise AgentProtocolError("Agent SDK structured output failed validation") from exc
        audit = payload.get("audit")
        metrics = payload.get("metrics")
        if not isinstance(audit, dict) or not isinstance(metrics, dict):
            raise AgentProtocolError("Agent SDK child omitted audit or metrics")
        return AgentExecution(
            reported=reported,
            audit=audit,
            session_id=str(metrics.get("session_id", "")),
            turns=max(0, int(metrics.get("turns", 0))),
            duration_ms=max(0, int(metrics.get("duration_ms", 0))),
        )


class MockAgentExecutor(BaseAgentExecutor):
    """Deterministic test backend; never contacts a model or gateway."""

    def __init__(
        self,
        callback: Callable[
            [TaskEnvelope, Path, int, asyncio.Event], Awaitable[AgentExecution]
        ]
        | None = None,
    ):
        self.callback = callback
        self.calls = 0

    async def run(
        self,
        request: TaskEnvelope,
        worktree: Path,
        cancel_event: asyncio.Event,
    ) -> AgentExecution:
        self.calls += 1
        if cancel_event.is_set():
            raise AgentCancelled("mock execution cancelled")
        if self.callback is not None:
            return await self.callback(request, worktree, self.calls, cancel_event)
        return AgentExecution(
            reported=WorkerReportedResult(
                summary="Mock Worker completed the bounded task",
                acceptance_result="mock acceptance criteria satisfied",
            ),
            audit={
                "files_read": [str(worktree / request.allowed_paths[0])],
                "commands": [],
                "denied": [],
            },
            session_id="mock-session",
            turns=1,
            duration_ms=1,
        )


def executor_for_config(config: WorkerConfig) -> BaseAgentExecutor:
    if config.runtime.backend == "mock":
        return MockAgentExecutor()
    return AgentExecutor(config)
