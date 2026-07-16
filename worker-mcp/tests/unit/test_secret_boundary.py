from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import venv

import pytest

from worker_mcp import agent_executor
from worker_mcp.agent_executor import (
    AgentExecution,
    AgentExecutionError,
    AgentExecutor,
    AgentProtocolError,
)
from worker_mcp.config import WorkerConfig
from worker_mcp.result_normalizer import normalize_failure, normalize_success
from worker_mcp.schemas import TaskEnvelope, WorkerReportedResult
from worker_mcp.worktree import WorktreeSnapshot


def _config(tmp_path: Path, *, result_limit: int = 4096) -> WorkerConfig:
    repository = tmp_path / "repo"
    repository.mkdir(exist_ok=True)
    return WorkerConfig.model_validate(
        {
            "state_dir": tmp_path / "state",
            "worktree_root": tmp_path / "state" / "worktrees",
            "allowed_repositories": [repository],
            "gateway": {
                "endpoint": "http://127.0.0.1:15721",
                "auth_token_env": "WORKER_MCP_TEST_AUTH_TOKEN",
                "require_auth_token": True,
            },
            "runtime": {
                "backend": "claude_sdk",
                "python_executable": sys.executable,
                "max_result_bytes": result_limit,
            },
            "limits": {
                "max_child_stdout_bytes": result_limit,
                "max_child_stderr_bytes": 1024,
            },
        }
    )


def _request(repo: Path) -> TaskEnvelope:
    return TaskEnvelope.model_validate(
        {
            "goal": "inspect bounded source",
            "repo": str(repo),
            "base_commit": "abcdef1",
            "allowed_paths": ["src"],
            "execution": {"timeout_sec": 5},
            "idempotency_key": "secret-boundary-0001",
        }
    )


def test_child_environment_uses_dedicated_home_and_translated_credential(
    monkeypatch, tmp_path
):
    secret = "dedicated-worker-token-value"
    monkeypatch.setenv("WORKER_MCP_TEST_AUTH_TOKEN", secret)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "ambient-token-must-not-win")
    executor = AgentExecutor(_config(tmp_path))

    environment = executor._child_environment()

    expected_home = (tmp_path / "state" / "agent-home").resolve()
    assert environment["HOME"] == str(expected_home)
    assert environment["CLAUDE_CONFIG_DIR"] == str(expected_home / ".claude")
    assert environment["ANTHROPIC_AUTH_TOKEN"] == secret
    assert "WORKER_MCP_TEST_AUTH_TOKEN" not in environment
    assert "ambient-token-must-not-win" not in environment.values()
    assert expected_home.stat().st_mode & 0o777 == 0o700


def test_child_path_and_python_resolution_exclude_untrusted_repository(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("WORKER_MCP_TEST_AUTH_TOKEN", "trusted-path-test-token")
    config = _config(tmp_path)
    repository = config.allowed_repositories[0]
    fake_python = repository / "python"
    fake_python.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    fake_python.chmod(0o755)
    real_bin = Path(sys.executable).resolve().parent
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join((str(repository), ".", str(config.state_dir), str(real_bin))),
    )
    config.runtime.python_executable = "python"
    executor = AgentExecutor(config)

    environment = executor._child_environment()
    resolved = executor._trusted_executable(
        config.runtime.python_executable, path=environment["PATH"]
    )

    path_entries = environment["PATH"].split(os.pathsep)
    assert str(repository.resolve()) not in path_entries
    assert str(config.state_dir.resolve()) not in path_entries
    assert "." not in path_entries and "" not in path_entries
    assert Path(resolved) != fake_python


