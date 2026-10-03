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

- The codex run is always ``-s read-only``: filesystem *writes* (Worker /
  crossover Edit scopes) are not available under this transport yet — the
  read-audit chain stays at the prompt layer exactly as today.
- Effort mapping: ``POK_LLM_EFFORT`` (official GLM档位 ``low``/``high``/
  ``max``) is forwarded as ``-c model_reasoning_effort=...`` (default
  ``max``).  ``POK_LLM_THINKING_BUDGET`` is not forwarded (codex has no
  fixed-budget CLI switch, mirroring the GLM-5.3 effort-only rule).
- Endpoint/model/wire_api/auth come from the operator's ``~/.codex/config.toml``
  (official GLM Coding Plan page: base_url ``https://open.bigmodel.cn/api/v1``,
  ``wire_api="responses"``, ``experimental_bearer_token``); ``POK_CODEX_MODEL``
  may override the model per dispatch.
"""

import asyncio
import contextlib
import collections
import json
import os
import signal

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

#: Transport selector env var.  ``claude`` (default) keeps the exact existing
#: SDK path; ``codex`` routes the dispatch through this adapter.
TRANSPORT_ENV = "POK_LLM_TRANSPORT"

#: Binary override (default ``codex`` resolved via PATH).
CODEX_BIN_ENV = "POK_CODEX_BIN"

#: Optional per-dispatch model override forwarded as ``-c model=<v>``.
CODEX_MODEL_ENV = "POK_CODEX_MODEL"

#: Grace (seconds) for SIGTERM→SIGKILL escalation in transport close.
CODEX_CLOSE_GRACE_ENV = "POK_CODEX_CLOSE_GRACE_SEC"

#: model_reasoning_effort档位 (official GLM Coding Plan levels).
_CODEX_EFFORT_LEVELS = ("low", "high", "max")

#: Role-IO/tool-result preview bound (mirrors _process_stream previews).
_TOOL_RESULT_PREVIEW_CHARS = 3000

#: Maximum stderr tail lines retained for provider-error text.
_STDERR_TAIL_LINES = 20

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
    """

    def __init__(self, full_prompt, options):
        self._prompt = str(full_prompt or "")
        self._options = options
        self._process = None
        self._argv = None

    def build_argv(self) -> list:
        """Codex exec argv; prompt arrives on stdin (``-``)."""

        argv = [
            codex_binary_from_env(),
            "exec",
            "--json",
            "--ephemeral",
            "-s",
            "read-only",
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
        self._argv = self.build_argv()
        self._process = await asyncio.create_subprocess_exec(
            *self._argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=_project_root(self._options),
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


def new_codex_exec_transport(full_prompt, options) -> CodexExecTransport:
    """Create the codex transport owned by one provider attempt."""

    return CodexExecTransport(full_prompt, options)


async def _write_stdin(stdin, data: bytes):
    stdin.write(data)
    await stdin.drain()
    stdin.close()
    with contextlib.suppress(Exception):
        await stdin.wait_closed()


async def _drain_stderr(stream, tail: "collections.deque[str]"):
    while True:
        line = await stream.readline()
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
    unparseable_lines = 0
    try:
        while True:
            line = await process.stdout.readline()
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
