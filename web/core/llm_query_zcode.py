"""ZCode app-server transport adapter for the run_claude_query chokepoint.

``POK_LLM_TRANSPORT`` selects the provider process transport (default
``claude``; ``codex`` spawns a one-shot ``codex exec``; ``zcode`` spawns the
local ZCode agent app-server and drives one session per attempt).

Why this adapter exists (block-48, 2026-11-03): the operator wanted the
production LLM base to run through the locally installed ZCode TUI client's
account (``account:bigmodel-individual-coding-plan`` / GLM-5.3-Flash) instead
of the codex CLI.  The adapter's single responsibility is the same as the
codex one — make the provider process look like the claude_agent_sdk stream
the rest of the control plane already consumes — so every downstream contract
is preserved by construction:

- **Event shape**: the ZCode agent protocol is newline-delimited JSON
  (``{id,method,params}`` request / ``{method,params}`` notification /
  ``{id,result}`` or ``{id,error}`` response).  Its session event stream
  (``turn.started`` / ``part.delta`` / ``part.upserted`` / ``tool.updated`` /
  ``turn.completed`` / ``turn.failed``) is translated into the exact SDK
  message objects (``SystemMessage`` / ``AssistantMessage([TextBlock|
  ThinkingBlock])`` / ``UserMessage([ToolResultBlock])`` / ``ResultMessage``)
  that ``llm_query_retry._process_stream`` already handles — role-IO evidence,
  stall/idle/first-activity timeout policy, availability classification, and
  ``llm_call_metrics`` recording therefore run unchanged.
- **Streaming**: ``part.delta`` arrives incrementally, so TTFT / stall / idle
  detection keep working.  This is the reason the adapter speaks the
  app-server RPC instead of the ``-p`` one-shot JSON mode: ``-p`` has no
  incremental events at all, and it also cannot carry a production prompt
  (the kernel caps a single argv argument at 128 KB while role prompts reach
  276 KB; ``-p -`` does not read stdin; ``--attach`` does not inline the whole
  file).  ``session/send`` carries ``content`` as an unbounded JSON string.
- **Usage mapping**: ``turn.completed`` carries ``usage`` and ``cacheStats``;
  ``cacheReadTokens`` maps to ``cache_read_input_tokens`` and
  ``cacheWriteTokens`` to ``cache_creation_input_tokens``, so the cache-hit
  KPI (``CR / input`` over full-prompt ``input_tokens``) keeps its exact
  meaning.  Missing fields are 0; vendor keys are preserved.
- **Cost**: ZCode reports no per-call cost; ``ResultMessage.total_cost_usd``
  stays ``None`` (cost fields are nullable in metrics/billing).
- **Error classification**: provider failures surface as ``ClaudeSDKError``
  whose text carries the raw provider body (``turn.failed.error`` message plus
  the stderr tail), so ``classify_llm_availability`` keeps parsing GLM
  1302/1308 patterns and the quota pause chain is untouched.
- **Ownership/cleanup**: :class:`ZCodeTransport` duck-types the SDK subprocess
  transport surface the provider-attempt lifecycle relies on (``_process``
  attribute + awaitable ``close()``), killing the process *group* and proving
  exit via ``returncode``.  One app-server process serves exactly one attempt,
  so timeout/cancel is attempt-local.

Reverse RPCs (server -> client)
-------------------------------
The app-server asks the CLIENT for runtime preferences while materializing a
session (``session/requestRuntimePreferences``) and — when official MCP
headers are needed — ``interaction/requestOfficialMcpAuthHeaders``.  A client
that never answers makes ``session/create`` fail with
``Client request timed out`` (measured: 15 s).  :class:`ZCodeTransport`
answers both explicitly: preferences come from the process environment
(``POK_ZCODE_PREFS_*``) with conservative defaults, and the MCP-auth request
is answered with an empty header set, which is what a dispatch with no
official MCP server wants.

Capability differences vs the claude transport
----------------------------------------------
- **No PreToolUse/PostToolUse hooks.**  The claude path enforces role read and
  write scopes through SDK hooks; the ZCode app-server exposes no hook surface
  of that shape, so this adapter maps the dispatch's declared tool list and
  write scope onto session creation (``mode`` + ``toolAllowlist`` /
  ``toolDenylist``) and relies on the lease-tree ``audit_worker_boundary``
  audit for file-level precision — exactly the posture the codex transport
  documents.  A write-capable role whose scope cannot be expressed fails
  closed rather than silently degrading to a broader surface.
- **No ``setting_sources`` / AGENTS.md injection equivalent.**  The prompt
  already carries the full role contract (that is how the claude and codex
  paths feed the model), so nothing depends on this.

This module is deliberately OUTSIDE ``CRITICAL_EXACT`` (like
``llm_query_codex.py`` and ``llm_query_retry.py``), so the transport switch
itself is not an evaluation-contract change.
"""

