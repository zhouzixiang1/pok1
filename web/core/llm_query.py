"""LLM query primitive and JSON output parsing.

Provides run_claude_query() for all sub-agent LLM calls, and parse_json_output()
for extracting structured data from LLM responses.
"""

import asyncio
import contextlib
import contextvars
import json
import logging
import os
import re
import shlex
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit

from claude_agent_sdk import (
    query as claude_query,
    ClaudeAgentOptions,
    AssistantMessage,
    UserMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    ThinkingBlock,
    ClaudeSDKError,
)
from bot_namespace import ACTIVE_BOT_PREFIX
from llm_failure import is_shutdown_cancel_error, is_success_error_result

log = logging.getLogger("pok.infra")
_shutdown_manager = None
_LLM_CANCEL_CONTEXT = contextvars.ContextVar("llm_cancel_context", default=None)
_LLM_BILLING_RESULTS = contextvars.ContextVar("llm_billing_results", default=None)


@contextlib.contextmanager
def llm_cancel_scope(scope, reason="parent_timeout", timeout_sec=None):
    """Attach structured context to intentional parent-driven LLM cancellation."""
    payload = {
        "cancel_scope": str(scope),
        "cancel_reason": str(reason),
    }
    if timeout_sec is not None:
        try:
            payload["timeout_sec"] = float(timeout_sec)
        except (TypeError, ValueError):
            payload["timeout_sec"] = timeout_sec
    token = _LLM_CANCEL_CONTEXT.set(payload)
    try:
        yield
    finally:
        _LLM_CANCEL_CONTEXT.reset(token)


def _current_llm_cancel_context():
    context = _LLM_CANCEL_CONTEXT.get()
    return dict(context) if isinstance(context, dict) else {}


def _cancelled_event(base_category, parent_category, default_severity="warn"):
    context = _current_llm_cancel_context()
    if context.get("cancel_reason") == "parent_timeout":
        return parent_category, "info", context
    return base_category, default_severity, context


_SUBAGENT_BASH_MUTATION_PATTERNS = (
    "sed -i", "tee ", "rm ", "rmdir", "mv ", "cp ", "mkdir",
    "touch ", "cat > ", "cat >>", "patch ",
    "git add", "git rm", "git checkout", "git restore", "git commit",
    "git push",
)
_SAFE_REDIRECT_TARGETS = {"/dev/null", "nul"}
_SUBAGENT_GIT_READONLY_COMMANDS = {
    "status", "diff", "log", "show", "rev-parse", "ls-files",
}
_SUBAGENT_GIT_GLOBAL_OPTIONS_WITH_VALUE = {
    "-C", "-c", "--git-dir", "--work-tree", "--namespace",
    "--exec-path", "--config-env",
}
_SUBAGENT_GIT_TAG_RE = re.compile(r"\bgit\s+tag\b([^;&|]*)", re.IGNORECASE)
_SUBAGENT_GIT_TAG_READONLY_OPTIONS_WITH_VALUE = {
    "--sort", "--format", "--points-at", "--contains", "--no-contains",
    "--merged", "--no-merged", "--column", "--color",
}
_SUBAGENT_GIT_TAG_READONLY_FLAGS = {
    "-l", "--list", "-n", "--ignore-case", "--no-column", "--no-color",
}
_SUBAGENT_GIT_TAG_MUTATION_FLAGS = {
    "-a", "--annotate", "-s", "--sign", "-u", "--local-user", "-f",
    "--force", "-d", "--delete",
}
_SUBAGENT_PYTHON_WRITE_PATTERNS = (
    ".write_text(", ".write_bytes(", ".unlink(", ".rename(",
    ".mkdir(", ".rmdir(", "shutil.move", "shutil.copy",
    "shutil.copytree", "shutil.rmtree", "os.remove", "os.unlink",
    "os.rename", "os.replace", "os.makedirs",
)
_SUBAGENT_PYTHON_OPEN_WRITE_RE = re.compile(r"open\([^)]*,\s*['\"][^'\"]*[wax+]")
_SUBAGENT_PYTHON_OPEN_WRITE_TARGET_RE = re.compile(
    r"(?<![\w.])open\s*\(\s*"
    r"(?P<quote>['\"])(?P<path>[^'\"]+)(?P=quote)\s*,\s*"
    r"(?:mode\s*=\s*)?"
    r"(?P<mode_quote>['\"])(?P<mode>[^'\"]*[wax+][^'\"]*)(?P=mode_quote)",
    re.IGNORECASE | re.DOTALL,
)
_SUBAGENT_PYTHON_PATH_WRITE_TARGET_RE = re.compile(
    r"(?:\bPath|\bpathlib\.Path)\s*\(\s*"
    r"(?P<quote>['\"])(?P<path>[^'\"]+)(?P=quote)\s*\)\s*\.\s*"
    r"(?P<method>write_text|write_bytes|unlink|mkdir|rmdir)\s*\(",
    re.IGNORECASE | re.DOTALL,
)
_SUBAGENT_PYTHON_PATH_ASSIGN_RE = re.compile(
    r"(?m)^\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
    r"(?:Path|pathlib\.Path)\s*\(\s*"
    r"(?P<quote>['\"])(?P<path>[^'\"]+)(?P=quote)\s*\)",
    re.IGNORECASE,
)
_SUBAGENT_PYTHON_VAR_PATH_WRITE_RE = re.compile(
    r"(?<![\w.])(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\.\s*"
    r"(?P<method>write_text|write_bytes|unlink|mkdir|rmdir)\s*\(",
    re.IGNORECASE,
)


def _subagent_git_tag_invocation_is_mutating(args):
    if not args:
        return False

    list_mode = False
    i = 0
    while i < len(args):
        arg = str(args[i])
        low = arg.lower()

        if low in _SUBAGENT_GIT_TAG_MUTATION_FLAGS:
            return True
        if low.startswith("--delete=") or low.startswith("--force="):
            return True
        if low in _SUBAGENT_GIT_TAG_READONLY_FLAGS or low.startswith("-n"):
            if low in {"-l", "--list"}:
                list_mode = True
            i += 1
            continue
        if any(low.startswith(opt + "=") for opt in _SUBAGENT_GIT_TAG_READONLY_OPTIONS_WITH_VALUE):
            i += 1
            continue
        if low in _SUBAGENT_GIT_TAG_READONLY_OPTIONS_WITH_VALUE:
            i += 2
            continue
        if list_mode:
            i += 1
            continue
        if low.startswith("-"):
            return True
        return True

    return False


def _subagent_git_tag_is_mutating(command):
    for match in _SUBAGENT_GIT_TAG_RE.finditer(str(command)):
        rest = match.group(1).strip()
        if not rest:
            continue
        try:
            args = shlex.split(rest)
        except ValueError:
            args = rest.split()
        if _subagent_git_tag_invocation_is_mutating(args):
            return True
    return False


def _strip_heredoc_bodies(command):
    """Remove heredoc bodies so Python comparisons inside them are not parsed as shell."""
    lines = str(command).splitlines()
    output = []
    pending = []
    for line in lines:
        if pending:
            if line.strip() == pending[0]:
                pending.pop(0)
            continue
        output.append(line)
        pending.extend(_shell_heredoc_delimiters(line))
    return "\n".join(output)


def _iter_shell_heredoc_bodies(command):
    """Yield heredoc bodies in shell evaluation order.

    The shell splitter intentionally strips bodies so quoted Python snippets do
    not look like shell syntax. Python workers often create a new assigned file
    via ``python3 - <<'PY'`` though, so the write-scope guard still needs to
    inspect those bodies for concrete Python file targets.
    """
    lines = str(command).splitlines()
    index = 0
    while index < len(lines):
        delimiters = _shell_heredoc_delimiters(lines[index])
        index += 1
        for delimiter in delimiters:
            body = []
            while index < len(lines) and lines[index].strip() != delimiter:
                body.append(lines[index])
                index += 1
            if index < len(lines) and lines[index].strip() == delimiter:
                index += 1
            yield "\n".join(body)


