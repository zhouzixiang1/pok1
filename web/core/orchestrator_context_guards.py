"""Orchestrator PreToolUse/PostToolUse guard hooks.

Extracted from orchestrator_context.py as a single business responsibility:
shell/python/git command parsing + mutation/write detection + operator-only /
llm-availability / strict-authority command targeting.  Used by the
orchestrator's own Claude agent tool guards (``_make_bot_dir_guard_hook``).

All public symbols are re-exported by orchestrator_context.py for backward
compatibility.
"""

import re
import shlex

_SAFE_REDIRECT_TARGETS = {"/dev/null", "nul"}
_SAFE_REDIRECT_PREFIXES = ("/tmp/", "/var/tmp/", "$tmpdir/", "${tmpdir}/")
_PYTHON_OPEN_WRITE_RE = re.compile(r"open\([^)]*,\s*['\"][^'\"]*[wax+]")
_PYTHON_WRITE_PATTERNS = (
    ".write_text(", ".unlink(", ".rename(", ".mkdir(", ".rmdir(",
    "shutil.move", "shutil.copy", "shutil.copytree", "shutil.rmtree",
    "os.remove", "os.unlink", "os.rename", "os.replace", "os.makedirs",
)
_BASH_MUTATION_COMMAND_RE = re.compile(
    r"(?:^|[;&|]\s*)(?:rm|rmdir|mv|cp|mkdir|touch|tee|patch)(?:\s|$)"
    r"|(?:^|[;&|]\s*)sed\s+(?:-[^\s;&|]*i[^\s;&|]*|--in-place(?:[=\s]|$))"
    r"|(?:^|[;&|]\s*)cat\s*(?:>|>>)"
    r"|\bgit\s+(?:add|rm|checkout|restore)\b",
    re.IGNORECASE,
)
_GIT_TAG_READONLY_OPTIONS_WITH_VALUE = {
    "--sort", "--format", "--points-at", "--contains", "--no-contains",
    "--merged", "--no-merged", "--column", "--color",
}
_GIT_TAG_READONLY_FLAGS = {
    "-l", "--list", "-n", "--ignore-case", "--no-column", "--no-color",
}
_GIT_TAG_MUTATION_FLAGS = {
    "-a", "--annotate", "-s", "--sign", "-u", "--local-user", "-f",
    "--force", "-d", "--delete",
}
_GIT_TAG_RE = re.compile(r"\bgit\s+tag\b([^;&|]*)", re.IGNORECASE)
_OPERATOR_ONLY_OFFICIAL_BOOTSTRAP_MARKERS = (
    "--acknowledge-one-time-first-strict-control",
    "--acknowledge-publish-first-strict",
    "bootstrap-first-strict",
    "bootstrap_first_strict",
    "finalize-first-strict",
    "finalize_first_strict",
    "official_bootstrap.py",
    "official_bootstrap_control.json",
    "import official_bootstrap",
    "from official_bootstrap",
)
_LLM_AVAILABILITY_CONTROL_MARKERS = (
    "pok_llm_resume_evidence_digest",
    "llm_availability_store",
    "llm_availability_pause.json",
    "reconcile_llm_pause",
    "resume_llm_pause",
    "consume_operator_resume_ack",
    "persist_llm_pause",
)
_STRICT_AUTHORITY_CONTROL_MARKERS = (
    "strict_authority_workflow",
    "strict-role-accepted",
    "accept_role_result",
    "dispatch_call",
    "complete_provider_call",
    "strictroleaccepted",
    "strictproviderresultobserved",
)


def _strip_heredoc_bodies(command: str) -> str:
    """Remove heredoc bodies so quoted code comparisons are not shell redirects."""
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


def _shell_heredoc_delimiters(line: str) -> list[str]:
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


def _read_shell_token(text: str, start: int) -> tuple[str, int]:
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
        if ch.isspace() or ch in ";&|":
            break
        token.append(ch)
        i += 1
    return "".join(token), i