import asyncio
import contextlib
import json
import os
import shutil
import signal
import tempfile

try:
    from claude_agent_sdk import (
        AssistantMessage,
        ResultMessage,
        SystemMessage,
        TextBlock,
        ThinkingBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
        ClaudeSDKError,
    )

    _SDK_IMPORT_ERROR = None
except ImportError as _sdk_exc:  # SDK-free interpreter (preflight surface)
    # The transport preflight surface (zcode_transport_enabled /
    # zcode_binary_from_env / resolve_zcode_binary /
    # assert_zcode_transport_ready) must stay importable by ANY python3 —
    # systemd ExecStartPre and operator smokes resolve ``python3`` from the
    # service PATH, which does not carry the venv's claude_agent_sdk.  No
    # placeholder SDK types are bound; every SDK-typed entry point calls
    # _require_sdk() and fails with an actionable chained error.
    _SDK_IMPORT_ERROR = _sdk_exc

#: Transport selector env var (shared with the codex adapter by design: one
#: key selects exactly one transport).
TRANSPORT_ENV = "POK_LLM_TRANSPORT"

#: Binary lookup: the ZCode CLI launcher ships inside the agent release tree.
#: ``zcode`` on PATH is usually the Electron desktop app (it exits without an
#: X server), so the default points at the server-side CLI entry point that
#: actually implements ``app-server``.
ZCODE_BIN_ENV = "POK_ZCODE_BIN"
ZCODE_DEFAULT_BIN = "/home/ubuntu/.zcode/server/bin/zcode"

#: Account/model selection forwarded as ``modelSelection``.  The provider id is
#: the operator's ZCode account; the model id must exist in that provider's
#: registry (``GLM-5.3-Flash`` is the plan's model name, ``GLM-5``-family ids
#: differ between the ``-p`` and app-server registry surfaces).
ZCODE_PROVIDER_ENV = "POK_ZCODE_PROVIDER_ID"
ZCODE_MODEL_ENV = "POK_ZCODE_MODEL"
ZCODE_DEFAULT_PROVIDER = "account:bigmodel-individual-coding-plan"
ZCODE_DEFAULT_MODEL = "GLM-5.3-Flash"

#: Session permission mode.  ``yolo`` maps closest to the claude path's
#: ``bypassPermissions`` (the runtime, not the model, owns the send path).
ZCODE_MODE_ENV = "POK_ZCODE_MODE"
ZCODE_DEFAULT_MODE = "yolo"

#: Reasoning level forwarded into ``modelSelection.options`` — the ZCode
#: analogue of ``POK_LLM_EFFORT``.  Left empty by default so the provider's
#: own default applies (``GLM-5.3`` accepts ``low``/``high``/``max``).
ZCODE_THOUGHT_ENV = "POK_ZCODE_THOUGHT_LEVEL"

#: Grace (seconds) for SIGTERM->SIGKILL escalation in transport close.
ZCODE_CLOSE_GRACE_ENV = "POK_ZCODE_CLOSE_GRACE_SEC"

#: Line limit for the protocol reader.  A ``--json`` style single line can
#: embed a whole diff; the asyncio default of 64 KiB voids the stream, so the
#: codex adapter raised it to 8 MiB and this adapter matches that.
_STREAM_LINE_LIMIT = 8 * 1024 * 1024

#: Chunk size for the over-long-line drain path.
_DRAIN_CHUNK = 64 * 1024

#: Per-request timeout for client->server RPCs.  Session creation on a cold
#: app-server measured 10-25 s; sends start fast but a long model turn is not
#: an RPC (it streams as events), so this only bounds control-plane calls.
_RPC_TIMEOUT_SEC = 120.0

#: Default reverse-RPC answers.  Every field is a real schema field from
#: ``zcodeSessionRuntimePreferencesResultSchema``; defaults are the
#: conservative (least-capability) values so an unconfigured deployment
#: cannot silently enable memory, native search, or auto-resolution.
#: Fixed, short instruction for every dispatch. ALL variable content rides in
#: the ``--attach`` file, so this string never grows with the role prompt
#: (the 128 KB per-argument kernel cap makes that a hard requirement).
_PROMPT_INSTRUCTION = (
    "The file attached to this message contains your complete task "
    "instructions. Treat it as your prompt: follow it exactly, including any "
    "output format it requires. Do not summarize the file; do the work it "
    "describes and answer with the requested output only."
)

_DEFAULT_PREFS = {
    "nativeSearchEnhancementsEnabled": False,
    "toolSearchMode": "off",
    "memoryEnabled": False,
    "askUserQuestionAutoResolutionEnabled": False,
    "modelContextBudgetStrategy": "preflight-v1",
}


