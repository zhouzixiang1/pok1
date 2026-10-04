"""Codex CLI transport adapter for the run_claude_query chokepoint.

``POK_LLM_TRANSPORT`` selects the provider process transport (default
``claude``; ``codex`` spawns a one-shot ``codex exec`` process per attempt,
exit-on-completion).  The adapter's single responsibility is to make a codex
process look like the claude_agent_sdk stream the rest of the control plane
already consumes, so every downstream contract is preserved by construction:

- **Event shape**: codex ``--json`` JSONL events (``thread.started`` /
  ``turn.started`` / ``item.completed`` / ``turn.completed`` / ``turn.failed`` /
  ``error``) are translated into the exact SDK message objects
  (``SystemMessage`` / ``AssistantMessage([TextBlock|ThinkingBlock])`` /
  ``UserMessage([ToolResultBlock])`` / ``ResultMessage``) that
  ``llm_query_retry._process_stream`` already handles — role-IO evidence,
  stall/idle/first-activity timeout policy, availability classification, and
  ``llm_call_metrics`` recording therefore run unchanged.
- **Usage mapping**: codex ``turn.completed.usage``
  (``input_tokens``/``output_tokens``/``cached_input_tokens``/
  ``cache_write_input_tokens``/``reasoning_output_tokens``) maps onto the SDK
  usage keys (``input_tokens``/``output_tokens``/``cache_read_input_tokens``/
  ``cache_creation_input_tokens``) plus the raw vendor keys, so
  ``llm_call_metrics.jsonl`` keeps its exact schema; missing fields are 0.
- **Cost**: codex reports no cost; ``ResultMessage.total_cost_usd`` stays
  ``None`` (cost fields are nullable in metrics/billing).
- **Error classification**: provider failures surface as ``ClaudeSDKError``
  whose text carries the raw codex/provider error body (JSON event payload or
  stderr tail), so ``classify_llm_availability`` keeps parsing GLM 1302/1308
  patterns and the quota pause chain is untouched.
- **Ownership/cleanup**: :class:`CodexExecTransport` duck-types the SDK
  subprocess transport surface the provider-attempt lifecycle relies on
  (``_process`` attribute + awaitable ``close()``), killing the process
  *group* and proving exit via ``returncode``.  A one-shot process has no
  persistent session, so timeout/cancel is naturally attempt-local.

Capability differences vs the claude transport (documented in AGENTS.md):

- Sandbox/write scope (P7-1, 2026-10-04; hardened by review block B1):
  a role that declared an Edit/Write tool AND supplied a resolvable
  ``allowed_write_dir`` runs under ``-s workspace-write`` with the scope's
  directory roots in ``sandbox_workspace_write.writable_roots`` AND the
  process cwd pinned to the scope's primary root. Codex workspace-write
  makes the ENTIRE cwd tree writable and writable_roots only ADD to it
  (live-CLI-proven), so the cwd pin is what collapses the writable surface
  to the declared scope union plus the codex-built-in system temp tree;
  the repository tree is NOT writable under a write-role dispatch
  (smoke-proven: a probe path inside the repo is rejected with
  ``Read-only file system``). Reads stay full-disk and codex still
  discovers the repository AGENTS.md by ancestor walk from the deep cwd
  (smoke-proven with a git-root instruction file honoured from a nested
  working directory). Every dispatch without a provable write declaration
  keeps the exact historical ``-s read-only`` argv and project-root cwd.
  Within-scope file-level precision stays with the lease-tree
  ``audit_worker_boundary`` audit — which is now also the entire writable
  surface, so the audit and the sandbox agree. A declared-but-unresolvable
  scope fails closed (:class:`CodexWriteScopeUnresolvable`) instead of
  silently degrading to read-only.
- Effort mapping: ``POK_LLM_EFFORT`` (official GLM档位 ``low``/``high``/
  ``max``) is forwarded as ``-c model_reasoning_effort=...`` (default
  ``max``).  ``POK_LLM_THINKING_BUDGET`` is not forwarded (codex has no
  fixed-budget CLI switch, mirroring the GLM-5.3 effort-only rule).
- Endpoint/model/wire_api/auth come from the operator's ``~/.codex/config.toml``
  (official GLM Coding Plan page: base_url ``https://open.bigmodel.cn/api/v1``,
  ``wire_api="responses"``, ``experimental_bearer_token``); ``POK_CODEX_MODEL``
  may override the model per dispatch.  The binary is pre-resolved via
  ``shutil.which`` before every spawn (:func:`resolve_codex_binary`):
  PATH first, then — for BARE names only — the user-local install base
  ``$HOME/.local/bin`` (the service PATH excludes ``~/.local/bin`` while
  the codex CLI user install lives exactly there, so the transport runs
  under the committed service environment with no operator-added env key).
  An
  unresolvable binary raises an actionable error that
  ``classify_llm_availability`` rates as NO availability issue (never a
  1302/1308/quota pause; the dispatch path surfaces it as
  ``ClaudeSDKError``), and the web lifespan fails fast at startup via
  :func:`assert_codex_transport_ready` when the codex transport is selected
  but the binary is missing from the service PATH.  The preflight surface
  (transport selection + binary resolution) is stdlib-only and importable
  by ANY python3 — claude_agent_sdk is imported lazily-guarded, so a
  service-environment smoke or systemd ExecStartPre that resolves
  ``python3`` from the service PATH still gets the binary diagnosis
  instead of ``ModuleNotFoundError`` (2026-10-04); SDK-typed entries fail
  closed via :func:`_require_sdk` with the POK_PYTHON guidance.
"""

from __future__ import annotations

import asyncio
import contextlib
import collections
import json
import logging
import os
import shutil
import signal