def _iter_shell_write_redirect_targets(command: str):
    text = _strip_heredoc_bodies(command)
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
        target, end = _read_shell_token(text, j)
        if target:
            yield target
        i = max(end, j + 1)


def _bash_has_file_write_redirect(command: str) -> bool:
    for target in _iter_shell_write_redirect_targets(command):
        target = target.strip("'\"")
        target_low = target.lower()
        if target.startswith("&"):
            continue
        if target_low in _SAFE_REDIRECT_TARGETS:
            continue
        if target_low.startswith(_SAFE_REDIRECT_PREFIXES):
            continue
        return True
    return False


def _python_snippet_is_mutating(command: str) -> bool:
    low = str(command).lower()
    if "python" not in low:
        return False
    if _PYTHON_OPEN_WRITE_RE.search(low):
        return True
    return any(pattern in low for pattern in _PYTHON_WRITE_PATTERNS)


def _bash_has_mutation_command(command: str) -> bool:
    """Detect mutating shell commands without matching harmless prose like 'confirm '."""
    return bool(_BASH_MUTATION_COMMAND_RE.search(str(command)))


def _git_tag_invocation_is_mutating(args: list[str]) -> bool:
    if not args:
        return False

    list_mode = False
    i = 0
    while i < len(args):
        arg = args[i]
        low = arg.lower()

        if low in _GIT_TAG_MUTATION_FLAGS:
            return True
        if low.startswith("--delete=") or low.startswith("--force="):
            return True
        if low in _GIT_TAG_READONLY_FLAGS or low.startswith("-n"):
            if low in {"-l", "--list"}:
                list_mode = True
            i += 1
            continue
        if any(low.startswith(opt + "=") for opt in _GIT_TAG_READONLY_OPTIONS_WITH_VALUE):
            i += 1
            continue
        if low in _GIT_TAG_READONLY_OPTIONS_WITH_VALUE:
            i += 2
            continue
        if list_mode:
            i += 1
            continue
        if low.startswith("-"):
            return True
        return True

    return False


def _git_tag_is_mutating(command: str) -> bool:
    for match in _GIT_TAG_RE.finditer(str(command)):
        rest = match.group(1).strip()
        if not rest:
            continue
        try:
            args = shlex.split(rest)
        except ValueError:
            args = rest.split()
        if _git_tag_invocation_is_mutating(args):
            return True
    return False


def _orchestrator_bash_is_mutation(command: str) -> bool:
    """True if a Bash command writes/deletes/edits files rather than inspecting."""
    low = str(command).lower()
    if _bash_has_file_write_redirect(command):
        return True
    if _python_snippet_is_mutating(command):
        return True
    if _bash_has_mutation_command(command):
        return True
    if "git commit" in low or "git push" in low:
        return True
    if _git_tag_is_mutating(command):
        return True
    return False


def _orchestrator_bash_targets_operator_only_bootstrap(command: str) -> bool:
    """Reject every main-LLM Bash route to the one-time formal ceremony.

    This is intentionally independent of file-mutation detection and the
    current pipeline stage.  Both ``bootstrap-first-strict`` and
    ``finalize-first-strict`` are operator actions whose durable side effects
    are outside the ordinary
    orchestrator tool route, even when its command text contains no shell
    redirect or other syntactic write marker.
    """
    low = str(command).lower().replace("\\", "/")
    return any(
        marker in low for marker in _OPERATOR_ONLY_OFFICIAL_BOOTSTRAP_MARKERS
    )


def _orchestrator_bash_targets_llm_availability_control(command: str) -> bool:
    """Reject main-LLM shell access to provider pause/resume authority."""

    low = str(command).lower().replace("\\", "/")
    return any(marker in low for marker in _LLM_AVAILABILITY_CONTROL_MARKERS)


def _orchestrator_bash_targets_strict_authority_control(command: str) -> bool:
    """Reject shell/import access to first-strict execution authority."""

    low = str(command).lower().replace("\\", "/")
    return any(marker in low for marker in _STRICT_AUTHORITY_CONTROL_MARKERS)