def _require_sdk():
    if _SDK_IMPORT_ERROR is not None:
        raise ClaudeSDKError(
            "zcode transport requires claude_agent_sdk (the dispatch-path "
            "surface); run this from the service venv "
            "(POK_PYTHON=/home/ubuntu/pok1/.venv/bin/python). "
            f"Original import error: {_SDK_IMPORT_ERROR}"
        ) from _SDK_IMPORT_ERROR


def zcode_transport_enabled() -> bool:
    """True when ``POK_LLM_TRANSPORT=zcode`` selects this adapter."""

    return (
        str(os.environ.get(TRANSPORT_ENV, "claude") or "claude").strip().lower()
        == "zcode"
    )


def zcode_binary_from_env() -> str:
    return str(os.environ.get(ZCODE_BIN_ENV, "") or ZCODE_DEFAULT_BIN)


def resolve_zcode_binary(value=None) -> str:
    """Resolve the ZCode CLI launcher to an absolute path, fail-fast.

    A value carrying a directory component is authoritative (checked directly,
    no fallback).  A bare name searches ``PATH``.  Resolution failure is an
    actionable error and never a provider-quota issue: the spawn class is
    distinct from a provider 429 by construction.
    """

    raw = str(value if value is not None else zcode_binary_from_env()).strip()
    if not raw:
        raise ZCodeBinaryNotFound(
            "no ZCode binary configured: set POK_ZCODE_BIN to the server-side "
            f"CLI entry point (default {ZCODE_DEFAULT_BIN})"
        )
    if os.sep in raw:
        if os.path.isfile(raw) and os.access(raw, os.X_OK):
            return os.path.abspath(raw)
        raise ZCodeBinaryNotFound(
            f"POK_ZCODE_BIN={raw!r} is not an executable file. Point it at the "
            "ZCode server CLI (``…/.zcode/server/bin/zcode``), not the Electron "
            "desktop launcher (which exits without an X display)."
        )
    found = shutil.which(raw)
    if found:
        return found
    raise ZCodeBinaryNotFound(
        f"zcode binary {raw!r} not found on PATH; set POK_ZCODE_BIN to an "
        f"absolute path (default {ZCODE_DEFAULT_BIN})"
    )


class ZCodeBinaryNotFound(RuntimeError):
    """The configured ZCode launcher cannot be resolved.

    Deliberately NOT a ``ClaudeSDKError``: ``classify_llm_availability`` must
    never read a missing binary as a provider quota/frequency event.
    """


def assert_zcode_transport_ready() -> str:
    """Preflight used by the web lifespan: resolve the binary, return its path."""

    return resolve_zcode_binary()


def _close_grace_sec() -> float:
    try:
        return float(os.environ.get(ZCODE_CLOSE_GRACE_ENV, "15"))
    except (TypeError, ValueError):
        return 15.0


def _signal_process_group(process, sig) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(process.pid), sig)


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _prefs_from_env() -> dict:
    """Reverse-RPC preference payload (defaults are least-capability)."""

    prefs = dict(_DEFAULT_PREFS)
    search = str(os.environ.get("POK_ZCODE_NATIVE_SEARCH", "") or "").strip().lower()
    if search in ("1", "true", "yes", "on"):
        prefs["nativeSearchEnhancementsEnabled"] = True
    mode = str(os.environ.get("POK_ZCODE_TOOLSEARCH_MODE", "") or "").strip()
    if mode in ("auto", "native", "local", "off"):
        prefs["toolSearchMode"] = mode
    memory = str(os.environ.get("POK_ZCODE_MEMORY", "") or "").strip().lower()
    if memory in ("1", "true", "yes", "on"):
        prefs["memoryEnabled"] = True
    return prefs


class _UnboundedLineReader:
    """Line reader that survives single lines longer than the reader limit.

    Same rationale and behaviour as the codex adapter's reader: on CPython
    3.12 ``StreamReader.readline()`` raises ``ValueError`` on an over-long line
    AND discards the buffered bytes, voiding the whole stream.  ``readuntil()``
    raises ``LimitOverrunError`` with the buffer intact, so the remainder can be
    reassembled from bounded ``read()`` calls.
    """

    def __init__(self, stream):
        self._stream = stream
        self._pending = b""

    async def readline(self) -> bytes:
        parts = []
        if self._pending:
            newline = self._pending.find(b"\n")
            if newline >= 0:
                line = self._pending[: newline + 1]
                self._pending = self._pending[newline + 1 :]
                return line
            parts.append(self._pending)
            self._pending = b""
        while True:
            try:
                tail = await self._stream.readuntil(b"\n")
                return b"".join(parts) + tail
            except asyncio.IncompleteReadError as exc:
                return b"".join(parts) + (exc.partial or b"")
            except asyncio.LimitOverrunError:
                chunk = await self._stream.read(_DRAIN_CHUNK)
                if not chunk:
                    return b"".join(parts)
                newline = chunk.find(b"\n")
                if newline >= 0:
                    parts.append(chunk[: newline + 1])
                    self._pending = chunk[newline + 1 :]
                    return b"".join(parts)
                parts.append(chunk)