try:
    from claude_agent_sdk import (
        AssistantMessage,
        UserMessage,
        SystemMessage,
        TextBlock,
        ThinkingBlock,
        ToolResultBlock,
        ResultMessage,
        ClaudeSDKError,
    )

    _SDK_IMPORT_ERROR = None
except ImportError as _sdk_exc:  # SDK-free interpreter (see below)
    # The transport preflight surface of this module (codex_transport_enabled
    # / codex_binary_from_env / resolve_codex_binary /
    # assert_codex_transport_ready) must stay importable by ANY python3:
    # service-environment smokes, systemd ExecStartPre, and operator deploy
    # checks resolve ``python3`` from the service PATH, which does NOT carry
    # the venv's claude_agent_sdk (2026-10-04 smoke: ``ModuleNotFoundError:
    # No module named 'claude_agent_sdk'`` replaced the binary diagnosis the
    # preflight exists to produce).  No placeholder SDK types are bound —
    # every SDK-typed entry point calls _require_sdk() and fails with an
    # actionable chained error instead of constructing broken objects.
    _SDK_IMPORT_ERROR = _sdk_exc

#: Transport selector env var.  ``claude`` (default) keeps the exact existing
#: SDK path; ``codex`` routes the dispatch through this adapter.
TRANSPORT_ENV = "POK_LLM_TRANSPORT"

#: Binary override (default ``codex``).  Resolved at spawn time by
#: :func:`resolve_codex_binary`: ``shutil.which`` first (honours a directory
#: component in the value), then — for BARE names only — the standard
#: user-local install bin dir ``$HOME/.local/bin`` (the service PATH never
#: includes it, but the codex CLI user install lives exactly there).
#: A value carrying a directory component is authoritative and gets no
#: fallback.
CODEX_BIN_ENV = "POK_CODEX_BIN"

#: Optional per-dispatch model override forwarded as ``-c model=<v>``.
CODEX_MODEL_ENV = "POK_CODEX_MODEL"

#: Grace (seconds) for SIGTERM→SIGKILL escalation in transport close.
CODEX_CLOSE_GRACE_ENV = "POK_CODEX_CLOSE_GRACE_SEC"

#: model_reasoning_effort档位 (official GLM Coding Plan levels).
_CODEX_EFFORT_LEVELS = ("low", "high", "max")

#: Built-in tool names whose presence means the role may mutate files. Only
#: a role that declared one of these AND supplied a resolvable write scope
#: gets ``-s workspace-write``; everything else stays ``-s read-only``.
_WRITE_TOOL_NAMES = ("Edit", "Write", "NotebookEdit")

#: Config override key carrying the directory-level writable roots under the
#: codex workspace-write sandbox. Verified against the real CLI on this host
#: (2026-10-04 smoke): ``-s workspace-write -c
#: 'sandbox_workspace_write.writable_roots=["/path"]'`` makes exactly that
#: root writable while a control directory outside it (and outside the
#: workspace / temp) stays ``Read-only file system``.
_WRITABLE_ROOTS_CONFIG_KEY = "sandbox_workspace_write.writable_roots"


class CodexWriteScopeUnresolvable(RuntimeError):
    """A write-capable role declared a write scope that resolves to zero
    existing directory roots — a dispatch-configuration error, fail-closed.

    Raised by :func:`codex_writable_roots` (via ``require_resolvable=True``)
    and by :meth:`CodexExecTransport.build_argv` so the failure can never
    silently degrade to a read-only sandbox (the exact v509 failure mode:
    every provider patch rejected, lease bytes frozen, zero-change detector
    blaming the model). The text deliberately matches no GLM 1302/1308/429
    marker, so ``classify_llm_availability`` never arms a pause from it.
    """


def _declares_write_tools(tools) -> bool:
    """True when the declared built-in tool set proves file-mutation intent."""

    if isinstance(tools, (str, bytes)) or not isinstance(tools, (list, tuple, set, frozenset)):
        # A ToolsPreset dict or scalar carries no provable built-in Edit/Write
        # declaration — fail closed to the read-only sandbox.
        return False
    return any(str(tool) in _WRITE_TOOL_NAMES for tool in tools)


def _normalize_write_scope(allowed_write_dir) -> tuple[list[str], list[str]]:
    """Normalize a write scope into (dirs, files) string lists.

    Accepts the dispatch-side normalized mapping (``{"dirs": [...],
    "files": [...]}``) or a scalar path. Missing keys and ``None`` normalize
    to empty lists (no declared scope).
    """

    if allowed_write_dir is None:
        return [], []
    if isinstance(allowed_write_dir, dict):
        dirs = [str(p) for p in (allowed_write_dir.get("dirs") or ())]
        files = [str(p) for p in (allowed_write_dir.get("files") or ())]
        return dirs, files
    if isinstance(allowed_write_dir, (list, tuple)):
        # Bare sequence form: every entry is a directory root.
        return [str(p) for p in allowed_write_dir], []
    return [str(allowed_write_dir)], []


