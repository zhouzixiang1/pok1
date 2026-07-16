from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "smoke_test.py"
SPEC = importlib.util.spec_from_file_location("worker_mcp_smoke_test", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
smoke_test = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke_test)


def test_tool_errors_and_nonhealthy_overall_status_fail_closed() -> None:
    with pytest.raises(RuntimeError, match="MCP tool error"):
        smoke_test._tool_content(
            SimpleNamespace(isError=True, structuredContent={"status": "healthy"}),
            "healthcheck",
        )

    for status in ("degraded", "unhealthy", "skipped", None):
        with pytest.raises(RuntimeError, match="overall healthy"):
            smoke_test._require_healthy({"status": status})

    smoke_test._require_healthy({"status": "healthy"})


def test_server_environment_forwards_only_configured_credential(
    worker_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WORKER_MCP_TEST_TOKEN", "dedicated-secret")
    monkeypatch.setenv("UNRELATED_API_TOKEN", "must-not-cross")
    monkeypatch.setenv("PYTHONPATH", "/untrusted/shadow")
    environment = smoke_test._server_environment(worker_config)
    assert environment["WORKER_MCP_TEST_TOKEN"] == "dedicated-secret"
    assert "UNRELATED_API_TOKEN" not in environment
    assert "PYTHONPATH" not in environment


class _NeverTerminalSession:
    async def call_tool(self, name, arguments):
        return SimpleNamespace(
            isError=False,
            structuredContent={"status": "running"},
        )


@pytest.mark.asyncio
async def test_task_polling_has_one_total_timeout() -> None:
    with pytest.raises(RuntimeError, match="total limit"):
        await smoke_test._poll_task(
            _NeverTerminalSession(), "task-0001", timeout_sec=0.01
        )


class _TerminalSession:
    async def call_tool(self, name, arguments):
        return SimpleNamespace(
            isError=False,
            structuredContent={"status": "succeeded"},
        )


@pytest.mark.asyncio
async def test_task_polling_returns_terminal_state() -> None:
    assert (
        await smoke_test._poll_task(_TerminalSession(), "task-0001", timeout_sec=1)
        == "succeeded"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_sec", [0, float("inf"), 7201])
async def test_task_polling_rejects_unbounded_limits(timeout_sec: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        await smoke_test._poll_task(_TerminalSession(), "task-0001", timeout_sec)
