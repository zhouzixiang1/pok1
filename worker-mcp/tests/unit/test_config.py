from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from worker_mcp.config import GatewayConfig, LimitsConfig, MCPServerConfig, WorkerConfig


def _payload(tmp_path: Path) -> dict[str, object]:
    return {
        "state_dir": tmp_path / "state",
        "worktree_root": tmp_path / "state" / "worktrees",
        "allowed_repositories": [tmp_path / "repository"],
    }


def test_default_state_paths_are_validated_expanded_and_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    config = WorkerConfig.model_validate(
        {"allowed_repositories": [tmp_path / "repository"]}
    )

    assert config.state_dir == (home / ".local/state/pok-worker-mcp").resolve()
    assert config.worktree_root == (
        home / ".local/state/pok-worker-mcp/worktrees"
    ).resolve()
    assert config.worktree_root.is_relative_to(config.state_dir)


@pytest.mark.parametrize("target", ["state_dir", "worktree_root", "repository"])
def test_evolution_runtime_checkout_component_is_rejected(
    tmp_path: Path, target: str
) -> None:
    payload = _payload(tmp_path)
    if target == "state_dir":
        payload["state_dir"] = tmp_path / ".evolution_pok" / "state"
        payload["worktree_root"] = tmp_path / ".evolution_pok" / "state/worktrees"
    elif target == "worktree_root":
        payload["worktree_root"] = tmp_path / "state/.evolution_pok/worktrees"
    else:
        payload["allowed_repositories"] = [tmp_path / ".evolution_pok" / "repository"]

    with pytest.raises(ValidationError, match=r"\.evolution_pok"):
        WorkerConfig.model_validate(payload)


@pytest.mark.parametrize("overlap", ["state_in_repo", "repo_in_state", "repo_in_worktrees"])
def test_state_and_worktrees_must_be_disjoint_from_repositories(
    tmp_path: Path, overlap: str
) -> None:
    payload = _payload(tmp_path)
    if overlap == "state_in_repo":
        payload["allowed_repositories"] = [tmp_path]
    elif overlap == "repo_in_state":
        payload["allowed_repositories"] = [tmp_path / "state/repository"]
    else:
        payload["allowed_repositories"] = [tmp_path / "state/worktrees/repository"]

    with pytest.raises(ValidationError, match="outside allowed repositories"):
        WorkerConfig.model_validate(payload)


@pytest.mark.parametrize(
    "worktree_root",
    [lambda root: root / "elsewhere", lambda root: root / "state"],
)
def test_worktree_root_must_be_strictly_inside_state_dir(
    tmp_path: Path, worktree_root
) -> None:
    payload = _payload(tmp_path)
    payload["worktree_root"] = worktree_root(tmp_path)

    with pytest.raises(ValidationError, match="strict descendant"):
        WorkerConfig.model_validate(payload)


def test_resource_limits_have_bounded_defaults() -> None:
    limits = LimitsConfig()

    assert limits.max_changed_files == 256
    assert limits.max_changed_file_bytes == 16 * 1024 * 1024
    assert limits.max_diff_bytes == 2 * 1024 * 1024
    assert limits.max_child_stdout_bytes == 4 * 1024 * 1024
    assert limits.max_child_stderr_bytes == 256 * 1024

    with pytest.raises(ValidationError):
        LimitsConfig(max_diff_bytes=0)


@pytest.mark.parametrize(
    "name",
    [
        "PATH",
        "PYTHONPATH",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "GITHUB_TOKEN",
        "bad-name",
        "x",
    ],
)
def test_credential_environment_name_is_not_process_control(name: str) -> None:
    with pytest.raises(ValidationError):
        GatewayConfig(auth_token_env=name)


def test_http_server_contract_is_loopback_and_credential_bound() -> None:
    config = MCPServerConfig(
        transport="streamable-http",
        host="127.0.0.1",
        port=8765,
        path="/mcp/",
        access_token_env="WORKER_MCP_ACCESS_TOKEN",
    )
    assert config.path == "/mcp"

    for patch in (
        {"host": "0.0.0.0"},
        {"host": "localhost"},
        {"port": 80},
        {"path": "/"},
        {"path": "/../mcp"},
        {"access_token_env": "ANTHROPIC_AUTH_TOKEN"},
    ):
        with pytest.raises(ValidationError):
            MCPServerConfig.model_validate(patch)


def test_http_and_gateway_credentials_require_distinct_environment_names(
    tmp_path: Path,
) -> None:
    payload = _payload(tmp_path)
    payload["gateway"] = {"auth_token_env": "WORKER_MCP_SHARED_TOKEN"}
    payload["server"] = {"access_token_env": "WORKER_MCP_SHARED_TOKEN"}

    with pytest.raises(ValidationError, match="different environment names"):
        WorkerConfig.model_validate(payload)


def test_system_forbidden_paths_cannot_be_removed_by_local_config(
    tmp_path: Path,
) -> None:
    payload = _payload(tmp_path)
    payload["mandatory_forbidden_paths"] = ["custom-private"]
    config = WorkerConfig.model_validate(payload)
    for required in (
        "archive",
        ".evolution_pok",
        ".codex_worktrees",
        ".claude",
        ".git",
        ".env",
    ):
        assert required in config.mandatory_forbidden_paths
    assert "custom-private" in config.mandatory_forbidden_paths


def test_prepare_directories_tightens_existing_permissions(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    for path in (
        payload["state_dir"],
        payload["worktree_root"],
        Path(payload["state_dir"]) / "logs",
    ):
        Path(path).mkdir(parents=True, exist_ok=True)
        Path(path).chmod(0o755)
    config = WorkerConfig.model_validate(payload)
    config.prepare_directories()
    for path in (config.state_dir, config.worktree_root, config.state_dir / "logs"):
        assert path.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    "patch",
    [
        {"schema_version": 2},
        {"runtime": {"expected_claude_agent_sdk": "0.2.90"}},
        {"runtime": {"expected_claude_code": "2.1.204"}},
        {"runtime": {"expected_cc_switch": "3.16.0"}},
        {"runtime": {"backend": "mock"}},
    ],
)
def test_schema_and_runtime_pins_are_code_owned(
    tmp_path: Path, patch: dict[str, object]
) -> None:
    payload = _payload(tmp_path)
    payload.update(patch)
    with pytest.raises(ValidationError):
        WorkerConfig.model_validate(payload)
