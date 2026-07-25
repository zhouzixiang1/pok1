"""LLM sub-agent guard hooks and shell command parsing.

Extracted from llm_query.py for maintainability. Contains:
- Shell command parsing (git/bash mutation detectors, heredoc tokenizers)
- Path scope resolvers (allowed read/write scope validation)
- Guard hook factories (_make_subagent_*_guard)
- Runtime path contract formatting

All symbols are re-exported by llm_query.py via __all__, so existing
`from llm_query import _make_subagent_*` imports continue to work.
"""

import contextlib
import contextvars
import fnmatch
import os
import re
import shlex
from pathlib import Path
from urllib.parse import unquote, urlsplit

from bot_namespace import ACTIVE_BOT_PREFIX


_LLM_CANCEL_CONTEXT = contextvars.ContextVar("llm_cancel_context", default=None)
_LLM_TOOL_TRACE = contextvars.ContextVar("llm_tool_trace", default=None)


def _patched_llm_query_helper(name, local_impl):
    """Return the ``llm_query.<name>`` override if tests patched it.

    Tests and operators routinely ``monkeypatch.setattr(llm_query, name, ...)``
    to inject deterministic behavior.  After this module was extracted from
    ``llm_query``, bare-name lookups inside guard closures would otherwise
    bypass those patches because Python resolves them against this module's
    globals rather than ``llm_query``'s.

    Each guard helper that tests patch wraps its real implementation with a
    call to this resolver: if ``llm_query.<name>`` is bound to a different
    object than the local implementation, the override wins.  Otherwise the
    real implementation runs unchanged.
    """

    try:
        import llm_query as _llm_query_module

        override = getattr(_llm_query_module, name, None)
        if override is not None and override is not local_impl:
            return override
    except Exception:
        pass
    return None


@contextlib.contextmanager
def capture_llm_tool_trace():
    """Capture typed SDK tool-use/result events for the current async context.

    Normal role callers pay no tracing cost beyond one context lookup.  The
    operator SDK probe uses this scope to prove that the production streaming
    path really executed its required tools; parsing the human role log is not
    an execution receipt.
    """

    events = []
    token = _LLM_TOOL_TRACE.set(events)
    try:
        yield events
    finally:
        _LLM_TOOL_TRACE.reset(token)


