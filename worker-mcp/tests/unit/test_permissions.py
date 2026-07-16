from pathlib import Path

import pytest
from claude_agent_sdk import ResultMessage

from worker_mcp import agent_child
from worker_mcp.compatibility import SandboxUnavailable, require_sandbox_runtime
from worker_mcp.permissions import PathPolicy, ToolAuditRecorder, ToolPolicy


def policy(tmp_path: Path, read_only=True):
    root = tmp_path / "worktree"
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(exist_ok=True)
    (root / "archive").mkdir(exist_ok=True)
    return ToolPolicy(
        PathPolicy(
            root=root,
            allowed_paths=("src", "tests"),
            forbidden_paths=("archive", ".git", ".env"),
            read_only=read_only,
        )
    )


def test_path_scope_and_forbidden_paths(tmp_path):
    p = policy(tmp_path)
    assert p.decide("Read", {"file_path": "src/file.py"}).allowed
    assert not p.decide("Read", {"file_path": "../secret"}).allowed
    assert not p.decide("Read", {"file_path": "archive/old.py"}).allowed
    assert not p.decide("Grep", {"pattern": "x", "path": ""}).allowed
    assert not p.decide("WebFetch", {"url": "https://example.com"}).allowed


def test_write_and_command_allowlist(tmp_path):
    readonly = policy(tmp_path)
    writer = policy(tmp_path, read_only=False)
    assert not readonly.decide("Write", {"file_path": "src/new.py"}).allowed
    assert writer.decide("Write", {"file_path": "src/new.py"}).allowed
    # Bash cannot receive a different environment from the credential-bearing
    # SDK/CLI process, so execution is disabled even though the defensive
    # command grammar remains independently testable.
    assert not writer.decide(
        "Bash", {"command": "python -m pytest tests -q"}
    ).allowed
    assert not readonly.decide(
        "Glob", {"path": "src", "pattern": "**/.env"}
    ).allowed
    assert not readonly.decide(
        "Grep",
        {
            "path": "src",
            "pattern": "secret",
            "glob": "**/.env",
            "output_mode": "content",
        },
    ).allowed
    assert writer.decide(
        "StructuredOutput", {"summary": "bounded result"}
    ).allowed
    commands = writer.commands
    assert commands.check("python -m pytest tests -q").allowed
    assert commands.check("git diff --check").allowed
    assert not commands.check("git commit -am bad").allowed
    assert not commands.check("rm -rf src").allowed
    assert not commands.check("python -c 'open(\"x\",\"w\")'").allowed
    assert not commands.check("python -m pytest tests; curl example.com").allowed


def test_command_executable_must_be_a_bare_allowlisted_name(tmp_path):
    commands = policy(tmp_path, read_only=False).commands
    for command in (
        "/usr/bin/git diff --check",
        "./git diff --check",
        "src/python -m pytest tests -q",
        r"C:\\tools\\npm.cmd --prefix src run test",
        "python-evil -m pytest tests -q",
        "python -m pytest tests/$TOKEN -q",
    ):
        assert not commands.check(command).allowed, command
    assert commands.check("python3.14 -m compileall -q src").allowed


def test_sandbox_runtime_fails_closed_when_dependency_missing(monkeypatch):
    monkeypatch.setattr(
        "worker_mcp.compatibility.shutil.which",
        lambda name, **kwargs: "/usr/bin/bwrap" if name == "bwrap" else None,
    )
    with pytest.raises(SandboxUnavailable, match="socat"):
        require_sandbox_runtime()


def test_tool_audit_records_only_completed_reads_and_explicit_exit_codes():
    recorder = ToolAuditRecorder()
    recorder.pre("Read", {"file_path": "src/failed.py"}, "read-failed")
    assert recorder.payload()["files_read"] == []
    recorder.finish("read-failed", success=False)
    assert recorder.payload()["files_read"] == []

    recorder.pre("Read", {"file_path": "src/passed.py"}, "read-passed")
    recorder.finish("read-passed", success=True)
    assert recorder.payload()["files_read"] == ["src/passed.py"]

    recorder.pre("Bash", {"command": "git diff --check"}, "bash-unknown")
    recorder.finish("bash-unknown", success=True, tool_response="command output")
    assert recorder.payload()["commands"][-1]["exit_code"] is None

    recorder.pre("Bash", {"command": "git diff --check"}, "bash-explicit")
    recorder.finish(
        "bash-explicit", success=False, tool_response={"exitCode": 7}
    )
    assert recorder.payload()["commands"][-1]["exit_code"] == 7


