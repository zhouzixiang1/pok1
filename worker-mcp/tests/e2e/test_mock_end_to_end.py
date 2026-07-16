from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest
import yaml

from conftest import run_git


@pytest.mark.asyncio
async def test_stdio_mcp_discovery_submit_poll_and_result(worker_config, git_repo, tmp_path):
    config_path = tmp_path / "worker.yaml"
    config_path.write_text(
        yaml.safe_dump(worker_config.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    source_root = Path(__file__).resolve().parents[2] / "src"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "PYTHONPATH": str(source_root),
        "LANG": "C.UTF-8",
    }
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "worker_mcp.server", "--config", str(config_path)],
        env=env,
        cwd=str(git_repo),
    )
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            listed = await session.list_tools()
            assert {tool.name for tool in listed.tools} == {
                "submit",
                "get_status",
                "get_result",
                "cancel",
                "list",
                "healthcheck",
            }
            assert all(tool.outputSchema for tool in listed.tools)
            health = await session.call_tool("healthcheck", {"deep": False})
            assert not health.isError and health.structuredContent["components"]

            submit = await session.call_tool(
                "submit",
                {
                    "goal": "inspect src/module.py",
                    "context": "stdio MCP end-to-end test",
                    "repo": str(git_repo),
                    "base_commit": run_git(git_repo, "rev-parse", "HEAD"),
                    "allowed_paths": ["src"],
                    "forbidden_paths": ["archive"],
                    "constraints": [],
                    "acceptance_criteria": ["return structured output"],
                    "idempotency_key": "stdio-e2e-task-0001",
                    "execution": {
                        "read_only": True,
                        "use_worktree": True,
                        "max_turns": 4,
                        "timeout_sec": 30,
                    },
                    "task_type": "analyze",
                },
            )
            assert not submit.isError
            task_id = submit.structuredContent["task_id"]
            async with asyncio.timeout(10):
                while True:
                    status = await session.call_tool("get_status", {"task_id": task_id})
                    state = status.structuredContent["status"]
                    if state in {"succeeded", "failed", "cancelled", "timed_out", "needs_review"}:
                        break
                    await asyncio.sleep(0.05)
            assert state == "succeeded"
            result = await session.call_tool("get_result", {"task_id": task_id})
            assert result.structuredContent["status"] == "succeeded"
            assert result.structuredContent["worktree_path"]
