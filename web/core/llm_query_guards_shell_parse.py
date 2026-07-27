"""Shell-command read-target / scope-violation parsing for llm_query_guards.

Extracted as a cohesive business cluster; ``llm_query_guards.py`` retains thin
delegate shells so external ``from llm_query_guards import <name>`` and
``monkeypatch.setattr(llm_query_guards, "<name>", ...)`` keep resolving.

Business responsibility (single cohesive domain):
* Shell simple-command segmentation and word/assignment/redirection parsing.
* Allowed write/read scope normalization and path-membership checks.
* Read-path violation detection for the LLM tool guards.
* Shell read-redirect target enumeration.
* Per-tool read-target extraction (grep-like, sed, python py_compile, git diff,
  bash segment).
* Subagent bash read/write scope-violation detectors.
* Subagent git/bash command mutation detectors and the bash cost detector.
* The ``_make_subagent_*_guard`` factories for read-scope, cost, llm-availability,
  write, and readonly guards.

Pure text/scope analysis: no state, no I/O.

Cross-references to symbols that remain in ``llm_query_guards`` (shell-comment
stripping, heredoc helpers, command-name/positional helpers, scope-sensitive
roots, the write-target iterators, the git subcommand/tag helpers, and the
various ``_READ_*`` / ``_SUBAGENT_*`` constant tables) are reached through
``_lg.<name>`` so that test monkeypatches on ``llm_query_guards.<name>``
propagate.

CRITICAL (wave-3 lesson): EVERY intra-companion call to a moved symbol ALSO
routes through ``_lg.<name>(...)`` so monkeypatches on
``llm_query_guards.<name>`` propagate even when both call sites now live in
this companion.
"""
from __future__ import annotations

import re
import shlex

import llm_query_guards as _lg  # for cross-refs


def _split_shell_simple_commands(command):
    """Split a shell command into simple command segments for guard analysis."""
    text = _lg._strip_heredoc_bodies(command)
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
    words = _lg._strip_shell_redirection_words(_lg._shell_words(segment))
    while words and _lg._is_shell_assignment(words[0]):
        words = words[1:]
    while words and words[0] in {"command", "builtin"}:
        words = words[1:]
    if words and words[0] == "env":
        words = words[1:]
        while words and (words[0].startswith("-") or _lg._is_shell_assignment(words[0])):
            if words[0] in {"-u", "--unset"} and len(words) > 1:
                words = words[2:]
            else:
                words = words[1:]
    return words



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
                resolved_dirs.append(str(Path(_lg._local_path_from_file_uri(item)).resolve()))
        for item in files:
            if item:
                resolved_files.append(str(Path(_lg._local_path_from_file_uri(item)).resolve()))
    except Exception:
        resolved_dirs = [str(item) for item in dirs if item]
        resolved_files = [str(item) for item in files if item]
    return {"dirs": resolved_dirs, "files": resolved_files}



def _path_inside_allowed_scope(path, allowed_scope, base_dir=None):
    try:
        from pathlib import Path

        candidate = Path(_lg._local_path_from_file_uri(path))
        if not candidate.is_absolute():
            base = Path(base_dir).resolve(strict=False) if base_dir else _lg._project_root_for_guard()
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