class ZCodeEventTranslator:
    """Translate ZCode session events into SDK stream messages.

    Pure state machine over decoded event payloads; the transport owns all
    process I/O and reverse-RPC answers.  Fatal events record
    ``fatal_error`` instead of raising so the transport can fold stderr
    context into the single ``ClaudeSDKError`` it raises — the same split the
    codex translator uses.

    ``part.delta`` with ``field="text"`` is the incremental answer text (the
    first one is what TTFT measures); ``field="reasoning"`` is thinking.
    ``part.upserted`` carries a complete part when the server coalesces
    (e.g. after a resume), so a part already emitted via deltas must not be
    emitted twice: ``_seen_text_parts`` tracks that.
    """

    def __init__(self, model_label: str = "zcode"):
        self.model_label = str(model_label or "zcode")
        self.final_text = ""
        self.text_parts = []
        self.thinking_parts = []
        self.tool_use_count = 0
        self.tool_result_count = 0
        self.usage_raw = {}
        self.cache_read_tokens = 0
        self.cache_write_tokens = 0
        self.saw_turn_completed = False
        self.fatal_error = None
        self.result_subtype = "success"
        self.duration_ms = 0
        self.session_id = ""
        self._text_buffers = {}
        self._thinking_buffers = {}
        self._seen_text_parts = set()
        self._seen_thinking_parts = set()

    def _assistant(self, blocks):
        return AssistantMessage(content=blocks, model=self.model_label)

    # -- text/thinking accumulation -------------------------------------

    def _append(self, buffers, key, chunk, sink, seen):
        buffers.setdefault(key, []).append(chunk)
        sink.append(chunk)

    def text_so_far(self) -> str:
        if self.text_parts:
            return "".join(self.text_parts)
        return "".join(chunk for buf in self._text_buffers.values() for chunk in buf)

    # -- event handling --------------------------------------------------

    def handle(self, event):
        if not isinstance(event, dict):
            return []
        messages = []
        kind = str(event.get("type") or "")
        if kind == "turn.started":
            start = event.get("executionStartedAt")
            if isinstance(start, (int, float)) and start > 0:
                messages.append(SystemMessage(subtype="init", data={"started_at": start}))
            return messages
        if kind == "part.delta":
            field = str(event.get("field") or "text")
            delta = event.get("delta") or ""
            if not delta:
                return messages
            part_id = str(event.get("partId") or "")
            if field == "reasoning":
                self._append(self._thinking_buffers, part_id, delta,
                             self.thinking_parts, self._seen_thinking_parts)
                messages.append(
                    self._assistant([ThinkingBlock(thinking=delta, signature="")])
                )
            else:
                self._append(self._text_buffers, part_id, delta,
                             self.text_parts, self._seen_text_parts)
                messages.append(self._assistant([TextBlock(text=delta)]))
            return messages
        if kind == "part.upserted":
            part = event.get("part") or {}
            if not isinstance(part, dict):
                return messages
            part_id = str(part.get("partId") or part.get("id") or "")
            ptype = str(part.get("type") or "")
            if ptype in ("tool", "toolCall", "tool_use"):
                self.tool_use_count += 1
                messages.append(
                    self._assistant(
                        [
                            ToolUseBlock(
                                id=str(part.get("toolCallId") or part_id or ""),
                                name=str(part.get("toolName") or ""),
                                input=part.get("input") or {},
                            )
                        ]
                    )
                )
                return messages
            text = part.get("text")
            if isinstance(text, str) and text:
                # Coalesced part: only emit when no delta was seen for it.
                if part_id and part_id in self._text_buffers:
                    return messages
                if part_id:
                    self._seen_text_parts.add(part_id)
                self.text_parts.append(text)
                messages.append(AssistantMessage(content=[TextBlock(text=text)]))
            return messages
        if kind == "tool.updated":
            state = str(event.get("kind") or "")
            if state in ("completed", "failed"):
                self.tool_result_count += 1
                messages.append(
                    UserMessage(
                        content=[
                            ToolResultBlock(
                                tool_use_id=str(event.get("toolCallId") or ""),
                                content=str(event.get("output") or ""),
                                is_error=(state == "failed"),
                            )
                        ]
                    )
                )
            elif state == "scheduled":
                self.tool_use_count += 1
                messages.append(
                    self._assistant(
                        [
                            ToolUseBlock(
                                id=str(event.get("toolCallId") or ""),
                                name=str(event.get("toolName") or ""),
                                input=event.get("input") or {},
                            )
                        ]
                    )
                )
            return messages
        if kind == "turn.completed":
            self.saw_turn_completed = True
            self.final_text = str(event.get("response") or "")
            usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
            self.usage_raw = dict(usage)
            cache_stats = event.get("cacheStats")
            if isinstance(cache_stats, dict):
                self.cache_read_tokens = _int(cache_stats.get("cacheReadTokens"))
            self.cache_read_tokens = _int(
                usage.get("cacheReadTokens", self.cache_read_tokens)
            )
            self.cache_write_tokens = _int(usage.get("cacheWriteTokens"))
            self.duration_ms = int(
                float(event.get("duration") or 0) * 1000
            )
            if event.get("toolCallCount") is not None:
                self.tool_use_count = max(
                    self.tool_use_count, _int(event.get("toolCallCount"))
                )
            result_type = str(event.get("resultType") or "success")
            self.result_subtype = (
                "success" if result_type == "success" else result_type
            )
            if self.result_subtype != "success":
                self.fatal_error = f"turn completed with resultType={result_type}"
            return messages
        if kind == "turn.failed":
            err = event.get("error") or {}
            if isinstance(err, dict):
                self.fatal_error = str(
                    err.get("message") or err.get("code") or "turn failed"
                )
                detail = err.get("detail") or err.get("underlyingErrorMessage")
                if detail:
                    self.fatal_error = f"{self.fatal_error} | {detail}"
            else:
                self.fatal_error = str(err) or "turn failed"
            self.result_subtype = "error_during_execution"
            return messages
        if kind in ("model.streaming", "session.created", "session.updated",
                    "session.resumed", "session.titleUpdated", "checkpoint.created"):
            return messages
        return messages

    # -- terminal message ------------------------------------------------

    def result_messages(self):
        _require_sdk()
        if not self.saw_turn_completed and not self.final_text and not self.text_parts:
            return []
        sdk_usage = {
            "input_tokens": _int(self.usage_raw.get("inputTokens")),
            "output_tokens": _int(self.usage_raw.get("outputTokens")),
            "cache_creation_input_tokens": self.cache_write_tokens,
            "cache_read_input_tokens": self.cache_read_tokens,
        }
        # Vendor-extension keys stay present (0 when absent) so downstream
        # consumers never see a shape that depends on which fields the
        # provider emitted.
        sdk_usage["inputTokens"] = sdk_usage["input_tokens"]
        sdk_usage["outputTokens"] = sdk_usage["output_tokens"]
        sdk_usage["cacheReadTokens"] = self.cache_read_tokens
        sdk_usage["cacheWriteTokens"] = self.cache_write_tokens
        sdk_usage["reasoningTokens"] = _int(self.usage_raw.get("reasoningTokens"))
        return [
            ResultMessage(
                subtype=self.result_subtype,
                duration_ms=self.duration_ms,
                duration_api_ms=self.duration_ms,
                is_error=(self.result_subtype != "success"),
                num_turns=1,
                session_id=self.session_id or "",
                total_cost_usd=None,
                usage=sdk_usage,
                result=(self.final_text or self.text_so_far() or None),
                api_error_status=None,
            )
        ]