def codex_writable_roots(
    allowed_write_dir, tools=None, *, require_resolvable: bool = False,
    base_dir=None,
) -> list[str]:
    """Resolve the codex directory-level writable roots for one dispatch.

    Codex grants writes at DIRECTORY granularity: the workspace (process
    cwd) PLUS ``sandbox_workspace_write.writable_roots`` PLUS the system
    temp tree — the roots are ADDITIVE to the cwd tree, not a replacement
    (B1 review block, 2026-10-04: a live CLI run wrote inside cwd but
    outside the declared roots). This resolver therefore only describes the
    DECLARED scope; :meth:`CodexExecTransport.spawn` separately pins the
    process cwd to the primary root so the actual writable surface is the
    declared scope union plus the codex-built-in temp tree — never the
    repository working tree. It maps the role's exact write scope onto
    directory roots: every declared directory root, and the PARENT directory
    of every declared exact file. Only roots that exist on disk are
    returned; within-scope file-level precision stays with the lease-tree
    ``audit_worker_boundary`` audit, which is the only writable surface.

    ``tools`` gates on a provable Edit/Write declaration; when omitted or
    write-free the resolver returns ``[]`` (read-only sandbox). Relative
    entries resolve against ``base_dir`` (falling back to the process cwd)
    — a bare relative FILE name must never widen the scope to an unrelated
    directory: its parent is the whole base tree (``dirname(base/policy.py)
    == base``), so under a repo-root base_dir it would silently make the
    entire repository writable; the resolver therefore fails closed with
    :class:`CodexWriteScopeUnresolvable` for any write-tool dispatch that
    declares one (P6, 2026-10-05). With ``require_resolvable=True`` a
    declared-but-unresolvable scope raises
    :class:`CodexWriteScopeUnresolvable` instead of returning ``[]`` so the
    caller can fail fast rather than silently downgrade, and every silently
    non-existing entry dropped from the roots is named in the exception text
    (or logged, when at least one root survived).
    """

    if not _declares_write_tools(tools):
        return []
    dirs, files = _normalize_write_scope(allowed_write_dir)
    if not dirs and not files:
        return []
    base = str(base_dir) if base_dir else os.getcwd()

    bare_files = sorted(
        {
            str(file)
            for file in files
            if not os.path.isabs(str(file))
            and os.path.basename(str(file)) == str(file)
        }
    )
    if bare_files:
        raise CodexWriteScopeUnresolvable(
            "codex transport write scope declared bare relative file "
            f"name(s) {bare_files!r}: a bare file name resolves against "
            f"base_dir {base!r}, whose whole tree would become writable via "
            "its parent directory — that widens the declared write scope "
            "(live repro 2026-10-05: files=['policy.py'] with a repo-root "
            "base_dir returned the repository root as the writable root). "
            "Qualify the file with its directory (e.g. "
            "'lease_dir/policy.py') or declare an absolute path."
        )

    def _absolute(candidate: str) -> str:
        if os.path.isabs(candidate):
            return candidate
        return os.path.join(base, candidate)

    roots: list[str] = []
    dropped: list[str] = []
    candidates = [_absolute(str(dir_)) for dir_ in dirs] + [
        os.path.dirname(_absolute(str(file))) for file in files
    ]
    for candidate in candidates:
        if not candidate:
            continue
        if not os.path.isdir(candidate):
            dropped.append(candidate)
            continue
        root = os.path.realpath(candidate)
        if root not in roots:
            roots.append(root)
    if dropped and require_resolvable:
        logging.getLogger(__name__).warning(
            "codex write scope silently narrowed: declared entries resolved "
            "to non-existing directories and were dropped: %r (surviving "
            "roots: %r)", dropped, roots,
        )
    if not roots and require_resolvable:
        raise CodexWriteScopeUnresolvable(
            "codex transport write scope declared but unresolvable: "
            f"dirs={dirs!r} files={files!r} resolve to zero existing "
            f"directory roots (dropped non-existing entries: {dropped!r}); "
            "the dispatch cannot build the workspace-write writable_roots "
            "argv and would silently degrade to the read-only sandbox "
            "(every provider patch rejected, lease bytes frozen). Fix the "
            "lease/workspace directory creation or the declared "
            "allowed_write_dir before dispatching this role."
        )
    return roots

#: Role-IO/tool-result preview bound (mirrors _process_stream previews).
_TOOL_RESULT_PREVIEW_CHARS = 3000

#: Maximum stderr tail lines retained for provider-error text.
_STDERR_TAIL_LINES = 20

#: StreamReader limit for the codex exec stdout/stderr pipes.
#:
#: The asyncio default (64 KiB) caps ONE JSONL line, and a codex
#: ``item.completed`` event can embed a large diff or command output: the
#: 2026-10-04 journal showed four whole one-shot streams voided in 20 minutes
#: by ``LimitOverrunError: Separator is found, but chunk is longer than
#: limit``.  8 MiB sits far above any observed event while keeping the
#: bounded-reader guarantee; :class:`_UnboundedLineReader` additionally
#: reassembles the (pathological) even-longer line instead of crashing.
_STREAM_LINE_LIMIT = 8 * 1024 * 1024

#: Markers of a benign codex item-error text. codex emits
#: "Model metadata for <model> not found. Defaulting to fallback metadata;
#: this can degrade performance and cause issues." for any non-built-in model
#: name and then continues executing normally — that item must NOT kill the
#: stream (real-smoke regression: a healthy glm-5.2 run died at this item).
_BENIGN_ITEM_ERROR_MARKERS = ("model metadata", "not found", "defaulting")

#: Bound on distinct benign warnings retained/surfaced per stream.
_MAX_WARNINGS = 8


def _is_benign_item_error(text: str) -> bool:
    """True for the known-benign model-metadata fallback warning."""

    lowered = str(text or "").lower()
    return all(marker in lowered for marker in _BENIGN_ITEM_ERROR_MARKERS)


def codex_transport_enabled() -> bool:
    """True when ``POK_LLM_TRANSPORT=codex`` selects this adapter."""

    return str(os.environ.get(TRANSPORT_ENV, "claude") or "claude").strip().lower() == "codex"


def codex_effort_from_env() -> str:
    """Map ``POK_LLM_EFFORT`` onto the official codex reasoning档位.

    ``max`` stays ``max`` (the production default); ``high`` stays ``high``;
    every other value (``medium``, ``low``, unset) maps to ``low`` — the
    official models.json declares exactly low/high/max, so an unknown value
    must fail closed to the lightest tier rather than be forwarded verbatim.
    """

    raw = str(os.environ.get("POK_LLM_EFFORT", "max") or "max").strip().lower()
    if raw in _CODEX_EFFORT_LEVELS:
        return raw
    return "max" if raw in ("", "max") else "low"