@pytest.mark.asyncio
async def test_agent_child_rejects_alternate_unverified_interpreter(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("WORKER_MCP_TEST_AUTH_TOKEN", "alternate-python-token")
    config = _config(tmp_path)
    config.runtime.python_executable = "/bin/true"
    executor = AgentExecutor(config)
    worktree = tmp_path / "untrusted-worktree"
    worktree.mkdir()

    async def no_gateway(_config):
        return None

    monkeypatch.setattr(agent_executor, "check_gateway", no_gateway)
    monkeypatch.setattr(
        agent_executor, "require_compatible_runtime", lambda *args, **kwargs: None
    )
    with pytest.raises(AgentProtocolError, match="must equal the MCP server"):
        await executor.run(
            _request(config.allowed_repositories[0]),
            worktree,
            asyncio.Event(),
        )


def test_agent_child_preserves_venv_launcher_identity(monkeypatch, tmp_path):
    environment_root = tmp_path / "venv"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(environment_root)
    launcher = environment_root / "bin" / "python"
    assert launcher.is_symlink()
    config = _config(tmp_path)
    config.runtime.python_executable = str(launcher)
    monkeypatch.setattr(agent_executor.sys, "executable", str(launcher))

    selected = AgentExecutor(config)._child_python()

    assert selected == str(launcher)
    prefix = subprocess.run(
        [selected, "-I", "-c", "import sys; print(sys.prefix)"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert Path(prefix) == environment_root


def test_malicious_repository_values_are_redacted_from_complete_result(
    monkeypatch, tmp_path
):
    secret = "repo-stolen-dedicated-token"
    other_secret = "hardcoded-secondary-secret"
    bearer = "bearercredential123456"
    monkeypatch.setenv("WORKER_MCP_TEST_AUTH_TOKEN", secret)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    execution = AgentExecution(
        reported=WorkerReportedResult.model_validate(
            {
                "summary": f"malicious test printed {secret}",
                "findings": [
                    {
                        "severity": "critical",
                        "message": f"api_key={other_secret}",
                        "evidence": f"Authorization: Bearer {bearer}",
                    }
                ],
                "checks_performed": [
                    {
                        "name": "secret probe",
                        "status": "failed",
                        "details": f"ANTHROPIC_AUTH_TOKEN={secret}",
                    }
                ],
                "acceptance_result": f"token: {secret}",
                "risks": [f"Bearer {bearer}"],
                "unresolved": [f"password='{other_secret}'"],
                "artifacts": [
                    {"kind": "log", "path": f"credential={secret}"}
                ],
            }
        ),
        audit={
            "files_read": [str(worktree / "src" / f"token={secret}.txt")],
            "commands": [
                {
                    "command": f"python -m pytest tests -q TOKEN={secret}",
                    "exit_code": None,
                    "allowed": True,
                }
            ],
        },
        session_id=f"session-{secret}",
        turns=1,
        duration_ms=2,
        redaction_secrets=(secret,),
    )
    snapshot = WorktreeSnapshot(
        path=worktree,
        head="a" * 40,
        changed_files=(f"src/token={secret}.py",),
        diff=(
            "diff --git a/src/leak.py b/src/leak.py\n"
            f'+LEAK = "{secret}"\n'
            f'+api_key = "{other_secret}"\n'
            f"+Authorization: Bearer {bearer}\n"
        ),
    )

    result = normalize_success(
        task_id="task-secret", execution=execution, snapshot=snapshot
    )
    serialized = result.model_dump_json()

    for value in (secret, other_secret, bearer):
        assert value not in serialized
    assert "[REDACTED]" in serialized
    assert result.tests[0].status == "unknown"


def test_failure_result_redacts_environment_secret_and_diff(monkeypatch, tmp_path):
    secret = "failure-path-secret-token"
    monkeypatch.setenv("WORKER_MCP_TEST_AUTH_TOKEN", secret)
    snapshot = WorktreeSnapshot(
        path=tmp_path,
        head="b" * 40,
        changed_files=("src/leak.py",),
        diff=f'+TOKEN = "{secret}"\n',
    )
    result = normalize_failure(
        task_id="failed-task",
        summary=f"child failed with {secret}",
        worktree_path=str(tmp_path),
        snapshot=snapshot,
        unresolved=[f"credential={secret}"],
    )
    assert secret not in result.model_dump_json()
    assert "[REDACTED]" in result.model_dump_json()


def test_python_isolated_mode_rejects_worktree_shadow_package(tmp_path):
    worktree = tmp_path / "untrusted"
    package = worktree / "worker_mcp"
    package.mkdir(parents=True)
    leak = tmp_path / "leaked.txt"
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "agent_child.py").write_text(
        "import os, pathlib\n"
        f"pathlib.Path({str(leak)!r}).write_text("
        "os.environ.get('ANTHROPIC_AUTH_TOKEN', 'missing'))\n",
        encoding="utf-8",
    )
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "ANTHROPIC_AUTH_TOKEN": "shadow-package-stolen-token",
    }

    subprocess.run(
        [sys.executable, "-I", "-m", "worker_mcp.agent_child"],
        cwd=worktree,
        env=environment,
        input=b"{}",
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert not leak.exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux prctl contract")
def test_parent_hard_death_terminates_guarded_child_and_descendant(tmp_path):
    ready = tmp_path / "guarded-pids.txt"
    source_root = Path(__file__).resolve().parents[2] / "src"
    child_code = (
        "import os, pathlib, subprocess, sys, time; "
        "from worker_mcp.agent_child import _install_parent_death_guard; "
        "_install_parent_death_guard(); "
        "desc=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"pathlib.Path({str(ready)!r}).write_text(f'{{os.getpid()}} {{desc.pid}}'); "
        "time.sleep(60)"
    )
    launcher_code = (
        "import os, pathlib, subprocess, sys, time; "
        f"ready=pathlib.Path({str(ready)!r}); "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}], "
        "start_new_session=True); "
        "deadline=time.monotonic()+5; "
        "\nwhile not ready.exists() and time.monotonic() < deadline: time.sleep(.01)\n"
        "os._exit(0)"
    )
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(source_root),
    }
    launcher = subprocess.Popen(
        [sys.executable, "-c", launcher_code],
        env=environment,
    )
    child_pid = None
    try:
        launcher.wait(timeout=10)
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        child_pid, descendant_pid = map(int, ready.read_text().split())

        def stopped(pid: int) -> bool:
            stat = Path(f"/proc/{pid}/stat")
            if not stat.exists():
                return True
            try:
                return stat.read_text().split()[2] == "Z"
            except OSError:
                return True

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not (
            stopped(child_pid) and stopped(descendant_pid)
        ):
            time.sleep(0.02)
        assert stopped(child_pid)
        assert stopped(descendant_pid)
    finally:
        if launcher.poll() is None:
            launcher.kill()
            launcher.wait()
        if child_pid is not None:
            try:
                os.killpg(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.asyncio
async def test_executor_uses_isolated_argv_and_never_promotes_child_stderr(
    monkeypatch, tmp_path
):
    secret = "stderr-must-remain-private-token"
    monkeypatch.setenv("WORKER_MCP_TEST_AUTH_TOKEN", secret)
    config = _config(tmp_path)
    executor = AgentExecutor(config)
    request = _request(config.allowed_repositories[0])
    worktree = tmp_path / "untrusted-worktree"
    worktree.mkdir()
    real_create = asyncio.create_subprocess_exec
    captured = {}

    async def no_gateway(_config):
        return None

    async def fake_create(*argv, **kwargs):
        captured["argv"] = argv
        code = (
            "import sys; sys.stdin.buffer.read(); "
            f"sys.stderr.write({secret!r}); raise SystemExit(2)"
        )
        return await real_create(sys.executable, "-c", code, **kwargs)

    monkeypatch.setattr(agent_executor, "check_gateway", no_gateway)
    monkeypatch.setattr(
        agent_executor, "require_compatible_runtime", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    with pytest.raises(AgentExecutionError) as caught:
        await executor.run(request, worktree, asyncio.Event())

    assert captured["argv"][1:4] == ("-I", "-m", "worker_mcp.agent_child")
    assert str(caught.value) == "Agent SDK child failed"
    assert secret not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stream_name", "error_label"),
    (("stdout", "result"), ("stderr", "stderr")),
)
async def test_executor_terminates_child_immediately_on_stream_limit(
    monkeypatch, tmp_path, stream_name, error_label
):
    monkeypatch.setenv("WORKER_MCP_TEST_AUTH_TOKEN", "stream-limit-test-token")
    config = _config(tmp_path, result_limit=1024)
    executor = AgentExecutor(config)
    request = _request(config.allowed_repositories[0])
    worktree = tmp_path / "untrusted-worktree"
    worktree.mkdir()
    real_create = asyncio.create_subprocess_exec

    async def no_gateway(_config):
        return None

    async def fake_create(*_argv, **kwargs):
        code = (
            "import sys, time; sys.stdin.buffer.read(); "
            f"sys.{stream_name}.buffer.write(b'x' * 4096); "
            f"sys.{stream_name}.flush(); "
            "time.sleep(30)"
        )
        return await real_create(sys.executable, "-c", code, **kwargs)

    monkeypatch.setattr(agent_executor, "check_gateway", no_gateway)
    monkeypatch.setattr(
        agent_executor, "require_compatible_runtime", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    with pytest.raises(
        AgentProtocolError, match=f"{error_label} exceeded byte limit"
    ):
        await asyncio.wait_for(
            executor.run(request, worktree, asyncio.Event()), timeout=3
        )


@pytest.mark.asyncio
async def test_successful_child_leader_cannot_leave_sdk_descendant_alive(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("WORKER_MCP_TEST_AUTH_TOKEN", "orphan-fence-test-token")
    config = _config(tmp_path)
    executor = AgentExecutor(config)
    request = _request(config.allowed_repositories[0])
    worktree = tmp_path / "untrusted-worktree"
    worktree.mkdir()
    descendant_record = tmp_path / "descendant.pid"
    real_create = asyncio.create_subprocess_exec
    frame = json.dumps(
        {
            "ok": True,
            "structured_output": {
                "summary": "valid child result",
                "findings": [],
                "checks_performed": [],
                "acceptance_result": "ok",
                "risks": [],
                "unresolved": [],
                "artifacts": [],
            },
            "audit": {"files_read": [], "commands": [], "denied": []},
            "metrics": {"session_id": "orphan-test", "turns": 1, "duration_ms": 1},
        },
        separators=(",", ":"),
    )

    async def no_gateway(_config):
        return None

    async def fake_create(*_argv, **kwargs):
        code = (
            "import pathlib, subprocess, sys; "
            "sys.stdin.buffer.read(); "
            "child=subprocess.Popen(['sleep','60'], stdout=subprocess.DEVNULL, "
            "stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL); "
            f"pathlib.Path({str(descendant_record)!r}).write_text(str(child.pid)); "
            f"print({frame!r}, flush=True)"
        )
        return await real_create(sys.executable, "-c", code, **kwargs)

    monkeypatch.setattr(agent_executor, "check_gateway", no_gateway)
    monkeypatch.setattr(
        agent_executor, "require_compatible_runtime", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    descendant_pid = None
    try:
        result = await asyncio.wait_for(
            executor.run(request, worktree, asyncio.Event()), timeout=10
        )
        assert result.reported.summary == "valid child result"
        descendant_pid = int(descendant_record.read_text(encoding="utf-8"))
        status_path = Path(f"/proc/{descendant_pid}/stat")
        assert not status_path.exists() or status_path.read_text().split()[2] == "Z"
    finally:
        if descendant_pid is not None:
            try:
                os.kill(descendant_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
