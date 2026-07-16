"""Pinned Claude/CC Switch compatibility boundary and local health probe."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import WorkerConfig


class GatewayUnavailable(RuntimeError):
    retryable = True


class GatewayContractError(RuntimeError):
    retryable = True


class CredentialUnavailable(RuntimeError):
    retryable = False


@dataclass(frozen=True)
class GatewayHealth:
    healthy: bool
    detail: str


def _gateway_health_sync(config: WorkerConfig) -> GatewayHealth:
    url = config.gateway.endpoint + config.gateway.health_path
    request = Request(url, method="GET", headers={"User-Agent": "pok-worker-mcp/0.1"})
    try:
        with urlopen(request, timeout=config.gateway.connect_timeout_sec) as response:
            body = response.read(16_384)
            if response.status != 200:
                raise GatewayUnavailable(f"local gateway health returned HTTP {response.status}")
    except HTTPError as exc:
        raise GatewayUnavailable(f"local gateway health returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise GatewayUnavailable("local gateway health endpoint is unavailable") from exc
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GatewayContractError("local gateway health response is not JSON") from exc
    if not isinstance(payload, dict) or payload.get("status") not in {"healthy", "ok"}:
        raise GatewayContractError("local gateway health response has an invalid status")
    return GatewayHealth(True, "local gateway is healthy")


async def check_gateway(config: WorkerConfig) -> GatewayHealth:
    return await asyncio.to_thread(_gateway_health_sync, config)


def require_worker_credential(config: WorkerConfig) -> str | None:
    value = os.environ.get(config.gateway.auth_token_env)
    if config.gateway.require_auth_token and not value:
        raise CredentialUnavailable(
            f"required Worker credential environment variable is missing: "
            f"{config.gateway.auth_token_env}"
        )
    return value


def runtime_inventory(config: WorkerConfig) -> dict[str, tuple[bool, str]]:
    inventory: dict[str, tuple[bool, str]] = {}
    try:
        sdk_version = importlib.metadata.version("claude-agent-sdk")
        inventory["claude_agent_sdk"] = (
            sdk_version == config.runtime.expected_claude_agent_sdk,
            f"installed={sdk_version}; expected={config.runtime.expected_claude_agent_sdk}",
        )
    except importlib.metadata.PackageNotFoundError:
        inventory["claude_agent_sdk"] = (False, "not installed")

    cli = config.runtime.claude_cli_path or "claude"
    try:
        result = subprocess.run(
            [cli, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env={"PATH": os.environ.get("PATH", ""), "HOME": str(Path.home())},
        )
        text = (result.stdout or result.stderr).strip()
        ok = result.returncode == 0 and config.runtime.expected_claude_code in text
        inventory["claude_code"] = (
            ok,
            f"installed output matches expected={config.runtime.expected_claude_code}" if ok else "version mismatch or CLI failure",
        )
    except (OSError, subprocess.TimeoutExpired):
        inventory["claude_code"] = (False, "CLI unavailable")

    inventory["cc_switch_contract"] = (
        bool(config.runtime.expected_cc_switch),
        f"pinned expected version={config.runtime.expected_cc_switch}; endpoint exposes no version",
    )
    return inventory