#: Env var names the CLI reads for provider config discovery.
_BUILTIN_CONFIG_ENV = "ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"
_BUILTIN_BUNDLED_CONFIG_ENV = "ZCODE_BUILTIN_PROVIDER_BUNDLED_CONFIG_FILE"
_PERSONAL_CONFIG_ENV = "ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"


def _resolve_active_builtin_config(base_dir: str) -> str | None:
    """Newest ``.../provider/<platform>/<version>/endpoint-*/zcode-builtin.json``.

    The CLI stores one built-in provider snapshot per (platform, app version,
    endpoint) with no machine-readable "current" pointer, so the newest file
    wins. Absent candidates return ``None`` and the child keeps the CLI's own
    resolution (which is what fails under systemd).
    """

    root = os.path.join(base_dir, ".zcode", "v2", "runtime", "provider")
    if not os.path.isdir(root):
        return None
    best = None
    best_mtime = -1.0
    for platform_entry in os.scandir(root):
        if not platform_entry.is_dir():
            continue
        for version_entry in os.scandir(platform_entry.path):
            if not version_entry.is_dir():
                continue
            for endpoint_entry in os.scandir(version_entry.path):
                if not endpoint_entry.is_dir():
                    continue
                candidate = os.path.join(endpoint_entry.path, "zcode-builtin.json")
                if not os.path.isfile(candidate):
                    continue
                mtime = os.path.getmtime(candidate)
                if mtime > best_mtime:
                    best, best_mtime = candidate, mtime
    return best


