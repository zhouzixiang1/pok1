"""Pinned Claude/CC Switch compatibility boundary and local health probe."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .config import WorkerConfig


class GatewayUnavailable(RuntimeError):
    retryable = True


class GatewayContractError(RuntimeError):
    retryable = True


class CredentialUnavailable(RuntimeError):
    retryable = False


class SandboxUnavailable(RuntimeError):
    retryable = False


class RuntimeCompatibilityError(RuntimeError):
    retryable = False


@dataclass(frozen=True)
class GatewayHealth:
    healthy: bool
    detail: str


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _gateway_health_sync(config: WorkerConfig) -> GatewayHealth:
    url = config.gateway.endpoint + config.gateway.health_path
    request = Request(url, method="GET", headers={"User-Agent": "pok-worker-mcp/0.1"})
    try:
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        with opener.open(
            request, timeout=config.gateway.connect_timeout_sec
        ) as response:
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


def sandbox_runtime_inventory(*, path: str | None = None) -> tuple[bool, str]:
    missing = [
        name
        for name in ("bwrap", "socat")
        if shutil.which(name, path=path) is None
    ]
    if missing:
        return False, "missing required executables: " + ", ".join(missing)
    return True, "bubblewrap and socat are available"


def require_sandbox_runtime(*, path: str | None = None) -> None:
    ok, detail = sandbox_runtime_inventory(path=path)
    if not ok:
        raise SandboxUnavailable(
            "required Claude Code Linux sandbox dependencies are unavailable: "
            + detail
        )


def runtime_inventory(
    config: WorkerConfig,
    *,
    path: str | None = None,
    home: str | None = None,
) -> dict[str, tuple[bool | None, str]]:
    """Return pinned runtime checks; ``None`` means explicitly unverified."""

    inventory: dict[str, tuple[bool | None, str]] = {}
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
            env={
                "PATH": path if path is not None else os.environ.get("PATH", ""),
                "HOME": home if home is not None else str(Path.home()),
            },
        )
        text = (result.stdout or result.stderr).strip()
        version_match = re.search(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)", text)
        installed_version = version_match.group(1) if version_match else None
        ok = (
            result.returncode == 0
            and installed_version == config.runtime.expected_claude_code
        )
        inventory["claude_code"] = (
            ok,
            (
                f"installed={installed_version}; expected={config.runtime.expected_claude_code}"
                if installed_version
                else "version unavailable or CLI failure"
            ),
        )
    except (OSError, subprocess.TimeoutExpired):
        inventory["claude_code"] = (False, "CLI unavailable")

    inventory["cc_switch_contract"] = (
        None,
        "unverified: "
        f"expected={config.runtime.expected_cc_switch}; endpoint exposes no version evidence",
    )
    inventory["linux_sandbox"] = sandbox_runtime_inventory(path=path)
    return inventory


def require_compatible_runtime(
    config: WorkerConfig,
    *,
    path: str,
    home: str,
) -> None:
    inventory = runtime_inventory(config, path=path, home=home)
    failed = [
        f"{name}: {detail}"
        for name, (ok, detail) in inventory.items()
        if name in {"claude_agent_sdk", "claude_code", "linux_sandbox"}
        and ok is not True
    ]
    if failed:
        raise RuntimeCompatibilityError(
            "Worker runtime compatibility gate failed: " + "; ".join(failed)
        )
