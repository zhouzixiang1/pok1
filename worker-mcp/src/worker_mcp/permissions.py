"""Defense-in-depth tool, path, command, and audit policies for SDK workers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
import re
import shlex
import time
from typing import Any

from claude_agent_sdk import HookMatcher, PermissionResultAllow, PermissionResultDeny


# The Agent SDK and Claude CLI currently share one process environment with
# built-in Bash commands.  The CLI needs the dedicated gateway credential, so
# repository-controlled pytest/npm code would inherit it too.  Until the SDK
# provides a distinct tool-process environment, Bash is fail-closed here.
READ_TOOLS = frozenset({"Read"})
WRITE_TOOLS = frozenset({"Edit", "Write"})
DISALLOWED_TOOLS = frozenset(
    {
        "Agent",
        "Bash",
        "Glob",
        "Grep",
        "Task",
        "WebSearch",
        "WebFetch",
        "NotebookEdit",
        "Skill",
        "EnterPlanMode",
        "ExitPlanMode",
    }
)
INTERNAL_TOOLS = frozenset({"StructuredOutput"})

_SHELL_CONTROL = re.compile(r"[\n\r;&|><`$]")
_BARE_EXECUTABLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_PYTHON_EXECUTABLE = re.compile(r"^python(?:3(?:\.\d+)?)?$")
_SENSITIVE_PARTS = frozenset(
    {
        ".git",
        ".env",
        ".ssh",
        ".gnupg",
        ".aws",
        ".kube",
        ".claude",
        ".evolution_pok",
        ".codex_worktrees",
        "archive",
        "id_rsa",
        "id_ed25519",
    }
)


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str


def _relative_parts(value: str) -> tuple[str, ...]:
    return tuple(PurePosixPath(value.replace("\\", "/")).parts)


@dataclass
class PathPolicy:
    root: Path
    allowed_paths: tuple[str, ...]
    forbidden_paths: tuple[str, ...]
    read_only: bool

    def __post_init__(self) -> None:
        self.root = self.root.resolve()
        self.allowed_paths = tuple(sorted(set(self.allowed_paths)))
        self.forbidden_paths = tuple(sorted(set(self.forbidden_paths)))

    def _normalize(self, value: str) -> tuple[Path | None, str | None, str | None]:
        text = str(value or "").strip()
        if not text:
            return None, None, "path is required"
        raw = Path(text)
        candidate = raw if raw.is_absolute() else self.root / raw
        resolved = candidate.resolve(strict=False)
        try:
            relative = resolved.relative_to(self.root).as_posix()
        except ValueError:
            return None, None, "path escapes the assigned worktree"
        if relative == ".":
            return resolved, relative, None
        parts = _relative_parts(relative)
        if any(part.lower() in _SENSITIVE_PARTS for part in parts):
            return None, relative, "sensitive filesystem path is forbidden"
        return resolved, relative, None

    @staticmethod
    def _under(relative: str, parent: str) -> bool:
        rel = PurePosixPath(relative)
        base = PurePosixPath(parent)
        return rel == base or base in rel.parents

    def _forbidden(self, relative: str) -> bool:
        return any(self._under(relative, item) for item in self.forbidden_paths)

    def _allowed(self, relative: str) -> bool:
        return any(self._under(relative, item) for item in self.allowed_paths)

    def check(
        self,
        value: str,
        *,
        write: bool,
        recursive: bool = False,
    ) -> PolicyDecision:
        _, relative, error = self._normalize(value)
        if error:
            return PolicyDecision(False, error)
        assert relative is not None
        if relative == ".":
            return PolicyDecision(False, "repository-root access is too broad")
        if self._forbidden(relative):
            return PolicyDecision(False, "path is inside a forbidden scope")
        if not self._allowed(relative):
            return PolicyDecision(False, "path is outside allowed_paths")
        if write and self.read_only:
            return PolicyDecision(False, "task is read-only")
        if recursive:
            rel_path = PurePosixPath(relative)
            for forbidden in self.forbidden_paths:
                forbidden_path = PurePosixPath(forbidden)
                if rel_path == forbidden_path or rel_path in forbidden_path.parents:
                    return PolicyDecision(
                        False,
                        "recursive access could cross into a forbidden descendant",
                    )
        return PolicyDecision(True, "path allowed")


class CommandPolicy:
    """Small grammar for this repository's diagnostic and verification commands."""

    def __init__(self, paths: PathPolicy):
        self.paths = paths

    def _path(self, token: str, *, write: bool = False) -> PolicyDecision:
        node_path = token.split("::", 1)[0]
        return self.paths.check(node_path, write=write)

    @staticmethod
    def _deny(message: str) -> PolicyDecision:
        return PolicyDecision(False, message)

    def check(self, command: str) -> PolicyDecision:
        text = str(command or "").strip()
        if not text:
            return self._deny("empty Bash command")
        if _SHELL_CONTROL.search(text):
            return self._deny("shell control operators and substitutions are forbidden")
        try:
            words = shlex.split(text, posix=True)
        except ValueError:
            return self._deny("invalid shell quoting")
        if not words:
            return self._deny("empty Bash command")
        executable = words[0]
        if not _BARE_EXECUTABLE.fullmatch(executable):
            return self._deny("executable must be an explicit bare command name")
        if executable in {"sudo", "su", "env", "bash", "sh", "zsh", "curl", "wget"}:
            return self._deny("command wrapper or network command is forbidden")
        if executable == "git":
            return self._git(words[1:])
        if _PYTHON_EXECUTABLE.fullmatch(executable):
            return self._python(words[1:])
        if executable in {"npm", "npm.cmd"}:
            return self._npm(words[1:])
        return self._deny(f"command is not allowlisted: {executable}")

    def _git(self, args: list[str]) -> PolicyDecision:
        if not args:
            return self._deny("git subcommand is required")
        subcommand, rest = args[0], args[1:]
        if subcommand == "status":
            allowed = {"--short", "--porcelain", "--porcelain=v1", "--branch", "-b"}
            if all(item in allowed for item in rest):
                return PolicyDecision(True, "read-only git status allowed")
            return self._deny("unsupported git status option")
        if subcommand == "diff":
            allowed_options = {
                "--check",
                "--stat",
                "--name-only",
                "--no-ext-diff",
                "--binary",
                "--cached",
            }
            path_mode = False
            for item in rest:
                if item == "--":
                    path_mode = True
                    continue
                if not path_mode:
                    if item not in allowed_options:
                        return self._deny("unsupported git diff option")
                else:
                    decision = self._path(item)
                    if not decision.allowed:
                        return decision
            return PolicyDecision(True, "read-only git diff allowed")
        if subcommand == "show":
            if 1 <= len(rest) <= 3 and all(
                item in {"--stat", "--oneline", "--no-patch"}
                or re.fullmatch(r"[0-9a-fA-F]{7,64}", item)
                for item in rest
            ):
                return PolicyDecision(True, "metadata-only git show allowed")
            return self._deny("git show is limited to commit metadata")
        return self._deny(f"git {subcommand} is forbidden")

    def _python(self, args: list[str]) -> PolicyDecision:
        if len(args) < 2 or args[0] != "-m":
            return self._deny("Python is limited to allowlisted -m commands")
        module, rest = args[1], args[2:]
        if module == "compileall":
            paths = [item for item in rest if item != "-q"]
            if not paths or any(item.startswith("-") for item in paths):
                return self._deny("compileall requires explicit allowed paths")
            for item in paths:
                decision = self._path(item)
                if not decision.allowed:
                    return decision
            return PolicyDecision(True, "compileall command allowed")
        if module != "pytest":
            return self._deny("Python module is not allowlisted")
        if not rest:
            return self._deny("pytest requires explicit test paths")
        path_count = 0
        index = 0
        while index < len(rest):
            item = rest[index]
            if item in {"-q", "-x", "-s", "--disable-warnings"} or item.startswith("--maxfail="):
                index += 1
                continue
            if item == "-k":
                if index + 1 >= len(rest):
                    return self._deny("pytest -k requires an expression")
                index += 2
                continue
            if item.startswith("-"):
                return self._deny(f"pytest option is not allowlisted: {item}")
            decision = self._path(item)
            if not decision.allowed:
                return decision
            path_count += 1
            index += 1
        if path_count == 0:
            return self._deny("pytest requires at least one explicit test path")
        return PolicyDecision(True, "pytest command allowed")

    def _npm(self, args: list[str]) -> PolicyDecision:
        if len(args) != 4 or args[0] != "--prefix" or args[2] != "run":
            return self._deny("npm requires: npm --prefix PATH run test|build|lint")
        decision = self._path(args[1])
        if not decision.allowed:
            return decision
        if args[3] not in {"test", "build", "lint"}:
            return self._deny("npm script is not allowlisted")
        return PolicyDecision(True, "npm verification command allowed")


