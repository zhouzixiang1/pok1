"""Isolated child-process wrapper around Claude Agent SDK."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import signal
import shutil
import sys
from typing import Any, Awaitable, Callable

from .compatibility import (
    check_gateway,
    require_compatible_runtime,
    require_worker_credential,
)
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
    redaction_secrets: tuple[str, ...] = field(
        default_factory=tuple, repr=False, compare=False
    )


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

    @staticmethod
    def _inside(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    def _trusted_path(self) -> str:
        governed = tuple(
            path.resolve(strict=False)
            for path in (
                *self.config.allowed_repositories,
                self.config.state_dir,
                self.config.worktree_root,
            )
        )
        entries: list[str] = []
        for value in os.environ.get("PATH", "/usr/bin:/bin").split(os.pathsep):
            candidate = Path(value)
            if not candidate.is_absolute():
                continue
            resolved = candidate.resolve(strict=False)
            if any(self._inside(resolved, root) for root in governed):
                continue
            text = str(resolved)
            if text not in entries:
                entries.append(text)
        return os.pathsep.join(entries) or "/usr/bin:/bin"

    @staticmethod
    def _trusted_executable(value: str, *, path: str) -> str:
        candidate = Path(value).expanduser()
        if candidate.is_absolute():
            invocation = candidate.absolute()
            resolved = candidate.resolve(strict=True)
        else:
            if candidate.name != value:
                raise AgentProtocolError(
                    "configured executable must be bare or absolute"
                )
            discovered = shutil.which(value, path=path)
            if not discovered:
                raise AgentProtocolError("configured executable is unavailable")
            invocation = Path(discovered).absolute()
            resolved = invocation.resolve(strict=True)
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise AgentProtocolError("configured executable is not executable")
        # Validate the target, but preserve the invocation path. In particular,
        # venv and CLI launchers are commonly symlinks whose lexical path
        # controls environment/resource discovery.
        return str(invocation)

    def _child_python(self) -> str:
        server = Path(os.path.abspath(sys.executable))
        configured = self.config.runtime.python_executable
        if configured:
            candidate = Path(configured).expanduser()
            if not candidate.is_absolute():
                raise AgentProtocolError(
                    "Agent child interpreter must be an absolute path"
                )
            requested = Path(os.path.abspath(candidate))
            if requested != server:
                raise AgentProtocolError(
                    "Agent child interpreter must equal the MCP server interpreter"
                )
        if not server.is_file() or not os.access(server, os.X_OK):
            raise AgentProtocolError("MCP server interpreter is not executable")
        return str(server)

    def _child_environment(self) -> dict[str, str]:
        token = require_worker_credential(self.config)
        home = (self.config.state_dir / "agent-home").resolve(strict=False)
        claude_config = home / ".claude"
        for path in (home, claude_config):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.chmod(0o700)
        environment = {
            "PATH": self._trusted_path(),
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_STATE_HOME": str(home / ".local" / "state"),
            "CLAUDE_CONFIG_DIR": str(claude_config),
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

    async def _read_limited(
        self,
        stream: asyncio.StreamReader,
        *,
        limit: int,
        label: str,
    ) -> bytes:
        data = bytearray()
        while True:
            chunk = await stream.read(min(65_536, limit + 1 - len(data)))
            if not chunk:
                return bytes(data)
            data.extend(chunk)
            if len(data) > limit:
                raise AgentProtocolError(
                    f"Agent SDK child {label} exceeded byte limit"
                )

    async def _communicate_limited(
        self,
        process: asyncio.subprocess.Process,
        payload: bytes,
    ) -> tuple[bytes, bytes]:
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_limit = min(
            self.config.runtime.max_result_bytes,
            int(
                getattr(
                    self.config.limits,
                    "max_child_stdout_bytes",
                    self.config.runtime.max_result_bytes,
                )
            ),
        )
        stderr_limit = int(
            getattr(
                self.config.limits,
                "max_child_stderr_bytes",
                min(stdout_limit, 256 * 1024),
            )
        )
        stdout_reader = asyncio.create_task(
            self._read_limited(
                process.stdout,
                limit=stdout_limit,
                label="result",
            )
        )
        stderr_reader = asyncio.create_task(
            self._read_limited(
                process.stderr,
                limit=stderr_limit,
                label="stderr",
            )
        )
        readers = (stdout_reader, stderr_reader)
        try:
            try:
                process.stdin.write(payload)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()
            stdout, stderr = await asyncio.gather(*readers)
            await process.wait()
            return stdout, stderr
        except BaseException:
            await self._terminate(process)
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            raise

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        group_id = process.pid
        try:
            os.killpg(group_id, signal.SIGTERM)
        except ProcessLookupError:
            if process.returncode is None:
                await process.wait()
            return
        if process.returncode is None:
            try:
                await asyncio.wait_for(
                    process.wait(),
                    timeout=self.config.runtime.child_shutdown_grace_sec,
                )
            except TimeoutError:
                pass

        # The group leader can exit after emitting a valid frame while a
        # detached SDK/CLI descendant keeps the same process group and secret
        # environment. Poll the group itself, not only the leader returncode.
        deadline = (
            asyncio.get_running_loop().time()
            + self.config.runtime.child_shutdown_grace_sec
        )
        while asyncio.get_running_loop().time() < deadline:
            try:
                os.killpg(group_id, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.05)
        else:
            try:
                os.killpg(group_id, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.returncode is None:
            await process.wait()

    async def run(
        self,
        request: TaskEnvelope,
        worktree: Path,
        cancel_event: asyncio.Event,
    ) -> AgentExecution:
        await check_gateway(self.config)
        environment = self._child_environment()
        require_compatible_runtime(
            self.config,
            path=environment["PATH"],
            home=environment["HOME"],
        )
        python = self._child_python()
        claude_cli = self.config.runtime.claude_cli_path
        if claude_cli:
            claude_cli = self._trusted_executable(
                claude_cli, path=environment["PATH"]
            )
        child_request = {
            "task": request.model_dump(mode="json"),
            "cwd": str(worktree),
            "gateway_endpoint": self.config.gateway.endpoint,
            "claude_cli_path": claude_cli,
            "mandatory_forbidden_paths": self.config.mandatory_forbidden_paths,
        }
        process = await asyncio.create_subprocess_exec(
            python,
            "-I",
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
            self._communicate_limited(
                process, json.dumps(child_request).encode("utf-8")
            )
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
            # Always fence the complete owned process group. A successful
            # leader exit is not proof that every SDK/CLI descendant exited.
            await self._terminate(process)

        if process.returncode != 0:
            # Child stderr is intentionally never promoted to exceptions, task
            # state, audit logs, or MCP results. Repository/model-controlled
            # diagnostics may contain the dedicated gateway credential.
            del stderr
            raise AgentExecutionError("Agent SDK child failed")
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
            redaction_secrets=tuple(
                value
                for value in (environment.get("ANTHROPIC_AUTH_TOKEN"),)
                if value
            ),
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
    return AgentExecutor(config)