def _read_path_violation(raw_path, allowed_scope, *, base_dir=None):
    """Return a typed denial for a single filesystem read target."""

    text = _lg._local_path_from_file_uri(raw_path)
    text = _lg._strip_shell_grouping_parens(str(text or "").strip().strip("'\""))
    if not text:
        return "empty_path"
    if text == "-" or text.lower() in _lg._SAFE_REDIRECT_TARGETS:
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
                else _lg._project_root_for_guard()
            )
            lexical = base / lexical
        lexical = lexical.absolute()
        if _lg._path_has_symlink_component(lexical):
            return f"symlink_path:{text[:120]}"
        resolved = lexical.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        return f"unresolvable_path:{type(exc).__name__}:{text[:100]}"

    lowered_parts = _lg._path_parts_lower(resolved)
    if ".git" in lowered_parts:
        return f"git_metadata_forbidden:{resolved}"
    if any(part in {"archive", ".archive"} for part in lowered_parts):
        return f"archived_tree_forbidden:{resolved}"

    project_root = _lg._project_root_for_guard()
    if not _lg._resolved_within(resolved, project_root):
        return f"outside_active_checkout:{resolved}"

    bot_root = _lg._bot_anchor(resolved)
    if bot_root is not None:
        authorized_bots = _lg._scope_sensitive_roots(allowed_scope, "bot")
        if not any(bot_root.resolve(strict=False) == root for root in authorized_bots):
            return f"bot_not_in_role_scope:{resolved}"

    if "results" in lowered_parts:
        authorized_results = _lg._scope_sensitive_roots(allowed_scope, "results")
        if not any(_lg._resolved_within(resolved, root) for root in authorized_results):
            return f"results_not_in_role_scope:{resolved}"

    if not _lg._path_inside_allowed_scope(resolved, allowed_scope):
        return f"outside_role_read_scope:{resolved}"
    return None



def _iter_shell_read_redirect_targets(command):
    """Yield simple ``< file`` targets; complex redirections are rejected above."""

    text = _lg._strip_shell_comments(str(command or ""))
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
        target, end = _lg._read_shell_token(text, cursor, extra_stop_chars="()")
        if target:
            yield target
        index = max(end, cursor + 1)



def _grep_like_read_targets(command_name, args):
    """Extract pattern-file and search-root reads for grep/rg."""

    targets = _lg._option_read_targets(command_name, args)
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
            inline_kind, inline_value = _lg._inline_option(command_name, arg)
            if inline_kind == "path":
                targets.append(inline_value)
                index += 1
                continue
            if inline_kind == "unknown":
                raise ValueError(f"unproved_inline_option:{arg.split('=', 1)[0]}")
            if inline_kind is not None:
                index += 1
                continue
            if _lg._option_consumes_value(command_name, arg):
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
        str(arg) in _lg._SUBAGENT_GIT_GLOBAL_OPTIONS_WITH_VALUE
        or any(
            str(arg).startswith(option + "=")
            for option in _lg._SUBAGENT_GIT_GLOBAL_OPTIONS_WITH_VALUE
        )
        for arg in args
    ):
        raise ValueError("git_global_path_or_config_forbidden")
    subcmd, rest = _lg._subagent_git_subcommand(args)
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
    positionals = _lg._command_positionals("diff", rest)
    positionals = [item for item in positionals if item != "--no-index"]
    if len(positionals) != 2:
        raise ValueError("git_no_index_diff_requires_two_paths")
    return positionals



