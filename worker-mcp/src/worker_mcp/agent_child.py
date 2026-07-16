"""Sanitized one-task Claude Agent SDK child process."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from .permissions import DISALLOWED_TOOLS, PathPolicy, ToolAuditRecorder, ToolPolicy
from .prompt import SYSTEM_PROMPT, render_worker_prompt
from .schemas import TaskEnvelope, WorkerReportedResult, worker_output_json_schema


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
    stream = query(prompt=render_worker_prompt(request), options=options)
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
