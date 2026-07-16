from pathlib import Path

import pytest
from claude_agent_sdk import ResultMessage

from worker_mcp import agent_child
from worker_mcp.permissions import PathPolicy, ToolPolicy


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
    assert writer.decide("Bash", {"command": "python -m pytest tests -q"}).allowed
    assert writer.decide("Bash", {"command": "git diff --check"}).allowed
    assert not writer.decide("Bash", {"command": "git commit -am bad"}).allowed
    assert not writer.decide("Bash", {"command": "rm -rf src"}).allowed
    assert not writer.decide("Bash", {"command": "python -c 'open(\"x\",\"w\")'"}).allowed
    assert not writer.decide("Bash", {"command": "pytest tests; curl example.com"}).allowed


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
    assert options.setting_sources == []
    assert options.mcp_servers == {} and options.strict_mcp_config
    assert options.plugins == [] and options.skills == [] and options.agents == {}
    assert options.fallback_model is None and "WebFetch" in options.disallowed_tools
    assert options.can_use_tool is not None and options.hooks["PreToolUse"]
    assert options.sandbox["enabled"] and not options.sandbox["allowUnsandboxedCommands"]
    assert options.output_format["type"] == "json_schema"