def _bash_segment_read_targets(segment, *, current_dir, depth):
    words = _lg._simple_command_words(segment)
    if not words:
        return [], current_dir
    command_token = _lg._strip_shell_grouping_parens(words[0])
    command_name = _lg._command_name(command_token)
    args = [_lg._strip_shell_grouping_parens(arg) for arg in words[1:]]
    if "/" in command_token or "\\" in command_token:
        executable = Path(command_token)
        if not executable.is_absolute():
            raise ValueError("relative_executable_forbidden")
        if executable.parent not in {Path("/bin"), Path("/usr/bin")}:
            raise ValueError("untrusted_executable_path")

    if command_name in {"pwd", "true", "false", ":", "echo", "printf"}:
        return [], current_dir
    if command_name == "cd":
        target = _lg._cd_target_from_args(args)
        if not target:
            raise ValueError("cd_requires_explicit_path")
        return [target], _lg._resolve_guard_path(target, base_dir=current_dir)
    if command_name in {"bash", "dash", "sh", "zsh"}:
        # The child shell can reinterpret quoting, positional parameters,
        # startup files, aliases, and its own cwd.  A static parent hook cannot
        # prove the final read set, so wrappers are intentionally unavailable.
        raise ValueError("shell_wrapper_forbidden")
    if command_name.startswith("python"):
        return _lg._python_pycompile_targets(args), current_dir
    if command_name == "git":
        return _lg._git_no_index_diff_targets(args), current_dir
    if command_name in {"rg", "grep"}:
        return _lg._grep_like_read_targets(command_name, args), current_dir
    if command_name == "sed":
        return _lg._sed_read_targets(args), current_dir
    if command_name in {"cat", "nl", "wc", "sha256sum", "md5sum", "file", "stat"}:
        return (
            _lg._option_read_targets(command_name, args)
            + _lg._command_positionals(command_name, args)
        ), current_dir
    if command_name in {"head", "tail"}:
        return _lg._command_positionals(command_name, args), current_dir
    if command_name in {"ls", "tree", "du"}:
        targets = _lg._command_positionals(command_name, args)
        return (targets or ["."]), current_dir
    if command_name in {"diff", "cmp"}:
        targets = _lg._option_read_targets("diff", args) + _lg._command_positionals("diff", args)
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
        positionals = _lg._command_positionals(command_name, args)
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
    if _lg._shell_heredoc_delimiters(text) or "<<<" in text or "<(" in text or ">(" in text:
        return "complex_shell_input_forbidden"
    if _lg._shell_has_dynamic_expansion(text):
        return "dynamic_shell_expansion_forbidden"
    try:
        for segment in _lg._split_shell_simple_commands(text):
            raw_words = _lg._shell_words(segment)
            if raw_words and _lg._command_name(raw_words[0]) == "env":
                raise ValueError("env_wrapper_forbidden")
            for word in raw_words:
                if _lg._is_shell_assignment(word):
                    name = str(word).split("=", 1)[0].upper()
                    if name not in {"LC_ALL", "LANG", "LANGUAGE", "TZ"}:
                        raise ValueError(f"shell_assignment_forbidden:{name}")
        current_dir = str(
            Path(initial_dir).resolve(strict=False)
            if initial_dir is not None
            else _lg._project_root_for_guard()
        )
        targets = []
        for redirect in _lg._iter_shell_read_redirect_targets(text):
            targets.append((redirect, current_dir))
        for segment in _lg._split_shell_simple_commands(text):
            segment_targets, next_dir = _lg._bash_segment_read_targets(
                segment,
                current_dir=current_dir,
                depth=depth,
            )
            targets.extend((target, current_dir) for target in segment_targets)
            current_dir = next_dir
        if return_targets:
            return tuple(target for target, _base in targets)
        for target, base in targets:
            violation = _lg._read_path_violation(target, allowed_scope, base_dir=base)
            if violation:
                return violation
        return None
    except Exception as exc:
        return f"unprovable_bash_read:{type(exc).__name__}:{str(exc)[:180]}"



