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
import fnmatch
import os
import re
import shlex
from pathlib import Path
from urllib.parse import unquote, urlsplit

import llm_trace_context as _tc
from bot_namespace import ACTIVE_BOT_PREFIX

# Shell/python write-target text->target parsing (heredoc/redirect/word/
# assignment parsers and the subagent bash/python write-target iterators)
# live in llm_query_guards_write_parsers as a cohesive business cluster.
# Thin delegate shells below preserve the historical public surface so
# ``from llm_query_guards import <name>`` and
# ``monkeypatch.setattr(llm_query_guards, "<name>", ...)`` keep resolving.
import llm_query_guards_write_parsers as _wp  # noqa: E402
import llm_query_guards_shell_parse as _lgs  # noqa: E402  (read-target/scope cluster)


_LLM_CANCEL_CONTEXT = _tc._LLM_CANCEL_CONTEXT
_LLM_TOOL_TRACE = _tc._LLM_TOOL_TRACE


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
    """Delegate to llm_trace_context."""
    with _tc.capture_llm_tool_trace() as events:
        yield events


def _record_llm_tool_trace_event(event):
    """Delegate to llm_trace_context."""
    return _tc._record_llm_tool_trace_event(event)


@contextlib.contextmanager
def llm_cancel_scope(scope, reason="parent_timeout", timeout_sec=None):
    """Delegate to llm_trace_context."""
    with _tc.llm_cancel_scope(scope, reason=reason, timeout_sec=timeout_sec):
        yield


def _current_llm_cancel_context():
    """Delegate to llm_trace_context."""
    return _tc._current_llm_cancel_context()


def _cancelled_event(base_category, parent_category, default_severity="warn"):
    """Delegate to llm_trace_context."""
    return _tc._cancelled_event(
        base_category, parent_category, default_severity=default_severity
    )


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
# The four ``_SUBAGENT_PYTHON_*_TARGET_RE`` / ``_ASSIGN_RE`` / ``_VAR_PATH_WRITE_RE``
# regexes moved to ``llm_query_guards_write_parsers`` (used exclusively by
# ``_iter_python_write_targets_from_text``). The mutation-detector cluster here
# only needs ``_SUBAGENT_PYTHON_OPEN_WRITE_RE`` and ``_SUBAGENT_PYTHON_WRITE_PATTERNS``.
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
    """Delegate to llm_query_guards_write_parsers."""
    return _wp._strip_heredoc_bodies(command)


def _iter_shell_heredoc_bodies(command):
    """Delegate to llm_query_guards_write_parsers."""
    yield from _wp._iter_shell_heredoc_bodies(command)


def _shell_heredoc_delimiters(line):
    """Delegate to llm_query_guards_write_parsers."""
    return _wp._shell_heredoc_delimiters(line)


def _read_shell_token(text, start, extra_stop_chars=""):
    """Delegate to llm_query_guards_write_parsers."""
    return _wp._read_shell_token(text, start, extra_stop_chars=extra_stop_chars)


def _split_shell_simple_commands(command):
    """Delegate to llm_query_guards_shell_parse."""
    yield from _lgs._split_shell_simple_commands(command)


def _shell_words(segment):
    """Delegate to llm_query_guards_shell_parse."""
    return _lgs._shell_words(segment)


def _is_shell_assignment(word):
    """Delegate to llm_query_guards_shell_parse."""
    return _lgs._is_shell_assignment(word)


def _strip_shell_redirection_words(words):
    """Delegate to llm_query_guards_shell_parse."""
    return _lgs._strip_shell_redirection_words(words)


def _simple_command_words(segment):
    """Delegate to llm_query_guards_shell_parse."""
    return _lgs._simple_command_words(segment)


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
    """Delegate to llm_query_guards_write_parsers."""
    yield from _wp._iter_shell_write_redirect_targets(command)


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
    """Delegate to llm_query_guards_write_parsers."""
    yield from _wp._iter_python_write_targets_from_text(text)


def _iter_subagent_segment_write_targets(segment, python_heredoc_body=None):
    """Delegate to llm_query_guards_write_parsers."""
    yield from _wp._iter_subagent_segment_write_targets(
        segment, python_heredoc_body=python_heredoc_body
    )