@dataclass
class ToolAuditRecorder:
    files_read: set[str] = field(default_factory=set)
    commands: list[dict[str, Any]] = field(default_factory=list)
    denied: list[dict[str, str]] = field(default_factory=list)
    _started: dict[str, float] = field(default_factory=dict)
    _bash_by_use_id: dict[str, str] = field(default_factory=dict)
    _read_by_use_id: dict[str, str] = field(default_factory=dict)

    def pre(self, tool_name: str, tool_input: dict[str, Any], tool_use_id: str) -> None:
        if tool_name in {"Read", "Bash"}:
            self._started[tool_use_id] = time.monotonic()
        if tool_name == "Read" and tool_input.get("file_path"):
            self._read_by_use_id[tool_use_id] = str(tool_input["file_path"])
        if tool_name == "Bash":
            command = str(tool_input.get("command", ""))
            self._bash_by_use_id[tool_use_id] = command

    def deny(self, tool_name: str, reason: str) -> None:
        self.denied.append({"tool": tool_name, "reason": reason})

    @staticmethod
    def _exit_code(tool_response: Any) -> int | None:
        """Use only explicit structured exit evidence, never tool success itself."""

        if not isinstance(tool_response, dict):
            return None
        for key in ("exit_code", "exitCode"):
            value = tool_response.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        return None

    def finish(
        self,
        tool_use_id: str,
        *,
        success: bool,
        tool_response: Any = None,
    ) -> None:
        read_path = self._read_by_use_id.pop(tool_use_id, None)
        if read_path is not None and success:
            self.files_read.add(read_path)

        command = self._bash_by_use_id.pop(tool_use_id, None)
        started = self._started.pop(tool_use_id, time.monotonic())
        if command is not None:
            self.commands.append(
                {
                    "command": command,
                    "exit_code": self._exit_code(tool_response),
                    "duration_ms": max(
                        0, int((time.monotonic() - started) * 1000)
                    ),
                    "allowed": True,
                }
            )

    def payload(self) -> dict[str, Any]:
        return {
            "files_read": sorted(self.files_read),
            "commands": list(self.commands),
            "denied": list(self.denied),
        }


