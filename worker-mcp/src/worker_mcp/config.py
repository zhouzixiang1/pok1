"""Configuration loading with local-gateway and repository safety checks."""

from __future__ import annotations

import ipaddress
from pathlib import Path
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GatewayConfig(ConfigModel):
    endpoint: str = "http://127.0.0.1:15721"
    health_path: str = "/health"
    auth_token_env: str = "WORKER_MCP_ANTHROPIC_AUTH_TOKEN"
    require_auth_token: bool = True
    connect_timeout_sec: float = Field(default=5.0, gt=0, le=60)

    @field_validator("endpoint")
    @classmethod
    def local_endpoint_only(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("gateway endpoint must be an HTTP(S) URL")
        host = parsed.hostname
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = host == "localhost"
        if not is_loopback:
            raise ValueError("gateway endpoint must be loopback-local")
        return value.rstrip("/")

    @field_validator("health_path")
    @classmethod
    def validate_health_path(cls, value: str) -> str:
        if not value.startswith("/") or ".." in value:
            raise ValueError("health_path must be an absolute URL path")
        return value


class RuntimeConfig(ConfigModel):
    backend: str = "claude_sdk"
    claude_cli_path: str | None = None
    python_executable: str | None = None
    expected_claude_agent_sdk: str = "0.2.91"
    expected_claude_code: str = "2.1.205"
    expected_cc_switch: str = "3.17.0"
    child_shutdown_grace_sec: float = Field(default=5.0, gt=0, le=30)
    max_result_bytes: int = Field(default=4 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024)

    @field_validator("backend")
    @classmethod
    def validate_backend(cls, value: str) -> str:
        if value not in {"claude_sdk", "mock"}:
            raise ValueError("runtime backend must be claude_sdk or mock")
        return value


class LimitsConfig(ConfigModel):
    global_read_tasks: int = Field(default=4, ge=1, le=32)
    repository_read_tasks: int = Field(default=3, ge=1, le=16)
    global_write_tasks: int = Field(default=2, ge=1, le=8)
    repository_write_tasks: int = Field(default=1, ge=1, le=2)
    max_subprocesses: int = Field(default=6, ge=1, le=32)
    max_task_timeout_sec: int = Field(default=3600, ge=30, le=7200)
    max_turns: int = Field(default=40, ge=1, le=100)
    read_retry_count: int = Field(default=1, ge=0, le=1)


class LoggingConfig(ConfigModel):
    max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    backup_count: int = Field(default=5, ge=1, le=20)


class WorkerConfig(ConfigModel):
    schema_version: int = 1
    state_dir: Path = Path("~/.local/state/pok-worker-mcp")
    worktree_root: Path = Path("~/.local/state/pok-worker-mcp/worktrees")
    allowed_repositories: list[Path]
    mandatory_forbidden_paths: list[str] = Field(
        default_factory=lambda: [
            "archive",
            "docs/archive",
            ".evolution_pok",
            ".git",
            ".env",
        ]
    )
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @field_validator("state_dir", "worktree_root", mode="after")
    @classmethod
    def expand_path(cls, value: Path) -> Path:
        return value.expanduser().resolve(strict=False)

    @field_validator("allowed_repositories", mode="after")
    @classmethod
    def resolve_repositories(cls, values: list[Path]) -> list[Path]:
        if not values:
            raise ValueError("at least one allowed repository is required")
        return [value.expanduser().resolve(strict=False) for value in values]

    @field_validator("mandatory_forbidden_paths")
    @classmethod
    def validate_forbidden(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            text = value.strip().replace("\\", "/").strip("/")
            if not text or text == "." or ".." in Path(text).parts:
                raise ValueError("forbidden paths must be repository-relative")
            normalized.append(text)
        return sorted(set(normalized))

    @model_validator(mode="after")
    def validate_limits(self) -> "WorkerConfig":
        if self.limits.repository_read_tasks > self.limits.global_read_tasks:
            raise ValueError("repository read limit cannot exceed global limit")
        if self.limits.repository_write_tasks > self.limits.global_write_tasks:
            raise ValueError("repository write limit cannot exceed global limit")
        return self

    def prepare_directories(self) -> None:
        for path in (self.state_dir, self.worktree_root, self.state_dir / "logs"):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)


def load_config(path: str | Path) -> WorkerConfig:
    source = Path(path).expanduser().resolve()
    payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Worker MCP config must be a YAML object")
    config = WorkerConfig.model_validate(payload)
    config.prepare_directories()
    return config