@pytest.mark.asyncio
async def test_agent_sdk_options_disable_ambient_capabilities(monkeypatch, tmp_path):
    root = tmp_path / "worker"
    (root / "src").mkdir(parents=True)
    (root / "src" / "file.py").write_text("VALUE = 1\n", encoding="utf-8")
    captured = {}

    class Stream:
        def __init__(self):
            self.sent = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.sent:
                raise StopAsyncIteration
            self.sent = True
            return ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                structured_output={
                    "summary": "ok",
                    "findings": [],
                    "checks_performed": [],
                    "acceptance_result": "ok",
                    "risks": [],
                    "unresolved": [],
                    "artifacts": [],
                },
            )

        async def aclose(self):
            pass

    def fake_query(*, prompt, options):
        captured["prompt"] = prompt
        captured["options"] = options
        return Stream()

    monkeypatch.setattr(agent_child, "query", fake_query)
    await agent_child.execute(
        {
            "task": {
                "goal": "read source",
                "context": "",
                "repo": "/tmp/repo",
                "base_commit": "abcdef1",
                "allowed_paths": ["src"],
                "forbidden_paths": ["archive"],
                "constraints": [],
                "acceptance_criteria": [],
                "execution": {
                    "read_only": True,
                    "use_worktree": True,
                    "max_turns": 4,
                    "timeout_sec": 30,
                },
                "idempotency_key": "sdk-options-0001",
                "task_type": "analyze",
            },
            "cwd": str(root),
            "gateway_endpoint": "http://127.0.0.1:15721",
            "claude_cli_path": None,
            "mandatory_forbidden_paths": ["archive", ".git"],
        }
    )
    options = captured["options"]
    prompt = captured["prompt"]
    assert hasattr(prompt, "__aiter__") and not isinstance(prompt, str)
    prompt_items = [item async for item in prompt]
    assert len(prompt_items) == 1
    assert prompt_items[0]["type"] == "user"
    assert "read source" in prompt_items[0]["message"]["content"]
    for forbidden_tool in ("Bash", "Glob", "Grep"):
        assert forbidden_tool not in options.tools
        assert forbidden_tool not in options.allowed_tools
        assert forbidden_tool in options.disallowed_tools
    assert options.setting_sources == []
    assert options.mcp_servers == {} and options.strict_mcp_config
    assert options.plugins == [] and options.skills == [] and options.agents == {}
    assert options.fallback_model is None and "WebFetch" in options.disallowed_tools
    assert options.can_use_tool is not None and options.hooks["PreToolUse"]
    assert options.sandbox["enabled"] and not options.sandbox["allowUnsandboxedCommands"]
    assert options.output_format["type"] == "json_schema"


def test_parent_death_guard_installs_signal_and_closes_prctl_race(monkeypatch):
    calls = []

    class LibC:
        def prctl(self, *args):
            calls.append(args)
            return 0

    monkeypatch.setattr(agent_child.sys, "platform", "linux")
    monkeypatch.setattr(agent_child.ctypes, "CDLL", lambda *args, **kwargs: LibC())
    monkeypatch.setattr(agent_child.signal, "signal", lambda *args: calls.append(args))
    monkeypatch.setattr(agent_child.os, "getppid", lambda: 1234)
    agent_child._install_parent_death_guard()
    assert (1, agent_child.signal.SIGTERM, 0, 0, 0) in calls


def test_parent_death_guard_detects_parent_change_after_prctl(monkeypatch):
    parent_ids = iter((1234, 1))
    terminated = []

    class LibC:
        @staticmethod
        def prctl(*_args):
            return 0

    monkeypatch.setattr(agent_child.sys, "platform", "linux")
    monkeypatch.setattr(agent_child.ctypes, "CDLL", lambda *args, **kwargs: LibC())
    monkeypatch.setattr(agent_child.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(agent_child.os, "getppid", lambda: next(parent_ids))
    monkeypatch.setattr(
        agent_child,
        "_terminate_owned_process_group",
        lambda *_args: terminated.append(True),
    )
    agent_child._install_parent_death_guard()
    assert terminated == [True]