class ToolPolicy:
    def __init__(self, paths: PathPolicy, recorder: ToolAuditRecorder | None = None):
        self.paths = paths
        self.commands = CommandPolicy(paths)
        self.recorder = recorder or ToolAuditRecorder()
        self.allowed_tools = set(READ_TOOLS)
        if not paths.read_only:
            self.allowed_tools.update(WRITE_TOOLS)

    def decide(self, tool_name: str, tool_input: dict[str, Any]) -> PolicyDecision:
        if tool_name in INTERNAL_TOOLS:
            return PolicyDecision(True, "system structured-output tool allowed")
        if tool_name in DISALLOWED_TOOLS or tool_name not in self.allowed_tools:
            return PolicyDecision(False, f"tool is not allowed: {tool_name}")
        if tool_name == "Read":
            return self.paths.check(str(tool_input.get("file_path", "")), write=False)
        if tool_name in {"Edit", "Write"}:
            return self.paths.check(str(tool_input.get("file_path", "")), write=True)
        if tool_name == "Bash":
            return self.commands.check(str(tool_input.get("command", "")))
        return PolicyDecision(False, "tool policy has no handler")

    async def can_use_tool(self, tool_name: str, tool_input: dict[str, Any], _context: Any):
        decision = self.decide(tool_name, tool_input)
        if decision.allowed:
            return PermissionResultAllow(updated_input=tool_input)
        self.recorder.deny(tool_name, decision.reason)
        return PermissionResultDeny(message=decision.reason, interrupt=False)

    async def pre_hook(self, hook_input: dict[str, Any], _tool_use_id: str | None, _context: Any):
        tool_name = str(hook_input.get("tool_name", ""))
        tool_input = hook_input.get("tool_input") or {}
        tool_use_id = str(hook_input.get("tool_use_id", ""))
        decision = self.decide(tool_name, tool_input)
        if decision.allowed:
            self.recorder.pre(tool_name, tool_input, tool_use_id)
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": decision.reason,
                }
            }
        self.recorder.deny(tool_name, decision.reason)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": decision.reason,
            }
        }

    async def post_hook(self, hook_input: dict[str, Any], _tool_use_id: str | None, _context: Any):
        self.recorder.finish(
            str(hook_input.get("tool_use_id", "")),
            success=True,
            tool_response=hook_input.get("tool_response"),
        )
        return {}

    async def failure_hook(self, hook_input: dict[str, Any], _tool_use_id: str | None, _context: Any):
        self.recorder.finish(
            str(hook_input.get("tool_use_id", "")),
            success=False,
            tool_response=hook_input.get("tool_response"),
        )
        return {}

    def hooks(self) -> dict[str, list[HookMatcher]]:
        return {
            "PreToolUse": [HookMatcher(matcher=".*", hooks=[self.pre_hook], timeout=10)],
            "PostToolUse": [
                HookMatcher(matcher="Read", hooks=[self.post_hook], timeout=10),
                HookMatcher(matcher="Bash", hooks=[self.post_hook], timeout=10),
            ],
            "PostToolUseFailure": [
                HookMatcher(matcher="Read", hooks=[self.failure_hook], timeout=10),
                HookMatcher(matcher="Bash", hooks=[self.failure_hook], timeout=10),
            ],
        }
