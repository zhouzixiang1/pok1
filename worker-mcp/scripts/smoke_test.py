#!/usr/bin/env python3
"""Exercise the installed STDIO server, six-tool discovery, and optional task flow."""

from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta
import json
from pathlib import Path
import subprocess
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run(args: argparse.Namespace) -> int:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "worker_mcp.server", "--config", str(args.config.resolve())],
        cwd=str(args.config.resolve().parent),
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
            health = await session.call_tool("healthcheck", {"deep": args.deep})
            print(json.dumps(health.structuredContent, indent=2, ensure_ascii=False))
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
                        "max_turns": 6,
                        "timeout_sec": 180,
                    },
                    "idempotency_key": args.idempotency_key,
                    "task_type": "analyze",
                },
            )
            task_id = submitted.structuredContent["task_id"]
            while True:
                status = await session.call_tool("get_status", {"task_id": task_id})
                state = status.structuredContent["status"]
                print(json.dumps(status.structuredContent, ensure_ascii=False))
                if state in {"succeeded", "failed", "cancelled", "timed_out", "needs_review"}:
                    break
                await asyncio.sleep(1)
            result = await session.call_tool("get_result", {"task_id": task_id})
            print(json.dumps(result.structuredContent, indent=2, ensure_ascii=False))
            return 0 if state == "succeeded" else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--deep", action="store_true")
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--base")
    parser.add_argument("--allowed-path", action="append", default=[])
    parser.add_argument("--forbidden-path", action="append", default=[])
    parser.add_argument("--idempotency-key", default="real-smoke-canary-0001")
    args = parser.parse_args()
    if args.repo and not args.allowed_path:
        parser.error("--repo requires at least one --allowed-path")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