def codex_binary_from_env() -> str:
    return str(os.environ.get(CODEX_BIN_ENV, "codex") or "codex")


#: Standard user-local install bin directory (pip ``--user`` base) consulted
#: when a BARE binary name is absent from PATH.  The committed service PATH
#: (``deploy/tencent-cloud/env.runtime``) intentionally excludes it, but the
#: runtime always has ``HOME`` and the codex CLI user install lives exactly
#: there on the cloud VM — so the codex transport must resolve it without an
#: operator-added env key (2026-10-04 service-env smoke: HOME/PATH/
#: POK_LLM_TRANSPORT alone failed at the preflight with a PATH-only lookup).
_USER_LOCAL_BIN_DIRNAME = ".local/bin"


class CodexBinaryNotFound(RuntimeError):
    """The configured codex binary is unresolvable — a service-CONFIG error.

    Raised by :func:`resolve_codex_binary` / :func:`assert_codex_transport_ready`
    under ANY interpreter (stdlib-only path, no claude_agent_sdk needed).
    The dispatch path (:meth:`CodexExecTransport.spawn`) re-raises the same
    text as ``ClaudeSDKError`` so the retry loop and availability classifier
    see the SDK error type they already handle.
    """


def _codex_binary_guidance(configured: str, attempted=None) -> str:
    tried = ", ".join(repr(path) for path in (attempted or [configured]))
    return (
        "codex binary not found in service PATH: none of "
        + tried
        + " is executable (PATH="
        + str(os.environ.get("PATH") or "")
        + "; bare names also try $HOME/"
        + _USER_LOCAL_BIN_DIRNAME
        + "); set POK_CODEX_BIN to an absolute path (e.g. "
        "POK_CODEX_BIN=/home/ubuntu/.local/bin/codex) or append its "
        "directory to PATH= in deploy/tencent-cloud/env.runtime, then "
        "restart pok-evolution"
    )


def _user_local_bin_candidates(name: str) -> list:
    """Bare-name fallback paths under the user-local install base."""

    home = str(os.environ.get("HOME") or "").strip()
    if not home or not name:
        return []
    return [os.path.join(home, _USER_LOCAL_BIN_DIRNAME, name)]


def resolve_codex_binary() -> str:
    """Resolve the configured codex binary to an absolute executable path.

    Resolution order: (1) ``shutil.which`` honours a directory component in
    ``POK_CODEX_BIN`` (absolute or relative path checked directly) and
    otherwise searches the process ``PATH``; (2) for a BARE name only, the
    standard user-local install bin dir ``$HOME/.local/bin`` (the pip
    ``--user`` base) is tried next — again through ``shutil.which``, so
    executability is proven.  The committed service ``PATH``
    (``deploy/tencent-cloud/env.runtime``) intentionally excludes
    ``~/.local/bin`` while the codex CLI user install lives exactly there,
    so the transport must resolve it from ``HOME`` with no operator-added
    env key (2026-10-04 service-env smoke: HOME/PATH/POK_LLM_TRANSPORT
    alone failed a PATH-only lookup at the preflight).  A ``POK_CODEX_BIN``
    carrying a directory component is authoritative and gets NO fallback.
    An unresolvable binary is a *service-configuration* failure, not
    provider unavailability: the raised error text deliberately matches no
    GLM 1302/1308/429/503/529 marker, so ``classify_llm_availability``
    returns ``None`` for it and no durable cooldown/quota pause is ever
    armed (2026-10-03 incident: every dispatch died as a bare
    ``FileNotFoundError: 'codex'`` and burned v490/v491/v492 while a stale
    claude-era 1302 pause took the blame).

    Stdlib-only: safe to call from any python3 (smoke / ExecStartPre)
    regardless of claude_agent_sdk availability.
    """

    configured = codex_binary_from_env()
    attempted = [configured]
    resolved = shutil.which(configured)
    if not resolved and not os.path.dirname(configured):
        # Bare name absent from PATH: try the user-local install base before
        # failing (the service PATH never includes ~/.local/bin).
        for candidate in _user_local_bin_candidates(configured):
            attempted.append(candidate)
            resolved = shutil.which(candidate)
            if resolved:
                break
    if resolved:
        return resolved
    raise CodexBinaryNotFound(_codex_binary_guidance(configured, attempted))


def _resolve_binary_or_sdk_error() -> str:
    """Dispatch-path resolver: the spawn failure must surface as
    ``ClaudeSDKError`` (the type the retry loop, provider-attempt lifecycle,
    and availability classifier already handle).  Under an SDK-free
    interpreter the actionable :class:`CodexBinaryNotFound` itself
    propagates — identical text, no SDK required."""

    try:
        return resolve_codex_binary()
    except CodexBinaryNotFound as exc:
        if _SDK_IMPORT_ERROR is not None:
            raise
        raise ClaudeSDKError(str(exc)) from exc


def assert_codex_transport_ready():
    """Startup preflight for the codex transport (web lifespan entry point).

    Returns the resolved absolute binary path when the codex transport is
    selected, ``None`` otherwise.  Raises the actionable
    :class:`CodexBinaryNotFound` at STARTUP when ``POK_LLM_TRANSPORT=codex``
    is configured but the binary cannot be resolved, so the
    misconfiguration is a diagnosable boot failure instead of one opaque
    per-role spawn error per dispatched role.  Stdlib-only: runnable by ANY
    python3 (service-environment smoke, systemd ExecStartPre) — it must NOT
    depend on claude_agent_sdk, because the smoke interpreter resolves
    ``python3`` from the service PATH, which does not carry the venv
    (2026-10-04: the preflight itself died with ``ModuleNotFoundError: No
    module named 'claude_agent_sdk'`` before producing the diagnosis).
    """

    if not codex_transport_enabled():
        return None
    return resolve_codex_binary()