def _record_llm_tool_trace_event(event):
    trace = _LLM_TOOL_TRACE.get()
    if not isinstance(trace, list):
        return
    payload = dict(event or {})
    payload["sequence"] = len(trace) + 1
    trace.append(payload)


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
_LLM_AVAILABILITY_CONTROL_MARKERS = (
    "pok_llm_resume_evidence_digest",
    "llm_availability_store",
    "llm_availability_pause.json",
    "reconcile_llm_pause",
    "resume_llm_pause",
    "consume_operator_resume_ack",
    "persist_llm_pause",
    "strict_authority_workflow",
    "strict-role-accepted",
    "accept_role_result",
    "dispatch_call",
    "complete_provider_call",
    "strictroleaccepted",
    "strictproviderresultobserved",
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
    override = _patched_llm_query_helper("_project_root_for_guard", _project_root_for_guard)
    if override is not None:
        return override()

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


def _normalize_allowed_read_scope(allowed_read_dirs):
    """Normalize explicit filesystem read authority.

    ``allowed_read_dirs`` historically meant "results paths exempt from the
    live-evidence marker".  That was not a read boundary: a role could still
    open every bot and the repository's Git history.  The same public argument
    now carries real capability semantics.  A scalar or sequence names
    directory roots; callers that need exact files may pass
    ``{"files": [...], "dirs": [...]}``.
    """

    dirs = []
    files = []
    raw = allowed_read_dirs
    if isinstance(raw, dict):
        dirs.extend(raw.get("dirs") or raw.get("directories") or [])
        files.extend(raw.get("files") or raw.get("paths") or [])
    elif isinstance(raw, (list, tuple, set)):
        dirs.extend(raw)
    elif raw is not None:
        dirs.append(raw)

    def _resolved(items):
        values = []
        for item in items:
            if item is None or not str(item).strip():
                continue
            try:
                values.append(
                    str(Path(_local_path_from_file_uri(item)).resolve(strict=False))
                )
            except Exception:
                # An unresolvable grant must not become an imprecise string
                # prefix.  Drop it; the eventual access will fail closed.
                continue
        return list(dict.fromkeys(values))

    return {"dirs": _resolved(dirs), "files": _resolved(files)}


def _merge_allowed_read_scopes(*scopes):
    merged = {"dirs": [], "files": []}
    for scope in scopes:
        normalized = _normalize_allowed_read_scope(scope)
        merged["dirs"].extend(normalized.get("dirs") or ())
        merged["files"].extend(normalized.get("files") or ())
    merged["dirs"] = list(dict.fromkeys(merged["dirs"]))
    merged["files"] = list(dict.fromkeys(merged["files"]))
    return merged


def _path_has_symlink_component(path):
    """Return true for existing or dangling symlinks in any path component."""

    try:
        candidate = Path(path)
        current = Path(candidate.anchor) if candidate.is_absolute() else Path()
        for part in candidate.parts:
            if candidate.is_absolute() and part == candidate.anchor:
                continue
            current = current / part
            if current.is_symlink():
                return True
    except (OSError, RuntimeError, ValueError):
        return True
    return False


def _path_parts_lower(path):
    try:
        return tuple(part.lower() for part in Path(path).parts)
    except Exception:
        return ()


def _bot_anchor(path):
    """Return ``.../bots/national_vN`` for an active-bot path, if present."""

    candidate = Path(path)
    parts = candidate.parts
    for index, part in enumerate(parts[:-1]):
        if part.lower() != "bots":
            continue
        name = parts[index + 1].lower()
        if re.fullmatch(rf"{re.escape(ACTIVE_BOT_PREFIX.lower())}\d+", name):
            return Path(*parts[: index + 2])
    return None


def _scope_sensitive_roots(allowed_scope, kind):
    """Derive narrow sensitive roots explicitly named by the capability."""

    roots = []
    for raw in (
        *(allowed_scope.get("dirs") or ()),
        *(allowed_scope.get("files") or ()),
    ):
        candidate = Path(raw)
        if kind == "bot":
            anchor = _bot_anchor(candidate)
            if anchor is not None:
                roots.append(anchor.resolve(strict=False))
            continue
        parts = _path_parts_lower(candidate)
        if "results" not in parts:
            continue
        results_index = parts.index("results")
        # A grant of the mutable results root is too broad to be evidence
        # authority.  Exact snapshots/workspaces below it are permitted.
        if len(parts) <= results_index + 1:
            continue
        roots.append(candidate.resolve(strict=False))
    return tuple(dict.fromkeys(roots))


def _resolved_within(path, root):
    try:
        Path(path).resolve(strict=False).relative_to(Path(root).resolve(strict=False))
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _read_path_violation(raw_path, allowed_scope, *, base_dir=None):
    """Return a typed denial for a single filesystem read target."""

    text = _local_path_from_file_uri(raw_path)
    text = _strip_shell_grouping_parens(str(text or "").strip().strip("'\""))
    if not text:
        return "empty_path"
    if text == "-" or text.lower() in _SAFE_REDIRECT_TARGETS:
        return None
    if "\x00" in text:
        return "nul_path"
    if text.startswith("~"):
        return f"home_alias:{text[:120]}"
    if any(marker in text for marker in ("$", "`", "*", "?", "[", "]", "{", "}")):
        return f"dynamic_path:{text[:120]}"

    try:
        lexical = Path(text)
        if ".." in lexical.parts:
            return f"parent_alias:{text[:120]}"
        if not lexical.is_absolute():
            base = (
                Path(base_dir).resolve(strict=False)
                if base_dir is not None
                else _project_root_for_guard()
            )
            lexical = base / lexical
        lexical = lexical.absolute()
        if _path_has_symlink_component(lexical):
            return f"symlink_path:{text[:120]}"
        resolved = lexical.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        return f"unresolvable_path:{type(exc).__name__}:{text[:100]}"

    lowered_parts = _path_parts_lower(resolved)
    if ".git" in lowered_parts:
        return f"git_metadata_forbidden:{resolved}"
    if any(part in {"archive", ".archive"} for part in lowered_parts):
        return f"archived_tree_forbidden:{resolved}"

    project_root = _project_root_for_guard()
    if not _resolved_within(resolved, project_root):
        return f"outside_active_checkout:{resolved}"

    bot_root = _bot_anchor(resolved)
    if bot_root is not None:
        authorized_bots = _scope_sensitive_roots(allowed_scope, "bot")
        if not any(bot_root.resolve(strict=False) == root for root in authorized_bots):
            return f"bot_not_in_role_scope:{resolved}"

    if "results" in lowered_parts:
        authorized_results = _scope_sensitive_roots(allowed_scope, "results")
        if not any(_resolved_within(resolved, root) for root in authorized_results):
            return f"results_not_in_role_scope:{resolved}"

    if not _path_inside_allowed_scope(resolved, allowed_scope):
        return f"outside_role_read_scope:{resolved}"
    return None


def _shell_has_dynamic_expansion(command):
    """Detect expansions whose eventual filesystem targets are unknowable."""

    text = str(command or "")
    quote = None
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and quote != "'":
            escaped = True
            index += 1
            continue
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if quote == '"':
            if char == '"':
                quote = None
                index += 1
                continue
            if char in {"$", "`"}:
                return True
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if char in {"$", "`"}:
            return True
        if text.startswith("<( ", index) or text.startswith(">(", index):
            return True
        index += 1
    return quote is not None


def _iter_shell_read_redirect_targets(command):
    """Yield simple ``< file`` targets; complex redirections are rejected above."""

    text = _strip_shell_comments(str(command or ""))
    quote = None
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\":
            escaped = True
            index += 1
            continue
        if quote:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            index += 1
            continue
        if char != "<":
            index += 1
            continue
        if index + 1 < len(text) and text[index + 1] in {"<", ">", "&", "("}:
            index += 2
            continue
        cursor = index + 1
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        target, end = _read_shell_token(text, cursor, extra_stop_chars="()")
        if target:
            yield target
        index = max(end, cursor + 1)


_READ_COMMAND_OPTIONS_WITH_VALUE = {
    "head": {"-n", "--lines", "-c", "--bytes"},
    "tail": {"-n", "--lines", "-c", "--bytes", "--pid", "-s", "--sleep-interval"},
    "grep": {
        "-A", "--after-context", "-B", "--before-context", "-C", "--context",
        "-m", "--max-count", "--exclude", "--exclude-from", "--exclude-dir",
        "--include", "--label", "--binary-files", "-D", "--devices", "-d",
        "--directories",
    },
    "rg": {
        "-A", "--after-context", "-B", "--before-context", "-C", "--context",
        "-g", "--glob", "-t", "--type", "-T", "--type-not", "--type-add",
        "-m", "--max-count", "--max-depth", "--max-filesize", "--sort",
        "--sortr", "--encoding", "--engine", "--path-separator", "--iglob",
        "--ignore-file", "--color", "--colors", "--context-separator",
        "--field-context-separator", "--field-match-separator", "-r", "--replace",
    },
    "diff": {"--exclude", "--exclude-from", "--label", "--starting-file", "-I"},
}

_READ_PATH_OPTIONS = {
    "diff": {"--exclude-from", "--from-file", "--to-file"},
    "grep": {"--exclude-from"},
    "rg": {"--ignore-file"},
    "wc": {"--files0-from"},
}

_SAFE_INLINE_VALUE_OPTIONS = {
    "diff": {"--exclude", "--label", "--starting-file", "--unified"},
    "grep": {
        "--after-context", "--before-context", "--binary-files", "--context",
        "--devices", "--directories", "--exclude", "--exclude-dir", "--include",
        "--label", "--max-count",
    },
    "head": {"--bytes", "--lines"},
    "rg": {
        "--after-context", "--before-context", "--color", "--colors", "--context",
        "--context-separator", "--encoding", "--engine", "--field-context-separator",
        "--field-match-separator", "--glob", "--iglob", "--max-count",
        "--max-depth", "--max-filesize", "--path-separator", "--replace", "--sort",
        "--sortr", "--type", "--type-add", "--type-not",
    },
    "tail": {"--bytes", "--lines", "--pid", "--sleep-interval"},
}


def _option_consumes_value(command_name, arg):
    options = _READ_COMMAND_OPTIONS_WITH_VALUE.get(command_name, set())
    if arg in options:
        return True
    return False


def _inline_option(command_name, arg):
    if not str(arg).startswith("--") or "=" not in str(arg):
        return None, None
    name, value = str(arg).split("=", 1)
    if name in _READ_PATH_OPTIONS.get(command_name, set()):
        return "path", value
    if name in _SAFE_INLINE_VALUE_OPTIONS.get(command_name, set()):
        return "safe", value
    return "unknown", value


def _option_read_targets(command_name, args):
    targets = []
    index = 0
    path_options = _READ_PATH_OPTIONS.get(command_name, set())
    while index < len(args):
        arg = str(args[index])
        inline_kind, inline_value = _inline_option(command_name, arg)
        if inline_kind == "path":
            targets.append(inline_value)
        if arg in path_options:
            if index + 1 >= len(args):
                raise ValueError(f"missing_option_value:{arg}")
            targets.append(str(args[index + 1]))
            index += 1
        index += 1
    return targets


def _command_positionals(command_name, args):
    """Return positionals while rejecting ambiguous/unbounded command options."""

    positionals = []
    index = 0
    while index < len(args):
        arg = str(args[index])
        low = arg.lower()
        if arg == "--":
            positionals.extend(str(item) for item in args[index + 1 :])
            break
        if command_name in {"rg", "grep"} and low in {"-l", "--follow", "-r", "--dereference-recursive"}:
            # rg -L means follow; grep -R follows.  Lower-casing deliberately
            # treats either spelling conservatively.
            if (command_name == "rg" and arg in {"-L", "--follow"}) or (
                command_name == "grep" and arg in {"-R", "--dereference-recursive"}
            ):
                raise ValueError(f"symlink_follow_option:{arg}")
        if command_name == "rg" and (low == "--pre" or low.startswith("--pre=")):
            raise ValueError("rg_preprocessor_forbidden")
        if arg.startswith("-") and arg != "-":
            inline_kind, _inline_value = _inline_option(command_name, arg)
            if inline_kind == "unknown":
                raise ValueError(f"unproved_inline_option:{arg.split('=', 1)[0]}")
            if inline_kind is not None:
                index += 1
                continue
            if _option_consumes_value(command_name, arg):
                if index + 1 >= len(args):
                    raise ValueError(f"missing_option_value:{arg}")
                index += 2
                continue
            # Common compact numeric forms (-n20, -C3) are self-contained.
            index += 1
            continue
        positionals.append(arg)
        index += 1
    return positionals


def _grep_like_read_targets(command_name, args):
    """Extract pattern-file and search-root reads for grep/rg."""

    targets = _option_read_targets(command_name, args)
    positionals = []
    pattern_from_option = False
    index = 0
    while index < len(args):
        arg = str(args[index])
        low = arg.lower()
        if arg == "--":
            positionals.extend(str(item) for item in args[index + 1 :])
            break
        if (command_name == "rg" and arg in {"-L", "--follow"}) or (
            command_name == "grep" and arg in {"-R", "--dereference-recursive"}
        ):
            raise ValueError(f"symlink_follow_option:{arg}")
        if command_name == "rg" and (low == "--pre" or low.startswith("--pre=")):
            raise ValueError("rg_preprocessor_forbidden")
        if arg in {"-e", "--regexp"}:
            if index + 1 >= len(args):
                raise ValueError(f"missing_option_value:{arg}")
            pattern_from_option = True
            index += 2
            continue
        if arg in {"-f", "--file"}:
            if index + 1 >= len(args):
                raise ValueError(f"missing_option_value:{arg}")
            targets.append(str(args[index + 1]))
            pattern_from_option = True
            index += 2
            continue
        if arg.startswith("--regexp="):
            pattern_from_option = True
            index += 1
            continue
        if arg.startswith("--file="):
            targets.append(arg.split("=", 1)[1])
            pattern_from_option = True
            index += 1
            continue
        if arg.startswith("-") and arg != "-":
            inline_kind, inline_value = _inline_option(command_name, arg)
            if inline_kind == "path":
                targets.append(inline_value)
                index += 1
                continue
            if inline_kind == "unknown":
                raise ValueError(f"unproved_inline_option:{arg.split('=', 1)[0]}")
            if inline_kind is not None:
                index += 1
                continue
            if _option_consumes_value(command_name, arg):
                if index + 1 >= len(args):
                    raise ValueError(f"missing_option_value:{arg}")
                index += 2
                continue
            index += 1
            continue
        positionals.append(arg)
        index += 1
    if not pattern_from_option and positionals:
        positionals.pop(0)
    targets.extend(positionals)
    if command_name == "rg" and not targets:
        # ripgrep searches the process cwd when no path is supplied.  Treat
        # that implicit directory exactly like an explicit ``.``.
        targets.append(".")
    return targets


def _sed_read_targets(args):
    targets = []
    positionals = []
    script_from_option = False
    index = 0
    while index < len(args):
        arg = str(args[index])
        if arg == "--":
            positionals.extend(str(item) for item in args[index + 1 :])
            break
        if arg in {"-e", "--expression"}:
            if index + 1 >= len(args):
                raise ValueError(f"missing_option_value:{arg}")
            script_from_option = True
            index += 2
            continue
        if arg in {"-f", "--file"}:
            if index + 1 >= len(args):
                raise ValueError(f"missing_option_value:{arg}")
            targets.append(str(args[index + 1]))
            script_from_option = True
            index += 2
            continue
        if arg.startswith("--expression="):
            script_from_option = True
            index += 1
            continue
        if arg.startswith("--file="):
            targets.append(arg.split("=", 1)[1])
            script_from_option = True
            index += 1
            continue
        if arg == "-i" or arg.startswith("-i") or arg == "--in-place" or arg.startswith("--in-place="):
            raise ValueError("sed_in_place_forbidden")
        if arg.startswith("-") and arg != "-":
            if "=" in arg:
                raise ValueError(f"unproved_inline_option:{arg.split('=', 1)[0]}")
            index += 1
            continue
        positionals.append(arg)
        index += 1
    if not script_from_option and positionals:
        positionals.pop(0)
    targets.extend(positionals)
    return targets


def _python_pycompile_targets(args):
    """Permit Python only as a non-executing compiler over explicit files."""

    values = [str(arg) for arg in args]
    index = 0
    while index < len(values) and values[index] in {"-B", "-I", "-S", "-E", "-s", "-q"}:
        index += 1
    if index + 1 >= len(values) or values[index] != "-m" or values[index + 1] != "py_compile":
        raise ValueError("python_execution_not_read_provable")
    targets = values[index + 2 :]
    if not targets or any(target.startswith("-") for target in targets):
        raise ValueError("py_compile_requires_explicit_files")
    return targets


def _git_no_index_diff_targets(args):
    if any(
        str(arg) in _SUBAGENT_GIT_GLOBAL_OPTIONS_WITH_VALUE
        or any(
            str(arg).startswith(option + "=")
            for option in _SUBAGENT_GIT_GLOBAL_OPTIONS_WITH_VALUE
        )
        for arg in args
    ):
        raise ValueError("git_global_path_or_config_forbidden")
    subcmd, rest = _subagent_git_subcommand(args)
    if subcmd != "diff" or "--no-index" not in rest:
        raise ValueError("git_repository_read_forbidden")
    safe_flags = {
        "--no-index", "--", "-u", "--patch", "--stat", "--numstat",
        "--shortstat", "--name-only", "--name-status", "--no-color",
        "--word-diff", "--exit-code", "--quiet", "--text", "-a",
    }
    for arg in rest:
        value = str(arg)
        if not value.startswith("-") or value == "-":
            continue
        if value in safe_flags or re.fullmatch(r"-U\d+", value):
            continue
        if value.startswith("--unified=") and value.split("=", 1)[1].isdigit():
            continue
        if value in {"--color=never", "--word-diff=plain", "--word-diff=porcelain"}:
            continue
        raise ValueError(f"git_no_index_option_forbidden:{value}")
    positionals = _command_positionals("diff", rest)
    positionals = [item for item in positionals if item != "--no-index"]
    if len(positionals) != 2:
        raise ValueError("git_no_index_diff_requires_two_paths")
    return positionals


def _bash_segment_read_targets(segment, *, current_dir, depth):
    words = _simple_command_words(segment)
    if not words:
        return [], current_dir
    command_token = _strip_shell_grouping_parens(words[0])
    command_name = _command_name(command_token)
    args = [_strip_shell_grouping_parens(arg) for arg in words[1:]]
    if "/" in command_token or "\\" in command_token:
        executable = Path(command_token)
        if not executable.is_absolute():
            raise ValueError("relative_executable_forbidden")
        if executable.parent not in {Path("/bin"), Path("/usr/bin")}:
            raise ValueError("untrusted_executable_path")

    if command_name in {"pwd", "true", "false", ":", "echo", "printf"}:
        return [], current_dir
    if command_name == "cd":
        target = _cd_target_from_args(args)
        if not target:
            raise ValueError("cd_requires_explicit_path")
        return [target], _resolve_guard_path(target, base_dir=current_dir)
    if command_name in {"bash", "dash", "sh", "zsh"}:
        # The child shell can reinterpret quoting, positional parameters,
        # startup files, aliases, and its own cwd.  A static parent hook cannot
        # prove the final read set, so wrappers are intentionally unavailable.
        raise ValueError("shell_wrapper_forbidden")
    if command_name.startswith("python"):
        return _python_pycompile_targets(args), current_dir
    if command_name == "git":
        return _git_no_index_diff_targets(args), current_dir
    if command_name in {"rg", "grep"}:
        return _grep_like_read_targets(command_name, args), current_dir
    if command_name == "sed":
        return _sed_read_targets(args), current_dir
    if command_name in {"cat", "nl", "wc", "sha256sum", "md5sum", "file", "stat"}:
        return (
            _option_read_targets(command_name, args)
            + _command_positionals(command_name, args)
        ), current_dir
    if command_name in {"head", "tail"}:
        return _command_positionals(command_name, args), current_dir
    if command_name in {"ls", "tree", "du"}:
        targets = _command_positionals(command_name, args)
        return (targets or ["."]), current_dir
    if command_name in {"diff", "cmp"}:
        targets = _option_read_targets("diff", args) + _command_positionals("diff", args)
        required = 2
        if len(targets) != required:
            raise ValueError(f"{command_name}_requires_two_paths")
        return targets, current_dir
    # Filter-only pipeline stages may consume stdin, but direct file operands
    # are too command-specific to infer safely.
    if command_name in {"sort", "uniq", "tr"}:
        if any(
            str(arg) in {"-o", "--output"}
            or str(arg).startswith("--output=")
            for arg in args
        ):
            raise ValueError(f"{command_name}_output_forbidden")
        positionals = _command_positionals(command_name, args)
        if command_name in {"sort", "uniq"} and positionals:
            raise ValueError(f"{command_name}_file_operand_forbidden")
        return [], current_dir
    raise ValueError(f"bash_command_not_read_provable:{command_name or 'empty'}")


def _subagent_bash_read_scope_violation(
    command,
    allowed_scope,
    *,
    initial_dir=None,
    depth=0,
    return_targets=False,
):
    """Fail closed unless every Bash filesystem read is statically authorized."""

    text = str(command or "")
    if not text.strip():
        return "empty_bash_command"
    if _shell_heredoc_delimiters(text) or "<<<" in text or "<(" in text or ">(" in text:
        return "complex_shell_input_forbidden"
    if _shell_has_dynamic_expansion(text):
        return "dynamic_shell_expansion_forbidden"
    try:
        for segment in _split_shell_simple_commands(text):
            raw_words = _shell_words(segment)
            if raw_words and _command_name(raw_words[0]) == "env":
                raise ValueError("env_wrapper_forbidden")
            for word in raw_words:
                if _is_shell_assignment(word):
                    name = str(word).split("=", 1)[0].upper()
                    if name not in {"LC_ALL", "LANG", "LANGUAGE", "TZ"}:
                        raise ValueError(f"shell_assignment_forbidden:{name}")
        current_dir = str(
            Path(initial_dir).resolve(strict=False)
            if initial_dir is not None
            else _project_root_for_guard()
        )
        targets = []
        for redirect in _iter_shell_read_redirect_targets(text):
            targets.append((redirect, current_dir))
        for segment in _split_shell_simple_commands(text):
            segment_targets, next_dir = _bash_segment_read_targets(
                segment,
                current_dir=current_dir,
                depth=depth,
            )
            targets.extend((target, current_dir) for target in segment_targets)
            current_dir = next_dir
        if return_targets:
            return tuple(target for target, _base in targets)
        for target, base in targets:
            violation = _read_path_violation(target, allowed_scope, base_dir=base)
            if violation:
                return violation
        return None
    except Exception as exc:
        return f"unprovable_bash_read:{type(exc).__name__}:{str(exc)[:180]}"


def _subagent_read_scope_violation(tool_name, tool_input, allowed_scope):
    override = _patched_llm_query_helper(
        "_subagent_read_scope_violation", _subagent_read_scope_violation
    )
    if override is not None:
        return override(tool_name, tool_input, allowed_scope)

    if not isinstance(tool_input, dict):
        return "malformed_tool_input"
    if tool_name == "Read":
        supplied = [
            tool_input.get(key)
            for key in ("file_path", "path")
            if tool_input.get(key) is not None
        ]
        if not supplied:
            return "read_path_missing"
        for path in supplied:
            violation = _read_path_violation(path, allowed_scope)
            if violation:
                return violation
        return None
    if tool_name == "Bash":
        return _subagent_bash_read_scope_violation(
            tool_input.get("command", ""),
            allowed_scope,
        )
    return None


def _make_subagent_read_scope_guard(role_name, allowed_read_scope):
    """Build the mandatory role-scoped Read/Bash capability hook."""

    scope = _normalize_allowed_read_scope(allowed_read_scope)

    async def handler(hook_input, tool_use_id, context):
        from claude_agent_sdk.types import SyncHookJSONOutput

        try:
            if not isinstance(hook_input, dict):
                raise ValueError("hook_input_not_object")
            tool_name = hook_input.get("tool_name", "")
            if tool_name not in {"Read", "Bash"}:
                raise ValueError("unexpected_read_guard_tool")
            tool_input = hook_input.get("tool_input")
            if not isinstance(tool_input, dict):
                raise ValueError("tool_input_not_object")
            violation = _subagent_read_scope_violation(
                tool_name,
                tool_input,
                scope,
            )
        except Exception as exc:
            violation = f"read_guard_internal_error:{type(exc).__name__}"
        if not violation:
            return SyncHookJSONOutput()
        reason = (
            f"{role_name} filesystem read denied by the role-scoped capability "
            f"guard ({violation}). Read only the exact candidate/source/snapshot "
            "roots supplied by the system; repository history and indirect shell "
            "read programs are unavailable."
        )
        try:
            from system_log import log_system_event

            log_system_event(
                "pipeline.subagent_read_scope_guard_block",
                "error",
                f"BLOCKED {role_name} {tool_name} read: {violation}",
                {
                    "role": role_name,
                    "tool": tool_name,
                    "reason": violation,
                    "tool_use_id": str(tool_use_id)[:64],
                    "allowed_dirs": list(scope.get("dirs") or ()),
                    "allowed_files": list(scope.get("files") or ()),
                },
            )
        except Exception:
            pass
        return SyncHookJSONOutput(hookSpecificOutput={
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        })

    try:
        from claude_agent_sdk.types import HookMatcher

        return {"PreToolUse": [
            HookMatcher(matcher="Read", hooks=[handler]),
            HookMatcher(matcher="Bash", hooks=[handler]),
        ]}
    except Exception:
        return None


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
    override = _patched_llm_query_helper(
        "_subagent_bash_write_scope_violation", _subagent_bash_write_scope_violation
    )
    if override is not None:
        return override(command, allowed_dir)

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


def _role_bash_read_violation(role_name, command):
    """Return role-specific read denials that generic cost guards cannot allow."""

    role = str(role_name or "").strip().upper()
    if not (
        role == "STRATEGY CRITIC" or role.startswith("STRATEGY CRITIC ")
    ):
        return None
    git_args = list(_iter_subagent_git_args(command))
    for segment in _split_shell_simple_commands(command):
        words = _simple_command_words(segment)
        if not words or _command_name(words[0]) not in {
            "bash", "dash", "sh", "zsh"
        }:
            continue
        for index, arg in enumerate(words[1:], start=1):
            low = str(arg).lower()
            if low == "-c" or (
                low.startswith("-") and "c" in low[1:]
            ):
                if index + 1 < len(words):
                    git_args.extend(
                        _iter_subagent_git_args(words[index + 1])
                    )
                break
    for args in git_args:
        subcmd, _rest = _subagent_git_subcommand(args)
        if subcmd in {"log", "show", "rev-list"}:
            return f"strategy_critic_git_history_forbidden:{subcmd}"
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
            role_violation = _role_bash_read_violation(role_name, cmd)
            reason = role_violation or _subagent_bash_cost_detector(cmd)
            if not reason:
                return SyncHookJSONOutput()
            if role_violation:
                blocked = (
                    f"Bash command denied by STRATEGY CRITIC evidence guard "
                    f"({reason}). Git history is not admissible critic evidence; "
                    "use only the system-supplied frozen envelope, exact "
                    "parent-to-target diff, Read, rg, or direct diff. Command: "
                    + str(cmd)[:180]
                )
            else:
                blocked = (
                    f"Bash command denied by runtime cost guard ({reason}). Use bounded "
                    "inspection only: rg/sed/head/tail or git log with --max-count <= 20 "
                    "or an explicit revision range. Command: " + str(cmd)[:180]
                )
            try:
                from system_log import log_system_event
                log_system_event(
                    (
                        "pipeline.subagent_role_evidence_guard_block"
                        if role_violation
                        else "pipeline.subagent_cost_guard_block"
                    ),
                    "error" if role_violation else "warn",
                    (
                        f"BLOCKED inadmissible {role_name} Git history: {reason}"
                        if role_violation
                        else f"BLOCKED high-cost {role_name} Bash: {reason}"
                    ),
                    {
                        "role": role_name,
                        "tool": tool_name,
                        "reason": reason,
                        "recoverable": True,
                        "next_action": (
                            "use_frozen_envelope_and_exact_diff"
                            if role_violation
                            else "retry_with_bounded_inspection"
                        ),
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


def _subagent_llm_availability_control_violation(command):
    """Return the protected marker used to reach global pause authority."""

    low = str(command or "").lower().replace("\\", "/")
    return next(
        (marker for marker in _LLM_AVAILABILITY_CONTROL_MARKERS if marker in low),
        None,
    )


def _make_subagent_llm_availability_guard(role_name):
    """Deny every sub-agent Bash route to the pause/resume control plane."""

    async def handler(hook_input, tool_use_id, context):
        from claude_agent_sdk.types import SyncHookJSONOutput

        tool_name = hook_input.get("tool_name", "") if isinstance(hook_input, dict) else ""
        tool_input = hook_input.get("tool_input", {}) if isinstance(hook_input, dict) else {}
        if tool_name != "Bash":
            return SyncHookJSONOutput()
        command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
        marker = _subagent_llm_availability_control_violation(command)
        if not marker:
            return SyncHookJSONOutput()
        reason = (
            f"{role_name} may not access operator-owned LLM pause/resume control "
            f"through Bash ({marker}). Manual resume is consumed once by the "
            "parent launcher before any SDK child starts."
        )
        try:
            from system_log import log_system_event
            log_system_event(
                "pipeline.subagent_llm_availability_guard_block",
                "error",
                f"BLOCKED {role_name} from LLM availability control",
                {
                    "role": role_name,
                    "tool": tool_name,
                    "marker": marker,
                    "tool_use_id": str(tool_use_id)[:64],
                    "command_preview": str(command)[:2000],
                    "command_truncated": len(str(command)) > 2000,
                    "operator_action_required": True,
                },
            )
        except Exception:
            pass
        return SyncHookJSONOutput(hookSpecificOutput={
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        })

    try:
        from claude_agent_sdk.types import HookMatcher
        return {"PreToolUse": [HookMatcher(matcher="Bash", hooks=[handler])]}
    except Exception:
        return None


def _make_exact_bash_allowlist_guard(role_name, allowed_commands):
    """Deny Bash unless its complete command is explicitly operator-owned.

    This is intentionally stricter than the generic read-only guard.  It is
    used by capability probes which need Bash execution evidence but must not
    let the model turn that capability into curl/wget/nc or an unreviewed shell
    program.  Stripping outer whitespace is the only normalization performed.
    """

    allowed = frozenset(str(command).strip() for command in allowed_commands or ())

    async def handler(hook_input, tool_use_id, context):
        from claude_agent_sdk.types import SyncHookJSONOutput

        try:
            if not isinstance(hook_input, dict):
                raise ValueError("hook_input_not_object")
            tool_name = hook_input.get("tool_name", "")
            if tool_name != "Bash":
                raise ValueError("unexpected_exact_bash_guard_tool")
            tool_input = hook_input.get("tool_input")
            if not isinstance(tool_input, dict):
                raise ValueError("tool_input_not_object")
            command = str(tool_input.get("command", "")).strip()
            if command in allowed:
                return SyncHookJSONOutput()
        except Exception as exc:
            return SyncHookJSONOutput(hookSpecificOutput={
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"{role_name} exact Bash guard failed closed "
                    f"({type(exc).__name__})."
                ),
            })
        reason = (
            f"{role_name} Bash command denied by the exact operator allowlist. "
            "Network commands, shell rewrites, and unreviewed variants are not permitted."
        )
        return SyncHookJSONOutput(hookSpecificOutput={
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        })

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
)


def _live_evidence_marker(text):
    """Return whether text names a mutable results tree or live alias.

    Bash inputs are shell programs rather than clean paths.  Checking only the
    basename of shlex tokens left directory reads and globs such as
    ``find web/core/results`` and ``cat web/core/results/*`` outside the old
    guard.  Treat a path-segment named ``results`` and every known live alias as
    evidence markers; the exact generation snapshot is allowed separately by
    resolved-path containment.
    """

    normalized = str(text or "").replace("\\", "/")
    lowered = normalized.lower()
    if any(name.lower() in lowered for name in _MASTER_LIVE_EVIDENCE_FILENAMES):
        return True
    # Preserve shell glob metacharacters inside each segment.  A literal-only
    # comparison still permits ``res*``, ``result?`` or ``[r]esults`` to expand
    # to the protected directory before the command executes.
    segments = re.split(r"[/\s'\"=():;,&|<>]+", lowered)
    protected_names = ("results",) + tuple(
        name.lower() for name in _MASTER_LIVE_EVIDENCE_FILENAMES
    )
    for segment in segments:
        pattern = segment.strip("`$")
        if not pattern:
            continue
        for protected in protected_names:
            try:
                if fnmatch.fnmatchcase(protected, pattern):
                    return True
            except (TypeError, ValueError):
                continue
    return False


def _master_live_evidence_read_violation(
    tool_name,
    tool_input,
    allowed_evidence_snapshot_dir=None,
    allowed_results_dirs=None,
):
    """Return a forbidden live-evidence path read by a Master role, if any."""
    override = _patched_llm_query_helper(
        "_master_live_evidence_read_violation", _master_live_evidence_read_violation
    )
    if override is not None:
        return override(
            tool_name,
            tool_input,
            allowed_evidence_snapshot_dir,
            allowed_results_dirs,
        )

    # Delayed import avoids a circular dependency on llm_query group A.
    from llm_query import _LLM_PROJECT_ROOT

    if not isinstance(tool_input, dict):
        return None

    def _path_violation(raw_path):
        raw_text = str(raw_path or "").strip().strip("'\"")
        if not raw_text:
            return None
        try:
            candidate_path = Path(_local_path_from_file_uri(raw_text))
            if not candidate_path.is_absolute():
                candidate_path = _LLM_PROJECT_ROOT / candidate_path
            resolved = candidate_path.resolve(strict=False)
            allowed_roots = []
            if allowed_evidence_snapshot_dir is not None:
                allowed_roots.append(allowed_evidence_snapshot_dir)
            allowed_roots.extend(allowed_results_dirs or ())
            for allowed_root in allowed_roots:
                allowed = Path(allowed_root).resolve(strict=False)
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
            if _live_evidence_marker(raw_text):
                return str(resolved)
            return None
        except Exception:
            if _live_evidence_marker(raw_text):
                return raw_text[:500]
            return None

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
        if not _live_evidence_marker(normalized):
            continue
        violation = _path_violation(normalized)
        if violation:
            return violation[:500]
    return None


def _make_master_evidence_read_guard(
    role_name,
    allowed_evidence_snapshot_dir=None,
    allowed_results_dirs=None,
):
    """Prevent a planning/review role from bypassing its frozen snapshot."""
    async def handler(hook_input, tool_use_id, context):
        from claude_agent_sdk.types import SyncHookJSONOutput

        try:
            if not isinstance(hook_input, dict):
                raise ValueError("hook_input_not_object")
            tool_name = hook_input.get("tool_name", "")
            if tool_name not in {"Read", "Bash"}:
                raise ValueError("unexpected_evidence_guard_tool")
            tool_input = hook_input.get("tool_input")
            if not isinstance(tool_input, dict):
                raise ValueError("tool_input_not_object")
            violation = _master_live_evidence_read_violation(
                tool_name,
                tool_input,
                allowed_evidence_snapshot_dir,
                allowed_results_dirs,
            )
        except Exception as exc:
            violation = f"evidence_guard_internal_error:{type(exc).__name__}"
        if not violation:
            return SyncHookJSONOutput()
        reason = (
            "Live evaluation evidence read denied. Use only the generation's "
            "vN/evidence_snapshot files supplied in the prompt; forbidden target: "
            f"{violation}"
        )
        try:
            from system_log import log_system_event

            log_system_event(
                "pipeline.frozen_evidence_read_blocked",
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

    try:
        from claude_agent_sdk.types import HookMatcher

        return {"PreToolUse": [
            HookMatcher(matcher="Bash", hooks=[handler]),
            HookMatcher(matcher="Read", hooks=[handler]),
        ]}
    except Exception:
        return None


def _role_requires_frozen_evidence_guard(role_name):
    """Return the registry-owned live-evidence isolation requirement."""

    # Delayed import avoids a circular dependency on llm_query group A.
    from llm_query import resolve_llm_role_contract, LLMRoleContractError

    try:
        return resolve_llm_role_contract(role_name).requires_frozen_evidence_guard
    except LLMRoleContractError:
        # ``run_claude_query`` rejects unregistered roles before hook assembly.
        # Keeping this predicate total is useful to guard-only diagnostics.
        return False


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
        from claude_agent_sdk.types import SyncHookJSONOutput

        try:
            if not isinstance(hook_input, dict):
                raise ValueError("hook_input_not_object")
            tool_name = hook_input.get("tool_name", "")
            if tool_name not in {"Bash", "Edit", "Write", "NotebookEdit"}:
                raise ValueError("unexpected_write_guard_tool")
            tool_input = hook_input.get("tool_input")
            if not isinstance(tool_input, dict):
                raise ValueError("tool_input_not_object")
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
        except Exception as exc:
            blocked = (
                "Write denied because the system write-scope guard failed closed "
                f"({type(exc).__name__})."
            )
            return SyncHookJSONOutput(hookSpecificOutput={
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": blocked,
            })
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
    override = _patched_llm_query_helper(
        "_subagent_readonly_mutation_violation", _subagent_readonly_mutation_violation
    )
    if override is not None:
        return override(tool_name, tool_input)

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
            "`sed -n 'START,ENDp' exact_file`, or `rg PATTERN exact_allowed_path`. "
            "Python snippets, shell wrappers, implicit-directory scans, and indirect "
            "configuration reads are unavailable. Redirect only to `/dev/null` for "
            "stderr noise."
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
        from claude_agent_sdk.types import SyncHookJSONOutput

        try:
            if not isinstance(hook_input, dict):
                raise ValueError("hook_input_not_object")
            tool_name = hook_input.get("tool_name", "")
            if tool_name not in {"Bash", "Edit", "Write", "NotebookEdit"}:
                raise ValueError("unexpected_readonly_guard_tool")
            tool_input = hook_input.get("tool_input")
            if not isinstance(tool_input, dict):
                raise ValueError("tool_input_not_object")
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
        except Exception as exc:
            return SyncHookJSONOutput(hookSpecificOutput={
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"{role_name} read-only guard failed closed "
                    f"({type(exc).__name__})."
                ),
            })
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
        "- If Bash is available to this role, each command starts from that directory; name every allowed file or directory explicitly and do not rely on a persisted working directory.",
        "- Python `-c`/heredoc snippets, `bash`/`sh -c` wrappers, globs, dynamic expansion, Git history, and implicit current-directory scans are unavailable. Use only the role-specific exact paths and commands supplied below.",
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
            "- This LLM role is read-only. If Bash is available, it may directly read, diff, grep, count, and print explicit allowed paths, but must not create, modify, delete, move, tag, checkout, or write any file anywhere.",
            "- Do not use output redirection (`>`, `>>`, `&>`, `&>>`) or `tee` except redirects to `/dev/null` for stderr/stdout noise.",
            "- For comparisons, use direct read-only commands such as `diff -u EXACT_A EXACT_B`, `git diff --no-index -- EXACT_A EXACT_B`, `sed -n 'START,ENDp' EXACT_FILE`, or `rg PATTERN EXACT_ALLOWED_PATH`.",
            "- Never write comparison snippets to `/tmp`, `/var/tmp`, the bot directory, or `web/core/results`; if a Bash command is denied, do not retry the same mutating pattern.",
        ])
    return "\n".join(lines) + "\n\n"
