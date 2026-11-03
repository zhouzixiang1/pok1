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


class ZCodeTransport:
    """One app-server process + one session, owned by one provider attempt.

    Duck-types the subprocess-CLI transport surface the provider-attempt
    lifecycle relies on: ``_process`` (asyncio subprocess once spawned) and an
    awaitable ``close()`` that terminates the process *group* and leaves
    ``returncode`` set, so exit confirmation works unchanged.

    Protocol handling is a single reader task: every server message is either a
    notification (session events, forwarded to the caller's queue), a response
    to one of our requests (resolved through ``_pending``), or a reverse
    request (answered immediately; see ``_answer_reverse_request``).
    """

    def __init__(self, full_prompt, options, allowed_write_dir=None):
        _require_sdk()
        self._prompt = full_prompt or ""
        self._options = options
        self._allowed_write_dir = allowed_write_dir
        self._process = None
        self._reader = None
        self._pending = {}
        self._queue = asyncio.Queue()
        self._next_id = 0
        self._session_id = None
        self._reader_task = None
        self._closed = False
        self._stderr_tail = []

    # -- process lifecycle ----------------------------------------------

    async def spawn(self):
        binary = resolve_zcode_binary()
        argv = [binary, "app-server", "--stdio"]
        self._process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_STREAM_LINE_LIMIT,
            start_new_session=True,
            cwd=self._workspace_cwd(),
        )
        self._reader = _UnboundedLineReader(self._process.stdout)
        self._reader_task = asyncio.create_task(self._read_loop())
        return self._process

    def _workspace_cwd(self) -> str:
        """Workspace root for the app-server process.

        ``session/create`` takes an explicit ``workspace``, so the process cwd
        is only a default; a write-capable dispatch pins it to its primary
        declared write root (the lease workspace) exactly like the codex
        adapter does, so the repository tree never becomes the implicit root.
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

    async def _read_loop(self):
        stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except Exception:
                    continue
                if not isinstance(message, dict):
                    continue
                await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # EOF mid-line or reader failure
            await self._queue.put(("error", f"zcode reader failed: {exc}"))
        finally:
            stderr_task.cancel()
            with contextlib.suppress(BaseException):
                await stderr_task
            await self._queue.put(("eof", None))

    async def _drain_stderr(self):
        stream = self._process.stderr
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                return
            self._stderr_tail.append(line.decode("utf-8", "replace").rstrip())
            if len(self._stderr_tail) > 40:
                self._stderr_tail.pop(0)

    async def _dispatch(self, message):
        method = message.get("method")
        msg_id = message.get("id")
        if method and msg_id is not None:
            await self._answer_reverse_request(msg_id, str(method),
                                               message.get("params") or {})
            return
        if msg_id is not None and ("result" in message or "error" in message):
            future = self._pending.pop(str(msg_id), None)
            if future is not None and not future.done():
                future.set_result(message)
            return
        if method:
            if method == "session/lifecycle/changed":
                state = str((message.get("params") or {}).get("state") or "")
                if state == "unloaded":
                    err = (message.get("params") or {}).get("lastError")
                    if isinstance(err, dict) and err.get("message"):
                        await self._queue.put(("error", str(err.get("message"))))
            if method.startswith("startup/") or method == "process/resourceSample":
                return
            if method.startswith("session/") or method.startswith("turn/"):
                await self._queue.put(("event", message.get("params") or {}))
            return

    async def _answer_reverse_request(self, msg_id, method, params):
        if method == "session/requestRuntimePreferences":
            prefs = _prefs_from_env()
            await self._send({"id": msg_id, "result": prefs})
            return
        if method == "interaction/requestOfficialMcpAuthHeaders":
            # No official MCP server is attached to a dispatch; an empty header
            # set is the honest answer.
            await self._send({"id": msg_id, "result": {"headers": {}}})
            return
        await self._send(
            {"id": msg_id, "error": {"code": -32601,
                                    "message": f"client does not implement {method}"}}
        )

    async def _send(self, obj):
        process = self._process
        if process is None or process.stdin is None or self._closed:
            return
        process.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())
        with contextlib.suppress(Exception):
            await process.stdin.drain()

    async def request(self, method, params=None, timeout=_RPC_TIMEOUT_SEC):
        self._next_id += 1
        req_id = str(self._next_id)
        future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        await self._send({"id": req_id, "method": method, "params": params or {}})
        try:
            message = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise ClaudeSDKError(
                f"zcode {method} timed out after {timeout:.0f}s"
                + self._stderr_note()
            )
        if "error" in message:
            err = message.get("error") or {}
            raise ClaudeSDKError(
                f"zcode {method} failed: {err.get('message') or err}"
                + self._stderr_note()
            )
        return message.get("result") or {}

    def _stderr_note(self) -> str:
        if not self._stderr_tail:
            return ""
        return " | stderr: " + " / ".join(self._stderr_tail[-6:])

    # -- session driving -------------------------------------------------

    def model_selection(self) -> dict:
        provider = str(
            os.environ.get(ZCODE_PROVIDER_ENV, "") or ZCODE_DEFAULT_PROVIDER
        ).strip()
        model = str(os.environ.get(ZCODE_MODEL_ENV, "") or ZCODE_DEFAULT_MODEL).strip()
        selection = {"providerId": provider, "modelId": model}
        level = str(os.environ.get(ZCODE_THOUGHT_ENV, "") or "").strip()
        if level:
            selection["options"] = {"reasoningLevel": level}
        return selection

    async def start_session(self):
        workspace_path = os.path.abspath(self._workspace_cwd())
        params = {
            "workspace": {
                "workspacePath": workspace_path,
                "workspaceKey": workspace_path,
            },
            "mode": str(os.environ.get(ZCODE_MODE_ENV, "") or ZCODE_DEFAULT_MODE),
            "model": self.model_selection(),
        }
        tools = getattr(self._options, "tools", None)
        if isinstance(tools, list) and tools:
            params["toolAllowlist"] = [str(t) for t in tools]
        result = await self.request("session/create", params)
        session_id = result.get("sessionId") if isinstance(result, dict) else None
        if not session_id:
            # The created-session notification carries the id when the reply is
            # a bare ack.
            session_id = self._session_id
        if not session_id:
            raise ClaudeSDKError("zcode session/create returned no sessionId")
        self._session_id = str(session_id)
        return self._session_id

    async def send_prompt(self):
        if not self._session_id:
            await self.start_session()
        await self.request(
            "session/send",
            {
                "sessionId": self._session_id,
                "content": self._prompt,
                "modelSelection": self.model_selection(),
            },
        )

    async def next_event(self):
        kind, payload = await self._queue.get()
        return kind, payload

    async def close(self):
        """Terminate the owned process group and prove exit via returncode."""

        self._closed = True
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            with contextlib.suppress(BaseException):
                await self._reader_task
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


def new_zcode_transport(full_prompt, options, allowed_write_dir=None) -> ZCodeTransport:
    """Create the ZCode transport owned by one provider attempt."""

    return ZCodeTransport(full_prompt, options, allowed_write_dir)


async def zcode_query(prompt=None, *, options=None, transport=None):
    """Async generator of SDK messages from one ZCode app-server session.

    Mirrors the ``claude_query`` / ``codex_query`` shape used at the retry-loop
    dispatch point.  Provider failures raise ``ClaudeSDKError`` carrying the
    raw provider body so the 1302/1308 classifier works unchanged.
    """

    _require_sdk()
    if transport is None:
        transport = new_zcode_transport(prompt or "", options)
    model_label = getattr(options, "model", None) or "zcode"
    translator = ZCodeEventTranslator(model_label)
    try:
        await transport.spawn()
        await transport.start_session()
        translator.session_id = transport._session_id or ""
        await transport.send_prompt()
        while True:
            kind, payload = await transport.next_event()
            if kind == "eof":
                break
            if kind == "error":
                raise ClaudeSDKError(
                    f"zcode transport error: {payload}{transport._stderr_note()}"
                )
            if kind != "event":
                continue
            event_type = str((payload or {}).get("type") or "")
            for message in translator.handle(payload):
                yield message
            if event_type in ("turn.completed", "turn.failed"):
                break
        if translator.fatal_error:
            raise ClaudeSDKError(
                f"zcode turn failed: {translator.fatal_error}"
                f"{transport._stderr_note()}"
            )
        if not translator.saw_turn_completed:
            if translator.text_so_far().strip():
                for message in translator.result_messages():
                    yield message
            else:
                raise ClaudeSDKError(
                    "zcode session ended without a completed turn"
                    f"{transport._stderr_note()}"
                )
        else:
            for message in translator.result_messages():
                yield message
    finally:
        await transport.close()