def _require_sdk(entry: str) -> None:
    """Fail closed (with guidance) on SDK-typed entries under an SDK-free
    interpreter, instead of NameError/TypeError from unbound SDK names."""

    if _SDK_IMPORT_ERROR is not None:
        raise RuntimeError(
            f"{entry} requires claude_agent_sdk (import failed: "
            f"{_SDK_IMPORT_ERROR}); run under the service interpreter "
            "(POK_PYTHON=/home/ubuntu/pok1/.venv/bin/python). The transport "
            "preflight assert_codex_transport_ready() is SDK-free and works "
            "under any python3."
        ) from _SDK_IMPORT_ERROR


def codex_metrics_model() -> str:
    """Model label recorded in llm_call_metrics under the codex transport."""

    return str(os.environ.get(CODEX_MODEL_ENV, "") or "").strip() or "codex"


def _close_grace_sec() -> float:
    try:
        return max(0.1, min(30.0, float(os.environ.get(CODEX_CLOSE_GRACE_ENV, "10"))))
    except (TypeError, ValueError):
        return 10.0


def _project_root(options) -> str:
    cwd = getattr(options, "cwd", None)
    if cwd:
        return str(cwd)
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _signal_process_group(process, sig) -> None:
    """Signal the spawned process group (single process fallback)."""

    pid = getattr(process, "pid", None)
    if not pid:
        return
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        pgid = None
    try:
        if pgid is not None and pgid == pid:
            os.killpg(pgid, sig)
        else:
            process.send_signal(sig)
    except (ProcessLookupError, PermissionError, OSError):
        # Already gone: returncode proof below is the authority.
        pass


