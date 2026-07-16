#!/usr/bin/env python3
"""Exercise the installed STDIO server, six-tool discovery, and optional task flow."""

from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta
import json
import math
import os
from pathlib import Path
import subprocess
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from worker_mcp.config import WorkerConfig, load_config


TERMINAL_STATES = {"succeeded", "failed", "cancelled", "timed_out", "needs_review"}


def _server_environment(config: WorkerConfig) -> dict[str, str]:
    """Forward only process basics and the one configured Worker credential."""

    allowed = ("PATH", "HOME", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR")
    environment = {
        name: os.environ[name]
        for name in allowed
        if os.environ.get(name)
    }
    credential_name = config.gateway.auth_token_env
    if os.environ.get(credential_name):
        environment[credential_name] = os.environ[credential_name]
    if config.gateway.require_auth_token and credential_name not in environment:
        raise RuntimeError(
            f"required Worker credential is not available: {credential_name}"
        )
    return environment


def _tool_content(result: object, tool_name: str) -> dict[str, object]:
    if getattr(result, "isError", False):
        raise RuntimeError(f"{tool_name} returned an MCP tool error")
    content = getattr(result, "structuredContent", None)
    if not isinstance(content, dict):
        raise RuntimeError(f"{tool_name} returned no structured content")
    return content


def _require_healthy(content: dict[str, object]) -> None:
    if content.get("status") != "healthy":
        raise RuntimeError("healthcheck must report overall healthy before smoke tasks run")


async def _poll_task(
    session: ClientSession, task_id: str, timeout_sec: float
) -> str:
    if not math.isfinite(timeout_sec) or not 0 < timeout_sec <= 7200:
        raise ValueError("task polling timeout must be finite and at most 7200 seconds")
    try:
        async with asyncio.timeout(timeout_sec):
            while True:
                status = await session.call_tool("get_status", {"task_id": task_id})
                content = _tool_content(status, "get_status")
                state = content.get("status")
                if not isinstance(state, str):
                    raise RuntimeError("get_status returned an invalid task status")
                print(json.dumps(content, ensure_ascii=False))
                if state in TERMINAL_STATES:
                    return state
                await asyncio.sleep(1)
    except TimeoutError as exc:
        raise RuntimeError(
            f"task polling exceeded the {timeout_sec:g} second total limit"
        ) from exc


async def run(args: argparse.Namespace) -> int:
    config_path = args.config.resolve()
    config = load_config(config_path)
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "worker_mcp.server", "--config", str(config_path)],
        cwd=str(config_path.parent),
        env=_server_environment(config),
    )
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(
            reader, writer, read_timeout_seconds=timedelta(seconds=180)
        ) as session:
            await session.initialize()
            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            expected = {"submit", "get_status", "get_result", "cancel", "list", "healthcheck"}
            if names != expected or any(not tool.outputSchema for tool in listed.tools):
                raise RuntimeError(f"unexpected tool contract: {sorted(names)}")
            health = await session.call_tool("healthcheck", {})
            health_content = _tool_content(health, "healthcheck")
            print(json.dumps(health_content, indent=2, ensure_ascii=False))
            _require_healthy(health_content)
            if not args.repo:
                return 0
            repo = args.repo.resolve()
            base = args.base or subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            submitted = await session.call_tool(
                "submit",
                {
                    "goal": "Read the requested paths and report their purpose with concrete file evidence.",
                    "context": "Real-environment Worker MCP smoke test; do not modify files.",
                    "repo": str(repo),
                    "base_commit": base,
                    "allowed_paths": args.allowed_path,
                    "forbidden_paths": args.forbidden_path,
                    "constraints": ["Read-only canary"],
                    "acceptance_criteria": ["Schema-valid evidence-backed result"],
                    "execution": {
                        "read_only": True,
                        "use_worktree": True,
                        "max_turns": min(12, config.limits.max_turns),
                        "timeout_sec": 180,
                    },
                    "idempotency_key": args.idempotency_key,
                    "task_type": "analyze",
                },
            )
            submitted_content = _tool_content(submitted, "submit")
            task_id = submitted_content.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                raise RuntimeError("submit returned an invalid task_id")
            state = await _poll_task(session, task_id, args.task_timeout_sec)
            result = await session.call_tool("get_result", {"task_id": task_id})
            result_content = _tool_content(result, "get_result")
            print(json.dumps(result_content, indent=2, ensure_ascii=False))
            return 0 if state == "succeeded" else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--base")
    parser.add_argument("--allowed-path", action="append", default=[])
    parser.add_argument("--forbidden-path", action="append", default=[])
    parser.add_argument("--idempotency-key", default="real-smoke-canary-0001")
    parser.add_argument("--task-timeout-sec", type=float, default=300.0)
    args = parser.parse_args()
    if args.repo and not args.allowed_path:
        parser.error("--repo requires at least one --allowed-path")
    if not math.isfinite(args.task_timeout_sec) or not 0 < args.task_timeout_sec <= 7200:
        parser.error("--task-timeout-sec must be finite, positive, and at most 7200")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