class ZCodeTransport:
    """One ``zcode -p`` process, owned by one provider attempt.

    Transport choice, and why it is the CLI and not the app-server:

    - The app-server entry point builds its provider registry from
      ``s2t(env)`` with **no** standalone context, so account providers
      (``access.type == "zhipu-account"``) never appear in it. Its only
      account-injection channel, ``provider/updateAccountConfig``, validates
      ``access`` against the api-key shape and rejects ``accountType`` /
      ``mode`` (measured: unrecognized_keys for both), so a zhipu-account
      provider cannot be injected either. Consequence: every GLM id under
      ``account:bigmodel-*`` is unreachable from the app-server.
    - ``zcode -p`` goes through ``prepareCliProviderRuntimeEnv`` +
      ``startProcessProviderRegistryRuntime(..., {standalone: ...})``, which
      IS the path that reads the account credential store and marks the
      provider ``entitled``. It resolves GLM correctly (measured:
      response "account:bigmodel-individual-coding-plan/GLM-5.3").

    The CLI has two constraints the adapter must absorb:

    1. **argv size.** A single argv argument is capped at 128 KB by the
       kernel, and production role prompts reach 276 KB. So the prompt is
       never passed on the command line: the adapter writes it to a private
       temp file and passes ``--attach <path>``, which the model receives as
       file content. Measured lossless at 192 KB (a marker at the end of the
       file came back verbatim) and at 3000 lines (line count answered
       exactly).
    2. **No streaming events.** ``-p --json`` emits ONE final object
       (response + usage incl. cacheReadTokens/cacheWriteTokens + eventCount).
       TTFT therefore cannot be measured on this transport and stall
       detection degrades to the transport-level total timeout; the adapter
       says so in ``transport_capabilities()`` instead of pretending.
       ``llm_call_metrics`` still gets full input/output/cache counts, so the
       cache-hit KPI is unaffected.

    Duck-types the SDK subprocess transport surface the provider-attempt
    lifecycle relies on: ``_process`` once spawned and an awaitable
    ``close()`` that terminates the process *group* and proves exit via
    ``returncode``.
    """

    #: Documented capability differences, consumed by callers/logging.
    native_streaming = False

    def __init__(self, full_prompt, options, allowed_write_dir=None):
        _require_sdk()
        self._prompt = full_prompt or ""
        self._options = options
        self._allowed_write_dir = allowed_write_dir
        self._process = None
        self._prompt_file = None
        self._stderr_tail = []
        self._result = None
        self._closed = False
        self._storage_root = None

    # -- argv / process --------------------------------------------------

    def build_argv(self, binary=None) -> list:
        """``zcode -p <instruction> --attach <prompt-file> --json ...``.

        The instruction stays short and fixed; all variable content travels
        through the attachment so argv never approaches the 128 KB cap.
        """

        argv = [
            binary or resolve_zcode_binary(),
            "-p",
            _PROMPT_INSTRUCTION,
            "--attach",
            self._prompt_file or "-",
            "--json",
            "--mode",
            str(os.environ.get(ZCODE_MODE_ENV, "") or ZCODE_DEFAULT_MODE),
            "--no-color",
        ]
        cwd = self._workspace_cwd()
        if cwd:
            argv += ["--cwd", cwd]
        return argv

    def _workspace_cwd(self) -> str:
        """Working directory for the CLI run.

        For a write-capable dispatch this is the primary declared write root
        (the lease workspace), mirroring the codex adapter's cwd pin: the
        repository tree never becomes the implicit workspace for untrusted
        worker output. Read-only dispatches keep the project root.
        """

        scope = self._allowed_write_dir
        candidates = []
        if isinstance(scope, dict):
            candidates = [str(p) for p in (scope.get("dirs") or ())]
            candidates += [str(p) for p in (scope.get("files") or ())]
        elif isinstance(scope, (str, os.PathLike)) and str(scope):
            candidates = [str(scope)]
        for candidate in candidates:
            path = os.path.abspath(candidate)
            if os.path.isdir(path):
                return path
            parent = os.path.dirname(path)
            if parent and os.path.isdir(parent):
                return parent
        return os.getcwd()

    def _write_prompt_file(self) -> str:
        """Materialize the prompt as a private file for ``--attach``.

        Created in a per-attempt directory under the system temp root with
        mode 0600, and removed in ``close()``. The prompt is already
        role-scoped content (it is what the claude path would pipe on stdin),
        so persisting it for the duration of one attempt changes nothing about
        what the model may see.
        """

        directory = tempfile.mkdtemp(prefix="pok-zcode-")
        path = os.path.join(directory, "prompt.txt")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(self._prompt)
        self._prompt_file = path
        return path

    def build_env(self) -> dict:
        """Environment for the CLI child.

        Two provider-config paths MUST be explicit. The CLI resolves the
        built-in provider config from its own entrypoint path plus
        ``ZCODE_DATA_BASE_DIR``; under the systemd service that resolution
        lands on ``.../runtime/provider/zcode-builtin.json`` (absent) and
        ``$HOME/config/provider/zcode-builtin.json`` (absent), so the run
        fails with "无法定位 CLI ZCode Built-in Provider Config" before any
        provider call. An interactive shell does not hit this because its env
        differs. Passing the two absolute paths is exactly what
        ``prepareCliProviderRuntimeEnv`` would have produced.

        ``ZCODE_DATA_BASE_DIR`` is pinned to the same home so the credential
        store resolves to the same ``.zcode/v2/credentials.json`` the operator
        logged into.
        """

        env = dict(os.environ)
        base = env.get("ZCODE_DATA_BASE_DIR") or os.path.expanduser("~")
        env.setdefault("ZCODE_DATA_BASE_DIR", base)
        if not env.get(_BUILTIN_CONFIG_ENV):
            builtin = _resolve_active_builtin_config(base)
            if builtin:
                env[_BUILTIN_CONFIG_ENV] = builtin
                env[_BUILTIN_BUNDLED_CONFIG_ENV] = builtin
        elif not env.get(_BUILTIN_BUNDLED_CONFIG_ENV):
            env[_BUILTIN_BUNDLED_CONFIG_ENV] = env[_BUILTIN_CONFIG_ENV]
        if not env.get(_PERSONAL_CONFIG_ENV):
            personal = os.path.join(base, ".zcode", "v2", "provider_config.json")
            if os.path.isfile(personal):
                env[_PERSONAL_CONFIG_ENV] = personal
        # Session-store isolation. Every dispatch is a separate CLI process and
        # the default session DB is one shared SQLite file
        # (~/.zcode/cli/db/db.sqlite); with several concurrent dispatches the
        # CLI fails with "Error: database is locked" before the provider call
        # (measured in production right after the cutover, and it also killed a
        # parallel agent batch). ZCODE_STORAGE_DIR relocates the whole store, so
        # each attempt gets its own directory and the lock contention is gone.
        # The credential store is NOT under this root (it is resolved from
        # ZCODE_DATA_BASE_DIR / $HOME), so login state is unaffected.
        storage_root = env.get("ZCODE_STORAGE_DIR")
        if not storage_root:
            storage_root = os.path.join(
                tempfile.gettempdir(), "pok-zcode-store-%d-%d" % (os.getpid(), id(self))
            )
            os.makedirs(storage_root, exist_ok=True)
            env["ZCODE_STORAGE_DIR"] = storage_root
            self._storage_root = storage_root
        return env

    async def spawn(self):
        if not self._prompt_file:
            self._write_prompt_file()
        argv = self.build_argv()
        self._process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_STREAM_LINE_LIMIT,
            start_new_session=True,
            cwd=self._workspace_cwd(),
            env=self.build_env(),
        )
        return self._process

    # -- output ----------------------------------------------------------

    async def communicate(self):
        """Read the whole run; return (stdout bytes, stderr tail).

        ``-p --json`` writes one JSON object at the end of the run, so a
        full read is the correct shape (there is nothing to stream). stderr
        is drained concurrently so a chatty provider cannot deadlock the pipe
        and its tail is available for error classification.
        """

        process = self._process
        if process is None:
            raise ClaudeSDKError("zcode transport was not spawned")
        stderr_task = asyncio.create_task(self._drain_stderr())
        stdout = b""
        try:
            if process.stdout is not None:
                stdout = await process.stdout.read()
            return_code = await process.wait()
        finally:
            with contextlib.suppress(BaseException):
                stderr_task.cancel()
                await stderr_task
        self._returncode = return_code
        return stdout, list(self._stderr_tail)

    async def _drain_stderr(self):
        stream = self._process.stderr if self._process is not None else None
        if stream is None:
            return
        reader = _UnboundedLineReader(stream)
        while True:
            line = await reader.readline()
            if not line:
                return
            self._stderr_tail.append(line.decode("utf-8", "replace").rstrip())
            if len(self._stderr_tail) > 40:
                self._stderr_tail.pop(0)

    def stderr_note(self) -> str:
        if not self._stderr_tail:
            return ""
        relevant = [
            line
            for line in self._stderr_tail
            if any(
                marker in line
                for marker in ("1308", "1302", "429", "Error", "error", "限额", "上限")
            )
        ]
        tail = relevant[-6:] if relevant else self._stderr_tail[-4:]
        return " | stderr: " + " / ".join(tail)

    # -- close -----------------------------------------------------------

    async def close(self):
        """Terminate the owned process group, prove exit, drop the temp prompt."""

        self._closed = True
        process = self._process
        if process is not None and process.returncode is None:
            _signal_process_group(process, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=_close_grace_sec())
            except (asyncio.TimeoutError, TimeoutError):
                _signal_process_group(process, signal.SIGKILL)
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except (asyncio.TimeoutError, TimeoutError):
                    pass
        self._cleanup_prompt_file()
        self._cleanup_storage_root()
        if process is None:
            return True
        return process.returncode is not None

    def _cleanup_storage_root(self):
        """Remove the per-attempt session store (CLI state only, no credentials)."""

        root = self._storage_root
        if not root:
            return
        self._storage_root = None
        with contextlib.suppress(OSError):
            shutil.rmtree(root, ignore_errors=True)

    def _cleanup_prompt_file(self):
        path = self._prompt_file
        if not path:
            return
        self._prompt_file = None
        with contextlib.suppress(OSError):
            os.unlink(path)
        with contextlib.suppress(OSError):
            os.rmdir(os.path.dirname(path))