def _usage_from_codex(usage_raw) -> dict:
    """Map codex turn usage onto the SDK usage keys (missing fields = 0).

    Keeps the raw vendor keys (``cached_input_tokens``,
    ``cache_write_input_tokens``, ``reasoning_output_tokens``) alongside the
    canonical four so ``raw_usage`` in llm_call_metrics preserves them.
    """

    data = usage_raw if isinstance(usage_raw, dict) else {}

    def _int(key):
        try:
            return int(data.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    sdk_usage = {
        "input_tokens": _int("input_tokens"),
        "output_tokens": _int("output_tokens"),
        "cache_creation_input_tokens": _int("cache_write_input_tokens"),
        "cache_read_input_tokens": _int("cached_input_tokens"),
    }
    # Vendor-extension keys are always present (0 when absent) so downstream
    # consumers never see a shape that depends on which fields codex emitted.
    for key in (
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "reasoning_output_tokens",
    ):
        sdk_usage[key] = _int(key)
    return sdk_usage


class CodexEventTranslator:
    """Translate codex ``--json`` JSONL events into SDK stream messages.

    Pure state machine over decoded event dicts; the transport owns process
    I/O.  Fatal provider events (``turn.failed`` / ``error`` / item type
    ``error``) record ``fatal_error`` instead of raising so the transport can
    fold stderr context into the single ``ClaudeSDKError`` it raises.  Two
    known-benign shapes are downgraded to ``warnings`` and surfaced once as a
    non-substantive ``SystemMessage`` instead: codex's model-metadata fallback
    item error (``codex_item_warning``) and the "Reconnecting... k/5" stream
    error (``codex_reconnecting``) that precedes codex's own retry — the
    latter keeps its provider body in ``last_stream_error`` so a terminal
    no-completion failure preserves the original GLM 1302/1308 semantics.
    """

    def __init__(self, model_label: str):
        _require_sdk("CodexEventTranslator (codex event translation)")
        self.model_label = str(model_label or "codex")
        self.thread_id = None
        self.final_text = ""
        self.usage_raw = None
        self.saw_turn_completed = False
        self.fatal_error = None
        self.warnings = []
        self.last_stream_error = None
        self.unknown_event_types = collections.Counter()

    def _assistant(self, blocks):
        return AssistantMessage(content=blocks, model=self.model_label)

    def _tool_result(self, item, *, is_error=None) -> UserMessage:
        try:
            payload = json.dumps(item, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            payload = str(item)
        if len(payload) > _TOOL_RESULT_PREVIEW_CHARS:
            payload = payload[:_TOOL_RESULT_PREVIEW_CHARS] + "…[truncated]"
        return UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id=str(item.get("id") or ""),
                    content=payload,
                    is_error=bool(is_error),
                )
            ],
        )

    def translate(self, event) -> list:
        """Return the SDK messages for one decoded codex event."""

        if not isinstance(event, dict):
            return []
        etype = str(event.get("type") or "")
        if etype == "thread.started":
            self.thread_id = str(event.get("thread_id") or "") or None
            return [
                SystemMessage(
                    subtype="init",
                    data={"thread_id": self.thread_id},
                )
            ]
        if etype == "turn.started":
            # Codex turn bookkeeping, not model output — keep the B2 contract
            # (non-substantive; does not lift first_activity_timeout).
            return [
                SystemMessage(subtype="codex_turn_started", data={})
            ]
        if etype in ("item.started", "item.updated"):
            # Streaming deltas — no complete item to evidence yet.
            return []
        if etype == "item.completed":
            return self._translate_item(event.get("item") or {})
        if etype == "turn.completed":
            self.saw_turn_completed = True
            self.usage_raw = event.get("usage") or {}
            return self._result_messages()
        if etype == "turn.failed":
            error = event.get("error")
            self.fatal_error = (
                f"codex turn.failed: {error.get('message') if isinstance(error, dict) else error}"
            )
            return []
        if etype == "error":
            message_text = str(event.get("message") or "")
            if not message_text:
                try:
                    message_text = json.dumps(
                        event, ensure_ascii=False, default=str
                    )
                except (TypeError, ValueError):
                    message_text = str(event)
            if "reconnecting" in message_text.lower():
                # codex emits "Reconnecting... k/5 (stream disconnected before
                # completion: <provider body>)" and then retries on its own.
                # Killing the stream here would rob codex of its retries; the
                # text is kept verbatim in last_stream_error so a terminal
                # no-completion failure still carries the original GLM
                # 1302/1308 semantics for classify_llm_availability.
                self.last_stream_error = message_text
                if (
                    message_text not in self.warnings
                    and len(self.warnings) < _MAX_WARNINGS
                ):
                    self.warnings.append(message_text)
                    return [
                        SystemMessage(
                            subtype="codex_reconnecting",
                            data={"message": message_text},
                        )
                    ]
                return []
            self.fatal_error = f"codex stream error: {message_text}"
            return []
        self.unknown_event_types[etype] += 1
        return []

    def _translate_item(self, item) -> list:
        itype = str(item.get("type") or "")
        if itype == "agent_message":
            text = str(item.get("text") or "")
            self.final_text += text
            return [self._assistant([TextBlock(text=text)])]
        if itype == "reasoning":
            thinking = str(item.get("text") or item.get("summary") or "[thinking...]")
            return [self._assistant([ThinkingBlock(thinking=thinking, signature="")])]
        if itype == "error":
            text = str(item.get("message") or "")
            if not text:
                try:
                    text = json.dumps(item, ensure_ascii=False, default=str)
                except (TypeError, ValueError):
                    text = str(item)
            if _is_benign_item_error(text):
                # codex warns about non-built-in model metadata ("Model
                # metadata for <model> not found. Defaulting to fallback
                # metadata...") and then continues executing normally.  This
                # is not a provider failure: record it and surface it exactly
                # once, then stream on.
                if text not in self.warnings and len(self.warnings) < _MAX_WARNINGS:
                    self.warnings.append(text)
                    return [
                        SystemMessage(
                            subtype="codex_item_warning",
                            data={"message": text},
                        )
                    ]
                return []
            self.fatal_error = f"codex item error: {text}"
            return []
        if itype == "command_execution":
            exit_code = item.get("exit_code")
            return [
                self._tool_result(
                    {
                        "id": item.get("id"),
                        "type": itype,
                        "command": item.get("command"),
                        "exit_code": exit_code,
                        "duration_ms": item.get("duration_ms"),
                    },
                    is_error=bool(exit_code),
                )
            ]
        if itype == "file_change":
            return [
                self._tool_result(
                    {
                        "id": item.get("id"),
                        "type": itype,
                        "changes": item.get("changes"),
                    },
                    is_error=False,
                )
            ]
        if itype:  # mcp_tool_call / web_search / todo_list / future kinds
            return [
                self._tool_result(
                    {"id": item.get("id"), "type": itype, "item": item},
                    is_error=False,
                )
            ]
        return []

    def _result_messages(self, *, duration_ms=None) -> list:
        """Build the terminal ResultMessage (+ thinking telemetry) for one turn."""

        messages = []
        sdk_usage = _usage_from_codex(self.usage_raw)
        reasoning_tokens = 0
        try:
            reasoning_tokens = int(
                (self.usage_raw or {}).get("reasoning_output_tokens") or 0
            )
        except (TypeError, ValueError):
            reasoning_tokens = 0
        if reasoning_tokens > 0:
            messages.append(
                SystemMessage(
                    subtype="thinking_tokens",
                    data={
                        "estimated_tokens": reasoning_tokens,
                        "estimated_tokens_delta": reasoning_tokens,
                    },
                )
            )
        duration = int(duration_ms) if duration_ms else 0
        messages.append(
            ResultMessage(
                subtype="success",
                duration_ms=duration,
                duration_api_ms=duration,
                is_error=False,
                num_turns=1,
                session_id=self.thread_id or "",
                total_cost_usd=None,
                usage=sdk_usage,
                result=(self.final_text or None),
                api_error_status=None,
            )
        )
        return messages


