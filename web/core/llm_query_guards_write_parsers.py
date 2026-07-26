"""Shell/python write-target parsing for llm_query_guards.

Extracted as a cohesive business cluster; llm_query_guards.py retains thin
delegate shells so external ``from llm_query_guards import <name>`` and
``monkeypatch.setattr(llm_query_guards, "<name>", ...)`` keep resolving.

Business responsibility (single cohesive domain):
* Shell/python write-target text->target extraction.
* Heredoc body / delimiter / shell-token primitive parsers.
* Redirect-target enumeration.
* Iteration helpers that walk subagent bash/python commands to find write
  targets.

Pure text parsing: no state, no I/O.

Cross-references to symbols that remain in ``llm_query_guards`` (shell
word/segment helpers, ``_simple_command_words``, ``_command_name``,
``_non_option_args``, ``_strip_shell_comments``, ``_split_shell_simple_commands``,
``_strip_shell_grouping_parens``, ``_cd_target_from_args``,
``_resolve_guard_path``, ``_project_root_for_guard``, and the
``_SAFE_REDIRECT_TARGETS`` constant) are reached through ``_lg.<name>`` so that
test monkeypatches on ``llm_query_guards.<name>`` propagate.

CRITICAL (wave-3 lesson): EVERY intra-companion call to a moved symbol ALSO
routes through ``_lg.<name>(...)`` so monkeypatches on
``llm_query_guards.<name>`` propagate even when both call sites now live in
this companion.
"""
from __future__ import annotations

import re

import llm_query_guards as _lg  # for cross-refs


# --- Python write-target regexes -------------------------------------------
# Used exclusively by ``_iter_python_write_targets_from_text``.  The sibling
# constants ``_SUBAGENT_PYTHON_WRITE_PATTERNS`` / ``_SUBAGENT_PYTHON_OPEN_WRITE_RE``
# are used by the mutation-detector cluster that stays in ``llm_query_guards``,
# so they remain there and are reached via ``_lg.<name>``.
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
        pending.extend(_lg._shell_heredoc_delimiters(line))
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
        delimiters = _lg._shell_heredoc_delimiters(lines[index])
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
            token, i = _lg._read_shell_token(text, i)
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


def _iter_shell_write_redirect_targets(command):
    text = _lg._strip_shell_comments(_lg._strip_heredoc_bodies(command))
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
        target, end = _lg._read_shell_token(text, j, extra_stop_chars="()")
        if target:
            yield target
        i = max(end, j + 1)


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
    for target in _lg._iter_shell_write_redirect_targets(segment):
        target = target.strip("'\"")
        if target.startswith("&") or target.lower() in _lg._SAFE_REDIRECT_TARGETS:
            continue
        yield "write_redirect", target

    words = _lg._simple_command_words(segment)
    if not words:
        return
    cmd = _lg._command_name(words[0])
    args = words[1:]

    if cmd.startswith("python"):
        yield from _lg._iter_python_write_targets_from_text(segment)
        if python_heredoc_body:
            yield from _lg._iter_python_write_targets_from_text(python_heredoc_body)
        return

    if cmd in {"mkdir", "touch", "rm", "rmdir"}:
        for target in _lg._non_option_args(args):
            yield cmd, target
        return

    if cmd == "cp":
        non_options = _lg._non_option_args(args, options_with_value=("-t", "--target-directory"))
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
        non_options = _lg._non_option_args(args, options_with_value=("-t", "--target-directory"))
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
        for target in _lg._non_option_args(args):
            if target != "-":
                yield "tee", target
        return

    if cmd == "sed" and any(arg == "-i" or str(arg).startswith("-i") for arg in args):
        non_options = _lg._non_option_args(args, options_with_value=("-e", "-f"))
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
    current_dir = str(_lg._project_root_for_guard())
    heredoc_bodies = iter(_lg._iter_shell_heredoc_bodies(command))
    for segment in _lg._split_shell_simple_commands(command):
        words = _lg._simple_command_words(segment)
        if not words:
            continue
        cmd = _lg._command_name(words[0])
        args = words[1:]
        python_heredoc_body = None
        if cmd.startswith("python") and _lg._shell_heredoc_delimiters(segment):
            python_heredoc_body = next(heredoc_bodies, "")
        for detector, target in _lg._iter_subagent_segment_write_targets(
            segment,
            python_heredoc_body=python_heredoc_body,
        ):
            yield detector, target, current_dir
        if cmd == "cd":
            target = _lg._cd_target_from_args(args)
            if target:
                current_dir = _lg._resolve_guard_path(target, base_dir=current_dir)


def _iter_subagent_bash_write_targets(command):
    for detector, target, _cwd in _lg._iter_subagent_bash_write_events(command):
        yield detector, target