def _extract_json_object(text: str):
    """Pull the single ``-p --json`` result object out of stdout.

    The CLI writes the result object last; anything before it is either the
    node experimental-SQLite warning or a provider diagnostic dump. Scanning
    brace-balanced candidates from the END keeps a provider error object (which
    also appears on stdout in some failure modes) from being mistaken for the
    result: the result always carries a ``usage`` or ``response`` key.
    """

    if not text:
        return None
    end = text.rfind("}")
    while end >= 0:
        depth = 0
        in_string = False
        escaped = False
        for index in range(end, -1, -1):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "}":
                depth += 1
            elif char == "{":
                depth -= 1
                if depth == 0:
                    candidate = text[index : end + 1]
                    try:
                        parsed = json.loads(candidate)
                    except Exception:
                        parsed = None
                    if isinstance(parsed, dict) and (
                        "usage" in parsed or "response" in parsed
                    ):
                        return parsed
                    break
        end = text.rfind("}", 0, end)
    return None


def _usage_from_cli(payload: dict) -> dict:
    """Map the CLI usage block onto the SDK usage keys.

    ``inputTokens`` is the FULL prompt and ``cacheReadTokens`` a subset of it,
    which is exactly the caliber the cache-hit KPI uses (CR / input). Vendor
    keys are preserved so a stored row is self-describing.
    """

    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    input_tokens = _int(usage.get("inputTokens"))
    output_tokens = _int(usage.get("outputTokens"))
    cache_read = _int(usage.get("cacheReadTokens"))
    cache_write = _int(usage.get("cacheWriteTokens"))
    mapped = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write,
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "cacheReadTokens": cache_read,
        "cacheWriteTokens": cache_write,
        "reasoningTokens": _int(usage.get("reasoningTokens")),
    }
    return mapped