class CodexExecTransport:
    """One-shot ``codex exec`` process owner.

    Duck-types the subprocess-CLI transport surface used by the
    provider-attempt lifecycle: ``_process`` (asyncio subprocess once
    spawned) and an awaitable ``close()`` that terminates the process group
    and leaves ``returncode`` set, so exit confirmation
    (``_provider_attempt_exit_confirmed``) works unchanged.

    ``allowed_write_dir`` (P7-1, 2026-10-04; hardened by review block B1)
    carries the dispatch-declared write scope into the sandbox surface:

    - argv: with a provable Edit/Write tool declaration AND a resolvable
      scope the sandbox becomes ``-s workspace-write`` with the scope's
      directory roots in ``sandbox_workspace_write.writable_roots``
      (verified CLI syntax, real-smoke proven). Anything else keeps the
      exact historical ``-s read-only`` argv.
    - cwd: for a write-role dispatch the process cwd is pinned to the
      FIRST resolved write root (the lease workspace). Codex's
      workspace-write sandbox makes the ENTIRE cwd tree writable and
      writable_roots only ADD to it (B1, live-CLI-proven), so a repo-root
      cwd would have exposed the whole service checkout — including
      web/core contracts, ``.git``, and the gitignored pipeline state —
      to untrusted Worker output while the claude-path PreToolUse write
      guard does not apply to codex. Pinning the cwd collapses the
      writable surface to the declared scope union plus the codex-built-in
      system temp tree; the repository tree is NOT writable. Reads stay
      full-disk (smoke-proven), and codex still discovers the repository
      AGENTS.md by ancestor walk from the deep cwd (smoke-proven with a
      git-root instruction file honoured from a nested working directory).
    """

    def __init__(self, full_prompt, options, allowed_write_dir=None):
        self._prompt = str(full_prompt or "")
        self._options = options
        self._allowed_write_dir = allowed_write_dir
        self._process = None
        self._argv = None

    def _resolved_write_roots(self) -> list:
        return codex_writable_roots(
            self._allowed_write_dir,
            tools=getattr(self._options, "tools", None),
            require_resolvable=True,
            base_dir=_project_root(self._options),
        )

    def _workspace_cwd(self) -> str:
        """Process cwd: the PRIMARY write root for write-role dispatches.

        Read-only dispatches keep the historical project root (nothing is
        writable anyway and the repo-root cwd preserves codex's AGENTS.md
        discovery exactly as before)."""
        roots = self._resolved_write_roots()
        return roots[0] if roots else _project_root(self._options)

    def build_argv(self) -> list:
        """Codex exec argv; prompt arrives on stdin (``-``)."""

        argv = [
            codex_binary_from_env(),
            "exec",
            "--json",
            "--ephemeral",
        ]
        write_roots = self._resolved_write_roots()
        if write_roots:
            argv += ["-s", "workspace-write"]
            # The value side must parse as TOML; json.dumps of a list of
            # plain strings is a valid TOML array of basic strings.
            argv += [
                "-c",
                f"{_WRITABLE_ROOTS_CONFIG_KEY}={json.dumps(write_roots)}",
            ]
        else:
            argv += ["-s", "read-only"]
        argv += [
            "--skip-git-repo-check",
            "-c",
            f"model_reasoning_effort={codex_effort_from_env()}",
        ]
        model = str(os.environ.get(CODEX_MODEL_ENV, "") or "").strip()
        if model:
            argv += ["-c", f"model={model}"]
        argv.append("-")
        return argv

    async def spawn(self):
        if self._process is not None:
            raise RuntimeError("codex transport process already spawned")
        # Resolve the binary BEFORE exec: the service PATH may not contain
        # the user-local install directory, and a bare name PATH cannot
        # resolve would otherwise die as an opaque per-role
        # FileNotFoundError inside every dispatch.  An unresolvable binary
        # raises an actionable ClaudeSDKError here (classified as no LLM
        # availability issue — never a 1302/1308/quota pause), and the
        # resolved absolute path keeps the exec independent of the child
        # PATH regardless of how the service environment mutates later.
        resolved_binary = _resolve_binary_or_sdk_error()
        self._argv = self.build_argv()
        self._argv[0] = resolved_binary
        self._process = await asyncio.create_subprocess_exec(
            *self._argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # B1: the workspace-write sandbox exposes the ENTIRE cwd tree,
            # so a write-role dispatch pins the cwd to its primary declared
            # write root (the lease workspace) — the repository tree stays
            # outside the sandbox write surface. Read-only dispatches keep
            # the historical project root.
            cwd=self._workspace_cwd(),
            # Raised StreamReader limit (see _STREAM_LINE_LIMIT): the 64 KiB
            # asyncio default voids the whole stream when one codex JSONL
            # event line (embedded diff / command output) exceeds it.
            limit=_STREAM_LINE_LIMIT,
            # Own process group so timeout/cancel kills the whole tree
            # (codex may wrap provider helpers in child processes).
            start_new_session=True,
        )
        return self._process

    async def close(self):
        """Terminate the owned process group and prove exit via returncode."""

        process = self._process
        if process is None or process.returncode is not None:
            return True
        _signal_process_group(process, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=_close_grace_sec())
        except (asyncio.TimeoutError, TimeoutError):
            _signal_process_group(process, signal.SIGKILL)
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except (asyncio.TimeoutError, TimeoutError):
                pass
        return process.returncode is not None


def new_codex_exec_transport(
    full_prompt, options, allowed_write_dir=None
) -> CodexExecTransport:
    """Create the codex transport owned by one provider attempt.

    ``allowed_write_dir`` is the dispatch-declared write scope (normalized
    ``{"dirs": [...], "files": [...]}`` mapping or scalar path); see
    :class:`CodexExecTransport` for the sandbox mapping.
    """

    return CodexExecTransport(full_prompt, options, allowed_write_dir)


async def _write_stdin(stdin, data: bytes):
    stdin.write(data)
    await stdin.drain()
    stdin.close()
    with contextlib.suppress(Exception):
        await stdin.wait_closed()


class _UnboundedLineReader:
    """Line reader that survives single lines longer than the StreamReader limit.

    On CPython 3.12 ``StreamReader.readline()`` converts an over-long line
    into ``ValueError`` AND discards the buffered bytes, so an unhandled
    raise at the ``codex_query`` read loop voids the entire one-shot stream
    (2026-10-04 journal: four dead attempts in 20 minutes on
    ``Separator is found, but chunk is longer than limit``).  This reader
    uses ``readuntil()`` instead, which raises
    :class:`asyncio.LimitOverrunError` with the buffer *intact*, then
    reassembles the remainder of the line out of bounded ``read()`` calls,
    which never raise on size.  EOF semantics match ``readline()``: an
    unterminated tail is delivered as the final line, then ``b""``.
    """

    #: Drain chunk for the overrun path (bytes per ``read()``).
    _DRAIN_CHUNK = 64 * 1024

    def __init__(self, stream):
        self._stream = stream
        self._pending = b""

    async def readline(self) -> bytes:
        parts = []
        if self._pending:
            newline = self._pending.find(b"\n")
            if newline >= 0:
                # A carry-over fragment already holds the next complete line.
                line = self._pending[: newline + 1]
                self._pending = self._pending[newline + 1 :]
                return line
            # The carry-over is a prefix of a line whose remainder the stream
            # still owns; complete it below.
            parts.append(self._pending)
            self._pending = b""
        while True:
            try:
                tail = await self._stream.readuntil(b"\n")
                return b"".join(parts) + tail
            except asyncio.IncompleteReadError as exc:
                # EOF: deliver the unterminated tail, then b"" on next call.
                return b"".join(parts) + (exc.partial or b"")
            except asyncio.LimitOverrunError:
                chunk = await self._stream.read(self._DRAIN_CHUNK)
                if not chunk:
                    # EOF mid-line: return the assembled (unterminated) tail.
                    return b"".join(parts)
                newline = chunk.find(b"\n")
                if newline >= 0:
                    parts.append(chunk[: newline + 1])
                    self._pending = chunk[newline + 1 :]
                    return b"".join(parts)
                parts.append(chunk)