def _subagent_read_scope_violation(tool_name, tool_input, allowed_scope):
    override = _lg._patched_llm_query_helper(
        "_subagent_read_scope_violation", _lg._subagent_read_scope_violation
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
            violation = _lg._read_path_violation(path, allowed_scope)
            if violation:
                return violation
        return None
    if tool_name == "Bash":
        return _lg._subagent_bash_read_scope_violation(
            tool_input.get("command", ""),
            allowed_scope,
        )
    return None



def _make_subagent_read_scope_guard(role_name, allowed_read_scope):
    """Build the mandatory role-scoped Read/Bash capability hook."""

    scope = _lg._normalize_allowed_read_scope(allowed_read_scope)

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
            violation = _lg._subagent_read_scope_violation(
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



def _subagent_bash_write_scope_violation(command, allowed_dir):
    """Return a violation reason when a Bash mutation writes outside allowed_dir."""
    override = _lg._patched_llm_query_helper(
        "_subagent_bash_write_scope_violation", _lg._subagent_bash_write_scope_violation
    )
    if override is not None:
        return override(command, allowed_dir)

    python_write_event_seen = False
    for detector, target, cwd in _lg._iter_subagent_bash_write_events(command):
        if detector.startswith("python_"):
            python_write_event_seen = True
        if _lg._subagent_write_target_outside_allowed(target, allowed_dir, base_dir=cwd):
            return f"{detector}:{str(target)[:120]}"

    mutation_detector = _lg._subagent_bash_mutation_detector(command)
    if not mutation_detector:
        return None

    if mutation_detector.startswith("python_"):
        if python_write_event_seen:
            return None
        return mutation_detector if _lg._subagent_is_outside_allowed(command, allowed_dir) else None
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
    return mutation_detector if _lg._subagent_is_outside_allowed(command, allowed_dir) else None



def _subagent_git_command_mutation_detector(command):
    for args in _lg._iter_subagent_git_args(command):
        subcmd, rest = _lg._subagent_git_subcommand(args)
        if subcmd in {"", "version", "help"}:
            continue
        if subcmd == "tag":
            if _lg._subagent_git_tag_invocation_is_mutating(rest):
                return "git_tag_mutation"
            continue
        if subcmd not in _lg._SUBAGENT_GIT_READONLY_COMMANDS:
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
    for detector, target in _lg._iter_subagent_bash_write_targets(command):
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
        if _lg._SUBAGENT_PYTHON_OPEN_WRITE_RE.search(low):
            return "python_open_write_mode"
        for pattern in _lg._SUBAGENT_PYTHON_WRITE_PATTERNS:
            if pattern in low:
                return f"python_write_pattern:{pattern}"
    git_detector = _lg._subagent_git_command_mutation_detector(command)
    if git_detector:
        return git_detector
    bash_detector = _lg._subagent_bash_command_mutation_detector(command)
    if bash_detector:
        return bash_detector
    if _lg._subagent_git_tag_is_mutating(command):
        return "git_tag_mutation"
    return None



def _subagent_bash_cost_detector(command):
    """Return a reason string for read-only but high-cost Bash commands."""
    for args in _lg._iter_subagent_git_args(command):
        subcmd, rest = _lg._subagent_git_subcommand(args)
        if subcmd != "log":
            continue
        args_text = [str(a) for a in rest]
        lows = [a.lower() for a in args_text]
        if any(a == "--all" or a.startswith("--all=") for a in lows):
            return "git_log_all_history"
        if any(a == "-S" or a.startswith("-S") or a == "-G" or a.startswith("-G")
               for a in args_text):
            return "git_log_pickaxe_full_history"
        if not _lg._git_log_has_bounded_scope(rest):
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
            role_violation = _lg._role_bash_read_violation(role_name, cmd)
            reason = role_violation or _lg._subagent_bash_cost_detector(cmd)
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



def _make_subagent_llm_availability_guard(role_name):
    """Deny every sub-agent Bash route to the pause/resume control plane."""

    async def handler(hook_input, tool_use_id, context):
        from claude_agent_sdk.types import SyncHookJSONOutput

        tool_name = hook_input.get("tool_name", "") if isinstance(hook_input, dict) else ""
        tool_input = hook_input.get("tool_input", {}) if isinstance(hook_input, dict) else {}
        if tool_name != "Bash":
            return SyncHookJSONOutput()
        command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
        marker = _lg._subagent_llm_availability_control_violation(command)
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
    _allowed_scope = _lg._normalize_allowed_write_scope(allowed_write_dir)
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
                mutation_detector = _lg._subagent_bash_mutation_detector(cmd)
                write_scope_violation = _lg._subagent_bash_write_scope_violation(cmd, _allowed_scope)
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
                if _lg._subagent_is_outside_allowed(fp, _allowed_scope):
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
            violation = _lg._subagent_readonly_mutation_violation(tool_name, tool_input)
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