def _result_message(payload: dict, model_label: str, session_id: str):
    _require_sdk()
    usage = _usage_from_cli(payload)
    text = payload.get("response")
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    return ResultMessage(
        subtype="success",
        duration_ms=0,
        duration_api_ms=0,
        is_error=False,
        num_turns=1,
        session_id=str(payload.get("sessionId") or session_id or ""),
        total_cost_usd=None,
        usage=usage,
        result=(text or None),
        api_error_status=None,
    )


def new_zcode_transport(full_prompt, options, allowed_write_dir=None) -> ZCodeTransport:
    """Create the ZCode transport owned by one provider attempt."""

    return ZCodeTransport(full_prompt, options, allowed_write_dir)


async def zcode_query(prompt=None, *, options=None, transport=None):
    """Async generator of SDK messages from one ``zcode -p`` run.

    Emits exactly one ``AssistantMessage`` carrying the whole answer and one
    ``ResultMessage`` with the mapped usage, which is the same terminal shape
    ``_process_stream`` accepts from the claude and codex paths. Provider
    failures raise ``ClaudeSDKError`` whose text keeps the raw provider body
    (including the GLM 1308/1302 envelope) so ``classify_llm_availability``
    and the quota pause chain behave identically.
    """

    _require_sdk()
    if transport is None:
        transport = new_zcode_transport(prompt if prompt is not None else "", options)
    model_label = getattr(options, "model", None) or "zcode"
    try:
        await transport.spawn()
        stdout, _ = await transport.communicate()
        text = stdout.decode("utf-8", "replace") if stdout else ""
        payload = _extract_json_object(text)
        if payload is None:
            raise ClaudeSDKError(
                "zcode returned no parseable result object"
                f"{transport.stderr_note()}"
            )
        # A provider error can still arrive as a well-formed object without a
        # response. Treat it as a failure whenever the object carries an
        # explicit error, or carries neither a response nor a NON-EMPTY usage
        # block (an empty usage means the call never reached the model). The
        # raw body is what the availability classifier needs.
        error_block = payload.get("error")
        has_response = "response" in payload
        raw_usage = payload.get("usage")
        has_usage = isinstance(raw_usage, dict) and bool(raw_usage)
        if error_block is not None or not (has_response or has_usage):
            raise ClaudeSDKError(
                "zcode run produced no result: "
                + json.dumps(payload, ensure_ascii=False)[:600]
                + transport.stderr_note()
            )
        answer = payload.get("response")
        if isinstance(answer, str) and answer.strip():
            yield AssistantMessage(
                content=[TextBlock(text=answer)], model=model_label
            )
        yield _result_message(payload, model_label, "")
    finally:
        await transport.close()