def _iter_subagent_bash_write_events(command):
    """Delegate to llm_query_guards_write_parsers."""
    yield from _wp._iter_subagent_bash_write_events(command)


def _iter_subagent_bash_write_targets(command):
    """Delegate to llm_query_guards_write_parsers."""
    yield from _wp._iter_subagent_bash_write_targets(command)


def _normalize_allowed_write_scope(allowed_write_dir):
    """Delegate to llm_query_guards_shell_parse."""
    return _lgs._normalize_allowed_write_scope(allowed_write_dir)


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
    """Delegate to llm_query_guards_shell_parse."""
    return _lgs._path_inside_allowed_scope(path, allowed_scope, base_dir=base_dir)


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
    """Delegate to llm_query_guards_shell_parse."""
    return _lgs._read_path_violation(raw_path, allowed_scope, base_dir=base_dir)


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
    """Delegate to llm_query_guards_shell_parse."""
    yield from _lgs._iter_shell_read_redirect_targets(command)


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
    """Delegate to llm_query_guards_shell_parse."""
    return _lgs._grep_like_read_targets(command_name, args)


def _sed_read_targets(args):
    """Delegate to llm_query_guards_shell_parse."""
    return _lgs._sed_read_targets(args)


def _python_pycompile_targets(args):
    """Delegate to llm_query_guards_shell_parse."""
    return _lgs._python_pycompile_targets(args)


def _git_no_index_diff_targets(args):
    """Delegate to llm_query_guards_shell_parse."""
    return _lgs._git_no_index_diff_targets(args)


def _bash_segment_read_targets(segment, *, current_dir, depth):
    """Delegate to llm_query_guards_shell_parse."""
    return _lgs._bash_segment_read_targets(segment, current_dir=current_dir, depth=depth)


def _subagent_bash_read_scope_violation(
    command,
    allowed_scope,
    *,
    initial_dir=None,
    depth=0,
    return_targets=False,
):
    """Delegate to llm_query_guards_shell_parse."""
    return _lgs._subagent_bash_read_scope_violation(
        command, allowed_scope, initial_dir=initial_dir, depth=depth, return_targets=return_targets
    )


def _subagent_read_scope_violation(tool_name, tool_input, allowed_scope):
    """Delegate to llm_query_guards_shell_parse."""
    return _lgs._subagent_read_scope_violation(tool_name, tool_input, allowed_scope)


def _make_subagent_read_scope_guard(role_name, allowed_read_scope):
    """Delegate to llm_query_guards_shell_parse."""
    return _lgs._make_subagent_read_scope_guard(role_name, allowed_read_scope)


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
    """Delegate to llm_query_guards_shell_parse."""
    return _lgs._subagent_bash_write_scope_violation(command, allowed_dir)


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
    """Delegate to llm_query_guards_shell_parse."""
    return _lgs._subagent_git_command_mutation_detector(command)


def _subagent_bash_command_mutation_detector(command):
    """Delegate to llm_query_guards_shell_parse."""
    return _lgs._subagent_bash_command_mutation_detector(command)


def _subagent_bash_mutation_detector(command):
    """Delegate to llm_query_guards_shell_parse."""
    return _lgs._subagent_bash_mutation_detector(command)


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
    """Delegate to llm_query_guards_shell_parse."""
    return _lgs._subagent_bash_cost_detector(command)


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
    """Delegate to llm_query_guards_shell_parse."""
    return _lgs._make_subagent_cost_guard(role_name)


def _subagent_llm_availability_control_violation(command):
    """Return the protected marker used to reach global pause authority."""

    low = str(command or "").lower().replace("\\", "/")
    return next(
        (marker for marker in _LLM_AVAILABILITY_CONTROL_MARKERS if marker in low),
        None,
    )


def _make_subagent_llm_availability_guard(role_name):
    """Delegate to llm_query_guards_shell_parse."""
    return _lgs._make_subagent_llm_availability_guard(role_name)


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
    """Delegate to llm_query_guards_shell_parse."""
    return _lgs._make_subagent_write_guard(allowed_write_dir)


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
    """Delegate to llm_query_guards_shell_parse."""
    return _lgs._make_subagent_readonly_guard(role_name)

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