def _shell_heredoc_delimiters(line):
    delimiters = []
    quote = None
    escaped = False
    i = 0
    text = str(line)
    while i < len(text):
        ch = text[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if ch == "\\":
            escaped = True
            i += 1
            continue
        if quote:
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if text.startswith("<<<", i):
            i += 3
            continue
        if text.startswith("<<", i):
            i += 2
            if i < len(text) and text[i] == "-":
                i += 1
            while i < len(text) and text[i].isspace():
                i += 1
            token, i = _read_shell_token(text, i)
            token = token.strip("'\"")
            if token:
                delimiters.append(token)
            continue
        i += 1
    return delimiters


def _read_shell_token(text, start, extra_stop_chars=""):
    token = []
    quote = None
    escaped = False
    i = start
    while i < len(text):
        ch = text[i]
        if escaped:
            token.append(ch)
            escaped = False
            i += 1
            continue
        if ch == "\\":
            escaped = True
            i += 1
            continue
        if quote:
            if ch == quote:
                quote = None
            else:
                token.append(ch)
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch.isspace() or ch in ";&|" or ch in extra_stop_chars:
            break
        token.append(ch)
        i += 1
    return "".join(token), i


def _split_shell_simple_commands(command):
    """Split a shell command into simple command segments for guard analysis."""
    text = _strip_heredoc_bodies(command)
    quote = None
    escaped = False
    segment = []
    i = 0

    def flush_segment():
        current = "".join(segment).strip()
        segment.clear()
        return current

    def at_comment_start(index):
        if text[index] != "#":
            return False
        if index == 0:
            return True
        prev = text[index - 1]
        return prev.isspace() or prev in ";&|("

    while i < len(text):
        ch = text[i]
        if escaped:
            segment.append(ch)
            escaped = False
            i += 1
            continue
        if ch == "\\":
            segment.append(ch)
            escaped = True
            i += 1
            continue
        if quote:
            if ch == quote:
                quote = None
            segment.append(ch)
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            segment.append(ch)
            i += 1
            continue
        if ch == "#" and at_comment_start(i):
            while i < len(text) and text[i] not in "\r\n":
                i += 1
            continue
        if ch in ";&|\r\n":
            if ch == "&" and (
                (i > 0 and text[i - 1] == ">") or text.startswith("&>", i)
            ):
                segment.append(ch)
                i += 1
                continue
            current = flush_segment()
            if current:
                yield current
            i += 1
            while i < len(text) and (
                text[i] == ch or (ch in "\r\n" and text[i] in "\r\n")
            ):
                i += 1
            continue
        segment.append(ch)
        i += 1
    current = flush_segment()
    if current:
        yield current


def _shell_words(segment):
    try:
        return shlex.split(str(segment), posix=True)
    except ValueError:
        return str(segment).split()


def _is_shell_assignment(word):
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", str(word)))


def _strip_shell_redirection_words(words):
    cleaned = []
    i = 0
    while i < len(words):
        word = str(words[i])
        if re.fullmatch(r"(?:\d+)?(?:>>?|<<?|&>>?|<>|>\|)", word):
            i += 2
            continue
        if re.fullmatch(r"(?:\d+)?(?:>>?|<<?|&>>?|<>|>\|).+", word):
            i += 1
            continue
        cleaned.append(word)
        i += 1
    return cleaned


def _simple_command_words(segment):
    words = _strip_shell_redirection_words(_shell_words(segment))
    while words and _is_shell_assignment(words[0]):
        words = words[1:]
    while words and words[0] in {"command", "builtin"}:
        words = words[1:]
    if words and words[0] == "env":
        words = words[1:]
        while words and (words[0].startswith("-") or _is_shell_assignment(words[0])):
            if words[0] in {"-u", "--unset"} and len(words) > 1:
                words = words[2:]
            else:
                words = words[1:]
    return words


def _strip_shell_grouping_parens(value):
    """Remove shell grouping parens that cling to simple command tokens.

    The guard's splitter does not execute full shell grammar. In grouped
    commands like ``(cd bot && rm cache)`` shlex sees ``(cd`` and ``cache)``;
    those parens are shell syntax, not command/path text.
    """
    text = str(value or "").strip()
    while text.startswith("("):
        text = text[1:].lstrip()
    while text.endswith(")"):
        text = text[:-1].rstrip()
    return text


def _command_name(word):
    return os.path.basename(_strip_shell_grouping_parens(word)).lower()


def _strip_shell_comments(command):
    """Remove shell comments while preserving quoted ``#`` characters."""
    text = str(command or "")
    out = []
    quote = None
    escaped = False
    i = 0
    while i < len(text):
        ch = text[i]
        if escaped:
            out.append(ch)
            escaped = False
            i += 1
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            i += 1
            continue
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "#":
            prev = out[-1] if out else "\n"
            if prev == "\n" or prev.isspace() or prev in ";&|(":
                while i < len(text) and text[i] != "\n":
                    i += 1
                if i < len(text) and text[i] == "\n":
                    out.append("\n")
                    i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _non_option_args(args, options_with_value=()):
    values = []
    i = 0
    while i < len(args):
        arg = str(args[i])
        if arg == "--":
            values.extend(args[i + 1:])
            break
        if arg.startswith("-") and arg != "-":
            if arg in options_with_value and i + 1 < len(args):
                i += 2
                continue
            i += 1
            continue
        values.append(arg)
        i += 1
    return values


def _iter_shell_write_redirect_targets(command):
    text = _strip_shell_comments(_strip_heredoc_bodies(command))
    quote = None
    escaped = False
    i = 0
    while i < len(text):
        ch = text[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if ch == "\\":
            escaped = True
            i += 1
            continue
        if quote:
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue

        op_start = None
        if text.startswith("&>>", i):
            op_start = i + 3
        elif text.startswith("&>", i):
            op_start = i + 2
        elif ch == ">":
            if i + 1 < len(text) and text[i + 1] == "=":
                i += 2
                continue
            op_start = i + (2 if i + 1 < len(text) and text[i + 1] == ">" else 1)

        if op_start is None:
            i += 1
            continue

        j = op_start
        while j < len(text) and text[j].isspace():
            j += 1
        target, end = _read_shell_token(text, j, extra_stop_chars="()")
        if target:
            yield target
        i = max(end, j + 1)


def _project_root_for_guard():
    try:
        from pathlib import Path
        from evolution_infra import PROJECT_ROOT

        return Path(PROJECT_ROOT).resolve()
    except Exception:
        from pathlib import Path

        return Path.cwd().resolve()


def _resolve_guard_path(path, base_dir=None):
    from pathlib import Path

    candidate = Path(str(path or "").strip().strip("'\""))
    if not candidate.is_absolute():
        base = Path(base_dir).resolve(strict=False) if base_dir else _project_root_for_guard()
        candidate = base / candidate
    return str(candidate.resolve(strict=False))


def _cd_target_from_args(args):
    for arg in _non_option_args(args, options_with_value=()):
        text = _strip_shell_grouping_parens(arg)
        if text:
            return text
    return None


def _iter_python_write_targets_from_text(text):
    path_vars = {
        match.group("name"): match.group("path")
        for match in _SUBAGENT_PYTHON_PATH_ASSIGN_RE.finditer(str(text))
    }
    for match in _SUBAGENT_PYTHON_OPEN_WRITE_TARGET_RE.finditer(str(text)):
        mode = (match.group("mode") or "").lower()
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            yield "python_open_write", match.group("path")
    for match in _SUBAGENT_PYTHON_PATH_WRITE_TARGET_RE.finditer(str(text)):
        yield f"python_path_{match.group('method').lower()}", match.group("path")
    for match in _SUBAGENT_PYTHON_VAR_PATH_WRITE_RE.finditer(str(text)):
        target = path_vars.get(match.group("name"))
        if target:
            yield f"python_path_{match.group('method').lower()}", target


def _iter_subagent_segment_write_targets(segment, python_heredoc_body=None):
    for target in _iter_shell_write_redirect_targets(segment):
        target = target.strip("'\"")
        if target.startswith("&") or target.lower() in _SAFE_REDIRECT_TARGETS:
            continue
        yield "write_redirect", target

    words = _simple_command_words(segment)
    if not words:
        return
    cmd = _command_name(words[0])
    args = words[1:]

    if cmd.startswith("python"):
        yield from _iter_python_write_targets_from_text(segment)
        if python_heredoc_body:
            yield from _iter_python_write_targets_from_text(python_heredoc_body)
        return

    if cmd in {"mkdir", "touch", "rm", "rmdir"}:
        for target in _non_option_args(args):
            yield cmd, target
        return

    if cmd == "cp":
        non_options = _non_option_args(args, options_with_value=("-t", "--target-directory"))
        target_dir = None
        for i, arg in enumerate(args):
            if arg in {"-t", "--target-directory"} and i + 1 < len(args):
                target_dir = args[i + 1]
                break
            if arg.startswith("--target-directory="):
                target_dir = arg.split("=", 1)[1]
                break
        if target_dir:
            yield "cp_dest", target_dir
        elif len(non_options) >= 2:
            yield "cp_dest", non_options[-1]
        return

    if cmd == "mv":
        non_options = _non_option_args(args, options_with_value=("-t", "--target-directory"))
        target_dir = None
        for i, arg in enumerate(args):
            if arg in {"-t", "--target-directory"} and i + 1 < len(args):
                target_dir = args[i + 1]
                break
            if arg.startswith("--target-directory="):
                target_dir = arg.split("=", 1)[1]
                break
        if target_dir:
            yield "mv_dest", target_dir
            for source in non_options:
                yield "mv_source", source
        elif len(non_options) >= 2:
            for source in non_options[:-1]:
                yield "mv_source", source
            yield "mv_dest", non_options[-1]
        return

    if cmd == "tee":
        for target in _non_option_args(args):
            if target != "-":
                yield "tee", target
        return

    if cmd == "sed" and any(arg == "-i" or str(arg).startswith("-i") for arg in args):
        non_options = _non_option_args(args, options_with_value=("-e", "-f"))
        for target in non_options[1:]:
            yield "sed_i", target
        return

    if cmd == "patch":
        yield "patch", "__unknown_patch_target__"


def _iter_subagent_bash_write_events(command):
    """Yield ``(detector, target, cwd)`` write events from a shell command.

    Claude sub-agents run Bash from the project root. If they explicitly
    ``cd`` before a mutation, relative write targets after that point should be
    resolved against the changed shell cwd; otherwise they remain project-root
    relative and must not be silently treated as bot-local writes.
    """
    current_dir = str(_project_root_for_guard())
    heredoc_bodies = iter(_iter_shell_heredoc_bodies(command))
    for segment in _split_shell_simple_commands(command):
        words = _simple_command_words(segment)
        if not words:
            continue
        cmd = _command_name(words[0])
        args = words[1:]
        python_heredoc_body = None
        if cmd.startswith("python") and _shell_heredoc_delimiters(segment):
            python_heredoc_body = next(heredoc_bodies, "")
        for detector, target in _iter_subagent_segment_write_targets(
            segment,
            python_heredoc_body=python_heredoc_body,
        ):
            yield detector, target, current_dir
        if cmd == "cd":
            target = _cd_target_from_args(args)
            if target:
                current_dir = _resolve_guard_path(target, base_dir=current_dir)


def _iter_subagent_bash_write_targets(command):
    for detector, target, _cwd in _iter_subagent_bash_write_events(command):
        yield detector, target


def _normalize_allowed_write_scope(allowed_write_dir):
    """Normalize a write scope into resolved directory and exact-file sets.

    Backward compatible input:
    - Path/str: directory scope, used by crossover and old worker callers.
    New input:
    - {"dirs": [...], "files": [...]} or a list/tuple/set: exact file scope.
    """
    dirs = []
    files = []
    raw = allowed_write_dir
    if isinstance(raw, dict):
        dirs.extend(raw.get("dirs") or raw.get("directories") or [])
        files.extend(raw.get("files") or raw.get("paths") or [])
    elif isinstance(raw, (list, tuple, set)):
        files.extend(raw)
    elif raw is not None:
        dirs.append(raw)

    resolved_dirs = []
    resolved_files = []
    try:
        from pathlib import Path
        for item in dirs:
            if item:
                resolved_dirs.append(str(Path(_local_path_from_file_uri(item)).resolve()))
        for item in files:
            if item:
                resolved_files.append(str(Path(_local_path_from_file_uri(item)).resolve()))
    except Exception:
        resolved_dirs = [str(item) for item in dirs if item]
        resolved_files = [str(item) for item in files if item]
    return {"dirs": resolved_dirs, "files": resolved_files}


def _local_path_from_file_uri(path):
    """Return a local filesystem path for file:/... URIs.

    Claude sometimes echoes repository paths as ``file:/abs/path`` in tool
    inputs. Treat local file URIs as their real path before scope checks so the
    guard does not block legitimate writes inside the assigned bot file.
    """
    text = str(path or "").strip().strip("'\"")
    if not text.lower().startswith("file:"):
        return text
    try:
        parsed = urlsplit(text)
    except Exception:
        return text
    if parsed.scheme.lower() != "file":
        return text
    netloc = (parsed.netloc or "").lower()
    if netloc not in {"", "localhost"}:
        return text
    return unquote(parsed.path or "")


def _path_inside_allowed_scope(path, allowed_scope, base_dir=None):
    try:
        from pathlib import Path

        candidate = Path(_local_path_from_file_uri(path))
        if not candidate.is_absolute():
            base = Path(base_dir).resolve(strict=False) if base_dir else _project_root_for_guard()
            candidate = base / candidate
        resolved = str(candidate.resolve(strict=False))
        for allowed_file in allowed_scope.get("files", []):
            if resolved == str(Path(allowed_file).resolve(strict=False)):
                return True
        for allowed_dir in allowed_scope.get("dirs", []):
            try:
                if os.path.commonpath([resolved, allowed_dir]) == allowed_dir:
                    return True
            except ValueError:
                continue
    except Exception:
        return False
    return False


def _subagent_write_target_outside_allowed(target, allowed_dir, base_dir=None):
    text = _strip_shell_grouping_parens(str(target or "").strip().strip("'\""))
    if not text:
        return True
    low = text.lower()
    if text.startswith("&") or low in _SAFE_REDIRECT_TARGETS or text == "-":
        return False
    if text == "__unknown_patch_target__":
        return True

    try:
        allowed_scope = _normalize_allowed_write_scope(allowed_dir)
        concrete = re.split(r"[*?\[$`{]", text, maxsplit=1)[0] or text
        concrete = concrete.rstrip()
        if not concrete:
            return True
        return not _path_inside_allowed_scope(concrete, allowed_scope, base_dir=base_dir)
    except Exception:
        return _subagent_is_outside_allowed(text, allowed_dir)


def _subagent_bash_write_scope_violation(command, allowed_dir):
    """Return a violation reason when a Bash mutation writes outside allowed_dir."""
    python_write_event_seen = False
    for detector, target, cwd in _iter_subagent_bash_write_events(command):
        if detector.startswith("python_"):
            python_write_event_seen = True
        if _subagent_write_target_outside_allowed(target, allowed_dir, base_dir=cwd):
            return f"{detector}:{str(target)[:120]}"

    mutation_detector = _subagent_bash_mutation_detector(command)
    if not mutation_detector:
        return None

    if mutation_detector.startswith("python_"):
        if python_write_event_seen:
            return None
        return mutation_detector if _subagent_is_outside_allowed(command, allowed_dir) else None
    if mutation_detector.startswith("git_") or mutation_detector == "git_tag_mutation":
        return mutation_detector
    if mutation_detector == "bash_pattern:patch":
        return mutation_detector

    target_aware = {
        "bash_pattern:cat >",
        "bash_pattern:cat >>",
        "bash_pattern:cp",
        "bash_pattern:mkdir",
        "bash_pattern:mv",
        "bash_pattern:rm",
        "bash_pattern:rmdir",
        "bash_pattern:sed -i",
        "bash_pattern:tee",
        "bash_pattern:touch",
    }
    if mutation_detector in target_aware:
        return None
    return mutation_detector if _subagent_is_outside_allowed(command, allowed_dir) else None


def _iter_subagent_git_args(command):
    text = _strip_heredoc_bodies(command)
    for match in re.finditer(r"(?:(?<=^)|(?<=[\s;&|()]))git\b([^;&|()]*)", text, re.IGNORECASE):
        rest = match.group(1).strip()
        try:
            yield shlex.split(rest)
        except ValueError:
            yield rest.split()


def _subagent_git_subcommand(args):
    i = 0
    while i < len(args):
        arg = str(args[i])
        low = arg.lower()
        if low in {"--version", "version", "--help", "help"}:
            return low.lstrip("-"), args[i + 1:]
        if low in _SUBAGENT_GIT_GLOBAL_OPTIONS_WITH_VALUE:
            i += 2
            continue
        if any(low.startswith(opt + "=") for opt in _SUBAGENT_GIT_GLOBAL_OPTIONS_WITH_VALUE):
            i += 1
            continue
        if low in {"--no-pager", "--paginate"}:
            i += 1
            continue
        if low.startswith("-"):
            i += 1
            continue
        return low, args[i + 1:]
    return "", []


def _subagent_git_command_mutation_detector(command):
    for args in _iter_subagent_git_args(command):
        subcmd, rest = _subagent_git_subcommand(args)
        if subcmd in {"", "version", "help"}:
            continue
        if subcmd == "tag":
            if _subagent_git_tag_invocation_is_mutating(rest):
                return "git_tag_mutation"
            continue
        if subcmd not in _SUBAGENT_GIT_READONLY_COMMANDS:
            return f"git_command:{subcmd}"
    return None


def _subagent_bash_command_mutation_detector(command):
    """Detect write-like shell commands by parsed command name, not substrings."""
    command_map = {
        "cp_dest": "bash_pattern:cp",
        "mkdir": "bash_pattern:mkdir",
        "mv_dest": "bash_pattern:mv",
        "mv_source": "bash_pattern:mv",
        "patch": "bash_pattern:patch",
        "rm": "bash_pattern:rm",
        "rmdir": "bash_pattern:rmdir",
        "sed_i": "bash_pattern:sed -i",
        "tee": "bash_pattern:tee",
        "touch": "bash_pattern:touch",
    }
    for detector, target in _iter_subagent_bash_write_targets(command):
        if detector == "write_redirect":
            return f"write_redirect:{str(target)[:120]}"
        mapped = command_map.get(detector)
        if mapped:
            return mapped
    return None


def _subagent_bash_mutation_detector(command):
    """Return the detector name when Bash appears to write/delete/move files."""
    text = str(command)
    low = text.lower()
    if "python" in low:
        if _SUBAGENT_PYTHON_OPEN_WRITE_RE.search(low):
            return "python_open_write_mode"
        for pattern in _SUBAGENT_PYTHON_WRITE_PATTERNS:
            if pattern in low:
                return f"python_write_pattern:{pattern}"
    git_detector = _subagent_git_command_mutation_detector(command)
    if git_detector:
        return git_detector
    bash_detector = _subagent_bash_command_mutation_detector(command)
    if bash_detector:
        return bash_detector
    if _subagent_git_tag_is_mutating(command):
        return "git_tag_mutation"
    return None


def _subagent_bash_is_mutation(command):
    """Return True when a Bash command appears to write/delete/move files."""
    return _subagent_bash_mutation_detector(command) is not None


def _git_log_has_bounded_scope(rest):
    """True when git-log arguments are constrained enough for LLM inspection.

    Full-history archaeology was a root cause of v255 Master stalls. The guard
    allows small bounded history reads and explicit revision ranges, but rejects
    all-repo pickaxe scans and unbounded repository history walks.
    """
    args = [str(a) for a in rest]
    for i, arg in enumerate(args):
        low = arg.lower()
        if low in {"--max-count", "-n"} and i + 1 < len(args):
            return True
        if low.startswith("--max-count="):
            return True
        if re.fullmatch(r"-\d+", low):
            return True
        if ".." in arg and not arg.startswith("-"):
            return True
        if arg == "--" and i + 1 < len(args):
            return True
    return False


def _subagent_bash_cost_detector(command):
    """Return a reason string for read-only but high-cost Bash commands."""
    for args in _iter_subagent_git_args(command):
        subcmd, rest = _subagent_git_subcommand(args)
        if subcmd != "log":
            continue
        args_text = [str(a) for a in rest]
        lows = [a.lower() for a in args_text]
        if any(a == "--all" or a.startswith("--all=") for a in lows):
            return "git_log_all_history"
        if any(a == "-S" or a.startswith("-S") or a == "-G" or a.startswith("-G")
               for a in args_text):
            return "git_log_pickaxe_full_history"
        if not _git_log_has_bounded_scope(rest):
            return "git_log_unbounded_history"
    return None


def _make_subagent_cost_guard(role_name):
    """Build a hook that denies high-cost read-only Bash commands.

    Mutation safety is handled separately. This guard addresses commands that
    are technically read-only but can monopolize CPU/wall time, such as
    `git log --all -S...` in Master planning.
    """
    async def handler(hook_input, tool_use_id, context):
        try:
            from claude_agent_sdk.types import SyncHookJSONOutput
            tool_name = hook_input.get("tool_name", "") if isinstance(hook_input, dict) else ""
            tool_input = hook_input.get("tool_input", {}) if isinstance(hook_input, dict) else {}
            if tool_name != "Bash":
                return SyncHookJSONOutput()
            cmd = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
            reason = _subagent_bash_cost_detector(cmd)
            if not reason:
                return SyncHookJSONOutput()
            blocked = (
                f"Bash command denied by runtime cost guard ({reason}). Use bounded "
                "inspection only: rg/sed/head/tail or git log with --max-count <= 20 "
                "or an explicit revision range. Command: " + str(cmd)[:180]
            )
            try:
                from system_log import log_system_event
                log_system_event(
                    "pipeline.subagent_cost_guard_block",
                    "warn",
                    f"BLOCKED high-cost {role_name} Bash: {reason}",
                    {
                        "role": role_name,
                        "tool": tool_name,
                        "reason": reason,
                        "recoverable": True,
                        "next_action": "retry_with_bounded_inspection",
                        "command_preview": str(cmd)[:2000],
                        "command_truncated": len(str(cmd)) > 2000,
                    },
                )
            except Exception:
                pass
            return SyncHookJSONOutput(hookSpecificOutput={
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": blocked,
            })
        except Exception:
            pass
        from claude_agent_sdk.types import SyncHookJSONOutput
        return SyncHookJSONOutput()

    try:
        from claude_agent_sdk.types import HookMatcher
        return {"PreToolUse": [HookMatcher(matcher="Bash", hooks=[handler])]}
    except Exception:
        return None


_MASTER_LIVE_EVIDENCE_FILENAMES = (
    "glicko_ratings.json",
    "head_to_head.json",
    "bot_stats.json",
    "selection_snapshot.json",
    "rating_history.jsonl",
    "match_history.jsonl",
    "eval_rounds.jsonl",
    "behavior_archive.json",
    "bot_action_stats.json",
    "bot_action_stats_per_opp.json",
    "experience_pool.md",
    "worker_failures.jsonl",
    "regression_guardian.jsonl",
    "spotlight_manifest.json",
    "battle_evidence.jsonl",
    "battle_pending_summaries.jsonl",
    "battle_lessons.jsonl",
    "battle_experience.md",
    "exploitability.json",
    "critic_calibration.jsonl",
    "cross_gen_exhausted_history.jsonl",
)


def _master_live_evidence_read_violation(
    tool_name,
    tool_input,
    allowed_evidence_snapshot_dir=None,
    deny_all_prompt_evidence=False,
):
    """Return a forbidden live-evidence path read by a Master role, if any."""
    if not isinstance(tool_input, dict):
        return None

    def _path_violation(raw_path):
        raw_text = str(raw_path or "").strip().strip("'\"")
        if not raw_text:
            return None
        try:
            candidate_path = Path(_local_path_from_file_uri(raw_text))
            if not candidate_path.is_absolute():
                candidate_path = Path(PROJECT_ROOT) / candidate_path
            resolved = candidate_path.resolve(strict=False)
            if allowed_evidence_snapshot_dir is not None:
                allowed = Path(allowed_evidence_snapshot_dir).resolve(strict=False)
                try:
                    resolved.relative_to(allowed)
                    return None
                except ValueError:
                    pass

            # Fail closed by evidence identity, not by this checkout's one
            # results root.  The operator and evolution checkouts coexist, and
            # a weak planner could otherwise read the other checkout's mutable
            # aliases.  Likewise, copied/stale results trees are not a valid
            # substitute for the exact generation snapshot.
            if resolved.name in _MASTER_LIVE_EVIDENCE_FILENAMES:
                return str(resolved)
            if "results" in resolved.parts:
                return str(resolved)
            if "official_certificates" in resolved.parts:
                return str(resolved)
            return None
        except Exception:
            normalized = raw_text.replace("\\", "/")
            if (
                any(name in normalized for name in _MASTER_LIVE_EVIDENCE_FILENAMES)
                or "/results/" in f"/{normalized.lstrip('/')}"
                or "/official_certificates/" in f"/{normalized.lstrip('/')}"
            ):
                return raw_text[:500]
            return None

    if deny_all_prompt_evidence:
        raw_target = (
            str(tool_input.get("file_path", ""))
            if tool_name == "Read"
            else str(tool_input.get("command", ""))
            if tool_name == "Bash"
            else ""
        )
        normalized_target = raw_target.replace("\\", "/")
        if (
            any(
                filename in normalized_target
                for filename in _MASTER_LIVE_EVIDENCE_FILENAMES
            )
            or "/results/" in f"/{normalized_target.lstrip('/')}"
            or "/official_certificates/" in f"/{normalized_target.lstrip('/')}"
        ):
            return raw_target[:500]

    if tool_name == "Read":
        return _path_violation(tool_input.get("file_path", ""))
    elif tool_name == "Bash":
        command = str(tool_input.get("command", ""))
        try:
            import shlex

            candidates = shlex.split(command)
        except Exception:
            candidates = command.split()
    else:
        return None
    for candidate in candidates:
        normalized = str(candidate).replace("\\", "/").strip("'\";,()[]{}")
        if (
            any(filename in normalized for filename in _MASTER_LIVE_EVIDENCE_FILENAMES)
            or "/results/" in f"/{normalized.lstrip('/')}"
            or normalized.rstrip("/").endswith("/results")
            or "/official_certificates/" in f"/{normalized.lstrip('/')}"
        ):
            violation = _path_violation(normalized)
            if violation:
                return violation[:500]
    return None


def _make_master_evidence_read_guard(
    role_name,
    allowed_evidence_snapshot_dir=None,
    *,
    deny_all_prompt_evidence=False,
):
    """Prevent an LLM role from bypassing its digest-bound prompt evidence."""
    async def handler(hook_input, tool_use_id, context):
        from claude_agent_sdk.types import SyncHookJSONOutput

        try:
            tool_name = hook_input.get("tool_name", "") if isinstance(hook_input, dict) else ""
            tool_input = hook_input.get("tool_input", {}) if isinstance(hook_input, dict) else {}
            violation = _master_live_evidence_read_violation(
                tool_name,
                tool_input,
                allowed_evidence_snapshot_dir,
                deny_all_prompt_evidence,
            )
            if not violation:
                return SyncHookJSONOutput()
            reason = (
                "Live/global prompt evidence read denied. Use only the "
                "system-owned evidence supplied in this role's prompt; "
                f"forbidden target: {violation}"
            )
            try:
                from system_log import log_system_event

                log_system_event(
                    "pipeline.master_live_evidence_read_blocked",
                    "error",
                    f"BLOCKED {role_name} live evaluation read",
                    {"role": role_name, "tool": tool_name, "target": violation},
                )
            except Exception:
                pass
            return SyncHookJSONOutput(hookSpecificOutput={
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            })
        except Exception:
            return SyncHookJSONOutput()

    try:
        from claude_agent_sdk.types import HookMatcher

        return {"PreToolUse": [
            HookMatcher(matcher="Bash", hooks=[handler]),
            HookMatcher(matcher="Read", hooks=[handler]),
        ]}
    except Exception:
        return None


def _merge_hooks(*hook_sets):
    merged = {}
    for hooks in hook_sets:
        if not hooks:
            continue
        for event_name, matchers in hooks.items():
            merged.setdefault(event_name, []).extend(matchers or [])
    return merged or None


def _subagent_is_outside_allowed(path_or_cmd, allowed_dir):
    """True if a target path/command references protected paths outside allowed_dir."""
    text = str(path_or_cmd or "")
    if not text:
        return False
    low = text.lower()
    allowed_scope = _normalize_allowed_write_scope(allowed_dir)
    stripped = text.strip().strip("'\"")
    if stripped and not re.search(r"[\s;&|()`$<>]", stripped):
        if _path_inside_allowed_scope(stripped, allowed_scope):
            return False
    allowed_values = [*allowed_scope.get("dirs", []), *allowed_scope.get("files", [])]
    allowed_markers = set()
    for allowed in allowed_values:
        allowed_low = str(allowed or "").lower()
        try:
            marker = allowed_low.split("bots/")[-1].replace("\\", "/").strip("/")
            if marker:
                allowed_markers.add(f"bots/{marker}")
                marker_win = marker.replace("/", "\\")
                allowed_markers.add(f"bots\\{marker_win}")
        except Exception:
            pass
        if allowed_low:
            allowed_markers.add(allowed_low.rstrip("/\\"))

    protected_markers = (
        "web/core", "web/server", "web/frontend", "engine/", "sever/",
        "docs/", "results/pipeline_state", "worker_failures",
        "pipeline_state.json", ".git", "claude.md", "agents.md",
    )
    for protected in protected_markers:
        if protected in low and not any(protected in marker for marker in allowed_markers):
            return True

    bot_refs = set(re.findall(rf"bots[/\\]{re.escape(ACTIVE_BOT_PREFIX)}\d+", low))
    allowed_bot_refs = {
        marker for marker in allowed_markers
        if f"bots/{ACTIVE_BOT_PREFIX}" in marker or f"bots\\{ACTIVE_BOT_PREFIX}" in marker
    }
    for ref in bot_refs:
        ref_norm = ref.replace("\\", "/")
        if not any(ref_norm == marker.replace("\\", "/") for marker in allowed_bot_refs):
            return True

    if allowed_markers and any(marker and marker in low for marker in allowed_markers):
        return False
    if f"bots/{ACTIVE_BOT_PREFIX}" in low or f"bots\\{ACTIVE_BOT_PREFIX}" in low:
        return True
    return bool(allowed_markers)


def _make_subagent_write_guard(allowed_write_dir):
    """A1 (2026-06-30): build a PreToolUse hook that restricts a sub-agent's
    Bash/Edit/Write/NotebookEdit to ONLY mutate files under `allowed_write_dir`.

    Sub-agents (workers, crossover) run with bypassPermissions + Bash/Edit tools
    but the orchestrator-level guard hook (_make_bot_dir_guard_hook) does NOT
    cover them (different ClaudeAgentOptions, no hooks= passed). A rogue worker
    prompt could otherwise edit web/core/*.py, other bot dirs, or pipeline state.
    This hook closes that gap by scoping writes to the agent's target bot dir.

    Read-only operations (grep/cat/git status) are allowed anywhere; only
    mutations outside allowed_write_dir are denied.
    """
    _allowed_scope = _normalize_allowed_write_scope(allowed_write_dir)
    _allowed_label = ", ".join(
        [f"dir:{p}" for p in _allowed_scope.get("dirs", [])]
        + [f"file:{p}" for p in _allowed_scope.get("files", [])]
    ) or str(allowed_write_dir)

    async def handler(hook_input, tool_use_id, context):
        try:
            from claude_agent_sdk.types import SyncHookJSONOutput
            tool_name = hook_input.get("tool_name", "") if isinstance(hook_input, dict) else ""
            tool_input = hook_input.get("tool_input", {}) if isinstance(hook_input, dict) else {}
            blocked = None
            if tool_name == "Bash":
                cmd = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
                mutation_detector = _subagent_bash_mutation_detector(cmd)
                write_scope_violation = _subagent_bash_write_scope_violation(cmd, _allowed_scope)
                outside_allowed = bool(write_scope_violation)
                if write_scope_violation:
                    blocked = ("Bash mutation targets a path outside the allowed bot dir "
                               + _allowed_label + " (" + write_scope_violation + "). "
                               "Sub-agents may only mutate the assigned write "
                               "scope. Do not use /tmp or /var/tmp for probe "
                               "logs; use inline pipes such as `2>&1 | grep ...` "
                               "or inspect source files directly. Do not delete "
                               "`__pycache__`, `.pytest_cache`, or generated caches "
                               "from sub-agent Bash; the harness ignores those "
                               "artifacts. If verification output is noisy, use "
                               "read-only filters such as `diff --exclude=__pycache__` "
                               "or `git diff -- <assigned files>`. Command: "
                               + str(cmd)[:100])
            elif tool_name in ("Edit", "Write", "NotebookEdit"):
                fp = tool_input.get("file_path", "") or tool_input.get("notebook_path", "")
                if _subagent_is_outside_allowed(fp, _allowed_scope):
                    blocked = (tool_name + " targets a path outside the allowed bot dir "
                               + _allowed_label + " (" + str(fp) + "). Sub-agents may only edit "
                               "their assigned target bot directory.")
            if blocked:
                try:
                    from system_log import log_system_event
                    command_text = str(cmd) if tool_name == "Bash" else str(
                        tool_input.get("file_path", "") or tool_input.get("notebook_path", "")
                    )
                    log_system_event("pipeline.subagent_guard_block", "error",
                                     "BLOCKED sub-agent " + tool_name + ": " + blocked[:120],
                                     {"tool": tool_name, "reason": blocked[:200],
                                      "allowed_dir": _allowed_label,
                                      "command_preview": command_text[:2000],
                                      "command_truncated": len(command_text) > 2000,
                                      "mutation_detector": locals().get("mutation_detector"),
                                      "write_scope_violation": locals().get("write_scope_violation"),
                                      "outside_allowed": locals().get("outside_allowed")})
                except Exception:
                    pass
                return SyncHookJSONOutput(hookSpecificOutput={
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": blocked,
                })
        except Exception:
            pass
        from claude_agent_sdk.types import SyncHookJSONOutput
        return SyncHookJSONOutput()

    try:
        from claude_agent_sdk.types import HookMatcher
        return {"PreToolUse": [
            HookMatcher(matcher="Bash", hooks=[handler]),
            HookMatcher(matcher="Edit", hooks=[handler]),
            HookMatcher(matcher="Write", hooks=[handler]),
            HookMatcher(matcher="NotebookEdit", hooks=[handler]),
        ]}
    except Exception:
        return None


def _subagent_readonly_mutation_violation(tool_name, tool_input):
    """Return a violation reason when a read-only role tries to mutate state."""
    if tool_name == "Bash":
        cmd = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
        return _subagent_bash_mutation_detector(cmd)
    if tool_name in ("Edit", "Write", "NotebookEdit"):
        return f"{tool_name}_not_allowed"
    return None


def _readonly_guard_recovery_hint(violation: str) -> str:
    """Give read-only roles a concrete non-mutating alternative after a block."""
    base = (
        "Use observe/read commands only. Do not create temp files, write redirects, "
        "tee output, mkdir/touch/rm, or mutate git state."
    )
    if str(violation or "").startswith("write_redirect:"):
        return (
            base
            + " For comparisons, run direct read-only commands such as "
            "`diff -u parent_file target_file`, `git diff --no-index -- parent target`, "
            "`sed -n 'START,ENDp' file`, or `python -c` snippets that only open/read "
            "files and print results. Redirect only to `/dev/null` for stderr noise."
        )
    if str(violation or "").startswith("tee:"):
        return (
            base
            + " Replace `tee` with a plain pipe to the next reader or print the output "
            "directly; do not materialize probe logs."
        )
    return base


def _make_subagent_readonly_guard(role_name):
    """Build a hook that enforces read-only tools for non-worker LLM roles."""
    async def handler(hook_input, tool_use_id, context):
        try:
            from claude_agent_sdk.types import SyncHookJSONOutput
            tool_name = hook_input.get("tool_name", "") if isinstance(hook_input, dict) else ""
            tool_input = hook_input.get("tool_input", {}) if isinstance(hook_input, dict) else {}
            violation = _subagent_readonly_mutation_violation(tool_name, tool_input)
            if not violation:
                return SyncHookJSONOutput()
            if tool_name == "Bash":
                command_text = str(tool_input.get("command", "") if isinstance(tool_input, dict) else "")
            else:
                if isinstance(tool_input, dict):
                    command_text = str(
                        tool_input.get("file_path", "") or tool_input.get("notebook_path", "")
                    )
                else:
                    command_text = ""
            blocked = (
                f"{role_name} is a read-only role; {tool_name} mutation is denied "
                f"({violation}). {_readonly_guard_recovery_hint(violation)}"
            )
            try:
                from system_log import log_system_event
                log_system_event(
                    "pipeline.subagent_readonly_guard_block",
                    "error",
                    f"BLOCKED read-only {role_name} {tool_name}: {violation}",
                    {
                        "role": role_name,
                        "tool": tool_name,
                        "reason": violation,
                        "command_preview": command_text[:2000],
                        "command_truncated": len(command_text) > 2000,
                    },
                )
            except Exception:
                pass
            return SyncHookJSONOutput(hookSpecificOutput={
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": blocked,
            })
        except Exception:
            pass
        from claude_agent_sdk.types import SyncHookJSONOutput
        return SyncHookJSONOutput()

    try:
        from claude_agent_sdk.types import HookMatcher
        return {"PreToolUse": [
            HookMatcher(matcher="Bash", hooks=[handler]),
            HookMatcher(matcher="Edit", hooks=[handler]),
            HookMatcher(matcher="Write", hooks=[handler]),
            HookMatcher(matcher="NotebookEdit", hooks=[handler]),
        ]}
    except Exception:
        return None

def _format_runtime_path_contract(project_root, allowed_write_dir=None):
    """Return a prompt prefix that anchors sub-agents to the active checkout."""
    root = str(Path(project_root).resolve())
    lines = [
        "# Runtime Path Contract",
        f"- The active repository root for this run is `{root}`.",
        "- Bash starts in that directory; prefer relative paths from this root.",
        "- Bash tool working directory may persist across calls. Do not use a bare `cd` that changes later commands. If a command needs a bot-local cwd, use a subshell such as `(cd bots/national_vN && python -B -c '...')`, or start the command with the active repository root explicitly.",
        "- Do not use sibling or parent checkout absolute paths as edit targets.",
        "- Do not write probe output, stderr captures, or temporary logs to `/tmp` or `/var/tmp`.",
        "- Prefer inline pipes such as `2>&1 | grep ...`; if a probe truly needs a file, place it inside the declared write scope and remove it in the same command.",
    ]
    if allowed_write_dir is not None:
        scope = _normalize_allowed_write_scope(allowed_write_dir)
        allowed = [
            *(f"`{path}`" for path in scope.get("files", [])),
            *(f"`{path}/`" for path in scope.get("dirs", [])),
        ]
        if allowed:
            lines.append(
                "- This call may write only inside the declared write scope: "
                + ", ".join(allowed)
                + "."
            )
            lines.append(
                "- Cleanup is also a write. Only mutate files inside the declared write "
                "scope. Do not delete `__pycache__`, `.pytest_cache`, generated caches, "
                "logs, or temporary files in the target, source, parent, opponent, or "
                "other bot directories."
            )
            lines.append(
                "- If probes or imports create caches, leave them in place. The harness "
                "ignores those caches; use read-only filters such as "
                "`diff --exclude=__pycache__` or inspect assigned source files directly."
            )
    else:
        lines.extend([
            "- This LLM role is read-only: Bash may read, diff, grep, count, and print, but must not create, modify, delete, move, tag, checkout, or write any file anywhere.",
            "- Do not use output redirection (`>`, `>>`, `&>`, `&>>`) or `tee` except redirects to `/dev/null` for stderr/stdout noise.",
            "- For snippet comparisons, use direct read-only commands such as `diff -u A B`, `git diff --no-index -- A B`, `sed -n 'START,ENDp' file`, `rg`, or `python -c` that opens files read-only and prints results.",
            "- Never write comparison snippets to `/tmp`, `/var/tmp`, the bot directory, or `web/core/results`; if a Bash command is denied, do not retry the same mutating pattern.",
        ])
    return "\n".join(lines) + "\n\n"


# Serialize role-IO log rotation across threads/processes (mirrors
# battle_experience._LOG_ROTATION_LOCK). Without this lock, two concurrent
# appenders can both observe the file over the size cap and race the rename
# (one wins, the other's rename throws FileNotFoundError — swallowed by the
# except, benign but loses the backup). The lock makes the rotate-then-append
# atomic. A threading.Lock suffices within one process; cross-process safety
# for the append itself is provided by fcntl (locked_file below).
_ROLE_IO_ROTATION_LOCK = threading.Lock()

#: Cap a single role-IO log at 20MB before rotating to one backup (``.1``).
#: battle_exp_llm.log previously grew to 103MB with no upper bound (root-cause
#: 6); this is the structural cap. Mirrors battle_experience's 50MB cap
#: (lowered here because role-IO files are append-heavy and per-role).
_ROLE_IO_MAX_BYTES = 20 * 1024 * 1024


_LLM_FIRST_ACTIVITY_WARN_SEC = float(
    os.environ.get("POK_LLM_FIRST_ACTIVITY_WARN_SEC", "60")
)

_LLM_PROGRESS_INTERVAL_SEC = float(
    os.environ.get("POK_LLM_PROGRESS_INTERVAL_SEC", "120")
)

_LLM_SILENCE_WARN_SEC = float(
    os.environ.get("POK_LLM_SILENCE_WARN_SEC", "240")
)

_ROLE_TIMEOUT_DEFAULTS = {
    # Fallback for analysis/probe roles such as MATCH ANALYST, COMBINED
    # ANALYST, literature probes, and battle-experience synthesis. These can be
    # slower than gate roles on GLM-backed Claude-compatible endpoints, but must
    # still have a hard ceiling so the pipeline cannot wait forever.
    "DEFAULT": (240.0, 360.0, 900.0),
    # Master is the highest leverage failure point: it plans, reads evidence,
    # and can otherwise burn the whole orchestrator cycle before any code exists.
    "MASTER": (120.0, 240.0, 900.0),
    # Review/Critic can be slow on GLM-backed Claude-compatible endpoints.
    # They still have ceilings, but defaults must be long enough to avoid
    # repeated 600s retries that keep the generation stuck at quality_passed.
    "REVIEW": (180.0, 360.0, 1200.0),
    "CRITIC": (180.0, 360.0, 900.0),
    # Crossover synthesizes a whole child bot from two parents and routinely
    # exceeds the generic analysis/probe budget on GLM-backed Claude-compatible
    # endpoints. Keep the idle ceiling, but give total wall-clock enough room so
    # a live stream is not killed and restarted at ~15 minutes.
    "CROSSOVER": (240.0, 420.0, 2400.0),
    # Workers already have an outer WORKER_TIMEOUT. Idle timeout catches stalled
    # streams inside that larger wall-clock budget.
    "WORKER": (180.0, 360.0, 1000.0),
}


def _role_timeout_policy(role_name: str) -> dict:
    """Return hard stream timeout policy for a role.

    Values <=0 disable that timeout. Environment overrides are intentionally
    role-scoped so slow backends can be tuned without changing code.
    """
    role = str(role_name or "").upper()
    key = ""
    if "MASTER" in role:
        key = "MASTER"
    elif "REVIEW" in role:
        key = "REVIEW"
    elif "CRITIC" in role:
        key = "CRITIC"
    elif "CROSSOVER" in role:
        key = "CROSSOVER"
    elif "WORKER" in role:
        key = "WORKER"
    defaults = _ROLE_TIMEOUT_DEFAULTS.get(key or "DEFAULT", (0.0, 0.0, 0.0))

    def _env(name, default):
        try:
            return float(os.environ.get(name, str(default)))
        except Exception:
            return float(default)

    prefix = f"POK_LLM_{key}_" if key else "POK_LLM_DEFAULT_"
    first_activity = _env(prefix + "FIRST_ACTIVITY_TIMEOUT", defaults[0])
    idle = _env(prefix + "IDLE_TIMEOUT", defaults[1])
    total = _env(prefix + "TOTAL_TIMEOUT", defaults[2])
    # B3 (2026-07-09): a shorter stall ceiling enforced AFTER the first
    # substantive model output, i.e. once the stream has entered the
    # tool/thinking loop. Backends like the deepseek-v4-pro endpoint behind
    # cc-switch intermittently stall mid-tool-loop (a tool_use is emitted but
    # its tool_result never returns, or the model stops streaming mid-think).
    # The full idle_timeout (240-420s) is appropriate for the FIRST real
    # output but is too long to wait once we are already in the loop: every
    # mid-loop stall costs the full idle budget before the role retry can
    # restart. Default to ~55% of idle (clamped to [60, 180]s) so a stall is
    # caught well before the full idle ceiling while still tolerating legit
    # slow tool/think deltas. 0 disables (falls back to idle_timeout).
    stall_default = 0.0
    if idle > 0:
        stall_default = max(60.0, min(180.0, idle * 0.55))
    stall = _env(prefix + "STALL_TIMEOUT", stall_default)
    return {
        "policy_key": key or "DEFAULT",
        "first_activity_timeout": first_activity,
        "idle_timeout": idle,
        "stall_timeout": stall,
        "total_timeout": total,
    }


class LLMRoleTimeout(asyncio.TimeoutError):
    """Raised when a role exceeds first-activity, idle, or total timeout."""

    def __init__(self, role_name, timeout_kind, timeout_sec):
        self.role_name = role_name
        self.timeout_kind = timeout_kind
        self.timeout_sec = timeout_sec
        super().__init__(
            f"{role_name}: LLM {timeout_kind} timeout after {timeout_sec:.1f}s"
        )


def set_shutdown_manager(shutdown_mgr):
    """Share the process shutdown state with all role-level LLM calls."""
    global _shutdown_manager
    _shutdown_manager = shutdown_mgr


def _is_shutdown_requested() -> bool:
    try:
        return bool(_shutdown_manager and _shutdown_manager.is_shutting_down)
    except Exception:
        return False


def _emit_llm_event(category, severity, message, **fields):
    """Emit an LLM lifecycle event without letting logging affect execution."""
    try:
        import event_bus
        event_bus.emit(category, severity, message, **fields)
    except Exception:
        pass


def _role_log_metadata(log_file_path):
    path = str(log_file_path or "")
    meta = {"log_file": path}
    match = re.search(r"/v(\d+)/logs/([^/]+)_io\.txt$", path)
    if match:
        meta["version"] = int(match.group(1))
        meta["role_log"] = match.group(2)
    return meta


def _tools_metadata(tools):
    if tools is None:
        return {"tools": []}
    if isinstance(tools, (list, tuple)):
        return {"tools": [str(t) for t in tools]}
    return {"tools": [type(tools).__name__]}


def _usage_metadata(usage):
    if not usage:
        return {}
    try:
        data = usage if isinstance(usage, dict) else usage.model_dump()
    except Exception:
        try:
            data = dict(usage)
        except Exception:
            data = {}
    summary = {}
    for key in (
        "input_tokens", "output_tokens", "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        if key in data:
            summary[key] = data.get(key)
    return summary


def _llm_failure_severity(exc: Exception) -> str:
    """Classify known noisy SDK/business failures without hiding hard failures."""
    if is_success_error_result(exc):
        return "info"
    return "error"


def _append_role_io(log_file_path, text):
    """Append text to a role-IO log file with fcntl locking + 20MB rotation.

    Replaces the bare ``with open(path, "a") as lf: lf.write(...)`` pattern that
    had no locking and no size cap (root-cause 6: battle_exp_llm.log reached
    103MB).

      - fcntl LOCK_EX via ``evolution_infra.locked_file`` → cross-process +
        cross-thread safe (orchestrator + battle_experience workers append
        concurrently to the same path).
      - Before writing: if the file exceeds ``_ROLE_IO_MAX_BYTES`` (20MB),
        rename it to ``.1`` (single overwrite backup, mirroring
        battle_experience._LOG_ROTATION_LOCK). Rotation is serialized by
        ``_ROLE_IO_ROTATION_LOCK`` so two appenders can't race the rename.
      - Each appended chunk is prefixed with ``[<run_id>] `` (or ``[-]`` when
        no run_id is resolvable) so role-IO lines join app.log + events.jsonl
        on the same correlation key (RC6).

    Never raises — logging must not crash the pipeline. Returns silently on any
    error (the underlying stream processing / return value is unaffected).
    """
    try:
        # Resolve the current run_id for the correlation prefix. event_bus reads
        # the live checkpoint as fallback, so this works even in long-lived
        # worker threads that are not pinned to one generation.
        try:
            from event_bus import capture_context
            _ctx = capture_context() or {}
            _rid = _ctx.get("run_id") or "-"
        except Exception:
            _rid = "-"
        chunk = f"[{_rid}] {text}" if not text.startswith("\n") else f"\n[{_rid}] " + text.lstrip("\n")
        # Rotation check + rename (serialized; size read without a lock, which
        # is best-effort — a concurrent writer can grow the file between the
        # stat and the rename, but that only delays rotation by one cycle).
        try:
            if os.path.exists(log_file_path) and os.path.getsize(log_file_path) > _ROLE_IO_MAX_BYTES:
                with _ROLE_IO_ROTATION_LOCK:
                    if os.path.exists(log_file_path) and os.path.getsize(log_file_path) > _ROLE_IO_MAX_BYTES:
                        _rotated = log_file_path + ".1"
                        try:
                            if os.path.exists(_rotated):
                                os.remove(_rotated)
                        except Exception:
                            pass
                        try:
                            os.rename(log_file_path, _rotated)
                        except Exception:
                            pass
        except Exception:
            pass
        from evolution_infra import locked_file
        with locked_file(log_file_path, "a", encoding="utf-8") as lf:
            lf.write(chunk)
    except Exception:
        pass


def extract_result_error(message) -> str:
    """Extract diagnostic error text from a ResultMessage.

    Uses correct SDK attributes:
    - message.errors: list[str]|None — error messages from the SDK
    - message.api_error_status: int|None — HTTP status code (429, 500, etc.)

    Falls back to 'Unknown SDK error' if no error info is available.
    """
    _err_list = getattr(message, 'errors', None) or []
    _status = getattr(message, 'api_error_status', None)
    if _err_list:
        return '; '.join(str(e) for e in _err_list)
    if _status:
        return f'API error {_status}'
    return 'Unknown SDK error'


def _is_rate_limited(output: str) -> bool:
    # Long responses are never rate-limit errors — avoid false positives
    # when LLM discusses "rate limit" or "overloaded" in normal output.
    # NOTE: 429 "Request rejected" is handled separately by _is_quota_exceeded()
    # to avoid triggering the 529 exponential-backoff retry loop.
    if len(output) > 2000:
        return False
    return (
        "overloaded" in output.lower()
        or "该模型当前访问量过大" in output
        or "rate limit" in output.lower()
        or re.search(r'(?:status["\s:=]+529|HTTP/\d\.?\d?\s+529|error.*529)', output, re.IGNORECASE) is not None
    )


def _is_quota_exceeded(output: str) -> bool:
    """Detect 429 quota exhaustion (distinct from 529 overloaded).

    Matches the GLM API error pattern:
        "Request rejected (429) · [1308][已达到 5 小时的使用上限...]"
    """
    if len(output) > 2000:
        return False
    return (
        "Request rejected (429)" in output
        or ("已达到" in output and "使用上限" in output)
    )


def _trim_to_budget(text: str, max_chars: int, tail: bool = False) -> str:
    """Trim text to max_chars. If tail=True, keep the LAST max_chars (most recent content)."""
    if len(text) <= max_chars:
        return text
    note = "\n...[TRIMMED]\n"
    if tail:
        return note + text[-(max_chars - len(note)):]
    return text[:max_chars - len(note)] + note


async def _process_stream(query_gen, log_file_path, ui, role_name):
    """Process a streaming LLM query, returning (texts, cost_usd, usage).

    Handles TextBlock, ThinkingBlock, ToolUseBlock, UserMessage ToolResultBlock,
    and ResultMessage.
    Writes to log file and emits UI events as they arrive.
    """
    texts = []
    cost_usd = None
    usage = None
    stream_started_at = time.time()
    first_activity_logged = False
    # B2 (2026-07-09): a SystemMessage (e.g. subtype=init, thinking_tokens) is
    # emitted by the SDK/proxy purely to acknowledge the request or carry
    # billing telemetry — it is not model output. Letting it satisfy the
    # first-activity gate flips the wait budget from first_activity_timeout to
    # idle_timeout (e.g. 240s → 420s for CROSSOVER). When a backend (here the
    # GLM proxy behind cc-switch) stalls right after init, that extra slack
    # turns a hard stall into a ~420s dead wait per attempt. Track substantive
    # activity (AssistantMessage/ToolUse/UserMessage/ResultMessage) separately
    # and keep enforcing first_activity_timeout until real output arrives.
    substantive_activity_logged = False
    message_count = 0
    last_progress_at = stream_started_at
    last_message_at = stream_started_at
    last_silence_event_at = stream_started_at
    stream_done = False
    text_chars = 0
    thinking_chars = 0
    tool_use_count = 0
    tool_result_count = 0
    system_message_count = 0
    thinking_tokens_estimate = 0
    thinking_tokens_delta_total = 0
    unknown_message_count = 0
    timeout_policy = _role_timeout_policy(role_name)
    total_timeout = float(timeout_policy.get("total_timeout") or 0)
    first_activity_timeout = float(timeout_policy.get("first_activity_timeout") or 0)
    idle_timeout = float(timeout_policy.get("idle_timeout") or 0)
    # B3: shorter stall ceiling once substantive output has started (tool/think
    # loop). 0 means "do not enforce a separate stall ceiling; use idle_timeout".
    stall_timeout = float(timeout_policy.get("stall_timeout") or 0)
    total_deadline = (stream_started_at + total_timeout) if total_timeout > 0 else None

    def _tool_result_text(content):
        if isinstance(content, str):
            return content
        if content is None:
            return ""
        try:
            return json.dumps(content, ensure_ascii=False, default=str)
        except Exception:
            return str(content)

    def _record_tool_result(content, is_error=None, source="ToolResultBlock"):
        nonlocal tool_result_count
        tool_result_count += 1
        result_text = _tool_result_text(content)
        if not result_text:
            result_text = "[empty tool result]"
        result_preview = result_text[:3000]
        header = f"[TOOL_RESULT source={source} is_error={bool(is_error)}]"
        _append_role_io(log_file_path, f"\n{header} {result_preview}\n")
        ui.log_io(result_preview, "tool_result", role_name)

    def _mark_first_activity(kind, substantive=True):
        nonlocal first_activity_logged, substantive_activity_logged
        # substantive output (assistant/tool/user/result) upgrades the gate so
        # the wait loop may switch to the idle_timeout budget. System-only
        # messages record the first-activity milestone for observability but do
        # NOT lift the (shorter) first_activity_timeout ceiling — see B2.
        if substantive:
            substantive_activity_logged = True
        if first_activity_logged:
            return
        first_activity_logged = True
        elapsed = time.time() - stream_started_at
        delayed = elapsed >= _LLM_FIRST_ACTIVITY_WARN_SEC
        category = (
            "pipeline.llm_role_first_activity_delayed"
            if delayed else
            "pipeline.llm_role_first_activity"
        )
        severity = "warn" if delayed else "info"
        _emit_llm_event(
            category, severity,
            f"{role_name}: first LLM stream activity after {elapsed:.1f}s",
            role=role_name,
            elapsed_sec=round(elapsed, 2),
            first_activity_warn_sec=_LLM_FIRST_ACTIVITY_WARN_SEC,
            activity_kind=kind,
            substantive=substantive,
            **_role_log_metadata(log_file_path),
        )

    def _emit_progress():
        nonlocal last_progress_at
        if _LLM_PROGRESS_INTERVAL_SEC <= 0:
            return
        now = time.time()
        if now - last_progress_at < _LLM_PROGRESS_INTERVAL_SEC:
            return
        elapsed = now - stream_started_at
        last_progress_at = now
        _emit_llm_event(
            "pipeline.llm_role_progress", "info",
            f"{role_name}: LLM stream active for {elapsed:.1f}s",
            role=role_name,
            elapsed_sec=round(elapsed, 2),
            messages_seen=message_count,
            system_messages_seen=system_message_count,
            unknown_messages_seen=unknown_message_count,
            text_chars=text_chars,
            thinking_chars=thinking_chars,
            thinking_tokens_estimate=thinking_tokens_estimate,
            thinking_tokens_delta_total=thinking_tokens_delta_total,
            tool_use_count=tool_use_count,
            tool_result_count=tool_result_count,
            progress_interval_sec=_LLM_PROGRESS_INTERVAL_SEC,
            **_role_log_metadata(log_file_path),
        )

    async def _silence_watchdog():
        nonlocal last_silence_event_at
        if _LLM_SILENCE_WARN_SEC <= 0:
            return
        sleep_for = max(0.01, min(_LLM_SILENCE_WARN_SEC / 2.0, 30.0))
        while not stream_done:
            await asyncio.sleep(sleep_for)
            if stream_done:
                return
            now = time.time()
            silent_for = now - last_message_at
            since_last_event = now - last_silence_event_at
            if silent_for < _LLM_SILENCE_WARN_SEC:
                continue
            if since_last_event < _LLM_SILENCE_WARN_SEC:
                continue
            last_silence_event_at = now
            _emit_llm_event(
                "pipeline.llm_role_stream_silent", "warn",
                f"{role_name}: no productive LLM stream messages for {silent_for:.1f}s",
                role=role_name,
                elapsed_sec=round(now - stream_started_at, 2),
                silent_for_sec=round(silent_for, 2),
                silence_warn_sec=_LLM_SILENCE_WARN_SEC,
                messages_seen=message_count,
                system_messages_seen=system_message_count,
                unknown_messages_seen=unknown_message_count,
                text_chars=text_chars,
                thinking_chars=thinking_chars,
                thinking_tokens_estimate=thinking_tokens_estimate,
                thinking_tokens_delta_total=thinking_tokens_delta_total,
                tool_use_count=tool_use_count,
                tool_result_count=tool_result_count,
                **_role_log_metadata(log_file_path),
            )

    def _should_log_sparse_count(count):
        return count == 1 or count in {5, 10, 20, 50} or count % 100 == 0

    def _timeout_limit(effective_kind, wait_timeout):
        if effective_kind == "total":
            return total_timeout
        if effective_kind == "first_activity":
            return first_activity_timeout
        if effective_kind == "idle":
            return idle_timeout
        if effective_kind == "stall":
            return stall_timeout
        return wait_timeout or 0

    def _raise_role_timeout(timeout_kind, wait_timeout):
        elapsed = time.time() - stream_started_at
        effective_kind = timeout_kind or "stream"
        effective_limit = _timeout_limit(effective_kind, wait_timeout)
        _emit_llm_event(
            f"pipeline.llm_role_{effective_kind}_timeout",
            "error",
            f"{role_name}: LLM {effective_kind} timeout after {effective_limit:.1f}s",
            role=role_name,
            elapsed_sec=round(elapsed, 2),
            timeout_sec=round(effective_limit, 2),
            messages_seen=message_count,
            system_messages_seen=system_message_count,
            unknown_messages_seen=unknown_message_count,
            text_chars=text_chars,
            thinking_chars=thinking_chars,
            thinking_tokens_estimate=thinking_tokens_estimate,
            thinking_tokens_delta_total=thinking_tokens_delta_total,
            tool_use_count=tool_use_count,
            tool_result_count=tool_result_count,
            **timeout_policy,
            **_role_log_metadata(log_file_path),
        )
        raise LLMRoleTimeout(role_name, effective_kind, effective_limit)

    try:
        watchdog_task = asyncio.create_task(_silence_watchdog())
        stream_iter = query_gen.__aiter__()
        while True:
            wait_timeout = None
            timeout_kind = None
            now = time.time()
            # B2: keep the (shorter) first_activity_timeout budget until we see
            # substantive model output, not just SDK/proxy bookkeeping
            # (SystemMessage init/thinking_tokens). This prevents a stalled
            # backend from degrading into the longer idle_timeout dead-wait.
            if not substantive_activity_logged and first_activity_timeout > 0:
                wait_timeout = max(
                    0.0,
                    first_activity_timeout - (now - stream_started_at),
                )
                timeout_kind = "first_activity"
            elif substantive_activity_logged:
                # B3: once we are inside the tool/think loop, a mid-loop stall
                # (tool_use emitted but tool_result never returns, or the model
                # stops streaming mid-think) should be caught at the shorter
                # stall_timeout rather than burning the full idle_timeout
                # before the role retry can restart. stall_timeout<=0 disables
                # this layer and falls back to idle_timeout.
                idle_budget = (idle_timeout - (now - last_message_at)) if idle_timeout > 0 else None
                stall_budget = (stall_timeout - (now - last_message_at)) if stall_timeout > 0 else None
                if stall_budget is not None and (idle_budget is None or stall_budget < idle_budget):
                    wait_timeout = max(0.0, stall_budget)
                    timeout_kind = "stall"
                elif idle_budget is not None:
                    wait_timeout = max(0.0, idle_budget)
                    timeout_kind = "idle"
            if total_deadline is not None:
                remaining_total = max(0.0, total_deadline - now)
                if wait_timeout is None or remaining_total < wait_timeout:
                    wait_timeout = remaining_total
                    timeout_kind = "total"
            if wait_timeout is not None and wait_timeout <= 0:
                _raise_role_timeout(timeout_kind, wait_timeout)
            try:
                if wait_timeout is None:
                    message = await stream_iter.__anext__()
                else:
                    message = await asyncio.wait_for(
                        stream_iter.__anext__(),
                        timeout=max(0.001, wait_timeout),
                    )
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                _raise_role_timeout(timeout_kind, wait_timeout)
            message_count += 1
            productive_message = False
            if isinstance(message, AssistantMessage):
                productive_message = True
                _mark_first_activity("assistant")
                for block in message.content:
                    if isinstance(block, TextBlock):
                        text = block.text
                        text_chars += len(text or "")
                        texts.append(text)
                        _append_role_io(log_file_path, text + "\n")
                        ui.log_io(text, "claude", role_name)
                    elif isinstance(block, ThinkingBlock):
                        thinking = block.thinking or "[thinking...]"
                        thinking_chars += len(thinking or "")
                        _append_role_io(log_file_path, f"\n[THINKING] {thinking[:2000]}\n")
                        ui.log_io(thinking, "thinking", role_name)
                    elif isinstance(block, ToolUseBlock):
                        tool_use_count += 1
                        args_str = json.dumps(block.input, ensure_ascii=False, indent=2)[:2000]
                        _append_role_io(log_file_path, f"\n[TOOL_CALL] {block.name}\n[ARGS] {args_str}\n")
                        ui.log_io(f"\n[tool: {block.name}]", "tool", role_name)
                        ui.emit_tool_call(block.name, block.input, role_name)
                    elif isinstance(block, ToolResultBlock):
                        _record_tool_result(block.content, getattr(block, "is_error", None))
                _emit_progress()
            elif isinstance(message, UserMessage):
                productive_message = True
                _mark_first_activity("user")
                saw_tool_result_block = False
                if isinstance(message.content, list):
                    for block in message.content:
                        if isinstance(block, ToolResultBlock):
                            saw_tool_result_block = True
                            _record_tool_result(
                                block.content,
                                getattr(block, "is_error", None),
                            )
                tool_use_result = getattr(message, "tool_use_result", None)
                if tool_use_result is not None and not saw_tool_result_block:
                    _record_tool_result(
                        tool_use_result,
                        None,
                        source="UserMessage.tool_use_result",
                    )
                _emit_progress()
            elif isinstance(message, SystemMessage):
                productive_message = True
                system_message_count += 1
                subtype = getattr(message, "subtype", None) or "unknown"
                data = getattr(message, "data", None)
                if not isinstance(data, dict):
                    data = {}
                if subtype == "thinking_tokens":
                    try:
                        estimate = int(data.get("estimated_tokens") or 0)
                    except (TypeError, ValueError):
                        estimate = 0
                    try:
                        delta = int(data.get("estimated_tokens_delta") or 0)
                    except (TypeError, ValueError):
                        delta = 0
                    thinking_tokens_estimate = max(
                        thinking_tokens_estimate,
                        estimate,
                    )
                    thinking_tokens_delta_total += max(0, delta)
                # B2: SystemMessages (init / thinking_tokens) are SDK/proxy
                # bookkeeping, not model output — do NOT let them satisfy the
                # substantive first-activity gate, otherwise a backend that
                # stalls right after init slips into the longer idle_timeout.
                _mark_first_activity(f"system:{subtype}", substantive=False)
                if _should_log_sparse_count(system_message_count):
                    _append_role_io(
                        log_file_path,
                        f"\n[SYSTEM_MESSAGE subtype={subtype} "
                        f"count={system_message_count} "
                        f"thinking_tokens={thinking_tokens_estimate} "
                        f"thinking_delta_total={thinking_tokens_delta_total}]\n",
                    )
                _emit_progress()
            elif isinstance(message, ResultMessage):
                productive_message = True
                _mark_first_activity("result")
                cost_usd = message.total_cost_usd
                usage = message.usage
                billing_results = _LLM_BILLING_RESULTS.get()
                if isinstance(billing_results, list):
                    billing_results.append(message)
                _emit_progress()
                # A1 (v125 retry-storm fix): capture ResultMessage diagnostic fields.
                # Previously this branch read ONLY cost/usage, discarding subtype /
                # is_error / num_turns / stop_reason. That made every Master-failure
                # mode (missing-return / NO_FENCE / empty-output) collapse to the SAME
                # undifferentiated "malformed JSON" symptom downstream, which caused
                # multiple rounds of mis-attribution (v125 wasted several analysis
                # cycles before the real root cause was found). Log the diagnostics so
                # future failures are classifiable. Return signature is UNCHANGED (3-tuple)
                # — this is pure observation and must not alter retry/circuit behavior.
                try:
                    _subtype = getattr(message, "subtype", None)
                    _is_err = bool(getattr(message, "is_error", False))
                    if _is_err or (_subtype and _subtype != "success"):
                        _num_turns = getattr(message, "num_turns", None)
                        _stop_reason = getattr(message, "stop_reason", None)
                        _diag = {
                            "role": role_name,
                            "subtype": _subtype,
                            "is_error": _is_err,
                            "num_turns": _num_turns,
                            "stop_reason": _stop_reason,
                        }
                        _append_role_io(
                            log_file_path,
                            "\n[RESULT_DIAG] "
                            + json.dumps(_diag, ensure_ascii=False, default=str)
                            + "\n",
                        )
                        if ui:
                            ui.log_history(
                                f"{role_name}: ResultMessage non-success "
                                f"(subtype={_subtype}, is_error={_is_err}, "
                                f"num_turns={_num_turns}, stop_reason={_stop_reason})",
                                "warn",
                            )
                            try:
                                import event_bus
                                event_bus.warn(
                                    "pipeline.llm_result_non_success",
                                    f"{role_name} ResultMessage non-success (subtype={_subtype})",
                                    role=role_name,
                                    subtype=_subtype,
                                    is_error=_is_err,
                                    num_turns=_num_turns,
                                    stop_reason=_stop_reason,
                                )
                            except Exception:
                                pass
                except Exception:
                    pass
            else:
                unknown_message_count += 1
                message_type = type(message).__name__
                message_module = type(message).__module__
                if _should_log_sparse_count(unknown_message_count):
                    _append_role_io(
                        log_file_path,
                        f"\n[UNKNOWN_SDK_MESSAGE] {message_module}.{message_type}: "
                        f"{repr(message)[:1000]}\n",
                    )
                    _emit_llm_event(
                        "pipeline.llm_role_unknown_message",
                        "warn",
                        f"{role_name}: unknown SDK stream message {message_type}",
                        role=role_name,
                        elapsed_sec=round(time.time() - stream_started_at, 2),
                        message_type=message_type,
                        message_module=message_module,
                        messages_seen=message_count,
                        system_messages_seen=system_message_count,
                        unknown_messages_seen=unknown_message_count,
                        text_chars=text_chars,
                        thinking_chars=thinking_chars,
                        thinking_tokens_estimate=thinking_tokens_estimate,
                        thinking_tokens_delta_total=thinking_tokens_delta_total,
                        tool_use_count=tool_use_count,
                        tool_result_count=tool_result_count,
                        **_role_log_metadata(log_file_path),
                    )
            if productive_message:
                last_message_at = time.time()
    except ClaudeSDKError as e:
        _emit_llm_event(
            "pipeline.llm_role_stream_sdk_error", "warn",
            f"{role_name}: SDK stream error: {str(e)[:180]}",
            role=role_name,
            elapsed_sec=round(time.time() - stream_started_at, 2),
            messages_seen=message_count,
            exception_type=type(e).__name__,
            error=str(e)[:500],
            **_role_log_metadata(log_file_path),
        )
        ui.log_io(f"[ERROR] {e}", "error", role_name)
        raise   # propagate so callers distinguish a hard SDK error from an empty-but-valid reply
    except asyncio.CancelledError:
        _category, _severity, _cancel_fields = _cancelled_event(
            "pipeline.llm_role_stream_cancelled",
            "pipeline.llm_role_stream_parent_timeout_cancelled",
        )
        _scope = _cancel_fields.get("cancel_scope")
        _timeout = _cancel_fields.get("timeout_sec")
        if _cancel_fields.get("cancel_reason") == "parent_timeout":
            _msg = (
                f"{role_name}: LLM stream cancelled by parent timeout"
                f" ({_scope}, {_timeout:g}s)"
                if isinstance(_timeout, (int, float))
                else f"{role_name}: LLM stream cancelled by parent timeout ({_scope})"
            )
        else:
            _msg = f"{role_name}: LLM stream cancelled"
        _emit_llm_event(
            _category, _severity,
            _msg,
            role=role_name,
            elapsed_sec=round(time.time() - stream_started_at, 2),
            messages_seen=message_count,
            **_cancel_fields,
            **_role_log_metadata(log_file_path),
        )
        ui.log_io(f"\n[{role_name} CANCELLED]", "error", role_name)
        raise
    finally:
        stream_done = True
        if 'watchdog_task' in locals():
            watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog_task
    return texts, cost_usd, usage


# claude_agent_sdk 0.2.91 intermittently raises ClaudeSDKError "Missing required
# field in assistant message: 'signature'" mid-stream. It is transient (a fresh
# query usually succeeds) but frequent enough that 3 retries occasionally exhaust,
# stalling Master/analyst. Bumped to 5 with slightly longer backoff so a brief
# SDK-side storm still resolves without surfacing a failure to the caller.
_SIGNATURE_MAX_ATTEMPTS = 5


def _merge_billing_usage(total, usage):
    if not isinstance(usage, dict):
        return total
    merged = dict(total or {})
    for key, value in usage.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            previous = merged.get(key, 0)
            if not isinstance(previous, (int, float)) or isinstance(previous, bool):
                previous = 0
            merged[key] = previous + value
        elif key not in merged:
            # Keep non-numeric metadata from the first result. It is not summed,
            # but callers do not lose fields such as service tier/model detail.
            merged[key] = value
    return merged


def _record_completed_billing_attempt(
    *,
    role_name,
    ui,
    billing_results,
    fallback_cost,
    fallback_usage,
    attempt,
    billing_call_id,
):
    """Record each SDK Result exactly once and return newly billed totals."""

    from orchestrator_cost_policy import (
        assert_operator_cost_limit_available,
        current_generation_cost_scope,
        record_generation_cost,
        sdk_result_event_id,
    )

    results = list(billing_results or [])
    if not results and (fallback_cost is not None or fallback_usage is not None):
        results = [None]
    billed_cost = 0.0
    billed_usage = None
    for result_index, result in enumerate(results):
        if result is None:
            cost_usd = fallback_cost
            usage = fallback_usage
            event_id = (
                f"llm-result-fallback:{billing_call_id}:"
                f"{int(attempt)}:{int(result_index)}"
            )
        else:
            cost_usd = getattr(result, "total_cost_usd", None)
            usage = getattr(result, "usage", None)
            event_id = sdk_result_event_id(
                result,
                source="llm_query",
                attempt=attempt,
            )
        status = record_generation_cost(
            role_name,
            cost_usd,
            usage,
            source="llm_query_attempt",
            event_id=event_id,
        )
        accepted = bool(
            not status.get("active")
            or status.get("recorded")
            or status.get("pending_only")
        )
        if accepted:
            if cost_usd is not None:
                billed_cost += float(cost_usd)
            billed_usage = _merge_billing_usage(billed_usage, usage)
            if ui:
                ui.update_cost(role_name, float(cost_usd or 0.0), usage)
        elif ui and status.get("active") and not status.get("accounting_ok"):
            # A pending write-ahead entry is already included in durable status;
            # refresh the projection without incrementing a replay twice.
            scope = current_generation_cost_scope()
            begin_cost = getattr(ui, "begin_generation_cost", None)
            if scope is not None and callable(begin_cost):
                begin_cost(
                    scope.generation_id,
                    status.get("spent_usd", 0.0),
                    scope.receipt(
                        spent_before_usd=float(status.get("spent_usd") or 0.0),
                        ledger_errors=tuple(status.get("accounting_errors") or ()),
                    ),
                )
        assert_operator_cost_limit_available()
    return billed_cost, billed_usage


async def _run_stream_with_signature_retry(full_prompt, options, log_file_path, ui, role_name):
    """Run one streaming query with retries on transient SDK signature errors.

    Extracted so the 529/429 retry paths reuse the same handling as the initial query.
    Returns (texts_list, cost_usd, usage).
    """
    last_sdk_err = None
    total_cost = 0.0
    total_usage = None
    billing_call_id = uuid.uuid4().hex
    for sdk_attempt in range(_SIGNATURE_MAX_ATTEMPTS):
        query_gen = claude_query(prompt=full_prompt, options=options)
        billing_results = []
        billing_token = _LLM_BILLING_RESULTS.set(billing_results)
        try:
            texts, cost_usd, usage = await _process_stream(query_gen, log_file_path, ui, role_name)
            attempt_cost, attempt_usage = _record_completed_billing_attempt(
                role_name=role_name,
                ui=ui,
                billing_results=billing_results,
                fallback_cost=cost_usd,
                fallback_usage=usage,
                attempt=sdk_attempt,
                billing_call_id=billing_call_id,
            )
            total_cost += attempt_cost
            total_usage = _merge_billing_usage(total_usage, attempt_usage)
            if sdk_attempt > 0 and ui:
                ui.log_history(
                    f"{role_name}: SDK stream recovered after {sdk_attempt} signature retry/retries",
                    "info",
                )
            # Empty-output retry (root-cause fix for Master JSON collapse, 2026-06-19).
            # claude_agent_sdk 0.2.91's signature bug has TWO failure modes:
            #   (a) raises ClaudeSDKError mid-stream — caught above, retried.
            #   (b) stream "succeeds" with a ResultMessage (cost/usage present) but ZERO
            #       TextBlocks → _process_stream returns ([], cost, usage) WITHOUT raising.
            # Mode (b) escaped ALL retry layers (only ClaudeSDKError was caught), so the
            # empty output reached the caller, parse_json_output('') returned None, and
            # the agent logged "malformed JSON" → 3x retry exhaust → abandon_generation.
            # Measured impact: 140/540 (26%) of MASTER [COST] lines were in=0 out=0, and
            # 713 "Missing required field ... signature" errors appeared app-wide — this
            # is the true root cause of the v107-110/v116/v121/v125 "Master JSON collapse"
            # (previously mis-attributed to direction-audit constraints; that is only a
            # minor secondary factor for the real-output-but-rejected subset).
            # Fix: treat 0-TextBlock output as a signature-truncation variant and retry it
            # on the same backoff schedule. `continue` here runs the finally (aclose) then
            # the for-loop's next attempt. Retries exhausted → fall through to return
            # (caller sees empty output and handles it, same as today, but now rare).
            # Condition covers BOTH empty-output variants: 0 TextBlocks (texts=[]) AND
            # empty-string TextBlocks (texts=[""] — also out=0, another face of the SDK
            # signature-truncation bug where a TextBlock carries empty text). The plain
            # `not texts` check missed the texts=[""] case ([""] is truthy). `not any
            # (... .strip())` is True iff every text is empty/whitespace, catching both.
            if not any((t or "").strip() for t in texts) and sdk_attempt < _SIGNATURE_MAX_ATTEMPTS - 1:
                _backoff = min(5 * (2 ** sdk_attempt), 30)
                if ui:
                    ui.log_history(
                        f"{role_name}: SDK stream returned 0 TextBlocks (cost={cost_usd}) — "
                        f"signature-truncation variant, retrying in {_backoff}s "
                        f"(attempt {sdk_attempt+1}/{_SIGNATURE_MAX_ATTEMPTS})",
                        "warn",
                    )
                    try:
                        import event_bus
                        event_bus.warn(
                            "pipeline.llm_empty_output_retry",
                            f"{role_name} SDK stream returned 0 TextBlocks (signature-truncation variant)",
                            role=role_name, cost=cost_usd,
                            attempt=sdk_attempt + 1, max_attempts=_SIGNATURE_MAX_ATTEMPTS,
                        )
                    except Exception:
                        pass
                await asyncio.sleep(_backoff)
                continue
            # 自适应并发:正常完成上报成功;若 output 含限速标记(529/429/503熔断)上报失败→降并发
            try:
                from api_concurrency import record_llm_outcome
                _joined = "".join(texts or "")
                if (_is_rate_limited(_joined) or _is_quota_exceeded(_joined)
                        or ("所有供应商" in _joined and "熔断" in _joined)):
                    # root-cause-audit 2026-06-21: 删 "503" in _joined[:200] 裸子串——绕过
                    # _is_rate_limited 的 2000-char guard，误匹配正常输出(筹码 -8503/版本号/对手名)。
                    # 真实 API 503 走下方 ClaudeSDKError 异常路径的 "503" in _es 检测。
                    record_llm_outcome(success=False, rate_limited=True)
                else:
                    record_llm_outcome(success=True)
            except Exception:
                pass
            return texts, total_cost, total_usage
        except ClaudeSDKError as e:
            last_sdk_err = e
            err_str = str(e).lower()
            if ("signature" in err_str or "missing required field" in err_str) and \
                    sdk_attempt < _SIGNATURE_MAX_ATTEMPTS - 1:
                # Exponential-ish backoff: 5, 10, 20, 30s — short enough to not stall
                # the pipeline, long enough for a transient SDK state to clear.
                _backoff = min(5 * (2 ** sdk_attempt), 30)
                if ui:
                    ui.log_history(
                        f"{role_name}: SDK stream error (attempt {sdk_attempt+1}/{_SIGNATURE_MAX_ATTEMPTS}), "
                        f"retrying in {_backoff}s: {e}",
                        "warn",
                    )
                _emit_llm_event(
                    "pipeline.llm_role_signature_retry", "warn",
                    f"{role_name}: SDK signature stream error, retrying in {_backoff}s",
                    role=role_name,
                    sdk_attempt=sdk_attempt + 1,
                    max_attempts=_SIGNATURE_MAX_ATTEMPTS,
                    backoff_sec=_backoff,
                    exception_type=type(e).__name__,
                    error=str(e)[:500],
                    **_role_log_metadata(log_file_path),
                )
                await asyncio.sleep(_backoff)
                continue
            # 自适应并发:非 signature 的 SDK error(可能含 503 熔断/overloaded/429)上报降并发
            try:
                _es = str(e).lower()
                if ("503" in _es or "overloaded" in _es or "熔断" in _es
                        or "所有供应商" in _es or "rate limit" in _es or "429" in _es):
                    from api_concurrency import record_llm_outcome
                    record_llm_outcome(success=False, rate_limited=True)
            except Exception:
                pass
            raise  # non-signature SDK error, or signature retries exhausted
        finally:
            _LLM_BILLING_RESULTS.reset(billing_token)
            # Defensive: ensure SDK generator is closed so subprocess is terminated.
            try:
                await query_gen.aclose()
            except Exception:
                pass  # suppress any aclose() errors
    if last_sdk_err is not None:
        raise last_sdk_err


async def run_claude_query(
    prompt,
    context_files,
    ui,
    role_name,
    log_file_path,
    model="sonnet",
    tools=None,
    allowed_write_dir=None,
    allowed_evidence_snapshot_dir=None,
    deny_live_prompt_evidence=False,
):
    """Run a Claude query via the Agent SDK with cost tracking and typed streaming.

    tools: list of built-in tool names (e.g. ["Bash", "Read"]) or a ToolsPreset dict.
           When None, no built-in tools are exposed to the model.
    allowed_write_dir: A1 fix (2026-06-30): when set (a pathlib.Path / str), a
           PreToolUse guard hook is installed that BLOCKS this sub-agent's
           Bash/Edit/Write from mutating anything OUTSIDE this directory.
           Workers/crossover pass their target bot dir so a rogue worker prompt
           cannot edit web/core/*.py, other bot dirs, or pipeline state (the
           orchestrator-level guard does not cover sub-agents).
    deny_live_prompt_evidence: install a read guard for mutable/global ratings,
           lessons, failures, replay manifests, official status prose, and
           other results sidecars. Bootstrap roles set this unconditionally.
    """
    call_started_at = time.time()
    from evolution_infra import (
        PROJECT_ROOT,
        MAX_PROMPT_CHARS,
        _BLOCKED_MCP_TOOLS,
        resolve_ui,
    )

    # Headless callers (official-platform certification, CLI probes, offline
    # analysis) intentionally have no dashboard. Normalize that boundary once
    # so stream, retry, cost, and error paths all receive the same UI contract.
    ui = resolve_ui(ui)

    # Cost is monitor-only unless the operator enabled a finite positive hard
    # limit in the parent process.  This system-owned check is intentionally
    # outside prompts and MCP arguments, so an LLM cannot grant itself more
    # budget or disable enforcement.  It also prevents starting another billed
    # call after an earlier parallel/sub-agent call crossed the operator limit.
    from orchestrator_cost_policy import assert_operator_cost_limit_available
    assert_operator_cost_limit_available()

    # Pre-check: if already rate-limited, wait before making any API call
    from rate_limiter import rate_limiter
    if rate_limiter.is_blocked():
        _emit_llm_event(
            "pipeline.llm_role_rate_limited_wait", "warn",
            f"{role_name}: waiting for API quota reset",
            role=role_name,
            reset_time=rate_limiter.reset_time_str(),
            **_role_log_metadata(log_file_path),
        )
        if ui:
            ui.log_history(
                f"API 配额受限，等待至 {rate_limiter.reset_time_str()}...",
                "warn",
            )
        await rate_limiter.wait_until_reset()

    prompt = _format_runtime_path_contract(PROJECT_ROOT, allowed_write_dir) + (prompt or "")

    # Build (path, content) pairs for context files
    context_parts = []
    context_chars = 0
    if context_files:
        for cf in context_files:
            if os.path.exists(cf):
                with open(cf, 'r') as f:
                    content = f.read()
                    context_chars += len(content)
                    context_parts.append((cf, content))

    # Assemble prompt with context files, smart-budgeting if needed
    if context_parts:
        ctx_section = "\n\n# Context Files:\n" + "".join(
            f"\n--- {p} ---\n{c}\n" for p, c in context_parts
        )
        full_prompt = prompt + ctx_section
        if len(full_prompt) > MAX_PROMPT_CHARS:
            # Compress context_files proportionally while keeping base prompt intact
            budget_for_files = MAX_PROMPT_CHARS - len(prompt) - 500
            if budget_for_files > 0:
                per_file = max(budget_for_files // len(context_parts), 500)
                ctx_section = "\n\n# Context Files:\n" + "".join(
                    f"\n--- {p} ---\n{_trim_to_budget(c, per_file)}\n"
                    for p, c in context_parts
                )
                full_prompt = prompt + ctx_section
            else:
                full_prompt = prompt + "\n\n[Context files omitted — prompt too long]"
            ui.log_history(f"Prompt budgeted to {len(full_prompt):,} chars (context compressed)", "warn")
    else:
        full_prompt = prompt
        if len(full_prompt) > MAX_PROMPT_CHARS:
            ui.log_history(f"Prompt too long ({len(full_prompt):,} chars), trimming...", "warn")
            full_prompt = _trim_to_budget(full_prompt, MAX_PROMPT_CHARS)

    ui.log_io(f"\n[{role_name} PROMPT]", "prompt", role_name)
    ui.log_io(prompt[:200] + "...\n[Context Attached]", "prompt", role_name)
    ui.log_io("\n[WAITING FOR CLAUDE...]\n", "prompt", role_name)

    _append_role_io(
        log_file_path,
        f"\n[{role_name} PROMPT]\n=============================\n"
        + full_prompt
        + "\n=============================\n[CLAUDE OUTPUT]\n",
    )

    # Install runtime hooks:
    # - cost guard for read-only but unbounded Bash (Master git-log stalls)
    # - write-scope guard for workers/crossover when allowed_write_dir is set
    _sub_hooks = None
    _cost_hooks = None
    if tools and any(t == "Bash" for t in (tools if isinstance(tools, list) else [])):
        _cost_hooks = _make_subagent_cost_guard(role_name)
    _write_hooks = None
    _readonly_hooks = None
    _master_evidence_hooks = None
    if allowed_write_dir is not None and tools and any(
        t in ("Bash", "Edit", "Write", "NotebookEdit") for t in (tools if isinstance(tools, list) else [])
    ):
        _write_hooks = _make_subagent_write_guard(allowed_write_dir)
    elif allowed_write_dir is None and tools and any(
        t in ("Bash", "Edit", "Write", "NotebookEdit") for t in (tools if isinstance(tools, list) else [])
    ):
        _readonly_hooks = _make_subagent_readonly_guard(role_name)
    if deny_live_prompt_evidence or str(role_name).upper().startswith("MASTER"):
        _master_evidence_hooks = _make_master_evidence_read_guard(
            role_name,
            allowed_evidence_snapshot_dir,
            deny_all_prompt_evidence=bool(deny_live_prompt_evidence),
        )
    _sub_hooks = _merge_hooks(
        _cost_hooks,
        _write_hooks,
        _readonly_hooks,
        _master_evidence_hooks,
    )
    options_kwargs = dict(
        model=model,
        permission_mode="bypassPermissions",
        cwd=str(PROJECT_ROOT),  # pok/ — workers use relative paths like bots/national_vN/
        mcp_servers={},
        strict_mcp_config=True,  # Direct sub-agents must not auto-start user/global MCP servers.
        tools=tools,
        disallowed_tools=_BLOCKED_MCP_TOOLS,
        thinking={"type": "adaptive"},
    )
    if _sub_hooks:
        options_kwargs["hooks"] = _sub_hooks
    options = ClaudeAgentOptions(**options_kwargs)

    lifecycle_fields = {
        "role": role_name,
        "model": model,
        "prompt_chars": len(prompt or ""),
        "full_prompt_chars": len(full_prompt or ""),
        "context_file_count": len(context_parts),
        "context_chars": context_chars,
        "allowed_write_dir": str(allowed_write_dir) if allowed_write_dir is not None else None,
        **_tools_metadata(tools),
        **_role_timeout_policy(role_name),
        **_role_log_metadata(log_file_path),
    }
    _emit_llm_event(
        "pipeline.llm_role_start", "info",
        f"{role_name}: LLM call started",
        startup_elapsed_sec=round(time.time() - call_started_at, 2),
        **lifecycle_fields,
    )

    # Initial query — retry transient SDK stream errors (signature field missing).
    # claude_agent_sdk 0.2.91 intermittently raises ClaudeSDKError "Missing required
    # field in assistant message: 'signature'"; a fresh query usually succeeds.
    # Without this retry, the error propagates and the calling tool either rejects
    # (critic) or skips (battle_exp), stalling the pipeline.
    try:
        full_text, cost_usd, usage = await _run_stream_with_signature_retry(
            full_prompt, options, log_file_path, ui, role_name)

        output = "\n".join(full_text)

        # Auto-retry on API rate limit (529) with exponential backoff
        if _is_rate_limited(output):
            for backoff in [30, 60, 120]:
                ui.log_history(f"API rate limited (529). Retrying in {backoff}s...", "warn")
                _emit_llm_event(
                    "pipeline.llm_role_rate_limit_retry", "warn",
                    f"{role_name}: API rate limited, retrying in {backoff}s",
                    role=role_name,
                    backoff_sec=backoff,
                    **_role_log_metadata(log_file_path),
                )
                await asyncio.sleep(backoff)
                full_text.clear()
                retry_texts, retry_cost, retry_usage = await _run_stream_with_signature_retry(
                    full_prompt, options, log_file_path, ui, role_name)
                if retry_texts:
                    full_text.extend(retry_texts)
                if retry_cost:
                    cost_usd = (cost_usd or 0) + retry_cost
                if retry_usage:
                    usage = _merge_billing_usage(usage, retry_usage)

                output = "\n".join(full_text)
                if not _is_rate_limited(output):
                    break

        # 429 quota exhaustion — parse reset time, block until reset, then retry once
        if _is_quota_exceeded(output):
            if rate_limiter.parse_429(output):
                wait = rate_limiter.wait_seconds()
                ui.log_history(
                    f"API 配额耗尽 (429)。等待 {wait:.0f}s 至 {rate_limiter.reset_time_str()}",
                    "error",
                )
                _emit_llm_event(
                    "pipeline.llm_role_quota_wait", "warn",
                    f"{role_name}: API quota exhausted, waiting {wait:.0f}s",
                    role=role_name,
                    wait_sec=round(wait, 2),
                    reset_time=rate_limiter.reset_time_str(),
                    **_role_log_metadata(log_file_path),
                )
                await rate_limiter.wait_until_reset()
                # Retry after reset
                full_text.clear()
                retry_texts, retry_cost, retry_usage = await _run_stream_with_signature_retry(
                    full_prompt, options, log_file_path, ui, role_name)
                if retry_texts:
                    full_text.extend(retry_texts)
                if retry_cost:
                    cost_usd = (cost_usd or 0) + retry_cost
                if retry_usage:
                    usage = _merge_billing_usage(usage, retry_usage)
                output = "\n".join(full_text)

        # Every completed SDK Result (including an empty-output/signature retry)
        # was already recorded and UI-projected inside
        # _run_stream_with_signature_retry.  Re-check here only to cover a
        # concurrent sibling that crossed the operator threshold meanwhile.
        from orchestrator_cost_policy import assert_operator_cost_limit_available
        assert_operator_cost_limit_available()
        _emit_llm_event(
            "pipeline.llm_role_done", "success",
            f"{role_name}: LLM call finished in {time.time() - call_started_at:.1f}s",
            elapsed_sec=round(time.time() - call_started_at, 2),
            cost_usd=round(cost_usd, 6) if cost_usd is not None else None,
            output_chars=len(output or ""),
            text_block_count=len(full_text or []),
            **_usage_metadata(usage),
            **lifecycle_fields,
        )
        return output, cost_usd, usage
    except asyncio.CancelledError:
        is_shutdown = _is_shutdown_requested()
        _category, _severity, _cancel_fields = _cancelled_event(
            "pipeline.llm_role_cancelled",
            "pipeline.llm_role_parent_timeout_cancelled",
        )
        if is_shutdown:
            _category = "pipeline.llm_role_shutdown_cancelled"
            _severity = "info"
            _message = f"{role_name}: LLM call stopped during shutdown after {time.time() - call_started_at:.1f}s"
        elif _cancel_fields.get("cancel_reason") == "parent_timeout":
            _scope = _cancel_fields.get("cancel_scope")
            _timeout = _cancel_fields.get("timeout_sec")
            _message = (
                f"{role_name}: LLM call cancelled by parent timeout after {time.time() - call_started_at:.1f}s"
                f" ({_scope}, {_timeout:g}s)"
                if isinstance(_timeout, (int, float))
                else f"{role_name}: LLM call cancelled by parent timeout after {time.time() - call_started_at:.1f}s"
                f" ({_scope})"
            )
        else:
            _message = f"{role_name}: LLM call cancelled after {time.time() - call_started_at:.1f}s"
        _emit_llm_event(
            _category,
            _severity,
            _message,
            elapsed_sec=round(time.time() - call_started_at, 2),
            **_cancel_fields,
            **lifecycle_fields,
        )
        raise
    except Exception as e:
        if is_shutdown_cancel_error(e):
            shutdown_requested = _is_shutdown_requested()
            event_type = (
                "pipeline.llm_role_shutdown_cancelled"
                if shutdown_requested
                else "pipeline.llm_role_process_terminated"
            )
            _emit_llm_event(
                event_type,
                "info" if shutdown_requested else "warn",
                (
                    f"{role_name}: LLM call stopped during shutdown after {time.time() - call_started_at:.1f}s"
                    if shutdown_requested
                    else f"{role_name}: LLM process received SIGTERM after {time.time() - call_started_at:.1f}s"
                ),
                elapsed_sec=round(time.time() - call_started_at, 2),
                exception_type=type(e).__name__,
                error=str(e)[:1000],
                shutdown_requested=shutdown_requested,
                **lifecycle_fields,
            )
            raise asyncio.CancelledError(
                f"{role_name}: LLM process received SIGTERM"
            ) from e
        severity = _llm_failure_severity(e)
        _emit_llm_event(
            "pipeline.llm_role_failed", severity,
            f"{role_name}: LLM call failed after {time.time() - call_started_at:.1f}s: {str(e)[:180]}",
            elapsed_sec=round(time.time() - call_started_at, 2),
            exception_type=type(e).__name__,
            error=str(e)[:1000],
            **lifecycle_fields,
        )
        raise


def parse_json_output(output):
    # Strategy 1: Find ALL ```json blocks, try from LAST to first.
    # Handles the case where the LLM references the prompt template before the actual plan.
    json_starts = list(re.finditer(r'```json\s*', output))
    for json_start in reversed(json_starts):
        after_start = output[json_start.end():]
        # Find all ``` positions after ```json
        close_positions = [m.start() for m in re.finditer(r'```', after_start)]
        # Try from the LAST ``` backward (most likely the actual closing)
        for pos in reversed(close_positions):
            candidate = after_start[:pos].strip()
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
        # Also try the full text after ```json (in case no closing ```)
        try:
            return json.loads(after_start.strip().rstrip('`').strip())
        except json.JSONDecodeError:
            pass

    # Strategy 1.5: Brace-matching from each ```json start.
    # Handles embedded ``` inside JSON string values (e.g., worker_prompt with code blocks).
    # Tracks string boundaries so ``` inside strings are ignored.
    for json_start in reversed(json_starts):
        after_start = output[json_start.end():]
        brace_pos = after_start.find('{')
        if brace_pos == -1:
            continue
        depth = 0
        in_string = False
        escape_next = False
        for i in range(brace_pos, len(after_start)):
            c = after_start[i]
            if escape_next:
                escape_next = False
                continue
            if c == '\\' and in_string:
                escape_next = True
                continue
            if c == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    candidate = after_start[brace_pos:i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break  # brace match failed, try next ```json block

    # Strategy 2: Try the whole output as raw JSON
    try:
        return json.loads(output)
    except Exception:
        pass
    return None


def parse_json_output_with_mode(output):
    """Same parsing as parse_json_output, but returns a classifiable failure mode.

    Returns ``(data, failure_mode)`` where ``failure_mode`` is one of:
      - ``"OK"``          — parsed successfully (data is the dict)
      - ``"NO_JSON"``     — output empty/whitespace (no text to parse at all)
      - ``"NO_FENCE"``    — output has text but no JSON structure (no ```json
                            block and no ``{``); the model never emitted JSON
      - ``"PARSE_ERROR"`` — output looked like JSON (had a fence or brace) but
                            every parse strategy failed

    The mode lets callers (notably _run_master_analysis) log a CLASSIFIABLE
    reason instead of the undifferentiated "malformed JSON" that previously
    hid three distinct root causes (missing-return / NO_FENCE / empty-output).
    """
    if not output or not output.strip():
        return None, "NO_JSON"
    data = parse_json_output(output)
    if data is not None:
        return data, "OK"
    # parse_json_output exhausted every strategy — distinguish why.
    has_fence = "```json" in output
    has_brace = "{" in output
    if has_fence or has_brace:
        return None, "PARSE_ERROR"
    return None, "NO_FENCE"
