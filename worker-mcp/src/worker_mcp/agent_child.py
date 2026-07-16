"""Sanitized one-task Claude Agent SDK child process."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import ctypes
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from .permissions import DISALLOWED_TOOLS, PathPolicy, ToolAuditRecorder, ToolPolicy
from .prompt import SYSTEM_PROMPT, render_worker_prompt
from .schemas import TaskEnvelope, WorkerReportedResult, worker_output_json_schema


def _terminate_owned_process_group(signum: int = signal.SIGTERM) -> None:
    """Terminate this isolated session and every SDK/CLI descendant."""

    try:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
    except ValueError:
        # A non-main-thread fallback may reach here on non-Linux platforms.
        pass
    try:
        if os.getpgrp() == os.getpid():
            os.killpg(os.getpgrp(), signum)
    except (OSError, ProcessLookupError):
        pass
    os._exit(128 + int(signum))


def _parent_death_handler(signum: int, _frame: Any) -> None:
    _terminate_owned_process_group(signum)


def _watch_parent(parent_pid: int) -> None:
    while os.getppid() == parent_pid:
        time.sleep(0.25)
    _terminate_owned_process_group()


def _install_parent_death_guard() -> None:
    """Ensure a hard-dead executor cannot leave an unfenced SDK child alive."""

    parent_pid = os.getppid()
    signal.signal(signal.SIGTERM, _parent_death_handler)
    if sys.platform.startswith("linux"):
        # PR_SET_PDEATHSIG=1. Install the handler first, then re-check PPID to
        # close the fork/exec/prctl race documented for this Linux primitive.
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
            error = ctypes.get_errno()
            raise OSError(error, "failed to install parent-death signal")
        if parent_pid == 1 or os.getppid() != parent_pid:
            _terminate_owned_process_group()
        return

    # The control plane is Linux-first. This conservative fallback keeps the
    # same ownership property on platforms without prctl.
    watcher = threading.Thread(
        target=_watch_parent,
        args=(parent_pid,),
        name="worker-mcp-parent-watchdog",
        daemon=True,
    )
    watcher.start()


async def _prompt_stream(text: str) -> AsyncIterator[dict[str, Any]]:
    """Send one user turn through the SDK streaming control protocol.

    ``can_use_tool`` requires an ``AsyncIterable`` prompt in Agent SDK 0.2.91;
    passing the same text as a plain string fails before the CLI is spawned.
    """

    yield {
        "type": "user",
        "session_id": "",
        "message": {"role": "user", "content": text},
        "parent_tool_use_id": None,
    }


async def execute(payload: dict[str, Any]) -> dict[str, Any]:
    request = TaskEnvelope.model_validate(payload["task"])
    cwd = Path(payload["cwd"]).resolve()
    mandatory = [str(item) for item in payload.get("mandatory_forbidden_paths", [])]
    recorder = ToolAuditRecorder()
    path_policy = PathPolicy(
        root=cwd,
        allowed_paths=tuple(request.allowed_paths),
        forbidden_paths=tuple(sorted(set(request.forbidden_paths + mandatory))),
        read_only=request.execution.read_only,
    )
    policy = ToolPolicy(path_policy, recorder)
    tools = sorted(policy.allowed_tools)
    options = ClaudeAgentOptions(
        tools=tools,
        allowed_tools=tools,
        disallowed_tools=sorted(DISALLOWED_TOOLS),
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={},
        strict_mcp_config=True,
        permission_mode="default",
        fallback_model=None,
        max_turns=request.execution.max_turns,
        cwd=cwd,
        cli_path=payload.get("claude_cli_path"),
        env={
            "ANTHROPIC_BASE_URL": str(payload["gateway_endpoint"]),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "0",
        },
        can_use_tool=policy.can_use_tool,
        hooks=policy.hooks(),
        agents={},
        setting_sources=[],
        skills=[],
        plugins=[],
        sandbox={
            "enabled": True,
            "autoAllowBashIfSandboxed": False,
            "allowUnsandboxedCommands": False,
            "excludedCommands": [],
            "network": {"allowLocalBinding": False, "allowUnixSockets": []},
        },
        output_format={"type": "json_schema", "schema": worker_output_json_schema()},
    )

    result_message: ResultMessage | None = None
    stream = query(
        prompt=_prompt_stream(render_worker_prompt(request)),
        options=options,
    )
    try:
        async for message in stream:
            if isinstance(message, ResultMessage):
                result_message = message
    finally:
        await stream.aclose()
    if result_message is None:
        raise RuntimeError("Agent SDK stream ended without ResultMessage")
    if result_message.is_error:
        raise RuntimeError("Agent SDK returned an error result")
    reported = WorkerReportedResult.model_validate(result_message.structured_output)
    return {
        "ok": True,
        "structured_output": reported.model_dump(mode="json"),
        "audit": recorder.payload(),
        "metrics": {
            "session_id": result_message.session_id,
            "turns": result_message.num_turns,
            "duration_ms": result_message.duration_ms,
        },
    }


def main() -> int:
    _install_parent_death_guard()
    try:
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw)
        result = asyncio.run(execute(payload))
    except Exception as exc:
        print(f"worker-mcp agent child error: {type(exc).__name__}", file=sys.stderr)
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_class": type(exc).__name__,
                    "error_message": "Agent SDK execution failed",
                },
                separators=(",", ":"),
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