async def _drain_stderr(stream, tail: "collections.deque[str]"):
    reader = _UnboundedLineReader(stream)
    while True:
        line = await reader.readline()
        if not line:
            return
        tail.append(line.decode("utf-8", "replace").rstrip())


async def _collect_stderr_tail(stderr_task, stderr_tail, timeout: float = 2.0):
    """Give the concurrent stderr drain a bounded chance before error text.

    The drain task shares the loop with the stream reader; at a failure the
    provider body may still be sitting in the pipe.  A short bounded join
    (the process has already exited at the nonzero-exit site, so EOF is
    imminent) folds it into the ClaudeSDKError text the 1302/1308 classifier
    parses.  Never raises — a slow drain keeps whatever tail it collected.
    """

    if not stderr_task.done():
        with contextlib.suppress(asyncio.TimeoutError, TimeoutError):
            await asyncio.wait({stderr_task}, timeout=timeout)
    return stderr_tail


async def codex_query(prompt=None, *, options=None, transport=None):
    """Async generator of SDK messages from one ``codex exec`` run.

    Mirrors the ``claude_query(prompt=..., options=..., transport=...)`` shape
    used at the retry-loop dispatch point.  Provider failures raise
    ``ClaudeSDKError`` carrying the raw codex error body so the existing
    GLM 1302/1308 classification chain parses them unchanged.
    """

    _require_sdk("codex_query (codex exec stream)")
    if transport is None:
        transport = new_codex_exec_transport(prompt, options)
    process = await transport.spawn()
    model_label = getattr(options, "model", None) or "codex"
    translator = CodexEventTranslator(model_label)
    stderr_tail: "collections.deque[str]" = collections.deque(maxlen=_STDERR_TAIL_LINES)
    stdin_task = asyncio.create_task(
        _write_stdin(process.stdin, (prompt or "").encode("utf-8"))
    )
    stderr_task = asyncio.create_task(_drain_stderr(process.stderr, stderr_tail))
    stdout_reader = _UnboundedLineReader(process.stdout)
    unparseable_lines = 0
    try:
        while True:
            line = await stdout_reader.readline()
            if not line:
                break
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except (json.JSONDecodeError, UnicodeDecodeError):
                unparseable_lines += 1
                continue
            if translator.fatal_error:
                break
            for message in translator.translate(event):
                yield message
            if translator.fatal_error:
                break
        if translator.fatal_error:
            raise ClaudeSDKError(
                _augment_error(
                    translator.fatal_error,
                    await _collect_stderr_tail(stderr_task, stderr_tail),
                )
            )
        # A non-fatal "Reconnecting..." stream error keeps its provider body
        # here so a terminal failure still classifies as 1302/1308.
        stream_error_note = (
            f"; last stream error: {translator.last_stream_error}"
            if translator.last_stream_error
            else ""
        )
        exit_code = process.returncode
        if exit_code is None:
            # stdout closed before the process exited — reap it first.
            try:
                exit_code = await asyncio.wait_for(process.wait(), timeout=5.0)
            except (asyncio.TimeoutError, TimeoutError):
                exit_code = None
        if exit_code not in (0, None):
            raise ClaudeSDKError(
                _augment_error(
                    f"codex exec exited with code {exit_code}{stream_error_note}",
                    await _collect_stderr_tail(stderr_task, stderr_tail),
                )
            )
        if not translator.saw_turn_completed:
            if (translator.final_text or "").strip():
                # The agent answer arrived but the terminal turn.completed did
                # not (e.g. the stream dropped right after the last item).
                # Synthesize the terminal ResultMessage with unknown usage
                # (zeros) so the one-ResultMessage stream contract holds.
                for message in translator._result_messages():
                    yield message
            else:
                raise ClaudeSDKError(
                    _augment_error(
                        "codex stream ended without a completed turn"
                        f"{stream_error_note} "
                        f"(unparseable_lines={unparseable_lines})",
                        await _collect_stderr_tail(stderr_task, stderr_tail),
                    )
                )
    finally:
        # One-shot process: timeout/cancel/normal-end all funnel here.  The
        # transport kills the process group; returncode proves exit for the
        # provider-attempt cleanup chain.
        for task in (stdin_task, stderr_task):
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        await transport.close()


def _augment_error(message: str, stderr_tail) -> str:
    """Fold the stderr tail into the provider error text.

    The stderr tail is where codex prints provider response bodies (GLM 429 /
    1302 / 1308 envelopes); keeping the raw bytes in the ClaudeSDKError text
    is what lets classify_llm_availability keep working unchanged.
    """

    parts = [str(message)]
    if stderr_tail:
        tail_text = "\n".join(str(x) for x in stderr_tail)
        parts.append(f"stderr: {tail_text}")
    return "\n".join(parts)
